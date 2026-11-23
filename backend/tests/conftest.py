import os
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.common import content_hash, normalized
from app.evidence.models import SourceVersion
from app.main import app
from app.models import Provider, WorkerHeartbeat
from app.schemas import Provider as ProviderDTO


def pytest_make_parametrize_id(config, val, argname):
    # Large response fixtures must not become multi-megabyte Windows env values.
    if isinstance(val, bytes) and len(val) > 256:
        return f'{argname}-{len(val)}bytes-{sha256(val).hexdigest()[:12]}'
    return None


def reset_sources(db, *, historical=False):
    # instrument_origins claims reference source_versions; both must truncate together.
    db.execute(text("TRUNCATE instrument_origins, source_versions"))
    db.execute(text("DELETE FROM providers WHERE family IS NOT NULL"))
    for source in db.scalars(select(Provider)):
        manual = source.kind == "manual"
        source.configured = source.enabled = manual
        source.entitlement_status = "verified" if manual else "unknown"
        source.rights = {key: "allowed" if manual and key in {"display", "cache"} else "unknown"
                         for key in ["display", "cache", "export", "redistribute"]}
        descriptor = normalized(ProviderDTO.model_validate(source))
        # Only this disposable database has synthetic historical registration times.
        db.add(SourceVersion(source_id=source.id, recorded_at=datetime(1970, 1, 1, tzinfo=UTC) if historical else datetime.now(UTC),
                             descriptor=descriptor, content_hash=content_hash(descriptor)))


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    if os.getenv("JL_ALLOW_TEST_DATABASE_RESET") != "yes":
        pytest.fail("Tests require the disposable Compose test project; refusing to clear a normal database.")
    # Existing business-projection tests intentionally read current raw results.
    # The dedicated cache tests opt in and verify stale/background semantics.
    from app.config import settings
    from app.read_cache_payloads import clear
    from app.market.terminal_public import clear as clear_public_prices
    from app.market import auction_source
    monkeypatch.setattr(auction_source, '_METADATA_CACHE', None)
    monkeypatch.setattr(settings(), 'read_cache_enabled', False)
    clear()
    clear_public_prices()
    with engine().begin() as db:
        db.execute(text('TRUNCATE evidence_read_facets'))
        db.execute(text('TRUNCATE data_routes,recovery_coverage,data_health'))
        db.execute(text('TRUNCATE read_cache_entries,read_cache_dependencies,read_cache_dispatch'))
        db.execute(text("TRUNCATE screen_events,screen_runs,screen_rule_versions,screen_rules,tomorrow_validations,tomorrow_confirmations,tomorrow_reports,tomorrow_runs,tomorrow_snapshots,tomorrow_settings,user_preferences,backup_records,maintenance_jobs,system_settings_state,system_update_state,chatgpt_authorizations,chatgpt_accounts,chatgpt_identities,chatgpt_installation,news_ai_versions,news_edition_items,news_editions,news_user_states,news_articles,news_source_coverage,news_center_state,ai_probe_proofs,ai_attempts,ai_runs,ai_bindings,ai_settings,service_connections,watchlists,instrument_origins,data_fetch_runs,source_settings,review_archives,validations,auction_observations,revision_links,plan_scenarios,publication_events,document_revisions,research_documents,snapshot_items,snapshots,evidence_versions,source_health,trading_sessions,calendar_versions,idempotency_keys,sessions,login_throttles,audit_events,jobs,users,instruments,worker_heartbeats"))
        db.execute(text("UPDATE instance_state SET app_mode='demo' WHERE id=1"))
    with Session(engine()) as db, db.begin():
        reset_sources(db, historical=True)
        db.add(WorkerHeartbeat(worker_id="test", seen_at=datetime.now(UTC)))
    yield
    with engine().begin() as db:
        db.execute(text('TRUNCATE evidence_read_facets'))
        db.execute(text('TRUNCATE data_routes,recovery_coverage,data_health'))
        db.execute(text('TRUNCATE read_cache_entries,read_cache_dependencies,read_cache_dispatch'))
        db.execute(text("TRUNCATE screen_events,screen_runs,screen_rule_versions,screen_rules,tomorrow_validations,tomorrow_confirmations,tomorrow_reports,tomorrow_runs,tomorrow_snapshots,tomorrow_settings,user_preferences,backup_records,maintenance_jobs,system_settings_state,system_update_state,chatgpt_authorizations,chatgpt_accounts,chatgpt_identities,chatgpt_installation,news_ai_versions,news_edition_items,news_editions,news_user_states,news_articles,news_source_coverage,news_center_state,ai_probe_proofs,ai_attempts,ai_runs,ai_bindings,ai_settings,service_connections,watchlists,instrument_origins,data_fetch_runs,source_settings,review_archives,validations,auction_observations,revision_links,plan_scenarios,publication_events,document_revisions,research_documents,snapshot_items,snapshots,evidence_versions,source_health,trading_sessions,calendar_versions,idempotency_keys,sessions,login_throttles,audit_events,jobs,users,instruments,worker_heartbeats"))
        db.execute(text("UPDATE instance_state SET app_mode='demo' WHERE id=1"))
    with Session(engine()) as db, db.begin():
        reset_sources(db)


@pytest.fixture
def client():
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield client
