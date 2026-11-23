"""Receipt-bounded native auction frames; GET never fetches or schedules."""
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from app.evidence.common import content_hash
from app.evidence.models import EvidenceVersion as Evidence
from app.market import read_facets
from app.market.auction_down import AuctionDownDay, AuctionDownRow, STAGES
from app.market.fuyao_auction import DATASET, FAMILY, UNITS, signed
from app.market.protocol import SHANGHAI, SourceFailure


def money(value):
    text=format(value,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def eligible(row, day, stage):
    try: received=datetime.fromisoformat(row.get('received_at',''))
    except (ValueError,TypeError): return False
    if received.utcoffset() is None or received.astimezone(SHANGHAI).date()!=day: return False
    final=row.get('auction_phase') in {'final','closed'} and row.get('data_status') in {'ready','final'}
    start=datetime.combine(day,time.fromisoformat(stage),SHANGHAI)
    if stage=='09:25': return final and received>=start
    return (row.get('auction_phase') in {'live','auction'} and row.get('data_status') in {'live','ready'}
            and start<=received<=start+timedelta(seconds=90))


def seal(row, restriction, side):
    if not row: return None
    try:
        price,quantity=signed(row.get('price')),signed(row.get('unmatched_shares'))
        bound=signed(restriction.get('up_limit' if side=='up' else 'down_limit'))
    except SourceFailure: return None
    if price is None or quantity is None or bound is None or bound<=0: return None
    # A fully returned, valid auction row away from the limit has no seal on
    # that side. A missing row remains unknown, never zero.
    return abs(price*quantity) if price==bound and (quantity>0 if side=='up' else quantity<0) else signed('0')


class NativeAuction:
    def __init__(self,reader):
        self.reader,self.by_day,self.cached=reader,{},{}
        if FAMILY not in reader.permitted: return
        query=read_facets.with_facets(reader.query({FAMILY},'reference')).where(read_facets.value('dataset')==DATASET)
        query=query.with_only_columns(Evidence.id,Evidence.source_id,Evidence.source_key,Evidence.received_at,
            Evidence.content_hash,read_facets.observed_day()).order_by(Evidence.received_at.desc(),Evidence.id.desc()).limit(4096)
        for ident,source,key,received,digest,raw_day in reader.db.execute(query):
            try: day=datetime.fromisoformat(raw_day).date()
            except (ValueError,TypeError): continue
            if key!='reference:fuyao-auction:'+str(day) or day>reader.cutoff.astimezone(SHANGHAI).date(): continue
            self.by_day.setdefault(day,[]).append(SimpleNamespace(id=ident,source_id=source,source_key=key,
                received_at=received,content_hash=digest,family=FAMILY))

    def data(self,day):
        if day in self.cached: return self.cached[day]
        self.cached[day]=None
        records=self.by_day.get(day,[])
        if not records: return None
        selected={records[0].id:records[0]}
        # At most two earliest frames for each node plus the latest frame.
        # Per-row receipts handle a request set crossing a stage boundary.
        for stage in STAGES:
            start=datetime.combine(day,time.fromisoformat(stage),SHANGHAI)
            end=start+timedelta(seconds=140) if stage!='09:25' else None
            candidates=[m for m in reversed(records) if m.received_at>=start and (end is None or m.received_at<=end)]
            for meta in candidates[:2]: selected[meta.id]=meta
        frames=[]
        for meta in sorted(selected.values(),key=lambda m:m.received_at):
            payload=self.reader.evidence(meta).payload
            columns={c['key']:c.get('unit') for c in payload.get('columns',[])}
            if any(columns.get(k)!=v for k,v in UNITS.items()): continue
            raw=payload.get('rows',[])
            if not raw or len(raw)>8000: continue
            header=raw[0]
            if header.get('row_type')!='coverage': continue
            rows={r['ts_code']:r for r in raw[1:] if r.get('row_type')=='stock' and r.get('ts_code')}
            if len(rows)!=len(raw)-1: continue
            if any(datetime.fromisoformat(r['received_at'])>meta.received_at for r in rows.values()): continue
            frames.append((meta,header,rows))
        if not frames: return None
        limits={r['ts_code']:r for r,_,_ in self.reader.table('tushare_market_limits',day)}
        if not limits: return None
        latest=max(frames,key=lambda item:item[0].received_at)
        points={stage:{} for stage in STAGES}
        for _,_,rows in frames:
            for stage in STAGES:
                for code,row in rows.items():
                    if eligible(row,day,stage): points[stage].setdefault(code,row)
        local=self.reader.cutoff.astimezone(SHANGHAI)
        live_context=local.date()==day and time(9,15)<=local.time()<time(9,25)
        fresh=0<=(self.reader.cutoff-latest[0].received_at).total_seconds()<=120
        result=SimpleNamespace(frames=frames,current=latest[2] if not live_context or fresh else {},
            header=latest[1],received_at=latest[0].received_at,limits=limits,points=points,
            live_context=live_context,live=live_context and fresh,stale=live_context and not fresh,
            evidence_ids=[str(m.id) for m,_,_ in frames])
        self.cached[day]=result
        return result

    def batch(self,day,side='up'):
        data=self.data(day)
        if data is None: return None
        codes={code for frame in [data.current,*data.points.values()] for code,row in frame.items()
               if (value:=seal(row,data.limits.get(code,{}),side)) is not None and value>0}
        rows=[]
        for code in sorted(codes):
            raw=data.current.get(code) or next((items[code] for items in reversed(list(data.points.values())) if code in items),{})
            row=dict(code=code[:6],name=raw.get('name') or code,tags_raw='',change_pct=raw.get('pct_change'))
            for stage in STAGES:
                value=seal(data.points[stage].get(code),data.limits.get(code,{}),side)
                row['fd_'+stage.replace(':','')+'_cny']=money(value) if value is not None else None
            current=seal(data.current.get(code),data.limits.get(code,{}),side)
            row['current_seal_cny']=money(current) if current is not None else None
            rows.append(row)
        header=dict(trade_day=str(day),table_kind='current',row_count=str(len(rows)),
            batch_hash=content_hash(data.evidence_ids),header_total_cny=None if data.stale else money(sum(
                (seal(row,data.limits.get(code,{}),side) or 0) for code,row in data.current.items())))
        stages=set()
        for stage,items in data.points.items():
            if items:
                stages.add(stage)
                header['total_fd_'+stage.replace(':','')+'_cny']=money(sum((seal(row,data.limits.get(code,{}),side) or 0) for code,row in items.items()))
        for row in rows: row.update(trade_day=str(day),batch_hash=header['batch_hash'])
        return SimpleNamespace(header=header,rows=rows,family=FAMILY,received_at=data.received_at,
            stages=stages,source='同花顺竞价',live=data.live,evidence_ids=data.evidence_ids)

    def day(self,day,side):
        batch=self.batch(day,side);data=self.data(day)
        if batch is None: return None
        coverage=int(data.header['usable']);expected=int(data.header['expected'])
        rows=[]
        for raw in batch.rows:
            code=raw['code']+('.SH' if raw['code'].startswith('6') else '.BJ' if raw['code'].startswith(('4','8','920')) else '.SZ')
            amounts={stage:float(raw['fd_'+stage.replace(':','')+'_cny']) if raw.get('fd_'+stage.replace(':','')+'_cny') is not None else None for stage in STAGES}
            current=float(raw['current_seal_cny']) if raw.get('current_seal_cny') is not None else None
            rows.append(AuctionDownRow(code=code,name=raw['name'],node_amounts=amounts,
                node_times={s:None for s in STAGES},current_seal_cny=current,pct_change=float(raw['change_pct']),
                price_basis='auction_indicative'))
        return AuctionDownDay(day=day,rows=rows,source_name='同花顺竞价',received_at=data.received_at.isoformat(),
            seal_basis='原生竞价未匹配量：正买负卖，手×100×竞价价；按当日原生限制价识别。时刻为接收时点，交易所时刻未知。',
            live=data.live,book_state='stale' if data.stale else 'partial' if coverage<expected else 'available',
            book_coverage=coverage,book_expected=expected,evidence_ids=data.evidence_ids,
            seal_coverage=sum(any(v is not None for v in row.node_amounts.values()) for row in rows),
            stage_coverage={s:sum(row.node_amounts[s] is not None for row in rows) for s in STAGES})
