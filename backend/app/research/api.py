import json
import re
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import WRITE_ERRORS, paged, business_key
from app.evidence.common import write_once
from app.evidence.models import Snapshot
from app.http import envelope
from app.research import schemas as s, service
from app.research.models import DocumentRevision, ResearchDocument
from app.schemas import Envelope, Page

router = APIRouter()
READ_ERRORS = {key: value for key, value in WRITE_ERRORS.items() if key not in {403, 409}}
EDIT_ERRORS = {**WRITE_ERRORS, 412: WRITE_ERRORS[409], 428: WRITE_ERRORS[409]}
ETAG = {"ETag": {"schema": {"type": "string", "pattern": r'^"[1-9][0-9]*"$'}, "description": "Document head revision number; an idempotent replay returns the original ETag."}}


def parse_etag(value):
    if value is None:
        raise APIError(428, "precondition_required", "请先读取文档当前版本，再带版本号保存或发布。")
    if not re.fullmatch(r'"[1-9][0-9]{0,8}"', value):
        raise APIError(422, "invalid_etag", "版本号格式不正确，请重新读取文档。")
    return int(value[1:-1])


def response_etag(result):
    data = json.loads(result.body)["data"]
    result.headers["ETag"] = '"' + str(data.get("head_revision_number", data.get("document_head_revision_number"))) + '"'
    return result


@router.post("/api/v1/research-documents", response_model=Envelope[s.Document], status_code=201,
             responses={**WRITE_ERRORS, 201: {"headers": ETAG}}, operation_id="createResearchDocument")
def create_document(body: s.DocumentInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    return response_etag(write_once(request, identity[0], key, body,
        lambda db: service.create_document(db, identity[0], body, request.state.request_id)))


@router.get("/api/v1/research-documents", response_model=Envelope[Page[s.Document]], responses=READ_ERRORS, operation_id="listResearchDocuments")
def list_documents(request: Request, kind: s.Kind | None = None, source_trade_day: date | None = None, target_trade_day: date | None = None,
                   cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=100, ge=1, le=500),
                   identity=Depends(principal), db: Session = Depends(db_session)):
    stmt = select(ResearchDocument).where(ResearchDocument.owner_id == identity[0].id)
    if kind:
        stmt = stmt.where(ResearchDocument.kind == kind)
    if source_trade_day:
        stmt = stmt.where(ResearchDocument.source_trade_day == source_trade_day)
    if target_trade_day:
        stmt = stmt.where(ResearchDocument.target_trade_day == target_trade_day)
    rows, next_cursor = paged(db, stmt, ResearchDocument, identity[0].id,
        {"resource": "research_documents", "kind": kind, "source_trade_day": source_trade_day, "target_trade_day": target_trade_day}, "created_at", cursor, limit)
    return envelope(request, {"items": service.document_dtos(db, rows), "next_cursor": next_cursor}, "complete" if rows else "empty")


@router.get("/api/v1/research-documents/{document_id}", response_model=Envelope[s.Document], responses={**READ_ERRORS, 200: {"headers": ETAG}}, operation_id="getResearchDocument")
def get_document(document_id: UUID, request: Request, response: Response, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(ResearchDocument).where(ResearchDocument.id == document_id, ResearchDocument.owner_id == identity[0].id))
    if not row:
        raise APIError(404, "not_found", "研究文档不存在或不可访问。")
    response.headers["ETag"] = '"' + str(row.head_revision_number) + '"'
    return envelope(request, service.document_dtos(db, [row])[0])


@router.post("/api/v1/research-documents/{document_id}/revisions", response_model=Envelope[s.Revision], status_code=201,
             responses={**EDIT_ERRORS, 201: {"headers": ETAG}}, operation_id="createResearchRevision")
def create_revision(document_id: UUID, body: s.RevisionInput, request: Request, if_match: str | None = Header(default=None),
                    identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    expected = parse_etag(if_match)
    return response_etag(write_once(request, identity[0], key, {"input": body, "if_match": expected},
        lambda db: service.create_revision(db, identity[0], document_id, body, expected, request.state.request_id)))


@router.get("/api/v1/research-documents/{document_id}/revisions", response_model=Envelope[Page[s.RevisionSummary]],
            responses={**READ_ERRORS, 200: {"headers": ETAG}}, operation_id="listResearchRevisions")
def list_revisions(document_id: UUID, request: Request, response: Response,
                   cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=100, ge=1, le=500),
                   identity=Depends(principal), db: Session = Depends(db_session)):
    document = db.scalar(select(ResearchDocument).where(ResearchDocument.id == document_id, ResearchDocument.owner_id == identity[0].id))
    if not document:
        raise APIError(404, "not_found", "研究文档不存在或不可访问。")
    rows, next_cursor = paged(db, select(DocumentRevision).where(DocumentRevision.document_id == document_id,
        DocumentRevision.owner_id == identity[0].id), DocumentRevision, identity[0].id,
        {"resource": "research_revisions", "document": document_id}, "revision_number", cursor, limit)
    response.headers["ETag"] = '"' + str(document.head_revision_number) + '"'
    return envelope(request, {"items": service.revision_summaries(db, rows), "next_cursor": next_cursor}, "complete" if rows else "empty")


@router.get("/api/v1/revisions/{revision_id}", response_model=Envelope[s.Revision], responses={**READ_ERRORS, 200: {"headers": ETAG}}, operation_id="getResearchRevision")
def get_revision(revision_id: UUID, request: Request, response: Response, identity=Depends(principal), db: Session = Depends(db_session)):
    row = db.scalar(select(DocumentRevision).where(DocumentRevision.id == revision_id, DocumentRevision.owner_id == identity[0].id))
    if not row:
        raise APIError(404, "not_found", "研究修订不存在或不可访问。")
    document = db.get(ResearchDocument, row.document_id)
    response.headers["ETag"] = '"' + str(document.head_revision_number) + '"'
    body = service.revision_input(db, row)
    snapshot = db.get(Snapshot, row.snapshot_id)
    quality, reasons = service.revision_quality(snapshot, body)
    return envelope(request, service.revision_dto(db, row, document), quality, reasons)


@router.post("/api/v1/revisions/{revision_id}/publish", response_model=Envelope[s.Revision], responses={**EDIT_ERRORS, 200: {"headers": ETAG}}, operation_id="publishResearchRevision")
def publish_revision(revision_id: UUID, request: Request, body: s.PublishInput = s.PublishInput(), if_match: str | None = Header(default=None),
                     identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], "admin", "researcher")
    expected = parse_etag(if_match)
    return response_etag(write_once(request, identity[0], key, {"input": body, "if_match": expected},
        lambda db: service.publish(db, identity[0], revision_id, expected, request.state.request_id), status=200))
