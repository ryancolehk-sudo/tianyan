"""Operator rollout: bind, genuinely probe, then enable the new group archive.

Run only after publishing the reviewed candidate to API and worker:
python -m app.market.enable_group_details prepare --day YYYY-MM-DD
python -m app.market.enable_group_details finish --day YYYY-MM-DD
No tokens, raw market rows or connection names are printed.
"""
import argparse
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import Connection
from app.ai.schemas import ConnectionInput
from app.ai.service import store_connection
from app.db import engine
from app.integrations.credentials import resolve_source_credentials, job_context
from app.integrations.data import connection_test
from app.market.models import SourceSettings
from app.market.review_jobs import queue_dataset
from app.market.review_store import family_id, permitted_provider
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source
from app.models import User


def run(mode, day):
    family = 'tushare_limit_list_ths'
    identity = family_id(family)
    with Session(engine()) as db, db.begin():
        base = permitted_provider(db, 'tushare_limit_list_d')
        assert base is not None, 'existing pool source unavailable'
        owner = db.get(User, db.get(SourceSettings, base.id).owner_id)
        assert owner and owner.enabled and owner.role == 'admin'
        bound = resolve_source_credentials(db, base.id, base.family)
        assert bound.enabled and not bound.reason and bound.credential
        matches = [c for c in db.scalars(select(Connection).where(Connection.archived.is_(False)))
                   if str(identity) in c.source_ids]
        assert len(matches) <= 1, 'ambiguous binding'
        conn = matches[0] if matches else None
        if mode == 'prepare':
            if conn is None:
                configure_source(db, owner, SourceConfiguration(
                    family=family, interval_seconds=86400, enabled=True,
                    entitlement_status='verified',
                    rights={**base.rights, 'export':'denied', 'redistribute':'denied'},
                    rights_basis='用户授权本机个人研究字段接入；2026-11-03已购兼容通道单日实测成功；展示缓存范围沿用已授权通道。'))
                result, _, _ = store_connection(db, owner, ConnectionInput(
                    name='涨停分组明细', kind='market', template='tushare',
                    endpoint=bound.endpoint, api_key=bound.credential,
                    enabled=False, source_ids=[identity]))
                # The new binding preserves every existing connection revision.
                from uuid import UUID
                conn = db.get(Connection, UUID(str(result['id'] if isinstance(result, dict) else result.id)))
            assert conn.template == 'tushare' and conn.source_ids == [str(identity)]
            if not conn.enabled:
                credentials = resolve_source_credentials(db, identity, family, allow_disabled=True, refresh=True)
                assert not credentials.reason
                ctx = job_context(credentials, identity, probe=True)
                job = queue_dataset(db, owner, 'pool_details', day, integration_probe=ctx, force=True)
                conn.last_test = {'status':'queued', 'job_id':str(job.id), 'source_id':str(identity),
                                  'revision':conn.revision, 'capability':family}
            else:
                job = queue_dataset(db, owner, 'pool_details', day)
            basic_job = queue_dataset(db, owner, 'group_basics', day)
            return {'pool_details_job':str(job.id), 'group_basics_job':str(basic_job.id), 'enabled':conn.enabled}
        assert conn is not None, 'run prepare first'
        test = connection_test(db, conn)
        if not conn.enabled:
            if not test or test.get('status') != 'success':
                return {'enabled':False, 'probe':test}
            body = ConnectionInput(id=conn.id, name=conn.name, kind=conn.kind, template=conn.template,
                endpoint=conn.endpoint, enabled=True, source_ids=[identity], options=conn.options,
                expected_revision=conn.revision)
            store_connection(db, owner, body, identity=conn.id)
        # Preserve the probe's archived receipt; later sessions use the regular
        # daily archive scheduler. No receipt is backdated to its trading day.
        from app.market.review_analysis import ArchiveReader, pool_groups
        rows = pool_groups(ArchiveReader(db, None), day)['records']
        keys = ('limit_type','change_reason','board_stat_label','seal_amount_yuan',
                'actual_float_value_yuan','float_market_value_yuan','total_market_value_yuan')
        return {'enabled':True, 'records':len(rows),
                'coverage':{key:sum(r.get(key) is not None for r in rows) for key in keys}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare','finish'))
    parser.add_argument('--day', type=date.fromisoformat, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.mode, args.day), ensure_ascii=False, default=str))
