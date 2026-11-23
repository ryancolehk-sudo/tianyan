"""Private credential, fixed destination, bounded POSTs and truthful source clocks."""
import calendar
import hashlib
import json
import math
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.evidence.schemas import BarPayload, ReferencePayload
from app.market.protocol import (CatalogBatch, CatalogRecord, InstrumentKey, NoRedirect,
    Quote, QuoteBatch, Response, SHANGHAI, SourceFailure, decimal_value)
from app.market.reference import ReferenceBatch
from app.market.tushare_specs import API_DOCS, DOCS, SPECS, allowed_endpoints

MAX_BODY = 2_000_000
FINANCIAL_APIS = frozenset({'income','balancesheet','cashflow','fina_indicator'})
DC_FUND_PAGE_SIZE = 3000
DC_FUND_MAX_PAGES = 3
DC_FUND_MAX_ROWS = 8000  # Existing immutable ReferencePayload contract, not an API entitlement.
EMPTY_PUBLICATION_APIS = frozenset({'ths_daily','limit_list_d','limit_list_ths','top_list','top_inst','dc_index','dc_member','moneyflow_dc','moneyflow_mkt_dc','moneyflow_ind_dc','limit_step','stk_premarket'})
SUFFIX = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}
LABELS = {'ts_code':'证券代码','symbol':'原代码','name':'名称','trade_date':'交易日期','date':'数据日期','month':'统计月份',
    'ann_date':'公告日','f_ann_date':'实际公告日','end_date':'报告期','report_type':'报告类型','open':'开盘','high':'最高','low':'最低','close':'收盘',
    'pre_close':'昨收','pct_chg':'涨跌幅','pct_change':'涨跌幅','vol':'成交量（原单位）','amount':'成交额（原单位）',
    'pe':'市盈率','pe_ttm':'市盈率TTM','pb':'市净率','total_mv':'总市值（原单位）','circ_mv':'流通市值（原单位）',
    'turnover_rate':'换手率','turnover_ratio':'换手率','adj_factor':'复权因子','limit':'涨跌停类别','limit_times':'连板次数',
    'first_time':'首次封板','last_time':'最后封板','open_times':'开板次数','net_mf_amount':'净流入额（原单位）',
    'total_revenue':'营业总收入','revenue':'营业收入','operate_profit':'营业利润','total_profit':'利润总额',
    'n_income':'净利润','n_income_attr_p':'归母净利润','total_assets':'资产总计','total_liab':'负债合计',
    'n_cashflow_act':'经营活动现金净额','n_cashflow_inv_act':'投资活动现金净额','n_cash_flows_fnc_act':'筹资活动现金净额',
    'eps':'每股收益','basic_eps':'基本每股收益','update_flag':'更新标识（供应商）','roe':'净资产收益率','roa':'总资产收益率','reason':'上榜原因','buyer':'买方','seller':'卖方'}
LABELS.update({'exalter':'营业部名称','side':'买卖榜类型','buy':'买入额','sell':'卖出额','net_buy':'净买入额',
    'buy_rate':'买入占总成交比例','sell_rate':'卖出占总成交比例','l_buy':'龙虎榜买入额','l_sell':'龙虎榜卖出额',
    'l_amount':'龙虎榜成交额','net_amount':'龙虎榜净买额','net_rate':'榜单净买额占比','amount_rate':'榜单成交额占比',
    'up_stat':'几天几板统计','limit_times':'连续封板数','fd_amount':'封单金额'})

# New parser output only. Old immutable tables with unknown units stay unknown.
# Unit labels follow the linked official endpoint documentation, not field-name
# guesses across different Tushare APIs (daily.amount and moneyflow differ).
REFERENCE_UNITS = {
    'moneyflow_mkt_dc': {'net_amount': '元'},
    'ths_daily': {'pct_change':'%','vol':'手','turnover_rate':'%'},
    'dc_index': {'total_mv':'万元','pct_change':'%','turnover_rate':'%'},
    'dc_member': {},
    'moneyflow_dc': {'net_amount':'万元','close':'元','pct_change':'%','net_amount_rate':'%'},
    'moneyflow_ind_dc': {'net_amount':'元','pct_change':'%','net_amount_rate':'%'},
    'stk_premarket': {'total_share':'万股','float_share':'万股','pre_close':'元','up_limit':'元','down_limit':'元'},
    'stk_auction': {'price':'元','pre_close':'元','amount':'元','vol':'股','turnover_rate':'%','volume_ratio':'倍','float_share':'万股'},
    'limit_step': {},
    'moneyflow': {**{side+'_'+size+'_amount':'万元' for side in ('buy','sell') for size in ('sm','md','lg','elg')},
        **{side+'_'+size+'_vol':'手' for side in ('buy','sell') for size in ('sm','md','lg','elg')},
        'net_mf_amount':'万元','net_mf_vol':'手'},
    'daily_basic': {'close':'元','turnover_rate':'%','turnover_rate_f':'%','dv_ratio':'%','dv_ttm':'%',
        'total_mv':'万元','circ_mv':'万元','total_share':'万股','float_share':'万股','free_share':'万股'},
    'top_inst': {'buy':'元','sell':'元','net_buy':'元','buy_rate':'%','sell_rate':'%'},
    'top_list': {**{f:'元' for f in ('amount','l_buy','l_sell','l_amount','net_amount','float_values')},
        'close':'元','pct_change':'%','turnover_rate':'%','net_rate':'%','amount_rate':'%'},
    'daily': {'open':'元','high':'元','low':'元','close':'元','pre_close':'元','change':'元','pct_chg':'%','vol':'手','amount':'千元'},
    'stk_limit': {'pre_close':'元','up_limit':'元','down_limit':'元'},
    'dividend': {'stk_div':'股/股','cash_div':'元/股','cash_div_tax':'元/股'},
    'limit_list_ths': {'price':'元','pct_chg':'%','turnover_rate':'%'},
    'limit_list_d': {'close':'元','pct_chg':'%','turnover_ratio':'%'},
}


def validate_top_inst(fields, rows, received_at):
    required={'trade_date','ts_code','exalter','side','buy','sell','net_buy','reason'}
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    for row in rows:
        if not isinstance(row['ts_code'],str) or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)',row['ts_code']):
            raise SourceFailure('instrument_mismatch')
        if day(row['trade_date']) > received_at.astimezone(SHANGHAI).date():
            raise SourceFailure('future_source_time')
        if row['side'] not in {'0','1'} or not row['exalter'] or not row['reason']:
            raise SourceFailure('schema_changed')
        for field in ('buy','sell','net_buy','buy_rate','sell_rate'):
            value=row.get(field)
            if value is None:
                continue
            if not re.fullmatch(r'-?\d{1,16}(?:\.\d{1,8})?',value):
                raise SourceFailure('invalid_number')
            if field!='net_buy' and Decimal(value)<0:
                raise SourceFailure('invalid_number')
        # Several distinct seats can share a label such as 机构专用. Preserve
        # each source row; name is not a seat identifier and must not dedupe it.


class TushareHTTPSHandler(urllib.request.HTTPSHandler):
    """Use the validated public address for the socket and the provider hostname for TLS."""
    def https_open(self, req):
        from app.ai.protocols import ModelFailure, PinnedHTTPSConnection, resolved_addresses, validate_endpoint
        try:
            parts = validate_endpoint(req.full_url)
            port = parts.port or 443
            addresses = resolved_addresses(parts.hostname, port, timeout=req.timeout)
        except ModelFailure as exc:
            raise SourceFailure('endpoint_not_allowed' if exc.code == 'endpoint_rejected' else 'transport_error') from None

        def connect(host, timeout):
            return PinnedHTTPSConnection(parts.hostname, addresses[0], port, timeout)

        return self.do_open(connect, req)


def request(api, params, fields, credential, endpoint):
    if credential is None or not credential.get_secret_value():
        raise SourceFailure('credentials_missing')
    if endpoint not in allowed_endpoints() or api not in {spec.api for spec in SPECS.values()}:
        raise SourceFailure('endpoint_not_allowed')
    token = credential.get_secret_value()
    body = json.dumps({'api_name': api, 'token': token, 'params': params, 'fields': fields}).encode()
    started = time.monotonic()
    req = urllib.request.Request(endpoint, data=body, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Tianyan/1.0 (private research)'})
    try:
        # No proxy, redirect or SDK stdout can disclose the credential. Only
        # the explicit operator-selected destination receives the POST body.
        with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}), TushareHTTPSHandler()).open(req, timeout=15) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise SourceFailure('response_too_large')
            if token.encode() in raw:
                raise SourceFailure('unsafe_upstream_response')
            return Response(raw, datetime.now(UTC), round((time.monotonic()-started)*1000), response.status)
    except urllib.error.HTTPError as error:
        reason = 'permission_missing' if error.code in {401, 403} else 'rate_limited' if error.code == 429 else 'upstream_unavailable'
        from app.market.http_retry import retry_after
        try:
            delay=retry_after((error.headers or {}).get('Retry-After',''),datetime.now(UTC)) if error.code in {429,503} else None
        except SourceFailure as failure:
            failure.http_status=error.code
            raise failure from None
        finally:error.close()
        raise SourceFailure(reason,retry_after=delay,http_status=error.code) from None
    except (TimeoutError, socket.timeout):
        raise SourceFailure('timeout') from None
    except (urllib.error.URLError, OSError):
        raise SourceFailure('transport_error') from None


def table(body, *, allow_empty_columns=False):
    try:
        if len(body) > MAX_BODY:
            raise SourceFailure('response_too_large')
        value = json.loads(body)
        if type(value.get('code')) is not int:
            raise ValueError()
        if value['code'] != 0:
            msg = str(value.get('msg', '')).lower()
            reason = 'permission_missing' if value['code'] in {2002, 401, 403} or any(s in msg for s in ['权限', '积分', 'permission']) else (
                'rate_limited' if any(s in msg for s in ['频次', '频率', '次数', 'limit']) else 'upstream_unavailable')
            raise SourceFailure(reason)
        fields, items = value['data']['fields'], value['data']['items']
        # Only an explicitly opted-in caller can interpret a successful,
        # fully empty table as unavailable data rather than a changed schema.
        # Null/missing/malformed fields and nonempty rows remain schema errors.
        if allow_empty_columns and fields == [] and items == []:
            return [], []
        if not isinstance(fields, list) or not 1 <= len(fields) <= 60 or len(set(fields)) != len(fields):
            raise ValueError()
        if any(not isinstance(f, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,59}', f) or
               any(word in f.lower() for word in ['token', 'password', 'secret', 'licence']) for f in fields):
            raise ValueError()
        if not isinstance(items, list) or len(items) > 8000:
            raise ValueError()
        rows = []
        for values in items:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError()
            row = {}
            for field, cell in zip(fields, values):
                if cell is None:
                    row[field] = None
                elif isinstance(cell, bool) or not isinstance(cell, (str, int, float)) or isinstance(cell, float) and not math.isfinite(cell):
                    raise ValueError()
                else:
                    text = str(cell)
                    if len(text) > 500 or '\0' in text:
                        raise ValueError()
                    row[field] = text
            rows.append(row)
        return fields, rows
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise SourceFailure('schema_changed') from None


def code(key):
    return key.symbol + '.' + SUFFIX[key.exchange]


def price_decimal(value, key):
    """Remove only a tiny binary-float tail near a quoted price tick.

    Raw reference rows stay unchanged. Never round a materially different
    price to a limit, and retain legitimate supported decimal precision.
    """
    if isinstance(value,str) and re.fullmatch(r'\d{1,16}(?:\.\d{1,20})?',value):
        number=Decimal(value)
        tick=Decimal('.001') if key.asset_type=='index' else Decimal('.01')
        canonical=number.quantize(tick)
        if 0 < abs(number-canonical) <= Decimal('0.00000001'):
            value=format(canonical,'f')
    return decimal_value(value,positive=True)


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{8}', value):
        raise SourceFailure('invalid_source_time')
    try:
        return datetime.strptime(value, '%Y%m%d').date()
    except ValueError:
        raise SourceFailure('invalid_source_time') from None


SOURCE_CLOCK_LEAD_SECONDS = 60


def clock(value, received_at):
    if value in {None, ''}:
        return None
    if not isinstance(value, str):
        raise SourceFailure('invalid_source_time')
    parsed = None
    for pattern in ['%Y-%m-%d %H:%M:%S', '%Y%m%d%H%M%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f']:
        try:
            parsed = datetime.strptime(value, pattern).replace(tzinfo=SHANGHAI)
            break
        except ValueError:
            pass
    if parsed is None:
        raise SourceFailure('invalid_source_time')
    if parsed > received_at + timedelta(seconds=SOURCE_CLOCK_LEAD_SECONDS):
        raise SourceFailure('future_source_time')
    return parsed


@dataclass(frozen=True)
class DailyQuote(Quote):
    trade_day: date | None = None
    quote_kind: str = 'daily_close'


def parse_quotes(body, selection, received_at, *, daily=False):
    fields, rows = table(body,allow_empty_columns=not daily)
    if not fields and not rows:
        raise SourceFailure('instrument_unavailable')
    required = {'ts_code', 'close', 'pre_close', 'open', 'high', 'low', 'trade_date' if daily else 'name'}
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    requested = {code(key): key for key in selection}
    result = QuoteBatch(expected=len(requested))
    grouped = {}
    for row in rows:
        symbol = row['ts_code']
        if symbol not in requested:
            raise SourceFailure('unexpected_instrument')
        grouped.setdefault(symbol, []).append(row)
    for symbol, key in requested.items():
        items = grouped.get(symbol, [])
        if not items:
            result.rejected[symbol] = 'instrument_missing'
            continue
        try:
            if daily:
                dates = [day(item['trade_date']) for item in items]
                if len(set(dates)) != len(dates) or any(d > received_at.astimezone(SHANGHAI).date() for d in dates):
                    raise SourceFailure('invalid_source_time')
                row = max(items, key=lambda item: item['trade_date'])
                trade_day, quote_time = day(row['trade_date']), None
            else:
                if len(items) != 1:
                    raise SourceFailure('duplicate_instrument')
                row = items[0]
                trade_day, quote_time = None, clock(row.get('trade_time'), received_at)
            price, previous, opening, high, low = [price_decimal(row.get(f) or '',key) for f in ['close','pre_close','open','high','low']]
            if high is not None and low is not None and (high < low or any(v is not None and not low <= v <= high for v in [price, opening])):
                raise SourceFailure('inconsistent_price_range')
            name = row.get('name') or key.symbol
            if not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
                raise SourceFailure('invalid_name')
            values = dict(instrument=key, name=name, price=price, previous_close=previous,
                open=opening, high=high, low=low, quote_time=quote_time, received_at=received_at,
                price_unit='point' if key.asset_type == 'index' else 'CNY')
            if not daily:
                volume = decimal_value(row.get('vol') or '')
                if volume is not None and volume != volume.to_integral_value():
                    raise SourceFailure('invalid_number')
                values.update(volume_shares=volume.to_integral_value() if volume is not None else None, amount_cny=decimal_value(row.get('amount') or ''))
            result.records.append(DailyQuote(**values, trade_day=trade_day) if daily else Quote(**values))
        except SourceFailure as error:
            result.rejected[symbol] = error.reason
    return result


def parse_catalog(body):
    fields, rows = table(body)
    if not {'ts_code','symbol','name','exchange','list_status'}.issubset(fields):
        raise SourceFailure('schema_changed')
    result = CatalogBatch(expected=len(rows))
    seen = set()
    for i, row in enumerate(rows):
        try:
            exchange = row['exchange']
            if exchange not in SUFFIX or row['ts_code'] != row['symbol']+'.'+SUFFIX[exchange] or row['list_status'] != 'L':
                raise SourceFailure('instrument_mismatch')
            key = InstrumentKey(exchange, row['symbol'], 'stock')
            if key.identity in seen:
                raise SourceFailure('duplicate_instrument')
            seen.add(key.identity)
            if not row['name'] or len(row['name']) > 200 or any(ord(c) < 32 for c in row['name']):
                raise SourceFailure('invalid_name')
            result.records.append(CatalogRecord(key, row['name'], None, 'A股'))
        except (SourceFailure, TypeError) as error:
            result.rejected[str(i)] = error.reason if isinstance(error, SourceFailure) else 'schema_changed'
    # A conflicting duplicate invalidates the batch rather than letting row order
    # decide a security's identity/name.
    if 'duplicate_instrument' in result.rejected.values():
        raise SourceFailure('duplicate_instrument')
    return result


def parse_calendar(body, exchange, start, end):
    fields, rows = table(body)
    if not {'exchange','cal_date','is_open'}.issubset(fields):
        raise SourceFailure('schema_changed')
    expected = {start + timedelta(days=i) for i in range((end-start).days+1)}
    result = {}
    for row in rows:
        d = day(row['cal_date'])
        if row['exchange'] != exchange or d not in expected or d in result or row['is_open'] not in {'0','1'}:
            raise SourceFailure('incomplete_calendar')
        result[d] = row['is_open'] == '1'
    if set(result) != expected:
        raise SourceFailure('incomplete_calendar')
    return sorted(result.items())


def recent_financial_rows(fields, rows, received_at, api):
    if not {'ann_date','end_date'}.issubset(fields) or api != 'fina_indicator' and 'report_type' not in fields:
        raise SourceFailure('schema_changed')
    today=received_at.astimezone(SHANGHAI).date()
    selected={}
    for row in rows:
        announced,period=day(row['ann_date']),day(row['end_date'])
        actual=day(row['f_ann_date']) if row.get('f_ann_date') else announced
        if not today-timedelta(days=730) <= announced <= today or period > today or actual > today:
            raise SourceFailure('invalid_source_time')
        if api != 'fina_indicator' and row['report_type'] != '1':
            continue
        previous=selected.get(period)
        flag=row.get('update_flag')
        if flag not in {None,'','0','1'}:raise SourceFailure('schema_changed')
        clock_key=(actual,announced,1 if flag=='1' else 0)
        if previous is not None and previous[0]==clock_key and previous[1]!=row:
            raise SourceFailure('schema_changed')
        if previous is None or clock_key > previous[0]:
            selected[period]=(clock_key,row)
    return [selected[period][1] for period in sorted(selected,reverse=True)[:4]]


def parse_reference(body, family, received_at, key=None):
    spec = SPECS[family]
    fields, rows = table(body, allow_empty_columns=spec.api in EMPTY_PUBLICATION_APIS)
    return _reference_table(fields,rows,family,received_at,key)


def _reference_table(fields,rows,family,received_at,key=None,*,paginated_dc=False):
    spec = SPECS[family]
    if spec.api in EMPTY_PUBLICATION_APIS and not rows:
        # Do not write an empty version over the last usable dated list, or
        # turn a normal publication gap into a permanent schema pause.
        raise SourceFailure('daily_data_not_published')
    if spec.api=='top_list':
        if not {'trade_date','ts_code','reason'}.issubset(fields):raise SourceFailure('schema_changed')
        today=received_at.astimezone(SHANGHAI).date()
        for row in rows:
            if not row['ts_code'] or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)',row['ts_code']):
                raise SourceFailure('instrument_mismatch')
            if day(row['trade_date'])>today:raise SourceFailure('future_source_time')
        # One stock can legitimately appear for multiple listing reasons.
        # This is a provider table, not a unique-stock count or breadth input.
    if spec.api == 'limit_list_ths':
        from app.market.review_fetch import REQUIRED
        if not REQUIRED['pool_details'].issubset(fields):
            raise SourceFailure('schema_changed')
        seen = set()
        for row in rows:
            identity = (row.get('trade_date'), row.get('ts_code'))
            if not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', row.get('ts_code') or ''):
                raise SourceFailure('instrument_mismatch')
            if day(row.get('trade_date')) > received_at.astimezone(SHANGHAI).date():
                raise SourceFailure('future_source_time')
            if identity in seen:
                raise SourceFailure('review_duplicate_row')
            seen.add(identity)
    if spec.api=='top_inst':
        validate_top_inst(fields,rows,received_at)
        if 'source_order' in fields:
            raise SourceFailure('schema_changed')
        fields=[*fields,'source_order']
        rows=[{**row,'source_order':str(index+1)} for index,row in enumerate(rows)]
    if spec.api in {'index_basic','stk_limit','dividend'}:
        validate_extended_table(spec,fields,rows,received_at,key)
    if spec.mode in {'dated_table','dated_members'}:
        validate_dated_table(spec, fields, rows, received_at, paginated_dc=paginated_dc)
    if key is not None and any(row.get('ts_code') != code(key) for row in rows):
        raise SourceFailure('unexpected_instrument')
    if spec.api in FINANCIAL_APIS:
        rows=recent_financial_rows(fields,rows,received_at,spec.api)
    # Numeric-leading SHIBOR field names get a reversible display key. Labels
    # retain the original provider column; unknown units remain unknown.
    column_key = lambda f: f if f[0].isalpha() else 'field_' + f
    if len({column_key(f) for f in fields}) != len(fields):
        raise SourceFailure('schema_changed')
    payload = ReferencePayload(dataset='tushare.'+spec.api, title='Tushare · '+spec.title,
        source_url=API_DOCS.get(spec.api,DOCS), attribution='用户指定的 Tushare 兼容服务；个人已购权益',
        scope=('两融通常次日发布；请求最近10个自然日、最多400行，按原交易日期展示，不代表当日全市场。' if spec.api == 'margin_detail' else '') +
            ('指数基础资料仅指定市场、最多2000行参考表，不自动认领股票身份，也不代表指数实时行情。' if spec.api=='index_basic' else '') +
            ('所选单股票近10自然日、最多20条价格限制资料；价格限制不代表已涨停/跌停或盘中状态。' if spec.api=='stk_limit' else '') +
            ('所选单股票最多100条供应商分红返回，不保证顺序或完整历史；预案/实施进度与未来登记、除权、派息计划分别保留，不当已发生事件。' if spec.api=='dividend' else '') +
            ('仅查询最近730自然日窗口、最多100条，按报告期取最近四期；三表仅合并报表report_type=1，修订按实际公告日及update_flag选择，指标窗口按报告期，均校验公告日；不能当全部历史。' if spec.api in FINANCIAL_APIS else '') +
            '当前有界返回，最多8000行；不是完整市场或完整历史。数据日期/公告日见原字段；本机观察日期不代表发布或成交时间。单位未知不换算，财务三表为已选字段。',
        columns=[{'key':column_key(f),'label':'主力净流入' if f=='net_amount' and spec.api in {'moneyflow_dc','moneyflow_mkt_dc'} else LABELS.get(f,f),'unit':REFERENCE_UNITS.get(spec.api,{}).get(f)} for f in fields],
        rows=[{column_key(f):row[f] for f in fields} for row in rows],
        observed_day=received_at.astimezone(SHANGHAI).date())
    result = ReferenceBatch(records=[(spec.api+(':'+code(key) if key else ''),payload,key)],expected=1,requests=1)
    if spec.api in {'daily','index_daily'} and key:
        if not {'ts_code','trade_date','open','high','low','close'}.issubset(fields):
            raise SourceFailure('schema_changed')
        seen=set()
        for row in rows:
            value=day(row.get('trade_date'))
            if value in seen:
                raise SourceFailure('duplicate_source_day')
            seen.add(value)
        for row in rows:
            try:
                d = day(row['trade_date'])
                if d > received_at.astimezone(SHANGHAI).date():
                    raise ValueError()
                result.bars.append((key, BarPayload(open=row['open'],high=row['high'],low=row['low'],close=row['close'],
                    interval='1d', adjustment='unadjusted', trade_day=d, price_unit='point' if key.asset_type=='index' else 'CNY',bar_end_at=None,
                    volume_shares=None,amount_cny=None)))
            except (ValueError, SourceFailure):
                result.rejected += 1
    return result


class DCFundPagination:
    """A finite same-day exchange, committed only after a short/empty final page.

    Individual pages retain the normal endpoint validation. Only this completed
    exchange can exceed its original 6,000-row single-response ambiguity bound.
    An overlap fails the entire exchange; silently dropping duplicates would
    hide ignored offsets and make a partial response look complete.
    """
    def __init__(self,trade_date):
        day(trade_date)
        self.trade_date=trade_date
        self.fields=None
        self.rows=[]
        self.codes=set()
        self.page_count=0
        self.received_at=None
        self.finished=False
        self.page_lineage=[]

    @property
    def offset(self):
        return self.page_count*DC_FUND_PAGE_SIZE

    def add(self,body,received_at):
        if self.finished or self.page_count>=DC_FUND_MAX_PAGES:
            raise SourceFailure('pagination_limit_reached')
        fields,rows=table(body,allow_empty_columns=True)
        if set(fields)&{'source_page_offset','source_page_received_at','source_page_sha256'}:
            raise SourceFailure('schema_changed')
        if len(rows)>DC_FUND_PAGE_SIZE:
            raise SourceFailure('pagination_size_mismatch')
        if not rows and self.page_count==0:
            raise SourceFailure('daily_data_not_published')
        # The supplier may return fields=[] only for a successful final empty
        # page; malformed/null columns are still rejected by table().
        if fields:
            if self.fields is not None and fields!=self.fields:
                raise SourceFailure('pagination_fields_changed')
            validate_dated_table(SPECS['tushare_moneyflow_dc'],fields,rows,received_at)
        if self.received_at is not None and received_at<self.received_at:
            raise SourceFailure('invalid_source_time')
        if any(row['trade_date']!=self.trade_date for row in rows):
            raise SourceFailure('history_response_out_of_range')
        codes={row['ts_code'] for row in rows}
        if codes & self.codes:
            raise SourceFailure('pagination_overlap')
        if len(self.rows)+len(rows)>DC_FUND_MAX_ROWS:
            raise SourceFailure('market_scope_truncated')
        self.page_lineage.extend([{
            'source_page_offset':str(self.offset),'source_page_received_at':received_at.isoformat(),
            'source_page_sha256':hashlib.sha256(body).hexdigest()} for _ in rows])
        self.fields=self.fields or fields
        self.rows.extend(rows)
        self.codes.update(codes)
        self.page_count+=1
        self.received_at=received_at
        self.finished=len(rows)<DC_FUND_PAGE_SIZE
        if not self.finished and self.page_count>=DC_FUND_MAX_PAGES:
            raise SourceFailure('market_scope_truncated')

    def batch(self):
        if not self.finished or not self.rows:
            raise SourceFailure('market_scope_truncated')
        fields=[*self.fields,'source_page_offset','source_page_received_at','source_page_sha256']
        rows=[{**row,**lineage} for row,lineage in zip(self.rows,self.page_lineage,strict=True)]
        result=_reference_table(fields,rows,'tushare_moneyflow_dc',self.received_at,paginated_dc=True)
        result.requests=self.page_count
        key,payload,instrument=result.records[0]
        payload=payload.model_copy(update={'scope':
            f'同一交易日有界分页，每页{DC_FUND_PAGE_SIZE}条、最多{DC_FUND_MAX_PAGES}次请求；以短页或空页确认本次返回结束，跨页重叠与总量超过{DC_FUND_MAX_ROWS}拒绝。本批{len(self.rows)}条，不代表证券目录全覆盖。'+payload.scope})
        result.records[0]=(key,payload,instrument)
        return result


def validate_dated_table(spec, fields, rows, received_at, *, paginated_dc=False):
    if spec.api == 'moneyflow_mkt_dc':
        if not {'trade_date', 'net_amount'}.issubset(fields) or len(rows) >= 3000:
            raise SourceFailure('schema_changed')
        days = [day(row['trade_date']) for row in rows]
        if len(set(days)) != len(days) or any(d > received_at.astimezone(SHANGHAI).date() for d in days):
            raise SourceFailure('invalid_source_time')
        return
    required = {'trade_date','ts_code'} | ({'con_code','name'} if spec.api == 'dc_member' else
        {'name'} if spec.api == 'dc_index' else {'net_amount','name'} if spec.api in {'moneyflow_dc','moneyflow_ind_dc'} else
        {'nums','name'} if spec.api == 'limit_step' else {'pct_change','vol'} if spec.api == 'ths_daily' else {'float_share','pre_close','up_limit','down_limit'})
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    cap = 3000 if spec.api == 'ths_daily' else 6000 if spec.api == 'moneyflow_dc' else 8000 if spec.api == 'stk_premarket' else 2000 if spec.api == 'limit_step' else 5000
    if paginated_dc and spec.api!='moneyflow_dc':
        raise SourceFailure('endpoint_not_allowed')
    if (paginated_dc and len(rows)>DC_FUND_MAX_ROWS) or (not paginated_dc and len(rows)>=cap):
        raise SourceFailure('market_scope_truncated')
    seen = set()
    for row in rows:
        if day(row['trade_date']) > received_at.astimezone(SHANGHAI).date():
            raise SourceFailure('future_source_time')
        pattern = r'\d{6}[RB]?\.TI' if spec.api == 'ths_daily' else r'BK\d{4,8}\.DC' if spec.api in {'dc_index','dc_member','moneyflow_ind_dc'} else r'\d{6}\.(?:SH|SZ|BJ)'
        if not row['ts_code'] or not re.fullmatch(pattern, row['ts_code']):
            raise SourceFailure('instrument_mismatch')
        if spec.api == 'dc_member' and (not row['con_code'] or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', row['con_code'])):
            raise SourceFailure('instrument_mismatch')
        sector_labels = {'行业', '行业板块'} if spec.sector_type == 'industry' else {'概念', '概念板块'}
        if spec.api == 'dc_index' and row.get('idx_type') not in sector_labels | ({None, ''} if spec.sector_type == 'concept' else set()):
            raise SourceFailure('unexpected_sector_type')
        if spec.api == 'moneyflow_ind_dc' and row.get('content_type') not in sector_labels | ({None, ''} if spec.sector_type == 'concept' else set()):
            raise SourceFailure('unexpected_sector_type')
        identity = (row['trade_date'], row['ts_code'], row.get('con_code'))
        if identity in seen:
            raise SourceFailure('duplicate_instrument')
        seen.add(identity)
        for field in REFERENCE_UNITS.get(spec.api, {}):
            value = row.get(field)
            if value not in {None,''}:
                try:
                    numeric=Decimal(value)
                    if not numeric.is_finite() or field in {'total_share','float_share','pre_close','up_limit','down_limit','close','total_mv'} and numeric<0:
                        raise ValueError()
                except (ValueError, ArithmeticError):
                    raise SourceFailure('invalid_number') from None
        if spec.api == 'limit_step':
            try:
                count=Decimal(row['nums']) if row['nums'] else Decimal(0)
                if not count.is_finite() or count!=count.to_integral_value() or not 1<=count<=99:raise ValueError()
            except (ValueError,TypeError,ArithmeticError):
                raise SourceFailure('invalid_number') from None


def validate_extended_table(spec, fields, rows, received_at, key):
    required={'index_basic':{'ts_code','name','market'},
              'stk_limit':{'ts_code','trade_date','up_limit','down_limit'},
              'dividend':{'ts_code','end_date','ann_date','div_proc'}}[spec.api]
    if not required.issubset(fields):
        raise SourceFailure('schema_changed')
    today=received_at.astimezone(SHANGHAI).date()
    seen=set()
    for row in rows:
        symbol=row['ts_code']
        pattern=r'[A-Za-z0-9]{1,32}\.(?:SH|SZ)' if spec.api=='index_basic' else r'\d{6}\.(?:SH|SZ|BJ)'
        if not symbol or not re.fullmatch(pattern,symbol):
            raise SourceFailure('instrument_mismatch')
        if spec.api=='index_basic':
            if row['market']!=spec.exchange or not symbol.endswith('.'+SUFFIX[spec.exchange]):
                raise SourceFailure('instrument_mismatch')
            if not row['name'] or len(row['name'])>200 or any(ord(c)<32 for c in row['name']):
                raise SourceFailure('invalid_name')
            identity=symbol
        elif spec.api=='stk_limit':
            if key is None or day(row['trade_date'])>today:
                raise SourceFailure('invalid_source_time')
            upper,lower=[decimal_value(row.get(f) or '',positive=True) for f in ['up_limit','down_limit']]
            if upper is not None and lower is not None and upper<lower:
                raise SourceFailure('inconsistent_price_range')
            identity=(symbol,row['trade_date'])
        else:
            if key is None or row['ann_date'] and day(row['ann_date'])>today:
                raise SourceFailure('invalid_source_time')
            if day(row['end_date'])>today or row.get('imp_ann_date') and day(row['imp_ann_date'])>today:
                raise SourceFailure('invalid_source_time')
            # Future ex/record/payment dates are announced schedules, not an
            # already-completed action. Keep them as original fields.
            for f in ['record_date','ex_date','pay_date','imp_ann_date']:
                if row.get(f):day(row[f])
            identity=tuple(row.get(f) for f in ['ts_code','end_date','ann_date','div_proc','imp_ann_date'])
        if identity in seen:raise SourceFailure('duplicate_instrument')
        seen.add(identity)


def parameters(family, selection, started, *, board_codes=None):
    """Bounded base parameters; moneyflow_dc uses its separately verified pager."""
    spec = SPECS[family]
    today = started.astimezone(SHANGHAI).date()
    params = {'limit': 8000 if spec.mode in {'catalog','calendar'} else 400}
    if spec.selected:
        if len(selection) != 1:
            raise SourceFailure('invalid_instrument_batch')
        params['ts_code'] = code(selection[0])
    if spec.mode == 'market_snapshot':
        params.update(ts_code='6*.SH,0*.SZ,3*.SZ,9*.BJ',limit=6000)
    elif spec.mode == 'fixed_indices':
        params.update(ts_code='000001.SH,399001.SZ,399006.SZ,000688.SH',limit=4)
    elif spec.mode == 'limit_snapshot':
        params.update(trade_date=today.strftime('%Y%m%d'),limit=8000)
    elif spec.mode == 'catalog':
        params['list_status'] = 'L'
    elif spec.mode == 'calendar':
        params.update(exchange=spec.exchange, start_date=today.replace(day=1).strftime('%Y%m%d'),
            end_date=today.replace(day=calendar.monthrange(today.year,today.month)[1]).strftime('%Y%m%d'))
    elif spec.mode == 'snapshot':
        params['limit'] = 1
    elif spec.mode in {'dated_table','dated_members'}:
        params['limit'] = 3000 if spec.api == 'ths_daily' else 6000 if spec.api == 'moneyflow_dc' else 8000 if spec.api == 'stk_premarket' else 2000 if spec.api == 'limit_step' else 5000
        if spec.api == 'dc_index':params['idx_type'] = '行业板块' if spec.sector_type == 'industry' else '概念板块'
        if spec.api == 'moneyflow_ind_dc':params['content_type'] = '行业' if spec.sector_type == 'industry' else '概念'
        if spec.api == 'dc_member' and board_codes is not None:
            from app.market.sector_scope import member_sector_codes
            params['ts_code']=member_sector_codes({'board_codes':board_codes})[0]
    elif spec.mode == 'ths_members':
        params = {'limit':5000, 'offset':0}
    elif spec.api == 'ths_index':
        params.update(exchange='A', type='N', limit=2000)
    elif spec.api == 'index_basic':
        params.update(market=spec.exchange,limit=2000)
    elif spec.api == 'stk_limit':
        params.update(start_date=(today-timedelta(days=10)).strftime('%Y%m%d'),end_date=today.strftime('%Y%m%d'),limit=20)
    elif spec.api == 'dividend':
        # This endpoint has no start/end-date window. Never silently request
        # unsupported dates or call a bounded provider return complete history.
        params['limit']=100
    elif spec.api == 'cn_cpi':
        params.update(start_m=(today-timedelta(days=365)).strftime('%Y%m'), end_m=today.strftime('%Y%m'))
    elif spec.api in FINANCIAL_APIS:
        params.update(start_date=(today-timedelta(days=730)).strftime('%Y%m%d'),end_date=today.strftime('%Y%m%d'),limit=100)
        if spec.api != 'fina_indicator':params['report_type']='1'
    elif spec.api in {'limit_list_d','limit_list_ths','top_list','top_inst','stk_auction'}:
        # The collector fills trade_date from rights-checked observed calendars;
        # the server's hidden default date is not used.
        params['limit'] = 8000
    else:
        params.update(start_date=(today-timedelta(days=180 if spec.selected else 10)).strftime('%Y%m%d'),end_date=today.strftime('%Y%m%d'))
    if spec.api == 'limit_list_ths':
        params.update(limit=4000, limit_type='涨停池')
    return params
