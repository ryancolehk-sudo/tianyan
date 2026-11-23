"""Bounded public direction-board snapshots, independently implemented.

Only verified raw fields are mapped. These are observation tables, never trade
signals or evidence of what a historical observer knew. No upstream message or
body is interpolated into failures. There are no credentials or copied crawlers.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, NoRedirect, Response, SourceFailure
from app.market.reference import ReferenceBatch

FAMILIES = frozenset({
    'direction_kpl_emotion',
    'direction_ths_hot', 'direction_tdx_hot', 'direction_em_hot', 'direction_kpl_strength',
})
MAX_BODY = 2_000_000
# Longer/unrepresentable waits require explicit source repair, never an
# automatic retry earlier than the supplier requested.
MAX_RETRY_AFTER = 315360000
MAX_HOT_ROWS = 100
MAX_STRENGTH_ROWS = 400
TOP_COUNT = 10
THS_URL = 'https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?stock_type=a&type=hour&list_type=normal'
TDX_URL = 'https://pul.tdx.com.cn/TQLEX?Entry=JNLPSE.hotStockList&RI='
EM_URL = 'https://emappdata.eastmoney.com/stockrank/getAllCurrentList'
KPL_URL = 'https://apphwhq.longhuvip.com/w1/api/index.php'
KPL_HISTORY_URL = 'https://apphis.longhuvip.com/w1/api/index.php'
ALLOWED_HOSTS = frozenset({
    'dq.10jqka.com.cn', 'pul.tdx.com.cn', 'emappdata.eastmoney.com',
    'apphwhq.longhuvip.com', 'apphis.longhuvip.com',
})
_SOURCES = {
    'direction_kpl_emotion': ('kpl_emotion', '开盘啦市场情绪', '开盘啦公开情绪指标', KPL_URL),
    'direction_ths_hot': ('ths_hot', '同花顺人气前10', '同花顺公开人气榜', THS_URL),
    'direction_tdx_hot': ('tdx_hot', '通达信热搜前10', '通达信公开热搜榜', TDX_URL),
    'direction_em_hot': ('em_hot', '东方财富人气前10', '东方财富股吧公开人气榜', EM_URL),
    'direction_kpl_strength': ('kpl_strength', '开盘啦题材强度前10', '开盘啦公开题材排行', KPL_URL),
}
_HOT_COLUMNS = (
    ('rank', '排名', None), ('code', '证券代码', None), ('name', '名称', None),
    ('heat', '来源人气值', None), ('rank_change', '来源排名变化', '名'),
    ('pct_change', '涨跌幅', '%'), ('source_day', '来源数据日期', None),
    ('source_time', '来源行情时刻', None),
)
_STRENGTH_COLUMNS = (
    ('rank', '排名', None), ('code', '题材代码', None), ('name', '题材名称', None),
    ('strength', '来源题材强度', None), ('source_day', '来源数据日期', None),
    ('source_time', '来源行情时刻', None),
)

# Only fixed diagnostics may cross the collector boundary. Neither arbitrary
# upstream keys/messages nor Python exception strings are retained here.
_DIAGNOSTIC_FIELDS = ('code', 'errcode', 'status_code', 'data', 'list', 'Day', 'Count', 'Time',
                      'errmsg', 'message', 'msg')
_VALIDATION_CODES = {
    'duplicate JSON key': 'duplicate_json_key',
    'nonfinite JSON constant': 'nonfinite_json_number',
    'invalid response budget': 'response_size',
    'invalid number': 'numeric_field', 'nonintegral number': 'integer_field',
    'invalid rank': 'rank_value', 'invalid name': 'display_name',
    'invalid stock code': 'stock_identity', 'invalid source day': 'source_day',
    'future source day': 'future_source_day', 'invalid row budget': 'row_budget',
    'invalid EM envelope': 'em_envelope', 'invalid EM row': 'em_row',
    'invalid EM code': 'em_identity', 'EM exchange mismatch': 'em_exchange_mismatch',
    'invalid KPL envelope': 'kpl_envelope', 'ambiguous KPL source day': 'kpl_source_day',
    'invalid KPL theme row': 'kpl_row_shape', 'KPL strength order changed': 'kpl_strength_order',
    'KPL count mismatch': 'kpl_count', 'duplicate identity': 'duplicate_identity',
    'KPL first page size': 'kpl_first_page_size',
    'ambiguous ranking': 'noncontiguous_ranks', 'invalid receipt/request date': 'request_clock',
}


def _shape(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float, Decimal)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'other'


def _diagnostic(family, body, data, error):
    """A bounded schema fingerprint; deliberately insufficient to reconstruct rows."""
    checkpoint = _VALIDATION_CODES.get(str(error), 'invalid_fields')
    if isinstance(error, SourceFailure):
        checkpoint = {'upstream_unavailable': 'source_rejected_or_empty',
                      'source_date_mismatch': 'source_date_mismatch'}.get(error.reason, 'source_rejected')
    elif isinstance(error, (json.JSONDecodeError, UnicodeError, RecursionError)):
        checkpoint = 'invalid_json'
    result = {'version': 1, 'family': family, 'checkpoint': checkpoint, 'root_shape': _shape(data)}
    if isinstance(body, bytes):
        result['body_bytes'] = len(body)
        if len(body) <= MAX_BODY:
            result['body_sha256'] = hashlib.sha256(body).hexdigest()
    table = None
    if isinstance(data, dict):
        result['fields'] = {key: _shape(data[key]) for key in _DIAGNOSTIC_FIELDS if key in data}
        result['other_field_count'] = sum(key not in _DIAGNOSTIC_FIELDS for key in data)
        status_field = {'direction_em_hot': 'code', 'direction_kpl_strength': 'errcode',
                        'direction_ths_hot': 'status_code'}.get(family)
        status = data.get(status_field)
        if type(status) in (int, str) and re.fullmatch(r'-?\d{1,9}', str(status)):
            result['source_error_code'] = str(status)
        if data.get('errmsg') == '参数出错':
            result['message_category'] = 'parameter_error'
        if family == 'direction_kpl_strength':
            count=data.get('Count')
            if type(count) in (str,int) and re.fullmatch(r'\d{1,5}',str(count)) and int(count)<=10000:
                result['source_total_count']=str(int(count))
            table = data.get('list')
        elif family == 'direction_em_hot':
            table = data.get('data')
        elif isinstance(data.get('data'), dict):
            table = data['data'].get('stock_list')
    elif family == 'direction_tdx_hot' and isinstance(data, list):
        table = data[3:]
    if isinstance(table, list):
        result['row_count'] = len(table)
        sample = table[:MAX_STRENGTH_ROWS]
        result['row_shapes'] = sorted({_shape(row) for row in sample})
        result['row_widths'] = sorted({min(len(row), 1000) for row in sample if isinstance(row, list)})[:8]
        result['row_fields'] = {key: sorted({_shape(row[key]) for row in sample
                                            if isinstance(row, dict) and key in row})
                                for key in ('sc', 'rk', 'code', 'order', 'name')
                                if any(isinstance(row, dict) and key in row for row in sample)}
        if family=='direction_kpl_strength' and sample and all(isinstance(row,list) and len(row)==19 for row in sample):
            try:
                values=[_number(row[2]) for row in sample]
                if all(value is not None for value in values):
                    values=[Decimal(value) for value in values];head,tail=values[:10],values[10:]
                    result['top_strength_order']=dict(
                        head_descending=all(a>=b for a,b in zip(head,head[1:])),
                        head_dominates_tail=min(head)>=max(tail) if tail else True,
                        head_inversions=sum(b>a for a,b in zip(head,head[1:])),
                        tail_inversions=sum(b>a for a,b in zip(tail,tail[1:])))
            except (ValueError,TypeError,InvalidOperation):
                pass
    return result


def _failure(reason, count=1):
    failure = SourceFailure(reason)
    failure.request_count = count
    return failure


def _request(family, day, *, index=0, plate_code=None, plate_summary=False):
    """Fixed origins and one request only: never paginate or follow redirects."""
    if type(index) is not int or not 0 <= index <= 400 or (index and family != 'direction_kpl_strength'):
        raise _failure('invalid_request_page', 0)
    if family not in FAMILIES:
        raise _failure('capability_not_implemented', 0)
    if plate_code is not None and (family != 'direction_kpl_strength' or not re.fullmatch(r'\d{6}', plate_code)):
        raise _failure('invalid_instrument', 0)
    if plate_summary and (plate_code is None or day is not None):
        raise _failure('invalid_request_day', 0)
    today = datetime.now(UTC).astimezone(SHANGHAI).date()
    if day is not None and (type(day) is not date or day > today):
        raise _failure('invalid_request_day', 0)
    if day is not None and family != 'direction_kpl_strength':
        raise _failure('historical_not_supported', 0)
    url = _SOURCES[family][3]
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity',
        'User-Agent': 'Tianyan/1.0 (private research)'}
    body = None
    if family == 'direction_kpl_emotion':
        return urllib.request.Request(url + '?a=ChangeStatistics&st=10&c=HomeDingPan',
            headers={**headers, 'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 12)'})
    if family == 'direction_tdx_hot':
        body = json.dumps([{'listType': '0', 'cycle': '0'}]).encode()
        headers['Content-Type'] = 'application/json'
    elif family == 'direction_em_hot':
        body = json.dumps({'appId': 'appId01', 'globalId': '786e4c21-70dc-435a-93bb-38',
            'marketType': '', 'pageNo': 1, 'pageSize': MAX_HOT_ROWS}).encode()
        headers['Content-Type'] = 'application/json'
    elif family == 'direction_kpl_strength':
        # Type=1 and Order=1 is the source's descending strength order; ZSType=7
        # is its own theme universe. Current requests MUST omit Date: a future
        # trading-date request can return an empty list and an older Day.
        params = {'a': 'RealRankingInfo', 'c': 'ZhiShuRanking', 'Order': '1',
            'Type': '1', 'ZSType': '7', 'Index': str(index), 'st': '30', 'PhoneOSNew': '1',
            'DeviceID': str(uuid.uuid4()), 'VerSion': '5.21.0.2', 'apiv': 'w42'}
        if plate_code is not None:
            params.update(a='ZhiShuStockList_W8', PlateID=plate_code, Type='6', st='1000', old='1')
            params.pop('ZSType')
            if plate_summary:
                params.update(a='GetPlate_Info_QJ', Date='')
                for key in ('Type', 'st', 'old', 'Index', 'Order'):
                    params.pop(key)
        if day is not None:
            params['Date'] = day.isoformat()
            url = KPL_HISTORY_URL
        body = urllib.parse.urlencode(params).encode()
        headers.update({'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 12)',
            'Content-Type': 'application/x-www-form-urlencoded'})
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.port not in {None, 443} or parsed.username or parsed.password
            or parsed.fragment or url not in {THS_URL, TDX_URL, EM_URL, KPL_URL, KPL_HISTORY_URL}):
        raise _failure('endpoint_not_allowed', 0)
    # Dated archives and the current summary use their verified GET contract.
    # Current ZhiShuStockList_W8 leaders accept POST; extending the archive GET
    # rule to all plate requests caused unavailable/transport failures.
    if family == 'direction_kpl_strength' and (day is not None or plate_summary):
        return urllib.request.Request(url + '?' + body.decode('ascii'), headers=headers)
    return urllib.request.Request(url, data=body, headers=headers)


def fetch(family, *, day=None, index=0, plate_code=None, plate_summary=False):
    """Fetch one public snapshot with verified TLS and a 15-second/2 MB budget."""
    request = _request(family, day, index=index, plate_code=plate_code, plate_summary=plate_summary)
    started = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise _failure('response_too_large')
            if response.headers.get('Content-Encoding', '').strip().lower() not in {'', 'identity'}:
                raise _failure('unsafe_upstream_response')
            if not 200 <= response.status < 300:
                raise _failure('upstream_unavailable')
            return Response(body, datetime.now(UTC), round((time.monotonic() - started) * 1000), response.status)
    except urllib.error.HTTPError as error:
        failure = _failure('permission_missing' if error.code in {401, 403} else
            'rate_limited' if error.code == 429 else 'upstream_unavailable')
        if error.code == 429:
            raw = (error.headers.get('Retry-After', '') if error.headers else '').strip()
            if re.fullmatch(r'\d+', raw):
                if len(raw)>12 or int(raw)>MAX_RETRY_AFTER:
                    raise _failure('retry_after_out_of_range') from None
                failure.retry_after = int(raw)
            elif len(raw) <= 80:
                from email.utils import parsedate_to_datetime
                import math
                try:
                    deadline = parsedate_to_datetime(raw)
                    if deadline.tzinfo is not None:
                        seconds=max(0,math.ceil((deadline-datetime.now(UTC)).total_seconds()))
                        if seconds>MAX_RETRY_AFTER:
                            raise _failure('retry_after_out_of_range') from None
                        failure.retry_after=seconds
                except (ValueError, TypeError, OverflowError):
                    pass
        raise failure from None
    except TimeoutError:
        raise _failure('timeout') from None
    except (urllib.error.URLError, OSError):
        raise _failure('transport_error') from None
    except SourceFailure as error:
        error.request_count = 1
        raise


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError('nonfinite JSON constant')


def _unpack(family, body):
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY:
        raise ValueError('invalid response budget')
    try:
        raw = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        if family != 'direction_tdx_hot':
            raise
        raw = body.decode('gb18030')
    return json.loads(raw, parse_float=Decimal, parse_constant=_bad_constant,
        object_pairs_hook=_object)


def _number(value, *, integer=False, signed=True, maximum=Decimal('1e16')):
    if value is None or value in ('', '--', '-'):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('invalid number')
    raw = str(value)
    if not re.fullmatch(r'[+-]?\d{1,17}(?:\.\d{1,10})?', raw):
        raise ValueError('invalid number')
    numeric = Decimal(raw)
    if not numeric.is_finite() or abs(numeric) > maximum or (not signed and numeric < 0):
        raise ValueError('invalid number')
    if integer:
        if numeric != numeric.to_integral_value():
            raise ValueError('nonintegral number')
        return str(int(numeric))
    if numeric == 0:
        return '0'
    return format(numeric, 'f').rstrip('0').rstrip('.') if '.' in raw else str(numeric)


def _rank(value, maximum=MAX_HOT_ROWS):
    result = _number(value, integer=True, signed=False, maximum=Decimal(maximum))
    if result is None or int(result) < 1:
        raise ValueError('invalid rank')
    return result


def _name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.strip()
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
            or any(c in value for c in ('<', '>', '\ufffd', '\u202e', '\u202d'))):
        raise ValueError('invalid name')
    return value


def _stock_code(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[03648]\d{5}|92\d{4})', value):
        raise ValueError('invalid stock code')
    return value


def _day(value, received_at):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('invalid source day')
    result = date.fromisoformat(value)
    if result > received_at.astimezone(SHANGHAI).date():
        raise ValueError('future source day')
    return result


def _hot_row(rank, code, name=None, *, heat=None, change=None, pct=None, source_day=None):
    return {'rank': _rank(rank), 'code': _stock_code(code),
        'name': _name(name) if name is not None else None,
        'heat': _number(heat, signed=False),
        'rank_change': _number(change, integer=True, maximum=Decimal(100000)),
        'pct_change': _number(pct, maximum=Decimal(1000)),
        'source_day': source_day, 'source_time': None}


def _rows(value, maximum=MAX_HOT_ROWS):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('invalid row budget')
    if not value:
        raise SourceFailure('upstream_unavailable')
    return value


def _ths(data):
    if not isinstance(data, dict) or 'status_code' not in data:
        raise ValueError('invalid THS envelope')
    if isinstance(data['status_code'], bool) or str(data['status_code']) != '0':
        raise SourceFailure('upstream_unavailable')
    table = _rows(data['data']['stock_list'])
    rows = []
    for row in table:
        if not isinstance(row, dict):
            raise ValueError('invalid THS row')
        rows.append(_hot_row(row['order'], row['code'], row['name'], heat=row.get('rate'),
            change=row.get('hot_rank_chg'), pct=row.get('rise_and_fall')))
    return rows, None, 'rate为来源人气值；hot_rank_chg为来源名次变化；缺失值不推算。'


def _em(data):
    if not isinstance(data, dict) or 'code' not in data:
        raise ValueError('invalid EM envelope')
    if isinstance(data['code'], bool) or str(data['code']) != '0':
        raise SourceFailure('upstream_unavailable')
    rows = []
    for raw in _rows(data['data']):
        if not isinstance(raw, dict) or not isinstance(raw.get('sc'), str):
            raise ValueError('invalid EM row')
        code = raw['sc']
        if not re.fullmatch(r'(SH|SZ|BJ)\d{6}', code):
            raise ValueError('invalid EM code')
        if ((code[:2] == 'SH' and not code[2:].startswith('6'))
                or (code[:2] == 'SZ' and not code[2:].startswith(('0', '3')))
                or (code[:2] == 'BJ' and not code[2:].startswith(('4', '8', '92')))):
            raise ValueError('EM exchange mismatch')
        rows.append(_hot_row(raw['rk'], code[2:]))
    return rows, None, '来源仅提供证券代码与排名；名称可由本地标的目录补充。rc/hisRc语义未核实，不作为名次变化；人气值、涨跌幅留空。'


def _tdx(data, received_at):
    if not isinstance(data, list) or len(data) < 3 or not isinstance(data[0], list) or len(data[0]) < 3:
        raise ValueError('invalid TDX envelope')
    if isinstance(data[0][0], bool) or str(data[0][0]) != '0':
        raise SourceFailure('upstream_unavailable')
    columns = data[1]
    required = {'market', 'sec_code', 'stock_name', 'chg', 'occur_time', 'ranking'}
    if (not isinstance(columns, list) or not 6 <= len(columns) <= 40
            or any(not isinstance(c, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,59}', c) for c in columns)
            or len(set(columns)) != len(columns) or not required.issubset(columns)
            or not isinstance(data[2], list)):
        raise ValueError('invalid TDX columns')
    rows, days = [], set()
    for raw in _rows(data[3:]):
        if not isinstance(raw, list) or len(raw) != len(columns):
            raise ValueError('invalid TDX row')
        row = dict(zip(columns, raw, strict=True))
        code = _stock_code(row['sec_code'])
        if ((row['market'] == '1' and not code.startswith('6'))
                or (row['market'] == '0' and not code.startswith(('0', '3')))
                or (row['market'] == '2' and not code.startswith(('4', '8', '92')))
                or row['market'] not in {'0', '1', '2'}):
            raise ValueError('TDX exchange mismatch')
        actual = _day(row['occur_time'], received_at)
        days.add(actual)
        rows.append(_hot_row(row['ranking'], code, row['stock_name'], pct=row['chg'],
            source_day=actual.isoformat()))
    if len(days) != 1:
        raise ValueError('mixed source days')
    count = _number(data[0][2], integer=True, signed=False, maximum=Decimal(MAX_HOT_ROWS))
    if count is None or int(count) != len(rows):
        raise ValueError('TDX count mismatch')
    return rows, days.pop(), 'occur_time仅提供来源数据日期，不补造盘中时刻；totalCount/gnbk不解释为人气值或排名变化。'


def _kpl(data, received_at, requested_day, *, check_order=True):
    if not isinstance(data, dict) or 'errcode' not in data:
        raise ValueError('invalid KPL envelope')
    if isinstance(data['errcode'], bool) or str(data['errcode']) != '0':
        raise SourceFailure('upstream_unavailable')
    source_days = data['Day']
    if not isinstance(source_days, list) or len(source_days) != 1:
        raise ValueError('ambiguous KPL source day')
    actual = _day(source_days[0], received_at)
    if requested_day is not None and actual != requested_day:
        raise SourceFailure('source_date_mismatch')
    rows, previous = [], None
    for rank, raw in enumerate(_rows(data['list'], MAX_STRENGTH_ROWS), 1):
        # RealRankingInfo's 19-column theme row is a different schema from the
        # per-plate GetPlate_Info_QJ concept/industry arrays. Never conflate them.
        if (not isinstance(raw, list) or len(raw) != 19 or not isinstance(raw[0], str)
                or not re.fullmatch(r'\d{6}', raw[0])):
            raise ValueError('invalid KPL theme row')
        strength = _number(raw[2])
        if strength is None or (check_order and previous is not None and Decimal(strength) > previous):
            raise ValueError('KPL strength order changed')
        previous = Decimal(strength)
        rows.append({'rank': str(rank), 'code': raw[0], 'name': _name(raw[1]),
            'strength': strength, 'source_day': actual.isoformat(), 'source_time': None})
    count = _number(data['Count'], integer=True, signed=False, maximum=Decimal(10000))
    if count is None or int(count) < len(rows):
        raise ValueError('KPL count mismatch')
    return rows, actual, 'RealRankingInfo题材口径、Type=1/Order=1强度降序，取来源第一页前10；不等同其他软件行业分类。Day为实际题材日期；Time为服务器元信息，未作为行情时刻。'


def parse(family, body, received_at, *, requested_day=None):
    """Normalize one bounded table; source dates always win over request dates."""
    if family not in FAMILIES:
        raise SourceFailure('capability_not_implemented')
    if requested_day is not None and family != 'direction_kpl_strength':
        raise SourceFailure('historical_not_supported')
    if family == 'direction_kpl_emotion':
        from app.market.kpl_emotion import parse as parse_emotion
        return parse_emotion(body, received_at)
    data = None
    try:
        if (not isinstance(received_at, datetime) or received_at.utcoffset() is None
                or (requested_day is not None and (type(requested_day) is not date
                    or requested_day > received_at.astimezone(SHANGHAI).date()))):
            raise ValueError('invalid receipt/request date')
        data = _unpack(family, body)
        if family == 'direction_ths_hot':
            rows, actual, scope = _ths(data)
        elif family == 'direction_em_hot':
            rows, actual, scope = _em(data)
        elif family == 'direction_tdx_hot':
            rows, actual, scope = _tdx(data, received_at)
        else:
            rows, actual, scope = _kpl(data, received_at, requested_day)
        if len({row['code'] for row in rows}) != len(rows):
            raise ValueError('duplicate identity')
        if family != 'direction_kpl_strength':
            if sorted(int(row['rank']) for row in rows) != list(range(1, len(rows) + 1)):
                raise ValueError('ambiguous ranking')
            rows.sort(key=lambda row: int(row['rank']))
        observed = actual or received_at.astimezone(SHANGHAI).date()
        basis = ('observed_day_basis=source_day；来源实际数据日。' if actual else
            'observed_day_basis=received_at_snapshot；来源未给行情日期/时刻，按采集日保存当前快照；received_at仅为采集时间，不是行情时点或当日收盘榜。')
        suffix, title, attribution, url = _SOURCES[family]
        if family == 'direction_kpl_strength' and requested_day is not None:
            url = KPL_HISTORY_URL
        columns = _STRENGTH_COLUMNS if family == 'direction_kpl_strength' else _HOT_COLUMNS
        dataset = 'market.direction.' + suffix
        payload = ReferencePayload(dataset=dataset, title=title, source_url=url,
            attribution=attribution, scope=basis + scope + '仅为公开榜单观察；不推导买入信号。',
            observed_day=observed,
            columns=[{'key': key, 'label': label, 'unit': unit} for key, label, unit in columns],
            rows=rows[:TOP_COUNT])
        return ReferenceBatch(records=[(dataset, payload, None)], expected=1, requests=1)
    except SourceFailure as error:
        error.direction_diagnostic = _diagnostic(family, body, data, error)
        raise
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation, UnicodeError, RecursionError) as error:
        failure = SourceFailure('schema_changed')
        failure.direction_diagnostic = _diagnostic(family, body, data, error)
        raise failure from None
