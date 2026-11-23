"""Resolve first boards from already committed previous-day price evidence."""
from decimal import Decimal, InvalidOperation

from app.market.terminal_archives import closing_table


def positive(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def verified_first_boards(reader, previous, stocks):
    """Absence from a ladder is insufficient; require an exact price pair.

    Closing archives enforce provider rights, final-day boundaries, unique
    identities, immutable lineage and receipt cutoff. The previous upper limit
    must belong to the same code/day, use its native price unit and be received by
    the same cutoff. Never infer a first board from a partial/empty pool.
    """
    if previous is None:
        return frozenset()
    pending = {code for code, stock in stocks.items() if stock.previous_height is None
               and (upper := positive(stock.limit_up)) is not None
               and ((price := positive(stock.price)) == upper
                    or (high := positive(stock.high)) is not None and high >= upper)}
    if not pending or not (table := closing_table(reader, previous)):
        return frozenset()
    closes = {row['ts_code']: positive(row.get('close')) for row in table['rows']
              if row['ts_code'] in pending}
    restrictions = {}
    for row, columns, meta in reader.table('tushare_market_limits', previous):
        code, stamp = row.get('ts_code'), meta.received_at
        if (code not in pending or reader.day(row.get('trade_date')) != previous
                or stamp.utcoffset() is None or stamp > reader.cutoff):
            continue
        # This family is the fixed stk_limit protocol, whose raw A-share price
        # fields share daily's native unit. Legacy batches omit column units;
        # an explicitly different unit cannot acquire this interpretation.
        upper = positive(row.get('up_limit')) if columns.get('up_limit') in {None, '元'} else None
        old = restrictions.get(code)
        if old is None or stamp > old[0]:
            restrictions[code] = (stamp, upper)
        elif stamp == old[0] and upper != old[1]:
            restrictions[code] = (stamp, None)
    return frozenset(code for code, close in closes.items() if close is not None
                     and (upper := restrictions.get(code, (None, None))[1]) is not None
                     and close < upper)
