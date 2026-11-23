"""Only synthetic collection in an explicitly named disposable demo project."""
import json
import os
import re
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import collect
from app.market.protocol import Response
from app.market.schemas import SourceConfiguration
from app.market.service import configure_source, enqueue_collection, source_id
from app.models import User
from app.worker import claim, process

project = os.getenv('JL_TEST_PROJECT', '')
if os.getenv('JL_APP_MODE') != 'demo' or not re.fullmatch(r'jlpm-check-[a-f0-9]{10}(-source)?', project):
    raise SystemExit('Refusing synthetic sources outside a disposable demo fixture')
username = sys.stdin.readline().strip()
rows = []
for key, name in [('sh000001', '合成测试指数'), ('sz000001', '合成测试股票')]:
    fields = [''] * 88
    for index, value in {1: name, 2: '000001', 3: '12.30', 4: '12', 5: '12.10', 6: '123',
                         30: '20011231150000', 33: '12.50', 34: '12'}.items():
        fields[index] = value
    rows.append(f'v_{key}="' + '~'.join(fields) + '";')
body = '\n'.join(rows).encode('gb18030')

def synthetic_fetch(url):
    assert url == 'https://qt.gtimg.cn/q=sh000001,sz000001'
    return Response(body, datetime.now(UTC), 3, 200)

# This process-local replacement cannot enable network access in the API/worker.
collect.fetch_https = synthetic_fetch
settings().data_collection_enabled = True
with Session(engine()) as db, db.begin():
    owner = db.scalar(select(User).where(User.username == username))
    assert owner and owner.role == 'admin'
    config = SourceConfiguration(family='tencent_web', symbols=[
        {'exchange': 'SSE', 'asset_type': 'index', 'symbol': '000001'},
        {'exchange': 'SZSE', 'asset_type': 'stock', 'symbol': '000001'}], interval_seconds=60,
        rights_basis='Self-created synthetic disposable fixtures only; no upstream data or rights grant.',
        rights={'display': 'allowed', 'cache': 'allowed', 'export': 'unknown', 'redistribute': 'unknown'},
        entitlement_status='verified', enabled=True)
    configure_source(db, owner, config)
    job = enqueue_collection(db, source_id('tencent_web'))
    expected_job = job.id
job = claim()
assert job and job.id == expected_job and job.kind == 'source.collect'
process(job)
with Session(engine()) as db:
    evidence = list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == source_id('tencent_web'))))
    assert len(evidence) == 2
    print(json.dumps({'source_id': str(source_id('tencent_web')), 'evidence_ids': [str(row.id) for row in evidence]}))
