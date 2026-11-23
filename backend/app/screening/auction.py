"""Auction matched-trade screening; independent of continuous-session quotes."""
from collections import Counter
from datetime import timedelta

from sqlalchemy import or_, select

from app.auth import APIError
from app.evidence.common import content_hash, now
from app.evidence.models import EvidenceVersion
from app.market.protocol import SHANGHAI
from app.market.review_analysis import ArchiveReader
from app.market.review_read import _dated_rows, _number, _number_text
from app.market.review_store import available_trade_days, permitted_provider
from app.market.session import market_session
from app.market.visibility import instrument_visible_sql
from app.market.watchlist import watchlist
from app.models import Instrument


def auction_context(db, owner_id, body):
    clock = now(db)
    cutoff = body.as_of_time or clock
    if cutoff > clock:
        raise APIError(422, 'future_cutoff', '研究截止不能晚于服务器当前时间。')
    day = body.day or cutoff.astimezone(SHANGHAI).date()
    if day > cutoff.astimezone(SHANGHAI).date():
        raise APIError(422, 'future_day', '竞价日期不能晚于研究截止日期。')
    days = available_trade_days(db, limit=3, end_day=day, as_of_time=cutoff)
    if day not in days and not (day == cutoff.astimezone(SHANGHAI).date()
                               and market_session(db, cutoff).is_trading_day is True):
        raise APIError(422, 'calendar_not_ready', '该日尚无可核实的交易日历。')
    previous_days = available_trade_days(db, limit=1, end_day=day-timedelta(days=1), as_of_time=cutoff)
    previous = previous_days[-1] if previous_days else None
    reader = ArchiveReader(db, cutoff)
    pool = reader.get('pools', previous)
    lhb = reader.get('lhb', previous)
    pools, listings = _dated_rows(pool, previous), _dated_rows(lhb, previous)
    grouped = {}
    for row in pools or []:
        grouped.setdefault(row.get('ts_code'), []).append(row)
    listed = {row.get('ts_code') for row in listings or []}
    provider = permitted_provider(db, 'tushare_stk_auction', cutoff)
    saved = db.scalar(select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.kind == 'reference',
        EvidenceVersion.visibility == 'shared', EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.provenance == 'source_reported', EvidenceVersion.received_at <= cutoff,
        EvidenceVersion.payload['dataset'].astext == 'tushare.stk_auction',
        EvidenceVersion.payload['rows'][0]['trade_date'].astext == day.strftime('%Y%m%d'),
    ).order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.id.desc()).limit(1)) if provider else None
    source_rows = saved.payload.get('rows', []) if saved else []
    if any(row.get('trade_date') != day.strftime('%Y%m%d') for row in source_rows):
        raise APIError(422, 'source_date_mismatch', '竞价表包含不一致的日期，未参与筛选。')
    counts = Counter(row.get('ts_code') for row in source_rows)
    instruments = {}
    for item in db.scalars(select(Instrument).where(Instrument.asset_type=='stock',
            Instrument.first_received_at <= cutoff,
            or_(Instrument.valid_from.is_(None), Instrument.valid_from <= day),
            or_(Instrument.valid_to.is_(None), Instrument.valid_to > day),
            instrument_visible_sql())):
        code = item.symbol + '.' + {'SSE':'SH','SZSE':'SZ','BSE':'BJ'}[item.exchange]
        instruments[code] = item
    watched = {str(w.instrument.id) for w in watchlist(db, owner_id).items} if body.scope=='watchlist' else None
    columns = {c['key']: c.get('unit') for c in saved.payload.get('columns', [])} if saved else {}
    rows = []
    for raw in source_rows:
        code = raw.get('ts_code')
        if counts[code] != 1 or not isinstance(code, str) or not code.endswith(('.SH','.SZ','.BJ')):
            continue
        instrument = instruments.get(code)
        if watched is not None and (instrument is None or str(instrument.id) not in watched):
            continue
        before = grouped.get(code, [])
        unique = before[0] if len(before)==1 else None
        price = _number(raw.get('price'), positive=True) if columns.get('price')=='元' else None
        close = _number(raw.get('pre_close'), positive=True) if columns.get('pre_close')=='元' else None
        values = {}
        for field, wire, unit in [('amount','auction_amount_cny','元'),('turnover_rate','auction_turnover_rate','%'),('volume_ratio','auction_volume_ratio','倍')]:
            value = _number(raw.get(field)) if columns.get(field)==unit else None
            values[wire] = _number_text(value) if value is not None and value>=0 else None
        height = _number(unique.get('limit_times'), positive=True) if unique and unique.get('limit')=='U' else None
        name = (unique or {}).get('name') or code if body.as_of_time else instrument.name if instrument else (unique or {}).get('name') or code
        rows.append(dict(code=code, name=name,
            instrument_id=str(instrument.id) if instrument else None, source_time=None,
            received_at=saved.received_at.isoformat(), **values,
            auction_pct=_number_text((price/close-1)*100) if price is not None and close else None,
            previous_height=_number_text(height) if height is not None and height==int(height) else None,
            yesterday_up=None if pools is None or len(before)>1 else '1' if unique and unique.get('limit')=='U' else '0',
            yesterday_broken=None if pools is None or len(before)>1 else '1' if unique and unique.get('limit')=='Z' else '0',
            yesterday_lhb=None if listings is None else '1' if code in listed else '0'))
    refs = [dict(evidence_id=str(saved.id),source_id=str(saved.source_id),content_hash=saved.content_hash,received_at=saved.received_at.isoformat())] if saved else []
    for table in reader.cache.values():
        refs.extend(dict(evidence_id=r['id'], source_id=r['source_id'], content_hash=r['content_hash'],received_at=r['received_at']) for r in table.get('evidence_refs', []))
    return dict(rows=rows, source_refs=refs, trade_day=day, as_of_time=cutoff,
        data_revision=content_hash(refs), mode='observed' if body.as_of_time else 'review', created_at=clock,
        screen_context='auction', coverage=dict(source_rows=len(source_rows), usable_rows=len(rows),
            source_state='complete' if saved else 'permission_denied' if provider is None else 'missing',
            received_at=saved.received_at.isoformat() if saved else None,
            ambiguous_rows=sum(n for n in counts.values() if n>1), previous_trade_day=str(previous) if previous else None,
            previous_pool_state=pool['state'], previous_lhb_state=lhb['state']),
        reasons=['竞价成交结果；不是09:15/09:20/09:25封单。来源未提供精确成交时间，接收时间另列。',
                 '来源竞价量比按倍展示；竞价换手率独立于盘中换手率。只筛选实际已保存样本，缺失不补零。'])
