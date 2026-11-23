"""Historical synthetic fixture for disposable tests only; no HTTP/production clock hook."""
import json
import os
import re
import sys
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import null, select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import CalendarVersion, EvidenceVersion, Snapshot, SnapshotItem, TradingSession
from app.models import InstanceState, Provider, User
from app.research import schemas as rs, service as research
from app.research.models import DocumentRevision, ResearchDocument


def seed(db, owner, *, auction=True, continuous=True, validation_quality='partial', validation_future_event=False, suffix=''):
    if os.getenv('JL_APP_MODE') != 'demo' or not re.fullmatch(r'jlpm-check-[a-f0-9]{10}(?:-[a-z0-9-]+)?', os.getenv('JL_TEST_PROJECT', '')):
        raise RuntimeError('Refusing historical seeding outside an explicit disposable test project')
    assert db.get(InstanceState, 1).app_mode == 'demo'
    base = datetime(2001, 12, 31, 8, tzinfo=UTC)
    checkpoint = datetime(2002, 1, 2, 1, 25, tzinfo=UTC)
    key = 'synthetic-auction-' + uuid.uuid4().hex + suffix
    provider = db.scalar(select(Provider).where(Provider.kind == 'manual'))
    def material(when, label, *, event=None):
        payload = {'text': '合成竞价材料：'+label+'，不是实际市场数据。', 'source_url': None}
        row = EvidenceVersion(source_id=provider.id, owner_id=owner.id, visibility='private', source_key=key+'-'+label,
            source_revision='1', version_number=1, kind='manual', instrument_id=None, event_at=event or when, published_at=when,
            received_at=when, available_at=when, availability='known', provenance='manual', historical_eligible=True,
            rights_basis='自制隔离测试', payload=payload, content_hash=content_hash(payload), supersedes_id=None)
        db.add(row); db.flush(); return row
    old = material(base, '当时材料'); late = material(checkpoint+timedelta(minutes=5), '迟到材料')
    def freeze(when, rows, quality='partial'):
        items = [{'evidence_version_id':str(row.id),'content_hash':row.content_hash} for row in rows]
        manifest = normalized({'format':'tianyan.snapshot.v1', 'app_mode':'demo','owner_id':owner.id,'as_of_time':when,
            'knowledge_mode':'observed','required_capabilities':[],'source_keys':None,'sources':[],
            'quality':{'status':quality,'reasons':['隔离合成历史fixture，不是真实行情或正式日历']},'items':items})
        row = Snapshot(owner_id=owner.id, as_of_time=when, recorded_at=when+timedelta(seconds=1), knowledge_mode='observed',
            state='pending', selection=manifest, manifest=null(), manifest_hash=None, error_code=None)
        db.add(row); db.flush()
        for item in rows: db.add(SnapshotItem(snapshot_id=row.id,evidence_version_id=item.id,content_hash=item.content_hash))
        db.flush(); row.state='ready'; row.manifest=manifest; row.manifest_hash=content_hash(manifest); db.flush(); return row
    prior = freeze(base+timedelta(minutes=1), [old])
    fresh = freeze(checkpoint+timedelta(minutes=1), [old])
    later = freeze(checkpoint+timedelta(minutes=6), [old,late])
    validation_material = material(checkpoint, '未来事件', event=checkpoint+timedelta(days=1)) if validation_future_event else late
    post_close = freeze(checkpoint+timedelta(hours=6), [old,validation_material], validation_quality)
    segments = ([{'start':'09:15','end':'09:25','kind':'auction'}] if auction else []) + ([{'start':'09:30','end':'11:30','kind':'continuous'}] if continuous else [])
    cal = CalendarVersion(exchange='SSE',version=key,source_ref='合成跨年日历，不是实际市场日程',rights_basis='自制测试',
        coverage_start=date(2001,12,31),coverage_end=date(2002,1,2),recorded_at=base,author_id=owner.id,content_hash=content_hash(segments))
    db.add(cal); db.flush()
    for day,opened in [(date(2001,12,31),True),(date(2002,1,1),False),(date(2002,1,2),True)]:
        db.add(TradingSession(calendar_id=cal.id,trade_day=day,is_open=opened,segments=segments if opened else []))
    db.flush()
    original_clock = research.now
    tick = base+timedelta(hours=1)
    def clock(_db):
        nonlocal tick
        tick += timedelta(seconds=1); return tick
    research.now = clock
    documents=[]
    try:
        for kind in ['review','direction','plan']:
            common={'limitations':['隔离测试，不是行情或交易意见']}
            if kind=='review': content={'type':kind,'summary':'合成原复盘','evidence_refs':[str(old.id)],**common}
            elif kind=='direction': content={'type':kind,'items':[{'label':'合成方向','judgment':'人工测试','core_instrument_ids':[],'evidence_refs':[str(old.id)]}],**common}
            else: content={'type':kind,'scenarios':[{'label':'合成竞价情景','trigger_text':'人工核查合成材料','invalidation_text':'材料缺失时不可判断','instrument_ids':[],'evidence_refs':[str(old.id)]}],**common}
            body=rs.DocumentInput.model_validate({'kind':kind,'source_trade_day':'2001-12-31','target_trade_day':'2002-01-02' if kind=='plan' else None,
                'revision':{'title':'合成竞价上游-'+kind+'-'+key,'snapshot_id':str(prior.id),'calendar_version_id':str(cal.id),
                'upstream_revision_ids':[str(documents[-1].head_revision_id)] if documents else [],'content':content}})
            doc,_,_=research.create_document(db,owner,body,uuid.uuid4()); research.publish(db,owner,doc.head_revision_id,1,uuid.uuid4()); documents.append(doc)
        plan=research.revision_dto(db,db.get(DocumentRevision,documents[-1].head_revision_id),db.get(ResearchDocument,documents[-1].id))
        # New draft head must not hide old published P1 from the UI picker.
        p2,_,_=research.create_revision(db,owner,documents[-1].id,rs.RevisionInput.model_validate({**body.revision.model_dump(),'title':'合成竞价预案新草稿-'+key}),1,uuid.uuid4())
    finally:
        research.now=original_clock
    return normalized({'plan_id':documents[-1].id,'revision_id':plan.id,'scenario_id':plan.scenarios[0].id,'new_head_id':p2.id,
        'snapshot_id':fresh.id,'late_snapshot_id':later.id,'prior_snapshot_id':prior.id,'validation_snapshot_id':post_close.id,'validation_material_id':validation_material.id,'material_id':old.id,'late_material_id':late.id,
        'calendar_id':cal.id,'key':key,'source_day':'2001-12-31','target_day':'2002-01-02','observed_at':checkpoint})


if __name__=='__main__':
    username=sys.stdin.readline().strip()
    with Session(engine()) as db, db.begin():
        owner=db.scalar(select(User).where(User.username==username))
        assert owner is not None
        print(json.dumps(seed(db,owner),ensure_ascii=False))
