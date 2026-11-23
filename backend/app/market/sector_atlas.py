"""Source-separated, cutoff-safe atlas over archived evidence only."""
from sqlalchemy import case, select
from app.evidence.common import now
from app.evidence.models import EvidenceVersion
from app.market.direction_read import decimal, panel, snapshot_panel
from app.market.kpl_atlas_source import DATASET as KPL_DATASET
from app.market.review_read import _context
from app.market.review_store import permitted_provider


def _tables(db, family, dataset, days, cutoff, *, dated=False):
    provider = permitted_provider(db, family, cutoff)
    if provider is None:
        return None
    key = (EvidenceVersion.payload['rows'][0]['trade_date'].astext if dated
           else EvidenceVersion.payload['observed_day'].astext)
    values = [d.strftime('%Y%m%d') if dated else d.isoformat() for d in days]
    query = select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference', EvidenceVersion.visibility=='shared',
        EvidenceVersion.owner_id.is_(None), EvidenceVersion.received_at<=(cutoff or now(db)),
        EvidenceVersion.payload['dataset'].astext==dataset, key.in_(values))
    order = [key]
    if dataset == KPL_DATASET:
        order.append(case((EvidenceVersion.payload['rows'][0]['archive_kind'].astext=='daily',0),else_=1))
    query = query.distinct(key).order_by(*order,EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc())
    return {(r.payload['rows'][0]['trade_date'] if dated else r.payload['observed_day']):r for r in db.scalars(query)}


def _directory(db, cutoff):
    provider = permitted_provider(db,'tushare_ths_index',cutoff)
    if provider is None:
        return None
    return db.scalar(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at<=(cutoff or now(db)),
        EvidenceVersion.payload['dataset'].astext=='tushare.ths_index').order_by(
            EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(1))


def _leaders(db, days, cutoff):
    from app.market.kpl_leaders import DATASET
    provider=permitted_provider(db,'direction_kpl_strength',cutoff)
    if provider is None:
        return {}
    day=EvidenceVersion.payload['observed_day'].astext
    code=EvidenceVersion.payload['rows'][0]['plate_code'].astext
    rows=db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id==provider.id,
        EvidenceVersion.kind=='reference',EvidenceVersion.visibility=='shared',EvidenceVersion.owner_id.is_(None),
        EvidenceVersion.received_at<=(cutoff or now(db)),EvidenceVersion.payload['dataset'].astext==DATASET,
        day.in_([d.isoformat() for d in days])).distinct(day,code).order_by(day,code,
            case((EvidenceVersion.payload['rows'][0]['archive_kind'].astext=='daily',0),else_=1),
            case((EvidenceVersion.payload['rows'][0]['coverage'].astext=='complete',0),else_=1),
            EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()))
    result={}
    for saved in rows:
        values=saved.payload.get('rows',[])
        if not values:
            continue
        result.setdefault(saved.payload['observed_day'],{})[values[0]['plate_code']]={
            'rows':[{k:r[k] for k in ('stock_code','name','label','rank')} for r in values if r.get('label')],
            'coverage':values[0]['coverage'],'archive_kind':values[0]['archive_kind'],
            'received_at':saved.received_at.isoformat(),'evidence_id':str(saved.id)}
    return result


def kpl_panel(saved, day, codes):
    rows = saved.payload.get('rows',[])
    if not rows or len({r.get('code') for r in rows})!=len(rows) or any(r.get('source_day')!=day.isoformat()
            or r.get('coverage')!='complete_pages' or r.get('total_count')!=str(len(rows))
            or r.get('rank')!=str(i+1) for i,r in enumerate(rows)):
        return panel(day,'开盘啦',status='error',reason='完整榜单的日期、分页或身份校验失败')
    normalized = [{**r,'rank':int(r['rank'])} for r in rows]
    result = panel(day,'开盘啦 · 题材强度',actual=day,status='ready',rows=normalized[:10],
        received_at=saved.received_at.isoformat(),total_count=len(rows))
    result.update(details=[r for r in normalized if not codes or r['code'] in codes],
        coverage='complete_pages',top_ten_complete=len(normalized)>=10,archive_kind=rows[0]['archive_kind'],evidence_id=str(saved.id),
        amount_unit='元')
    return result


def ths_panel(saved, directory, day, codes):
    label='同花顺 · 已购兼容通道日行情'
    if saved is None or directory is None:
        return panel(day,label,status='missing',reason='该日行情或同花顺板块目录尚未归档')
    catalog = {}
    for row in directory.payload.get('rows',[]):
        if row.get('type') not in {'N',None,''} or row.get('exchange') not in {'A',None,''}:
            continue
        if row.get('list_date') and row['list_date'] > day.strftime('%Y%m%d'):
            continue
        code = row.get('ts_code')
        if not code or code in catalog:
            return panel(day,label,status='error',reason='板块目录身份缺失或重复')
        catalog[code]=row.get('name')
    ranked, seen, missing = [], set(), 0
    for row in saved.payload.get('rows',[]):
        code=row.get('ts_code')
        if code in seen or row.get('trade_date')!=day.strftime('%Y%m%d'):
            return panel(day,label,status='error',reason='行情日期或板块身份不一致')
        seen.add(code)
        if code not in catalog:
            continue
        pct=decimal(row.get('pct_change'))
        if pct is None or not catalog[code]:
            missing+=1
            continue
        ranked.append(dict(code=code,name=catalog[code],pct_change=str(pct)))
    ranked.sort(key=lambda r:(-decimal(r['pct_change']),r['code']))
    for i,row in enumerate(ranked):
        row['rank']=i+1
    absent=len(set(catalog)-seen)
    result=panel(day,label,actual=day,status='partial' if missing or absent else 'ready' if ranked else 'missing',
        rows=ranked[:10],received_at=saved.received_at.isoformat(),total_count=len(ranked))
    result.update(details=[r for r in ranked if not codes or r['code'] in codes],
        coverage='dated_concept_catalog',top_ten_complete=not missing and not absent and len(ranked)>=10,
        archive_kind='daily',evidence_id=str(saved.id),
        directory_received_at=directory.received_at.isoformat(),directory_evidence_id=str(directory.id),
        missing_value_count=missing,missing_catalog_count=absent,reason='按已保存同花顺概念目录匹配日行情；同涨幅按来源代码排序。目录不代表历史全量成员。')
    return result


def sector_atlas(db, trade_day=None, lookback=20, source='kpl', as_of_time=None, selected_codes=None):
    context, days, selected = _context(db,trade_day,lookback,as_of_time)
    days=list(reversed(days if selected else []))
    codes=set(filter(None,(selected_codes or '').split(',')))
    records=[]
    if days:
        tables=_tables(db,'direction_kpl_strength' if source=='kpl' else 'tushare_ths_daily',
            KPL_DATASET if source=='kpl' else 'tushare.ths_daily',days,as_of_time,dated=source=='ths')
        directory=_directory(db,as_of_time) if source=='ths' else None
        leaders=_leaders(db,days,as_of_time) if source=='kpl' else {}
        for day in days:
            if tables is None:
                value=panel(day,'开盘啦' if source=='kpl' else '同花顺',status='disabled',reason='来源尚未启用或展示权限未核实')
            else:
                saved=tables.get(day.isoformat() if source=='kpl' else day.strftime('%Y%m%d'))
                if source=='ths':
                    value=ths_panel(saved,directory,day,codes)
                elif saved is not None:
                    value=kpl_panel(saved,day,codes)
                else:
                    value=snapshot_panel(db,'direction_kpl_strength','kpl_strength','开盘啦题材强度',day,as_of_time,strength=True)
                    value.update(coverage='legacy_top_ten',details=[],archive_kind='unknown',reason='仅有旧前十归档；未上榜强度、量能及日终状态不能据此判断')
            if source=='kpl':
                value['leaders']=leaders.get(day.isoformat(),{})
            records.append(value)
    coverage={'requested_days':lookback,'calendar_days':len(days),
        'ranking_days':sum(bool(r['rows']) for r in records),
        'full_days':sum(r.get('status')=='ready' and r.get('coverage') in {'complete_pages','dated_concept_catalog'} for r in records),
        'partial_days':sum(r.get('status')=='partial' for r in records),
        'missing_catalog_observations':sum(r.get('missing_catalog_count',0) for r in records),
        'missing_value_observations':sum(r.get('missing_value_count',0) for r in records),
        'daily_days':sum(r.get('archive_kind')=='daily' for r in records)}
    return {**context,'source':source,'lookback':lookback,'days':records,'coverage':coverage,
        'state':'partial' if records else 'unavailable',
        'limitations':(['开盘啦按ZSType=7题材范围、原始强度降序；当前分页与历史分页独立校验，旧前十不冒充完整榜单。',
            '量能取开盘啦原始板块成交额（元）；领涨使用来源原始龙一至龙五标记。仅已采板块有领涨资料，覆盖不足不解释为当日无领涨。',
            '与短线侠的板块范围和取样时点差异仍在核对；未取得对方完整计算说明。'] if source=='kpl' else
            ['同花顺日行情来自现有已购兼容通道；这是盘后数据，不能代表实时涨幅。',
             '只匹配已保存同花顺概念目录的原始代码，未匹配指数不改名填充；同涨幅按来源代码排序。']) +
            ['缺失与未上榜分开；历史截止同时约束行情和目录接收时刻，补采不会进入更早的历史时点。']}
