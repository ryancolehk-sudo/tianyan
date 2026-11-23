"""Read the existing Eastmoney cumulative-flow samples without collecting.

Snapshots retain their own source clocks. Cash changes require an actually
saved, same-source observation approximately five minutes earlier in the same
trading segment; daily tables and receipt times never supply a missing point.
"""
from dataclasses import dataclass
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import math
import re
from typing import Literal
from types import SimpleNamespace

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI
from app.models import Provider
from app.schemas import DTO

FAMILY = 'eastmoney_funds'
DATASETS = {'market.eastmoney.concept_funds': 'concept',
            'market.eastmoney.industry_funds': 'industry',
            'market.eastmoney.stock_funds': 'stock'}
MAX_ROWS = 200
SCOPE = ('东方财富主力净流入累计值；单页或未齐页组不代表完整市场，题材可能重叠。'
         '股票、二级行业和概念按实际保存的分页范围读取；覆盖数与来源总数分别核对。曲线仅绘已保存时点；'
         '近5分钟差值要求同源、同股/板块、同交易段270至330秒的两个真实样本。'
         '名次变化按概念或各级行业独立计算，仅在前后同类板块样本范围一致时提供。')


class IntradayFundSector(DTO):
    code: str
    name: str
    main_net_cny: float | None = None
    pct_change: float | None = None
    amount_cny: float | None = None
    rank: int | None = None
    member_count: int | None = None
    change_5m_cny: float | None = None
    rank_change_5m: int | None = None
    source_time: AwareDatetime
    category: Literal['concept', 'industry']
    industry_level: int | None = None


class IntradayFundPoint(DTO):
    time: AwareDatetime
    sector_code: str
    value: float | None = None


class IntradayStockFund(DTO):
    code: str
    value: float | None = None
    change_5m_cny: float | None = None
    source_time: AwareDatetime


class IntradayFundTarget(DTO):
    code: str
    category: Literal['stock', 'concept', 'industry']
    state: Literal['available', 'stale', 'preparing', 'recovering', 'unavailable']
    source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    next_attempt_at: AwareDatetime | None = None


class IntradayFundCoverage(DTO):
    category: Literal['stock', 'industry', 'concept']
    represented_count: int
    expected_count: int | None = None
    latest_batch_count: int
    complete: bool | None = None
    source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    latest_batch_source_time: AwareDatetime | None = None
    latest_batch_received_at: AwareDatetime | None = None


class IntradayFunds(DTO):
    state: Literal['available', 'stale', 'missing', 'permission_denied']
    trade_day: date
    source_family: Literal['eastmoney_funds'] = FAMILY
    source_name: str | None = None
    latest_source_time: AwareDatetime | None = None
    received_at: AwareDatetime | None = None
    scope: str = SCOPE
    sectors: list[IntradayFundSector] = Field(default_factory=list)
    points: list[IntradayFundPoint] = Field(default_factory=list)
    stocks: list[IntradayStockFund] = Field(default_factory=list)
    focused_sectors: list[IntradayFundSector] = Field(default_factory=list)
    target_states: list[IntradayFundTarget] = Field(default_factory=list)
    coverage: list[IntradayFundCoverage] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Sample:
    code: str
    name: str
    stamp: datetime
    net: Decimal | None
    pct: Decimal | None
    amount: Decimal | None
    industry_level: int | None = None


@dataclass
class Snapshot:
    evidence: object
    category: str
    rows: dict[str, Sample]
    ranking: bool = True
    inputs: tuple = ()
    coverage: dict | None = None


def legacy_coverage(payload, parsed_count):
    """Old single pages remain visible, without becoming full-market evidence."""
    raw = payload.get('rows', [])
    totals = {r.get('source_total') for r in raw if isinstance(r, dict)}
    if len(totals) != 1:
        return None
    total = next(iter(totals))
    if not isinstance(total, str) or not total.isdecimal():
        return None
    maximum = 8000 if payload.get('dataset') == 'market.eastmoney.stock_funds' else 5000
    if not len(raw) <= int(total) <= maximum:
        return None
    return dict(total=int(total), complete=parsed_count == len(raw) == int(total), legacy=True)


def decimal(value):
    if value is None or isinstance(value, bool) or isinstance(value, str) and value in {'', '-', '--'}:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and math.isfinite(float(result)) else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def numeric(value):
    return float(value) if value is not None else None


def segment(stamp):
    local = stamp.astimezone(SHANGHAI)
    minute = local.time().replace(tzinfo=None)
    if time(9, 30) <= minute < time(11, 31):
        return local.date(), 'am'
    if time(13) <= minute < time(15, 1):
        return local.date(), 'pm'
    return None


def _samples(evidence, day, cutoff, category, facts=None, *, parse_clock=datetime.fromisoformat, identities=None):
    payload = evidence.payload
    columns = {column.get('key'): column.get('unit') for column in payload.get('columns', [])
               if isinstance(column, dict)}
    rows, seen = {}, set()
    raw_rows = payload.get('rows')
    if not isinstance(raw_rows, list):
        return rows
    if len(raw_rows)>MAX_ROWS or raw_rows and isinstance(raw_rows[0],dict) and 'capture_id' in raw_rows[0]:
        from app.market.fund_universe import saved_capture
        if saved_capture(payload,evidence.received_at) is None:
            return rows
    trade_day = day.isoformat()
    for row in raw_rows:
        if not isinstance(row, dict) or row.get('trade_day') != trade_day:
            continue
        raw_stamp = row.get('source_time')
        if not isinstance(raw_stamp, str):
            continue
        try:
            stamp = parse_clock(raw_stamp)
        except ValueError:
            continue
        if stamp.utcoffset() is not None and stamp > cutoff and facts is not None:
            facts['future_seen'] = True
        if (stamp.utcoffset() is None or stamp > cutoff or stamp > evidence.received_at + timedelta(seconds=60)
                or stamp.astimezone(SHANGHAI).date() != day):
            continue
        code = row.get('code')
        if not isinstance(code, str):
            continue
        if category == 'stock':
            suffix = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}.get(row.get('exchange'))
            if suffix is None or not re.fullmatch(r'\d{6}', code):
                continue
            code += '.' + suffix
        elif re.fullmatch(r'BK\d{4}', code):
            code += '.DC'
        else:
            continue
        if identities is not None:
            code = identities.setdefault(code, code)
        if code in seen:
            # Ambiguous saved identities cannot produce a legitimate ranking.
            return {}
        seen.add(code)
        rows[code] = Sample(code=code, name=code if category == 'stock' else row.get('name') or code, stamp=stamp,
            net=decimal(row.get('net_inflow_cny')) if columns.get('net_inflow_cny') == 'CNY' else None,
            pct=decimal(row.get('change_percent')) if category != 'stock' and columns.get('change_percent') == '%' else None,
            amount=decimal(row.get('amount_cny')) if category != 'stock' and columns.get('amount_cny') == 'CNY' else None,
            industry_level=2 if category=='industry' and row.get('industry_level')=='2' else None)
    return rows


def _previous_index(snapshots, *, current=None):
    """Index only this read's already-authorized, receipt-bounded samples."""
    bounds = None
    if current is not None:
        bounds = {}
        for snapshot in current:
            for code, row in snapshot.rows.items():
                key = (snapshot.category, snapshot.evidence.source_id, code)
                lower, upper = row.stamp - timedelta(seconds=330), row.stamp - timedelta(seconds=270)
                if key in bounds:
                    lower, upper = min(lower, bounds[key][0]), max(upper, bounds[key][1])
                bounds[key] = lower, upper
    grouped = {}
    for snapshot in snapshots:
        for code, row in snapshot.rows.items():
            if bounds is not None:
                interval = bounds.get((snapshot.category, snapshot.evidence.source_id, code))
                if interval is None or not interval[0] <= row.stamp <= interval[1]:
                    continue
            span = segment(row.stamp)
            if span is not None:
                grouped.setdefault((snapshot.category, snapshot.evidence.source_id, code, span), []).append(
                    (row.stamp, snapshot, row))
    result = {}
    for key, entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        result[key] = ([entry[0] for entry in entries], entries)
    return result


def _previous(current, snapshot, snapshots, *, index=None):
    span = segment(current.stamp)
    if span is None:
        return None
    index = index if index is not None else _previous_index(snapshots)
    stamps, entries = index.get((snapshot.category, snapshot.evidence.source_id, current.code, span), ([], []))
    start = bisect_left(stamps, current.stamp - timedelta(seconds=330))
    end = bisect_right(stamps, current.stamp - timedelta(seconds=270))
    if start == end:
        return None
    # Stable sorting retains the original snapshot order for a complete tie.
    best = min(entries[start:end], key=lambda item: (
        abs((current.stamp - item[0]).total_seconds() - 300), -item[0].timestamp(),
        -item[1].evidence.received_at.timestamp()))
    return best[1:]


def _freshness_clock(cutoff, day, session):
    phase = session.get('phase') if isinstance(session, dict) else getattr(session, 'phase', None)
    local = cutoff.astimezone(SHANGHAI)
    if day == local.date() and phase == 'break':
        return datetime.combine(day, time(11, 30), SHANGHAI)
    if day == local.date() and phase == 'closed' and local.time().replace(tzinfo=None) >= time(15):
        return datetime.combine(day, time(15), SHANGHAI)
    return cutoff


def _history_snapshots(db, provider, day, cutoff):
    """Aggregate saved per-symbol archives without inventing one-symbol ranks."""
    from app.market.fund_history import saved_archives
    snapshots=[]
    parse_clock = lru_cache(maxsize=1024)(datetime.fromisoformat)
    grouped={};categories={}
    for cohort in saved_archives(db,provider,day,cutoff):
        category=cohort['category']
        scope=categories.setdefault(category,dict(targets=set(),complete=True,plans={}))
        scope['targets'].update(cohort['targets'])
        scope['complete'] &= cohort['complete']
        scope['plans'][cohort['evidence'].id]=cohort['evidence']
        for code,(evidence,rows,_) in cohort['series'].items():
            evidence_key=evidence.id
            for row in rows:
                suffix={'SSE':'SH','SZSE':'SZ'}.get(row.get('exchange'))
                identity=code+'.'+suffix if category=='stock' and suffix else code+'.DC' if category!='stock' else None
                if identity is None: continue
                stamp=parse_clock(row['source_time'])
                values,inputs=grouped.setdefault((category,stamp),({},{}))
                values[identity]=Sample(identity,row.get('name') or identity,stamp,
                    decimal(row.get('net_inflow_cny')),None,None)
                inputs[evidence_key]=evidence
    for (category,stamp),(rows,inputs) in grouped.items():
            scope=categories[category]
            evidence=next(iter(scope['plans'].values()))
            all_inputs=(*scope['plans'].values(),*inputs.values())
            # This temporary grouping never becomes an evidence identifier.
            # The original manifest and each actual response remain the inputs.
            anchor=SimpleNamespace(id=f'{evidence.id}:{stamp.isoformat()}',source_id=provider.id,
                received_at=max(value.received_at for value in all_inputs),
                version_number=max(value.version_number for value in all_inputs))
            snapshots.append(Snapshot(anchor,category,rows,
                ranking=scope['complete'] and len(rows)==len(scope['targets']),inputs=all_inputs))
    return snapshots


def selected_samples(db, provider, day, cutoff, owner_id, fresh_clock, tolerance, current):
    from app.market.fund_focus import saved
    from app.market.resilience import stamp
    evidence, jobs = saved(db, provider, day, cutoff, owner_id)
    snapshots, latest = [], {}
    for item in evidence:
        rows = item.payload.get('rows', [])
        columns = {row.get('key'): row.get('unit') for row in item.payload.get('columns', []) if isinstance(row, dict)}
        if not 1 <= len(rows) <= 242 or columns.get('net_inflow_cny') != 'CNY':
            continue
        head = rows[0]
        category, raw_code, exchange = head.get('category'), head.get('code'), head.get('exchange')
        suffix = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}.get(exchange)
        if category == 'stock' and suffix and isinstance(raw_code, str) and re.fullmatch(r'\d{6}', raw_code):
            code = raw_code + '.' + suffix
        elif category in {'concept', 'industry'} and isinstance(raw_code, str) and re.fullmatch(r'BK\d{4}', raw_code):
            code = raw_code + '.DC'
        else:
            continue
        values = []
        previous = None
        for row in rows:
            at = stamp(row.get('source_time'))
            if (row.get('category') != category or row.get('code') != raw_code or row.get('exchange') != exchange
                or row.get('trade_day') != day.isoformat() or at is None or at > cutoff or at > item.received_at
                or at.astimezone(SHANGHAI).date() != day or segment(at) is None or previous is not None and at <= previous):
                values = []
                break
            previous = at
            values.append(Snapshot(item, category, {code: Sample(code, row.get('name') or code, at,
                decimal(row.get('net_inflow_cny')), None, None)}, ranking=False))
        if values:
            snapshots.extend(values)
            candidate = values[-1]
            prior = latest.get(code)
            if prior is None or (previous, item.received_at) > (next(iter(prior.rows.values())).stamp, prior.evidence.received_at):
                latest[code] = candidate
    states = []
    for job in jobs:
        context = job.payload['funds_focus_context']
        code = context['code'] + ('.' + {'SSE':'SH', 'SZSE':'SZ', 'BSE':'BJ'}[context['exchange']] if context['category'] == 'stock' else '.DC')
        sample = latest.get(code)
        at = next(iter(sample.rows.values())).stamp if sample else None
        state = 'available' if at and (fresh_clock-at).total_seconds() <= tolerance else 'stale' if at else 'unavailable'
        if current and job.state in {'queued', 'running'}:
            state = 'recovering' if job.error_code else 'preparing'
        elif current and job.state == 'failed':
            state = 'unavailable'
        states.append(IntradayFundTarget(code=code, category=context['category'], state=state,
            source_time=at, received_at=sample.evidence.received_at if sample else None,
            next_attempt_at=job.available_at if current and job.state == 'queued' else None))
    return snapshots, latest, states


def _native_funds(db, day, cutoff, *, sector=None, compare_codes=(), session=None, input_manifest=None, facts=None):
    """Return only currently permitted, receipt-bounded, exact-date evidence."""
    from app.market.service import permission_reason
    provider = db.scalar(select(Provider).where(Provider.family == FAMILY))
    base = dict(trade_day=day, source_name=provider.name if provider else None)
    if provider is None:
        return IntradayFunds(**base, state='missing')
    if permission_reason(provider):
        return IntradayFunds(**base, state='permission_denied')
    config = db.get(SourceSettings, provider.id)
    tolerance = max(180, config.interval_seconds * 2 if config else 300)
    fresh_clock = _freshness_clock(cutoff, day, session)
    facts = facts if facts is not None else {}
    from app.market import read_facets
    # Stock curves are not drawn from this table. Defer large row bodies until
    # needed; keep scanning metadata for every independent board observation.
    query = read_facets.with_facets(select(EvidenceVersion, read_facets.value('dataset'))
        .options(defer(EvidenceVersion.payload))).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        read_facets.value('dataset').in_(DATASETS),
        read_facets.observed_day() == day.isoformat(),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(),
               EvidenceVersion.id.desc()).execution_options(yield_per=1)
    # The provider, accepted trading date and receipt cutoff bound this scan.
    # A newest-N limit across independent tables discards already saved opening
    # samples as the day progresses, and becomes worse with paged collection.
    # Stream the bounded day's evidence instead of deleting history by count.
    from app.market.industry_catalog import saved_catalog
    catalog=saved_catalog(db,provider.id,day,cutoff)
    levels={row['code']+'.DC':int(row['industry_level']) for row in catalog.payload.get('rows',[])
        if isinstance(row,dict) and row.get('industry_level') in {'1','2','3'} and isinstance(row.get('code'),str)} if catalog else {}
    def group(category,row):
        return (category, row.industry_level or levels.get(row.code) if category=='industry' else None)
    snapshots = []
    parse_clock = lru_cache(maxsize=1024)(datetime.fromisoformat)
    identities = {}
    stock_floor, stock_key = None, None
    for evidence, dataset in db.execute(query):
        category = DATASETS[dataset]
        # The parser and the reader both cap a legacy source clock at receipt
        # +60s. A receipt older than the earliest active stock clock -390s
        # cannot contain a newer ranking or any stock's 270-330s predecessor.
        # Unknown coverage takes the full safe scan instead of guessing.
        if category == 'stock' and stock_floor is not None and evidence.received_at < stock_floor:
            continue
        rows = _samples(evidence, day, cutoff, category, facts, parse_clock=parse_clock, identities=identities)
        if rows:
            from app.market.fund_universe import saved_capture
            head=evidence.payload['rows'][0]
            paged=isinstance(head,dict) and 'capture_id' in head
            capture=saved_capture(evidence.payload,evidence.received_at) if paged else legacy_coverage(evidence.payload,len(rows))
            if capture and len(rows)!=capture['total']:
                capture=dict(capture,complete=False)
            # The complete day's native payloads remain in PostgreSQL. Only the
            # parsed values used by this projection and their evidence identity
            # stay resident, rather than thousands of duplicated raw row dicts.
            identity = SimpleNamespace(**{key: getattr(evidence, key) for key in
                ('id', 'source_id', 'content_hash', 'received_at', 'version_number')})
            snapshot=Snapshot(identity, category, rows,ranking=not paged or bool(capture and capture['complete']),coverage=capture)
            snapshots.append(snapshot)
            if category == 'stock' and snapshot.ranking:
                candidate=(max(row.stamp for row in rows.values()),evidence.received_at,evidence.version_number,str(evidence.id))
                if stock_key is None or candidate > stock_key:
                    stock_key=candidate
                    stock_floor=min(row.stamp for row in rows.values())-timedelta(seconds=390) if capture else None
    history_snapshots=_history_snapshots(db,provider,day,cutoff)
    for snapshot in history_snapshots:
        # The original stock minute cohort is bounded and may cover only a
        # fraction of the live universe. Its valid points can supply a previous
        # value, but must not replace a whole-market native stock ranking.
        if snapshot.category == 'stock':
            snapshot.ranking=False
    snapshots.extend(history_snapshots)
    # A late receipt carrying an earlier source clock cannot replace the newer
    # market sample. Keep one coherent source batch for each bounded ranking.
    latest = {}
    for snapshot in snapshots:
        if not snapshot.ranking:
            continue
        key = (max(row.stamp for row in snapshot.rows.values()), snapshot.evidence.received_at,
               snapshot.evidence.version_number, str(snapshot.evidence.id))
        previous = latest.get(snapshot.category)
        if previous is None or key > previous[0]:
            latest[snapshot.category] = key, snapshot
    active = [entry[1] for entry in latest.values()]
    captures={}
    for snapshot in snapshots:
        if snapshot.coverage is None:
            continue
        key=(max(row.stamp for row in snapshot.rows.values()),snapshot.evidence.received_at)
        if snapshot.category not in captures or key>captures[snapshot.category][0]:
            captures[snapshot.category]=key,snapshot
    incomplete_capture=any(not item.coverage['complete'] for _,item in captures.values())
    coverage=[]
    for category in ('stock','industry','concept'):
        current_sample=latest.get(category,(None,None))[1]
        recent=captures.get(category,(None,current_sample))[1]
        if recent is None:
            continue
        coverage.append(IntradayFundCoverage(category=category,
            represented_count=len(current_sample.rows) if current_sample else 0,
            expected_count=recent.coverage['total'] if recent.coverage else None,
            latest_batch_count=len(recent.rows),complete=recent.coverage['complete'] if recent.coverage else None,
            source_time=max((r.stamp for r in current_sample.rows.values()),default=None) if current_sample else None,
            received_at=current_sample.evidence.received_at if current_sample else None,
            latest_batch_source_time=max((r.stamp for r in recent.rows.values()),default=None),
            latest_batch_received_at=recent.evidence.received_at))
    previous_index = _previous_index(snapshots, current=active)
    used = {}
    recorded_snapshots = set()
    def record(snapshot):
        identity = id(snapshot)
        if identity in recorded_snapshots:
            return
        recorded_snapshots.add(identity)
        for evidence in snapshot.inputs or (snapshot.evidence,):
            if evidence.id in used:
                continue
            used[evidence.id] = {'evidence_id': str(evidence.id), 'source_id': str(evidence.source_id),
                'content_hash': evidence.content_hash, 'received_at': evidence.received_at.isoformat()}
    for _,snapshot in captures.values():
        record(snapshot)
    if catalog:
        used[catalog.id]={'evidence_id':str(catalog.id),'source_id':str(catalog.source_id),
            'content_hash':catalog.content_hash,'received_at':catalog.received_at.isoformat()}
    sectors, stocks, old_values = [], [], {}
    rank_scope_matches = {}
    grouped_rows, grouped_codes = {}, {}
    def scope_rows(snapshot):
        identity = id(snapshot)
        if identity not in grouped_rows:
            groups = {}
            for item in snapshot.rows.values():
                groups.setdefault(group(snapshot.category, item), []).append(item)
            grouped_rows[identity] = groups
            grouped_codes[identity] = {key: frozenset(item.code for item in rows)
                                       for key, rows in groups.items()}
        return grouped_rows[identity]
    def scope_codes(snapshot, key):
        scope_rows(snapshot)
        return grouped_codes[id(snapshot)].get(key, frozenset())
    for snapshot in active:
        for key, rows in scope_rows(snapshot).items():
            clocks=[item.stamp for item in rows]
            rank_scope_matches[key]=(max(clocks)-min(clocks)).total_seconds()<=60
    duplicates = {code for snapshot in active if snapshot.category != 'stock' for code in snapshot.rows
                  if sum(code in candidate.rows for candidate in active if candidate.category != 'stock') > 1}
    for snapshot in active:
        record(snapshot)
        for code, row in snapshot.rows.items():
            if code in duplicates:
                continue
            paired = _previous(row, snapshot, snapshots, index=previous_index)
            difference = None
            if paired is not None:
                previous_snapshot, previous_row = paired
                record(previous_snapshot)
                if row.net is not None and previous_row.net is not None:
                    difference = numeric(row.net - previous_row.net)
            if snapshot.category == 'stock':
                stocks.append(IntradayStockFund(code=code, value=numeric(row.net),
                    change_5m_cny=difference, source_time=row.stamp))
            else:
                key=group(snapshot.category,row)
                current_scope=scope_codes(snapshot,key)
                previous_scope=scope_codes(previous_snapshot,key) if paired else frozenset()
                if (paired is None or row.net is None or previous_scope != current_scope or previous_row.net is None):
                    rank_scope_matches[key] = False
                else:
                    old_values.setdefault(key,{})[code] = previous_row.net
                sectors.append(IntradayFundSector(code=code, name=row.name, main_net_cny=numeric(row.net),
                    pct_change=numeric(row.pct), amount_cny=numeric(row.amount), change_5m_cny=difference,
                    source_time=row.stamp, category=snapshot.category,industry_level=key[1]))
    sectors.sort(key=lambda row: (row.main_net_cny is None,
        -row.main_net_cny if row.main_net_cny is not None else 0, row.code))
    previous_ranks_by_group = {key: {code: rank for rank, (code, _) in enumerate(
        sorted(values.items(), key=lambda item: (-item[1], item[0])), 1)}
        for key, values in old_values.items() if rank_scope_matches.get(key)}
    group_ranks={}
    for row in sectors:
        key=(row.category,row.industry_level)
        rank=group_ranks.get(key,0)+1
        group_ranks[key]=rank
        row.rank = rank
        previous_ranks=previous_ranks_by_group.get(key,{})
        if row.main_net_cny is not None and row.code in previous_ranks:
            row.rank_change_5m = previous_ranks[row.code] - rank
    stocks.sort(key=lambda row: (row.value is None, -row.value if row.value is not None else 0, row.code))
    plotted = {row.code for row in sectors if row.rank<=10}
    plotted.update(compare_codes)
    if sector:
        plotted.add(sector)
    points = {}
    # Same source clock corrections use the latest receipt permitted by the
    # cutoff. Never interpolate a missing interval or borrow another day.
    for snapshot in sorted(snapshots, key=lambda item: (item.evidence.received_at,
            item.evidence.version_number, str(item.evidence.id))):
        if snapshot.category == 'stock':
            continue
        for row in snapshot.rows.values():
            if row.code in plotted and row.code not in duplicates:
                points[row.code, row.stamp] = IntradayFundPoint(time=row.stamp, sector_code=row.code,
                                                              value=numeric(row.net))
                record(snapshot)
    latest_source = max((row.stamp for snapshot in active for row in snapshot.rows.values()), default=None)
    received = max((snapshot.evidence.received_at for snapshot in active), default=None)
    ranked_batches = active
    fresh = all(max(0, (fresh_clock - max(row.stamp for row in snapshot.rows.values())).total_seconds())
                <= tolerance for snapshot in ranked_batches)
    from app.market.fund_history import CAPABILITY as HISTORY_CAPABILITY
    from app.market.fund_focus import CAPABILITY as SELECTED_CAPABILITY
    attempt = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
        DataFetchRun.capability.not_in([HISTORY_CAPABILITY, SELECTED_CAPABILITY]),
        DataFetchRun.received_at <= cutoff).order_by(DataFetchRun.received_at.desc(),
        DataFetchRun.id.desc()).limit(1))
    from app.market.eastmoney_trading import CATALOG_FAILURES, CATALOG_DEFERRALS
    catalog_reasons={'rate_limited', *(f'industry_catalog_{reason}'
        for reason in CATALOG_FAILURES | CATALOG_DEFERRALS)}
    metadata_only_failure = bool(attempt and attempt.status == 'partial'
        and attempt.expected == 4 and attempt.rejected == 1
        and (attempt.accepted or 0)+(attempt.duplicates or 0) == 3
        and attempt.reason in catalog_reasons)
    partial_failure = bool(attempt and received is not None and attempt.received_at >= received
        and attempt.status == 'partial' and attempt.rejected and not metadata_only_failure)
    failed = bool(attempt and received is not None and attempt.received_at >= received
                  and (attempt.status in {'error', 'unavailable', 'empty'} or partial_failure))
    paused = bool(config and config.paused_reason)
    state = 'missing' if not active else 'available' if fresh and not paused and not failed and not incomplete_capture else 'stale'
    if input_manifest is not None:
        input_manifest.extend(used[key] for key in sorted(used,key=str))
    counts = ', '.join(('概念' if item.category == 'concept' else '行业' if item.category == 'industry' else '个股')
                      + str(len(item.rows)) + '条' for item in sorted(active, key=lambda item: item.category))
    status_note = ('采集已暂停，保留最后实际时点。' if paused else
        '本轮部分资金表未能更新，保留各表最后实际时点，不代表本轮完整快照。' if partial_failure else
        '最近采集未成功，保留最后实际时点。' if failed else
        '行业分类目录暂未更新，资金采样已保存；未核实分类的旧样本不混入二级行业。' if metadata_only_failure else '')
    for category,(_,snapshot) in sorted(captures.items()):
        status_note+=({'concept':' 概念','industry':' 二级行业','stock':' 个股'}[category])+f"本批有效覆盖{len(snapshot.rows)}/{snapshot.coverage['total']}条。"
    if incomplete_capture:
        status_note+=' 本轮分页未齐，保留已存排行和本轮真实采样；旧单页同样可能未齐，等待正常重试。'
    if any(snapshot.inputs for snapshot in snapshots):
        status_note+=' 已接入收盘后补取的实际分钟历史；补采接收时间不进入更早的当时已知回放，范围未齐前不替换原排行。'
    facts.update(has_active=bool(active),held=bool(paused or failed or incomplete_capture),levels=levels,
        clocks=[max(row.stamp for row in item.rows.values()) for item in ranked_batches],
        max_receipt=max((datetime.fromisoformat(row['received_at']) for row in used.values()),default=None),
        max_source_time=max((row.stamp for item in snapshots for row in item.rows.values()),default=None))
    return IntradayFunds(**base, state=state, latest_source_time=latest_source, received_at=received,
        sectors=sectors, stocks=stocks, coverage=coverage,
        points=sorted(points.values(), key=lambda row: (row.time, row.sector_code)),
        scope=SCOPE + '本批实际返回：' + counts + '。' + status_note)


def _native_key(db, provider, config, day, cutoff, sector, compare_codes=()):
    from app.market import read_facets
    from app.market.fund_history import HISTORY_DATASET, PLAN_DATASET, CAPABILITY as HISTORY
    from app.market.fund_focus import CAPABILITY as SELECTED
    from app.market.industry_catalog import DATASET as CATALOG
    from app.models import InstanceState
    metadata = read_facets.with_facets(select(EvidenceVersion.id, EvidenceVersion.content_hash, EvidenceVersion.received_at)).where(
        EvidenceVersion.source_id==provider.id, EvidenceVersion.kind=='reference',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility=='shared',
        EvidenceVersion.provenance=='source_reported', EvidenceVersion.received_at<=cutoff,
        read_facets.value('dataset').in_([*DATASETS,HISTORY_DATASET,PLAN_DATASET,CATALOG]),
        read_facets.value('observed_day')==day.isoformat()).order_by(EvidenceVersion.id)
    attempt = db.scalar(select(DataFetchRun.id).where(DataFetchRun.source_id==provider.id,
        DataFetchRun.capability.not_in([HISTORY,SELECTED]),DataFetchRun.received_at<=cutoff)
        .order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
    instance = db.scalar(select(InstanceState.cache_id).where(InstanceState.id==1))
    base = (instance,provider.id,provider.name,config.configured_at if config else None,
        config.paused_reason if config else None,config.interval_seconds if config else None,
        day,sector,attempt,tuple(db.execute(metadata)))
    return base + (('compare',tuple(sorted(set(compare_codes)))),) if compare_codes else base


def intraday_funds(db, day, cutoff, *, sector=None, compare_codes=(), session=None, input_manifest=None, owner_id=None, current=False):
    from app.config import settings
    from app.market.service import permission_reason
    from app.market import funds_public
    provider=db.scalar(select(Provider).where(Provider.family==FAMILY))
    if provider is None or permission_reason(provider):
        return _native_funds(db,day,cutoff,sector=sector,compare_codes=compare_codes,session=session,input_manifest=input_manifest)
    config=db.get(SourceSettings,provider.id)
    tolerance=max(180,config.interval_seconds*2 if config else 300)
    fresh_clock=_freshness_clock(cutoff,day,session)
    key=_native_key(db,provider,config,day,cutoff,sector,compare_codes) if current and settings().read_cache_enabled else None
    cached=funds_public.get(key,cutoff) if key is not None else None
    if cached:
        result,facts,inputs=cached
        fresh=all(max(0,(fresh_clock-at).total_seconds())<=tolerance for at in facts['clocks'])
        result.state='missing' if not facts['has_active'] else 'available' if fresh and not facts['held'] else 'stale'
    else:
        facts,inputs={},[]
        result=_native_funds(db,day,cutoff,sector=sector,compare_codes=compare_codes,session=session,input_manifest=inputs,facts=facts)
        if key is not None:
            funds_public.put(key,result,facts,inputs)
    samples,latest,states=selected_samples(db,provider,day,cutoff,owner_id,fresh_clock,tolerance,current)
    previous_index = _previous_index(samples)
    used={row['evidence_id'] for row in inputs}
    for code,snapshot in latest.items():
        row=snapshot.rows[code]
        paired=_previous(row,snapshot,samples,index=previous_index)
        difference=numeric(row.net-paired[1].net) if paired and row.net is not None and paired[1].net is not None else None
        if snapshot.category=='stock':
            original=next((item for item in result.stocks if item.code==code),None)
            if original is None or row.stamp>original.source_time:
                result.stocks=[item for item in result.stocks if item.code!=code]
                result.stocks.append(IntradayStockFund(code=code,value=numeric(row.net),change_5m_cny=difference,source_time=row.stamp))
        else:
            result.focused_sectors.append(IntradayFundSector(code=code,name=row.name,main_net_cny=numeric(row.net),
                change_5m_cny=difference,source_time=row.stamp,category=snapshot.category,
                industry_level=facts.get('levels',{}).get(code) if snapshot.category=='industry' else None))
    points={(row.sector_code,row.time):row for row in result.points}
    for snapshot in samples:
        code,row=next(iter(snapshot.rows.items()))
        if snapshot.category!='stock':
            points[code,row.stamp]=IntradayFundPoint(time=row.stamp,sector_code=code,value=numeric(row.net))
        evidence=snapshot.evidence
        if str(evidence.id) not in used:
            inputs.append(dict(evidence_id=str(evidence.id),source_id=str(evidence.source_id),
                content_hash=evidence.content_hash,received_at=evidence.received_at.isoformat()))
            used.add(str(evidence.id))
    result.points=sorted(points.values(),key=lambda row:(row.time,row.sector_code))
    result.target_states=states
    if input_manifest is not None:
        input_manifest.extend(inputs)
    return result
