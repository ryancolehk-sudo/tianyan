"""短线侠公开集合竞价封单表的取数、解码与解析。

请求链路（2026-10-21 复核，公开页面，无需账号）：

* 页面 ``https://duanxianxia.com/web/jjlive``（导航「竞价 → 竞价异动」）
* 脚本 ``/static/js/jjlive.js`` 使用两条公开请求：
  - ``GET /vendor/stockdata/jjlive.json``：当前表，响应体是 AES-256-CBC 加密后的
    Base64（浏览器用 ``decryptData`` 解出 ``{"th","table","t15","t20","t25"}``）。
  - ``POST /api/getFengdanLast``：历史表，明文 JSON ``{"YYYY-MM-DD": {...}}``，
    每个交易日同样含 ``th/table/t15/t20/t25``。
* 页面另有「竞价强度」``/web/jingjia``，未登录 302 到 ``/web/login``，不在本模块范围。

本模块只做有界 HTTP、协议校验、解密与字段解析，返回
``ReferenceBatch``（summary + stocks 两张 ``ReferencePayload``）；不落库、
不限频、不调度、不建本地证券身份。缺失（``-``）保留原文且金额为 ``None``，
零值保留为 ``0``；整表为空时抛 ``SourceFailure('instrument_unavailable')``，
不写任何版本。

对外接口：``auction_url(kind)``、``fetch_auction(kind)``、
``parse_auction(body, received_at, kind)``（单表）、``parse_history(body, received_at)``
（每天一个批次）、``source_keys(family, trade_day, batch_hash)``。
"""
import base64
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as day_time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from app.evidence.schemas import ReferencePayload
from app.market.protocol import SHANGHAI, Response, SourceFailure, fetch_https
from app.market.reference import ReferenceBatch

FAMILY = 'duanxianxia_auction'
HISTORY_FAMILY = 'duanxianxia_auction_history'
FAMILIES = frozenset({FAMILY, HISTORY_FAMILY})
SOURCE_URL = 'https://duanxianxia.com/web/jjlive'
ATTRIBUTION = '短线侠公开网页「竞价异动」封单表（无需登录）'
TABLE_KINDS = ('current', 'history')
KIND_FAMILY = {'current': FAMILY, 'history': HISTORY_FAMILY}
CHECKPOINTS = ('09:15', '09:20', '09:25')
SUMMARY_DATASET = 'market.duanxianxia.auction.summary'
STOCKS_DATASET = 'market.duanxianxia.auction.stocks'
MAX_BODY = 2_000_000
MAX_ROWS = 2000
MAX_TAGS = 12
REQUEST_TIMEOUT = 15

CURRENT_PATH = '/vendor/stockdata/jjlive.json'
HISTORY_PATH = '/api/getFengdanLast'
BASE = 'https://duanxianxia.com'
DATA_ORIGINS = frozenset({BASE, 'https://ds.duanxianxia.com'})
# Independently verified original data URL; it still serves the same encrypted
# dated contract after the legacy browser initialization files were removed.
# This is not the new website redirect, and TLS verification remains enabled.
PINNED_CURRENT_ORIGIN = 'https://ds.duanxianxia.com'
DATASOURCE_URL = BASE + '/vendor/stockdata/datasource.json'
CRYPTO_URL = BASE + '/static/js/crypto.js?v1'
# Exact public script reviewed on 2026-10-21; bytes are hashed, never executed.
CRYPTO_SHA256 = '731070b1655dd298c4c4ac8311c6919dc0ed9bc9e64288266e7721b4d4304fd3'

# A browser checks these initialization resources once, then polls the table.
# Cache only the verified public origin, never a table or a permission decision.
# One entry bounds memory and a lock prevents simultaneous cold initializations.
_METADATA_LOCK = threading.Lock()
_METADATA_CACHE = None


def _metadata_context():
    clock = datetime.now(SHANGHAI)
    inside = day_time(9, 15) <= clock.time() < day_time(9, 35)
    return (clock.date(), inside), 1200 if inside else 300


def _current_origin(read):
    global _METADATA_CACHE
    with _METADATA_LOCK:
        scope, lifetime = _metadata_context()
        tick = time.monotonic()
        if _METADATA_CACHE is not None:
            saved_scope, expires, origin = _METADATA_CACHE
            if saved_scope == scope and tick < expires:
                return origin
        # Expired or out-of-phase metadata must never become fallback data when
        # the replacement configuration or decoder fails validation.
        _METADATA_CACHE = None
        try:
            routing = read(DATASOURCE_URL)
        except SourceFailure as error:
            if error.reason == 'upstream_unavailable' and error.http_status == 404:
                # Only an explicit missing legacy route permits this fixed
                # original endpoint. Authentication, redirects, TLS failures,
                # changed routing/decoder bytes and 5xx errors still fail closed.
                # Do not cache a failed metadata response or execute remote JS.
                return PINNED_CURRENT_ORIGIN
            raise
        try:
            config = json.loads(routing.body)
        except (ValueError, UnicodeError):
            raise SourceFailure('schema_changed') from None
        if not isinstance(config, dict) or type(config.get('istrade')) is not int or config['istrade'] not in {0, 1}:
            raise SourceFailure('schema_changed')
        origins = config.get('base_url')
        data_origin = config.get('data_url')
        if (not isinstance(origins, list) or not origins or
                any(not isinstance(origin, str) or origin not in DATA_ORIGINS for origin in origins) or
                not isinstance(data_origin, str) or data_origin not in DATA_ORIGINS):
            raise SourceFailure('endpoint_not_allowed')
        origin = data_origin if config['istrade'] else origins[0]
        script = read(CRYPTO_URL)
        if hashlib.sha256(script.body).hexdigest() != CRYPTO_SHA256:
            raise SourceFailure('decoder_changed')
        # Start TTL at initialization, so slow requests cannot extend it.
        _METADATA_CACHE = scope, tick + lifetime, origin
        return origin

# 供应商页面内嵌的固定密钥/IV（crypto.js → decryptData），仅用于解开公开响应体。
_CIPHER_KEY = bytes.fromhex(
    '7365637265746b65793332327965732121616161616161616161616161616161')
_CIPHER_IV = bytes.fromhex('666978656469765f313676616c756564')

SCOPE = ('供应商「竞价异动」封单表：每个交易日按 09:15/09:20/09:25 三列展示涨停封单金额，'
         '仅覆盖进入该表的部分标的，不是全市场、不是逐笔委托或未匹配量，也不代表撮合成交完成。'
         '检查点顺序来自供应商表头声明，来源未提供行级时点，因此不宣称精确检查点。'
         '竞价中未来阶段列可能提前填入滚动值，非该阶段已经发生；须结合本机接收时间核验，不能当作实时盘口。'
         '金额原文（亿/万）与换算后的元同时保留，缺失「-」不补 0，零值保留为 0；'
         '汇总为供应商口径合计，不由本模块重算。涨幅仅当前表提供，历史表为 null。'
         '供应商代码不创建本地证券身份。不构成交易建议。')

SUMMARY_COLUMNS = (
    ('trade_day', '交易日', ''),
    ('source_date_raw', '来源日期原文', ''),
    ('source_header_text', '来源表头纯文本', ''),
    ('table_kind', '表类型（当前/历史）', ''),
    ('row_count', '表内标的数', '只'),
    ('one_word_count', '供应商一字数', '只'),
    ('header_total_raw', '表头封单合计原文', ''),
    ('header_total_cny', '表头封单合计', '元'),
    ('total_fd_0915_raw', '09:15封单合计原文', ''),
    ('total_fd_0920_raw', '09:20封单合计原文', ''),
    ('total_fd_0925_raw', '09:25封单合计原文', ''),
    ('total_fd_0915_cny', '09:15封单合计', '元'),
    ('total_fd_0920_cny', '09:20封单合计', '元'),
    ('total_fd_0925_cny', '09:25封单合计', '元'),
    ('batch_hash', '批次哈希', ''),
)
STOCKS_COLUMNS = (
    ('trade_day', '交易日', ''),
    ('source_order', '原表顺序', ''),
    ('code', '供应商代码', ''),
    ('name', '名称', ''),
    ('tags_raw', '供应商标签原文', ''),
    ('board_raw', '连板标签原文', ''),
    ('fd_0915_raw', '09:15封单原文', ''),
    ('fd_0920_raw', '09:20封单原文', ''),
    ('fd_0925_raw', '09:25封单原文', ''),
    ('fd_0915_cny', '09:15封单', '元'),
    ('fd_0920_cny', '09:20封单', '元'),
    ('fd_0925_cny', '09:25封单', '元'),
    ('change_pct_raw', '涨幅原文', ''),
    ('change_pct', '涨幅', '%'),
    ('batch_hash', '批次哈希', ''),
)

_ROW = re.compile(r"<td code='(\d{6})'[^>]*>(.*?)</td>", re.S)
_CELL_SPLIT = '|'
_AMOUNT = re.compile(r'^(?:(\d{1,12}(?:\.\d{1,6})?)\s*(亿|万|元)?|-{1,2})$')
_PCT = re.compile(r'^(-?\d{1,4}(?:\.\d{1,4})?)\s*%$')
_BOARD = re.compile(r'^(?:首板|\d{1,3}板)$')
_TAG = re.compile(r'<p>(.*?)</p>', re.S)
_NAME = re.compile(r'<b>(.*?)<i>(.*?)</i></b>', re.S)
_HEAD_DATE = re.compile(r'^\s*(\d{4}-\d{2}-\d{2})')
_HEAD_ONE_WORD = re.compile(r'一字[:：]\s*(\d{1,4})\s*个')
_HEAD_TOTAL = re.compile(r'封单[:：]\s*(\d{1,12}(?:\.\d{1,6})?\s*[亿万]?)')
_HEAD_LABEL = re.compile(r'<i[^>]*>\s*([^<]{0,12}?)\s*</i>')


# --- AES-256-CBC（仅解密；stdlib 无 AES，避免为一条公开源新增加密依赖） ---

_SBOX = bytes.fromhex(
    '637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0'
    'b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275'
    '09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf'
    'd0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2'
    'cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb'
    'e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08'
    'ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e'
    'e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16')
_INV_SBOX = bytes(_SBOX.index(value) for value in range(256))
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)


def _xtime(value):
    value <<= 1
    return (value ^ 0x11B) if value & 0x100 else value


def _mul(left, right):
    result = 0
    while right:
        if right & 1:
            result ^= left
        left = _xtime(left)
        right >>= 1
    return result


def _round_keys(key):
    if len(key) != 32:
        raise SourceFailure('schema_changed')
    words = [list(key[index:index + 4]) for index in range(0, 32, 4)]
    for index in range(8, 60):
        temp = list(words[index - 1])
        if index % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[value] for value in temp]
            temp[0] ^= _RCON[index // 8 - 1]
        elif index % 8 == 4:
            temp = [_SBOX[value] for value in temp]
        words.append([left ^ right for left, right in zip(words[index - 8], temp, strict=True)])
    return [bytes(sum(words[4 * round:4 * round + 4], [])) for round in range(15)]


def _add_key(state, key):
    for index, value in enumerate(key):
        state[index] ^= value


def _inv_shift_rows(state):
    for row in range(1, 4):
        column = [state[4 * ((col - row) % 4) + row] for col in range(4)]
        for col in range(4):
            state[4 * col + row] = column[col]


def _inv_mix_columns(state):
    for col in range(4):
        a0, a1, a2, a3 = state[4 * col:4 * col + 4]
        state[4 * col] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        state[4 * col + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        state[4 * col + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        state[4 * col + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def _decrypt_block(block, round_keys):
    state = list(block)
    _add_key(state, round_keys[14])
    for round in range(13, 0, -1):
        _inv_shift_rows(state)
        for index, value in enumerate(state):
            state[index] = _INV_SBOX[value]
        _add_key(state, round_keys[round])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    for index, value in enumerate(state):
        state[index] = _INV_SBOX[value]
    _add_key(state, round_keys[0])
    return bytes(state)


def _decrypt_cbc(ciphertext, key, iv):
    if not ciphertext or len(ciphertext) % 16 or len(iv) != 16:
        raise SourceFailure('schema_changed')
    round_keys = _round_keys(key)
    previous, plain = iv, bytearray()
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        mixed = _decrypt_block(block, round_keys)
        plain += bytes(left ^ right for left, right in zip(mixed, previous, strict=True))
        previous = block
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        raise SourceFailure('schema_changed')
    return bytes(plain[:-pad])


# --- 请求 ---


def auction_url(kind, *, cache_bust=True):
    """公开端点 URL；当前表加一次性参数，避免中间层返回旧快照。"""
    if kind == 'current':
        url = BASE + CURRENT_PATH
        return url + (f'?_={time.time_ns() // 1_000_000}' if cache_bust else '')
    if kind == 'history':
        return BASE + HISTORY_PATH
    raise SourceFailure('capability_not_implemented')


def fetch_auction(kind='current') -> Response:
    """Current: verified initialization + uncached table. History: one POST.

    One persisted permit covers the whole task, including failed attempts. There
    are no alternate-host probes. Only a current-table transport failure or
    timeout permits one retry of the identical URL: at most four cold GETs or
    two warm GETs. Initialization failures and history are never retried here.
    """
    requests, elapsed = 0, 0

    def read(url, **kwargs):
        nonlocal requests, elapsed
        requests += 1
        started = time.monotonic()
        try:
            result = fetch_https(url, timeout=REQUEST_TIMEOUT, **kwargs)
        except SourceFailure:
            elapsed += max(0, round((time.monotonic() - started) * 1000))
            raise
        elapsed += result.elapsed_ms
        return result

    try:
        if kind == 'history':
            result = read(auction_url(kind), data=b'')
        elif kind == 'current':
            origin = _current_origin(read)
            url = origin + CURRENT_PATH + f'?_={time.time_ns() // 1_000_000}'
            try:
                result = read(url)
            except SourceFailure as error:
                if error.reason not in {'transport_error', 'timeout'}:
                    raise
                result = read(url)
        else:
            raise SourceFailure('capability_not_implemented')
        return Response(result.body, result.received_at, elapsed, result.status, request_count=requests)
    except SourceFailure as error:
        error.request_count = requests
        raise


# --- 解码与协议校验 ---


def decode_table(body, kind):
    """把单表响应体解成 ``_Table``（当前表先解密；历史表的单日对象同样适用）。"""
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY:
        raise SourceFailure('response_too_large' if body else 'schema_changed')
    try:
        text = body.decode('utf-8')
    except UnicodeError:
        raise SourceFailure('schema_changed') from None
    text = text.strip()
    if kind == 'current':
        text = _decrypt_body(text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise SourceFailure('schema_changed') from None
    if not isinstance(payload, dict) or set(payload) != {'th', 'table', 't15', 't20', 't25'}:
        raise SourceFailure('schema_changed')
    return _one_table(payload)


def _decrypt_body(text):
    if len(text) % 4 or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', text):
        raise SourceFailure('schema_changed')
    try:
        ciphertext = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        raise SourceFailure('schema_changed') from None
    plain = _decrypt_cbc(ciphertext, _CIPHER_KEY, _CIPHER_IV)
    try:
        return plain.decode('utf-8')
    except UnicodeError:
        raise SourceFailure('schema_changed') from None


@dataclass(frozen=True)
class _Table:
    day: date
    date_raw: str
    header_text: str
    one_word_raw: str | None
    header_total_raw: str | None
    total_raw: tuple[str | None, str | None, str | None]
    labels: tuple[str, str, str]
    rows: str


def _one_table(payload):
    header, table = payload['th'], payload['table']
    if not isinstance(header, str) or not isinstance(table, str) or len(header) > 2000 or len(table) > MAX_BODY:
        raise SourceFailure('schema_changed')
    match = _HEAD_DATE.match(header)
    if not match:
        raise SourceFailure('schema_changed')
    try:
        day = date.fromisoformat(match.group(1))
    except ValueError:
        raise SourceFailure('schema_changed') from None
    labels = tuple(value.strip() for value in _HEAD_LABEL.findall(header)[:3])
    expected = tuple(f'{int(part[:2])}:{part[3:]}' for part in CHECKPOINTS)
    if labels != expected:
        # 列序只认供应商表头声明，绝不按位置猜测检查点。
        raise SourceFailure('schema_changed')
    one_word = _HEAD_ONE_WORD.search(header)
    total = _HEAD_TOTAL.search(header)
    totals = tuple(payload[key] for key in ('t15', 't20', 't25'))
    if any(value is not None and not isinstance(value, str) for value in totals):
        raise SourceFailure('schema_changed')
    return _Table(day=day, date_raw=match.group(1), header_text=_plain_text(header), one_word_raw=one_word.group(1) if one_word else None,
                  header_total_raw=total.group(1).strip() if total else None,
                  total_raw=totals, labels=labels, rows=table)


# --- 字段解析 ---


def _amount_raw(value):
    """保留原文；`-`/空为缺失，0 与 0万/0亿都是真实的 0。"""
    if value is None:
        return None, None
    text = value.strip()
    if not text:
        return None, None
    match = _AMOUNT.match(text)
    if not match:
        raise SourceFailure('schema_changed')
    if match.group(1) is None:
        return text, None
    unit = {'亿': Decimal(100_000_000), '万': Decimal(10_000), '元': Decimal(1), None: Decimal(1)}[match.group(2)]
    try:
        amount = Decimal(match.group(1)) * unit
    except InvalidOperation:
        raise SourceFailure('schema_changed') from None
    if amount != amount.to_integral_value() or amount < 0:
        raise SourceFailure('schema_changed')
    return text, str(int(amount))


def _pct_text(text):
    if text in {'', '-', '--'}:
        return None, None
    match = _PCT.match(text)
    if not match:
        raise SourceFailure('schema_changed')
    return text, match.group(1)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif tag in {'br', 'p', 'div'} and not self.hidden:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden:
            self.hidden -= 1
        elif tag in {'p', 'div'} and not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain_text(value):
    parser = _Text()
    parser.feed(value)
    parser.close()
    return ' '.join(''.join(parser.parts).split())


def _cells(body):
    """把 `</b>` 之后按 `|` 分隔的单元格拆成 (文本, 是否涨幅列)；结构变了就拒绝。"""
    tail = body.split('</b>', 1)
    if len(tail) != 2:
        raise SourceFailure('schema_changed')
    cells = []
    for cell in re.sub(r'^\s*<br\s*/?>', '', tail[1]).split(_CELL_SPLIT):
        if "class='zf'" in cell:
            # 涨幅列是双层 span（颜色在外层），取纯文本，颜色不作为数据。
            cells.append((re.sub(r'<[^>]+>', '', cell).strip(), True))
            continue
        match = re.fullmatch(r'\s*<span>\s*([^<>]*?)\s*</span>\s*', cell)
        if not match:
            raise SourceFailure('schema_changed')
        cells.append((match.group(1), False))
    return cells


def _stocks(table, day_text):
    rows, rejected, seen = [], 0, set()
    for order, (code, body) in enumerate(_ROW.findall(table.rows), start=1):
        match = _NAME.search(body)
        if not match:
            raise SourceFailure('schema_changed')
        name = _plain_text(match.group(1))
        if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise SourceFailure('invalid_name')
        tags = [text for tag in _TAG.findall(match.group(2)) if (text := _plain_text(tag))]
        if len(tags) > MAX_TAGS or any(len(tag) > 40 for tag in tags):
            raise SourceFailure('schema_changed')
        cells = _cells(body)
        amounts = [text for text, is_pct in cells if not is_pct]
        changes = [text for text, is_pct in cells if is_pct]
        if len(amounts) != 3 or len(changes) > 1:
            raise SourceFailure('schema_changed')
        if code in seen:
            rejected += 1
            continue
        seen.add(code)
        row = {'trade_day': day_text, 'source_order': str(order), 'code': code, 'name': name,
               'tags_raw': '|'.join(tags) or None,
               'board_raw': next((tag for tag in reversed(tags) if _BOARD.match(tag)), None)}
        for label, cell in zip(CHECKPOINTS, amounts, strict=True):
            raw, amount = _amount_raw(cell)
            suffix = label.replace(':', '')
            row[f'fd_{suffix}_raw'] = raw
            row[f'fd_{suffix}_cny'] = amount
        pct_raw, pct = _pct_text(changes[0]) if changes else (None, None)
        row['change_pct_raw'], row['change_pct'] = pct_raw, pct
        rows.append(row)
    return rows, rejected


def _payload(dataset, title, columns, rows, day):
    return ReferencePayload(dataset=dataset, title=title, source_url=SOURCE_URL, attribution=ATTRIBUTION,
                            scope=SCOPE, columns=[{'key': key, 'label': label, 'unit': unit or None}
                                                  for key, label, unit in columns],
                            rows=rows, observed_day=day)


def source_keys(family, trade_day, batch_hash):
    if family not in FAMILIES:
        raise SourceFailure('capability_not_implemented')
    # A stable daily series records A → B → A as three immutable observations.
    base = f'auction:{family}:{trade_day or "unknown"}'
    return base + ':summary', base + ':stocks'


def _batch(table, kind, received_at, raw_rows, batch_hash):
    if not raw_rows or raw_rows > MAX_ROWS:
        raise SourceFailure('instrument_unavailable' if not raw_rows else 'schema_changed')
    if table.day > received_at.astimezone(SHANGHAI).date():
        raise SourceFailure('future_source_date')
    family = KIND_FAMILY[kind]
    day_text = table.day.isoformat()
    stocks, rejected = _stocks(table, day_text)
    if not stocks:
        raise SourceFailure('instrument_unavailable')
    for row in stocks:
        row['batch_hash'] = batch_hash
    totals = [_amount_raw(value) for value in table.total_raw]
    header_total = _amount_raw(table.header_total_raw)
    summary = {'trade_day': day_text, 'source_date_raw': table.date_raw, 'source_header_text': table.header_text, 'table_kind': kind,
               'row_count': str(len(stocks)), 'one_word_count': table.one_word_raw,
               'header_total_raw': header_total[0], 'header_total_cny': header_total[1],
               'total_fd_0915_raw': totals[0][0], 'total_fd_0920_raw': totals[1][0],
               'total_fd_0925_raw': totals[2][0], 'total_fd_0915_cny': totals[0][1],
               'total_fd_0920_cny': totals[1][1], 'total_fd_0925_cny': totals[2][1],
               'batch_hash': batch_hash}
    title = '短线侠竞价封单' + ('（当前表）' if kind == 'current' else '（历史表）')
    summary_key, stocks_key = source_keys(family, day_text, batch_hash)
    return ReferenceBatch(
        records=[(summary_key, _payload(SUMMARY_DATASET, title + ' · 批次汇总', SUMMARY_COLUMNS, [summary], table.day), None),
                 (stocks_key, _payload(STOCKS_DATASET, title + ' · 个股封单', STOCKS_COLUMNS, stocks, table.day), None)],
        expected=1 + raw_rows, rejected=rejected, requests=1, batch_hash=batch_hash)


def parse_auction(body, received_at, kind='current'):
    """解析当前表或单日历史表，返回 summary + stocks 两表配对的 ``ReferenceBatch``。"""
    if kind not in TABLE_KINDS:
        raise SourceFailure('capability_not_implemented')
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    table = decode_table(body, kind)
    return _batch(table, kind, received_at, len(_ROW.findall(table.rows)), hashlib.sha256(body).hexdigest())


def parse_history(body, received_at):
    """历史响应含多个交易日：每天一个批次，按交易日升序返回。"""
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise SourceFailure('invalid_received_time')
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY:
        raise SourceFailure('response_too_large' if body else 'schema_changed')
    try:
        payload = json.loads(body.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise SourceFailure('schema_changed') from None
    if not isinstance(payload, dict) or not payload or len(payload) > 60:
        raise SourceFailure('instrument_unavailable' if isinstance(payload, dict) and not payload else 'schema_changed')
    batches = []
    batch_hash = hashlib.sha256(body).hexdigest()
    for key, value in payload.items():
        if not isinstance(value, dict) or set(value) != {'th', 'table', 't15', 't20', 't25'}:
            raise SourceFailure('schema_changed')
        table = _one_table(value)
        if table.date_raw != key:
            # 键与表头日期不一致时无法确定归属，宁可整批拒绝。
            raise SourceFailure('schema_changed')
        batches.append(_batch(table, 'history', received_at, len(_ROW.findall(table.rows)), batch_hash))
    return sorted(batches, key=lambda batch: batch.records[0][1].observed_day)
