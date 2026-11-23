"""Read published daily archives without converting them into live ticks."""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import re

from app.market.protocol import SHANGHAI
from app.market.review_protocol import DATASET_FAMILIES
from app.market.review_fallback import pool_table
from app.market.review_store import load_dataset
from app.market.terminal_schemas import TerminalPoolRecord, TerminalRelaySummary


def _number(value, *, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or integer and result != result.to_integral_value():
            return None
        return int(result) if integer else float(result)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clock(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r'\d{5}', value):
        value = value.zfill(6)
    if re.fullmatch(r'\d{6}', value):
        value = ':'.join((value[:2], value[2:4], value[4:]))
    try:
        return time.fromisoformat(value).isoformat() if re.fullmatch(r'\d{2}:\d{2}(?::\d{2})?', value) else None
    except ValueError:
        return None


def _remember(reader, table):
    for ref in table.get('evidence_refs', []):
        reader.input_versions[ref['id']] = dict(evidence_id=ref['id'], source_id=ref['source_id'],
            content_hash=ref['content_hash'], received_at=ref['received_at'])
    if ref := table.get('manifest_ref'):
        reader.input_versions[ref['id']] = dict(evidence_id=ref['id'], source_id=table['source_id'],
            content_hash=ref['content_hash'], received_at=table['received_at'])


def _scope(table):
    value = table.get('coverage_scope') or ''
    return value if re.search(r'[\u4e00-\u9fff]', value) else '已保存的供应商日榜；封单与首末封板为日榜字段，不是本机盘中盘口。'


def closing_table(reader, day, dataset='daily'):
    """A committed, exact-date closing table with its receipt, never a tick clock."""
    key = ('terminal_closing_table', dataset, day)
    if key in reader.tables:
        return reader.tables[key]
    reader.tables[key] = None
    local = reader.cutoff.astimezone(SHANGHAI)
    if DATASET_FAMILIES[dataset] not in reader.permitted or day > local.date() or day == local.date() and local.time() < time(15):
        return None
    table = load_dataset(reader.db, dataset, day, as_of_time=reader.cutoff)
    if table.get('state') != 'complete' or not table.get('rows'):
        return None
    try:
        received = datetime.fromisoformat(table['received_at'])
    except (ValueError, TypeError, KeyError):
        return None
    if received.utcoffset() is None or received > reader.cutoff or received < datetime.combine(day, time(15), SHANGHAI):
        return None
    # A malformed or mixed-date table must not replace the whole market. The
    # store has already verified all pages, rights, hashes and cutoff receipts.
    codes = set()
    for row in table['rows']:
        code = row.get('ts_code')
        pattern = r'\d{6}\.(?:SH|SZ|CSI)' if dataset == 'indices' else r'\d{6}\.(?:SH|SZ|BJ)'
        if not isinstance(code, str) or not re.fullmatch(pattern, code) or reader.day(row.get('trade_date')) != day or code in codes:
            return None
        codes.add(code)
    _remember(reader, table)
    reader.tables[key] = table
    return table


def closing_counts(table, limits, day):
    """Daily amounts are thousand CNY; exact limits never need a fake quote time."""
    def value(raw):
        try:
            result = Decimal(str(raw))
            return result if result.is_finite() and result >= 0 else None
        except (InvalidOperation, TypeError, ValueError):
            return None
    by_code = {row['ts_code']: row for row in limits if row.get('trade_date') in {day.isoformat(), day.strftime('%Y%m%d')}}
    counts = dict(advancing=0, declining=0, unchanged=0, limit_up=0, limit_down=0, comparable=0)
    total, complete = Decimal(0), True
    for row in table['rows']:
        amount = value(row.get('amount'))
        if amount is None:
            complete = False
        else:
            total += amount * 1000
        price, previous = value(row.get('close')), value(row.get('pre_close'))
        if price is None or price <= 0 or previous is None or previous <= 0:
            continue
        counts['advancing' if price > previous else 'declining' if price < previous else 'unchanged'] += 1
        restriction = by_code.get(row['ts_code'], {})
        upper, lower = value(restriction.get('up_limit')), value(restriction.get('down_limit'))
        if upper is None or upper <= 0 or lower is None or lower <= 0:
            continue
        counts['comparable'] += 1
        counts['limit_up'] += int(price == upper)
        counts['limit_down'] += int(price == lower)
    return {**counts, 'total_amount_cny': str(total) if complete else None}


def daily_pool_table(reader, day):
    key = ('terminal_daily_pool', day)
    if key in reader.tables:
        return reader.tables[key]
    local = reader.cutoff.astimezone(SHANGHAI)
    if not {'tushare_limit_list_d', 'mairui_pools'} & reader.permitted.keys() or day is None or day > local.date() or day == local.date() and local.time() < time(15):
        reader.tables[key] = None
        return None
    table = load_dataset(reader.db, 'pools', day, as_of_time=reader.cutoff)
    if table.get('state') != 'complete' or not table.get('rows'):
        # Old source.collect tables remain usable. Their unlabelled amount
        # columns do NOT acquire a guessed unit just because a UI needs them.
        native = reader.table('tushare_limit_list_d', day)
        if native:
            rows = []
            for row, columns, _ in native:
                value = _number(row.get('fd_amount'))
                multiplier = {'元': 1, '万元': 10000, '亿元': 100000000}.get(columns.get('fd_amount'))
                rows.append({**row, 'fd_amount': value * multiplier if value is not None and multiplier else None})
            table = dict(state='complete', rows=rows, source_family='tushare_limit_list_d',
                seal_amount_unit_verified=True,
                received_at=max(meta.received_at for _, _, meta in native).isoformat(),
                coverage_scope='Tushare 已保存日榜；封单与首末封板为供应商日榜字段，不是本机盘中盘口。')
        else:
            table = pool_table(reader.db, day, as_of_time=reader.cutoff)
    if not table or table.get('state') != 'complete' or not table.get('rows'):
        reader.tables[key] = None
        return None
    table = {**table, 'source_family': table.get('source_family', 'tushare_limit_list_d')}
    if table['source_family']=='tushare_limit_list_d' and table.get('seal_amount_unit_verified') is not True:
        table={**table,'rows':[{**row,'fd_amount':None} for row in table['rows']]}
    _remember(reader, table)
    reader.tables[key] = table
    return table


def _pool_rows(reader, day, table):
    rows, duplicates = {}, set()
    for index, row in enumerate(table['rows']):
        code = row.get('ts_code')
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', code):
            continue
        if reader.day(row.get('trade_date')) != day or row.get('limit') not in {'U', 'D', 'Z'}:
            continue
        if code in rows:
            duplicates.add(code)
        rows[code] = (index, row)
    # Conflicting pool states cannot become a guessed current status.
    return {code: row for code, row in rows.items() if code not in duplicates}


def pool_rows(reader, day):
    table = daily_pool_table(reader, day)
    return (table, _pool_rows(reader, day, table)) if table else (None, {})


def attach_daily_pools(reader, day, stocks):
    table, rows = pool_rows(reader, day)
    if not table:
        return
    extra=None;extra_rows={}
    if table['source_family']!='mairui_pools' and 'mairui_pools' in reader.permitted and any(
            code in stocks and _number(row.get('fd_amount')) is None for code,(_,row) in rows.items()):
        key=('terminal_daily_pool_supplement',day)
        if key not in reader.tables:
            reader.tables[key]=pool_table(reader.db,day,as_of_time=reader.cutoff)
        extra=reader.tables[key]
        if extra and extra.get('state')=='complete':extra_rows=_pool_rows(reader,day,extra)
    for code, (index, row) in rows.items():
        if code not in stocks:
            continue
        chosen=table
        if _number(row.get('fd_amount')) is None and code in extra_rows:
            position,other=extra_rows[code]
            height,other_height=_number(row.get('limit_times'),integer=True),_number(other.get('limit_times'),integer=True)
            if (other['limit']==row['limit'] and _number(other.get('fd_amount')) is not None and
                    (height is None or other_height is None or height==other_height)):
                # Keep a whole independent supplier row and its provenance;
                # never relabel a field from a different source as primary.
                chosen,index,row=extra,position,other
                _remember(reader,chosen)
        provider = reader.providers.get(chosen['source_family'])
        label = provider.name if provider else '麦蕊日榜' if chosen['source_family'] == 'mairui_pools' else 'Tushare 日榜'
        receipt = next((ref.get('received_at') for ref in chosen.get('evidence_refs', [])
            if ref.get('start_row', 0) <= index < ref.get('start_row', 0) + ref.get('row_count', 0)), chosen.get('received_at'))
        stocks[code].daily_pool = TerminalPoolRecord(trade_day=day, state=row['limit'], source_name=label,
            received_at=receipt, scope=_scope(chosen), seal_amount_cny=_number(row.get('fd_amount')),
            first_limit_time=_clock(row.get('first_time')), last_limit_time=_clock(row.get('last_time')),
            board_height=_number(row.get('limit_times'), integer=True), open_times=_number(row.get('open_times'), integer=True))


def relay_summary(reader, day, stocks, heights, main_board):
    local = reader.cutoff.astimezone(SHANGHAI)
    days = [value for value in reader.calendar_days if value <= day
        and (value < local.date() or local.time() >= time(15))][-10:]
    known = []
    for value in days:
        rows = heights(reader, value)
        if rows:
            # A supplier list can have a smaller market scope. Its original
            # scope stays explicit; ten available dates do not imply all stocks.
            known.append((value, max(rows.values())))
    table, today = pool_rows(reader, day)
    first = [code for code, (_, row) in today.items() if row['limit'] == 'U'
        and _number(row.get('limit_times'), integer=True) == 1 and main_board(code, row.get('name') or '')]
    high = [item for item in stocks.values() if (item.previous_height or 0) >= 2 and main_board(item.code, item.name)]
    sealed = [item for item in high if item.seal_amount_cny is not None and item.seal_amount_cny > 0]
    return TerminalRelaySummary(history_high=max((height for _, height in known), default=None)
        if len(days) == 10 and len(known) == 10 else None, history_covered_days=len(known),
        history_start=days[0] if days else None, history_end=days[-1] if days else None,
        history_scope='近10个已收盘交易日的已存供应商连板日榜；缺一天不输出十日最高，不保证覆盖全市场。',
        high_sealed_count=len(sealed) if sealed else None, high_seal_total=len(high),
        first_board_codes=first, daily_ladder_available=bool(today),
        daily_ladder_source=_scope(table) if table else None)


def archived_members(reader, day, selected):
    key = ('terminal_archived_members', day, selected)
    if key in reader.tables:
        return reader.tables[key]
    result = []
    if selected and 'tushare_dc_member' in reader.permitted:
        for scope in (selected, 'all'):
            dataset_key = ('terminal_members_dataset', day, scope)
            if dataset_key not in reader.tables:
                reader.tables[dataset_key] = load_dataset(reader.db, 'members', day, scope=scope, as_of_time=reader.cutoff)
            table = reader.tables[dataset_key]
            if table.get('state') != 'complete':
                continue
            rows = [row for row in table.get('rows', []) if row.get('ts_code') == selected
                and reader.day(row.get('trade_date')) == day]
            if rows:
                _remember(reader, table)
                result = [(row, {}, None) for row in rows]
                break
    reader.tables[key] = result
    return result
