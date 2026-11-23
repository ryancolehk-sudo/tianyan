"""Stage bulletins rebuilt from immutable, paired auction receipts.

One successful receipt per stage is frozen. Later stages replace it in the
view, while source versions keep it after close and across restarts. Reads do
not collect, write a report, call a model, or send an external message.
"""
import re
from datetime import date
from typing import Literal
from uuid import UUID
from decimal import Decimal

from pydantic import AwareDatetime
from sqlalchemy import case, func, select

from app.evidence.common import now
from app.evidence.models import EvidenceVersion as Evidence
from app.market.auction_source import FAMILY, MAX_ROWS, STOCKS_DATASET, SUMMARY_DATASET
from app.market.protocol import SHANGHAI
from app.market.terminal_auction import STAGES, stock_code
from app.models import Provider
from app.schemas import DTO

Stage = Literal['09:15', '09:20', '09:25']
RULES_VERSION = 'auction-reference-v1'


class AuctionReferenceStock(DTO):
    code: str
    name: str
    tags: list[str]
    node_amounts: dict[str, str | None]
    seal_cny: str
    change_cny: str | None = None


class AuctionReferenceTheme(DTO):
    name: str
    total_cny: str
    members: list[AuctionReferenceStock]


class AuctionReferenceReport(DTO):
    trade_day: date
    stage: Stage
    received_at: AwareDatetime
    source_name: str
    source_update_time: AwareDatetime | None = None
    batch_hash: str
    evidence_ids: list[UUID]
    rules_version: str = RULES_VERSION
    row_count: int
    valid_count: int
    totals: dict[str, str | None]
    total_change_cny: str | None
    themes: list[AuctionReferenceTheme]
    strongest: list[AuctionReferenceStock]
    additions: list[AuctionReferenceStock]
    reductions: list[AuctionReferenceStock]
    ready_for_trade: Literal[False] = False
    recovered: bool = False


class AuctionReferenceView(DTO):
    requested_day: date | None = None
    state: Literal['available', 'empty', 'unavailable']
    report: AuctionReferenceReport | None = None
    reason: str | None = None


def amount(value):
    return Decimal(value) if isinstance(value, str) and re.fullmatch(r'\d{1,24}(?:\.\d{1,2})?', value) else None


def summarize(header, rows, *, stage, received_at, source_name, evidence_ids):
    """Independent implementation of the user's dated-table observation logic."""
    index = STAGES.index(stage)
    day = received_at.astimezone(SHANGHAI).date()
    if (header.get('trade_day') != day.isoformat() or header.get('table_kind') != 'current'
            or not rows or len(rows) > MAX_ROWS or str(len(rows)) != header.get('row_count')):
        return None
    digest = header.get('batch_hash')
    codes = [stock_code(row.get('code')) for row in rows]
    if (not digest or None in codes or len(set(codes)) != len(codes)
            or any(row.get('trade_day') != day.isoformat() or row.get('batch_hash') != digest for row in rows)):
        return None
    totals = {key: amount(header.get('total_fd_' + key.replace(':', '') + '_cny'))
              if i <= index else None for i, key in enumerate(STAGES)}
    if totals[stage] is None:
        return None
    valid = []
    for code, row in zip(codes, rows):
        values = {key: amount(row.get('fd_' + key.replace(':', '') + '_cny'))
                  if i <= index else None for i, key in enumerate(STAGES)}
        if values[stage] is None or not row.get('name'):
            continue
        previous = values[STAGES[index - 1]] if index else None
        tags = list(dict.fromkeys(tag for tag in (row.get('tags_raw') or '').split('|') if tag))
        valid.append(AuctionReferenceStock(code=code, name=row['name'], tags=tags,
            node_amounts={k: str(v) if v is not None else None for k, v in values.items()},
            seal_cny=str(values[stage]), change_cny=str(values[stage] - previous) if previous is not None else None))
    if not valid:
        return None
    ranked = sorted(valid, key=lambda row: (-Decimal(row.seal_cny), row.code))
    themes = {}
    for row in valid:
        for tag in row.tags:
            if not re.fullmatch(r'(?:\d+板|首板|昨\d+板|昨首板)', tag):
                themes.setdefault(tag, []).append(row)
    grouped = {}
    for name, members in themes.items():
        key = tuple(sorted(row.code for row in members))
        if key in grouped:
            grouped[key]['names'].append(name)
        else:
            grouped[key] = dict(names=[name], members=members, total=sum(Decimal(row.seal_cny) for row in members))
    theme_rows = sorted(grouped.values(), key=lambda value: (-value['total'], '/'.join(value['names'])))[:3]
    previous_total = totals[STAGES[index - 1]] if index else None
    return AuctionReferenceReport(trade_day=day, stage=stage, received_at=received_at, source_name=source_name,
        recovered=received_at.astimezone(SHANGHAI).strftime('%H:%M:%S') >= '09:30:00',
        batch_hash=digest, evidence_ids=evidence_ids, row_count=len(rows), valid_count=len(valid),
        totals={k: str(v) if v is not None else None for k, v in totals.items()},
        total_change_cny=str(totals[stage] - previous_total) if previous_total is not None else None,
        themes=[AuctionReferenceTheme(name='/'.join(value['names']), total_cny=str(value['total']),
            members=sorted(value['members'], key=lambda row: (-Decimal(row.seal_cny), row.code))[:2]) for value in theme_rows],
        strongest=[row for row in ranked if Decimal(row.seal_cny) > 0][:5],
        additions=sorted((row for row in valid if row.change_cny is not None and Decimal(row.change_cny) > 0),
                         key=lambda row: (-Decimal(row.change_cny), row.code))[:3],
        reductions=sorted((row for row in valid if row.change_cny is not None and Decimal(row.change_cny) < 0),
                          key=lambda row: (Decimal(row.change_cny), row.code))[:3])


def reference_view(db, *, trade_day=None, as_of_time=None):
    from app.market.service import permission_reason
    clock = now(db)
    cutoff = min(clock, as_of_time) if as_of_time else clock
    through = min(trade_day or cutoff.astimezone(SHANGHAI).date(), cutoff.astimezone(SHANGHAI).date())
    native = native_reference(db,through,cutoff,trade_day)
    if native is not None:
        return native
    providers = {row.id: row for row in db.scalars(select(Provider).where(Provider.family == FAMILY))
                 if not permission_reason(row)}
    if not providers:
        return AuctionReferenceView(requested_day=trade_day, state='unavailable', reason='竞价封单来源未启用或读取权限不可用')
    local = func.timezone('Asia/Shanghai', Evidence.received_at)
    minute = func.to_char(local, 'HH24:MI:SS')
    receipt_day = func.to_char(local, 'YYYY-MM-DD')
    stage_order = case((minute >= '09:25:00', 2), (minute >= '09:20:00', 1), else_=0)
    base = select(Evidence).where(Evidence.source_id.in_(providers), Evidence.kind == 'reference',
        Evidence.owner_id.is_(None), Evidence.visibility == 'shared', Evidence.provenance == 'source_reported',
        Evidence.received_at <= cutoff)
    # Small summary payloads only. Keep the earliest valid receipt within a
    # stage; same-day late recovery may supply genuine supplier-labelled nodes,
    # but retains its actual receipt and is explicitly marked recovered.
    candidates = base.where(Evidence.payload['dataset'].astext == SUMMARY_DATASET,
        Evidence.payload['observed_day'].astext == receipt_day, receipt_day <= through.isoformat(),
        minute >= '09:15:00').order_by(receipt_day.desc(), stage_order.desc(),
        Evidence.received_at, Evidence.version_number, Evidence.id).limit(512)
    for summary in db.scalars(candidates):
        headers = summary.payload.get('rows') or []
        if len(headers) != 1:
            continue
        header = headers[0]
        day = summary.received_at.astimezone(SHANGHAI).date()
        source_key = f'reference:auction:{FAMILY}:{day}:'
        if summary.source_key != source_key + 'summary':
            continue
        clock_text = summary.received_at.astimezone(SHANGHAI).strftime('%H:%M:%S')
        stage = next(value for value in reversed(STAGES) if value + ':00' <= clock_text)
        if amount(header.get('total_fd_' + stage.replace(':', '') + '_cny')) is None:
            continue
        stocks = db.scalar(base.where(Evidence.source_id == summary.source_id,
            Evidence.source_key == source_key + 'stocks', Evidence.received_at == summary.received_at,
            Evidence.payload['dataset'].astext == STOCKS_DATASET,
            Evidence.payload['rows'][0]['batch_hash'].astext == header.get('batch_hash'))
            .order_by(Evidence.version_number, Evidence.id).limit(1))
        if stocks is None:
            continue
        report = summarize(header, stocks.payload.get('rows') or [], stage=stage,
            received_at=summary.received_at, source_name=providers[summary.source_id].name,
            evidence_ids=[summary.id, stocks.id])
        if report is not None:
            return AuctionReferenceView(requested_day=trade_day, state='available', report=report)
    return AuctionReferenceView(requested_day=trade_day, state='empty', reason='尚无已保存的竞价阶段观察')


def native_reference(db, through, cutoff, requested_day):
    from app.market.fuyao_auction import FAMILY as NATIVE_FAMILY, DATASET
    from app.market.terminal_read import SavedTables
    from app.market.service import permission_reason
    providers={p.id:p for p in db.scalars(select(Provider).where(Provider.family==NATIVE_FAMILY)) if not permission_reason(p)}
    if not providers: return None
    local=func.timezone('Asia/Shanghai',Evidence.received_at)
    minute=func.to_char(local,'HH24:MI:SS')
    receipt_day=func.to_char(local,'YYYY-MM-DD')
    stage_order=case((minute>='09:25:00',2),(minute>='09:20:00',1),else_=0)
    query=select(Evidence.id,Evidence.received_at).where(Evidence.source_id.in_(providers),
        Evidence.kind=='reference',Evidence.owner_id.is_(None),Evidence.visibility=='shared',
        Evidence.provenance=='source_reported',Evidence.received_at<=cutoff,
        Evidence.payload['dataset'].astext==DATASET,Evidence.payload['observed_day'].astext==receipt_day,
        receipt_day<=str(through),minute>='09:15:00').order_by(receipt_day.desc(),stage_order.desc(),Evidence.received_at).limit(16)
    for _,received in db.execute(query):
        day=received.astimezone(SHANGHAI).date()
        reader=SavedTables(db,received,day)
        batch=reader.auction.native.batch(day)
        if batch is None or not batch.rows or not batch.stages: continue
        stage=next(value for value in reversed(STAGES) if value in batch.stages)
        report=summarize(batch.header,batch.rows,stage=stage,received_at=received,source_name=batch.source,
            evidence_ids=batch.evidence_ids)
        if report is not None:
            return AuctionReferenceView(requested_day=requested_day,state='available',report=report)
    return None
