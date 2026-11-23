"""Enable the user-requested THS concepts using an existing authorized connection."""
import argparse
import json
from datetime import timedelta
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.ai.models import Connection
from app.ai.schemas import ConnectionInput
from app.ai.service import store_connection
from app.integrations.credentials import resolve_source_credentials, job_context
from app.integrations.data import connection_test
from app.market.models import SourceSettings
from app.market.review_store import family_id, permitted_provider
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, enqueue_collection
from app.models import Provider, User


def run(mode):
    family='tushare_ths_member'
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
            if db.get(Provider,identity) is None:
                configure_source(db,owner,SourceConfiguration(family=family,interval_seconds=21600,enabled=True,
                    entitlement_status='verified',rights={**base.rights,'export':'denied','redistribute':'denied'},
                    rights_basis='用户2026-11-04要求同花顺题材分类；沿用本机已购兼容通道，ths_member与分页实测成功，仅本机个人展示缓存。'))
            if conn is None:
                result,_,_=store_connection(db,owner,ConnectionInput(name='同花顺概念成分',kind='market',template='tushare',
                    endpoint=bound.endpoint,api_key=bound.credential,enabled=False,source_ids=[identity]))
                conn=db.get(Connection,UUID(str(result['id'] if isinstance(result,dict) else result.id)))
            assert conn.template=='tushare' and conn.source_ids==[str(identity)]
            probe=None
            if not conn.enabled:
                credentials=resolve_source_credentials(db,identity,family,allow_disabled=True,refresh=True)
                assert not credentials.reason
                probe=job_context(credentials,identity,probe=True)
            job=enqueue_collection(db,identity,integration_probe=probe)
            if probe and job.state=='queued' and job.error_code in {'transport_error','timeout','upstream_unavailable'} and job.attempts<3:
                from app.evidence.common import now
                from app.market.models import DataFetchRun
                from app.models import AuditEvent
                last=db.scalar(select(DataFetchRun.received_at).where(DataFetchRun.job_id==job.id)
                    .order_by(DataFetchRun.received_at.desc()).limit(1))
                if last:
                    job.available_at=min(job.available_at,max(now(db),last+timedelta(seconds=60)))
                    db.add(AuditEvent(actor_id=owner.id,action='source.membership_probe_retry',resource_type='job',resource_id=job.id,changed_field_names=['available_at']))
            if probe:conn.last_test={'status':'queued','job_id':str(job.id),'source_id':str(identity),'revision':conn.revision,'capability':family}
            return {'job_id':str(job.id),'enabled':conn.enabled}
        assert conn is not None,'run prepare first'
        test=connection_test(db,conn)
        if not conn.enabled:
            if not test or test.get('status')!='success':return {'enabled':False,'probe':test}
            store_connection(db,owner,ConnectionInput(id=conn.id,name=conn.name,kind=conn.kind,template=conn.template,
                endpoint=conn.endpoint,enabled=True,source_ids=[identity],options=conn.options,expected_revision=conn.revision),identity=conn.id)
        return {'enabled':True,'interval_seconds':21600,'historical_backfill':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','finish'))
    print(json.dumps(run(parser.parse_args().mode),ensure_ascii=False,default=str))
