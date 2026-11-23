"""Permission-checked direction rankings. All GETs read immutable local evidence."""
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.models import DataFetchRun
from app.market.protocol import SHANGHAI
from app.market.review_protocol import DATASET_FAMILIES
from app.market.review_store import (
    available_trade_days,
    load_dataset,
    permitted_provider,
)
from app.market.visibility import instrument_name_sql, instrument_visible_sql
from app.models import Instrument

PUBLIC = {'ths': ('direction_ths_hot', 'ths_hot', '同花顺公开人气榜'),
          'tdx': ('direction_tdx_hot', 'tdx_hot', '通达信公开热搜榜'),
          'eastmoney': ('direction_em_hot', 'em_hot', '东方财富公开人气榜')}


def panel(day, source, *, status='missing', reason=None, rows=None, actual=None,
          received_at=None, total_count=0, source_time=None):
    return dict(trade_day=actual.isoformat() if isinstance(actual, date) else actual,
                requested_day=day.isoformat() if day else None, status=status,
                source_label=source, source_time=source_time, received_at=received_at,
                reason=reason, rows=rows or [], total_count=total_count, truncated=False)


def decimal(value):
    if value is None or str(value).strip() in {'', '--', '-'}:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _table(db, dataset, day, cutoff):
    """Prefer a committed complete manifest; support existing dated source tables."""
    table = load_dataset(db, dataset, day, as_of_time=cutoff)
    if table['state'] == 'complete' or table['state'] not in {'missing'}:
        return table
    family = DATASET_FAMILIES[dataset]
    provider = permitted_provider(db, family, cutoff)
    if provider is None:
        return table
    api = 'moneyflow_ind_dc' if dataset in {'theme_funds', 'industry_funds'} else 'dc_index'
    saved = db.scalar(select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at <= (cutoff or now(db)),
        EvidenceVersion.payload['dataset'].astext == 'tushare.' + api,
        EvidenceVersion.payload['rows'][0]['trade_date'].astext == day.strftime('%Y%m%d'),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if saved is not None:
        return dict(state='complete', rows=saved.payload['rows'], received_at=saved.received_at.isoformat())
    return table


def board_panel(db, dataset, day, cutoff, *, funds=False, limit=10):
    source = '东方财富 · 已购兼容通道'
    if day is None:
        return panel(None, source, reason='上一交易日历尚未齐备')
    table = _table(db, dataset, day, cutoff)
    state = table['state']
    if state != 'complete':
        reason = '请在数据中心核对来源权限' if state == 'permission_denied' else '该交易日尚无完整保存资料'
        if state == 'missing' and day == (cutoff or now(db)).astimezone(SHANGHAI).date():
            provider = permitted_provider(db, DATASET_FAMILIES[dataset], cutoff)
            last = db.scalar(select(DataFetchRun).where(
                DataFetchRun.source_id == provider.id,
                DataFetchRun.received_at <= (cutoff or now(db)),
            ).order_by(DataFetchRun.received_at.desc()).limit(1)) if provider else None
            reason = ('最近一次盘后板块表尚未发布，已保存的盘中资金样本不代表全板块涨幅榜'
                      if last and last.reason == 'daily_data_not_published'
                      and last.received_at.astimezone(SHANGHAI).date() == day else
                      '当日完整板块表尚未归档，已保存的盘中资金样本不代表全板块涨幅榜')
        return panel(day, source, actual=day,
            status='disabled' if state == 'permission_denied' else 'error' if state == 'broken_lineage' else 'missing',
            reason=reason)
    rows = table['rows']
    field = 'net_amount' if funds else 'pct_change'
    classified = 'industry' in dataset
    classifier = 'content_type' if funds else 'idx_type'
    expected = {'行业', '行业板块'} if classified else {'概念', '概念板块', None, ''}
    unique, usable = set(), []
    for row in rows:
        if row.get('trade_date') != day.strftime('%Y%m%d') or row.get(classifier) not in expected:
            return panel(day, source, actual=day, status='error', reason='来源日期或板块分类不一致，请核对数据中心')
        code, value = row.get('ts_code'), decimal(row.get(field))
        if not code or code in unique:
            return panel(day, source, actual=day, status='error', reason='来源板块身份重复或缺失')
        unique.add(code)
        if value is not None and row.get('name'):
            usable.append((value, code, row))
    usable.sort(key=lambda entry: (-entry[0], entry[1]))
    top = [dict(rank=i + 1, code=code, name=row['name'], **{field: str(value)})
           for i, (value, code, row) in enumerate(usable[:limit])]
    reason = '部分板块缺少可排序数值，保留实际可用范围' if len(usable) != len(rows) else None
    value = panel(day, source, actual=day, rows=top, received_at=table.get('received_at'),
        status='partial' if reason else 'ready' if top else 'missing', reason=reason,
        total_count=len(usable))
    value['archive_state'] = 'complete'
    return value


def rotation_view(db, trade_day=None, sector_type='concept', sector_code=None, lookback=20, as_of_time=None):
    from app.market.review_read import _context
    context, days, selected = _context(db, trade_day, lookback, as_of_time)
    dataset = 'industry_themes' if sector_type == 'industry' else 'themes'
    fund_dataset = 'industry_funds' if sector_type == 'industry' else 'theme_funds'
    records = []
    for day in days if selected else []:
        ranks = board_panel(db, dataset, day, as_of_time, limit=None)
        funds = board_panel(db, fund_dataset, day, as_of_time, funds=True, limit=None)
        row = next((r for r in ranks['rows'] if r['code'] == sector_code), None)
        fund = next((r for r in funds['rows'] if r['code'] == sector_code), None)
        rank_complete = ranks['status'] == 'ready' or ranks.get('archive_state') == 'complete' and ranks['total_count'] == 0
        funds_complete = funds['status'] == 'ready' or funds.get('archive_state') == 'complete' and funds['total_count'] == 0
        records.append(dict(trade_day=day.isoformat(), status=ranks['status'], funds_status=funds['status'],
            rank=row['rank'] if row and rank_complete else None, in_top_ten=row['rank'] <= 10 if row and rank_complete else None,
            selection_state='ranked' if row and rank_complete else 'not_disclosed' if rank_complete else 'unavailable',
            rank_complete=rank_complete, funds_complete=funds_complete,
            pct_change=row.get('pct_change') if row else None, net_amount=fund.get('net_amount') if fund else None,
            top_ten=ranks['rows'][:10], total_count=ranks['total_count'],
            received_at=ranks['received_at'], funds_received_at=funds['received_at']))
    return {**context, 'state': 'complete' if records and all(r['status']=='ready' and r['funds_status']=='ready' for r in records) else 'partial' if records else 'unavailable',
            'sector_type': sector_type, 'sector_code': sector_code, 'lookback': lookback, 'records': records,
            'coverage': {'requested_days': lookback, 'calendar_days': len(records),
                         'ranking_days': sum(r['rank_complete'] for r in records),
                         'funds_days': sum(r['funds_complete'] for r in records)},
            'scope_note': '同来源完整已保存日榜排名；当日板块资金与原右侧上一交易日资金分开展示，缺失不补零'}


def _failed_run(run):
    # Older history jobs used "empty" for an upstream rejection. Its recorded
    # reason remains a failure; it cannot make an older successful list ready.
    return bool(run and (run.status in {'error','unavailable'} or run.reason and run.status!='complete'))


def snapshot_panel(db, family, dataset, label, day, cutoff, *, allow_latest=False, strength=False):
    provider = permitted_provider(db, family, cutoff)
    if provider is None:
        return panel(day, label, status='disabled', reason='来源尚未启用或展示权限未核实，请到数据中心检查')
    conditions = [EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at <= (cutoff or now(db)),
        EvidenceVersion.payload['dataset'].astext.in_(['market.direction.' + dataset,
            *(['market.direction.em_hot_daily'] if family=='direction_em_hot' else [])])]
    exact = select(EvidenceVersion).where(*conditions,
        EvidenceVersion.payload['observed_day'].astext == day.isoformat())
    ordering = [EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()]
    if cutoff is None and day == now(db).astimezone(SHANGHAI).date():
        ordering.insert(0, (EvidenceVersion.payload['dataset'].astext == 'market.direction.' + dataset).desc())
    saved = db.scalar(exact.order_by(*ordering).limit(1))
    if saved is None and allow_latest:
        saved = db.scalar(select(EvidenceVersion).where(*conditions).order_by(
            EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1))
    if saved is None:
        last = db.scalar(select(DataFetchRun).where(DataFetchRun.source_id == provider.id,
            DataFetchRun.received_at <= (cutoff or now(db))).order_by(DataFetchRun.received_at.desc()).limit(1))
        return panel(day, label, status='error' if _failed_run(last) else 'missing',
            reason='最近一次采集失败，原有档案已保留，可在数据中心查看' if last and last.reason else '该日尚无保存榜单；当前快照不能补造历史排名')
    raw = saved.payload.get('rows', [])
    historical_daily=saved.payload.get('dataset')=='market.direction.em_hot_daily'
    actual = saved.payload.get('observed_day')
    rows = []
    for row in raw:
        rank = decimal(row.get('rank'))
        if rank is None or rank != int(rank) or not 1 <= rank <= 10:
            continue
        item = dict(rank=int(rank), code=row['code'], name=row.get('name') or row['code'])
        for field in ('pct_change', 'heat', 'strength'):
            value = decimal(row.get(field))
            if value is not None:
                item[field] = str(value)
        change = decimal(row.get('rank_change_previous_trading_day') if historical_daily else row.get('rank_change'))
        item['rank_change'] = int(change) if change is not None and change == int(change) else None
        rows.append(item)
    rows.sort(key=lambda row: row['rank'])
    if len({row['rank'] for row in rows}) != len(rows) or len({row['code'] for row in rows}) != len(rows):
        return panel(day, label, status='error', reason='保存榜单的排名或身份冲突')
    source_times = {r.get('source_time') for r in raw if r.get('source_time')}
    source_time = next(iter(source_times)) if len(source_times) == 1 else None
    if actual != day.isoformat():
        state = 'stale' if strength else 'partial'
        reason = '展示最近保存的题材强度，实际数据日期见标题' if strength else '最新保存人气快照；不代表所选日的收盘排名'
    elif not strength and not any(r.get('source_day') for r in raw):
        state, reason = 'partial', '日期为本机采集归档日；来源未提供榜单交易时刻'
    else:
        state, reason = ('ready', None) if len(rows) == 10 else ('partial', '来源返回不足10条')
    last = db.scalar(select(DataFetchRun).where(
        DataFetchRun.source_id == provider.id, DataFetchRun.received_at <= (cutoff or now(db)),
    ).order_by(DataFetchRun.received_at.desc()).limit(1))
    if last and last.received_at > saved.received_at and _failed_run(last):
        state = 'stale'
        reason = '后续采集失败，保留最近成功榜单；实际接收时刻见标题'
    result = panel(day, label, actual=actual, status=state, rows=rows, reason=reason,
        received_at=saved.received_at.isoformat(), total_count=len(raw), source_time=source_time)
    if historical_daily:
        result['rank_change_basis']='previous_trading_day'
        comparison={r.get('rank_comparison_day') for r in raw if r.get('rank_comparison_day')}
        result['rank_comparison_day']=next(iter(comparison)) if len(comparison)==1 else None
        result['reason']='历史日频人气榜；名次变化比较已核上一交易日，未提供独立盘中时刻。'
    else:
        result['rank_change_basis'] = 'source_reported' if any(r.get('rank_change') is not None for r in raw) else 'not_provided'
    return result


def enrich_popularity_quotes(db, popularity, day, cutoff, clock):
    """Batch existing authorized quotes; ranking and price keep separate clocks."""
    from app.market.read_models import quote_batch
    codes = {row['code'] for panel in popularity.values() for row in panel['rows']}
    if not codes:
        return
    ids = list(db.scalars(select(Instrument.id).where(Instrument.symbol.in_(codes),
        Instrument.asset_type == 'stock', Instrument.valid_to.is_(None), instrument_visible_sql())))
    historical = cutoff or (datetime.combine(day, time.max, SHANGHAI)
                            if day < clock.astimezone(SHANGHAI).date() else None)
    quotes = quote_batch(db, instrument_ids=ids, as_of_time=historical,
                         observation_cutoff=clock if historical is None else None) if ids else None
    by_code = {q.instrument.symbol: q for q in quotes.items} if quotes else {}
    for panel in popularity.values():
        for row in panel['rows']:
            row['quote_state'] = 'ranking_snapshot' if row.get('pct_change') is not None else 'missing'
            row['quote_source'] = panel['source_label']
            row['quote_time'] = panel['source_time']
            row['quote_received_at'] = panel['received_at']
            q = by_code.get(row['code'])
            if q is None or q.quote is None or q.change_percent is None:
                continue
            price_day = q.quote.trade_day or (q.quote.quote_time.astimezone(SHANGHAI).date() if q.quote.quote_time else None)
            if price_day != day:
                continue
            row.update(pct_change=str(q.change_percent), quote_state=q.state,
                quote_source=q.provenance.source_name if q.provenance else None,
                quote_time=q.quote.quote_time.isoformat() if q.quote.quote_time else None,
                quote_received_at=q.provenance.received_at.isoformat() if q.provenance else None)


def direction_view(db, owner_id, *, trade_day=None, sector_type='concept', as_of_time=None, end_day=None,
                   automatic_current=False):
    clock = as_of_time or now(db)
    local = clock.astimezone(SHANGHAI)
    current_day = None
    if as_of_time is None and (automatic_current or trade_day == local.date()) and trade_day in {None, local.date()}:
        from app.market.session import market_session
        session = market_session(db, clock)
        if session.is_trading_day is True and local.time().replace(tzinfo=None) >= time(9):
            current_day = local.date()
    available = available_trade_days(db, limit=200, end_day=current_day or trade_day, as_of_time=clock)
    day = trade_day or current_day or (available[-1] if available else None)
    if day is None or day not in available and day != current_day:
        from app.auth import APIError
        raise APIError(422, 'calendar_not_ready', '所选日期尚无可核实的收盘交易日历，请先在数据中心补齐')
    # At 09:00 a verified trading day owns an empty slot before any quote.
    # Older columns still refer to their actual closed trading dates.
    snapshot_day = current_day or day
    window = [value for value in available if value <= (end_day or current_day or day)][-5:]
    if current_day and (end_day is None or end_day == current_day):
        window = [value for value in available if value < current_day][-4:] + [current_day]
    previous = next((value for value in reversed(available) if value < day), None)
    funds_day = next((value for value in reversed(available) if value < snapshot_day), None) if current_day else previous
    latest = available_trade_days(db, limit=1, as_of_time=clock)
    # An automatic today's slot stays empty until today's exact data arrives.
    # Legacy explicit historical requests keep their existing fallback rules.
    allow_latest = not automatic_current and not as_of_time and bool(latest and day == latest[-1])
    dataset = 'industry_themes' if sector_type == 'industry' else 'themes'
    fund_dataset = 'industry_funds' if sector_type == 'industry' else 'theme_funds'
    days = [board_panel(db, dataset, value, as_of_time) for value in reversed(window)]
    for entry in days:
        if current_day and entry['requested_day'] == current_day.isoformat():
            if entry['rows'] and local.time().replace(tzinfo=None) < time(15):
                entry['status'] = 'partial'
                entry['reason'] = '今日尚未收盘，展示当前已保存板块样本；接收时间另列。'
            elif not entry['rows'] and entry['status'] == 'missing':
                entry['reason'] = '今日槽位已预留，等待当日板块资料；昨日资料保留在后一列。'
    popularity = {key: snapshot_panel(db, *spec, snapshot_day, as_of_time, allow_latest=allow_latest)
                  for key, spec in PUBLIC.items()}
    enrich_popularity_quotes(db, popularity, snapshot_day, as_of_time, clock)
    names = {row['code']: row['name'] for value in popularity.values() for row in value['rows']
             if row['name'] != row['code']}
    if not as_of_time:
        codes = {row['code'] for value in popularity.values() for row in value['rows']}
        if codes:
            names.update({code: name for code, name in db.execute(select(Instrument.symbol, instrument_name_sql()).where(
                Instrument.symbol.in_(codes), Instrument.asset_type == 'stock', Instrument.valid_to.is_(None),
                instrument_visible_sql())) if name})
    for value in popularity.values():
        for row in value['rows']:
            if row['name'] == row['code']:
                row['name'] = names.get(row['code'], row['code'])
    return dict(trade_day=day.isoformat(), sector_type=sector_type,
        snapshot_day=snapshot_day.isoformat(), automatic_current=current_day is not None,
        funds_day=funds_day.isoformat() if funds_day else None,
        previous_trade_day=previous.isoformat() if previous else None,
        days=days, funds=board_panel(db, fund_dataset, funds_day, as_of_time, funds=True),
        popularity=popularity, strength=snapshot_panel(db, 'direction_kpl_strength', 'kpl_strength',
            '开盘啦题材强度', snapshot_day, as_of_time, allow_latest=allow_latest, strength=True))
