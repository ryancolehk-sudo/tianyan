"""On-demand review details. Only authorized, dated local archives are read."""
from collections import Counter, defaultdict
import uuid

from sqlalchemy import select, func, true, column
from sqlalchemy.dialects.postgresql import JSONB
from app.evidence.models import EvidenceVersion

from app.market.review_read import (
    _Reader, _aggregate, _context, _dated_rows, _json, _metadata, _number,
    _referenced, _references, _event_key,
)
from app.market.review_store import load_dataset
from app.market.review_metrics import build_daily_metrics
from app.market.review_series import _compact_metrics
from app.market.review_ladder import saved_ladder_table


class ArchiveReader(_Reader):
    """New analysis uses purchased archives, never legacy fallback suppliers."""
    def get(self, dataset, day, scope='all', *, include_rows=True):
        key = (dataset, day, scope, include_rows)
        if key not in self.cache:
            table = dict(load_dataset(self.db, dataset, day, scope=scope,
                as_of_time=self.as_of_time, include_rows=include_rows)) if day else dict(state='missing', rows=[])
            table.update(dataset=dataset, trade_day=day.isoformat() if day else None, scope=scope)
            self.cache[key] = table
        return self.cache[key]

    def group_units(self, table):
        """Read immutable unit declarations without rewriting old archives."""
        result = {}
        for ref in table.get('evidence_refs', []):
            evidence = self.db.get(EvidenceVersion, uuid.UUID(ref['id']))
            if evidence is not None and evidence.content_hash == ref.get('content_hash'):
                result[ref['id']] = {c.get('key'): c.get('unit') for c in evidence.payload.get('columns', [])}
        return result

    def stock(self, dataset, day, code):
        """Validate complete manifests, then project one code inside PostgreSQL."""
        table = self.get(dataset, day, include_rows=False)
        if table['state'] != 'complete':
            return table
        selected = []
        for ref in table.get('evidence_refs', []):
            cells = func.jsonb_array_elements(EvidenceVersion.payload['rows']).table_valued(
                column('value', JSONB), with_ordinality='row_number').render_derived().alias('analysis_rows')
            statement = select(cells.c.value, cells.c.row_number).select_from(EvidenceVersion).join(cells, true()).where(
                EvidenceVersion.id == uuid.UUID(ref['id']), EvidenceVersion.content_hash == ref['content_hash'],
                cells.c.value['ts_code'].astext == code)
            for row, number in self.db.execute(statement):
                selected.append({**row, 'row_reference': {'id': ref['id'], 'content_hash': ref['content_hash'],
                    'source_id': ref['source_id'], 'received_at': ref['received_at'], 'source_row': number-1}})
        table = {**table, 'rows': selected}
        self.cache[(dataset, day, 'stock:'+code, True)] = table
        return table


def pool_groups(reader, day):
    pool, members, themes = (reader.get(key, day) for key in ('pools', 'members', 'themes'))
    rows = _dated_rows(pool, day)
    membership = _dated_rows(members, day)
    catalog = _dated_rows(themes, day)
    daily = reader.get('daily', day)
    daily_rows = _dated_rows(daily, day)
    quotes = defaultdict(list)
    for row in daily_rows or []:
        quotes[row.get('ts_code')].append(row)
    records = [_referenced(row, pool, i) for i, row in enumerate(rows or [])
               if row.get('limit') == 'U' and str(row.get('ts_code')).endswith(('.SH', '.SZ'))]
    counts = Counter(row['ts_code'] for row in records)
    # Ambiguous source rows cannot establish one stock's board or theme membership.
    unique = [row for row in records if counts[row['ts_code']] == 1]
    for row in unique:
        height = _number(row.get('limit_times'), positive=True)
        row['board_height'] = int(height) if height is not None and height == int(height) else None
        same_day = quotes.get(row['ts_code'], [])
        value = _number(same_day[0].get('amount')) if len(same_day) == 1 else None
        row['turnover_yuan'] = str(value*1000) if value is not None and value >= 0 else None
    # Enrichment remains bounded to the same archived day and receipt cutoff.
    from app.market.review_group_fields import enrich_groups
    listings = reader.get('lhb', day)
    listing_rows = _dated_rows(listings, day)
    unit_maps = reader.group_units(pool) if hasattr(reader, 'group_units') else {}
    detail_table, basic_table = reader.get('pool_details', day), reader.get('group_basics', day)
    detail_rows = [_referenced(r, detail_table, i) for i, r in enumerate(_dated_rows(detail_table, day) or [])]
    basic_rows = [_referenced(r, basic_table, i) for i, r in enumerate(_dated_rows(basic_table, day) or [])]
    enrich_groups(unique, pool, unit_maps, listings, listing_rows, detail_rows, basic_rows)
    boards, mapped = defaultdict(list), defaultdict(set)
    for row in unique:
        height = _number(row.get('limit_times'), positive=True)
        key = str(int(height)) if height is not None and height == int(height) else 'unknown'
        boards[key].append(row['ts_code'])
    names = {row.get('ts_code'): row.get('name') for row in catalog or []}
    for row in membership or []:
        if row.get('ts_code') and row.get('con_code'):
            mapped[row['con_code']].add(row['ts_code'])
    groups, unassigned = defaultdict(list), []
    for row in unique:
        codes = mapped.get(row['ts_code'], set())
        if not codes:
            unassigned.append(row['ts_code'])
        for code in sorted(codes):
            groups[code].append(row['ts_code'])
    return dict(state=pool.get('state'), records=unique, ambiguous_n=len(records)-len(unique),
        boards=[dict(key=key, label='高度待核' if key == 'unknown' else key+'板', codes=codes)
                for key, codes in sorted(boards.items(), key=lambda x: -(int(x[0]) if x[0].isdigit() else 0))],
        themes=[dict(key=key, label=names.get(key) or key, codes=codes)
                for key, codes in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))],
        unassigned_codes=unassigned, membership_state=members.get('state'),
        membership_day=day.isoformat() if membership is not None else None,
        scope='当日供应商沪深涨停池；题材可重复归属，不能将分组数相加作为股票总数',
        coverage=[_metadata(t) for t in (pool, members, themes, daily, listings, detail_table, basic_table)])


def emotion_history(db, trade_day=None, lookback=20, as_of_time=None):
    """New curves and ladder comparisons share the exact purchased pool archive.

    The legacy review reader deliberately retains its older supplier fallback.
    It must not establish this new feature's universe when the purchased pool is
    missing or explicitly empty. Receipt cutoffs apply to warmup inputs too.
    """
    warmup_count = 3
    context, days, selected = _context(db, trade_day, lookback + warmup_count, as_of_time)
    window = days[-lookback:] if selected else []
    warmup = days[:-len(window)] if window else []
    result = {**context, 'state': 'unavailable', 'kind': 'emotion', 'lookback': lookback,
        'trade_days': [day.isoformat() for day in window],
        'returned_day_count': len(window), 'calendar_window_complete': len(window) == lookback,
        'warmup_trade_days': [day.isoformat() for day in warmup],
        'warmup_calendar_complete': len(warmup) == warmup_count,
        'requested_warmup_days': warmup_count, 'records': [], 'coverage': [], 'evidence_refs': [],
        'pool_dataset': 'pools', 'source_policy': 'purchased_archives',
        'scope_note': '与多日梯队统一使用已购兼容通道原日沪深涨停池；仅完整日频归档，'
                      '不以其他供应商或盘中样本补池；缺失不补零，历史补采不代表当时已知'}
    if not selected:
        return result
    reader, refs = ArchiveReader(db, as_of_time), {}
    for index in range(len(days) - len(window), len(days)):
        day = days[index]
        previous = days[index - 1] if index else None
        prior = days[index - 2] if index >= 2 else None
        current = {key: reader.get(key, day) for key in ('daily', 'limits', 'pools')}
        yesterday = {key: reader.get(key, previous) for key in ('daily', 'limits', 'pools')}
        prior_pool = reader.get('pools', prior)
        calculation_valid = True
        try:
            metrics = build_daily_metrics(trade_day=day.isoformat(),
                daily=_dated_rows(current['daily'], day),
                price_limits=_dated_rows(current['limits'], day),
                pools=_dated_rows(current['pools'], day),
                previous_daily=_dated_rows(yesterday['daily'], previous),
                previous_price_limits=_dated_rows(yesterday['limits'], previous),
                previous_pools=_dated_rows(yesterday['pools'], previous),
                prior_pools=_dated_rows(prior_pool, prior),
                seal_amount_unit_verified=current['pools'].get('seal_amount_unit_verified') is True)
        except ValueError:
            calculation_valid = False
            metrics = {'state': 'unavailable', 'relay': {'by_height': {}},
                       'quality_flags': [{'code': 'invalid_metric_input_table'}]}
        metrics['calendar'] = dict(trade_day=day.isoformat(),
            previous_trade_day=previous.isoformat() if previous else None,
            prior_trade_day=prior.isoformat() if prior else None)
        compact = _compact_metrics(metrics)
        # A selected date can differ from the range end. Preserve only the
        # disjoint height cohorts for every point so its denominator can be
        # inspected without fetching or silently changing the chart window.
        compact.setdefault('relay', {})['by_height'] = metrics.get('relay', {}).get('by_height', {})
        tables = [*current.values(), *yesterday.values(), prior_pool]
        coverage = [_metadata(table) for table in tables]
        references = _references(tables)
        result['records'].append(dict(trade_day=day.isoformat(),
            state=_aggregate([metrics['state'], current['pools']['state'], yesterday['pools']['state']])
                  if calculation_valid else 'unavailable',
            metrics=compact, by_height_samples_included=calculation_valid,
            coverage=coverage, evidence_refs=references))
        result['coverage'].extend(coverage)
        refs.update((ref['id'], ref) for ref in references)
        # Bound raw memory to the current day and the previous pool, as in the
        # existing series; only compact results cross the requested window.
        reader.cache = {key: table for key, table in reader.cache.items()
                        if key[0] in {'daily', 'limits', 'pools'} and key[1] == day
                        or key[0] == 'pools' and key[1] == previous}
    result['state'] = _aggregate(row['state'] for row in result['records'])
    if result['state'] == 'complete' and not (result['calendar_window_complete'] and result['warmup_calendar_complete']):
        result['state'] = 'partial'
    result['evidence_refs'] = list(refs.values())
    return _json(result)


def analysis_view(db, trade_day=None, kind='groups', lookback=20, stock=None, as_of_time=None):
    if kind not in {'groups', 'ladder', 'stock', 'emotion', 'comparison'} or lookback not in {20, 60}:
        raise ValueError('unsupported review analysis')
    if kind == 'comparison':
        from app.market.review_comparison import comparison_view
        return comparison_view(db, trade_day, as_of_time)
    if kind == 'emotion':
        return emotion_history(db, trade_day, lookback, as_of_time)
    context, days, selected = _context(db, trade_day, lookback, as_of_time)
    result = {**context, 'state': 'unavailable', 'kind': kind, 'trade_days': [d.isoformat() for d in days],
              'records': [], 'coverage': [], 'scope_note': '仅有权已保存资料；缺失不补零，历史补采不代表当时已知'}
    if not selected:
        return result
    reader = ArchiveReader(db, as_of_time)
    if kind == 'groups':
        result.update(groups=pool_groups(reader, selected))
        result['state'] = result['groups']['state']
    else:
        refs = {}
        for day in days:
            table = reader.get('pools', day) if kind == 'ladder' else reader.stock('daily', day, stock)
            rows = _dated_rows(table, day)
            if kind == 'ladder':
                candidates = [(i, row) for i, row in enumerate(rows or []) if row.get('limit') == 'U' and str(row.get('ts_code')).endswith(('.SH','.SZ'))]
                counts = Counter(row.get('ts_code') for _, row in candidates)
                up = []
                for i, row in candidates:
                    if counts[row['ts_code']] != 1:
                        continue
                    height = _number(row.get('limit_times'), positive=True)
                    up.append({**_referenced(row, table, i), 'board_height': int(height) if height is not None and height == int(height) else None})
                entry = dict(trade_day=day.isoformat(), state=table.get('state'), stocks=up)
                # The full U/D/Z archive can be pending after the separate close
                # ladder already arrived. Show that original same-day list with
                # its narrower scope; it must not establish emotion denominators.
                if table.get('state') in {'missing', 'complete'} and not rows:
                    saved = saved_ladder_table(db, day, as_of_time)
                    if saved is not None:
                        entry.update(state='partial', basis='supplier_ladder',
                            source_family=saved['source_family'],
                            coverage_note='收盘连板榜 · 2板及以上；首板待完整日榜',
                            stocks=[_referenced(row, saved, i) for i, row in enumerate(saved['rows'])])
                        reader.cache[('ladder', day)] = saved
                        result['coverage'].append(_metadata(saved))
            else:
                matching = [(i, row) for i, row in enumerate(rows or []) if row.get('ts_code') == stock]
                quote = matching[0][1] if len(matching) == 1 else None
                listing = reader.stock('lhb', day, stock)
                listing_rows = _dated_rows(listing, day)
                markers = [dict(record_key=_event_key(day, stock, row.get('reason')), reason=row.get('reason'))
                           for row in listing_rows or [] if row.get('ts_code') == stock]
                entry = dict(trade_day=day.isoformat(), state=table.get('state'), quote=quote,
                             listing_state=listing.get('state'), listings=markers, adjustment='unadjusted')
            result['records'].append(entry)
            result['coverage'].append(_metadata(table))
            for ref in _references(reader.cache.values()):
                refs[ref['id']] = ref
            # Keep at most one day's raw market tables in memory, even at 60 days.
            reader.cache.clear()
        result['state'] = _aggregate(row['state'] for row in result['records'])
        result['evidence_refs'] = list(refs.values())
    if kind == 'groups':
        result['evidence_refs'] = _references(reader.cache.values())
    return _json(result)
