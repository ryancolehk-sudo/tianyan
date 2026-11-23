"""Same-day group fields; no unit guesses, synthetic classifications or live fetches."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

MONEY_FIELDS = {'fd_amount': 'seal_amount_yuan', 'float_mv': 'float_market_value_yuan',
                'total_mv': 'total_market_value_yuan'}
MULTIPLIERS = {'元': 1, '千元': 1000, '万元': 10000, '亿元': 100000000}


def nonnegative(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() and n >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def enrich_groups(records, pool, unit_maps, listings, listing_rows, details=(), basics=()):
    supplements = []
    for rows in (details, basics):
        grouped = defaultdict(list)
        for item in rows: grouped[item.get('ts_code')].append(item)
        supplements.append({code: items[0] for code, items in grouped.items() if len(items) == 1})
    by_code = defaultdict(list)
    for row in listing_rows or []:
        by_code[row.get('ts_code')].append(row)
    for row in records:
        ref = row.get('row_reference') or {}
        units = unit_maps.get(ref.get('id'), {})
        fields = {}
        for source, target in MONEY_FIELDS.items():
            raw = nonnegative(row.get(source))
            multiplier = MULTIPLIERS.get(units.get(source))
            row[target] = str(raw * multiplier) if raw is not None and multiplier is not None else None
            fields[target] = {'state': 'available' if row[target] is not None else 'unit_unverified' if raw is not None else 'missing',
                              'source_field': source, 'source_value': str(raw) if raw is not None else None,
                              'source_unit': units.get(source), 'reference': ref}
        hits = by_code.get(row.get('ts_code'), [])
        # A complete empty listing is distinct from unavailable/invalid archives.
        row['lhb_status'] = 'listed' if hits else 'not_listed' if listing_rows is not None and listings.get('state') == 'complete' else 'unavailable'
        row['lhb_count'] = len(hits)
        row['lhb_reasons'] = sorted({str(r['reason']) for r in hits if r.get('reason')})
        # This endpoint does not disclose these three fields. Keep the gap explicit.
        for key, note in [('limit_type', '当前涨停池未披露一字板、T字板等分类'),
                          ('change_reason', '当前涨停池未披露异动原因'),
                          ('actual_float_value_yuan', '当前涨停池未披露实际流通市值')]:
            row[key] = None
            fields[key] = {'state': 'missing', 'note': note}
        detail, basic = (items.get(row['ts_code']) for items in supplements)
        if detail:
            for target, source in [('limit_type','status'),('change_reason','lu_desc'),('board_stat_label','tag')]:
                value = detail.get(source)
                if isinstance(value, str) and value.strip():
                    row[target] = value.strip()
                    fields[target] = {'state':'available', 'source_field':source, 'reference':detail.get('row_reference')}
        if basic:
            # These units are explicitly specified by daily_basic (doc_id=32).
            for target, source in [('float_market_value_yuan','circ_mv'),('total_market_value_yuan','total_mv')]:
                value = nonnegative(basic.get(source))
                if value is not None:
                    row[target] = str(value * 10000)
                    fields[target] = {'state':'available', 'source_field':source, 'source_unit':'万元', 'reference':basic.get('row_reference')}
            shares, close = nonnegative(basic.get('free_share')), nonnegative(basic.get('close'))
            if shares is not None and close is not None and close > 0:
                row['actual_float_value_yuan'] = str(shares * 10000 * close)
                fields['actual_float_value_yuan'] = {'state':'available', 'basis':'derived', 'note':'同日自由流通股本（万股）×10000×收盘价（元）', 'reference':basic.get('row_reference')}
            if detail and close is not None and close > 0 and nonnegative(detail.get('price')) == close:
                quantity, sealed = nonnegative(detail.get('limit_order')), nonnegative(detail.get('limit_amount'))
                # The compatible THS endpoint documents mixed yuan/share fields.
                # Accept this row only when the declared price and share quantity
                # reconcile to the monetary total (allow two shares of rounding).
                if quantity is not None and sealed is not None and abs(quantity * close - sealed) <= close * 2:
                    row['seal_amount_yuan'] = str(sealed)
                    fields['seal_amount_yuan'] = {'state':'available', 'source_field':'limit_amount', 'source_unit':'元',
                        'note':'封单额与封单股数×同日元报价逐行核对通过', 'reference':detail.get('row_reference'), 'validation_reference':basic.get('row_reference')}
        row['group_field_details'] = fields
