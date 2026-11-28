"""Authenticated review cache reads and explicit, idempotent acquisition."""
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.auth import APIError, csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import now, write_once
from app.http import envelope
from app.market.market_radar import router as market_radar_router
from app.market.review_jobs import batch_status, enqueue_collection
from app.market.review_notes import (
    ReviewNotesInput,
    notes_days,
    notes_history,
    notes_view,
    save_notes,
)
from app.market.review_protocol import ReviewCollectionInput
from app.market.new_high_collection import NewHighCollectionInput, enqueue_new_highs
from app.market.review_read import coverage_view, seat_disclosures_view
from app.market.review_store import available_trade_days
from app.schemas import Envelope, Error
from app.read_cache import cached_endpoint

router = APIRouter()
router.include_router(market_radar_router)
ERRORS = {code: {'model': Error} for code in (401, 403, 404, 409, 422, 429, 503)}
CACHED_RESPONSES = {**ERRORS, 304: {'description': 'Authorized cached representation is unchanged.'}}


def check_cutoff(db, as_of_time):
    if as_of_time is not None and as_of_time > now(db):
        raise APIError(422, 'future_cutoff', '复盘截止时间不能晚于服务器当前时间。')


@router.post('/api/v1/review-data/collections', response_model=Envelope[dict], status_code=202,
             responses=ERRORS, operation_id='collectReviewData')
def collect_review(body: ReviewCollectionInput, request: Request,
                   identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def queue(db):
        result = enqueue_collection(db, identity[0], body, request.state.request_id)
        return result, 'partial', ['采集已排队；覆盖以完整分页入库结果为准。']
    return write_once(request, identity[0], key, body, queue, status=202)


@router.get('/api/v1/review-data/collections/{batch_id}', response_model=Envelope[dict],
            responses=ERRORS, operation_id='getReviewCollection')
def collection_state(batch_id: UUID, request: Request, identity=Depends(principal),
                     db: Session=Depends(db_session)):
    result = batch_status(db, identity[0].id, batch_id)
    return envelope(request, result, 'complete' if result['state'] == 'complete' else 'partial')


@router.post('/api/v1/review-data/new-highs/collections', response_model=Envelope[dict], status_code=202,
             responses=ERRORS, operation_id='collectHundredDayHighs')
def collect_highs(body: NewHighCollectionInput, request: Request,
                  identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def queue(db):
        return enqueue_new_highs(db,identity[0],body,request.state.request_id), 'partial', ['逐日完整名单已排队。']
    return write_once(request,identity[0],key,body,queue,status=202)


def cached_response(request, result, as_of_time):
    state = result.get('state')
    quality = 'complete' if state == 'complete' else 'empty' if state in {'empty', 'missing'} else 'unavailable' if state == 'unavailable' else 'partial'
    return envelope(request, result, quality,
                    ['只读本机已核实来源缓存；源数据日、接收时间与研究判断分别保存。历史补采不代表当时已经收到。'],
                    as_of_time=as_of_time)


@router.get('/api/v1/review-data/coverage', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewCoverage')
def review_coverage(request: Request, trade_day: date | None=None, lookback: Literal['20', '60']='20',
                    as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                    db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_response(request, coverage_view(db, trade_day, int(lookback), as_of_time), as_of_time)


@router.get('/api/v1/review-data/series', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewSeries')
def review_series(request: Request, trade_day: date | None=None, lookback: Literal['20', '60']='20',
                  as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                  db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_endpoint(request, db, identity[0], 'review-series',
                           dict(trade_day=trade_day, lookback=int(lookback), as_of_time=as_of_time))


@router.get('/api/v1/review-data/overview', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getPostcloseOverview')
def postclose_overview(request: Request, trade_day: date | None=None, lookback: Literal['20', '60']='20',
                      as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                      db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    if trade_day is None:
        from app.evidence.common import now
        from app.market.postclose import default_day
        trade_day = default_day(db, as_of_time or now(db))
    return cached_endpoint(request, db, identity[0], 'review-overview',
        dict(trade_day=trade_day, lookback=int(lookback), as_of_time=as_of_time))


@router.get('/api/v1/review-data/heights', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getClosingHeightHistory')
def closing_height_history(request: Request, trade_day: date | None=None,
                           lookback: Literal['20', '60', '120']='60',
                           as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                           db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    if trade_day is None:
        from app.market.postclose import default_day
        trade_day = default_day(db, as_of_time or now(db))
    return cached_endpoint(request, db, identity[0], 'review-heights',
                           dict(trade_day=trade_day, lookback=int(lookback), as_of_time=as_of_time))


@router.get('/api/v1/review-data/new-highs', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getHundredDayHighs')
def hundred_day_highs(request: Request, trade_day: date | None=None, start_day: date | None=None,
                      as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                      db: Session=Depends(db_session)):
    check_cutoff(db,as_of_time)
    if trade_day is None:
        from app.market.postclose import default_day
        trade_day = default_day(db,as_of_time or now(db))
    if start_day and trade_day and start_day > trade_day:
        raise APIError(422,'invalid_date_range','开始日期不能晚于结束日期。')
    return cached_endpoint(request,db,identity[0],'review-new-highs',
                           dict(trade_day=trade_day,start_day=start_day,as_of_time=as_of_time))


@router.get('/api/v1/review-data/analysis', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewAnalysis')
def review_analysis(request: Request, trade_day: date,
                    kind: Literal['groups', 'ladder', 'stock', 'emotion', 'comparison']='groups',
                    lookback: Literal['20', '60']='20',
                    stock: str | None=Query(default=None, pattern=r'^\d{6}\.(?:SH|SZ)$'),
                    as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                    db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    if kind == 'stock' and stock is None:
        raise APIError(422, 'stock_required', '请选择要回看的股票')
    return cached_endpoint(request, db, identity[0], 'review-analysis',
        dict(trade_day=trade_day, kind=kind, lookback=int(lookback), stock=stock, as_of_time=as_of_time))


@router.get('/api/v1/review-data/day', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewDay')
def review_day(request: Request, trade_day: date | None=None,
               sector: str | None=Query(default=None, pattern=r'^BK\d{4,8}\.DC$'),
               stock: str | None=Query(default=None, pattern=r'^\d{6}\.(?:SH|SZ)$'),
               as_of_time: AwareDatetime | None=None, identity=Depends(principal),
               db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_endpoint(request, db, identity[0], 'review-day',
                           dict(trade_day=trade_day, sector=sector, stock=stock, as_of_time=as_of_time))


@router.get('/api/v1/review-data/lhb', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewLhb')
def review_lhb(request: Request, trade_day: date,
               stock: str | None=Query(default=None, pattern=r'^\d{6}\.(?:SH|SZ)$'),
               record_key: str | None=Query(default=None, max_length=200),
               as_of_time: AwareDatetime | None=None, identity=Depends(principal),
               db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_endpoint(request, db, identity[0], 'review-lhb',
                           dict(trade_day=trade_day, stock=stock, record_key=record_key, as_of_time=as_of_time))


@router.get('/api/v1/review-data/seat-disclosures', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewSeatDisclosures')
def review_seat_disclosures(request: Request, trade_day: date,
                            seat_name: str=Query(min_length=1, max_length=500),
                            lookback: int=Query(default=5, ge=1, le=20),
                            min_amount_yuan: Decimal | None=Query(default=None, ge=0),
                            as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                            db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_response(request, seat_disclosures_view(db, trade_day, seat_name, lookback,
                                                         min_amount_yuan, as_of_time), as_of_time)


@router.get('/api/v1/review-data/stocks', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewStocks')
def review_stocks(request: Request, trade_day: date,
                  codes: str=Query(default='', max_length=300,
                      pattern=r'^(?:\d{6}\.(?:SH|SZ)(?:,\d{6}\.(?:SH|SZ)){0,29})?$'),
                  as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                  db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_endpoint(request, db, identity[0], 'review-stocks',
                           dict(trade_day=trade_day, codes=codes.split(',') if codes else [], as_of_time=as_of_time))


@router.get('/api/v1/review-data/theme-history', response_model=Envelope[dict], responses=CACHED_RESPONSES,
            operation_id='getReviewThemeHistory')
def review_theme_history(request: Request, trade_day: date,
                         sector_codes: str=Query(default='', max_length=150,
                             pattern=r'^(?:BK\d{4,8}\.DC(?:,BK\d{4,8}\.DC){0,9})?$'),
                         as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                         db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_endpoint(request, db, identity[0], 'review-theme-history',
                           dict(trade_day=trade_day, sector_codes=sector_codes.split(',') if sector_codes else [], as_of_time=as_of_time))


@router.get('/api/v1/review-data/notes', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewNotes')
def review_notes(request: Request, trade_day: date, revision_id: UUID | None=None,
                 as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                 db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    return cached_response(request, notes_view(db, identity[0].id, trade_day, revision_id, as_of_time), as_of_time)


@router.get('/api/v1/review-data/notes/history', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewNoteHistory')
def review_note_history(request: Request, trade_day: date, identity=Depends(principal),
                        db: Session=Depends(db_session)):
    return cached_response(request, notes_history(db, identity[0].id, trade_day), None)


@router.get('/api/v1/review-data/notes/days', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewNoteDays')
def review_note_days(request: Request, trade_day: date, lookback: Literal['20', '60']='60',
                     as_of_time: AwareDatetime | None=None, identity=Depends(principal),
                     db: Session=Depends(db_session)):
    check_cutoff(db, as_of_time)
    days = available_trade_days(db, limit=int(lookback), end_day=trade_day, as_of_time=as_of_time)
    if trade_day not in days:
        days = []
    return cached_response(request, notes_days(db, identity[0].id, days, as_of_time), as_of_time)


@router.post('/api/v1/review-data/notes', response_model=Envelope[dict], responses=ERRORS,
             status_code=201, operation_id='saveReviewNotes')
def review_save_notes(body: ReviewNotesInput, request: Request, identity=Depends(csrf),
                      key=Depends(business_key)):
    require_role(identity[0], 'admin', 'researcher')
    return write_once(request, identity[0], key, body,
                      lambda db: save_notes(db, identity[0], body, request.state.request_id))


@router.get('/api/v1/review-data/seat-labels', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getReviewSeatLabelSources')
def review_seat_label_sources(request: Request, trade_day: date, codes: str=Query('', max_length=223, pattern=r'^(?:\d{6}(?:,\d{6}){0,31})?$'), identity=Depends(principal),
                              db: Session=Depends(db_session)):
    from app.config import settings
    from app.market.protocol import SHANGHAI
    from app.market.review_store import permitted_provider
    from app.market.review_seat_labels import review_seat_labels
    clock = now(db).astimezone(SHANGHAI)
    if trade_day > clock.date():
        raise APIError(422, 'invalid_trade_day', '交易日不能晚于服务器当前日期。')
    config = settings()
    if (config.deployment_mode != 'local' or config.app_mode != 'real'
            or not config.data_collection_enabled or not config.seat_label_lookup_enabled
            or not codes or not permitted_provider(db, 'tushare_top_inst')
            or not permitted_provider(db, 'tushare_top_list')):
        return envelope(request, dict(day=trade_day.isoformat(), rows=[], kpl_status='unavailable'), 'unavailable')
    return envelope(request, review_seat_labels(trade_day, tuple(sorted(set(codes.split(',')) - {''}))), 'partial')
