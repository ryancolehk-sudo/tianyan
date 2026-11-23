"""Immutable received catalog captures; current origins are not past membership.

A capture describes the validated rows actually returned by one catalog HTTP.
It is not an exchange-verified list of every stock trading on its receipt day.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
import uuid

from sqlalchemy import select

from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import CNINFO_CATALOG_URL, InstrumentKey, SHANGHAI, SourceFailure
from app.models import Provider

FAMILIES = frozenset({'cninfo', 'tushare_stock_basic'})
MEMBERS = 'instrument.catalog.members'
MANIFEST = 'instrument.catalog.capture'
PAGE_SIZE = 5000
MAX_MEMBERS = 20000
_ROW_KEYS = ('capture_id', 'page', 'exchange', 'symbol', 'asset_type', 'name', 'category', 'org_id', 'reported_listing_status')


@dataclass(frozen=True)
class CatalogScope:
    family: str
    source_id: uuid.UUID
    source_version_id: uuid.UUID
    manifest_id: uuid.UUID
    page_ids: tuple[uuid.UUID, ...]
    received_at: datetime
    capture_id: str
    scope_sha256: str
    body_sha256: str
    expected: int
    rejected: dict[str, int]
    instruments: tuple[InstrumentKey, ...]
    names: tuple[str, ...]


def capture_payloads(db, provider, batch, body, received_at):
    """Build before writes so an invalid scope cannot partially update names."""
    if provider.family not in FAMILIES or not isinstance(body, bytes) or not body:
        raise SourceFailure('invalid_catalog_capture')
    if (received_at.tzinfo is None or not 0 <= len(batch.records) <= MAX_MEMBERS
            or batch.expected != len(batch.records) + len(batch.rejected)):
        raise SourceFailure('invalid_catalog_capture')
    records = sorted(batch.records, key=lambda row:row.instrument.identity)
    identities = [row.instrument.identity for row in records]
    if len(set(identities)) != len(identities) or any(row.instrument.asset_type != 'stock' for row in records):
        raise SourceFailure('invalid_catalog_capture')
    rows = [dict(exchange=row.instrument.exchange, symbol=row.instrument.symbol,
        asset_type=row.instrument.asset_type, name=row.name, category=row.category,
        org_id=row.org_id,reported_listing_status='L' if provider.family=='tushare_stock_basic' else None) for row in records]
    source_version = db.scalar(select(SourceVersion).where(SourceVersion.source_id == provider.id,
        SourceVersion.recorded_at <= received_at).order_by(SourceVersion.recorded_at.desc(), SourceVersion.id.desc()).limit(1))
    body_hash = hashlib.sha256(body).hexdigest()
    rejected = dict(sorted(Counter(batch.rejected.values()).items()))
    capture = content_hash(dict(source=str(provider.id), body=body_hash, received_at=received_at,
        rows=rows, source_version=str(source_version.id) if source_version else None,
        expected=batch.expected,rejected=rejected))
    reasons = json.dumps(rejected, ensure_ascii=True, separators=(',', ':'))
    if len(reasons) > 500:
        raise SourceFailure('invalid_catalog_capture')
    url = CNINFO_CATALOG_URL if provider.family == 'cninfo' else 'https://tushare.pro/document/2?doc_id=25'
    scope = ('一次目录HTTP实际有效股票范围；不含程序附带的四指数。接收日不是上市状态或历史纳入日。'
        '拒收/排除项和原生正文hash独立保留，不能从累计标的记录推定本批成员或交易所在市全集。')
    def payload(dataset, values):
        return ReferencePayload(dataset=dataset, title='标的目录采集范围', source_url=url,
            attribution='巨潮公开目录' if provider.family == 'cninfo' else '本人已有Tushare兼容服务目录',
            scope=scope, observed_day=received_at.astimezone(SHANGHAI).date(),
            columns=[dict(key=key,label=key) for key in values[0]], rows=values)
    pages=[]
    for offset in range(0, len(rows), PAGE_SIZE):
        number=offset//PAGE_SIZE
        values=[dict(capture_id=capture,page=str(number),**row) for row in rows[offset:offset+PAGE_SIZE]]
        pages.append((f'catalog-scope:page:{number}',payload(MEMBERS,values)))
    manifest=dict(capture_id=capture,pages=str(len(pages)),expected=str(batch.expected),
        accepted=str(len(rows)),rejected=str(len(batch.rejected)),rejection_counts=reasons,
        body_sha256=body_hash,rows_sha256=content_hash(rows),scope_sha256=content_hash(identities),
        source_version_id=str(source_version.id) if source_version else None,
        received_at=received_at.isoformat(),schema_version='1')
    return pages+[("catalog-scope:manifest",payload(MANIFEST,[manifest]))]


def read_catalog_scope(db, family, cutoff, *, max_age):
    """Return ONE closed capture under current rights and the historical cutoff.

    Callers choose freshness and handle rejected rows explicitly. Never fall back
    to an older capture or synthesize membership from current InstrumentOrigin.
    """
    if (family not in FAMILIES or cutoff.tzinfo is None or not isinstance(max_age,timedelta)
            or not timedelta(0) < max_age <= timedelta(days=31)):
        raise SourceFailure('invalid_catalog_scope')
    from app.market.service import permission_reason, source_id
    provider=db.get(Provider,source_id(family))
    if provider is None or permission_reason(provider):
        return None
    base=select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id,
        EvidenceVersion.kind == 'reference', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.visibility == 'shared', EvidenceVersion.received_at <= cutoff)
    manifest=db.scalar(base.where(EvidenceVersion.payload['dataset'].astext == MANIFEST)
        .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
    if manifest is None or cutoff-manifest.received_at > max_age:
        return None
    try:
        if len(manifest.payload['rows']) != 1:
            return None
        meta=manifest.payload['rows'][0]
        counts={key:int(meta[key]) for key in ('pages','expected','accepted','rejected')}
        if (meta['schema_version'] != '1' or datetime.fromisoformat(meta['received_at']) != manifest.received_at
                or not 1 <= counts['accepted'] <= MAX_MEMBERS
                or counts['expected'] != counts['accepted']+counts['rejected']
                or not 0 <= counts['pages'] <= (MAX_MEMBERS+PAGE_SIZE-1)//PAGE_SIZE):
            return None
        for key in ('capture_id','body_sha256','rows_sha256','scope_sha256'):
            if not re.fullmatch(r'[a-f0-9]{64}',meta[key]):return None
        rejected=json.loads(meta['rejection_counts'])
        if (not isinstance(rejected,dict) or any(not isinstance(key,str) or type(value) is not int or value <= 0
                for key,value in rejected.items()) or sum(rejected.values()) != counts['rejected']):
            return None
        version=db.get(SourceVersion,uuid.UUID(meta['source_version_id']))
        if version is None or version.source_id != provider.id or version.recorded_at > manifest.received_at:
            return None
        pages=list(db.scalars(base.where(EvidenceVersion.payload['dataset'].astext == MEMBERS,
            EvidenceVersion.payload['rows'][0]['capture_id'].astext == meta['capture_id'])))
        if len(pages) != counts['pages'] or any(page.received_at != manifest.received_at for page in pages):
            return None
        pages.sort(key=lambda page:int(page.payload['rows'][0]['page']))
        rows=[]
        for index,page in enumerate(pages):
            values=page.payload['rows']
            if (not 0 < len(values) <= PAGE_SIZE or index+1 < counts['pages'] and len(values) != PAGE_SIZE
                    or any(set(row) != set(_ROW_KEYS) or row['capture_id'] != meta['capture_id']
                        or row['page'] != str(index) for row in values)):
                return None
            rows.extend({key:value for key,value in row.items() if key not in {'capture_id','page'}} for row in values)
        instruments=tuple(InstrumentKey(row['exchange'],row['symbol'],row['asset_type']) for row in rows)
        identities=[key.identity for key in instruments]
        if (len(rows) != counts['accepted'] or len(set(identities)) != len(rows)
                or identities != sorted(identities) or any(key.asset_type != 'stock' for key in instruments)
                or content_hash(rows) != meta['rows_sha256'] or content_hash(identities) != meta['scope_sha256']
                or content_hash(dict(source=str(provider.id),body=meta['body_sha256'],received_at=manifest.received_at,
                    rows=rows,source_version=str(version.id),expected=counts['expected'],rejected=rejected)) != meta['capture_id']):
            return None
        return CatalogScope(family,provider.id,version.id,manifest.id,tuple(page.id for page in pages),
            manifest.received_at,meta['capture_id'],meta['scope_sha256'],meta['body_sha256'],
            counts['expected'],rejected,instruments,tuple(row['name'] for row in rows))
    except (KeyError,ValueError,TypeError,AttributeError,SourceFailure):
        return None
