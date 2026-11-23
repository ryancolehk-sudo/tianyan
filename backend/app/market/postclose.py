"""After-close history and rankings over authorized, immutable archives.

No HTTP, writes or current-quote overlays. Windows are verified exchange
sessions, not calendar-day offsets. One day's large raw tables are discarded
per iteration; only bounded ranking accumulators and chart points survive.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from sqlalchemy import select

from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.evidence.reference_summary import reference_summary
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.review_metrics import build_daily_metrics
from app.market.review_read import _dated_rows, _metadata
from app.market.review_store import available_trade_days, load_dataset, permitted_provider
from app.models import Instrument, Provider

VERSION = 'postclose-v1'
METRICS = (
    ('emotion', '情绪热度', '点'), ('height', '连板高度', '板'),
    ('second_height', '次高高度', '板'), ('gem_height', '创业板高度', '板'),
    ('up_amount', '涨停成交额', '元'), ('broken_amount', '炸板成交额', '元'),
    ('market_amount', '市场成交额', '元'), ('advancing', '上涨家数', '只'),
    ('declining', '下跌家数', '只'), ('consecutive', '连板数量', '只'),
    ('limit_up', '涨停数量', '只'), ('limit_down', '跌停数量', '只'),
    ('first_promotion', '首板晋级率', '%'), ('relay_promotion', '连板晋级率', '%'),
    ('seal_rate', '封板率', '%'), ('auction_up', '竞价一字涨停', '只'),
    ('auction_seal', '竞价涨停封单', '元'), ('auction_down', '竞价一字跌停', '只'),
    ('main_net', '主力净流入', '元'), ('yesterday_up', '昨日涨停平均涨幅', '%'),
    ('yesterday_relay', '昨日连板平均涨幅', '%'), ('broken', '炸板家数', '只'),
    ('broken_rate', '炸板率', '%'),
)


def number(value, *, nonnegative=False):
    try:
        if value is None or isinstance(value, bool):
            return None
        result = Decimal(str(value))
        return result if result.is_finite() and (not nonnegative or result >= 0) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def text(value):
    return str(value) if value is not None else None


def indexed(rows):
    if rows is None:
        return None
    found, duplicate = {}, set()
    for row in rows:
        code = row.get('ts_code', '')
        if not code.endswith(('.SH', '.SZ')):
            continue
        if code in found:
            duplicate.add(code)
        found[code] = row
    # A corrupted table must not silently redefine a ranking universe.
    return None if duplicate else found


def pool_amount(pool, quotes, kind):
    if pool is None or quotes is None:
        return None
    amounts = [number((quotes.get(code) or {}).get('amount'), nonnegative=True)
               for code, row in pool.items() if row.get('limit') == kind]
    return text(sum(amounts, Decimal(0)) * 1000) if all(v is not None for v in amounts) else None


def clock_text(value):
    if not isinstance(value, str):
        return None
    try:
        normalized = value if ':' in value else f'{value[:2]}:{value[2:4]}:{value[4:]}'
        return time.fromisoformat(normalized).isoformat() if len(normalized) == 8 else None
    except ValueError:
        return None


class References:
    """Small permission/as-of reader for existing membership and auction code."""
    def __init__(self, db, cutoff, as_of_time):
        self.db, self.cutoff, self.as_of_time = db, cutoff, as_of_time
        self.refs = {}
        self.coverage = {}
        self.permitted = {}

    def query(self, families, kind):
        ids = []
        for family in families:
            provider = permitted_provider(self.db, family, self.as_of_time)
            if provider:
                self.permitted[family] = provider
                ids.append(provider.id)
        return select(EvidenceVersion).join(Provider, Provider.id == EvidenceVersion.source_id).where(
            EvidenceVersion.source_id.in_(ids), EvidenceVersion.kind == kind,
            EvidenceVersion.owner_id.is_(None), EvidenceVersion.visibility == 'shared',
            EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= self.cutoff)

    def record_input(self, evidence):
        key = str(evidence.id)
        ref = {'id': key, 'content_hash': evidence.content_hash,
               'received_at': evidence.received_at.isoformat()}
        self.refs[key] = ref
        summary = getattr(evidence, 'reference_summary', None)
        if summary is None:
            summary = reference_summary(evidence.payload)
        self.coverage[key] = dict(dataset=summary['dataset'], trade_day=summary['trade_day'],
            scope=summary['scope'] or '来源原始归档；分类采用所选日以前已接收的完整采集批次。',
            state='complete', row_count=summary['row_count'], source_id=str(evidence.source_id),
            received_at=evidence.received_at.isoformat(), evidence_refs=[ref])


def auction_metrics(reader, day):
    from app.market.auction_source import SUMMARY_DATASET
    from app.market.auction_book import DATASET, FAMILY, UNITS, seal
    values = dict(auction_up=None, auction_down=None, auction_seal=None)
    base = reader.query({'duanxianxia_auction', 'duanxianxia_auction_history'}, 'reference')
    rows = reader.db.scalars(base.where(EvidenceVersion.payload['dataset'].astext == SUMMARY_DATASET,
        EvidenceVersion.payload['rows'][0]['trade_day'].astext == day.isoformat()).order_by(
            EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(10))
    for evidence in rows:
        header = (evidence.payload.get('rows') or [{}])[0]
        amount = number(header.get('total_fd_0925_cny'), nonnegative=True)
        if amount is None and header.get('table_kind') == 'history':
            amount = number(header.get('header_total_cny'), nonnegative=True)
        count = number(header.get('one_word_count'), nonnegative=True)
        if amount is not None and count is not None and count == count.to_integral_value():
            values.update(auction_up=int(count), auction_seal=text(amount))
            reader.record_input(evidence)
            break
    # A closing down pool is NOT an auction down count. Use only a completed
    # native 09:25 full-market book capture; unknown/partial captures stay null.
    base = reader.query({FAMILY}, 'reference')
    records = reader.db.scalars(base.where(EvidenceVersion.payload['dataset'].astext == DATASET,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat()).order_by(
            EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(8))
    for evidence in records:
        rows = evidence.payload.get('rows', [])
        units = {c['key']: c.get('unit') for c in evidence.payload.get('columns', [])}
        if any(units.get(k) != v for k, v in UNITS.items()):
            continue
        header = next((r for r in rows if r.get('row_type') == 'coverage'), {})
        usable, expected = number(header.get('usable_count')), number(header.get('universe_count'))
        try:
            raw_stamp = datetime.fromisoformat(header.get('trade_time', ''))
            if raw_stamp.utcoffset() is None:
                continue
            stamp = raw_stamp.astimezone(SHANGHAI)
        except ValueError:
            continue
        if (expected is None or not 0 < expected <= 8000 or expected != expected.to_integral_value()
                or expected != usable or stamp.date() != day or stamp.time() != time(9, 25)
                or stamp > reader.cutoff):
            continue
        candidates = [r for r in rows if r.get('row_type') == 'stock']
        if any(r.get('trade_time') != header.get('trade_time') for r in candidates):
            continue
        try:
            values['auction_down'] = sum((seal(r, 'down') or 0) > 0 for r in candidates)
        except (SourceFailure, ValueError, TypeError):
            continue
        reader.record_input(evidence)
        break
    return values


def daily_point(day, current, previous, prior_pools, tables, emotion, auction):
    try:
        metrics = build_daily_metrics(trade_day=day.isoformat(), daily=current['daily'],
            price_limits=current['limits'], pools=current['pools'],
            previous_daily=previous.get('daily'), previous_price_limits=previous.get('limits'),
            previous_pools=previous.get('pools'), prior_pools=prior_pools)
    except ValueError:
        return {'trade_day': day.isoformat(), 'values': {key: None for key, _, _ in METRICS},
                'state': 'unavailable', 'reason': 'invalid_archive_table', 'leaders': {}}
    supplier, relay = metrics['limits']['supplier_reported'], metrics['relay']
    pool, quotes = indexed(current['pools']), indexed(current['daily'])
    heights = [(int(height), code, row.get('name') or code)
               for code, row in (pool or {}).items() if row.get('limit') == 'U'
               and (height := number(row.get('limit_times'))) is not None and height > 0
               and height == height.to_integral_value()]
    gem = [v for v in heights if v[1].startswith(('300', '301', '302'))]
    broken_rate = number(supplier.get('broken_rate_pct'))
    values = dict(emotion=emotion, height=supplier.get('max_height'),
        second_height=supplier.get('second_height'), gem_height=max((h for h, _, _ in gem), default=0)
        if pool is not None and not supplier.get('height_missing_n') else None,
        up_amount=pool_amount(pool, quotes, 'U'), broken_amount=pool_amount(pool, quotes, 'Z'),
        market_amount=metrics['turnover']['amount_yuan'],
        advancing=metrics['breadth']['up'], declining=metrics['breadth']['down'],
        consecutive=sum(h >= 2 for h, _, _ in heights) if pool is not None and not supplier.get('height_missing_n') else None,
        limit_up=supplier['up'], limit_down=supplier['down'], broken=supplier['broken'],
        broken_rate=supplier['broken_rate_pct'], seal_rate=text(100 - broken_rate) if broken_rate is not None else None,
        first_promotion=relay['first_board']['promotion_pct'], relay_promotion=relay['consecutive']['promotion_pct'],
        yesterday_up=relay['yesterday_limit_up']['close_mean_pct'],
        yesterday_relay=relay['consecutive']['close_mean_pct'], main_net=None, **auction)
    leaders = {key: [name for height, _, name in (gem if key == 'gem_height' else heights) if height == values[key]]
               for key in ('height', 'second_height', 'gem_height')}
    return dict(trade_day=day.isoformat(), values=values, leaders=leaders,
        state='complete' if all(v is not None for v in values.values()) else 'partial',
        coverage=[_metadata(table) for table in tables.values()],
        universe_count=metrics['sample']['N'])


def stock_rows(day, daily, basics, pools, funds, names):
    quotes, basic, pool, flows = map(indexed, (daily, basics, pools, funds))
    result = []
    for code, row in (quotes or {}).items():
        b, p, f = [(table or {}).get(code, {}) for table in (basic, pool, flows)]
        close, basic_close, shares = number(row.get('close')), number(b.get('close')), number(b.get('free_share'))
        actual_float = shares * 10000 * close if close and close == basic_close and shares and shares > 0 else None
        amount, net = number(row.get('amount'), nonnegative=True), number(f.get('net_amount'))
        turnover = number(b.get('turnover_rate_f'), nonnegative=True)
        reasons = {}
        if actual_float is None:
            reasons['actual_float_cny'] = f'{day}：自由流通股本或同日价格缺失、价格不一致，未用总流通市值代替。'
        if turnover is None:
            reasons['actual_turnover_pct'] = f'{day}：原始日频资料未提供自由流通换手率，缺失不补零。'
        if net is None:
            reasons['main_net_cny'] = f'{day}：原始个股资金资料未提供该股主力净额。'
        result.append(dict(ts_code=code, name=f.get('name') or p.get('name') or names.get(code, code),
            name_basis='same_day' if f.get('name') or p.get('name') else 'catalog',
            pct_change=text(number(row.get('pct_chg'))), amount_cny=text(amount * 1000) if amount is not None else None,
            main_net_cny=text(net * 10000) if net is not None else None,
            actual_float_cny=text(actual_float), actual_turnover_pct=text(turnover), field_reasons=reasons,
            first_time=clock_text(p.get('first_time')), last_time=clock_text(p.get('last_time')),
            board_label=p.get('up_stat'), open_times=text(number(p.get('open_times'), nonnegative=True)),
            pool=p.get('limit'), themes=[]))
    return result


def ranked(rows, field, limit, *, ready=True, reason=None, window=None):
    valid = [row for row in rows if number(row.get(field)) is not None] if ready else []
    valid.sort(key=lambda row: (-number(row[field]), row['ts_code']))
    return dict(rows=[dict(row, rank=i + 1) for i, row in enumerate(valid[:limit])],
        state='ready' if valid else 'empty' if ready else 'unavailable', reason=reason,
        ranking_field=field, limit=limit, valid_count=len(valid), sample_count=len(rows), window=window)


def accumulate(accumulators, day_index, day_count, daily, basics):
    quotes, basic = indexed(daily), indexed(basics)
    for period, acc in accumulators.items():
        if day_index < day_count - period:
            continue
        if quotes is None:
            acc['missing_days'] += 1
            continue
        acc['loaded_days'] += 1
        for code, row in quotes.items():
            entry = acc['stocks'].setdefault(code, dict(n=0, factor=Decimal(1), amount=Decimal(0),
                amount_n=0, turnover=Decimal(0), turnover_n=0))
            close, previous = number(row.get('close')), number(row.get('pre_close'))
            if close and previous and close > 0 and previous > 0:
                entry['factor'] *= close / previous
                entry['n'] += 1
            value = number(row.get('amount'), nonnegative=True)
            if value is not None:
                entry['amount'] += value * 1000
                entry['amount_n'] += 1
            turn = number(((basic or {}).get(code) or {}).get('turnover_rate_f'), nonnegative=True)
            if turn is not None:
                entry['turnover'] += turn
                entry['turnover_n'] += 1


def default_day(db, clock):
    days = available_trade_days(db, limit=2, as_of_time=clock)
    local = clock.astimezone(SHANGHAI)
    eligible = [d for d in days if d < local.date() or local.time() >= time(16, 30)]
    return eligible[-1] if eligible else None


def overview_view(db, trade_day=None, lookback=20, as_of_time=None):
    if type(lookback) is not int or lookback not in {20, 60}:
        raise ValueError('lookback must be 20 or 60')
    clock = as_of_time or now(db)
    end = trade_day or default_day(db, clock) or clock.astimezone(SHANGHAI).date()
    navigation = available_trade_days(db, limit=200, as_of_time=clock)
    days = [d for d in navigation if d <= end][-max(32, lookback + 2):]
    selected = end if trade_day else days[-1] if days else None
    known = selected is not None and selected in days
    result = dict(version=VERSION, state='unavailable', trade_day=selected.isoformat() if selected else None,
        as_of_time=as_of_time.isoformat() if as_of_time else None, knowledge_mode='observed' if as_of_time else 'review',
        calendar_state='complete' if known else 'unavailable', lookback=lookback,
        trade_days=[d.isoformat() for d in navigation], records=[], rankings={}, coverage=[], evidence_refs=[],
        metric_definitions=[dict(key=k, label=label, unit=unit) for k, label, unit in METRICS],
        scope='沪深日线实际返回范围；涨跌停、连板与封板按同日供应商池；不宣称全交易所覆盖。',
        schedule=dict(time='16:30', timezone='Asia/Shanghai', retry_minutes=15, late_refresh='17:30'),
        interval_basis='连续交易日逐日close/pre_close链式收益（原始pre_close为除权昨收）；区间成交额与自由流通换手逐日相加，缺任一交易日不入区间排名。')
    if not known:
        return result
    refs = References(db, clock, as_of_time)
    names = {symbol + ('.SH' if exchange == 'SSE' else '.SZ'): name
             for symbol, exchange, name in db.execute(select(Instrument.symbol, Instrument.exchange, Instrument.name).where(
                 Instrument.asset_type == 'stock', Instrument.exchange.in_(['SSE', 'SZSE']), Instrument.valid_to.is_(None)))}
    acc = {n: dict(stocks={}, missing_days=0, loaded_days=0) for n in (5, 10, 20, 30)}
    previous, prior_pools = {}, None
    last_rows, selected_tables = [], {}
    metadata, evidence_refs = [], {}
    for index, day in enumerate(days):
        chart_day = index >= len(days) - lookback
        datasets = ['daily', 'group_basics'] + (['limits', 'pools', 'emotion_history', 'market_funds'] if chart_day or index >= len(days)-lookback-2 else [])
        if day == selected:
            datasets.append('stock_funds')
        tables, current = {}, {}
        for dataset in datasets:
            table = dict(load_dataset(db, dataset, day, as_of_time=as_of_time))
            if dataset == 'pools' and table.get('state') == 'complete' and not table.get('row_count'):
                table.update(state='missing', reason='empty_pool_publication')
            table.update(dataset=dataset, trade_day=day.isoformat(), scope='all')
            tables[dataset] = table
            current[dataset] = _dated_rows(table, day)
            if chart_day or day == selected:
                metadata.append(_metadata(table))
                for ref in table.get('evidence_refs', []):
                    evidence_refs[ref['id']] = ref
        accumulate(acc, index, len(days), current['daily'], current['group_basics'])
        if chart_day:
            emotions = current.get('emotion_history') or []
            emotion = text(number(emotions[0].get('emotion'))) if len(emotions) == 1 else None
            point = daily_point(day, current, previous, prior_pools, tables, emotion, auction_metrics(refs, day))
            funds = current.get('market_funds') or []
            point['values']['main_net'] = text(number(funds[0].get('net_amount'))) if len(funds) == 1 else None
            point['state'] = 'complete' if all(v is not None for v in point['values'].values()) else 'partial'
            result['records'].append(point)
        if day == selected:
            last_rows = stock_rows(day, current['daily'], current['group_basics'], current.get('pools'), current.get('stock_funds'), names)
            selected_tables = tables
        prior_pools = previous.get('pools')
        previous = {key: current.get(key) for key in ('daily', 'limits', 'pools')}
        # No request-wide cache of 32 full-market raw tables.
    daily_ready = selected_tables.get('daily', {}).get('state') == 'complete'
    result['rankings'] = {
        'amount': ranked(last_rows, 'amount_cny', 10, ready=daily_ready),
        'main_net': ranked(last_rows, 'main_net_cny', 10,
            ready=daily_ready and selected_tables.get('stock_funds', {}).get('state') == 'complete'),
        'limit_up': ranked([r for r in last_rows if r['pool'] == 'U'], 'amount_cny', 10,
            ready=daily_ready and selected_tables.get('pools', {}).get('state') == 'complete'),
        'broken': ranked([r for r in last_rows if r['pool'] == 'Z'], 'amount_cny', 10,
            ready=daily_ready and selected_tables.get('pools', {}).get('state') == 'complete'),
    }
    by_code = {r['ts_code']: r for r in last_rows}
    for period, values in acc.items():
        ready = daily_ready and values['loaded_days'] == period and not values['missing_days']
        rows = []
        for code, item in values['stocks'].items():
            if item['n'] != period or code not in by_code:
                continue
            reasons = dict(by_code[code]['field_reasons'])
            if item['turnover_n'] != period:
                reasons['interval_turnover_pct'] = f"截至{selected}的{period}个交易日：仅收到{item['turnover_n']}/{period}日自由流通换手率，缺失不补零。"
            if item['amount_n'] != period:
                reasons['interval_amount_cny'] = f"截至{selected}的{period}个交易日：仅收到{item['amount_n']}/{period}日成交额，未显示不完整的合计。"
            rows.append(dict(by_code[code], field_reasons=reasons, interval_pct=text((item['factor']-1)*100),
                interval_amount_cny=text(item['amount']) if item['amount_n'] == period else None,
                interval_turnover_pct=text(item['turnover']) if item['turnover_n'] == period else None))
        result['rankings'][str(period)] = ranked(rows, 'interval_pct', 20, ready=ready,
            reason=None if ready else 'interval_sessions_incomplete',
            window=dict(sessions=period, start=days[-period].isoformat() if len(days) >= period else None,
                        end=selected.isoformat(), loaded_sessions=values['loaded_days'],
                        excluded_incomplete_count=len(values['stocks'])-len(rows)))
    # Use the completed original THS membership capture known by the selected
    # date. A later current membership must never leak into an older review.
    from app.market.ths_members import attach_themes
    stocks = {row['ts_code']: SimpleNamespace(ths_themes=[], ths_themes_received_at=None)
              for panel in result['rankings'].values() for row in panel['rows']}
    attach_themes(refs, selected, stocks)
    for panel in result['rankings'].values():
        for row in panel['rows']:
            item = stocks[row['ts_code']]
            row['themes'] = item.ths_themes
            row['themes_received_at'] = item.ths_themes_received_at.isoformat() if item.ths_themes_received_at else None
    evidence_refs.update(refs.refs)
    result.update(state='partial' if any(p['state'] != 'ready' for p in result['rankings'].values())
                  or any(p['state'] != 'complete' for p in result['records']) else 'complete',
        coverage=metadata + list(refs.coverage.values()), evidence_refs=list(evidence_refs.values()), sample_count=len(last_rows))
    return result
