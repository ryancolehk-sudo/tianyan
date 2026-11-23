"""Local off-hours policy for automatic static references, not publication times.

Calendars and the stock directory remain available as live dependencies. Explicit
requests/history keep their existing permissions, configured spacing and budgets.
"""
from datetime import datetime, time

from app.market.protocol import SHANGHAI

FAMILIES = frozenset({
    'tushare_ths_member', 'tushare_ths_index',
    'tushare_income', 'tushare_balancesheet', 'tushare_cashflow',
    'tushare_fina_indicator', 'tushare_dividend', 'tushare_adj_factor',
    'tushare_index_basic_sse', 'tushare_index_basic_szse', 'tushare_cn_cpi',
})
# Keep automatic long member pages away from live collection and its 15:05
# receipt window. A missing calendar must not reopen a live resource window.
QUIET_START = time(9)
QUIET_END = time(15, 10)


def align_reference(family, clock):
    if family in FAMILIES:
        local = clock.astimezone(SHANGHAI)
        if QUIET_START <= local.time() < QUIET_END:
            return datetime.combine(local.date(), QUIET_END, SHANGHAI)
    return clock


def deferred_reference(family, clock):
    deadline = align_reference(family, clock)
    return deadline if deadline > clock else None
