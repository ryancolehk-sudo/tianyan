"""Adjacent verified trading-day comparison; absence is never a failed promotion."""
from collections import Counter

from app.market.review_read import _context, _dated_rows, _json, _metadata, _number, _referenced, _references


def compare_pools(previous, current, previous_day, day):
    old, new = _dated_rows(previous, previous_day), _dated_rows(current, day)
    comparable = (old is not None and new is not None and bool(previous.get('source_id'))
                  and previous.get('source_id') == current.get('source_id')
                  and previous.get('scope') == current.get('scope')
                  and bool(previous.get('coverage_scope'))
                  and previous.get('coverage_scope') == current.get('coverage_scope'))
    old_counts = Counter(row.get('ts_code') for row in old or [])
    new_counts = Counter(row.get('ts_code') for row in new or [])
    by_code = {row.get('ts_code'):(i,row) for i,row in enumerate(new or []) if new_counts[row.get('ts_code')]==1}
    records, seen = [], set()
    for i,row in enumerate(old or []):
        if row.get('limit') != 'U' or not str(row.get('ts_code')).endswith(('.SH','.SZ')):
            continue
        code = row.get('ts_code')
        if code in seen: continue
        seen.add(code)
        height = _number(row.get('limit_times'), positive=True)
        candidate = by_code.get(code)
        now = candidate[1] if candidate else None
        next_height = _number(now.get('limit_times'), positive=True) if now else None
        state, reason = 'unknown', '未观测或资料不足'
        if comparable and old_counts[code] == 1 and height is not None and height == int(height) and now:
            if now.get('limit') == 'U' and next_height == height+1:
                state, reason = 'promoted', '相邻交易日同源日榜，板数增加一板'
            elif now.get('limit') in {'D','Z'}:
                state, reason = 'failed', '当日同源日榜明确记录为跌停或炸板'
            else:
                reason = '板数不连续或状态不足，不自动判为掉队'
        records.append(dict(code=code, name=row.get('name') or code, previous_height=height,
            current_height=next_height, state=state, reason=reason,
            previous_evidence=_referenced(row,previous,i).get('row_reference'),
            current_evidence=_referenced(now,current,candidate[0]).get('row_reference') if candidate else None))
    counts = Counter(row['state'] for row in records)
    return dict(comparable=comparable, records=records, candidate_n=len(records),
        promoted_n=counts['promoted'], failed_n=counts['failed'], unknown_n=counts['unknown'],
        promotion_pct=counts['promoted']/len(records)*100 if comparable and records and not counts['unknown'] else None)


def comparison_view(db, trade_day, as_of_time=None):
    from app.market.review_analysis import ArchiveReader
    context, days, day = _context(db,trade_day,20,as_of_time)
    previous_day = days[days.index(day)-1] if day in days and days.index(day)>0 else None
    reader = ArchiveReader(db,as_of_time)
    previous, current = reader.get('pools',previous_day),reader.get('pools',day)
    result = compare_pools(previous,current,previous_day,day)
    events = []
    # These are source-reported first/last seals, not a reconstructed path.
    for i,row in enumerate(_dated_rows(current,day) or []):
        opened = _number(row.get('open_times'))
        if row.get('limit') != 'Z' and not (opened is not None and opened>0): continue
        events.append(dict(code=row.get('ts_code'),name=row.get('name'),state=row.get('limit'),
            open_times=opened, first_seal=row.get('first_time'),last_seal=row.get('last_time'),
            evidence=_referenced(row,current,i).get('row_reference'),
            sequence_note='来源首末封板时刻；未存逐笔炸板回封事件序列，不能还原全过程'))
    return _json({**context, 'kind':'comparison', 'state':'partial', 'records':[],
        'trade_days':[d.isoformat() for d in (previous_day,day) if d],
        'comparison':{**result,'previous_trade_day':previous_day,'trade_day':day,'events':events},
        'coverage':[_metadata(previous),_metadata(current)],'evidence_refs':_references(reader.cache.values()),
        'scope_note':'相邻已核交易日、同源同覆盖范围；缺席记未观测，不以名单缺失推断掉队；不完整分类不计算晋级率'})
