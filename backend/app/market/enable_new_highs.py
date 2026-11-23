"""Local explicit activation. Does not change any incumbent source or binding."""
import json
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db import engine
from app.ai.models import Connection
from app.ai.schemas import ConnectionInput
from app.ai.service import store_connection
from app.integrations.credentials import resolve_source_credentials, job_context
from app.integrations.data import connection_test
from app.market.new_high_protocol import FAMILY
from app.market.models import SourceSettings
from app.market.review_store import family_id, permitted_provider
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, enqueue_collection
from app.models import Provider, User


def run(mode='prepare'):
    with Session(engine()) as db, db.begin():
        base=permitted_provider(db,'direction_kpl_strength')
        assert base is not None, 'Existing KPL source unavailable'
        owner=db.get(User,db.get(SourceSettings,base.id).owner_id)
        assert owner and owner.enabled and owner.role=='admin'
        ident=family_id(FAMILY)
        if db.get(Provider,ident) is None:
            configure_source(db,owner,SourceConfiguration(family=FAMILY,interval_seconds=86400,enabled=True,
                entitlement_status='verified',rights={**base.rights,'export':'denied','redistribute':'denied'},
                rights_basis='用户2026-11-09要求百日新高原生盘后资料与16:30自动更新；匿名实测，仅本人本机展示缓存，禁止导出及再分发。'))
        bindings=[c for c in db.scalars(select(Connection).where(Connection.archived.is_(False))) if str(ident) in c.source_ids]
        assert len(bindings)<=1
        conn=bindings[0] if bindings else None
        if not conn:
            result,_,_=store_connection(db,owner,ConnectionInput(name='开盘啦百日新高',kind='market',template='kaipanla',
                endpoint='https://apphwhq.longhuvip.com/',enabled=False,source_ids=[ident]))
            conn=db.get(Connection,UUID(str(result['id'] if isinstance(result,dict) else result.id)))
        if mode=='finish':
            test=connection_test(db,conn)
            if not conn.enabled:
                assert test and test.get('status')=='success', 'Real probe has not succeeded'
                store_connection(db,owner,ConnectionInput(id=conn.id,name=conn.name,kind=conn.kind,
                    template=conn.template,endpoint=conn.endpoint,enabled=True,source_ids=[ident],
                    options=conn.options,expected_revision=conn.revision),identity=conn.id)
            return dict(enabled=True,interval_seconds=db.get(SourceSettings,ident).interval_seconds)
        probe=None
        if not conn.enabled:
            bound=resolve_source_credentials(db,ident,FAMILY,allow_disabled=True,refresh=True)
            assert not bound.reason
            probe=job_context(bound,ident,probe=True)
        job=enqueue_collection(db,ident,integration_probe=probe)
        if probe:
            conn.last_test=dict(status='queued',job_id=str(job.id),source_id=str(ident),revision=conn.revision,capability=FAMILY)
        return dict(job_id=str(job.id),interval_seconds=db.get(SourceSettings,ident).interval_seconds)


if __name__=='__main__':
    import sys
    print(json.dumps(run(sys.argv[1] if len(sys.argv)>1 else 'prepare')))
