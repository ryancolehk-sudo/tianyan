"""Review acquisition boundaries on disposable PostgreSQL and mocked vendors.

These fixtures are arithmetic/transport examples, never real market evidence.
The repository's normal isolated_database guard applies to every test here.
"""

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.evidence.models import EvidenceVersion, SourceVersion
from app.evidence.schemas import ReferencePayload
from app.market import review_fetch as fetch
from app.market import review_jobs as jobs
from app.market.collect import store_observation
from app.market.minute_backfill import configuration_fingerprint
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import SHANGHAI, Response, SourceFailure
from app.market.review_protocol import CONTRACT_VERSION, DATASET_FAMILIES, ReviewPage
from app.market.review_store import family_id, load_dataset, persist_pages
from app.models import Job, Provider, User


@pytest.fixture(autouse=True)
def reject_real_vendor_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("review tests must not contact a real upstream")
    monkeypatch.setattr(jobs, "fetch_public", forbidden)
    monkeypatch.setattr("app.market.tushare.request", forbidden)
    monkeypatch.setattr(jobs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(settings(), "data_collection_enabled", True)
    monkeypatch.setattr(settings(), "tushare_token", SecretStr("synthetic-review-test-token"))


def day():
    target = datetime.now(UTC).astimezone(SHANGHAI).date() - timedelta(days=2)
    # Minute fixtures must remain on a weekday when the suite runs on Monday.
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


def context(dataset, target=None, *, start=None, scope="all"):
    target = target or day()
    return {"dataset": dataset, "day": str(target), "start_day": str(start or target), "scope": scope}


def encoded(rows, fields=None):
    fields = fields or list(rows[0])
    return json.dumps({"code": 0, "data": {"fields": fields,
                                           "items": [[row.get(k) for k in fields] for row in rows]}}).encode()


def daily(number=0, target=None):
    return {"ts_code": f"{600000 + number:06d}.SH", "trade_date": (target or day()).strftime("%Y%m%d"),
            "open": "10", "high": "11", "low": "9", "close": "10", "pre_close": "10",
            "pct_chg": "0", "amount": "1"}


def page(rows, *, dataset="daily", scope="all"):
    body = encoded(rows)
    return ReviewPage(list(rows[0]), rows, "https://tushare.pro/document/2?doc_id=27",
                      hashlib.sha256(body).hexdigest(), scope)


def new_job(db, owner, provider, config, dataset, *, target=None, start=None, scope="all", state="running"):
    ctx = {"version": CONTRACT_VERSION, **context(dataset, target, start=start, scope=scope),
           "configuration_sha256": configuration_fingerprint(provider, config)}
    job = Job(owner_id=owner.id, kind="review.collect", state=state, attempts=1,
              payload={"source_id": str(provider.id), "review_context": ctx},
              lease_token=uuid.uuid4(), lease_until=datetime.now(UTC) + timedelta(minutes=30))
    db.add(job)
    db.flush()
    return job


def setup_source(dataset="daily", *, target=None, start=None, scope="all"):
    family = DATASET_FAMILIES[dataset]
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        owner = User(username="review-test-" + uuid.uuid4().hex,
                     password_hash="synthetic-disabled-password", role="admin")
        db.add(owner)
        db.flush()
        provider = db.get(Provider, family_id(family))
        if provider is None:
            provider = Provider(id=family_id(family), family=family, name="Synthetic review source", kind="api")
            db.add(provider)
        provider.configured = provider.enabled = True
        provider.entitlement_status = "verified"
        provider.adapter_version = "synthetic-review-test"
        provider.capabilities = ["reference"]
        provider.rights = {"display": "allowed", "cache": "allowed",
                           "export": "unknown", "redistribute": "unknown"}
        db.flush()
        descriptor = {"configured": True, "enabled": True, "entitlement_status": "verified",
                      "rights": provider.rights}
        db.add(SourceVersion(source_id=provider.id, recorded_at=datetime(1970, 1, 1, tzinfo=UTC),
                             descriptor=descriptor, content_hash="a" * 64))
        config = SourceSettings(source_id=provider.id, owner_id=owner.id,
                                rights_basis="Synthetic isolated-test fixture only", config={},
                                interval_seconds=21600, configured_at=datetime.now(UTC),
                                next_run_at=datetime.now(UTC) + timedelta(hours=6))
        db.add(config)
        db.flush()
        job = new_job(db, owner, provider, config, dataset, target=target, start=start, scope=scope)
        return owner, provider, config, job


@pytest.mark.parametrize("dataset,size", [("daily", 5000), ("limits", 5000), ("pools", 2500),
                                          ("themes", 5000), ("theme_funds", 5000),
                                          ("lhb", 5000), ("seats", 5000), ("members", 5000)])
def test_each_paged_request_advances_offset_with_its_actual_page_size(dataset, size):
    _, first, _ = fetch.parameters(context(dataset), offset=0)
    _, second, _ = fetch.parameters(context(dataset), offset=size)
    assert first["limit"] == second["limit"] == size
    assert first["offset"] == 0 and second["offset"] == size
    assert first["trade_date"] == second["trade_date"] == day().strftime("%Y%m%d")


def test_raw_missing_values_stay_distinct_from_zero_and_same_named_seats_survive():
    row = daily()
    row.update(amount=None, pct_chg="0")
    result = fetch.validate_table(encoded([row]), context("daily"))
    assert result.rows[0]["amount"] is None and result.rows[0]["pct_chg"] == "0"
    seats = [{"trade_date": day().strftime("%Y%m%d"), "ts_code": "600001.SH", "reason": "synthetic",
              "exalter": "机构专用", "side": "0", "buy": str(amount), "sell": "0"}
             for amount in ("100", "200")]
    assert len(fetch.validate_table(encoded(seats), context("seats")).rows) == 2
    with pytest.raises(SourceFailure):
        fetch.validate_table(encoded([daily(), daily()]), context("daily"))
    with pytest.raises(SourceFailure):
        fetch.validate_table(encoded([daily(target=day() - timedelta(days=1))]), context("daily"))


def test_repeated_lhb_code_reason_rows_remain_source_ordered():
    rows = [{"trade_date": day().strftime("%Y%m%d"), "ts_code": "600001.SH",
             "reason": "same disclosed text", "l_buy": amount,
             "l_sell": "1", "net_amount": "1"} for amount in ("100", "200", "100")]
    result = fetch.validate_table(encoded(rows), context("lhb"))
    assert [row["l_buy"] for row in result.rows] == ["100", "200", "100"]


@pytest.mark.parametrize("dataset", ["lhb", "seats"])
def test_repeated_disclosure_pages_fail_even_if_response_metadata_changes(monkeypatch, dataset):
    _, _, _, job = setup_source(dataset)
    started = datetime.now(UTC)
    class ScenarioTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return started.astimezone(tz) if tz else started.replace(tzinfo=None)
    monkeypatch.setattr(jobs, 'datetime', ScenarioTime)
    row = {"trade_date": day().strftime("%Y%m%d"), "ts_code": "600001.SH", "reason": "repeated",
           "l_buy": "100", "l_sell": "1", "net_amount": "99",
           "exalter": "机构专用", "side": "0", "buy": "100", "sell": "1"}
    calls = []
    def request(api, params, fields, *args):
        calls.append(params["offset"])
        body = json.loads(encoded([row] * 5000))
        body["request_id"] = str(len(calls))
        # Request receipts are deterministic and later than collection start;
        # the assertion concerns repeated content, not host clock adjustment.
        return Response(json.dumps(body).encode(), started + timedelta(milliseconds=len(calls)), 1, 200)
    monkeypatch.setattr("app.market.tushare.request", request)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    assert calls == [0, 5000]
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == "failed" and saved.error_code == "review_pagination_overlap"
        assert load_dataset(db, dataset, day())["state"] == "missing"


@pytest.mark.parametrize("empty_end", [False, True])
def test_two_page_market_persists_one_complete_manifest_with_both_parts(monkeypatch, empty_end):
    _, provider, _, job = setup_source()
    first, last = [daily(i) for i in range(5000)], [] if empty_end else [daily(5000)]
    offsets = []
    def request(api, params, fields, *args):
        offsets.append(params["offset"])
        return Response(encoded(first if params["offset"] == 0 else last, list(first[0])), datetime.now(UTC), 1, 200)
    monkeypatch.setattr("app.market.tushare.request", request)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    assert offsets == [0, 5000]
    with Session(engine()) as db:
        saved = db.get(Job, job.id)
        assert saved.state == "succeeded", saved.error_code
        result = load_dataset(db, "daily", day())
        assert result["state"] == "complete" and result["row_count"] == 5000 + len(last)
        assert [ref["row_count"] for ref in result["evidence_refs"]] == [5000, len(last)]
        assert len({r["ts_code"] for r in result["rows"]}) == 5000 + len(last)
        assert all(not row.historical_eligible and row.available_at is None
                   for row in db.scalars(select(EvidenceVersion).where(EvidenceVersion.source_id == provider.id)))


def test_empty_first_market_page_cannot_publish_a_complete_market(monkeypatch):
    _, _, _, job = setup_source()
    monkeypatch.setattr("app.market.tushare.request", lambda *args:
                        Response(encoded([], list(daily())), datetime.now(UTC), 1, 200))
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "failed"
        assert load_dataset(db, "daily", day())["state"] == "missing"


@pytest.mark.parametrize('clock_moves_backwards',[False,True])
def test_failed_later_page_never_publishes_a_partial_manifest(monkeypatch,clock_moves_backwards):
    _, provider, _, job = setup_source()
    if clock_moves_backwards:
        started=datetime.now(UTC)
        class AdjustedClock(datetime):
            calls=0
            @classmethod
            def now(cls,tz=None):
                cls.calls+=1
                stamp=started-timedelta(seconds=30 if cls.calls>1 else 0)
                return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
        monkeypatch.setattr(jobs,'datetime',AdjustedClock)
    offsets = []
    def request(api, params, fields, *args):
        offsets.append(params["offset"])
        if params["offset"] == 5000:
            raise SourceFailure("timeout")
        return Response(encoded([daily(i) for i in range(5000)]), datetime.now(UTC), 1, 200)
    monkeypatch.setattr("app.market.tushare.request", request)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "queued"
        assert load_dataset(db, "daily", day())["state"] == "missing"
        assert db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id == provider.id)) is None
        assert offsets == [0, 5000, 5000, 5000]
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job.id)))
        assert sum(r.request_count for r in runs) == 4
        assert all(r.elapsed_ms is None or r.elapsed_ms>=0 for r in runs)


@pytest.mark.parametrize("change,reason", [("configuration", "configuration_changed"), ("rights", "permission_missing")])
def test_changes_during_transport_block_commit(monkeypatch, change, reason):
    _, provider, _, job = setup_source()
    def request(*args):
        with Session(engine()) as db, db.begin():
            if change == "configuration":
                db.get(SourceSettings, provider.id).interval_seconds += 1
            else:
                current = db.get(Provider, provider.id)
                current.rights = {**current.rights, "cache": "denied"}
        return Response(encoded([daily()]), datetime.now(UTC), 1, 200)
    monkeypatch.setattr("app.market.tushare.request", request)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    with Session(engine()) as db:
        assert db.get(Job, job.id).error_code == reason
        assert db.scalar(select(EvidenceVersion.id).where(EvidenceVersion.source_id == provider.id)) is None


def test_retry_preserves_previous_successful_manifest_and_live_schedule(monkeypatch):
    owner, provider, config, original = setup_source()
    stamp = datetime.now(UTC) - timedelta(minutes=1)
    with Session(engine(), expire_on_commit=False) as db, db.begin():
        persist_pages(db, provider, config, original, [(page([daily()]), stamp)])
        db.get(Job, original.id).state = "succeeded"
        before = load_dataset(db, "daily", day())["manifest_ref"]
        retry = new_job(db, owner, provider, config, "daily")
    def fail(*args):
        raise SourceFailure("timeout")
    monkeypatch.setattr("app.market.tushare.request", fail)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(retry)
    with Session(engine()) as db:
        assert db.get(Job, retry.id).state == "queued"
        assert load_dataset(db, "daily", day())["manifest_ref"] == before
        assert db.get(SourceSettings, provider.id).next_run_at == config.next_run_at


def test_transient_paid_page_retry_keeps_validated_first_page(monkeypatch):
    _, _, _, job = setup_source()
    first, calls = [daily(i) for i in range(5000)], []
    def request(api, params, fields, *args):
        calls.append(params["offset"])
        if calls == [0, 5000]:
            raise SourceFailure("transport_error")
        rows = first if params["offset"] == 0 else [daily(5000)]
        return Response(encoded(rows), datetime.now(UTC), 1, 200)
    monkeypatch.setattr("app.market.tushare.request", request)
    monkeypatch.setattr("app.market.budget.reserve_tushare_request", lambda job: True)
    jobs.collect_job(job)
    assert calls == [0, 5000, 5000]
    with Session(engine()) as db:
        assert db.get(Job, job.id).state == "succeeded"
        assert load_dataset(db, "daily", day())["row_count"] == 5001
        attempts = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job.id)))
        assert sum(row.request_count for row in attempts) == 3


def test_manifest_missing_part_is_broken_even_on_count_only_read():
    _, provider, config, job = setup_source()
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, job, [(page([daily()]), datetime.now(UTC))])
        result = load_dataset(db, "daily", day())
        part_id = uuid.UUID(result["evidence_refs"][0]["id"])
        manifest = db.get(EvidenceVersion, uuid.UUID(result["manifest_ref"]["id"]))
        broken = ReferencePayload.model_validate(copy.deepcopy(manifest.payload))
        broken.rows[0]["part_id"] = str(uuid.uuid4())
        # Simulate broken lineage with a new fixture revision; immutable prior
        # evidence and its DB triggers remain untouched.
        store_observation(db, provider, config, source_key=manifest.source_key,
                          kind="reference", payload=broken, received_at=datetime.now(UTC),
                          eligible=False, repeated_state=True)
        assert db.get(EvidenceVersion, part_id) is not None
    with Session(engine()) as db:
        result = load_dataset(db, "daily", day(), include_rows=False)
        assert result["state"] == "broken_lineage"
        assert result["row_count"] == 0 and not result["evidence_refs"]


def test_later_rolling_minutes_do_not_hide_already_saved_older_day():
    old_day, recent = day() - timedelta(days=10), day()
    owner, provider, config, old_job = setup_source("tencent_minutes", target=old_day)
    def minute_page(target):
        rows = [{"ts_code": "000001.SH", "trade_time": f"{target}T09:31:00+08:00", "close": "3000"}]
        return page(rows, dataset="tencent_minutes", scope="000001.SH")
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, old_job,
                      [(minute_page(old_day), datetime.now(UTC) - timedelta(minutes=1))])
        latest = new_job(db, owner, provider, config, "tencent_minutes", target=recent)
        persist_pages(db, provider, config, latest, [(minute_page(recent), datetime.now(UTC))])
    with Session(engine()) as db:
        result = load_dataset(db, "tencent_minutes", old_day)
        assert result["state"] == "complete" and result["row_count"] == 1
        assert result["rows"][0]["trade_time"].startswith(str(old_day))


def test_short_index_history_cannot_satisfy_a_longer_backfill_request():
    target = day()
    old_start = target - timedelta(days=20)
    owner, provider, config, original = setup_source("indices", target=target, start=old_start, scope="000001.SH")
    rows = [{"ts_code": "000001.SH", "trade_date": d.strftime("%Y%m%d"), "close": "3000"}
            for d in (old_start, target)]
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, original, [(page(rows, dataset="indices"), datetime.now(UTC))])
        db.get(Job, original.id).state = "succeeded"
        queued = jobs.queue_dataset(db, owner, "indices", target, start_day=target - timedelta(days=90), scope="000001.SH")
        assert queued.id != original.id
        assert queued.payload["review_context"]["start_day"] == str(target - timedelta(days=90))


def test_reconstructed_table_is_not_visible_before_its_receipt():
    _, provider, config, job = setup_source()
    stamp = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        persist_pages(db, provider, config, job, [(page([daily()]), stamp)])
    with Session(engine()) as db:
        result = load_dataset(db, "daily", day(), as_of_time=stamp - timedelta(seconds=1))
        assert result["state"] == "missing" and not result["rows"]
        after = load_dataset(db, "daily", day(), as_of_time=stamp + timedelta(seconds=1))
        assert after["state"] == "complete" and after["rows"]


def test_news_duplicate_identity_is_rejected_and_truncation_is_explicit():
    stamp = int(datetime.fromisoformat(f"{day()}T10:00:00+08:00").timestamp())
    item = {"id": 12345, "ctime": stamp, "title": "<b>合成新闻</b>", "content": "测" * 700}
    def body(items):
        return json.dumps({"errno": 0, "data": {"roll_data": items}}).encode()
    with pytest.raises(SourceFailure):
        fetch.news_page(Response(body([item, item]), datetime.now(UTC), 1, 200), day())
    result, cursor, returned = fetch.news_page(Response(body([item]), datetime.now(UTC), 1, 200), day())
    assert cursor == stamp and returned == 1
    assert result.rows[0]["title"] == "合成新闻"
    assert len(result.rows[0]["summary"]) == 500
    assert result.rows[0]["text_truncated"] == "true"


def test_public_transient_next_index_retries_same_window_without_losing_prior_window(monkeypatch):
    _, _, _, job = setup_source('tencent_minutes')
    calls, permits = [], []
    def reserve(*args):
        permits.append(True)
        return True
    def reply(url):
        calls.append(url)
        if len(calls) == 2:
            raise SourceFailure('transport_error')
        symbol = url.split('param=')[1].split(',')[0]
        point = [day().strftime('%Y%m%d') + '0931', '10', '10', '11', '9', '1', {}, '0']
        raw = json.dumps({'code': 0, 'data': {symbol: {'m1': [point]}}}).encode()
        return Response(raw, datetime.now(UTC), 1, 200)
    monkeypatch.setattr(jobs, '_reserve_public', reserve)
    monkeypatch.setattr(jobs, 'fetch_public', reply)
    jobs.collect_job(job)
    assert len(permits) == 4 and calls[1] == calls[2]
    with Session(engine()) as db:
        saved = load_dataset(db, 'tencent_minutes', day())
        assert saved['state'] == 'complete' and saved['row_count'] == 3
        assert len(saved['evidence_refs']) == 3
        assert db.get(Job, job.id).state == 'succeeded'
        runs = list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id == job.id)))
        assert sum(r.request_count for r in runs) == 4
        assert sum(r.reason == 'transport_error' for r in runs) == 1


def test_news_is_one_declared_latest_twenty_window_not_full_day(monkeypatch):
    _, _, _, job = setup_source('news')
    stamp = int(datetime.fromisoformat(f'{day()}T14:00:00+08:00').timestamp())
    calls = []
    def reply(url):
        calls.append(url)
        rows = [{'id': 5000 + i, 'ctime': stamp - i, 'title': 'Synthetic news'} for i in range(20)]
        return Response(json.dumps({'errno': 0, 'data': {'roll_data': rows}}).encode(), datetime.now(UTC), 1, 200)
    monkeypatch.setattr(jobs, '_reserve_public', lambda *args: True)
    monkeypatch.setattr(jobs, 'fetch_public', reply)
    jobs.collect_job(job)
    assert len(calls) == 1
    with Session(engine()) as db:
        saved = load_dataset(db, 'news', day())
        assert saved['state'] == 'complete' and saved['row_count'] == 20
        assert saved['coverage_scope'] == 'daily_latest_20_news'
