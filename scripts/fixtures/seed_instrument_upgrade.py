"""Synthetic 0008 upgrade fixture. Never run against a normal instance."""
import os
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import SourceVersion
from app.evidence.schemas import QuotePayload
from app.market.collect import store_observation
from app.market.models import InstrumentOrigin
from app.models import Provider

if os.getenv('JL_APP_MODE') != 'demo' or not re.fullmatch(r'jlpm-check-[a-f0-9]{10}-originsupgrade', os.getenv('JL_TEST_PROJECT','')):
    raise SystemExit('This fixture requires its own disposable demo upgrade project.')

with Session(engine()) as db, db.begin():
    assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='0008_instrument_origins'
    source=Provider(name='Synthetic upgrade source',kind='market',configured=True,enabled=False,
        entitlement_status='denied',rights={key:'denied' for key in ['display','cache','export','redistribute']},
        capabilities=['quote.l1'],adapter_version='synthetic-only')
    db.add(source); db.flush()
    descriptor={'name':source.name,'rights':source.rights}
    version=SourceVersion(source_id=source.id,recorded_at=datetime(2000,1,1,tzinfo=UTC),
        descriptor=descriptor,content_hash=content_hash(descriptor))
    db.add(version); db.flush()
    local,claimed,old_quote=[uuid.uuid4() for _ in range(3)]
    for key,symbol,name in [(local,'600991','Synthetic local'),(claimed,'600992','Synthetic revoked'),
                            (old_quote,'600993','Synthetic pre-0008 quote')]:
        db.execute(text("INSERT INTO instruments(id,exchange,symbol,name,asset_type) VALUES(:id,'SSE',:symbol,:name,'stock')"),
            {'id':key,'symbol':symbol,'name':name})
    received=datetime(2001,1,1,tzinfo=UTC)
    db.add(InstrumentOrigin(instrument_id=claimed,source_id=source.id,origin_kind='catalog',observed_name='Synthetic revoked',
        first_observed_at=received,last_observed_at=received,last_content_hash='a'*64,source_version_id=version.id))
    payload=QuotePayload(price='10',quote_time=received,instrument_name='Synthetic pre-0008 quote',price_unit='CNY')
    config=type('SyntheticConfig',(),{'rights_basis':'Self-created isolated upgrade evidence only.'})()
    store_observation(db,source,config,source_key='quote:SSE:stock:600993',kind='quote',payload=payload,
        received_at=received,event_at=received,instrument_id=old_quote,eligible=True)
print('Seeded only synthetic local/catalog/legacy-quote upgrade identities.')
