from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import READ_ERRORS, WRITE_ERRORS, business_key
from app.evidence.common import cursor_scope, decode_cursor, encode_cursor, write_once
from app.http import envelope
from app.review_archives import schemas as s, service
from app.review_archives.models import ReviewArchive
from app.schemas import Envelope, Page

router = APIRouter()


@router.post("/api/v1/review-archives", response_model=Envelope[s.ReviewArchive], status_code=201,
             responses=WRITE_ERRORS, operation_id="importReviewArchive")
def import_archive(body: s.ArchiveInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return write_once(request, identity[0], key, body, lambda db: service.import_archive(db, identity[0], body, request.state.request_id))


@router.get("/api/v1/review-archives", response_model=Envelope[Page[s.ArchiveSummary]], responses=READ_ERRORS, operation_id="listReviewArchives")
def list_archives(request: Request, target_trade_day: date | None = None, cursor: str | None = Query(default=None, max_length=512),
                  limit: int = Query(default=30, ge=1, le=100), identity=Depends(principal), db: Session = Depends(db_session)):
    owner = identity[0].id
    scope = cursor_scope(owner, {"resource": "review_archives", "target_trade_day": target_trade_day})
    position = decode_cursor(cursor, scope)
    stmt = select(ReviewArchive).where(ReviewArchive.owner_id == owner)
    if target_trade_day:
        stmt = stmt.where(ReviewArchive.target_trade_day == target_trade_day)
    if position:
        anchor = db.scalar(select(ReviewArchive).where(ReviewArchive.owner_id == owner, ReviewArchive.id == position[1], ReviewArchive.imported_at == position[0]))
        if anchor is None:
            raise APIError(422, "invalid_cursor", "归档列表位置已失效，请从第一页读取。")
        stmt = stmt.where(tuple_(ReviewArchive.target_trade_day, ReviewArchive.source_version, ReviewArchive.imported_at, ReviewArchive.id) <
            (anchor.target_trade_day, anchor.source_version, anchor.imported_at, anchor.id))
    rows = list(db.scalars(stmt.order_by(ReviewArchive.target_trade_day.desc(), ReviewArchive.source_version.desc(),
        ReviewArchive.imported_at.desc(), ReviewArchive.id.desc()).limit(limit + 1)))
    following = encode_cursor(rows[limit - 1], scope, "imported_at") if len(rows) > limit else None
    return envelope(request, {"items": [service.summary(row) for row in rows[:limit]], "next_cursor": following}, "partial" if rows else "empty",
        ["旧项目归档按适用日与原版号排序，不表示当前有效预案"] if rows else [])


@router.get("/api/v1/review-archives/{archive_id}", response_model=Envelope[s.ReviewArchive], responses=READ_ERRORS, operation_id="getReviewArchive")
def get_archive(archive_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(ReviewArchive).where(ReviewArchive.id == archive_id, ReviewArchive.owner_id == identity[0].id))
    if row is None:
        raise APIError(404, "not_found", "复盘归档不存在或不可访问。")
    return envelope(request, service.details(row), "partial", ["历史研究条件仅供回查，不代表当前可执行；本次阅读未调用模型"])
