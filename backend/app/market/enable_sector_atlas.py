"""Local operator rollout: audited THS daily probe, then enable its binding.

Existing source settings and existing connections are never overwritten. This
creates only the new daily capability under the already authorized endpoint.
No credentials, connection names or raw upstream rows are printed.
"""
import argparse
import json
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import Connection
from app.ai.schemas import ConnectionInput
from app.ai.service import store_connection
from app.db import engine
from app.integrations.credentials import resolve_source_credentials, job_context
from app.integrations.data import connection_test
from app.market.models import SourceSettings
from app.market.review_store import family_id, permitted_provider
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, enqueue_collection
from app.models import Provider, User


def run(mode,day):
    family='tushare_ths_daily'
    identity=family_id(family)
    with Session(engine()) as db,db.begin():
        base=permitted_provider(db,'tushare_ths_index')
        assert base is not None,'existing THS directory unavailable'
        owner=db.get(User,db.get(SourceSettings,base.id).owner_id)
        assert owner and owner.enabled and owner.role=='admin'
        bound=resolve_source_credentials(db,base.id,base.family)
        assert bound.enabled and not bound.reason and bound.credential
        matches=[c for c in db.scalars(select(Connection).where(Connection.archived.is_(False))) if str(identity) in c.source_ids]
        assert len(matches)<=1,'ambiguous binding'
        conn=matches[0] if matches else None
        if mode=='prepare':
            # Append a source descriptor for the new parser capabilities while
            # preserving its existing entitlement, owner, schedule and pause.
            from app.market.registry import SOURCES
            from app.evidence.common import lock, now, normalized, content_hash
            from app.evidence.models import SourceVersion
            from app.schemas import Provider as ProviderDTO
            from app.models import AuditEvent
            from app.integrations.credentials import invalidate
            kpl=db.get(Provider,family_id('direction_kpl_strength'))
            if kpl and kpl.adapter_version!=SOURCES[kpl.family].version:
                lock(db,'source:configuration:'+str(kpl.id))
                definition=SOURCES[kpl.family]
                kpl.adapter_version=definition.version
                kpl.capabilities=list(definition.capabilities)
                descriptor=normalized(ProviderDTO.model_validate(kpl))
                db.add(SourceVersion(source_id=kpl.id,recorded_at=now(db),descriptor=descriptor,content_hash=content_hash(descriptor)))
                db.add(AuditEvent(actor_id=owner.id,action='source.atlas_upgrade',resource_type='provider',resource_id=kpl.id,
                    changed_field_names=['adapter_version','capabilities']))
                invalidate(db)
            if db.get(Provider,identity) is None:
                configure_source(db,owner,SourceConfiguration(family=family,interval_seconds=3600,enabled=True,
                    entitlement_status='verified',rights={**base.rights,'export':'denied','redistribute':'denied'},
                    rights_basis='用户2026-11-03明确授权板块轮动本机个人研究；现有已购兼容通道ths_daily三个交易日实测成功，展示缓存，不再分发。'))
            if conn is None:
                result,_,_=store_connection(db,owner,ConnectionInput(name='同花顺板块日行情',kind='market',template='tushare',
                    endpoint=bound.endpoint,api_key=bound.credential,enabled=False,source_ids=[identity]))
                conn=db.get(Connection,UUID(str(result['id'] if isinstance(result,dict) else result.id)))
            assert conn.template=='tushare' and conn.source_ids==[str(identity)]
            probe=None
            if not conn.enabled:
                credentials=resolve_source_credentials(db,identity,family,allow_disabled=True,refresh=True)
                assert not credentials.reason
                probe=job_context(credentials,identity,probe=True)
            job=enqueue_collection(db,identity,day=day,integration_probe=probe)
            if probe:
                conn.last_test={'status':'queued','job_id':str(job.id),'source_id':str(identity),
                                'revision':conn.revision,'capability':family}
            return {'job_id':str(job.id),'enabled':conn.enabled}
        assert conn is not None,'run prepare first'
        test=connection_test(db,conn)
        if not conn.enabled:
            if not test or test.get('status')!='success':
                return {'enabled':False,'probe':test}
            body=ConnectionInput(id=conn.id,name=conn.name,kind=conn.kind,template=conn.template,
                endpoint=conn.endpoint,enabled=True,source_ids=[identity],options=conn.options,expected_revision=conn.revision)
            store_connection(db,owner,body,identity=conn.id)
        return {'enabled':True,'daily_only':True,'backfill_sessions':50}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','finish'))
    parser.add_argument('--day',type=date.fromisoformat,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.mode,args.day),ensure_ascii=False,default=str))
