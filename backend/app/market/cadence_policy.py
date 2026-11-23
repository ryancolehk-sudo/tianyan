"""Local polling floors, separate from supplier rights and physical permits.

Lower floors enable bounded trials; they never enable a source, alter its
configured interval, grant points, or override a persisted supplier cooldown.
"""

FAST_FLOORS = {
    'tencent_market_rt': 15,
    'direction_kpl_top': 5,
    'tencent_web': 3,
    'tushare_rt_k': 15,
    'tushare_market_rt': 15,
    'tushare_rt_indices': 15,
    'fuyao_quotes': 15,
    'fuyao_indices': 15,
    'ths_industry_funds': 30,
    'ths_concept_funds': 60,
}

QUOTE_FAMILIES = frozenset({'tencent_web', 'tencent_market_rt', 'tushare_rt_k', 'tushare_market_rt',
    'tushare_rt_indices', 'fuyao_quotes', 'fuyao_indices'})

FAST_FEED_FAMILIES = (QUOTE_FAMILIES-{'tencent_market_rt'}) | {'direction_kpl_top'}


def minimum_interval(family):
    return FAST_FLOORS.get(family, 60)


def effective_interval(family, configured):
    return max(minimum_interval(family), configured)


def automatic_interval(family, configured, clock):
    from datetime import time
    from app.market.protocol import SHANGHAI
    interval=effective_interval(family,configured)
    local=clock.astimezone(SHANGHAI).time()
    if family in QUOTE_FAMILIES:
        if time(11,30)<=local<time(13) or local>=time(15):
            return max(60,interval)
        if family.startswith('tushare_') and local<time(9,30):
            return max(60,interval)
    return interval


def remaining_requests(family, configured, clock):
    """Conservative same-day reserve using the actual automatic policy."""
    from datetime import datetime, time
    from math import ceil
    from app.market.protocol import SHANGHAI
    from app.market.realtime_schedule import windows
    local=clock.astimezone(SHANGHAI)
    total=0
    for start,end in windows(family):
        edges=sorted({start,end,*[point for point in (time(9,30),time(11,30),time(15)) if start<point<end]})
        for opening,closing in zip(edges,edges[1:]):
            first=max(local,datetime.combine(local.date(),opening,SHANGHAI))
            last=datetime.combine(local.date(),closing,SHANGHAI)
            if last>first:
                total+=ceil((last-first).total_seconds()/automatic_interval(family,configured,first))
    return total
