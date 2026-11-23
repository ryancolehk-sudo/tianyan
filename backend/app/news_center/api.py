from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import csrf, principal
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import write_once
from app.http import envelope
from app.news_center import read
from app.news_center import schemas as s
from app.news_center.service import update_state
from app.schemas import Envelope, Error
from app.read_cache import cached_endpoint
from pydantic import AwareDatetime

router = APIRouter()
ERRORS = {401: {'model': Error}, 403: {'model': Error}, 404: {'model': Error},
          409: {'model': Error}, 422: {'model': Error}, 503: {'model': Error}}
CACHED_RESPONSES = {**ERRORS, 304: {'description': 'Authorized cached representation is unchanged.'}}


@router.get('/api/v1/news/topics', response_model=Envelope[s.TopicResearchView], responses=ERRORS, operation_id='getTopicResearch')
def topic_research(request: Request, topic_source: str = Query(min_length=1, max_length=80),
    topic_key: str = Query(min_length=1, max_length=120), keyword: str = Query(min_length=1, max_length=80),
    trade_day: date = Query(), as_of_time: AwareDatetime | None = None,
    cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=30, ge=1, le=100),
    identity=Depends(principal), db: Session = Depends(db_session)):
    from app.news_center.topics import research
    data = research(db, identity[0].id, topic_source=topic_source, topic_key=topic_key, keyword=keyword,
        trade_day=trade_day, as_of_time=as_of_time, cursor=cursor, limit=limit)
    return envelope(request, data, 'partial', data['gaps'])


@router.get('/api/v1/news/items', response_model=Envelope[s.NewsFeedView], responses=CACHED_RESPONSES, operation_id='listNewsCenterItems')
def list_items(request: Request, q: str | None = Query(default=None, min_length=1, max_length=200),
    category: Literal['精选', '全部', '政策', '产业', '公司公告', '海外', '风险澄清', '财经'] | None = None,
    day: date | None = None, family: str | None = Query(default=None, max_length=80), saved: bool = False,
    unread: bool = False, following: bool = False, order: Literal['latest', 'earliest', 'selected'] = 'latest',
    cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=50, ge=1, le=100),
    identity=Depends(principal), db: Session = Depends(db_session)):
    return cached_endpoint(request, db, identity[0], 'news-items', dict(q=q, category=category,
        day=day, family=family, saved=saved, unread=unread, following=following, order=order, cursor=cursor, limit=limit))


@router.get('/api/v1/news/items/{article_id}', response_model=Envelope[s.NewsArticleView], responses=ERRORS, operation_id='getNewsCenterItem')
def get_item(article_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return envelope(request, read.get_article(db, identity[0].id, article_id), 'partial', ['保留来源原始表述'])


@router.post('/api/v1/news/items/{article_id}/state', response_model=Envelope[s.NewsArticleView], responses=ERRORS, operation_id='updateNewsReadingState')
def save_state(article_id: UUID, body: s.NewsStateUpdate, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    def save(db):
        return update_state(db, identity[0], article_id, body, request.state.request_id), 'complete', []
    return write_once(request, identity[0], key, body, save, status=200)


@router.get('/api/v1/news/editions', response_model=Envelope[s.EditionList], responses=CACHED_RESPONSES, operation_id='listNewsEditions')
def list_editions(request: Request, period: s.Period = 'day',
    cursor: str | None = Query(default=None, max_length=512), limit: int = Query(default=30, ge=1, le=100),
    identity=Depends(principal), db: Session = Depends(db_session)):
    return cached_endpoint(request, db, identity[0], 'news-editions', dict(period=period, cursor=cursor, limit=limit))


@router.get('/api/v1/news/editions/{edition_id}', response_model=Envelope[s.EditionDetail], responses=CACHED_RESPONSES, operation_id='getNewsEdition')
def get_edition(edition_id: UUID, request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    return cached_endpoint(request, db, identity[0], 'news-edition', dict(edition_id=edition_id))


@router.get('/api/v1/news/ai-status', response_model=Envelope[s.NewsAiStatus], responses=ERRORS, operation_id='getNewsAiStatus')
def ai_status(request: Request, identity=Depends(principal)):
    from app.model_connections import connection_status
    return envelope(request, connection_status())
