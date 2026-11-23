"""Bounded, atomic THS membership snapshots. Latest membership is not history."""
from hashlib import sha256
import json
import re

from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch
from app.market.tushare import table

PAGE_SIZE = 5000
MAX_PAGES = 256
DATASET = 'tushare.ths_member'
MANIFEST = DATASET + '.manifest'
DOC = 'https://tushare.pro/document/2?doc_id=261'


def request_page(call):
    """Retry only transport failures; each call obtains a fresh supplier permit."""
    for attempt in range(3):
        try:
            return call()
        except SourceFailure as exc:
            if exc.reason not in {'transport_error','timeout','upstream_unavailable'} or attempt==2:
                raise


class MembershipPages:
    def __init__(self):
        self.pages = []
        self.pairs = set()
        self.finished = False

    @property
    def params(self):
        return {'limit': PAGE_SIZE, 'offset': len(self.pages) * PAGE_SIZE}

    def add(self, response):
        if self.finished or len(self.pages) >= MAX_PAGES:
            raise SourceFailure('pagination_limit_reached')
        fields, rows = table(response.body, allow_empty_columns=True)
        if len(rows) > PAGE_SIZE or rows and not {'ts_code', 'con_code', 'con_name'}.issubset(fields):
            raise SourceFailure('schema_changed')
        seen = set()
        for row in rows:
            # The all-board endpoint also returns Hong Kong constituents.
            # Preserve those source rows; only A-share codes can match stocks
            # and only directory type N/exchange A can supply display labels.
            # Foreign index variants include suffixes, e.g. 700012R.TI.
            # Do not coerce vendor identifiers to six-digit mainland codes.
            if not re.fullmatch(r'[A-Z0-9]{1,16}\.TI', row.get('ts_code') or '') or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,24}\.[A-Z]{1,5}', row.get('con_code') or ''):
                raise SourceFailure('instrument_mismatch')
            pair = (row['ts_code'], row['con_code'])
            if pair in self.pairs or pair in seen:
                raise SourceFailure('pagination_overlap')
            seen.add(pair)
        if self.pages and response.received_at < self.pages[-1][1]:
            raise SourceFailure('invalid_source_time')
        self.pairs.update(seen)
        self.pages.append((rows, response.received_at, sha256(response.body).hexdigest()))
        self.finished = len(rows) < PAGE_SIZE
        if self.finished and not self.pairs:
            raise SourceFailure('daily_data_not_published')

    def batch(self):
        if not self.finished:
            raise SourceFailure('pagination_limit_reached')
        capture = sha256(json.dumps([(stamp.isoformat(), digest) for _, stamp, digest in self.pages]).encode()).hexdigest()
        observed = self.pages[-1][1].astimezone(SHANGHAI).date()
        batch = ReferenceBatch(expected=len(self.pages)+1, requests=len(self.pages))
        def payload(dataset, rows):
            return ReferencePayload(dataset=dataset, title='同花顺概念成分', source_url=DOC,
                attribution='用户指定的 Tushare 兼容服务；个人已购权益',
                scope='当前成分分页快照；接收日不代表历史纳入日。仅目录中同花顺概念类型N参与题材展示。',
                observed_day=observed, columns=[{'key':k,'label':k} for k in rows[0]], rows=rows)
        for index, (rows, stamp, digest) in enumerate(self.pages):
            if rows:
                values=[{**row,'capture_id':capture,'page_received_at':stamp.isoformat(),'page_sha256':digest} for row in rows]
                batch.records.append((f'ths_member:page:{index}', payload(DATASET, values), None))
        meta=[{'capture_id':capture,'pages':str(sum(bool(rows) for rows,_,_ in self.pages)),
               'memberships':str(len(self.pairs))}]
        batch.records.append(('ths_member:manifest',payload(MANIFEST,meta),None))
        batch.expected=len(batch.records)
        return batch


def attach_themes(reader, target, stocks):
    """Use exactly one completed capture, bounded by receipt and selected day."""
    from datetime import datetime, time, timedelta
    from app.evidence.models import EvidenceVersion
    from app.market.sector_atlas import _directory
    from app.market import read_facets
    cutoff=min(reader.cutoff,datetime.combine(target+timedelta(days=1),time.min,SHANGHAI)-timedelta(microseconds=1))
    directory = _directory(reader.db, cutoff)
    if directory is None:
        return
    names={row['ts_code']:row['name'] for row in directory.payload.get('rows',[])
           if row.get('type')=='N' and row.get('exchange')=='A' and row.get('name')}
    base=reader.query({'tushare_ths_member'},'reference')
    manifest=reader.db.scalar(read_facets.with_facets(base).where(read_facets.value('dataset')==MANIFEST,
        read_facets.observed_day()<=target.isoformat()).order_by(
            EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
    if manifest is None:
        return
    meta=manifest.payload['rows'][0]
    cache_key=_theme_index_key(reader,base,directory,manifest)
    if cache_key is not None:
        from app import read_cache_payloads
        cached=read_cache_payloads.get(cache_key)
        if cached is not None:
            reader.record_input(directory);reader.record_input(manifest)
            _apply_theme_index(reader,stocks,manifest,cached)
            return
    page_ids=list(reader.db.scalars(read_facets.with_facets(base.with_only_columns(EvidenceVersion.id)).where(
        read_facets.value('dataset')==DATASET,
        read_facets.value('ths_member_capture_id')==meta['capture_id'])))
    if len(page_ids)!=int(meta['pages']):
        return
    reader.record_input(directory)
    reader.record_input(manifest)
    themes={}
    labels=[];label_ids={};members={};inputs=[]
    # Verify completeness using IDs, then decode one page at a time. The
    # provider's worldwide directory must not fill the workbench process heap.
    pages=reader.db.scalars(base.where(EvidenceVersion.id.in_(page_ids)).execution_options(yield_per=1))
    for page in pages:
        reader.record_input(page)
        if cache_key is not None:
            from app.evidence.reference_summary import reference_summary
            inputs.append(dict(id=str(page.id),source_id=str(page.source_id),
                content_hash=page.content_hash,received_at=page.received_at.isoformat(),
                reference_summary=reference_summary(page.payload)))
        for row in page.payload.get('rows',[]):
            name=names.get(row['ts_code'])
            if name and cache_key is not None:
                if name not in label_ids:
                    label_ids[name]=len(labels);labels.append(name)
                members.setdefault(row['con_code'],set()).add(label_ids[name])
            if name and row['con_code'] in stocks:
                themes.setdefault(row['con_code'],set()).add(name)
    for code, values in themes.items():
        stocks[code].ths_themes=sorted(values)
        stocks[code].ths_themes_received_at=manifest.received_at
    if cache_key is not None:
        read_cache_payloads.put(cache_key,dict(labels=labels,
            members={code:sorted(values) for code,values in members.items()},inputs=inputs),protected=True)


def _theme_index_key(reader,base,directory,manifest):
    """Reuse derived shared names, never a permission or a cutoff decision."""
    from sqlalchemy import func,select
    from app.config import settings
    from app.evidence.models import EvidenceVersion
    from app.read_cache_models import ReadDependency
    if not settings().read_cache_enabled:return None
    revision=reader.db.scalar(select(ReadDependency.revision).where(
        ReadDependency.key=='family:tushare_ths_member'))
    if revision is None:return None
    # A row already inserted with a later receipt can become visible without
    # another dependency bump. Bind the cache to the visible receipt frontier.
    visible=reader.db.scalar(base.with_only_columns(func.max(EvidenceVersion.received_at)))
    return ('ths-theme-index-v2',str(directory.id),directory.content_hash,
        str(manifest.id),manifest.content_hash,revision,visible.isoformat() if visible else None)


def _apply_theme_index(reader,stocks,manifest,index):
    from datetime import datetime
    from types import SimpleNamespace
    for item in index['inputs']:
        reader.record_input(SimpleNamespace(id=item['id'],source_id=item['source_id'],
            content_hash=item['content_hash'],received_at=datetime.fromisoformat(item['received_at']),
            reference_summary=item['reference_summary']))
    for code,stock in stocks.items():
        positions=index['members'].get(code)
        if positions:
            stock.ths_themes=sorted(index['labels'][position] for position in positions)
            stock.ths_themes_received_at=manifest.received_at
