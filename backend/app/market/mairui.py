"""Licensed personal reference tables, with no credential-bearing provenance URLs."""
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, time as day_time, timedelta
from decimal import Decimal

from sqlalchemy import case, select

from app.evidence.models import EvidenceVersion
from app.evidence.schemas import ReferencePayload
from app.market.protocol import MAX_BODY, SHANGHAI, NoRedirect, Response, SourceFailure
from app.market.reference import ReferenceBatch
from app.models import Provider

FAMILIES = frozenset({'mairui_funds', 'mairui_pools'})
FUNDS = {'industry_flow': 'hibk/zjhhy', 'stock_flow': 'higg/jlr'}
POOLS = {'limit_up': 'hslt/ztgc', 'broken_limit': 'hslt/zbgc', 'limit_down': 'hslt/dtgc'}
TITLES = {'industry_flow': '供应商行业资金流参考', 'stock_flow': '供应商个股资金流参考',
          'limit_up': '供应商涨停股池', 'broken_limit': '供应商炸板股池', 'limit_down': '供应商跌停股池'}
DOCS = 'https://www.mairuiapi.com/'


class MairuiFailure(SourceFailure):
    def __init__(self, reason, requests=0, *, retry_after=None):
        super().__init__(reason, retry_after=retry_after)
        self.request_count = requests


def next_day_wait(clock):
    local = clock.astimezone(SHANGHAI)
    return max(1, int((datetime.combine(local.date() + timedelta(days=1), day_time.min, SHANGHAI) - local).total_seconds()) + 1)


def choose_pool_day(calendars, clock, *, auction=False, preopen=False):
    if clock.utcoffset() is None:
        raise SourceFailure('calendar_not_ready')
    local = clock.astimezone(SHANGHAI)
    today = local.date()
    days = []
    for exchange in ('SSE', 'SZSE'):
        entries = calendars.get(exchange, {})
        if type(entries.get(today)) is not bool:
            raise SourceFailure('calendar_not_ready')
        if entries[today] and (preopen or local.time() >= (day_time(9, 26) if auction else day_time(9, 30))):
            days.append(today)
        else:
            previous = [day for day, opened in entries.items() if opened is True and day < today]
            if not previous:
                raise SourceFailure('calendar_not_ready')
            days.append(max(previous))
    if days[0] != days[1]:
        raise SourceFailure('calendar_not_ready')
    return days[0]


def pool_day(db, clock, *, auction=False, preopen=False):
    """Use rights-checked external calendar evidence, never infer weekdays."""
    if clock.utcoffset() is None:
        raise SourceFailure('calendar_not_ready')
    today = clock.astimezone(SHANGHAI).date()
    records = db.execute(select(EvidenceVersion, Provider).join(Provider, Provider.id == EvidenceVersion.source_id).where(
        EvidenceVersion.kind == 'calendar', EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= clock,
        EvidenceVersion.payload['trade_day'].astext >= str(today - timedelta(days=40)),
        EvidenceVersion.payload['trade_day'].astext <= str(today),
        Provider.family.in_(['tushare_calendar_sse', 'tushare_calendar_szse', 'baostock_daily', 'szse_public']), Provider.configured.is_(True), Provider.enabled.is_(True),
        Provider.entitlement_status == 'verified', Provider.rights['display'].astext == 'allowed', Provider.rights['cache'].astext == 'allowed')
        .order_by(case((Provider.family.in_(['tushare_calendar_sse','tushare_calendar_szse']),0),
            (Provider.family=='baostock_daily',1),else_=2),
            EvidenceVersion.version_number.desc(),EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()))
    calendars = {'SSE': {}, 'SZSE': {}}
    allowed={'SSE':{'tushare_calendar_sse','baostock_daily'},
             'SZSE':{'tushare_calendar_szse','baostock_daily','szse_public'}}
    for evidence, provider in records:
        payload = evidence.payload
        exchange = payload.get('exchange')
        if exchange in calendars and provider.family in allowed[exchange]:
            try:
                day = date.fromisoformat(payload['trade_day'])
            except (ValueError, TypeError, KeyError):
                raise SourceFailure('calendar_not_ready') from None
            calendars[exchange].setdefault(day, payload.get('is_open'))
    return choose_pool_day(calendars, clock, auction=auction, preopen=preopen)


def fetch_mairui(family, credential, reserve, *, query_day=None):
    if family not in FAMILIES:
        raise MairuiFailure('endpoint_not_allowed')
    if credential is None:
        raise MairuiFailure('credentials_missing')
    token = credential.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', token):
        raise MairuiFailure('credentials_invalid')
    if family == 'mairui_pools' and type(query_day) is not date:
        raise MairuiFailure('calendar_not_ready')
    paths = FUNDS if family == 'mairui_funds' else POOLS
    opener = urllib.request.build_opener(NoRedirect())
    started = time.monotonic()
    pieces, requests = [], 0
    try:
        for table, path in paths.items():
            reserve()
            requests += 1
            suffix = ('/' + query_day.isoformat()) if family == 'mairui_pools' else ''
            request = urllib.request.Request('https://api.mairuiapi.com/' + path + suffix + '/' + token,
                headers={'User-Agent': 'Tianyan/1.0 (private licensed research)', 'Accept': 'application/json'})
            with opener.open(request, timeout=15) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise SourceFailure('response_too_large')
            if token.encode() in raw:
                raise SourceFailure('unsafe_upstream_response')
            data = json.loads(raw, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if isinstance(data, dict):
                code = data.get('code')
                if code == 102:
                    raise SourceFailure('permission_missing')
                if code in {101, 103, 429}:
                    raise SourceFailure('rate_limited', retry_after=next_day_wait(datetime.now(UTC)) if code == 101 else 60)
                raise SourceFailure('upstream_unavailable')
            if not isinstance(data, list):
                raise SourceFailure('schema_changed')
            # Wrap original JSON bytes, retaining numeric precision and no URL/key.
            pieces.append(b'{"table":' + json.dumps(table).encode() + b',"rows":' + raw + b'}')
        body = (b'{"family":' + json.dumps(family).encode() + b',"query_day":' + json.dumps(str(query_day) if query_day else None).encode()
                + b',"request_count":' + str(requests).encode() + b',"tables":[' + b','.join(pieces) + b']}')
        if len(body) > MAX_BODY:
            raise SourceFailure('response_too_large')
        return Response(body, datetime.now(UTC), round((time.monotonic() - started) * 1000), 200)
    except urllib.error.HTTPError as error:
        reason = 'permission_missing' if error.code in {401, 403} else 'rate_limited' if error.code == 429 else 'upstream_unavailable'
        raise MairuiFailure(reason, requests, retry_after=60 if error.code == 429 else None) from None
    except SourceFailure as error:
        raise MairuiFailure(error.reason, requests, retry_after=error.retry_after) from None
    except (TimeoutError, socket.timeout):
        raise MairuiFailure('timeout', requests) from None
    except (OSError, urllib.error.URLError):
        raise MairuiFailure('transport_error', requests) from None
    except (ValueError, TypeError, UnicodeError):
        raise MairuiFailure('schema_changed', requests) from None


def text(value, *, required=False, maximum=100):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 or c in '<>' for c in value):
        raise ValueError('invalid supplier text')
    return value


def number(value, *, integer=False, nonnegative=False):
    if value is None:
        return None
    if type(value) not in {int, Decimal, str}:
        raise ValueError('invalid number')
    raw = format(value, 'f') if isinstance(value, Decimal) else str(value)
    if not re.fullmatch(r'-?\d{1,16}' + ('' if integer else r'(?:\.\d{1,18})?'), raw) or (nonnegative and Decimal(raw) < 0):
        raise ValueError('invalid number')
    return raw


def clock_text(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3])(?::[0-5]\d:[0-5]\d|[0-5]\d[0-5]\d)', value):
        raise ValueError('invalid time text')
    day_time.fromisoformat(value)
    return value


def parse_mairui(body, received_at, family):
    try:
        if family not in FAMILIES or received_at.utcoffset() is None or len(body) > MAX_BODY:
            raise ValueError('invalid envelope')
        value = json.loads(body, parse_float=Decimal, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        tables = FUNDS if family == 'mairui_funds' else POOLS
        if not isinstance(value, dict) or value.get('family') != family or value.get('request_count') != len(tables):
            raise ValueError('wrong family or request count')
        groups = value.get('tables')
        if not isinstance(groups, list) or [group.get('table') for group in groups if isinstance(group, dict)] != list(tables):
            raise ValueError('unexpected tables')
        query_day = date.fromisoformat(value['query_day']) if family == 'mairui_pools' else None
        if query_day and query_day > received_at.astimezone(SHANGHAI).date():
            raise ValueError('future query date')
        records = []
        for group in groups:
            table = group['table']
            if not isinstance(group.get('rows'), list) or len(group['rows']) > 8000:
                raise ValueError('oversized table')
            rows, seen, source_days = [], set(), set()
            is_funds = family == 'mairui_funds'
            fields = ([('jj', '均价', '元'), ('zdf', '涨跌幅', '%'), ('lrzj', '流入资金', '元'), ('lczj', '流出资金', '元'), ('jlr', '净流入', '元'), ('jlrl', '净流入率', '%')]
                if table == 'industry_flow' else [('zxj', '参考价', '元'), ('zdf', '涨跌幅', '%'), ('hsl', '换手率', '%'), ('cje', '成交额', '元'), ('lrzj', '流入资金', '元'), ('lczj', '流出资金', '元'), ('jlr', '净流入', '元'), ('jlrl', '净流入率', '%')]
                if is_funds else [('p', '池内价格', '元'), ('zf', '涨跌幅', '%'), ('cje', '成交额', '元'), ('hs', '换手率', '%'), ('lbc', '供应商连板数', None), ('zbc', '供应商炸板次数', None), ('zj', '封板资金', '元'), ('ztp', '涨停价', '元')])
            for index, row in enumerate(group['rows']):
                if not isinstance(row, dict):
                    raise ValueError('invalid row')
                symbol = text(row.get('dm'), required=True, maximum=40)
                if symbol in seen or (table != 'industry_flow' and not re.fullmatch(r'(?:sh|sz|bj)?\d{6}', symbol)):
                    raise ValueError('duplicate or invalid supplier identity')
                seen.add(symbol)
                item = {'source_order': str(index + 1), 'symbol': symbol, 'name': text(row.get('mc'), required=True)}
                item.update({key: number(row.get(key), integer=key in {'lbc', 'zbc'}, nonnegative=key in {'jj', 'zxj', 'p', 'cje', 'hs', 'hsl', 'lbc', 'zbc', 'zj', 'ztp'}) for key, _, _ in fields})
                if is_funds:
                    stamp = text(row.get('t'), required=True, maximum=30)
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2} ?\d{2}:\d{2}:\d{2}', stamp):
                        raise ValueError('invalid raw server update')
                    parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S' if ' ' in stamp else '%Y-%m-%d%H:%M:%S')
                    if parsed.date() > received_at.astimezone(SHANGHAI).date():
                        raise ValueError('future source date')
                    source_days.add(parsed.date())
                    item['updated_raw'] = stamp
                else:
                    item.update({'first_limit_raw': clock_text(row.get('fbt')), 'last_limit_raw': clock_text(row.get('lbt')),
                        'limit_statistic': text(row.get('tj')), 'industry': text(row.get('hy')),
                        'extra_zdf_raw': number(row.get('zdf'))})
                rows.append(item)
            if len(source_days) > 1:
                raise ValueError('mixed source update dates')
            day = next(iter(source_days), None) if is_funds else query_day
            scope = ('供应商盘后资金流参考，更新日来自原始服务器更新时间；原时区未注明，不补成行情时刻。流入/净流入为供应商口径，不等于实际识别主力或本地规则；概念资金流端点未接入。'
                if is_funds else '按已核交易日历选择的请求日期查询；原响应未提供独立数据日期或完整更新时间，日期依据接口请求参数。封板时间保留HHmmss或HH:mm:ss原文，不拼为发布/事件时刻；连板/炸板为供应商字段，不是本地规则结果。涨跌幅采用文档zf字段；实际额外zdf原值单列，口径/单位未核，不与zf合并。')
            payload = ReferencePayload(dataset='market.mairui.' + table, title=TITLES[table], source_url=DOCS + ('hanalyse' if is_funds else 'hsdata'),
                attribution='麦蕊智数 · 本人免费证书', observed_day=day, scope=scope + ' 保留返回顺序和供应商代码，不创建本地证券身份。仅本人本机研究/缓存，禁止导出、转售和再分发；缺值不补零，非逐笔实时或完整历史。',
                columns=[{'key': 'source_order', 'label': '原返回顺序'}, {'key': 'symbol', 'label': '供应商代码'}, {'key': 'name', 'label': '名称'},
                    *[{'key': key, 'label': label, 'unit': unit} for key, label, unit in fields],
                    *([{'key': 'updated_raw', 'label': '服务器更新时间原文（时区未明）'}] if is_funds else [
                        {'key': 'first_limit_raw', 'label': '首次封板原文'}, {'key': 'last_limit_raw', 'label': '末次封板原文'},
                        {'key': 'limit_statistic', 'label': '涨停统计原值'}, {'key': 'industry', 'label': '供应商行业'},
                        {'key': 'extra_zdf_raw', 'label': '附加zdf原值（口径未核）'}])], rows=rows)
            records.append((payload.dataset, payload, None))
        return ReferenceBatch(records=records, expected=len(tables), requests=len(tables))
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise SourceFailure('schema_changed') from None
