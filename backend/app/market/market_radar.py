"""Authenticated read models for the market status card and its historical radar."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from app.auth import APIError, principal
from app.db import db_session
from app.http import envelope
from app.market.protocol import SHANGHAI
from app.market.review_metrics import _intraday_limits, _number, _Quality, _rows
from app.market.review_read import _dated_rows, _Reader
from app.market.review_snapshot import snapshot_table
from app.market.review_store import available_trade_days, permitted_provider
from app.schemas import Envelope, Error

router = APIRouter()
ERRORS = {code: {'model': Error} for code in (401, 422, 429)}


def ratio(count, total):
    return 100 * count / total if count is not None and total else None


def indexed(table, day, *, pool=False):
    rows = _dated_rows(table, day)
    try:
        data, _, _ = _rows(rows, 'pools' if pool else 'daily', _Quality(), day.isoformat(), pool=pool)
        return data
    except ValueError:
        return None


def read_pool(reader, day):
    if day is None:
        return None, None
    table = reader.get('pools', day)
    data = indexed(table, day, pool=True)
    if data is None:
        return None, None
    source = '麦蕊收盘日榜' if table.get('source_family') == 'mairui_pools' else '涨停池日榜'
    return data, source


def summarize(day, minute, rows, prev_pool, sealed, touched, pool_source):
    flags = _Quality()
    changes = []
    amount = Decimal(0) if rows else None
    for row in (rows or {}).values():
        pct = _number(row, 'pct_chg', 'daily', flags)
        if pct is not None:
            changes.append(pct)
        if amount is not None:
            part = _number(row, 'amount', 'daily', flags, nonnegative=True)
            amount = None if part is None else amount + part * 1000
    n = len(changes)
    advancing = sum(v > 0 for v in changes)
    falling = sum(v <= -5 for v in changes)
    feedback = None
    cohort_size = None
    if prev_pool is not None:
        cohort = [code for code, r in prev_pool.items() if r['limit'] == 'U']
        cohort_size = len(cohort)
        gains = []
        for code in cohort:
            row = (rows or {}).get(code)
            if row is None:
                break
            last = _number(row, 'close', 'daily', flags, positive=True)
            prior = _number(row, 'pre_close', 'daily', flags, positive=True)
            if last is None or prior is None:
                break
            gains.append((last / prior - 1) * 100)
        if gains and len(gains) == cohort_size:
            feedback = float(sum(gains) / cohort_size)
    return dict(day=day.isoformat(), minute=minute, n=n if rows is not None else None,
                median_pct=float(median(changes)) if n else None,
                advancing=advancing if n else None, falling=falling if n else None,
                sealed=sealed, touched=touched, pool_source=pool_source, cohort_n=cohort_size,
                raw=[ratio(advancing, n), float(amount) if amount is not None else None,
                     ratio(sealed, touched), feedback, ratio(falling, n)])


def close_point(reader, day, pool, prior_pool, source):
    table = reader.get('daily', day)
    rows = None if table.get('basis') == 'intraday_snapshot' else indexed(table, day)
    sealed = touched = None
    if pool is not None:
        sealed = sum(row['limit'] == 'U' for row in pool.values())
        touched = sealed + sum(row['limit'] == 'Z' for row in pool.values())
    return summarize(day, None, rows, prior_pool, sealed, touched, source)


def snapshot(db, cutoff, dataset):
    family = 'tushare_market_rt' if dataset == 'daily' else 'tushare_market_limits'
    if not permitted_provider(db, family, cutoff):
        return None
    return snapshot_table(db, dataset, cutoff.date(), clock=cutoff)


def read_point(db, cutoff, previous_day, match_minute=None):
    daily = snapshot(db, cutoff, 'daily')
    if not daily or not daily.get('source_time'):
        return None
    observed = datetime.fromisoformat(daily['source_time']).astimezone(SHANGHAI)
    minute = observed.strftime('%H:%M')
    if observed.date() != cutoff.date() or (match_minute and minute != match_minute):
        return None
    rows = {row['ts_code']: row for row in daily['rows']}
    sealed = touched = None
    if rows and all(row.get('name') for row in rows.values()):
        eligible = {code: row for code, row in rows.items() if 'ST' not in row['name'].upper()}
        caps = snapshot(db, cutoff, 'limits')
        if eligible and caps:
            limits = _intraday_limits(eligible, {r['ts_code']: r for r in caps['rows']})
            sealed, touched = limits['close_up'], limits['touched_up']
    prior_pool, _ = read_pool(_Reader(db, cutoff), previous_day)
    return summarize(cutoff.date(), minute, rows, prior_pool, sealed, touched, '沪深非 ST 触板快照')


def minute_view(db, sample_at, window=20):
    cutoff = sample_at.astimezone(SHANGHAI)
    days = sorted(set(available_trade_days(db, limit=window+1, end_day=cutoff.date()-timedelta(days=1), as_of_time=cutoff)))
    current = read_point(db, cutoff, days[-1] if days else None)
    history = []
    if current:
        hour, minute = map(int, current['minute'].split(':'))
        for i in range(max(0, len(days)-window), len(days)):
            end = datetime.combine(days[i], time(hour, minute, 59, 999999), SHANGHAI)
            history.append(read_point(db, end, days[i-1] if i else None, current['minute']))
    return dict(day=cutoff.date().isoformat(), basis='minute', current=current, history=history,
                previous_day=days[-1].isoformat() if days else None)


def close_view(db, trade_day, as_of, window):
    days = sorted(set(available_trade_days(db, limit=window+2, end_day=trade_day, as_of_time=as_of)))
    result = dict(day=trade_day.isoformat(), basis='close', current=None, history=[], previous_day=None)
    if not days or days[-1] != trade_day:
        return result
    first = max(0, len(days)-window-1)
    prior_pool, _ = read_pool(_Reader(db, as_of), days[first-1] if first else None)
    for day in days[first:]:
        reader = _Reader(db, as_of)
        pool, source = read_pool(reader, day)
        point = close_point(reader, day, pool, prior_pool, source)
        if day == trade_day:
            result['current'] = point
        else:
            result['history'].append(point)
            result['previous_day'] = day.isoformat()
        prior_pool = pool
    return result


@router.get('/api/v1/market-status', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getMarketStatus')
def market_status(request: Request, trade_day: date, window: Literal['1','20']='1',
                  sample_at: AwareDatetime | None=None, as_of_time: AwareDatetime | None=None,
                  identity=Depends(principal), db: Session=Depends(db_session)):
    from app.market.review_api import check_cutoff
    check_cutoff(db, as_of_time)
    if as_of_time and trade_day > as_of_time.astimezone(SHANGHAI).date():
        raise APIError(422, 'invalid_trade_day', '交易日不能晚于复盘截止时刻。')
    if sample_at is not None:
        check_cutoff(db, sample_at)
        if sample_at.astimezone(SHANGHAI).date() != trade_day or (as_of_time and sample_at > as_of_time):
            raise APIError(422, 'invalid_sample', '行情采样与所选日期或截止时刻不符。')
        result = minute_view(db, sample_at, int(window))
    else:
        result = close_view(db, trade_day, as_of_time, int(window))
    points = [result['current'], *result['history']]
    complete = len(result['history']) == int(window) and all(p and None not in p['raw'] for p in points)
    return envelope(request, result, 'complete' if complete else 'partial')


@router.get('/api/v1/market-radar/minute', response_model=Envelope[dict], responses=ERRORS,
            operation_id='getMarketRadarMinute')
def market_radar(request: Request, sample_at: AwareDatetime, identity=Depends(principal), db: Session=Depends(db_session)):
    from app.market.review_api import check_cutoff
    check_cutoff(db, sample_at)
    result = minute_view(db, sample_at)
    complete = len(result['history']) == 20 and all(p and None not in p['raw'] for p in [result['current'], *result['history']])
    return envelope(request, result, 'complete' if complete else 'partial')
