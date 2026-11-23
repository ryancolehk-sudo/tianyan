"""Saved, permission-checked read model for the four fixed workbench views.

GET never collects. Every raw table is current-rights checked, receipt bounded,
and selected by its own trade date. Daily cash flow never becomes an intraday
curve. Large market batches are loaded only for current/5-minute prices and a
bounded overview curve; selected-stock history is projected inside PostgreSQL.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from types import SimpleNamespace
import math
from hashlib import sha256
import json

from sqlalchemy import cast, func, or_, select, true
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import undefer

from app.auth import APIError
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun, Watchlist
from app.market.models import SourceSettings
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.service import permission_reason
from app.market.terminal_schemas import (
    TerminalAuctionDay, TerminalBreadth, TerminalBreadthHistory, TerminalBreadthPoint, TerminalCapability,
    TerminalCoverage, TerminalEvent, TerminalFundPoint, TerminalIndex, TerminalSector,
    TerminalSeal, TerminalStock, TerminalView, TerminalDetails, TerminalSectorMember,
)
from app.market.trading_datasets import source_day
from app.market import read_facets, terminal_public
from app.market.tushare import SOURCE_CLOCK_LEAD_SECONDS, clock as source_clock
from app.market.tushare_market import compare_limits, comparison_for, comparison_memo
from app.models import Instrument, Provider
from app.read_cache_models import ReadDependency
from app.market.terminal_auction import AuctionTables, FAMILIES as AUCTION_NODE_FAMILIES, NODE_FIELDS, project_nodes, stock_code, header_total
from app.market.session import market_session, observation_gap_seconds, quote_fresh_at
from app.market.intraday_funds import intraday_funds
from app.market.intraday_ladder import live_ladder
from app.market.ladder_heights import verified_first_boards
from app.market.market_series import FAMILIES as MARKET_FAMILIES, DATASETS as MARKET_DATASETS
from app.market.terminal_archives import archived_members, attach_daily_pools, closing_counts, closing_table, pool_rows, relay_summary

MARKET = 'tushare_market_rt'
LIMITS = 'tushare_market_limits'
REFERENCES = frozenset({LIMITS,'tushare_limit_list_d','tushare_limit_step','tushare_stk_auction',
    'tushare_dc_index','tushare_dc_member','tushare_moneyflow_ind_dc','tushare_moneyflow_dc','tushare_ths_index','tushare_ths_member',
    'tushare_stk_premarket','tushare_daily_basic','tushare_direction_industry','tushare_direction_industry_funds'})
CALENDARS = ('tushare_calendar_sse','tushare_calendar_szse','baostock_daily','szse_public')
DIRECT_QUOTES = frozenset({'fuyao_quotes','fuyao_indices','tdx_quotes','tencent_web','tickflow_live','ashare_public','tushare_rt_k'})
ALL_FAMILIES = REFERENCES | AUCTION_NODE_FAMILIES | MARKET_FAMILIES | {'cninfo','tushare_stock_basic','tencent_auction','tushare_rt_indices','tushare_daily','tushare_index_daily','eastmoney_funds','mairui_pools','xgb_breadth_history',*CALENDARS,*DIRECT_QUOTES}
STOCK_SCOPE = '沪深京批量行情实际返回；综合盯盘返回全部已存股票，其他页面列表最多200只；涨跌与限制价统计基于全部同日有效样本。'
BREADTH_SCOPE = '沪深京实际返回（含ST）；当前价精确等于当日限制价。曲线为真实采样点的涨停/跌停家数，约每5分钟取已保存末点；缺测不补零。'
TRACKING_MEMBER_LIMIT = 8000
DETAIL_CONTRACT_VERSION = 'market-session-detail-manifest-v12-ths-themes'
CLOSING_SCOPE = '沪深京同日完整日线实际返回（含ST）；收盘价与同日原始限制价精确比较，成交额为实际返回样本汇总。盘中曲线独立保留真实采样，日线不补画缺口。'


def number(value, multiplier=1):
    """All wire numbers finite; source strings/unknowns never coerce to zero."""
    if value is None or isinstance(value,bool) or isinstance(value,str) and value in {'','-','--'}:return None
    try:
        result = Decimal(str(value))*Decimal(str(multiplier))
        value = float(result)
        return value if result.is_finite() and math.isfinite(value) else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def positive(value):
    result=number(value)
    return result if result is not None and result>0 else None


def quoted_price(value):
    result=positive(value)
    if result is None:return None
    decimal=Decimal(str(value))
    cents=decimal.quantize(Decimal('.01'))
    return number(cents) if abs(decimal-cents)<=Decimal('0.00000001') else result


def pct(price, previous):
    a,b=number(price),positive(previous)
    return number((Decimal(str(a))/Decimal(str(b))-1)*100) if a is not None and b is not None else None


def units(payload):
    return {column['key']:column.get('unit') for column in payload.get('columns',[])}


def cny(row, field, columns):
    multiplier={'元':1,'万元':10000,'亿元':100000000,'千元':1000}.get(columns.get(field))
    return number(row.get(field),multiplier) if multiplier is not None else None


def source_time(value, received_at):
    try:
        return source_clock(value,received_at)
    except SourceFailure:
        return None


def at(row, received_at):
    return source_time(row.get('trade_time'),received_at)


def segment(clock):
    if clock is None:return None
    local=clock.astimezone(SHANGHAI)
    minute=local.time().replace(tzinfo=None)
    if time(9,30)<=minute<time(11,31):return (local.date(),'am')
    if time(13)<=minute<time(15,1):return (local.date(),'pm')
    return None


def speed_5m(current, previous, received_at, previous_received_at, *, parse_time=at):
    end,start=parse_time(current,received_at),parse_time(previous,previous_received_at)
    if not end or not start or segment(end) is None or segment(start)!=segment(end):return None,None
    elapsed=(end-start).total_seconds()
    if not 270<=elapsed<=330:return None,None
    return pct(current.get('close'),previous.get('close')),elapsed


def state(row, upper, lower):
    price=quoted_price(row.get('close'))
    if price is None:return '行情缺失',None
    # Compare decimal cents exactly; floats are only the transport boundary.
    eq=lambda a,b:a is not None and b is not None and Decimal(str(a))==Decimal(str(b))
    bid,bid_volume=quoted_price(row.get('bid_price1')),number(row.get('bid_volume1'))
    ask_volume=number(row.get('ask_volume1'))
    if eq(price,upper):
        if eq(bid,upper) and bid_volume is not None and bid_volume>0 and ask_volume==0:
            return '封住',number(Decimal(str(bid))*Decimal(str(bid_volume)))
        return '涨停价（盘口待核）',None
    if eq(price,lower):return '跌停价',None
    if upper is None:return '限制价待核',None
    high=quoted_price(row.get('high'))
    if upper is not None and high is not None and high>=upper and price<upper:return '触板后回落',None
    return ('未触板' if high is not None else '触板状态待核'),None


def height(row):
    raw=row.get('nums') or row.get('limit_times')
    n=number(raw)
    return int(n) if n is not None and n.is_integer() and 1<=n<=99 else None


def main_board(code,name):
    return ((code.endswith('.SH') and code[:3] in {'600','601','603','605'}) or
            (code.endswith('.SZ') and code.startswith(('000','001','002','003')))) and 'ST' not in name.upper() and '退' not in name


@dataclass(frozen=True)
class Meta:
    id: object
    source_id: object
    family: str
    source_key: str
    received_at: datetime
    content_hash: str
    dates: frozenset


def schedule_recheck(reader, stamp):
    pending=getattr(reader,'recheck_at',None)
    if pending is not None and stamp is not None and stamp>reader.cutoff:
        pending.append(stamp)


def schedule_feed_expiry(reader, stamp, freshness):
    if getattr(reader,'recheck_at',None) is None or stamp is None:return
    boundary=(stamp+timedelta(seconds=freshness,microseconds=1)).astimezone(SHANGHAI)
    local=boundary.time().replace(tzinfo=None)
    # quote_fresh_at freezes feed age during lunch and after the close.
    if time(11,30)<local<time(13):boundary=datetime.combine(boundary.date(),time(13),SHANGHAI)
    if local>time(15):return
    schedule_recheck(reader,boundary)


def pending_market_visibility(reader, target, cutoff, snapshots):
    # The parser admits at most this existing clock lead. Older receipts cannot
    # contain a valid row waiting to become visible. Only fingerprint pending
    # row positions, anchored to their immutable batch, so wall-clock progress
    # alone does not create a new detail context.
    pending=[]
    for meta in snapshots:
        if meta.received_at + timedelta(seconds=SOURCE_CLOCK_LEAD_SECONDS) <= cutoff:
            continue
        waiting=[(index,stamp) for index,row in enumerate(reader.evidence(meta).payload.get('rows',[]))
              if (stamp:=reader.at(row,meta.received_at)) is not None
              and stamp.astimezone(SHANGHAI).date()==target and stamp>cutoff]
        if waiting:
            pending.append((str(meta.id),[index for index,_ in waiting]))
            schedule_recheck(reader,min(stamp for _,stamp in waiting))
    return sorted(pending)


def detail_manifest(reader, target, as_of_time, cutoff, routed, snapshots, snapshot=None):
    """One detail identity for every parent view and bounded detail selection.

    Parent-only intraday funds retain their own input manifest. They must not
    change the common stock-detail identity merely because a parent reads them.
    All shared evidence, permissions, calendars and cutoff rules still apply.
    """
    return dict(version=DETAIL_CONTRACT_VERSION, trade_day=target.isoformat(), as_of=str(as_of_time),
        permitted=sorted(reader.permitted),
        routing_health=reader.db.scalar(select(ReadDependency.revision).where(
            ReadDependency.key == 'routing-health')) or 0,
        catalog=reader.db.scalar(select(ReadDependency.revision).where(
            ReadDependency.key == 'catalog')) or 0,
        routed=sorted((code, str(item.provenance.evidence_version_id), item.provenance.content_hash)
                      for code, item in routed.items()),
        calendar=[day.isoformat() for day in reader.calendar_days],
        current_day=target == cutoff.astimezone(SHANGHAI).date(),
        before_close=target == cutoff.astimezone(SHANGHAI).date() and cutoff.astimezone(SHANGHAI).time() < time(15),
        references=sorted((str(meta.id), meta.content_hash) for meta in reader.reference_meta),
        auction=sorted((str(meta.id), meta.content_hash) for pair in reader.auction.pairs.values() for meta in pair),
        market=sorted((str(meta.id), meta.content_hash) for meta in snapshots),
        market_pending=pending_market_visibility(reader,target,cutoff,snapshots),
        live_snapshot=(str(snapshot[0].id),snapshot[0].content_hash,
            sorted(row['ts_code'] for row in snapshot[1])
            if len(snapshot[1])!=len(snapshot[0].payload.get('rows',[])) else None) if snapshot else None,
        supplemental=reader.supplemental_revision)


class SavedTables:
    def __init__(self,db,cutoff,requested_day=None):
        self.db,self.cutoff=db,cutoff
        self.market_family=MARKET
        self.providers={p.family:p for p in db.scalars(select(Provider).options(undefer(Provider.family)).where(Provider.family.in_(ALL_FAMILIES)))}
        self.permitted={f:p for f,p in self.providers.items() if not permission_reason(p)}
        self.cached={}
        self.input_versions={}
        # Every cache belongs to this permission-checked, fixed-cutoff read.
        # Repeated panels can reuse parsing without retaining market data or
        # authorization across requests. Receipt stays in the clock key.
        self.tables={}
        self.quote_rows={}
        self.parse_day=lru_cache(maxsize=512)(source_day)
        self.parse_clock=lru_cache(maxsize=8192)(source_time)
        self.reference_meta=[]
        # Detail caches share one revision across views. Saved review manifests
        # and supplier day lists can change without changing the last quote.
        ids=[self.permitted[f].id for f in ('tushare_limit_list_d','tushare_dc_member','mairui_pools','tushare_daily','tushare_index_daily','eastmoney_funds') if f in self.permitted]
        supplemental=read_facets.with_facets(select(EvidenceVersion.id,EvidenceVersion.content_hash,
            read_facets.value('row_dataset'),read_facets.value('requested_day'))).where(
            EvidenceVersion.source_id.in_(ids),EvidenceVersion.kind=='reference',
            EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
            EvidenceVersion.provenance=='source_reported',EvidenceVersion.received_at<=cutoff,
            or_(read_facets.value('dataset').in_({'review.pools','review.members','review.daily','review.indices',
                'market.mairui.limit_up','market.mairui.limit_down','market.mairui.broken_limit','market.eastmoney.industry_catalog'}),
                EvidenceVersion.source_key.startswith('review.manifest:pools:'),
                EvidenceVersion.source_key.startswith('review.manifest:members:'),
                EvidenceVersion.source_key.startswith('review.manifest:daily:'),
                EvidenceVersion.source_key.startswith('review.manifest:indices:')))
        supplemental_rows=list(db.execute(supplemental)) if ids else []
        self.supplemental_revision=sorted((str(ident),digest) for ident,digest,_,_ in supplemental_rows)
        self.daily_days={parsed for _,_,dataset,value in supplemental_rows if dataset=='daily'
            and (parsed:=source_day(value)) is not None and parsed<=cutoff.astimezone(SHANGHAI).date()
            and (parsed<cutoff.astimezone(SHANGHAI).date() or cutoff.astimezone(SHANGHAI).time()>=time(15))}
        # Date metadata only: do not transfer every historical 6,000-row batch.
        stmt=read_facets.with_facets(self.query(REFERENCES,'reference')).with_only_columns(EvidenceVersion.id,EvidenceVersion.source_id,
            Provider.family,EvidenceVersion.source_key,EvidenceVersion.received_at,EvidenceVersion.content_hash,
            read_facets.trade_dates()).order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.version_number.desc(),EvidenceVersion.id.desc())
        for ident,source_id,family,key,received,hash_value,dates in db.execute(stmt).yield_per(100):
            actual=frozenset(parsed for value in (dates or []) if (parsed:=source_day(value)) is not None and parsed<=cutoff.astimezone(SHANGHAI).date())
            self.reference_meta.append(Meta(ident,source_id,family,key,received,hash_value,actual))
        self.available_days=set().union(*(meta.dates for meta in self.reference_meta)) if self.reference_meta else set()
        observed=read_facets.with_facets(self.query(MARKET_FAMILIES,'reference')).where(
            read_facets.value('dataset').in_(MARKET_DATASETS.values())).with_only_columns(read_facets.observed_day()).distinct()
        self.market_days={d for value in db.scalars(observed) if (d:=source_day(value)) is not None}
        # A newly selected provider can advance to a new trade day even when
        # the previous batch supplier has stopped. Server/receipt-only clocks
        # do not contribute dates to this selector.
        quote_day=func.date(func.timezone('Asia/Shanghai',EvidenceVersion.event_at))
        from app.integrations.routing import preferred_families
        selected_quotes=set(preferred_families(db,'stock_quote',DIRECT_QUOTES)) | set(preferred_families(db,'index_quote',DIRECT_QUOTES))
        dated_quotes=self.query(selected_quotes,'quote').where(EvidenceVersion.event_at.is_not(None),
            EvidenceVersion.event_at<=cutoff).with_only_columns(quote_day).distinct().order_by(quote_day.desc()).limit(366)
        self.market_days.update(day for day in db.scalars(dated_quotes) if day is not None)
        self.available_days.update(self.market_days)
        self.available_days.update(self.daily_days)
        self.calendar_days=self.calendar()
        self.auction=AuctionTables(self)
        self.available_days.update(self.auction.dates(cutoff.astimezone(SHANGHAI).date()))

    def query(self,families,kind):
        ids=[self.permitted[f].id for f in families if f in self.permitted]
        return select(EvidenceVersion).join(Provider,Provider.id==EvidenceVersion.source_id).where(
            EvidenceVersion.source_id.in_(ids),EvidenceVersion.kind==kind,
            EvidenceVersion.owner_id.is_(None),EvidenceVersion.visibility=='shared',
            ~EvidenceVersion.source_key.startswith('review.'),
            EvidenceVersion.provenance=='source_reported',EvidenceVersion.received_at<=self.cutoff)

    def evidence(self,meta):
        if meta.id not in self.cached:
            from app.config import settings
            from app import read_cache_payloads
            # Meta was just selected through current permissions and cutoff.
            # Only immutable shared payloads are reused; never an ORM Session.
            key=(str(meta.id),meta.content_hash)
            payload=read_cache_payloads.get(key) if settings().read_cache_enabled else None
            if payload is None:
                evidence=self.db.get(EvidenceVersion,meta.id)
                payload=evidence.payload
                if settings().read_cache_enabled:read_cache_payloads.put(key,payload)
            self.cached[meta.id]=SimpleNamespace(payload=payload,id=meta.id,source_id=meta.source_id,content_hash=meta.content_hash,
                received_at=meta.received_at)
        self.record_input(meta)
        return self.cached[meta.id]

    def record_input(self,evidence):
        self.input_versions[str(evidence.id)]={'evidence_id':str(evidence.id),'source_id':str(evidence.source_id),
            'content_hash':evidence.content_hash,'received_at':evidence.received_at.isoformat()}

    def table(self,family,day):
        key=(family,day)
        if key in self.tables:return self.tables[key]
        selected={}
        for meta in self.reference_meta:
            if meta.family==family and day in meta.dates:selected.setdefault(meta.source_key,meta)
        result=[]
        # Source tables declaring daily final results cannot be used as a
        # pre-close observation of that same day, even if a provider misdates
        # a cached response. Review of older days is deliberately late-aware.
        if day==self.cutoff.astimezone(SHANGHAI).date() and self.cutoff.astimezone(SHANGHAI).time()<time(15) and family in {
            'tushare_moneyflow_ind_dc','tushare_direction_industry_funds','tushare_moneyflow_dc','tushare_limit_step','tushare_daily_basic','tushare_limit_list_d'}:
            self.tables[key]=result
            return result
        for meta in selected.values():
            evidence=self.evidence(meta)
            columns=units(evidence.payload)
            result.extend((row,columns,meta) for row in evidence.payload.get('rows',[]) if self.day(row.get('trade_date'))==day)
        self.tables[key]=result
        return result

    def at(self,row,received_at):
        value=row.get('trade_time')
        # Treat malformed saved JSON as an unknown clock before it becomes
        # an unhashable cache key that aborts the whole workbench response.
        return self.parse_clock(value,received_at) if isinstance(value,str) else None

    def day(self,value):
        return self.parse_day(value) if isinstance(value,str) else None

    def dates(self,family,through):
        return sorted({d for meta in self.reference_meta if meta.family==family for d in meta.dates if d<=through},reverse=True)

    def calendar(self):
        from app.market.calendar_selection import common_open_days,selected_days
        providers={self.permitted[family].id:family for family in CALENDARS if family in self.permitted}
        # Select the latest canonical correction before inspecting its origin
        # or payload. A non-source correction cannot revive an old open day.
        stmt=select(EvidenceVersion).where(EvidenceVersion.source_id.in_(providers),
            EvidenceVersion.kind=='calendar',EvidenceVersion.owner_id.is_(None),
            EvidenceVersion.visibility=='shared',EvidenceVersion.received_at<=self.cutoff
        ).order_by(EvidenceVersion.version_number.desc(),EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc())
        calendars,inputs=selected_days(self.db.scalars(stmt),providers)
        for evidence in inputs:
            self.record_input(evidence)
        return common_open_days(calendars)

    def previous_day(self,day):
        return next((d for d in reversed(self.calendar_days) if d<day),None)

    def market(self,day,*,historical=False):
        from app.market.market_series import choose
        self.market_family=choose(self.db,self.providers,day,self.cutoff,historical=historical)
        if self.market_family=='tencent_market_rt':
            from app.market.tencent_market import snapshot_metadata
            provider=self.permitted.get(self.market_family)
            return snapshot_metadata(self.db,provider,day,self.cutoff) if provider else []
        stmt=read_facets.with_facets(self.query({self.market_family},'reference')).where(
            read_facets.observed_day()==day.isoformat(),read_facets.value('dataset')==MARKET_DATASETS[self.market_family]).with_only_columns(
            EvidenceVersion.id,EvidenceVersion.source_id,Provider.family,EvidenceVersion.source_key,EvidenceVersion.received_at,EvidenceVersion.content_hash)
        return [Meta(*row,frozenset({day})) for row in self.db.execute(stmt.order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(2000))]

    def capability(self,key,label,family,has_data,*,status='available',reason=''):
        provider=self.providers.get(family)
        metas=[m for m in self.reference_meta if m.family==family]
        received=max((m.received_at for m in metas),default=None)
        if family not in self.permitted:
            return TerminalCapability(key=key,label=label,status='permission_denied' if provider else 'missing',
                reason='来源展示或缓存权限未满足。' if provider else '尚未配置此来源。',source_family=family,last_received_at=None)
        if not has_data:
            from app.market.minute_backfill import live_run_condition
            run=self.db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==provider.id,DataFetchRun.received_at<=self.cutoff)
                .where(live_run_condition())
                .order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
            reason=reason or ('最近采集结果：'+str(run.reason or run.status)+'；所选日期尚无可用缓存。' if run else '所选日期尚无已保存数据。')
            return TerminalCapability(key=key,label=label,status='missing',reason=reason,source_family=family,last_received_at=run.received_at if run else received)
        return TerminalCapability(key=key,label=label,status=status,reason=reason,source_family=family,last_received_at=received)


def current_rows(reader,meta,day):
    if meta is None:return []
    key=(meta.id,day)
    if key not in reader.quote_rows:
        reader.quote_rows[key]=[row for row in reader.evidence(meta).payload.get('rows',[]) if
            (stamp:=reader.at(row,meta.received_at)) is not None and stamp.astimezone(SHANGHAI).date()==day and stamp<=reader.cutoff]
    return reader.quote_rows[key]


def prior_heights(reader,day):
    if day is None:return {}
    result={row['ts_code']:height(row) for row,_,_ in reader.table('tushare_limit_step',day) if height(row) is not None}
    for row,_,_ in reader.table('tushare_limit_list_d',day):
        if row.get('limit')=='U' and height(row) is not None:result.setdefault(row['ts_code'],height(row))
    _,records=pool_rows(reader,day)
    for code,(_,row) in records.items():
        if row.get('limit')=='U' and height(row) is not None:result.setdefault(code,height(row))
    return result


def make_stock(code,names,identities):
    return TerminalStock(code=code,name=names.get(code,code),instrument_id=identities.get(code))


def live_snapshot_overlay(reader, target, session, latest, quotes, *, strict):
    """Choose one authorized bulk fallback without redating its rows.

    The primary series continues to own its historical curves and breadth;
    prices from this undated snapshot carry a different, explicit basis.
    """
    if strict or session.is_trading_day is not True or session.trade_day != target:
        return None
    if target != reader.cutoff.astimezone(SHANGHAI).date() or session.phase not in {'trading','break'}:
        return None
    family='fuyao_quotes'
    if family not in reader.permitted:
        return None
    from app.integrations.routing import preferred_families
    from app.market.resilience import serving_order, saved_quality, latest_run, binding_reason
    primary=getattr(reader,'market_family',MARKET)
    order=serving_order(reader.db,'stock_quote',preferred_families(reader.db,'stock_quote',[primary,family]))
    if family not in order:
        return None
    provider=reader.permitted[family]
    config=reader.db.get(SourceSettings,provider.id)
    if config is None or config.config.get('collection_mode')!='full_market' or config.paused_reason:
        return None
    primary_config=reader.db.get(SourceSettings,latest.source_id) if latest else None
    freshness=max(120,primary_config.interval_seconds*2) if primary_config else 120
    if primary in order and order.index(primary)<order.index(family) and any(
            (quote_fresh_at(reader.cutoff)-reader.at(row,latest.received_at)).total_seconds()<=freshness for row in quotes):
        schedule_feed_expiry(reader,max(reader.at(row,latest.received_at) for row in quotes),freshness)
        return None
    run=latest_run(reader.db,provider.id,reader.cutoff)
    from app.market.fuyao_market import DATASET, snapshot_predicate, snapshot_clock
    evidence=reader.db.scalar(read_facets.with_facets(reader.query({family},'reference')).where(
        read_facets.value('dataset')==DATASET,
        read_facets.observed_day()==target.isoformat()).order_by(
            EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
    if evidence is None:
        return None
    if getattr(reader,'recheck_at',None) is not None:
        clocks={row.get('snapshot_time') for row in evidence.payload.get('rows',[])
                if isinstance(row.get('snapshot_time'),str)}
        for value in clocks:
            stamp=snapshot_clock({'snapshot_time':value},evidence.received_at)
            if stamp is not None and stamp.astimezone(SHANGHAI).date()==target:
                schedule_recheck(reader,stamp)
    if binding_reason(reader.db,provider,run) or not saved_quality(
            reader.db,provider,config,'stock_quote',target,reader.cutoff,run).valid:
        return None
    current=snapshot_predicate(evidence.received_at,target,reader.cutoff,max(120,config.interval_seconds*2))
    rows=[row for row in evidence.payload.get('rows',[]) if current(row)]
    if not rows:
        return None
    if getattr(reader,'recheck_at',None) is not None:
        clocks={row.get('snapshot_time') for row in rows if isinstance(row.get('snapshot_time'),str)}
        for value in clocks:
            schedule_feed_expiry(reader,snapshot_clock({'snapshot_time':value},evidence.received_at),
                                 max(120,config.interval_seconds*2))
    reader.record_input(evidence)
    return evidence,rows


def apply_snapshot_prices(reader, snapshot, stocks, names, identities):
    evidence,rows=snapshot
    for row in rows:
        code=row['ts_code']
        item=stocks.setdefault(code,make_stock(code,names,identities))
        item.price,item.pre_close,item.open,item.high=[number(row.get(field)) for field in ('close','pre_close','open','high')]
        item.pct_change=pct(item.price,item.pre_close)
        item.open_pct=pct(item.open,item.pre_close)
        item.amount_cny=number(row.get('amount'))
        native=reader.at(row,evidence.received_at)
        item.source_time=native.isoformat() if native else None
        item.source_snapshot_time=datetime.fromisoformat(row['snapshot_time']) if row.get('snapshot_time') else None
        item.source_received_at=evidence.received_at
        item.source_family='fuyao_quotes'
        item.source_name=reader.permitted['fuyao_quotes'].name
        item.price_basis='snapshot'
        item.speed_5m=item.speed_window_seconds=item.seal_amount_cny=None
        # Share/valuation projections must not retain a different quote's price.
        item.turnover_rate=item.float_mv_cny=None
        item.status='快照 · '+state(row,item.limit_up,item.limit_down)[0]


def apply_auction(item,row,columns):
    item.auction_price=number(row.get('price'))
    item.auction_pct=pct(row.get('price'),row.get('pre_close'))
    item.auction_amount_cny=cny(row,'amount',columns)
    item.volume_ratio=number(row.get('volume_ratio'))
    # stk_auction contains matched trades, NOT three order-book checkpoints.
    item.node_amounts={'09:15':None,'09:20':None,'09:25':None}


def price_universe(reader,target,previous,latest,prior_batches,quotes,quote_map,
                   closing,limits,premarket_map,heights,names,limit_records,*,as_of_time,prepare_breadth):
    """Prepare bulk prices once; fresh objects isolate every owner/view overlay."""
    from app.config import settings
    enabled=settings().read_cache_enabled
    # A saved future source clock may become visible without another receipt.
    # Such a read takes the ordinary path instead of reusing an earlier filter.
    future_pending=any(reader.at(row,meta.received_at) is not None and
        reader.at(row,meta.received_at)>reader.cutoff for meta in [latest, *(meta for meta, _ in prior_batches)] if meta
        for row in reader.evidence(meta).payload.get('rows',[])) if enabled else False
    key=None
    if enabled and not future_pending:
        catalog=reader.db.scalar(select(ReadDependency.revision).where(ReadDependency.key=='catalog')) or 0
        source_names=sorted((family,str(provider.id),provider.name) for family,provider in reader.permitted.items()
            if family in {*MARKET_FAMILIES,LIMITS,'tushare_daily','tushare_stk_premarket','tushare_limit_step',
                          'tushare_limit_list_d','tushare_moneyflow_dc','tushare_daily_basic',*CALENDARS})
        identity=(target.isoformat(),str(previous),str(as_of_time),bool(closing),catalog,source_names,
                  [reader.input_versions[ident] for ident in sorted(reader.input_versions)])
        key=sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        cached=terminal_public.get(key)
        if cached is not None:
            if prepare_breadth:
                if cached.get('breadth') is None:
                    cached=dict(cached,breadth=current_breadth(target,quotes,limit_records,closing))
                    terminal_public.put(key,cached)
                reader.breadth_values=dict(cached['breadth'])
            return ({code:TerminalStock(**values) for code,values in cached['stocks'].items()},
                    dict(cached['names']),dict(cached['identities']))
    identities={}
    directory=reader.db.execute(select(Instrument.id,Instrument.exchange,Instrument.symbol,Instrument.name)
        .where(Instrument.asset_type=='stock',Instrument.valid_to.is_(None)))
    for ident,exchange,symbol,name in directory:
        suffix={'SSE':'SH','SZSE':'SZ','BSE':'BJ'}.get(exchange)
        if suffix:
            identities[symbol+'.'+suffix]=str(ident)
            names.setdefault(symbol+'.'+suffix,name)
    stocks={code:make_stock(code,names,identities) for code in quote_map.keys()|heights.keys()}
    for code,item in stocks.items():
        item.previous_height=heights.get(code)
        restriction=limits.get(code,{})
        item.limit_up=quoted_price(restriction.get('up_limit'))
        item.limit_down=quoted_price(restriction.get('down_limit'))
        if not (row:=quote_map.get(code)):continue
        item.price,item.pre_close,item.open,item.high=[quoted_price(row.get(field)) for field in ('close','pre_close','open','high')]
        item.pct_change=pct(item.price,item.pre_close)
        item.open_pct=pct(item.open,item.pre_close)
        item.amount_cny=number(row.get('amount'),1000 if closing else 1)
        item.source_time=None if closing else reader.at(row,latest.received_at).isoformat()
        item.price_basis='daily_close' if closing else 'intraday'
        item.source_received_at=datetime.fromisoformat(closing['received_at']) if closing else latest.received_at
        item.source_family='tushare_daily' if closing else reader.market_family
        item.source_name=reader.permitted[item.source_family].name
        item.status,item.seal_amount_cny=state(row,item.limit_up,item.limit_down)
        if closing:
            item.seal_amount_cny=None
            item.status='收盘 · '+item.status.replace('（盘口待核）','')
        if not closing:
            for meta, previous_quotes in prior_batches:
                if (prior := previous_quotes.get(code)) is None:
                    continue
                value, elapsed = speed_5m(row, prior, latest.received_at, meta.received_at, parse_time=reader.at)
                if value is not None and (item.speed_window_seconds is None
                        or abs(elapsed - 300) < abs(item.speed_window_seconds - 300)):
                    item.speed_5m, item.speed_window_seconds = value, elapsed
        if code in premarket_map:
            shares_row,columns=premarket_map[code]
            shares=number(shares_row.get('float_share'),10000) if columns.get('float_share')=='万股' else None
            volume=number(row.get('vol'),100 if closing else 1)
            if shares is not None and shares>0:
                item.turnover_rate=volume/shares*100 if volume is not None else None
                item.float_mv_cny=item.price*shares if item.price is not None else None
    breadth=current_breadth(target,quotes,limit_records,closing) if prepare_breadth else None
    if prepare_breadth:reader.breadth_values=breadth
    if key is not None:
        # Mutable per-page auction dictionaries/lists are always reconstructed
        # by TerminalStock defaults; later overlays cannot corrupt shared data.
        terminal_public.put(key,dict(stocks={code:{field:value for field,value in item.__dict__.items()
            if value is not None and field not in {'node_amounts','auction_tags'}} for code,item in stocks.items()},
            names=dict(names),identities=dict(identities),breadth=breadth))
    return stocks,names,identities


def workbench_view(db,owner_id,view='overview',day=None,stock=None,sector=None,as_of_time=None,instrument_id=None,fund_compare=None,*,detail_codes=None,observation_cutoff=None,full_universe=False,input_manifest=None,recheck_at=None):
    actual_current=now(db)
    if observation_cutoff is not None and (detail_codes is None or as_of_time is not None):
        raise APIError(422,'conflicting_cutoffs','详情资料截止与历史回看不能同时指定。')
    if any(value is not None and value>actual_current for value in (as_of_time,observation_cutoff)):
        raise APIError(422,'future_cutoff','观察时点不能晚于服务器当前时间。')
    # Keep a live parent's receipt boundary and market phase while reading its
    # details. This is not strict historical replay: unknown native quote times
    # retain exactly the same eligibility and source labels as that parent.
    current=observation_cutoff or actual_current
    cutoff=as_of_time or current
    if day is not None and (day>actual_current.astimezone(SHANGHAI).date() or day>cutoff.astimezone(SHANGHAI).date()):
        raise APIError(422,'future_trade_day','观察日期不能晚于当前日期或所选观察时点。')
    if stock is None and instrument_id is not None:
        identity=db.get(Instrument,instrument_id)
        if identity is not None and identity.asset_type=='stock' and identity.exchange in {'SSE','SZSE','BSE'}:
            stock=identity.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[identity.exchange]
    reader=SavedTables(db,cutoff,day)
    reader.recheck_at=recheck_at
    session=market_session(db,current)
    primary=sorted(set(reader.dates('tushare_moneyflow_ind_dc',cutoff.astimezone(SHANGHAI).date())) | set(reader.dates('tushare_direction_industry_funds',cutoff.astimezone(SHANGHAI).date())) | reader.market_days,reverse=True) if view=='funds' else (
        sorted(set(reader.dates('tushare_stk_auction',cutoff.astimezone(SHANGHAI).date())) |
               set(reader.auction.dates(cutoff.astimezone(SHANGHAI).date())),reverse=True)
        if view=='auction' else sorted(reader.market_days | reader.daily_days,reverse=True))
    automatic_day=session.trade_day if session.current_day_active and as_of_time is None else None
    target=day or automatic_day or next(iter(primary),None) or max(reader.available_days,default=cutoff.astimezone(SHANGHAI).date())
    fund_inputs=[]
    compare_codes=tuple(sorted(set((fund_compare or '').split(','))-{''}))[:10] if view=='funds' else ()
    live_funds=intraday_funds(db,target,cutoff,sector=sector,compare_codes=compare_codes,session=session,input_manifest=fund_inputs,
        owner_id=owner_id,current=as_of_time is None and target==current.astimezone(SHANGHAI).date()) if (
        detail_codes is None and (view in {'overview','funds'} or sector is not None)) else None
    current_context=target==current.astimezone(SHANGHAI).date() and as_of_time is None
    if sector is None and current_context and live_funds and live_funds.sectors:
        sector=next((item.code for item in live_funds.sectors if item.industry_level==2),live_funds.sectors[0].code) if view=='funds' else live_funds.sectors[0].code
    previous=reader.previous_day(target)
    snapshots=reader.market(target,historical=as_of_time is not None)
    latest=next((m for m in snapshots if current_rows(reader,m,target)),None)
    quotes=current_rows(reader,latest,target)
    closing=closing_table(reader,target)
    quote_map={row['ts_code']:row for row in closing['rows'] if row['ts_code'].endswith(('.SH','.SZ','.BJ'))} if closing else {row['ts_code']:row for row in quotes}
    if reader.market_family=='tencent_market_rt' and not closing:
        from app.market.tencent_market import native_limits
        limit_records=[(row,{},latest) for row in native_limits(quotes,target)]
    else:
        limit_records=reader.table(LIMITS,target)
    limits={row['ts_code']:row for row,_,_ in limit_records}
    # Review may use a later-received historical limit table, while a strict
    # observation can only see receipts at/before the cutoff supplied above.
    premarket=reader.table('tushare_stk_premarket',target)
    premarket_map={row['ts_code']:(row,columns) for row,columns,_ in premarket}
    if reader.market_family!='tencent_market_rt' or closing:
        for row,_,_ in premarket:limits.setdefault(row['ts_code'],row)
    heights=prior_heights(reader,previous)
    names={row['ts_code']:row['name'] for row in quotes if row.get('name')}
    for family in ('tushare_limit_step','tushare_limit_list_d','tushare_moneyflow_dc'):
        for data_day in {target,previous} - {None}:
            for row,_,_ in reader.table(family,data_day):
                if row.get('name'):names.setdefault(row['ts_code'],row['name'])
    prior_batches = []
    if latest and not closing:
        # Receipt time only bounds the lookup. Each stock is matched by its
        # actual quote clock, which may be earlier than its batch receipt.
        for meta in snapshots:
            age = (latest.received_at - meta.received_at).total_seconds()
            if age > 390:
                break
            if age >= 210 and meta.source_id == latest.source_id:
                prior_batches.append((meta, {row['ts_code']: row for row in current_rows(reader, meta, target)}))
    stocks,names,identities=price_universe(reader,target,previous,latest,prior_batches,quotes,quote_map,
        closing,limits,premarket_map,heights,names,limit_records,
        as_of_time=as_of_time,prepare_breadth=detail_codes is None)
    snapshot=live_snapshot_overlay(reader,target,session,latest,quotes,strict=as_of_time is not None)
    if snapshot:
        apply_snapshot_prices(reader,snapshot,stocks,names,identities)
    # Explicitly configured quote providers feed the same stock projection.
    # Their small configured scope never replaces the whole-market statistics.
    routed={} if closing else selected_quote_projection(reader,target,strict=as_of_time is not None)
    from app.market.quote_speed import quote_speeds
    routed_speeds=quote_speeds(reader,routed,strict=as_of_time is not None)
    for code,quote in routed.items():
        item=stocks.setdefault(code,make_stock(code,names,identities))
        payload=quote.quote;provenance=quote.provenance
        item.price,item.pre_close,item.open,item.high=[number(getattr(payload,field)) for field in ('price','previous_close','open','high')]
        item.pct_change=number(quote.change_percent);item.open_pct=pct(payload.open,payload.previous_close)
        item.amount_cny=number(payload.amount_cny);item.source_time=payload.quote_time.isoformat()
        item.source_family=provenance.family;item.source_name=provenance.source_name
        item.price_basis='intraday';item.source_received_at=provenance.received_at
        item.name=payload.instrument_name or item.name
        # Price-only providers cannot inherit the old supplier's order book or
        # sampling speed after a route change.
        item.speed_5m=item.speed_window_seconds=item.seal_amount_cny=None
        if code in routed_speeds:
            item.speed_5m,item.speed_window_seconds=routed_speeds[code]
        item.status='涨停价（盘口待核）' if item.price is not None and item.price==item.limit_up else (
            '跌停价' if item.price is not None and item.price==item.limit_down else '限制价待核' if item.limit_up is None else '未触板')
    # Selected-stock daily basic is safe only on its exact original trade date.
    for row,columns,_ in reader.table('tushare_daily_basic',target):
        if item:=stocks.get(row.get('ts_code')):
            if item.turnover_rate is None:item.turnover_rate=number(row.get('turnover_rate'))
            if item.float_mv_cny is None:item.float_mv_cny=cny(row,'circ_mv',columns)
    auctions=auction_projection(reader,target,names,identities,stock,only_codes=detail_codes)
    for row,columns,_ in reader.table('tushare_stk_auction',target):
        code=row.get('ts_code')
        if code and code not in stocks:stocks[code]=make_stock(code,names,identities)
        if code:apply_auction(stocks[code],row,columns)
    for entry in auctions:
        if entry.day==target:
            for projected in entry.items:
                item=stocks.setdefault(projected.code,projected.model_copy(deep=True))
                item.name=projected.name
                for field in NODE_FIELDS:setattr(item,field,getattr(projected,field))
    funds_day=target if reader.table('tushare_moneyflow_ind_dc',target) or reader.table('tushare_direction_industry_funds',target) else None
    if funds_day is None and view=='overview':
        funds_day=next(iter(reader.dates('tushare_moneyflow_ind_dc',target)),None)
    if detail_codes is not None and sector is None:
        # Stock-only batches must not repeatedly return an unrelated default
        # board's members and ten-day fund history.
        sectors,members,member_day=[],[],None
    else:
        sectors,members,member_day=sector_projection(reader,target,funds_day,sector,stocks,names,identities,allow_recent_members=current_context)
        sector=sector or next((item.code for item in sectors if view=='funds' and item.industry_level==2),sectors[0].code if sectors else None)
        if sector and not members:
            sectors,members,member_day=sector_projection(reader,target,funds_day,sector,stocks,names,identities,allow_recent_members=current_context)
    # Only same-day per-stock funds are merged into that day's quotes.
    for row,columns,_ in reader.table('tushare_moneyflow_dc',target):
        code=row.get('ts_code')
        if not code:continue
        if code not in stocks:stocks[code]=make_stock(code,names,identities)
        item=stocks[code]
        item.main_net_cny=cny(row,'net_amount',columns)
        item.funds_trade_day=target
        if item.price is None and not closing:
            item.price=number(row.get('close'));item.pct_change=number(row.get('pct_change'));item.status='盘后资金附带收盘价'
    # Rebuild member references after attaching exact-date stock funds.
    members=[stocks.get(item.code,item) for item in members]
    members.sort(key=lambda item:(item.code!=stock,-(item.previous_height or 0),-(item.main_net_cny if item.main_net_cny is not None else -math.inf),item.code))
    if stock is not None and stock not in stocks:stocks[stock]=make_stock(stock,names,identities)
    attach_daily_pools(reader,target,stocks)
    from app.market.ths_members import attach_themes
    attach_themes(reader,target,stocks)
    # A data revision, not a wall clock: unchanged saved evidence remains reusable.
    # Permissions and effective temporal visibility participate in the identity.
    revision=sha256(json.dumps(detail_manifest(reader,target,as_of_time,cutoff,routed,snapshots,snapshot),
                               sort_keys=True).encode()).hexdigest()[:24]
    if detail_codes is not None:
        chosen=[stocks.get(code,make_stock(code,names,identities)) for code in detail_codes]
        histories=stock_histories(reader,target,snapshots,chosen)
        sector_history=daily_fund_history(reader,target,sectors,[sector] if sector else [])
        return TerminalDetails(trade_day=target,revision=revision,stocks=chosen,auction_days=auctions,
            events=[event for events,_ in histories.values() for event in events],
            seals={code:seals for code,(_,seals) in histories.items()},
            sector_code=sector,members=members[:60],members_trade_day=member_day,funds_history=sector_history,
            tracking_members=tracking_projection(members),tracking_member_count=len(members),
            tracking_members_truncated=len(members)>TRACKING_MEMBER_LIMIT)
    watch=db.get(Watchlist,owner_id)
    watched=set(watch.instrument_ids) if watch else set()
    displayed_candidates=(item for item in stocks.values() if view!='ladder' or main_board(item.code,item.name))
    ranked=sorted(displayed_candidates,key=lambda item:(
        0 if item.code==stock else 1 if item.instrument_id in watched else 2 if item.previous_height else 3,
        -(item.previous_height or 0),-(item.pct_change if item.pct_change is not None else -10000),item.code))
    auction_selected=next((entry.items[0].code for entry in auctions if entry.day==target and entry.items),None) if view=='auction' else None
    selected=stock or auction_selected or next((item.code for item in ranked if item.previous_height),None) or (ranked[0].code if ranked else None)
    full_universe = full_universe or view=='overview'
    displayed=ranked if full_universe else ranked[:200]
    broken=broken_projection(reader,target,stocks,names,identities)
    # Retain breadth statistics over every actual quote before limiting rows.
    breadth=breadth_projection(reader,target,quotes,snapshots,latest,limit_records,view,closing=closing)
    events,seals=[],[]
    if view=='ladder':
        # The scrollable table exposes every retained ladder row. Project their
        # histories in one pass, including peers below the old five-row cutoff.
        visible=[item for item in displayed if item.previous_height]
        chosen={item.code:item for item in visible}
        if selected in stocks:chosen[selected]=stocks[selected]
        histories=stock_histories(reader,target,snapshots,list(chosen.values()))
        events,seals=histories.get(selected,([],[]))
    history_codes=[]
    for category in ('concept','industry'):
        history_codes.extend(item.code for item in [item for item in sectors if item.category==category and (category!='industry' or item.industry_level==2)][:10])
    if sector and sector not in history_codes:history_codes.append(sector)
    history_codes.extend(code for code in compare_codes if code not in history_codes)
    history=daily_fund_history(reader,target,sectors,history_codes)
    indices=index_projection(reader,target,strict=as_of_time is not None)
    capabilities=build_capabilities(reader,target,latest,quotes,auctions,sectors,members,previous,heights,stocks,view)
    focus_provider=reader.permitted.get('tencent_web')
    focus_config=reader.db.get(SourceSettings,focus_provider.id) if focus_provider else None
    if focus_config and focus_config.owner_id==owner_id and focus_config.config.get('collection_mode')=='focused':
        capabilities.append(TerminalCapability(key='focused_quotes',label='关注报价',status='available',
            source_family='tencent_web',reason='当前关注范围的独立报价采样，逐股来源时点单列。'))
    if closing:
        capabilities[0]=TerminalCapability(key='quotes',label='收盘日线',status='after_market',source_family='tushare_daily',
            reason=f'{target} 完整归档实际返回{len(quote_map)}只；价格、涨幅与成交额使用收盘日线，盘中曲线独立保留。',last_received_at=closing['received_at'])
        for item in capabilities:
            if item.key=='speed':item.reason='收盘日线不提供最后5分钟涨速；未用较早盘中变化冒充收盘涨速。'
            if item.key=='seal':item.reason='收盘日线不提供买卖盘口；独立日榜封单另列，盘口曲线只显示真实采样。'
    elif snapshot:
        evidence,rows=snapshot
        capabilities[0]=TerminalCapability(key='quotes',label='同花顺备用快照',status='partial',
            source_family='fuyao_quotes',last_received_at=evidence.received_at,
            reason=f'全市场接口实际返回{len(rows)}只；价格使用同花顺完整快照记录。'
                '逐股行情日期和成交时刻未知，服务器快照时刻及接收时刻单列；不补竞价、盘口或历史曲线。')
    if live_funds is not None:
        capabilities.append(TerminalCapability(key='intraday_funds',label='盘中主力资金采样',
            status={'available':'partial','stale':'stale','missing':'missing','permission_denied':'permission_denied'}[live_funds.state],
            reason=live_funds.scope,source_family=live_funds.source_family,last_received_at=live_funds.received_at))
    if routed and not quotes:
        capabilities[0]=TerminalCapability(key='quotes',label='已配置行情',status='partial',
            source_family=next(iter({item.provenance.family for item in routed.values()})) if len({item.provenance.family for item in routed.values()})==1 else None,
            reason=f'按能力选源后实际覆盖{len(routed)}只股票；来源和时点随每行显示，不代表全市场覆盖。',
            last_received_at=max(item.provenance.received_at for item in routed.values()))
    for item in capabilities:
        if item.source_family==reader.market_family and latest:item.last_received_at=latest.received_at
    member_count=next((item.member_count for item in sectors if item.code==sector),None) or len(members)
    latest_at=max([m.received_at for m in (latest,) if m]+[m.received_at for m in reader.reference_meta if target in m.dates]+
        [item.provenance.received_at for item in routed.values()]+
        [m.received_at for m in reader.auction.pairs.get(target,())]+
        ([datetime.fromisoformat(closing['received_at'])] if closing else [])+
        [item.source_received_at for item in indices if item.source_received_at],default=None)
    latest_source=max((reader.at(row,latest.received_at) for row in quotes),default=None) if latest else None
    freshness_clock=quote_fresh_at(current)
    fresh=bool(latest and latest_source and freshness_clock-latest.received_at<=timedelta(seconds=180) and freshness_clock-latest_source<=timedelta(seconds=180)) or any(
        item.quote.quote_time and freshness_clock-item.quote.quote_time<=timedelta(seconds=180)
        and freshness_clock-item.provenance.received_at<=timedelta(seconds=180) for item in routed.values())
    auction_fresh=(view=='auction' and target==current.astimezone(SHANGHAI).date()
        and time(9,15)<=current.astimezone(SHANGHAI).time()<time(9,35)
        and any(entry.day==target and entry.node_received_at and current-entry.node_received_at<=timedelta(seconds=180) for entry in auctions))
    if current_context:
        schedule_feed_expiry(reader,latest.received_at if latest else None,180)
        schedule_feed_expiry(reader,latest_source,180)
        for item in routed.values():
            schedule_feed_expiry(reader,item.quote.quote_time,180)
            schedule_feed_expiry(reader,item.provenance.received_at,180)
        if auction_fresh:
            for entry in auctions:
                if entry.day==target and entry.node_received_at:
                    schedule_recheck(reader,entry.node_received_at+timedelta(seconds=180,microseconds=1))
            schedule_recheck(reader,datetime.combine(target,time(9,35),SHANGHAI))
    mode='observed' if as_of_time is not None else 'live' if not closing and (auction_fresh or target==current.astimezone(SHANGHAI).date() and fresh and (segment(current) or session.phase=='break')) else 'review'
    relay=relay_summary(reader,target,stocks,prior_heights,main_board) if view=='ladder' else None
    strength=[]
    if view=='funds':
        from app.market.sector_strength import sector_strength
        strength_sectors={item.code:item for item in sectors}
        if live_funds:
            strength_sectors.update({item.code:item for item in live_funds.sectors})
        strength=sector_strength(reader,list(strength_sectors.values()),stocks,target,
            current=current_context,daily_close=closing is not None)
    live = None
    if view in {'overview','ladder'} and as_of_time is None and closing is None:
        first_boards = verified_first_boards(reader, previous, stocks)
        live = live_ladder(stocks,target,previous,cutoff,session,first_board_codes=first_boards)
    # Internal research callers freeze the exact immutable versions read by this
    # same projection. Never infer a whole-market scope from the 200-row UI cap.
    if input_manifest is not None:
        input_manifest.extend(reader.input_versions[key] for key in sorted(reader.input_versions))
        input_manifest.extend(fund_inputs)
    return TerminalView(view=view,trade_day=target,available_days=sorted(reader.available_days|({automatic_day} if automatic_day else set()),reverse=True)[:366],mode=mode,
        relay_summary=relay,sector_strength=strength,
        market_session=session,intraday_funds=live_funds,
        intraday_ladder=live,
        latest_at=latest_at,indices=indices,breadth=breadth,stocks=displayed,sectors=sectors,
        detail_codes=sorted(stocks),detail_revision=revision,
        # Explicit offset and full microseconds survive URL/notification
        # canonicalization without JavaScript Date millisecond truncation.
        detail_observation_cutoff=cutoff.astimezone(SHANGHAI) if as_of_time is None else None,
        selected_stock_code=selected,selected_sector_code=sector,auction_days=auctions,selected_members=members[:60],
        funds_history=history,broken=broken,events=events,stock_seals=seals,capabilities=capabilities,
        funds_trade_day=funds_day,members_trade_day=member_day,previous_trade_day=previous,
        coverage=TerminalCoverage(quote_count=len({row['ts_code'] for row in snapshot[1]}) if snapshot else len(set(quote_map)|set(routed)),displayed_stock_count=len(displayed),member_count=member_count,
            displayed_member_count=min(60,len(members)),quote_scope=(
                '同花顺全市场接口实际返回的价格快照；逐股行情日期和时刻未知，名单与主来源可能不同。缺项不补零，列表最多200只；每行来源独立显示。'
                if snapshot else CLOSING_SCOPE if closing else
                '腾讯目录实际范围逐页采集，各股时刻独立；不保证交易所当日在市全集，局部关注报价不计入完整批次统计。' if reader.market_family=='tencent_market_rt' else STOCK_SCOPE)+(f' 另有按能力选源的已配置股票{len(routed)}只，以每行来源为准。' if routed else ''),
            members_scope=(f'DC成员日{member_day}，行情日{target}；较早成员不代表当日完整目录。' if member_day and member_day!=target else '同日DC成分。')+
                '最多展示60只，优先选中股票、昨日梯队和净额较高成分。'))


def tracking_projection(members):
    # Rank the whole exact-date constituency before the wire cap. The existing
    # funds/ladder priority and 60-row members response remain independent.
    ranked=sorted(members,key=lambda item:(item.speed_5m is None,
        -item.speed_5m if item.speed_5m is not None else 0,item.code))
    fields=TerminalSectorMember.model_fields
    return [TerminalSectorMember(**{key:getattr(item,key) for key in fields})
        for item in ranked[:TRACKING_MEMBER_LIMIT]]


def auction_projection(reader,target,names,identities,selected,only_codes=None):
    days=[day for day in reversed(reader.calendar_days) if day<=target][:4]
    source_days=set(reader.dates('tushare_stk_auction',target))|set(reader.auction.dates(target))
    days=sorted(set(days)|source_days,reverse=True)[:4]
    result=[]
    for day in days:
        items={}
        heights=prior_heights(reader,reader.previous_day(day))
        # Rank the raw rows first. Only the displayed 200 need full stock DTOs
        # and price/ratio calculations; the ordering still covers every row.
        records=[record for record in reader.table('tushare_stk_auction',day) if record[0].get('ts_code')]
        records.sort(key=lambda record:-(amount if (amount:=cny(record[0],'amount',record[1])) is not None else -1))
        paired=reader.auction.batch(day)
        vendor_codes={stock_code(row.get('code')) for row in paired.rows} if paired else set()
        displayed=([record for record in records if record[0]['ts_code'] in vendor_codes] if paired else records[:200]) if only_codes is None else [record for record in records if record[0]['ts_code'] in only_codes]
        if not paired and only_codes is None and selected and len(records)>200:
            chosen=next((record for record in records if record[0]['ts_code']==selected),None)
            if chosen is not None and chosen not in displayed:displayed=records[:199]+[chosen]
        for row,columns,_ in displayed:
            code=row.get('ts_code')
            item=make_stock(code,names,identities);item.previous_height=heights.get(code)
            apply_auction(item,row,columns)
            items[code]=item
        batch=project_nodes(reader,day,items,make_stock,names,identities,number,only_codes)
        if only_codes is not None:
            # A first board may have formed after the auction. Its dated pool
            # remains useful even when the seal supplier did not list it.
            _, day_pool = pool_rows(reader, day)
            for code in only_codes:
                if code in day_pool and code not in items:
                    items[code] = make_stock(code, names, identities)
                    items[code].previous_height = heights.get(code)
            attach_daily_pools(reader, day, items)
        if batch:
            for item in items.values():item.previous_height=heights.get(item.code)
            latest_stage=next((stage for stage in ('09:25','09:20','09:15') if stage in batch.stages),None)
            ordered=sorted(items.values(),key=lambda item:-(
                item.auction_current_seal_cny if item.auction_current_seal_cny is not None else
                item.node_amounts.get(latest_stage) if item.node_amounts.get(latest_stage) is not None else -1))
        else:ordered=list(items.values())
        # Independent day ranking is based on matched amount, never fake orders.
        count=number(batch.header.get('one_word_count')) if batch else None
        result.append(TerminalAuctionDay(day=day,items=ordered,
            node_received_at=batch.received_at if batch else None,node_source=batch.source if batch else None,
            node_status='available' if batch else 'unavailable',
            one_word_count=int(count) if count is not None else None,
            header_total_cny=header_total(batch, number)))
    return result


def industry_levels(reader,day):
    from app.market.industry_catalog import saved_catalog
    provider=reader.permitted.get('eastmoney_funds')
    if not provider:return {}
    evidence=saved_catalog(reader.db,provider.id,day,reader.cutoff)
    if evidence is None:return {}
    reader.record_input(evidence)
    return {row['code']+'.DC':int(row['industry_level']) for row in evidence.payload.get('rows',[])
        if isinstance(row,dict) and row.get('industry_level') in {'1','2','3'} and isinstance(row.get('code'),str)}


def daily_fund_history(reader,target,sectors,codes):
    categories={item.code:item.category for item in sectors}
    # A selected board may have no current-day row but still have dated history.
    for code in codes:
        if code in categories:continue
        matches=set()
        for category,family in (('concept','tushare_moneyflow_ind_dc'),('industry','tushare_direction_industry_funds')):
            if any(row.get('ts_code')==code for day in reader.dates(family,target)[:10]
                   for row,_,_ in reader.table(family,day)):
                matches.add(category)
        if len(matches)==1:categories[code]=matches.pop()
    output=[]
    for category,family in (('concept','tushare_moneyflow_ind_dc'),('industry','tushare_direction_industry_funds')):
        chosen=[code for code in codes if categories.get(code)==category]
        if not chosen:continue
        for day in reversed(reader.dates(family,target)[:10]):
            actual={row['ts_code']:cny(row,'net_amount',columns) for row,columns,_ in reader.table(family,day)}
            levels=industry_levels(reader,day) if category=='industry' else {}
            for code in chosen:
                # Each historical day needs its own received classification.
                value=actual.get(code) if category=='concept' or levels.get(code)==2 else None
                output.append(TerminalFundPoint(day=day,sector_code=code,value=value))
    return output


def sector_projection(reader,target,funds_day,selected,stocks,names,identities,*,allow_recent_members=False):
    # Same-day directory and member identity. No name-based joins to THS or an
    # unrelated date's constituency, even when that would fill more UI cells.
    catalog_day=funds_day or target
    if allow_recent_members and not reader.table('tushare_dc_index',catalog_day):
        catalog_day=next(iter(reader.dates('tushare_dc_index',target)),target)
    catalog={row['ts_code']:row for row,_,_ in reader.table('tushare_dc_index',catalog_day)}
    member_day=target
    member_rows=reader.table('tushare_dc_member',member_day)
    if selected and not any(row.get('ts_code')==selected for row,_,_ in member_rows):
        member_rows=[*member_rows,*archived_members(reader,member_day,selected)]
    if allow_recent_members and not any(row.get('ts_code')==selected for row,_,_ in member_rows):
        candidates=sorted(set(reader.dates('tushare_dc_member',target)) |
            {day for day in reader.calendar_days if day<target},reverse=True)[:20]
        for candidate in candidates:
            rows=reader.table('tushare_dc_member',candidate)
            if not any(row.get('ts_code')==selected for row,_,_ in rows):
                rows=[*rows,*archived_members(reader,candidate,selected)]
            if any(row.get('ts_code')==selected for row,_,_ in rows):
                member_rows,member_day=rows,candidate
                break
    by_sector={}
    for row,_,_ in member_rows:by_sector.setdefault(row['ts_code'],{})[row['con_code']]=row
    sectors=[]
    for row,columns,_ in reader.table('tushare_moneyflow_ind_dc',funds_day) if funds_day else []:
        code=row['ts_code']
        sectors.append(TerminalSector(code=code,name=row.get('name') or catalog.get(code,{}).get('name') or code,category='concept',
            main_net_cny=cny(row,'net_amount',columns),pct_change=number(row.get('pct_change')),rank=None,
            member_count=len(by_sector[code]) if code in by_sector else None))
    sectors.sort(key=lambda item:-(item.main_net_cny if item.main_net_cny is not None else -math.inf))
    for rank,item in enumerate(sectors,1):item.rank=rank
    # Directory-only sectors remain selectable without invented fund values.
    existing={item.code for item in sectors}
    for code,row in catalog.items():
        if code not in existing:
            sectors.append(TerminalSector(code=code,name=row.get('name') or code,category='concept',pct_change=number(row.get('pct_change')),
                member_count=len(by_sector[code]) if code in by_sector else None))
    # Industry and concept data use separate upstream tables. The official
    # same-day catalog supplies levels; names and suffixes are not evidence.
    industry_day=funds_day or target
    levels=industry_levels(reader,industry_day)
    industry_directory={row['ts_code']:row for row,_,_ in reader.table('tushare_direction_industry',industry_day)}
    industry=[]
    for row,columns,_ in reader.table('tushare_direction_industry_funds',funds_day) if funds_day else []:
        code=row['ts_code']
        industry.append(TerminalSector(code=code,name=row.get('name') or industry_directory.get(code,{}).get('name') or code,
            category='industry',industry_level=levels.get(code),main_net_cny=cny(row,'net_amount',columns),
            pct_change=number(row.get('pct_change')),member_count=len(by_sector[code]) if code in by_sector else None))
    existing={item.code for item in industry}
    for code,row in industry_directory.items():
        if code not in existing:
            industry.append(TerminalSector(code=code,name=row.get('name') or code,category='industry',industry_level=levels.get(code),
                pct_change=number(row.get('pct_change')),member_count=len(by_sector[code]) if code in by_sector else None))
    industry.sort(key=lambda item:-(item.main_net_cny if item.main_net_cny is not None else -math.inf))
    for level in (None,1,2,3):
        for rank,item in enumerate((item for item in industry if item.industry_level==level),1):item.rank=rank
    sectors.extend(industry)
    members=[]
    for code,row in by_sector.get(selected,{}).items():
        names.setdefault(code,row.get('name') or code)
        if code not in stocks:stocks[code]=make_stock(code,names,identities)
        item=stocks[code]
        item.theme=next((sector.name for sector in sectors if sector.code==selected),selected)
        members.append(item)
    return sectors,members,member_day if members else None


def selected_quote_projection(reader,target,*,strict=False):
    from app.market.read_models import quote_batch
    families={'tencent_web','tickflow_live','ashare_public','tushare_rt_k','fuyao_quotes','tdx_quotes'}
    providers=list(reader.db.scalars(select(Provider).where(Provider.family.in_(families))))
    selected=set()
    for provider in providers:
        if permission_reason(provider):continue
        config=reader.db.get(SourceSettings,provider.id)
        for item in config.config.get('symbols',[]) if config else []:
            if item.get('asset_type')=='stock':selected.add((item.get('exchange'),item.get('symbol')))
        if config and provider.family=='tencent_web' and config.config.get('collection_mode')=='focused':
            start=datetime.combine(target,time.min,SHANGHAI)
            focus_query=select(Instrument.exchange,Instrument.symbol).join(
                EvidenceVersion,EvidenceVersion.instrument_id==Instrument.id).where(
                EvidenceVersion.source_id==provider.id,EvidenceVersion.kind=='quote',
                EvidenceVersion.provenance=='source_reported',EvidenceVersion.owner_id.is_(None),
                EvidenceVersion.visibility=='shared',EvidenceVersion.received_at<=reader.cutoff,
                EvidenceVersion.event_at>=start,EvidenceVersion.event_at<start+timedelta(days=1),
                EvidenceVersion.event_at<=reader.cutoff,Instrument.asset_type=='stock',
                Instrument.valid_to.is_(None)).group_by(Instrument.exchange,Instrument.symbol)
            if not strict:
                # A day's visited pages can span the entire market. Bound the
                # live overlay to recent batches rather than re-reading every
                # stock ever focused; full-market prices keep their own path.
                from app.market.quote_focus import focused_selection,MAX_BATCH
                selected.update((item.exchange,item.symbol) for item in focused_selection(reader.db,config,reader.cutoff)
                    if item.asset_type=='stock')
                focus_query=focus_query.order_by(func.max(EvidenceVersion.received_at).desc(),
                    Instrument.exchange,Instrument.symbol).limit(MAX_BATCH)
            selected.update(reader.db.execute(focus_query))
    if not selected:return {}
    from sqlalchemy import tuple_
    identities=list(reader.db.scalars(select(Instrument.id).where(Instrument.asset_type=='stock',Instrument.valid_to.is_(None),
        tuple_(Instrument.exchange,Instrument.symbol).in_(selected))))
    output={}
    # This read has an immutable receipt cutoff. Unknown market clocks cannot
    # be assigned to a selected day, even when they contain a plausible price.
    query={'as_of_time':reader.cutoff} if strict else {'observation_cutoff':reader.cutoff}
    for item in quote_batch(reader.db,instrument_ids=identities,**query).items:
        payload,proof=item.quote,item.provenance
        if payload is None or proof is None or proof.family not in families:continue
        if payload.quote_time is None or payload.quote_time.astimezone(SHANGHAI).date()!=target:continue
        if item.state not in {'available','stale'}:continue
        symbol=item.instrument.symbol+'.'+{'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[item.instrument.exchange]
        output[symbol]=item
        reader.record_input(SimpleNamespace(id=proof.evidence_version_id,source_id=proof.source_id,
            content_hash=proof.content_hash,received_at=proof.received_at))
    return output


def index_projection(reader,target,*,strict=False):
    result={}
    start=datetime.combine(target,time.min,SHANGHAI)
    ranked=reader.query({'tushare_rt_indices'},'quote').where(
        EvidenceVersion.event_at>=start,EvidenceVersion.event_at<start+timedelta(days=1),EvidenceVersion.event_at<=reader.cutoff
    ).with_only_columns(EvidenceVersion.id,func.row_number().over(partition_by=EvidenceVersion.instrument_id,
        order_by=(EvidenceVersion.event_at.desc(),EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc())).label('position')).subquery()
    for evidence in reader.db.scalars(select(EvidenceVersion).join(ranked,ranked.c.id==EvidenceVersion.id).where(ranked.c.position==1)):
        payload=evidence.payload
        raw=payload.get('quote_time')
        try:stamp=datetime.fromisoformat(raw) if raw else None
        except (ValueError,TypeError):continue
        if stamp is None or stamp.utcoffset() is None or stamp.astimezone(SHANGHAI).date()!=target or stamp>reader.cutoff:continue
        identity=reader.db.get(Instrument,evidence.instrument_id) if evidence.instrument_id else None
        if identity is None or identity.asset_type!='index':continue
        code=identity.symbol+'.'+{'SSE':'SH','SZSE':'SZ'}.get(identity.exchange,'')
        if code in result:continue
        result[code]=TerminalIndex(code=code,name=payload.get('instrument_name') or code,
            price=number(payload.get('price')),pct_change=pct(payload.get('price'),payload.get('previous_close')),
            amount_cny=number(payload.get('amount_cny')),source_time=stamp.isoformat(),price_basis='intraday',source_received_at=evidence.received_at,
            source_family='tushare_rt_indices',source_name=reader.permitted['tushare_rt_indices'].name)
    from app.market.read_models import quote_batch
    query={'as_of_time':reader.cutoff} if strict else {'observation_cutoff':reader.cutoff}
    for item in quote_batch(reader.db,**query).items:
        payload,proof=item.quote,item.provenance
        if payload is None or proof is None:continue
        snapshot_basis=False
        if payload.quote_time is None:
            if strict or proof.family!='fuyao_indices':continue
            session=market_session(reader.db,reader.cutoff)
            from app.market.fuyao_market import current_snapshot
            if (session.is_trading_day is not True or session.trade_day!=target or session.phase not in {'trading','break'}
                or not current_snapshot(dict(snapshot_time=payload.source_snapshot_time.isoformat() if payload.source_snapshot_time else None),
                    proof.received_at,target,reader.cutoff,item.freshness_limit_seconds)):
                continue
            snapshot_basis=True
        elif payload.quote_time.astimezone(SHANGHAI).date()!=target:continue
        code=item.instrument.symbol+'.'+{'SSE':'SH','SZSE':'SZ'}[item.instrument.exchange]
        existing=result.get(code)
        if (existing and existing.price is not None and existing.source_time
            and datetime.fromisoformat(existing.source_time)>=datetime.combine(target,time(15),SHANGHAI)
            and (payload.quote_time is None or payload.quote_time<datetime.combine(target,time(15),SHANGHAI))):continue
        result[code]=TerminalIndex(code=code,name=item.instrument.name,price=number(payload.price),
            pct_change=number(item.change_percent),amount_cny=number(payload.amount_cny),
            source_time=payload.quote_time.isoformat() if payload.quote_time else None,
            source_snapshot_time=payload.source_snapshot_time,
            source_family=proof.family,source_name=proof.source_name,price_basis='snapshot' if snapshot_basis else 'intraday',source_received_at=proof.received_at)
        reader.record_input(SimpleNamespace(id=proof.evidence_version_id,source_id=proof.source_id,
            content_hash=proof.content_hash,received_at=proof.received_at))
    closing=closing_table(reader,target,'indices')
    if closing:
        from app.market.review_protocol import INDEX_CODES
        for row in closing['rows']:
            code=row['ts_code']
            existing=result.get(code)
            # A real saved closing quote keeps its true supplier timestamp.
            if existing and existing.price is not None and existing.source_time and datetime.fromisoformat(existing.source_time)>=datetime.combine(target,time(15),SHANGHAI):continue
            result[code]=TerminalIndex(code=code,name=INDEX_CODES.get(code,(None,code))[1],
                price=quoted_price(row.get('close')),pct_change=pct(row.get('close'),row.get('pre_close')),
                amount_cny=number(row.get('amount'),1000),price_basis='daily_close',source_received_at=closing['received_at'],
                source_family='tushare_index_daily',source_name=reader.permitted['tushare_index_daily'].name)
    return list(result.values())


def current_breadth(target,quotes,limit_records,closing):
    limits=[row for row,_,_ in limit_records]
    memo=comparison_memo()
    # A review can show the day using a later saved exact-date limit table.
    return closing_counts(closing,limits,target) if closing else compare_limits(quotes,limits,datetime.combine(target,time(23,59),SHANGHAI),memo=memo) if quotes else {}


def breadth_projection(reader,target,quotes,snapshots,latest,limit_records,view,*,closing=None):
    result=getattr(reader,'breadth_values',None)
    if result is None:result=current_breadth(target,quotes,limit_records,closing)
    memo=comparison_memo()
    breadth=TerminalBreadth(up=result.get('advancing'),down=result.get('declining'),
        flat=result.get('unchanged'),limit_up=result.get('limit_up') if result.get('comparable') else None,
        limit_down=result.get('limit_down') if result.get('comparable') else None,comparable=result.get('comparable'),
        scope=CLOSING_SCOPE if closing else BREADTH_SCOPE,amount_cny=number(result.get('total_amount_cny')),
        basis='daily_close' if closing else 'intraday',trade_day=target,
        source_received_at=closing['received_at'] if closing else latest.received_at if latest else None,
        source_family='tushare_daily' if closing else reader.market_family,
        source_name=reader.permitted.get('tushare_daily' if closing else reader.market_family).name
            if ('tushare_daily' if closing else reader.market_family) in reader.permitted else None)
    if not closing and reader.market_family=='tencent_market_rt':
        breadth.scope='腾讯目录实际范围；同批原生上下限价精确比较，旧日与缺值不补零。各页与各股时刻独立，与其他来源不拼接。'
    if view!='overview':return breadth
    from app.market.breadth_history import saved_series
    historical=saved_series(reader.db,target,reader.cutoff)
    if historical:
        breadth.historical_series=TerminalBreadthHistory.model_validate({
            key:historical[key] for key in TerminalBreadthHistory.model_fields if key in historical})
        for ref in historical.get('evidence_refs',[]):reader.input_versions[ref['evidence_id']]=dict(ref)
    # Curve points keep the limit version that had actually arrived at that
    # observation. Later limit corrections cannot rewrite an earlier point.
    buckets={}
    for meta in snapshots:
        stamp=meta.received_at.astimezone(SHANGHAI)
        if segment(stamp):buckets.setdefault((stamp.hour,stamp.minute),meta)
    versions=[meta for meta in reader.reference_meta if meta.family==LIMITS and target in meta.dates]
    previous_stamp=None
    for snapshot in sorted(buckets.values(),key=lambda item:item.received_at):
        available=next((reader.evidence(meta) for meta in versions if meta.received_at<=snapshot.received_at),None)
        if reader.market_family=='tencent_market_rt':
            from app.market.tencent_market import comparison
            counts=comparison(reader.db,snapshot,reader.cutoff)
        else:
            counts=comparison_for(reader.db,snapshot,available,memo,curve_only=True)
        stamp=counts.get('source_time_max')
        if stamp and stamp<=reader.cutoff:
            if previous_stamp and observation_gap_seconds(previous_stamp,stamp)>120:
                breadth.points.append(TerminalBreadthPoint(time=(previous_stamp+timedelta(seconds=1)).isoformat(),up=None,down=None))
            breadth.points.append(TerminalBreadthPoint(time=stamp.isoformat(),up=counts['limit_up'] if counts['comparable'] else None,
                down=counts['limit_down'] if counts['comparable'] else None))
            previous_stamp=stamp
    return breadth


def stock_histories(reader,target,snapshots,items):
    """One JSON projection for a bounded batch; never recompute breadth per stock."""
    if not snapshots or not items:return {item.code:([],[]) for item in items}
    from app.config import settings
    from app import read_cache_payloads
    metas={meta.id:meta for meta in snapshots}
    ordered=sorted(metas.values(),key=lambda meta:(meta.received_at,meta.id))
    reusable=(len(items)==1 and settings().read_cache_enabled
        and all(getattr(meta,'content_hash',None) for meta in metas.values())
        and reader.db.connection().get_isolation_level()=='REPEATABLE READ')
    # Only raw matching rows are shared, never clocks, limits or final events.
    # IDs/hashes were just selected through this reader's rights and cutoff.
    # Small matches use the existing protected share of the same 64MiB budget.
    known={};missing=[]
    def key(meta):return ('terminal-history-rows-v1',str(meta.id),meta.content_hash,items[0].code)
    for meta in ordered:
        cached=read_cache_payloads.get(key(meta)) if reusable else None
        if cached is not None:known[meta.id]=cached['rows']
        else:
            known[meta.id]=[]
            missing.append(meta)
    # Expand once per receipt and use a relational membership predicate. A
    # JSONPath OR expression compares every full-market row to every selected
    # stock, making a complete ladder cost seconds even on a local database.
    projected={item.code:[] for item in items}
    if missing:
        values=func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued('value')
        rows=reader.db.execute(select(EvidenceVersion.id,values.c.value)
            .select_from(EvidenceVersion).join(values,true())
            .where(EvidenceVersion.id.in_([m.id for m in missing]),
                values.c.value.op('->>')('ts_code').in_([item.code for item in items]))
            .order_by(EvidenceVersion.received_at,EvidenceVersion.id))
        for ident,row in rows:
            if row.get('ts_code') in projected:
                if reusable:known[ident].append(row)
                else:projected[row['ts_code']].append((ident,row))
    if reusable:
        for meta in missing:
            read_cache_payloads.put(key(meta),dict(evidence_id=str(meta.id),content_hash=meta.content_hash,
                code=items[0].code,rows=known[meta.id]),protected=True)
        # Preserve receipt/id and duplicate-row order, including cached absence.
        projected[items[0].code]=[(meta.id,row) for meta in ordered for row in known[meta.id]]
    return {item.code:stock_history(reader,target,snapshots,item.code,item,projected[item.code]) for item in items}


def stock_history(reader,target,snapshots,selected,item,projected=None):
    if not snapshots or item is None or getattr(item,'price_basis',None)=='snapshot':return [],[]
    ids={meta.id:meta for meta in snapshots}
    # selected is API-validated TS code, and then interpolated only into a
    # SQL-bound JSONPath value (never SQL text).
    path=cast('$.rows[*] ? (@.ts_code == "'+selected+'")',JSONPATH)
    rows=projected if projected is not None else reader.db.execute(select(EvidenceVersion.id,func.jsonb_path_query_first(EvidenceVersion.payload,path))
        .where(EvidenceVersion.id.in_(ids)).order_by(EvidenceVersion.received_at,EvidenceVersion.id))
    events,seals=[],[]
    previous_state=previous_time=None
    limit_metas=[meta for meta in reader.reference_meta if meta.family in {LIMITS,'tushare_stk_premarket'} and target in meta.dates]
    # Immutable reference rows repeat across every quote receipt. Resolve this
    # stock once per reference version, but keep the receipt test below for
    # EACH snapshot so a later correction cannot rewrite earlier events.
    limits_by_version={}
    def known_limits(snapshot):
        # The selection is tied to EACH original receipt. The current detail's
        # later-received limit/correction must not rewrite old observations.
        for meta in limit_metas:
            if meta.received_at>snapshot.received_at:continue
            if meta.id not in limits_by_version:
                raw=next((row for row in reader.evidence(meta).payload.get('rows',[]) if row.get('ts_code')==selected and source_day(row.get('trade_date'))==target),None)
                limits_by_version[meta.id]=(quoted_price(raw.get('up_limit')),quoted_price(raw.get('down_limit'))) if raw else None
            limits=limits_by_version[meta.id]
            if limits is not None:return limits
        return None,None
    for ident,row in rows:
        meta=ids[ident]
        stamp=reader.at(row,meta.received_at) if row else None
        if not stamp or stamp.astimezone(SHANGHAI).date()!=target or stamp>reader.cutoff:continue
        upper,lower=(quoted_price(row.get('up_limit')),quoted_price(row.get('down_limit'))) if getattr(meta,'family',MARKET)=='tencent_market_rt' else known_limits(meta)
        status,seal=state(row,upper,lower)
        gap=previous_time is not None and observation_gap_seconds(previous_time,stamp)>180
        if gap:
            seals.append(TerminalSeal(time=(previous_time+timedelta(seconds=60)).isoformat(),value=None))
            events.append(TerminalEvent(time=stamp.isoformat(),label='采样中断后重新观测',code=selected,kind='gap'))
        seals.append(TerminalSeal(time=stamp.isoformat(),value=seal))
        if status!=previous_state:
            label=('观测'+status if previous_state is None or gap else
                   '观测开板' if previous_state=='封住' and status=='触板后回落' else
                   '观测回封' if status=='封住' and previous_state=='触板后回落' else '观测'+status)
            events.append(TerminalEvent(time=stamp.isoformat(),label=label,code=selected,kind='observed'))
            if item.first_observed is None and status in {'封住','涨停价（盘口待核）'}:item.first_observed=stamp.strftime('%H:%M:%S')
            item.latest_change=stamp.strftime('%H:%M:%S')+' '+label
        previous_state,previous_time=status,stamp
    return events[-30:],seals


def broken_projection(reader,target,stocks,names,identities):
    result=[]
    recent=[day for day in reversed(reader.calendar_days) if day<target][:3]
    for break_day in recent:
        previous=reader.previous_day(break_day)
        heights=prior_heights(reader,previous)
        for row,_,_ in reader.table('tushare_limit_list_d',break_day):
            code=row.get('ts_code')
            prior=heights.get(code)
            if row.get('limit')!='Z' or prior is None or prior<3 or any(item.code==code for item in result):continue
            names.setdefault(code,row.get('name') or code)
            item=stocks.get(code,make_stock(code,names,identities)).model_copy(deep=True)
            if not main_board(code,item.name):continue
            item.previous_height=prior;item.break_day=break_day
            item.days_since_break=sum(break_day<day<=target for day in reader.calendar_days)
            result.append(item)
    return result[:30]


def auction_orders_capability(reader,target,auction_days):
    from app.market.auction_source import FAMILY
    entry=next((entry for entry in auction_days if entry.day==target and entry.node_received_at),None)
    permitted=AUCTION_NODE_FAMILIES & reader.permitted.keys()
    if entry is None:
        return TerminalCapability(key='auction_orders',label='09:15 / 09:20 / 09:25 封单',
            status='missing' if permitted else 'permission_denied',source_family=FAMILY,
            reason='所选日期没有有权且完整配对的短线侠封单表；缺失保持横线。')
    batch=reader.auction.batch(target)
    status='partial'
    reason='短线侠公开封单快照；来源行级时点未知，本机接收时间独立显示。未到阶段不显示；盘中滚动封单与撮合成交额独立。'
    if (target==reader.cutoff.astimezone(SHANGHAI).date()
            and time(9,15)<=reader.cutoff.astimezone(SHANGHAI).time()<time(9,35)
            and reader.cutoff-entry.node_received_at>timedelta(seconds=180)):
        status='stale';reason='封单采集已超过180秒，显示上次成功快照；请以本机接收时刻判断时效。'
    provider=reader.permitted.get(batch.family)
    run=reader.db.scalar(select(DataFetchRun).where(DataFetchRun.source_id==provider.id,
        DataFetchRun.received_at<=reader.cutoff).order_by(DataFetchRun.received_at.desc(),DataFetchRun.id.desc()).limit(1))
    if run and run.received_at>entry.node_received_at and run.status in {'error','unavailable'}:
        status='stale';reason=f'最近采集失败（{run.reason or run.status}），保留上次成功封单与接收时间。'
    return TerminalCapability(key='auction_orders',label='09:15 / 09:20 / 09:25 封单',
        status=status,source_family=batch.family,last_received_at=entry.node_received_at,reason=reason)


def build_capabilities(reader,target,latest,quotes,auction_days,sectors,members,previous,heights,stocks,view):
    same_day_funds=reader.table('tushare_moneyflow_ind_dc',target)
    capabilities=[
        reader.capability('quotes','批量行情',reader.market_family,bool(quotes),status='partial',reason='报价来自所选交易日的已保存批次；各股票来源时点独立，包含停牌旧价的行不会计入本日。'),
        reader.capability('limits','涨跌停价',reader.market_family if reader.market_family=='tencent_market_rt' else LIMITS,
            any(positive(row.get('up_limit')) and positive(row.get('down_limit')) for row in quotes)
                if reader.market_family=='tencent_market_rt' else bool(reader.table(LIMITS,target)),
            reason='只比较同批原生限制价，不按固定比例推算。' if reader.market_family=='tencent_market_rt' else '只比较同日原始限制价，不按固定10%推算。'),
        reader.capability('auction_trades','竞价成交','tushare_stk_auction',bool(reader.table('tushare_stk_auction',target)),reason='显示成交价和真实竞价成交额；与供应商封单快照独立。'),
        auction_orders_capability(reader,target,auction_days),
        reader.capability('ladder','昨日梯队','tushare_limit_step',bool(heights),reason='以沪深共同已保存交易日历确定昨日；昨日高度来自收盘日榜，不以今日盘中触板确认晋级。'),
        reader.capability('sector_funds','板块资金（日频）','tushare_moneyflow_ind_dc',bool(same_day_funds),status='after_market',reason='每日盘后更新，金额元；盘中页面若展示较早日期会单列资金交易日。'),
        reader.capability('stock_funds','个股资金（日频）','tushare_moneyflow_dc',bool(reader.table('tushare_moneyflow_dc',target)),status='after_market',reason='原表万元转元；仅与相同交易日的成分、行情联动。'),
        reader.capability('members','历史成分','tushare_dc_member',bool(members),status='partial',reason='只采用所选日期和相同DC代码的成分；未配置的板块保留缺项。'),
        reader.capability('premarket','盘前股本','tushare_stk_premarket',bool(reader.table('tushare_stk_premarket',target)),reason='流通股本万股转股后计算当前流通市值和累计换手。'),
        TerminalCapability(key='intraday_funds',label='分钟资金与5分钟资金变化',status='pending',source_family='tushare_moneyflow_ind_dc',reason='已核验接口是盘后日频；分钟资金、5分钟净额和盘中名次变化等待补充接口。'),
        TerminalCapability(key='ai',label='AI观察与预案核对',status='pending',reason='结构已预留；本轮不生成分析或核对结论。'),
    ]
    if reader.market_family=='tencent_market_rt' and latest:
        coverage=getattr(latest,'coverage',{})
        capabilities[0].reason=f"本批请求{coverage.get('expected','未知')}只，返回{coverage.get('accepted','未知')}只，本日有效时点{len(quotes)}只；目录在市状态与逐页时刻独立核验，不把缺项补零。"
    speeds=any(item.speed_5m is not None for item in stocks.values())
    seals=any(item.seal_amount_cny is not None for item in stocks.values())
    capabilities.append(reader.capability('speed','近5分钟涨速',reader.market_family,speeds,status='partial',reason='同一来源两次真实行情，时差270至330秒且在同一个连续交易时段；午休和缺测不跨越。'))
    capabilities.append(reader.capability('seal','盘口封单快照',reader.market_family,seals,status='partial',reason='买一价等于涨停价、买一量为正且卖一量为0才计算封单；无完整盘口时仅显示价格状态。'))
    if previous is None:
        capabilities.append(TerminalCapability(key='calendar',label='交易日历',status='missing',reason='缺少沪深共同日历，昨日梯队和四日对照不推算自然日。'))
    latest_source=max((reader.at(row,latest.received_at) for row in quotes),default=None) if latest else None
    freshness_clock=quote_fresh_at(reader.cutoff)
    if latest and target==reader.cutoff.astimezone(SHANGHAI).date() and (
        freshness_clock-latest.received_at>timedelta(seconds=180) or latest_source is None or freshness_clock-latest_source>timedelta(seconds=180)):
        capabilities[0].status='stale';capabilities[0].reason='最新已保存批次或来源行情超过180秒；再次接收旧价不会变成新行情。'
    return capabilities
