"""Dated closing height curves from immutable, permission-checked pool archives."""
from app.evidence.common import now
from app.market.postclose import default_day, indexed, number
from app.market.review_read import _dated_rows, _metadata
from app.market.review_store import available_trade_days, load_dataset
from app.market.protocol import SHANGHAI


def height_point(day, rows):
    values = {key: None for key in ('height', 'second_height', 'gem_height')}
    leaders = {key: [] for key in values}
    stocks = indexed(rows)
    if stocks is None:
        return dict(trade_day=day.isoformat(), values=values, leaders=leaders, state='unavailable')
    up = [row for row in stocks.values() if row.get('limit') == 'U']
    heights = [(number(row.get('limit_times')), code, row.get('name') or code)
               for code, row in stocks.items() if row.get('limit') == 'U']
    if any(h is None or h <= 0 or h != h.to_integral_value() for h, _, _ in heights):
        return dict(trade_day=day.isoformat(), values=values, leaders=leaders, state='partial')
    distinct = sorted({int(h) for h, _, _ in heights}, reverse=True)
    gem = [entry for entry in heights if entry[1].startswith(('300', '301', '302'))]
    values.update(height=distinct[0] if distinct else 0,
                  second_height=distinct[1] if len(distinct) > 1 else 0,
                  gem_height=max((int(h) for h, _, _ in gem), default=0))
    for key, value in values.items():
        leaders[key] = [name for h, _, name in (gem if key == 'gem_height' else heights) if int(h) == value]
    return dict(trade_day=day.isoformat(), values=values, leaders=leaders, state='complete', sample_count=len(up))


def height_history_view(db, trade_day=None, lookback=60, as_of_time=None):
    if type(lookback) is not int or lookback not in {20, 60, 120}:
        raise ValueError('lookback must be 20, 60 or 120')
    clock = as_of_time or now(db)
    end = trade_day or default_day(db, clock) or clock.astimezone(SHANGHAI).date()
    navigation = available_trade_days(db, limit=230, as_of_time=clock)
    days = [day for day in navigation if day <= end][-lookback:]
    result = dict(version='height-history-v1', trade_day=end.isoformat(),
                  trade_days=[str(day) for day in navigation], records=[], coverage=[], evidence_refs=[],
                  state='unavailable', lookback=lookback,
                  schedule=dict(time='16:30', timezone='Asia/Shanghai', late_refresh='17:30'),
                  scope='同日供应商沪深收盘涨停池的最高、次高不同连板高度与创业板高度；未用今天的股票状态改写历史。')
    if end not in days:
        return result
    for day in days:
        table = dict(load_dataset(db, 'pools', day, as_of_time=as_of_time))
        table.update(dataset='pools', trade_day=str(day), scope='all')
        rows = _dated_rows(table, day)
        # Empty pool publication cannot establish a market with zero boards.
        if rows == []:
            rows = None
        result['records'].append(height_point(day, rows))
        result['coverage'].append(_metadata(table))
        result['evidence_refs'].extend(table.get('evidence_refs', []))
    result['state'] = 'complete' if all(row['state'] == 'complete' for row in result['records']) else 'partial'
    return result
