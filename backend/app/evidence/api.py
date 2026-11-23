from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import AwareDatetime
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence import schemas as s
from app.evidence import service
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor, now, write_once
from app.evidence.models import CalendarVersion, EvidenceVersion, Snapshot, SourceHealth, TradingSession
from app.http import envelope
from app.models import Provider
from app.schemas import Envelope, Error, Page

router = APIRouter()
READ_ERRORS = {401: {"model": Error}, 404: {"model": Error}, 422: {"model": Error}, 503: {"model": Error}}
WRITE_ERRORS = {**READ_ERRORS, 403: {"model": Error}, 409: {"model": Error}}


def business_key(value: str = Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9._:-]+$")):
    return value


def visible_evidence(owner_id):
    from app.provider_access import cloud_share_sql
    return or_(EvidenceVersion.owner_id == owner_id, and_(EvidenceVersion.visibility == "shared",
        Provider.entitlement_status == "verified", Provider.enabled.is_(True), Provider.rights["display"].astext == "allowed", cloud_share_sql()))


def paged(db, stmt, model, owner_id, filters, field, cursor, limit):
    scope = cursor_scope(owner_id, filters)
    position = decode_cursor(cursor, scope)
    column = getattr(model, field)
    if position:
        stmt = stmt.where(tuple_(column, model.id) < position)
    rows = list(db.scalars(stmt.order_by(column.desc(), model.id.desc()).limit(limit + 1)))
    return rows[:limit], encode_cursor(rows[limit - 1], scope, field) if len(rows) > limit else None


@router.post("/api/v1/evidence", response_model=Envelope[s.EvidenceVersion], status_code=201, responses=WRITE_ERRORS, operation_id="createEvidence")
def create_evidence(body: s.EvidenceInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return write_once(request, identity[0], key, body, lambda db: service.import_evidence(db, identity[0], body, request.state.request_id))


@router.get("/api/v1/evidence", response_model=Envelope[Page[s.EvidenceVersion]], responses=READ_ERRORS, operation_id="listEvidence")
def list_evidence(request: Request, kind: s.Kind | None = None, source_key: str | None = Query(default=None, max_length=200),
                  as_of_time: AwareDatetime | None = None, cursor: str | None = Query(default=None, max_length=512),
                  limit: int = Query(default=100, ge=1, le=500), identity=Depends(principal), db: Session = Depends(db_session)):
    stmt = select(EvidenceVersion).join(Provider, Provider.id == EvidenceVersion.source_id).where(visible_evidence(identity[0].id))
    if kind:
        stmt = stmt.where(EvidenceVersion.kind == kind)
    if source_key:
        stmt = stmt.where(EvidenceVersion.source_key == source_key)
    if as_of_time:
        if as_of_time > now(db):
            raise APIError(422, "future_cutoff", "查询截止时间不能晚于服务器当前时间。")
        stmt = stmt.where(EvidenceVersion.received_at <= as_of_time, EvidenceVersion.available_at <= as_of_time,
                          EvidenceVersion.historical_eligible.is_(True), or_(EvidenceVersion.event_at.is_(None), EvidenceVersion.event_at <= as_of_time))
    rows, next_cursor = paged(db, stmt, EvidenceVersion, identity[0].id,
        {"kind": kind, "key": source_key, "as_of_time": as_of_time, "resource": "evidence"}, "received_at", cursor, limit)
    return envelope(request, {"items": service.evidence_dtos(db, rows), "next_cursor": next_cursor},
        "partial" if any(not row.historical_eligible for row in rows) else "complete" if rows else "empty",
        ["列表含未核验的外部材料，不等于当时已知事实"] if any(not row.historical_eligible for row in rows) else [], as_of_time=as_of_time)


@router.get("/api/v1/evidence/{evidence_id}", response_model=Envelope[s.EvidenceVersion], responses=READ_ERRORS, operation_id="getEvidence")
def get_evidence(evidence_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(EvidenceVersion).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.id == evidence_id, visible_evidence(identity[0].id)))
    if not row:
        raise APIError(404, "not_found", "材料不存在或不可访问。")
    return envelope(request, service.evidence_dtos(db, [row])[0], "complete" if row.historical_eligible else "partial",
                    [] if row.historical_eligible else ["外部材料尚未核验，不进入严格历史快照"])


@router.get("/api/v1/source-health", response_model=Envelope[Page[s.SourceHealth]], responses=READ_ERRORS, operation_id="listSourceHealth")
def list_source_health(request: Request, source_id: UUID | None = None, cursor: str | None = Query(default=None, max_length=512),
                       limit: int = Query(default=100, ge=1, le=500), identity=Depends(principal), db: Session = Depends(db_session)):
    stmt = select(SourceHealth).where(SourceHealth.owner_id == identity[0].id)
    if source_id:
        stmt = stmt.where(SourceHealth.source_id == source_id)
    rows, next_cursor = paged(db, stmt, SourceHealth, identity[0].id, {"source": source_id, "resource": "health"}, "checked_at", cursor, limit)
    return envelope(request, {"items": [s.SourceHealth.model_validate(row) for row in rows], "next_cursor": next_cursor}, "complete" if rows else "empty")


@router.post("/api/v1/snapshots", response_model=Envelope[s.JobReceipt], status_code=202, responses=WRITE_ERRORS, operation_id="createSnapshot")
def create_snapshot(body: s.SnapshotInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    selection = service.snapshot_selection(identity[0].id, body)
    return write_once(request, identity[0], key, body, lambda db: service.queue_snapshot(db, identity[0], selection, request.state.request_id), status=202, as_of_time=body.as_of_time)


@router.get("/api/v1/snapshots", response_model=Envelope[Page[s.Snapshot]], responses=READ_ERRORS, operation_id="listSnapshots")
def list_snapshots(request: Request, cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=100, ge=1, le=500),
                   identity=Depends(principal), db: Session = Depends(db_session)):
    rows, next_cursor = paged(db, select(Snapshot).where(Snapshot.owner_id == identity[0].id), Snapshot, identity[0].id,
                             {"resource": "snapshots"}, "recorded_at", cursor, limit)
    return envelope(request, {"items": [service.snapshot_dto(row) for row in rows], "next_cursor": next_cursor}, "complete" if rows else "empty")


@router.get("/api/v1/snapshots/{snapshot_id}", response_model=Envelope[s.Snapshot], responses=READ_ERRORS, operation_id="getSnapshot")
def get_snapshot(snapshot_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(Snapshot).where(Snapshot.id == snapshot_id, Snapshot.owner_id == identity[0].id))
    if not row:
        raise APIError(404, "not_found", "快照不存在或不可访问。")
    data = service.snapshot_dto(row)
    return envelope(request, data, data.quality.status, data.quality.reasons, as_of_time=row.as_of_time)


@router.post("/api/v1/calendar-versions", response_model=Envelope[s.CalendarVersion], status_code=201, responses=WRITE_ERRORS, operation_id="createCalendarVersion")
def create_calendar(body: s.CalendarInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin")
    return write_once(request, identity[0], key, body, lambda db: service.import_calendar(db, identity[0], body, request.state.request_id))


@router.get("/api/v1/calendar-versions", response_model=Envelope[Page[s.CalendarVersion]], responses=READ_ERRORS, operation_id="listCalendarVersions")
def list_calendars(request: Request, exchange: s.Exchange | None = None, cursor: str | None = Query(default=None, max_length=512),
                   limit: int = Query(default=100, ge=1, le=500), identity=Depends(principal), db: Session = Depends(db_session)):
    stmt = select(CalendarVersion)
    if exchange:
        stmt = stmt.where(CalendarVersion.exchange == exchange)
    rows, next_cursor = paged(db, stmt, CalendarVersion, identity[0].id, {"resource": "calendars", "exchange": exchange}, "recorded_at", cursor, limit)
    return envelope(request, {"items": [s.CalendarVersion.model_validate(row) for row in rows], "next_cursor": next_cursor},
                    "partial" if rows else "empty", ["日历版本由本机管理员导入，交易所来源未自动核验"] if rows else [])


@router.get("/api/v1/trading-sessions", response_model=Envelope[s.CalendarLookup], responses=READ_ERRORS, operation_id="getTradingSession")
def get_trading_session(request: Request, exchange: s.Exchange, trade_day: date, calendar_version: str = Query(min_length=1, max_length=200),
                        identity=Depends(principal), db: Session = Depends(db_session)):
    result = service.session_lookup(db, exchange, calendar_version, trade_day)
    return envelope(request, result, "partial" if result.session else "unavailable", ["人工导入日历，来源未自动核验"] if result.session else [result.reason])


@router.get("/api/v1/calendar-versions/{calendar_id}/next-session", response_model=Envelope[s.CalendarLookup], responses=READ_ERRORS, operation_id="getNextSession")
def get_next_session(calendar_id: UUID, request: Request, after: date, identity=Depends(principal), db: Session = Depends(db_session)):
    calendar = db.get(CalendarVersion, calendar_id)
    if not calendar:
        raise APIError(404, "not_found", "日历版本不存在。")
    row = db.scalar(select(TradingSession).where(TradingSession.calendar_id == calendar_id, TradingSession.trade_day > after,
        TradingSession.is_open.is_(True)).order_by(TradingSession.trade_day).limit(1)) if calendar.coverage_start <= after <= calendar.coverage_end else None
    result = s.CalendarLookup(calendar_id=calendar.id, exchange=calendar.exchange, requested_day=after,
        calendar_version=calendar.version, session=s.SessionInput.model_validate(row) if row else None,
        reason=None if row else "覆盖范围内没有可确定的后续开市日")
    return envelope(request, result, "partial" if row else "unavailable", ["人工导入日历，来源未自动核验"] if row else [result.reason])


@router.get("/api/v1/calendar-versions/{calendar_id}/elapsed", response_model=Envelope[s.ElapsedTime], responses=READ_ERRORS, operation_id="getElapsedTradingSeconds")
def elapsed_seconds(calendar_id: UUID, request: Request, start_at: AwareDatetime, end_at: AwareDatetime,
                     identity=Depends(principal), db: Session = Depends(db_session)):
    calendar = db.get(CalendarVersion, calendar_id)
    if not calendar:
        raise APIError(404, "not_found", "日历版本不存在。")
    if end_at < start_at:
        raise APIError(422, "invalid_time_range", "结束时间不能早于开始时间。")
    zone = ZoneInfo("Asia/Shanghai")
    start, end = start_at.astimezone(zone), end_at.astimezone(zone)
    if start.date() < calendar.coverage_start or end.date() > calendar.coverage_end:
        return envelope(request, s.ElapsedTime(seconds=None, reason="时间范围超出日历覆盖，不能推断交易秒数"), "unavailable", ["日历覆盖不足"])
    rows = db.scalars(select(TradingSession).where(TradingSession.calendar_id == calendar_id,
        TradingSession.trade_day >= start.date(), TradingSession.trade_day <= end.date(), TradingSession.is_open.is_(True))).all()
    total = 0
    for row in rows:
        for segment in row.segments:
            if segment["kind"] != "continuous":
                continue
            first = datetime.combine(row.trade_day, time.fromisoformat(segment["start"]), zone)
            last = datetime.combine(row.trade_day, time.fromisoformat(segment["end"]), zone)
            total += max(0, int((min(end, last) - max(start, first)).total_seconds()))
    return envelope(request, s.ElapsedTime(seconds=total, reason=None), "partial", ["按人工日历声明的连续交易时段计算，不含竞价与休市"])
