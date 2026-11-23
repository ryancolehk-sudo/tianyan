"""Pure, dated membership unions and chart projections for 百日新高."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.evidence.common import now
from app.market.new_high_protocol import HISTORY_SESSIONS
from app.market.postclose import default_day, number
from app.market.protocol import SHANGHAI
from app.market.review_read import _dated_rows, _metadata
from app.market.review_store import available_trade_days, load_dataset

VALUE_FIELDS = ('price', 'pct_chg', 'amount', 'main_net', 'actual_float', 'actual_turnover')


def complete_rows(table, day):
    rows = _dated_rows(table, day)
    if rows is None:
        return None
    summary = [r for r in rows if r.get('kind') == 'summary']
    stocks = [r for r in rows if r.get('kind') == 'stock']
    if len(summary) != 1 or len({r.get('ts_code') for r in stocks}) != len(stocks):
        return None
    try:
        if (len(stocks) != int(summary[0]['total']) or
                sum(int(r['is_new']) for r in stocks) != int(summary[0]['new_count'])):
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return stocks


def aggregate_groups(rows):
    counts = defaultdict(lambda: dict(count=0, new_count=0))
    for row in rows:
        entry = counts[row['group_name']]
        entry['count'] += 1
        entry['new_count'] += int(row['is_new'])
    groups, other = [], dict(name='其他', count=0, new_count=0, members=[])
    for name, value in counts.items():
        if value['count'] < 3 or name == '其他':
            other['count'] += value['count']
            other['new_count'] += value['new_count']
            other['members'].append(name)
        else:
            groups.append(dict(name=name, **value, members=[name]))
    groups.sort(key=lambda g: (-g['count'], g['name']))
    if other['count']:
        groups.append(other)
    return groups


def union_rows(tables, days):
    selected, added = {}, set()
    missing = []
    for day in days:
        rows = complete_rows(tables[day], day)
        if rows is None:
            missing.append(str(day))
            continue
        for row in rows:
            code = row['ts_code']
            selected[code] = {**row, 'membership_day': str(day), 'quote_day': str(day)}
            if row['is_new'] == '1':
                added.add(code)
    for code, row in selected.items():
        row['is_new'] = code in added
    return list(selected.values()), missing


def apply_end_quotes(rows, end, supplements):
    """Earlier membership never masquerades as the selected day's quotation.

    Native end-day fields remain exact. Earlier members use independently saved,
    same-day purchased daily tables where available, with per-field provenance.
    Original native rows remain unchanged in their immutable evidence parts.
    """
    maps = {}
    for dataset, table in supplements.items():
        items = _dated_rows(table, end)
        grouped = defaultdict(list)
        for item in items or []:
            grouped[item.get('ts_code')].append(item)
        maps[dataset] = {key: values[0] for key, values in grouped.items() if len(values) == 1}
    for row in rows:
        row['field_sources'] = {field: dict(dataset='new_highs', trade_day=row['membership_day']) for field in VALUE_FIELDS}
        if row['membership_day'] == str(end):
            continue
        row['native_values'] = {field: row.get(field) for field in VALUE_FIELDS}
        row.update({field: None for field in VALUE_FIELDS})
        row['quote_day'] = None
        row['field_sources'] = {}
        code = row['ts_code']
        daily = maps.get('daily', {}).get(code, {})
        basic = maps.get('group_basics', {}).get(code, {})
        funds = maps.get('stock_funds', {}).get(code, {})
        def put(field, value, dataset, basis):
            if value is not None:
                row[field] = str(value)
                row['field_sources'][field] = dict(dataset=dataset, trade_day=str(end), basis=basis)
        close, shares = number(daily.get('close'), nonnegative=True), number(basic.get('free_share'), nonnegative=True)
        put('price', close, 'daily', 'close')
        put('pct_chg', number(daily.get('pct_chg')), 'daily', 'pct_chg')
        amount = number(daily.get('amount'), nonnegative=True)
        put('amount', amount * 1000 if amount is not None else None, 'daily', 'amount千元×1000')
        net = number(funds.get('net_amount'))
        put('main_net', net * 10000 if net is not None else None, 'stock_funds', 'net_amount万元×10000')
        if close is not None and close > 0 and shares is not None and shares > 0 and number(basic.get('close')) == close:
            put('actual_float', shares * 10000 * close, 'group_basics', 'free_share万股×10000×同日close')
            volume = number(daily.get('vol'), nonnegative=True)
            put('actual_turnover', volume / shares if volume is not None else None, 'group_basics', '同日vol手/free_share万股（%）')
        if row['price'] is not None:
            row['quote_day'] = str(end)


def new_highs_view(db, trade_day=None, start_day=None, as_of_time=None):
    clock = as_of_time or now(db)
    end = trade_day or default_day(db, clock) or clock.astimezone(SHANGHAI).date()
    navigation = available_trade_days(db, limit=230, as_of_time=clock)
    days = [d for d in navigation if d <= end][-HISTORY_SESSIONS:]
    first = start_day or end
    result = dict(version='new-highs-v1', state='unavailable', trade_day=str(end), start_day=str(first),
                  trade_days=[str(d) for d in navigation], rows=[], groups=[], records=[], total=None, new_count=None,
                  coverage=[], evidence_refs=[], missing_days=[],
                  schedule=dict(time='16:30', timezone='Asia/Shanghai', late_refresh='17:30'),
                  scope='开盘啦原生百日新高名单、原生主板块及新增标记；多日按代码去重，期间新增按原生新增标记去重。')
    if end not in days or first not in days or first > end:
        return result
    tables = {}
    for day in days:
        table = dict(load_dataset(db, 'new_highs', day, as_of_time=as_of_time))
        table.update(dataset='new_highs', trade_day=str(day), scope='all')
        tables[day] = table
        history = load_dataset(db, 'new_high_history', day, as_of_time=as_of_time)
        summaries = _dated_rows(history, day) or []
        native = complete_rows(table, day)
        summary = summaries[0] if len(summaries) == 1 else None
        if summary is None and native is not None:
            summary = next(r for r in table['rows'] if r.get('kind') == 'summary')
        result['records'].append(dict(trade_day=str(day),
            total=int(summary['total']) if summary is not None else None,
            new_count=int(summary['new_count']) if summary is not None else None,
            groups=aggregate_groups(native) if native is not None else None,
            group_counts={name:dict(count=sum(r['group_name']==name for r in native),
                new_count=sum(r['group_name']==name and r['is_new']=='1' for r in native))
                for name in {r['group_name'] for r in native}} if native is not None else None))
        for value in (table, history):
            result['evidence_refs'].extend(value.get('evidence_refs', []))
    selected_days = [d for d in days if first <= d <= end]
    rows, missing = union_rows(tables, selected_days)
    supplements = {}
    if any(r['membership_day'] != str(end) for r in rows):
        supplements = {dataset:load_dataset(db, dataset, end, as_of_time=as_of_time)
                       for dataset in ('daily','group_basics','stock_funds')}
        for table in supplements.values():
            result['evidence_refs'].extend(table.get('evidence_refs', []))
    apply_end_quotes(rows, end, supplements)
    for day in selected_days:
        result['coverage'].append(_metadata(tables[day]))
    result.update(rows=rows, groups=aggregate_groups(rows), missing_days=missing,
                  total=len(rows) if not missing else None,
                  new_count=sum(r['is_new'] for r in rows) if not missing else None,
                  loaded_count=len(rows), state='complete' if not missing else 'partial' if rows else 'unavailable')
    # One immutable part can back many history days; avoid duplicate references.
    result['evidence_refs'] = list({r['id']:r for r in result['evidence_refs']}.values())
    return result
