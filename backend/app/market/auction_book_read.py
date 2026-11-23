"""Read saved trial books only. Current books and frozen nodes stay distinct."""
from datetime import datetime, time, timedelta

from app.evidence.models import EvidenceVersion
from app.market import read_facets
from app.market.auction_book import DATASET, FAMILY, UNITS, seal
from app.market.auction_down import AuctionDownDay, AuctionDownRow, STAGES
from app.market.protocol import SHANGHAI, SourceFailure
from app.market.models import SourceSettings
from app.market.terminal_read import Meta, number, pct
from app.models import Provider


def clock(row,received_at):
    value=row.get('trade_time')
    try:
        stamp=datetime.fromisoformat(value) if isinstance(value,str) else None
        return stamp if stamp is not None and stamp.utcoffset() is not None and stamp<=received_at+timedelta(seconds=60) else None
    except ValueError:
        return None


def latest_book_day(reader):
    if FAMILY not in reader.permitted: return None
    config=reader.db.get(SourceSettings,reader.permitted[FAMILY].id)
    if config is None or config.config.get('collection_mode')!='full_market': return None
    query=read_facets.with_facets(reader.query({FAMILY},'reference')).where(read_facets.value('dataset')==DATASET)
    value=reader.db.scalar(query.with_only_columns(read_facets.observed_day()).order_by(
        EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))
    try: day=datetime.fromisoformat(value).date() if isinstance(value,str) else None
    except ValueError: return None
    if day is not None and day<=reader.cutoff.astimezone(SHANGHAI).date():
        reader.available_days.add(day)
        return day
    return None


def book_views(reader, day, *, down_codes=()):
    if FAMILY not in reader.permitted:
        return None
    config=reader.db.get(SourceSettings,reader.permitted[FAMILY].id)
    if config is None or config.config.get('collection_mode')!='full_market': return None
    ev = EvidenceVersion
    query = read_facets.with_facets(reader.query({FAMILY}, 'reference')).where(
        read_facets.observed_day()==day.isoformat(), read_facets.value('dataset')==DATASET)
    query = query.with_only_columns(ev.id,ev.source_id,Provider.family,ev.source_key,ev.received_at,ev.content_hash)
    records = reader.db.execute(query.order_by(ev.received_at.desc(),ev.id.desc()).limit(64)).all()
    batches = []
    for record in records:
        meta = Meta(*record,frozenset({day}))
        if meta.source_key!='reference:auction-book:'+day.isoformat(): continue
        payload = reader.evidence(meta).payload
        if len(payload.get('rows',[]))>8001: continue
        columns={c.get('key'):c.get('unit') for c in payload.get('columns',[])}
        if any(columns.get(key)!=value for key,value in UNITS.items()): continue
        header = next((r for r in payload.get('rows',[]) if r.get('row_type')=='coverage'),None)
        if not header or number(header.get('usable_count')) in (None,0): continue
        coverage,expected=number(header.get('usable_count')),number(header.get('universe_count'))
        if expected is None or not 0<coverage<=expected<=8000 or not coverage.is_integer() or not expected.is_integer(): continue
        source_clock=clock(header,meta.received_at)
        if source_clock is None or source_clock.date()!=day or not time(9,15)<=source_clock.time()<=time(9,25): continue
        rows = {r['ts_code']:r for r in payload['rows'] if r.get('row_type')=='stock' and r.get('ts_code')}
        batches.append((meta,header,rows))
    local = reader.cutoff.astimezone(SHANGHAI)
    live_context = day==local.date() and time(9,15)<=local.time()<=time(9,25)
    if not batches:
        empty = AuctionDownDay(day=day,source_name='腾讯公开竞价盘口试采',book_state='waiting' if live_context else 'missing')
        return empty, empty.model_copy(deep=True), live_context
    reader.available_days.add(day)
    meta, header, current = batches[0]
    latest = clock(header,meta.received_at)
    fresh = latest is not None and timedelta(0)<=reader.cutoff-latest<=timedelta(seconds=120)
    live = live_context and fresh
    evidence_ids = [str(m.id) for m,_,_ in batches]
    def project(side):
        result=[]
        # During the live auction an old batch must not masquerade as current.
        candidates=dict(current) if not live_context or live else {}
        if side=='down' and not live_context:
            for _,_,items in batches:
                for code in down_codes:
                    if code in items:
                        try:
                            previous=seal(candidates.get(code,{}),side)
                            value=seal(items[code],side)
                        except SourceFailure: continue
                        if previous is None and value is not None and value>0: candidates[code]=items[code]
        candidates=candidates.items()
        for code, row in candidates:
            try: amount = seal(row,side)
            except SourceFailure: continue
            if amount is None or amount<=0: continue
            stamp = clock(row,meta.received_at)
            if stamp is None or stamp.date()!=day or not time(9,15)<=stamp.time()<=time(9,25) or stamp>reader.cutoff or (live_context and reader.cutoff-stamp>timedelta(seconds=120)): continue
            amounts, clocks = {}, {}
            for stage in STAGES:
                centre=datetime.combine(day,time.fromisoformat(stage),SHANGHAI)
                if centre>reader.cutoff:
                    amounts[stage],clocks[stage]=None,None
                    continue
                start=centre-timedelta(seconds=90) if stage=='09:25' else centre
                end=centre if stage=='09:25' else centre+timedelta(seconds=90)
                options=[]
                for m,_,items in batches:
                    item=items.get(code)
                    if item is None: continue
                    at=clock(item,m.received_at)
                    if at is None or not start<=at<=min(end,reader.cutoff): continue
                    try: value=seal(item,side)
                    except SourceFailure: continue
                    if value is not None: options.append((at,m.received_at,value))
                point=(max if stage=='09:25' else min)(options) if options else None
                amounts[stage]=point[2] if point else None
                clocks[stage]=point[0].isoformat() if point else None
            indicative=row.get('up_limit' if side=='up' else 'down_limit')
            try: current_amount=seal(current.get(code,{}),side)
            except SourceFailure: current_amount=None
            result.append(AuctionDownRow(code=code,name=row.get('name') or code,node_amounts=amounts,node_times=clocks,
                pct_change=pct(indicative,row.get('pre_close')),price_basis='auction_indicative',source_time=stamp.isoformat(),
                current_seal_cny=current_amount,book_time=stamp.isoformat()))
        return AuctionDownDay(day=day,rows=result,source_name='腾讯公开竞价盘口试采',live=live,
            seal_basis=('买一价等于原生涨停价且卖一量为0；买一价×买一量（股）。' if side=='up' else
                        '卖一价等于原生跌停价且买一量为0；卖一价×卖一量（股）。'),
            latest_at=latest.isoformat() if latest else None,received_at=meta.received_at.isoformat(),
            book_state=('stale' if live_context and not fresh else
                        'partial' if coverage < expected else 'available'),book_coverage=int(header['usable_count']),
            book_expected=int(header['universe_count']),evidence_ids=evidence_ids,
            seal_coverage=sum(any(v is not None for v in r.node_amounts.values()) for r in result),
            stage_coverage={stage:sum(r.node_amounts.get(stage) is not None for r in result) for stage in STAGES})
    return project('down'), project('up'), live_context


def attach_book(base, book):
    """Keep the final matched cohort/closing quotes; attach one book source."""
    by_code={r.code:r for r in book.rows}
    for row in base.rows:
        native=by_code.get(row.code)
        if native is not None:
            row.node_amounts=native.node_amounts
            row.node_times=native.node_times
            row.current_seal_cny=native.current_seal_cny
            row.book_time=native.book_time
    base.seal_coverage=sum(any(v is not None for v in r.node_amounts.values()) for r in base.rows)
    base.stage_coverage={stage:sum(r.node_amounts.get(stage) is not None for r in base.rows) for stage in STAGES}
    if book.book_state not in {'missing','waiting'}:
        base.source_name='Tushare竞价成交 / 腾讯竞价盘口'
        for field in ('latest_at','received_at','live','book_state','book_coverage','book_expected'):
            setattr(base,field,getattr(book,field))
        base.evidence_ids=sorted(set(base.evidence_ids+book.evidence_ids))
    return base
