"""Atomic full-universe KPL pages, separate from the legacy top-ten archive.

No source code is copied. The verified 19-column RealRankingInfo contract is
shared with direction_sources. Historic Count is a PAGE count, current Count a
universe count. Empty terminal pages are only accepted for the dated endpoint.
"""
import hashlib
import json
import time
from decimal import Decimal

from app.evidence.schemas import ReferencePayload
from app.market import direction_sources as source
from app.market.protocol import Response, SourceFailure
from app.market.reference import ReferenceBatch

DATASET = 'market.direction.kpl_atlas_v1'
PAGE_SIZE = 30
MAX_PAGES = 14


class Pagination:
    def __init__(self, day=None, *, live=False):
        self.day = day
        self.live = live and day is None
        self.consumed = 0
        self.overlaps = 0
        self.rows = []
        self.seen = set()
        self.hashes = []
        self.receipts = []
        self.actual = None
        self.total = None
        self.finished = False

    @property
    def offset(self):
        return self.consumed if self.live else len(self.rows)

    def add(self, response):
        data = None
        try:
            data = source._unpack('direction_kpl_strength', response.body)
            if self.finished or len(self.hashes) >= MAX_PAGES:
                raise SourceFailure('market_scope_truncated')
            if not isinstance(data, dict) or 'errcode' not in data:
                raise SourceFailure('schema_changed')
            if str(data['errcode']) != '0':
                raise SourceFailure('upstream_unavailable')
            days = data.get('Day')
            if not isinstance(days, list) or len(days) != 1:
                raise SourceFailure('source_date_mismatch')
            actual = source._day(days[0], response.received_at)
            if actual != (self.day or self.actual or actual):
                raise SourceFailure('source_date_mismatch')
            self.actual = actual
            raw = data.get('list')
            if not isinstance(raw, list) or len(raw) > PAGE_SIZE:
                raise SourceFailure('schema_changed')
            count = int(source._number(data.get('Count'), integer=True, signed=False, maximum=Decimal(400)))
            if self.day is None:
                if count < 1 or (self.total is not None and count != self.total):
                    raise SourceFailure('kpl_universe_changed')
                self.total = count
            elif count != len(raw):
                raise SourceFailure('kpl_page_count_mismatch')
            if raw:
                normalized, _, _ = source._kpl(data, response.received_at, self.day, check_order=not self.live)
                if len({r['code'] for r in normalized}) != len(normalized):
                    raise SourceFailure('duplicate_instrument')
                for item, original in zip(normalized, raw, strict=True):
                    if item['code'] in self.seen:
                        if not self.live:
                            raise SourceFailure('duplicate_instrument')
                        self.overlaps += 1
                        self.rows = [r for r in self.rows if r['code'] != item['code']]
                    if not self.live and self.rows and Decimal(item['strength']) > Decimal(self.rows[-1]['strength']):
                        raise SourceFailure('kpl_ranking_changed')
                    amount = source._number(original[5], signed=False)
                    pct = source._number(original[3])
                    item.update(rank=str(len(self.rows)+1), amount=amount, pct_change=pct,
                        amount_unit='元', coverage='complete_pages',
                        archive_kind='daily' if self.day else 'intraday',
                        page_received_at=response.received_at.isoformat())
                    self.rows.append(item)
                    self.seen.add(item['code'])
            self.consumed += len(raw)
            self.hashes.append(hashlib.sha256(response.body).hexdigest())
            self.receipts.append(response.received_at)
            if self.day is None:
                size = self.consumed if self.live else len(self.rows)
                if size > self.total or len(raw) < PAGE_SIZE and size != self.total:
                    raise SourceFailure('market_scope_truncated')
                self.finished = size == self.total
            else:
                self.finished = len(raw) < PAGE_SIZE
                if self.finished:
                    self.total = len(self.rows)
            if not self.rows or len(self.rows) > 400 or not self.finished and len(self.hashes) >= MAX_PAGES:
                raise SourceFailure('market_scope_truncated')
        except SourceFailure as error:
            error.direction_diagnostic = source._diagnostic('direction_kpl_strength',response.body,data,error)
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            failure=SourceFailure('schema_changed')
            failure.direction_diagnostic=source._diagnostic('direction_kpl_strength',response.body,data,error)
            raise failure from None

    def batch(self):
        if not self.finished or not self.rows:
            raise SourceFailure('market_scope_truncated')
        partial = self.live and len(self.rows) != self.total
        if self.live:
            self.rows.sort(key=lambda r: (-Decimal(r['strength']), r['code']))
            for rank, row in enumerate(self.rows, 1):
                row.update(rank=str(rank), coverage='partial_pages' if partial else 'complete_pages',
                           rank_basis='sampled_strength', sampled_count=str(len(self.rows)),
                           cross_page_duplicates=str(self.overlaps))
        columns = [dict(key=k,label=k,unit='元' if k=='amount' else '%' if k=='pct_change' else None)
                   for k in self.rows[0]]
        rows = [{**r, 'total_count':str(self.total), 'page_count':str(len(self.hashes))} for r in self.rows]
        columns += [dict(key=k,label=k) for k in ('total_count','page_count')]
        payload = ReferencePayload(dataset=DATASET, title='开盘啦完整题材排行',
            source_url=source.KPL_HISTORY_URL if self.day else source.KPL_URL,
            attribution='开盘啦公开题材接口；本人本机研究', observed_day=self.actual,
            scope='ZSType=7/Type=1/Order=1；历史以短末页封口，当前核对Count总数。'
                  '实时分页允许排名移动，跨页同代码保留后页观察，再按采样强度排序；少于Count标partial_pages，不冒充全榜。'
                  'amount为来源排行第6字段、元；非成分求和。盘中分页为连续观察，非同一原子行情时刻；'
                  '未提供可验证的独立源时刻，页接收时刻分别保留。领涨通过独立来源标记归档，不从强度或涨幅推算。',
            columns=columns, rows=rows)
        # Preserve the legacy endpoint and schema, but publish only after ALL
        # pages validate. New readers never treat old top-ten evidence as full.
        legacy = payload.model_copy(update={'dataset':'market.direction.kpl_strength',
            'title':'开盘啦题材强度前10','rows':rows[:10]})
        return ReferenceBatch(records=[(DATASET,payload,None),
            ('market.direction.kpl_strength',legacy,None)],expected=2,requests=len(self.hashes),rejected=int(partial))


def fetch(*, day=None, before_request, include_leaders=False, include_counts=False, leader_codes=None,
          on_failure=None, saved_rows=None):
    pages = Pagination(day, live=day is None)
    requests = 0
    batch = None
    response = None
    last_failure = None
    count_hashes = []
    def request_page(**kwargs):
        nonlocal requests
        for attempt in range(2):
            if requests:
                time.sleep(4 if attempt else 2.05)
            if not before_request():
                from app.market.realtime_schedule import RealtimeWait
                raise RealtimeWait()
            requests+=1
            try:
                return source.fetch('direction_kpl_strength',day=day,**kwargs)
            except SourceFailure as error:
                if on_failure is not None and error.reason in {'rate_limited','retry_after_out_of_range'}:
                    on_failure(error)
                if attempt or error.reason not in {'transport_error','timeout'}:
                    raise
    try:
        if saved_rows is None:
            while not pages.finished:
                response = request_page(index=pages.offset)
                pages.add(response)
            batch = pages.batch()
            if include_counts and day is None:
                from app.market.kpl_limit_counts import parse as parse_count
                for row in pages.rows:
                    row.update(limit_up_count=None, limit_up_received_at=None)
                # The visible strength leaders update every round. This bounded
                # extra work cannot multiply into hundreds of requests per minute.
                for row in sorted(pages.rows, key=lambda r: -Decimal(r['strength']))[:10]:
                    try:
                        detail = request_page(plate_code=row['code'], plate_summary=True)
                        row.update(limit_up_count=parse_count(detail, pages.actual),
                                   limit_up_received_at=detail.received_at.isoformat())
                        count_hashes.append(hashlib.sha256(detail.body).hexdigest())
                        response = detail
                    except SourceFailure as error:
                        if error.reason not in {'schema_changed', 'source_date_mismatch',
                                'upstream_unavailable', 'transport_error', 'timeout',
                                'response_too_large', 'unsafe_upstream_response'}:
                            raise
            batch=pages.batch()
        else:
            # Trusted caller supplies validated daily ranking rows; repairs
            # never fetch or replace the already complete ranking again.
            if day is None or not saved_rows or len(saved_rows)>400:
                raise SourceFailure('invalid_repair_scope')
            pages.rows, pages.actual = saved_rows, day
            batch=ReferenceBatch(records=[],expected=0,requests=0)
        if include_leaders:
            from app.market.kpl_leaders import parse, DATASET as LEADERS
            codes=set(leader_codes or [])
            if saved_rows is None:
                codes.update(r['code'] for r in pages.rows[:10])
            if saved_rows is not None and (len(codes)>10 or not codes):
                raise SourceFailure('invalid_repair_scope')
            for row in [r for r in pages.rows if r['code'] in codes]:
                batch.expected+=1
                try:
                    detail=request_page(plate_code=row['code'])
                    payload=parse(detail,row['code'],pages.actual,daily=day is not None)
                    batch.records.append((LEADERS+':'+row['code'],payload,None))
                    if payload.rows[0]['coverage'] != 'complete':
                        batch.rejected+=1
                    pages.hashes.append(hashlib.sha256(detail.body).hexdigest())
                    response=detail
                except SourceFailure as error:
                    last_failure=error
                    # Only data/transport failures may leave a recoverable
                    # partial day. Every permission, budget, lease, config or
                    # maintenance control refusal stops the entire operation.
                    if error.reason not in {'schema_changed','source_date_mismatch',
                            'upstream_unavailable','transport_error','timeout',
                            'response_too_large','unsafe_upstream_response'}:
                        raise
                    batch.rejected+=1
        batch.requests=requests
        if response is None:
            raise last_failure or SourceFailure('invalid_repair_scope')
        combined = Response(json.dumps(pages.hashes + count_hashes).encode(), response.received_at,
            int((pages.receipts[-1]-pages.receipts[0]).total_seconds()*1000) if pages.receipts else response.elapsed_ms, response.status,
            request_count=requests)
        return combined, batch
    except SourceFailure as error:
        error.request_count = requests
        error.partial_batch = batch
        error.response = response
        raise
