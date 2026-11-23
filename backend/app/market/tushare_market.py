"""Bounded market snapshots; exact daily limits, never +/-10% guesses.

One immutable table per HTTP response avoids thousands of per-stock writes on
every poll. Individual quotes retain that table's evidence hash and their own
source clock. Counts are read-time arithmetic over two permission-checked raw
tables, not supplier-reported aggregate counts or reconstructed ticks.
"""
import json
import re
from collections import OrderedDict
from threading import RLock
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache

from app.evidence.schemas import ReferencePayload
from app.market.protocol import InstrumentKey, SHANGHAI, SourceFailure
from app.market.reference import ReferenceBatch
from app.market.tushare import clock, day, parse_quotes, price_decimal, table

MARKET = 'tushare_market_rt'
INDICES = 'tushare_rt_indices'
LIMITS = 'tushare_market_limits'
SNAPSHOT = 'tushare.rt_market'
LIMIT_TABLE = 'tushare.market_limits'
LIVE_FAMILIES = frozenset({MARKET, INDICES})
QUOTE_FIELDS = 'ts_code,name,pre_close,high,open,low,close,vol,amount,trade_time'
PATTERN = re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)')
_COMPARISONS = OrderedDict()
_COMPARISON_LOCK = RLock()
_CURVE_FIELDS = ('limit_up', 'limit_down', 'comparable', 'source_time_max')


def comparison_memo():
    """Bounded scalar parsing within ONE read, never a second raw-table cache.

    Identical prices use the same stock tick irrespective of exchange. Source
    clocks still include the observation receipt in their key so a later
    observation cannot relax an earlier observation's future-time check.
    """
    stock = InstrumentKey('SSE', '600000', 'stock')
    clocks = lru_cache(maxsize=8192)(clock)
    fresh_for = timedelta(seconds=120)

    @lru_cache(maxsize=8192)
    def clock_facts(value, received_at):
        stamp = clocks(value, received_at)
        if stamp is None:return None,False,False
        return (stamp,stamp.astimezone(SHANGHAI).date()==received_at.astimezone(SHANGHAI).date(),
                received_at-stamp<=fresh_for)

    return {
        'prices': lru_cache(maxsize=65536)(lambda value: price_decimal(value, stock) or Decimal(0)),
        'keys': lru_cache(maxsize=8000)(instrument_key),
        'clocks': clocks,
        'clock_facts': clock_facts,
        'days': lru_cache(maxsize=32)(day),
        'limits': {},
        'limit_prices': {},
    }


def comparison_for(db, snapshot, available, memo=None, *, curve_only=False):
    # Only tiny calculated metadata is cached, never raw tables. Each key binds
    # both immutable evidence identities/hashes and the original receipt clock.
    # Callers must recheck current permissions BEFORE reaching this cache.
    full_key=(snapshot.id,snapshot.content_hash,snapshot.received_at,
              available.id if available else None,available.content_hash if available else None)
    cache_key=full_key+('curve-v1',) if curve_only else full_key
    with _COMPARISON_LOCK:
        cached=_COMPARISONS.get(cache_key)
        hit_key=cache_key
        if cached is None and curve_only:
            cached=_COMPARISONS.get(full_key)
            hit_key=full_key
        if cached is not None:
            _COMPARISONS.move_to_end(hit_key)
            return {key:cached[key] for key in _CURVE_FIELDS} if curve_only else dict(cached)
    from sqlalchemy import cast,column,func,select,Text
    from sqlalchemy.dialects.postgresql import ARRAY,JSONB,aggregate_order_by
    from app.evidence.models import EvidenceVersion
    cells=func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(
        column('value',JSONB),with_ordinality='row_number').render_derived()
    # History statistics do not need names, volume or OHLC range fields. Keep
    # all other keys and the original row order, including duplicate amounts.
    point=cells.c.value.op('-')(cast(['name','high','open','low','vol'],ARRAY(Text)))
    projected=select(func.json_agg(aggregate_order_by(point,cells.c.row_number))).select_from(
        cells).correlate(EvidenceVersion).scalar_subquery()
    present,rows=db.execute(select(EvidenceVersion.payload['rows'].is_not(None),projected).where(
        EvidenceVersion.id==snapshot.id,EvidenceVersion.content_hash==snapshot.content_hash,
        EvidenceVersion.received_at==snapshot.received_at)).one()
    if not present:raise ValueError('Missing market rows')
    result=compare_limits(rows or [],available.payload['rows'] if available else [],snapshot.received_at,
        memo=memo,limits_key=(available.id,available.content_hash) if available else None,curve_only=curve_only)
    with _COMPARISON_LOCK:
        _COMPARISONS[cache_key]=dict(result)
        _COMPARISONS.move_to_end(cache_key)
        if len(_COMPARISONS)>4096:_COMPARISONS.popitem(last=False)
    return result


def parse_market(body, received_at, *, limits=False):
    fields, rows = table(body, allow_empty_columns=True)
    if limits and not rows and (not fields or {'ts_code','trade_date','up_limit','down_limit'}.issubset(fields)):
        raise SourceFailure('daily_data_not_published')
    if not fields and not rows:
        raise SourceFailure('instrument_unavailable')
    required = {'ts_code','trade_date','up_limit','down_limit'} if limits else set(QUOTE_FIELDS.split(','))
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    # An exact cap is ambiguous: fail rather than advertise a truncated return
    # as complete, even if the supplier ignores the limit parameter.
    if len(rows) >= (8000 if limits else 6000):
        raise SourceFailure('market_scope_truncated')
    if not rows:
        raise SourceFailure('instrument_unavailable')
    seen = set()
    today = received_at.astimezone(SHANGHAI).date()
    for row in rows:
        symbol = row['ts_code']
        if not isinstance(symbol,str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',symbol):
            raise SourceFailure('instrument_mismatch')
        if symbol in seen:
            raise SourceFailure('duplicate_instrument')
        seen.add(symbol)
        if limits:
            if day(row['trade_date']) != today:
                raise SourceFailure('invalid_source_time')
            prices = []
            for key in ('up_limit','down_limit'):
                value = row.get(key)
                try:
                    number = Decimal(value) if value not in {None,''} else None
                    if number is not None and (not number.is_finite() or number < 0): raise ValueError()
                except (ValueError, ArithmeticError):
                    raise SourceFailure('invalid_number') from None
                prices.append(number)
            if all(p is not None and p > 0 for p in prices) and prices[0] < prices[1]:
                raise SourceFailure('inconsistent_price_range')
        else:
            if not PATTERN.fullmatch(symbol):
                raise SourceFailure('unexpected_instrument')
            key = instrument_key(symbol)
            parsed = parse_quotes(encoded(fields,[row]),[key],received_at)
            if parsed.rejected:
                raise SourceFailure(next(iter(parsed.rejected.values())))
    payload = ReferencePayload(dataset=LIMIT_TABLE if limits else SNAPSHOT,
        title='Tushare · 当日全市场限制价' if limits else 'Tushare · 沪深京股票批量行情',
        source_url='https://tushare.pro/document/2?doc_id=' + ('183' if limits else '372'),
        attribution='用户指定的 Tushare 兼容服务；个人已购权益',
        scope=('当日返回的价格限制，不是涨跌停家数；缺失、零价限制及无涨跌幅限制均不推算。' if limits else
               '通配符6*.SH,0*.SZ,3*.SZ,9*.BJ；代码覆盖以本批实际返回为准，不保证目录完整。每行trade_time为来源行情时点，可能包含停牌旧价；不能把采集时间当成全市场统一行情时点。') +
              '原表不可变留存；实时状态、可比范围与缺口按实际字段计算，旧证据不补造。',
        columns=[{'key':f,'label':f} for f in fields],rows=rows,observed_day=today)
    return ReferenceBatch(records=[(payload.dataset,payload,None)],expected=1,requests=1)


def encoded(fields, rows):
    return json.dumps({'code':0,'data':{'fields':fields,'items':[[row.get(f) for f in fields] for row in rows]}}).encode()


def instrument_key(symbol):
    return InstrumentKey({'SH':'SSE','SZ':'SZSE','BJ':'BSE'}[symbol[-2:]],symbol[:6],'stock')


def quote_from_snapshot(payload, identity, received_at):
    suffix = {'SSE':'SH','SZSE':'SZ','BSE':'BJ'}.get(identity.exchange)
    if identity.asset_type != 'stock' or suffix is None: return None
    symbol = identity.symbol+'.'+suffix
    rows = [row for row in payload['rows'] if row['ts_code']==symbol]
    if len(rows)!=1:return None
    parsed = parse_quotes(encoded([c['key'] for c in payload['columns']],rows),
                          [InstrumentKey(identity.exchange,identity.symbol,'stock')],received_at)
    return parsed.records[0] if parsed.records else None


def compare_limits(quotes, limits, received_at, *, memo=None, limits_key=None, curve_only=False):
    """Exact decimal equality on the same source date, including ST/20%/30%.

    Row count is the actual returned scope, not an assertion of full coverage.
    Missing/old-day prices and missing/nonpositive limits remain unknown.
    Historical curves consume only four fields; the default keeps all statistics.
    """
    current_day = received_at.astimezone(SHANGHAI).date()
    memo = memo if memo is not None else comparison_memo()
    if curve_only and any(not isinstance(row,dict) or
            row.get('amount') is not None and not isinstance(row.get('amount'),str) for row in quotes):
        # Legacy JSON numbers and malformed containers keep the original full
        # evaluation order and errors. Normal source rows use strings/nulls.
        full=compare_limits(quotes,limits,received_at,memo=memo,limits_key=limits_key)
        return {key:full[key] for key in _CURVE_FIELDS}
    cache_key = (limits_key, current_day) if limits_key is not None else None
    by_code = memo['limits'].get(cache_key) if cache_key is not None else None
    if by_code is None:
        by_code = {row['ts_code']:row for row in limits if memo['days'](row['trade_date'])==current_day}
        if cache_key is not None:
            if len(memo['limits']) >= 100:
                expired = next(iter(memo['limits']))
                memo['limits'].pop(expired)
                memo['limit_prices'].pop(expired,None)
            memo['limits'][cache_key] = by_code
            memo['limit_prices'][cache_key] = {}
    parsed_limits = memo['limit_prices'][cache_key] if cache_key is not None else {}
    counts = dict(limit_up=0,limit_down=0,advancing=0,declining=0,unchanged=0,
                  returned=len(quotes),comparable=0,unknown=0,old_day=0,fresh=0)
    times=[]
    amount_total=Decimal(0);amount_codes=set();amount_complete=True
    prices,keys,facts = memo['prices'],memo['keys'],memo['clock_facts']
    for row in quotes:
        at,current,fresh = facts(row.get('trade_time'),received_at)
        if not current:
            counts['old_day']+=1;counts['unknown']+=1;continue
        # Amounts are native CNY in this specific batch adapter. Sum only
        # distinct source codes from the actual current source day. Unknown
        # amounts invalidate the total; old-day prices and indices are absent.
        if not curve_only and row['ts_code'] not in amount_codes:
            amount_codes.add(row['ts_code'])
            raw_amount=row.get('amount')
            try:
                value=Decimal(raw_amount) if raw_amount not in {None,''} else None
                if value is None or not value.is_finite() or value<0:
                    amount_complete=False
                else:
                    amount_total+=value
            except (ValueError,ArithmeticError):
                amount_complete=False
        keys(row['ts_code'])
        price = prices(row.get('close') or '')
        previous = prices(row.get('pre_close') or '')
        if price<=0 or previous<=0:
            counts['unknown']+=1;continue
        if not curve_only:
            counts['advancing' if price>previous else 'declining' if price<previous else 'unchanged']+=1
        bounds = parsed_limits.get(row['ts_code'])
        if bounds is None:
            limit = by_code.get(row['ts_code'])
            # Parse only when a current, valid quote actually uses this row.
            # An unrelated malformed limit must not gain a new failure path.
            upper = prices(limit.get('up_limit') or '') if limit else Decimal(0)
            lower = prices(limit.get('down_limit') or '') if limit else Decimal(0)
            bounds = parsed_limits[row['ts_code']] = (upper,lower)
        upper,lower = bounds
        if upper<=0 or lower<=0:
            counts['unknown']+=1;continue
        counts['comparable']+=1;times.append(at)
        if not curve_only:counts['fresh']+=int(fresh)
        counts['limit_up']+=int(price==upper)
        counts['limit_down']+=int(price==lower)
    if curve_only:
        return {key:counts[key] for key in _CURVE_FIELDS[:-1]} | {'source_time_max':max(times) if times else None}
    return {**counts,'source_time_min':min(times) if times else None,'source_time_max':max(times) if times else None,
        'total_amount_cny':format(amount_total,'f') if amount_complete and amount_codes else None}


def breadth_series(db, target_day, now, *, latest_only=False):
    """A separate paid series; never stitch the old free series into this one.

    The limit table must already have been received at each observation. Later
    imports/corrections cannot retrospectively change an earlier point.
    """
    from sqlalchemy import select
    from app.evidence.models import EvidenceVersion
    from app.market.schemas import BreadthPoint, BreadthSeries
    from app.market.service import permission_reason, source_id
    from app.models import Provider
    market=db.get(Provider,source_id(MARKET))
    limits_provider=db.get(Provider,source_id(LIMITS))
    if market is None or permission_reason(market):return None
    def query(provider, dataset):
        return select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
            EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',
            EvidenceVersion.owner_id.is_(None),EvidenceVersion.payload['dataset'].astext==dataset,
            EvidenceVersion.payload['observed_day'].astext==target_day.isoformat(),EvidenceVersion.received_at<=now)
    snapshots=db.execute(query(market,SNAPSHOT).with_only_columns(EvidenceVersion.id,EvidenceVersion.content_hash,EvidenceVersion.received_at)
        .order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1 if latest_only else 2001))
    limit_versions=list(db.scalars(query(limits_provider,LIMIT_TABLE).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(100))) if limits_provider and not permission_reason(limits_provider) else []
    points=[]
    memo=comparison_memo()
    for snapshot in snapshots:
        available=next((version for version in limit_versions if version.received_at<=snapshot.received_at),None)
        result=comparison_for(db,snapshot,available,memo)
        if not result['comparable']:result.update(limit_up=None,limit_down=None)
        points.append(BreadthPoint(id=snapshot.id,received_at=snapshot.received_at,**result,
            limit_evidence_id=available.id if available else None))
    if not points:return None
    truncated=len(points)>2000
    points=list(reversed(points[:2000]))
    clock=lambda point:point.received_at.astimezone(SHANGHAI).strftime('%H:%M:%S')
    items=[p for p in points if '09:30:00'<=clock(p)<='11:30:59' or '13:00:00'<=clock(p)<='15:00:59']
    return BreadthSeries(observation_day=target_day,source_name=market.name,source_family=MARKET,
        scope='沪深京A股通配符实际返回范围；以当日最新价等于当日限制价计当前涨跌停，包含ST，不是曾触板家数。旧日行情、未知/零限制价不计入；可比数及行情时段见本批。',
        items=items,latest=points[-1],truncated=truncated)
