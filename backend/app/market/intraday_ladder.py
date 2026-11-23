"""Derived live ladder over the existing authorized, unbounded quote projection.

No collection, writes or reconstructed order-book events. Missing yesterday
heights stay unknown: absence from a partial ladder never implies first board.
"""
from datetime import datetime, time
from math import isfinite

from app.market.protocol import SHANGHAI
from app.market.session import quote_fresh_at
from app.market.terminal_schemas import IntradayLadder, IntradayLadderStock


def live_ladder(stocks, day, previous, cutoff, session, *, historical=False, first_board_codes=frozenset()):
    if (historical or session.is_trading_day is not True or session.trade_day != day
            or day != cutoff.astimezone(SHANGHAI).date()):
        return None
    # A close-phase last observation remains explicitly intraday until the
    # independently received final daily ladder replaces it in the view.
    reference = quote_fresh_at(cutoff)
    rows, clocks = [], []
    eligible = 0
    for stock in stocks.values():
        code, name = stock.code, stock.name
        main = ((code.endswith('.SH') and code[:3] in {'600', '601', '603', '605'})
                or (code.endswith('.SZ') and code.startswith(('000', '001', '002', '003'))))
        if not main or 'ST' in name.upper() or '退' in name:
            continue
        old = stock.previous_height
        old = int(old) if previous and old is not None and isfinite(old) and old == int(old) and old > 0 else None
        if old is None and previous and code in first_board_codes:
            old = 0
        stamp = None
        if stock.price_basis == 'intraday' and stock.source_time:
            try:
                candidate = datetime.fromisoformat(stock.source_time)
                local = candidate.astimezone(SHANGHAI)
                clock = local.time().replace(tzinfo=None)
                if (candidate.utcoffset() is not None and local.date() == day and candidate <= cutoff
                        and (stock.source_received_at is None or stock.source_received_at <= cutoff)
                        and (time(9, 30) <= clock < time(11, 31) or time(13) <= clock < time(15, 1))):
                    stamp = candidate
            except (ValueError, TypeError):
                pass
        freshness = 'unknown' if stamp is None else 'stale' if (reference - stamp).total_seconds() > 180 else 'current'
        status = 'unknown'
        if stamp is not None:
            eligible += int(stock.price is not None)
            clocks.append(stamp)
            price, upper, high = stock.price, stock.limit_up, stock.high
            if price is not None and upper is not None and price == upper:
                status = 'sealed' if stock.seal_amount_cny is not None and stock.seal_amount_cny > 0 else 'touched'
            elif price is not None and upper is not None and high is not None and high >= upper and price < upper:
                status = 'opened'
            elif price is not None and upper is not None:
                status = 'waiting'
        if old is None and status not in {'sealed', 'touched', 'opened'}:
            continue
        rows.append(IntradayLadderStock(code=code, name=name, previous_height=old,
            target_height=old + 1 if old is not None else None, status=status, freshness=freshness,
            source_time=stamp, source_name=stock.source_name, pct_change=stock.pct_change))
    rows.sort(key=lambda row: (-(row.target_height or 0), row.code))
    return IntradayLadder(trade_day=day, previous_trade_day=previous, as_of_time=cutoff,
        phase=session.phase, source_time=max(clocks) if clocks else None, sample_count=eligible,
        sealed_count=sum(row.status == 'sealed' and row.freshness == 'current' for row in rows),
        stale_count=sum(row.freshness != 'current' for row in rows), rows=rows,
        scope='沪深主板非ST已存行情，使用列表截断前全部样本；非全市场完整性承诺。昨日板数取已存日榜；昨日收盘价低于同日涨停价才确认昨日0板，不从名单缺席猜首板；触板和开板不是晋级确认。')
