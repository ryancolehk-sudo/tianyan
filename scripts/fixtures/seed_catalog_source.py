"""Import a synthetic cninfo catalogue claim into an explicitly named disposable demo project."""
import json
import os
import re
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.market.collect import store_catalog_batch, store_quote_batch
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import InstrumentKey, parse_cninfo_catalog, parse_tencent_quotes
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, source_id
from app.models import User

project = os.getenv('JL_TEST_PROJECT', '')
if os.getenv('JL_APP_MODE') != 'demo' or not re.fullmatch(r'jlpm-check-[a-f0-9]{10}(-source)?', project):
    raise SystemExit('Refusing synthetic catalogue outside a disposable demo fixture')
username = os.getenv('JL_FIXTURE_USER') or sys.stdin.readline().strip()
# Inline synthetic catalogue only. It is not an upstream response and the
# runtime image intentionally does not ship backend test fixtures.
rows = [
    {'category': 'A股', 'code': '002724', 'orgId': 'synthetic-catalog-2', 'zwjc': '合成目录乙'},
    {'category': 'A股', 'code': '600123', 'orgId': 'synthetic-catalog-1', 'zwjc': '合成目录甲'},
    {'category': 'B股', 'code': '200002', 'orgId': 'synthetic-b-share', 'zwjc': '合成B股样本'},
]
batch = parse_cninfo_catalog(json.dumps({'stockList': rows}).encode())
with Session(engine()) as db, db.begin():
    owner = db.scalar(select(User).where(User.username == username))
    assert owner and owner.role == 'admin'
    config = SourceConfiguration(family='cninfo', symbols=[], interval_seconds=3600,
        rights_basis='Self-created synthetic disposable fixture only; no upstream data or rights grant.',
        rights={'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'},
        entitlement_status='verified', enabled=True)
    provider = configure_source(db, owner, config)
    settings = db.get(SourceSettings, source_id('cninfo'))
    accepted, duplicates = store_catalog_batch(db, provider, settings, batch, datetime.now(UTC))
    db.add(DataFetchRun(source_id=provider.id, capability='instrument.catalog', job_id=None,
        started_at=datetime.now(UTC), received_at=datetime.now(UTC), status='partial',
        reason=None, expected=batch.expected + 4, accepted=accepted, rejected=len(batch.rejected),
        duplicates=duplicates, elapsed_ms=5, request_count=1, cost_cny=None, body_hash=None))

# A second, independently authorized source proves that revoking the catalogue
# claim does not hide identities this source still permits displaying.
quote_rows = []
for key, name in [('sh000001', '合成指数'), ('sz000001', '合成股票')]:
    fields = [''] * 88
    for index, value in {1: name, 2: '000001', 3: '12.30', 4: '12', 5: '12.10', 6: '123',
                         30: '20011231150000', 33: '12.50', 34: '12'}.items():
        fields[index] = value
    quote_rows.append(f'v_{key}="' + '~'.join(fields) + '";')
quote_body = '\n'.join(quote_rows).encode('gb18030')
with Session(engine()) as db, db.begin():
    owner = db.scalar(select(User).where(User.username == username))
    config = SourceConfiguration(family='tencent_web', symbols=[
        {'exchange': 'SSE', 'asset_type': 'index', 'symbol': '000001'},
        {'exchange': 'SZSE', 'asset_type': 'stock', 'symbol': '000001'}], interval_seconds=60,
        rights_basis='Self-created synthetic disposable fixture only; no upstream data or rights grant.',
        rights={'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'},
        entitlement_status='verified', enabled=True)
    provider = configure_source(db, owner, config)
    settings = db.get(SourceSettings, source_id('tencent_web'))
    keys = [InstrumentKey('SSE', '000001', 'index'), InstrumentKey('SZSE', '000001', 'stock')]
    quote_batch = parse_tencent_quotes(quote_body, keys, datetime.now(UTC))
    store_quote_batch(db, provider, settings, quote_batch)
print(json.dumps({'family': 'cninfo', 'accepted': accepted, 'duplicates': duplicates, 'rejected': len(batch.rejected)}))
