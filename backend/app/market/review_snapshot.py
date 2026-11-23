"""Today's saved observations while the complete daily archive is pending.

This reader never fetches. It adapts the already validated market table to the
existing review arithmetic, retaining raw row references and an explicit
intraday basis. A current price is never represented as a confirmed close.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
import re

from sqlalchemy import select

from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_store import permitted_provider
from app.market.terminal_read import at, pct, quoted_price
from app.models import Instrument


SCOPE = ('当日已保存的沪深盘中报价样本；最新价用于当前反馈，不代表已核实收盘。'
         '停采后的时段不补点；完整日频归档到达后自动替换。')


def eligible(day, as_of_time=None, clock=None):
    return as_of_time is None and day == (clock or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()


def _query(db, family, kind, cutoff):
    provider = permitted_provider(db, family)
    if provider is None:
        return None
    return select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == kind,
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
    )


def _reference(evidence, selected_rows):
    return dict(id=str(evidence.id), content_hash=evidence.content_hash,
                source_id=str(evidence.source_id), received_at=evidence.received_at.isoformat(),
                start_row=0, row_count=len(selected_rows), selected_rows=selected_rows)


def _table(evidence, day, rows, selected_rows, source_time, *, basis='intraday_snapshot'):
    return dict(state='complete', rows=rows, row_count=len(rows), source_id=str(evidence.source_id),
                received_at=evidence.received_at.isoformat(), requested_day=day.isoformat(),
                evidence_refs=[_reference(evidence, selected_rows)], basis=basis,
                source_time=source_time.isoformat() if source_time else None,
                coverage_scope=SCOPE if basis == 'intraday_snapshot' else '当日实际价格限制表',
                reason='daily_archive_pending' if basis == 'intraday_snapshot' else None)


def snapshot_table(db, dataset, day, scope='all', *, as_of_time=None, clock=None):
    cutoff = clock or datetime.now(SHANGHAI)
    if not eligible(day, as_of_time, cutoff) or dataset not in {'daily', 'limits', 'indices'}:
        return None
    if dataset == 'indices':
        return _index_table(db, day, scope, cutoff)
    family, name = ('tushare_market_rt', 'tushare.rt_market') if dataset == 'daily' else (
        'tushare_market_limits', 'tushare.market_limits')
    stmt = _query(db, family, 'reference', cutoff)
    if stmt is None:
        return None
    saved = db.scalar(stmt.where(
        EvidenceVersion.payload['dataset'].astext == name,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat(),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if saved is None:
        return None
    rows, selected, stamps, seen = [], [], [], set()
    for position, raw in enumerate(saved.payload.get('rows', [])):
        if not isinstance(raw, dict):
            return None
        code = raw.get('ts_code')
        if not isinstance(code, str) or not code.endswith(('.SH', '.SZ')):
            continue
        if not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', code):
            return None
        if code in seen:
            return None
        seen.add(code)
        if dataset == 'limits':
            if raw.get('trade_date') != day.strftime('%Y%m%d'):
                return None
            rows.append(dict(raw))
        else:
            if not isinstance(raw.get('trade_time'), str):
                continue
            stamp = at(raw, saved.received_at)
            if stamp is None or stamp > cutoff or stamp.astimezone(SHANGHAI).date() != day:
                continue
            # rt_market.amount is yuan; daily.amount is thousand yuan. Preserve
            # absent values, and use the common price normalization/percentage.
            amount = _nonnegative(raw.get('amount'))
            volume = _nonnegative(raw.get('vol'))
            row = {key: quoted_price(raw.get(key)) for key in ('open', 'high', 'low', 'close', 'pre_close')}
            row.update(ts_code=code, name=raw.get('name'), trade_date=day.strftime('%Y%m%d'),
                       pct_chg=pct(row['close'], row['pre_close']),
                       amount=str(amount / 1000) if amount is not None else None,
                       vol=str(volume / 100) if volume is not None else None,
                       source_time=stamp.isoformat(), price_basis='intraday_snapshot')
            rows.append(row)
            stamps.append(stamp)
        selected.append(position)
    if not rows:
        return None
    return _table(saved, day, rows, selected, max(stamps) if stamps else None,
                  basis='intraday_snapshot' if dataset == 'daily' else 'daily_limits')


def _nonnegative(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _index_table(db, day, code, cutoff):
    stmt = _query(db, 'tushare_rt_indices', 'quote', cutoff)
    if stmt is None or not isinstance(code, str) or not code.endswith(('.SH', '.SZ')):
        return None
    exchange = 'SSE' if code.endswith('.SH') else 'SZSE'
    start = datetime.combine(day, time.min, SHANGHAI)
    saved = db.scalar(stmt.join(Instrument, Instrument.id == EvidenceVersion.instrument_id).where(
        Instrument.asset_type == 'index', Instrument.symbol == code[:-3], Instrument.exchange == exchange,
        EvidenceVersion.event_at >= start, EvidenceVersion.event_at < start + timedelta(days=1),
        EvidenceVersion.event_at <= cutoff,
    ).order_by(EvidenceVersion.event_at.desc(), EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if saved is None:
        return None
    raw = saved.payload
    try:
        stamp = datetime.fromisoformat(raw['quote_time'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (stamp.utcoffset() is None or stamp > cutoff or stamp > saved.received_at + timedelta(seconds=60)
            or stamp.astimezone(SHANGHAI).date() != day):
        return None
    row = dict(ts_code=code, trade_date=day.strftime('%Y%m%d'), close=raw.get('price'),
               pre_close=raw.get('previous_close'), open=raw.get('open'), high=raw.get('high'), low=raw.get('low'),
               pct_chg=pct(raw.get('price'), raw.get('previous_close')), source_time=stamp.isoformat(),
               price_basis='intraday_snapshot')
    result = _table(saved, day, [row], [0], stamp)
    # Quote evidence is an object, not a reference-table row.
    result['evidence_refs'][0].pop('selected_rows')
    result['evidence_refs'][0]['object_payload'] = True
    return result


def observation(table):
    if table.get('basis') != 'intraday_snapshot':
        return None
    return dict(basis='intraday_snapshot', source_time=table.get('source_time'),
                received_at=table.get('received_at'), scope=SCOPE, sample_count=table.get('row_count'))


def mark_metrics(metrics, table):
    observed = observation(table)
    if observed is None or 'sample' not in metrics:
        return
    metrics.update(basis=observed['basis'], source_time=observed['source_time'],
                   source_received_at=observed['received_at'], scope=observed['scope'], state='partial')
    metrics.setdefault('sample', {}).update(daily_loaded=False, snapshot_loaded=True)
    metrics.setdefault('universe', {})['scope'] = SCOPE
    intraday = metrics.get('limits', {}).get('intraday')
    if intraday is not None:
        intraday.update(basis=observed['basis'], source_time=observed['source_time'],
                        scope='同日已保存的沪深盘中行情与个股真实限制价；曾涨停/曾跌停仅指截至采样时点触板且已打开，不含采样时点封板')
    # A partial-day accumulated amount cannot be a day-over-day closing change.
    if metrics.get('turnover'):
        metrics['turnover']['change_pct'] = None
    metrics.setdefault('quality_flags', []).append({'code': 'intraday_snapshot_not_close'})
