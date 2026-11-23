"""New review tables cannot replace or duplicate ordinary workbench inputs."""
from datetime import UTC, datetime

from sqlalchemy.orm import Session
from test_review_collection import day, page, setup_source

from app.db import engine
from app.evidence.schemas import ReferencePayload
from app.market.collect import store_observation
from app.market.datasets import current_rows
from app.market.review_store import persist_pages
from app.market.terminal_read import SavedTables
from app.market.workbench_v2 import table_metadata


def test_review_namespace_does_not_enter_normal_workbench_tables():
    _, provider, config, job = setup_source('pools')
    row = {'trade_date': day().strftime('%Y%m%d'), 'ts_code': '600000.SH', 'limit': 'U',
           'limit_times': '1', 'fd_amount': '123', 'amount': '1000'}
    stamp = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        payload = ReferencePayload(dataset='tushare.limit_list_d', title='Synthetic normal input',
                                   source_url='https://tushare.pro/', attribution='Synthetic test only',
                                   scope='Synthetic original live selection',
                                   columns=[{'key': k, 'label': k} for k in row], rows=[row], observed_day=day())
        store_observation(db, provider, config, source_key='normal-pool', kind='reference',
                          payload=payload, received_at=stamp, eligible=False)
        normal_before = [m.source_key for m in SavedTables(db, datetime.now(UTC)).reference_meta]
        assert normal_before == ['normal-pool']
        persist_pages(db, provider, config, job, [(page([row], dataset='pools'), datetime.now(UTC))])
        assert [m.source_key for m in SavedTables(db, datetime.now(UTC)).reference_meta] == normal_before
        assert [m.source_key for m in table_metadata(db, {provider.family: provider}, datetime.now(UTC))] == normal_before
        assert [m.source_key for m, _ in current_rows(db, 'reference', family=provider.family)] == normal_before
        explicit = current_rows(db, 'reference', family=provider.family, dataset='review.pools')
        assert len(explicit) == 1 and explicit[0][0].payload['dataset'] == 'review.pools'
