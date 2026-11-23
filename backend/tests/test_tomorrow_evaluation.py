"""Shadow evaluation using synthetic records, never evidence of real returns."""

import copy
import json
import os
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.auth import APIError
from app.tomorrow import evaluation as ev

DAY = date(2026, 10, 23)
PREVIOUS = date(2026, 10, 22)
CUTOFF = datetime(2026, 10, 23, 16, tzinfo=UTC)
CREATED = datetime(2026, 10, 22, 15, tzinfo=UTC)
RUN, REPORT, REF = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)


def records():
    return dict(runs=[dict(id=RUN, source_trade_day=PREVIOUS, target_trade_day=DAY,
                          created_at=CREATED, completed_at=CREATED + timedelta(minutes=2), as_of_time=CREATED,
                          state="succeeded", app_mode="real", synthetic=False, knowledge_mode="received_by_cutoff", stages=[])],
                reports=[dict(id=REPORT, run_id=RUN, created_at=CREATED + timedelta(minutes=1),
                              revision_number=1, author_type="rule", is_head=True, is_selected_confirmation=True,
                              is_prospective_head=True, core_ready=True, target_verified=True,
                              evidence_refs=[{"id": str(REF), "content_hash": "a" * 64}], analysis_count=2,
                              analysis_refs=[[str(REF)], []], scenarios=[
                                  {"id": "s1", "verification_method": "manual", "next_checks": [{"window": "open"}]},
                                  {"id": "s2", "verification_method": "rule"}], audit_passed=None)],
                confirmations=[dict(report_id=REPORT, created_at=CREATED + timedelta(minutes=3))],
                validations=[], attempts=[],
                evidence={str(REF): dict(content_hash="a" * 64, received_at=CREATED - timedelta(hours=1),
                                         available_at=None, published_at=None, historical_eligible=False)},
                revisions=[dict(id=REPORT, run_id=RUN, author_type="rule", created_at=CREATED, reason=None)],
                known_closed_trade_days=[PREVIOUS, DAY], truncated=False)


class EvaluationTests(unittest.TestCase):
    def test_shadow_progress_is_target_date_based_and_never_profit(self):
        result = ev._summarize(records(), CUTOFF, [DAY], 20)
        self.assertEqual(result["progress"]["eligible_shadow_target_day_count"], 1)
        self.assertEqual(result["progress"]["confirmed_before_target_day_count"], 1)
        self.assertFalse(result["progress"]["observation_window_complete"])
        self.assertEqual(result["reports"]["confirmed_latest_count"], 1)
        self.assertEqual(result["evidence"]["narrative_reference_coverage_pct"], "50")
        self.assertEqual(result["evidence"]["strict_historical_eligible_reference_occurrences"], 0)
        self.assertNotIn("profit", result)
        self.assertIn("不计算回测收益", result["limitations"][0])
        json.dumps(result, allow_nan=False)

    def test_demo_unknown_and_retrospective_do_not_count_as_observed_days(self):
        for mode in ("demo", None):
            source = records()
            source["runs"][0]["app_mode"] = mode
            result = ev._summarize(source, CUTOFF, [DAY], 1)
            self.assertEqual(result["progress"]["eligible_shadow_target_day_count"], 0)
        source = records()
        source["runs"][0]["created_at"] = CUTOFF
        result = ev._summarize(source, CUTOFF, [DAY], 1)
        self.assertEqual(result["runs"]["knowledge_modes"]["historical_or_retrospective"], 1)
        self.assertEqual(result["progress"]["eligible_shadow_target_day_count"], 0)
        source["runs"][0]["created_at"] = CREATED
        source["runs"][0]["knowledge_mode"] = "historical_reconstruction"
        self.assertEqual(ev._summarize(source, CUTOFF, [DAY], 1)["progress"]["eligible_shadow_target_day_count"], 0)

    def test_after_market_manual_revision_does_not_erase_original_pre_market_sample(self):
        source = records()
        original = source["reports"][0]
        original["is_head"] = False
        newer = copy.deepcopy(original)
        newer.update(id=uuid.UUID(int=4), created_at=CUTOFF, is_head=True,
                     is_selected_confirmation=False, is_prospective_head=False, author_type="manual", revision_number=2)
        source["reports"].append(newer)
        source["revisions"].append(dict(id=newer["id"], run_id=RUN, author_type="manual", created_at=CUTOFF, reason="核对次日反证"))
        result = ev._summarize(source, CUTOFF, [DAY], 1)
        self.assertTrue(result["progress"]["observation_window_complete"])
        self.assertEqual(result["reports"]["draft_latest_count"], 1)
        self.assertEqual(result["manual_revisions"]["reason_counts"], [{"reason": "核对次日反证", "count": 1}])

    def test_late_or_unavailable_evidence_withholds_qualification(self):
        for change in ({"received_at": CREATED + timedelta(seconds=1)}, {"content_hash": "b" * 64}):
            source = records()
            source["evidence"][str(REF)].update(change)
            result = ev._summarize(source, CUTOFF, [DAY], 1)
            self.assertEqual(result["evidence"]["unavailable_reference_occurrences"], 1)
            self.assertEqual(result["evidence"]["narrative_reference_coverage_pct"], "0")
            self.assertEqual(result["progress"]["eligible_shadow_target_day_count"], 0)

    def test_usage_is_actual_attempt_based_unknown_cost_is_never_zero(self):
        completed = CREATED + timedelta(seconds=1)
        attempts = [dict(id=1, request_count=1, sent_at=CREATED, completed_at=completed, state="succeeded",
                         input_tokens=100, output_tokens=10, cost_cny=Decimal("0.25"), elapsed_ms=1000),
                    dict(id=2, request_count=1, sent_at=CREATED, completed_at=completed, state="unknown",
                         input_tokens=None, output_tokens=None, cost_cny=None, elapsed_ms=500),
                    dict(id=3, request_count=0, sent_at=None, completed_at=completed, state="failed", cost_cny=99)]
        result = ev._usage(attempts, CUTOFF)
        self.assertEqual(result["actual_request_count"], 2)
        self.assertIsNone(result["cost_cny"]["total"])
        self.assertEqual(result["cost_cny"]["known_total"], "0.25")
        self.assertEqual(result["input_tokens"]["unknown_attempts"], 1)
        self.assertEqual(result["elapsed_ms"]["total"], "1500")

    def test_finished_after_cutoff_usage_does_not_leak_into_old_evaluation(self):
        result = ev._usage([dict(request_count=1, sent_at=CREATED, completed_at=CUTOFF + timedelta(seconds=1),
                                input_tokens=500, output_tokens=50, cost_cny=1, elapsed_ms=123, state="succeeded")], CUTOFF)
        self.assertEqual(result["states"], {"in_progress_at_cutoff": 1})
        self.assertIsNone(result["cost_cny"]["total"])
        self.assertIsNone(result["elapsed_ms"]["total"])

    def test_validation_is_latest_status_per_original_condition_not_repeated_observations(self):
        source = records()
        source["validations"] = [dict(id=1, report_id=REPORT, scenario_id="s1", status="pending", created_at=CREATED),
                                 dict(id=2, report_id=REPORT, scenario_id="s1", status="confirmed", created_at=CUTOFF)]
        result = ev._summarize(source, CUTOFF, [DAY], 20)
        self.assertEqual(result["conditions"]["manual_observation"], 1)
        self.assertEqual(result["conditions"]["declared_machine_method"], 1)
        self.assertIsNone(result["conditions"]["machine_checkability_verified"])
        self.assertEqual(result["conditions"]["actionable_manual_observation"], 0)
        self.assertEqual(result["conditions"]["validation_record_count"], 2)
        self.assertEqual(result["conditions"]["latest_statuses"], {"confirmed": 1})

    def test_core_gap_and_truncation_are_not_success(self):
        source = records()
        source["reports"][0]["core_ready"] = False
        result = ev._summarize(source, CUTOFF, [DAY], 1)
        self.assertEqual(result["data_quality"]["core_data_missing"], 1)
        self.assertFalse(result["progress"]["observation_window_complete"])
        source = records()
        source["truncated"] = True
        self.assertFalse(ev._summarize(source, CUTOFF, [DAY], 1)["progress"]["observation_window_complete"])

    def test_actionable_observation_requires_specific_checks_and_no_known_data_gap(self):
        source = records()
        scenario = source["reports"][0]["scenarios"][0]
        scenario["next_checks"] = [{"subject": "合成对象", "window": "目标日开盘观察", "support": "核对同组相对强弱变化",
                                    "invalidation": "同组走弱且对象失去支撑", "missing_data": []}]
        result = ev._summarize(source, CUTOFF, [DAY], 1)
        self.assertEqual(result["conditions"]["actionable_manual_observation"], 1)
        scenario["next_checks"][0]["missing_data"] = ["未保存盘口"]
        result = ev._summarize(source, CUTOFF, [DAY], 1)
        self.assertEqual(result["conditions"]["actionable_manual_observation"], 0)
        self.assertEqual(result["conditions"]["necessary_data_missing"], 1)

    def test_empty_calendar_is_not_fabricated_from_weekdays(self):
        source = {key: [] for key in ("runs", "reports", "confirmations", "validations", "attempts", "revisions")}
        source.update(evidence={}, truncated=False)
        result = ev._summarize(source, CUTOFF, [], 20)
        self.assertEqual(result["window"]["actual_calendar_days"], 0)
        self.assertIsNone(result["evidence"]["reference_availability_pct"])
        self.assertEqual(result["model_usage"]["cost_cny"]["total"], "0")

    def test_public_function_only_reads_and_propagates_as_of(self):
        db = object()
        with patch.object(ev, "now", return_value=CUTOFF), patch.object(ev, "available_trade_days", return_value=[DAY]) as calendar, patch.object(ev, "_load_records", return_value=records()) as query:
            result = ev.shadow_evaluation(db, uuid.uuid4(), CUTOFF, 20)
        self.assertEqual(calendar.call_args.kwargs["as_of_time"], CUTOFF)
        self.assertEqual(query.call_args.args[2], CUTOFF)
        self.assertEqual(result["window"]["requested_trading_days"], 20)
        with patch.object(ev, "now", return_value=CUTOFF):
            for bad in (0, 61, True, "20"):
                with self.assertRaises(APIError):
                    ev.shadow_evaluation(db, uuid.uuid4(), CUTOFF, bad)
            with self.assertRaises(APIError):
                ev.shadow_evaluation(db, uuid.uuid4(), datetime(2026, 10, 23))
            with self.assertRaises(APIError):
                ev.shadow_evaluation(db, uuid.uuid4(), CUTOFF + timedelta(seconds=1))


@unittest.skipUnless(os.getenv("JL_ALLOW_TEST_DATABASE_RESET") == "yes", "requires guarded disposable PostgreSQL")
class PostgreSQLEvaluationTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.orm import Session

        from app.db import engine

        self.db = self.enterContext(Session(engine()))
        self.transaction = self.db.begin()
        self.addCleanup(self.transaction.rollback)

    def run_fixture(self, *, owner=None, mode="demo", ready=True):
        from app.models import Job, User
        from app.tomorrow.models import PlanReport, PlanRun

        if owner is None:
            owner = User(username="synthetic-evaluation-" + uuid.uuid4().hex, password_hash="disabled", role="admin")
            self.db.add(owner)
            self.db.flush()
        job = Job(owner_id=owner.id, kind="tomorrow.build", state="succeeded", attempts=1, payload={})
        self.db.add(job)
        self.db.flush()
        run = PlanRun(owner_id=owner.id, job_id=job.id, source_trade_day=PREVIOUS, target_trade_day=DAY,
                      as_of_time=CREATED, slot="evening", state="succeeded", stage="Synthetic fixture", use_ai=False,
                      dedup_key=uuid.uuid4().hex * 2, bundle={"knowledge_mode": "received_by_cutoff"}, stages=[],
                      config_snapshot={"app_mode": mode}, limitations=[], preparation={}, created_at=CREATED,
                      completed_at=CREATED + timedelta(minutes=2))
        self.db.add(run)
        self.db.flush()
        report = PlanReport(owner_id=owner.id, run_id=run.id, revision_number=1, author_type="rule",
                            payload={"dossier": {"ready": ready, "target_trade_day_verified": True, "scenarios": []},
                                     "evidence_refs": [], "sections": [{"analysis": []}]},
                            content_hash="a" * 64, created_at=CREATED + timedelta(minutes=1))
        self.db.add(report)
        self.db.flush()
        return owner, run, report

    def test_postgresql_owner_scoping_and_read_only_metadata_queries(self):
        from sqlalchemy import func, select

        from app.models import Job
        from app.tomorrow.models import PlanConfirmation

        owner, _, report = self.run_fixture()
        self.run_fixture()
        self.db.add(PlanConfirmation(owner_id=owner.id, report_id=report.id, created_at=CREATED + timedelta(minutes=3)))
        self.db.flush()
        before = self.db.scalar(select(func.count()).select_from(Job))
        with patch.object(ev, "now", return_value=CUTOFF), patch.object(ev, "available_trade_days", return_value=[DAY]):
            result = ev.shadow_evaluation(self.db, owner.id)
        self.assertEqual(result["runs"]["count"], 1)
        self.assertEqual(result["reports"]["confirmed_latest_count"], 1)
        self.assertEqual(result["runs"]["knowledge_modes"], {"synthetic_or_demo": 1})
        self.assertEqual(result["progress"]["eligible_shadow_target_day_count"], 0)
        self.assertEqual(before, self.db.scalar(select(func.count()).select_from(Job)))

    def test_later_report_revision_is_excluded_by_requested_cutoff(self):
        from app.tomorrow.models import PlanReport

        owner, run, _ = self.run_fixture(ready=False)
        self.db.add(PlanReport(owner_id=owner.id, run_id=run.id, revision_number=2, author_type="manual",
                               payload={"dossier": {"ready": True}, "evidence_refs": [], "sections": [], "revision_reason": "After-cutoff synthetic change"},
                               content_hash="b" * 64, created_at=CUTOFF + timedelta(seconds=1)))
        self.db.flush()
        with patch.object(ev, "now", return_value=CUTOFF), patch.object(ev, "available_trade_days", return_value=[DAY]):
            result = ev.shadow_evaluation(self.db, owner.id)
        self.assertEqual(result["data_quality"]["core_data_missing"], 1)
        self.assertEqual(result["manual_revisions"]["count"], 0)

    def test_recorded_model_attempts_and_evidence_are_aggregated_without_payload_exposure(self):
        from app.ai.models import AiAttempt, AiRun, Connection
        from app.evidence.models import EvidenceVersion
        from app.models import Job, Provider

        owner, run, report = self.run_fixture()
        provider = Provider(name="Synthetic metadata source", kind="api", configured=True, enabled=True,
                            entitlement_status="verified", rights={"display": "allowed", "cache": "allowed"})
        self.db.add(provider)
        self.db.flush()
        evidence = EvidenceVersion(source_id=provider.id, owner_id=None, visibility="shared", source_key="synthetic-eval",
                                   source_revision="1", version_number=1, kind="reference", received_at=CREATED - timedelta(hours=1),
                                   available_at=None, availability="unknown", provenance="source_reported", historical_eligible=False,
                                   rights_basis="Synthetic isolated test only", payload={"private_body": "MUST_NOT_APPEAR"}, content_hash="a" * 64)
        self.db.add(evidence)
        self.db.flush()
        report.payload = {**report.payload, "evidence_refs": [{"id": str(evidence.id), "content_hash": evidence.content_hash}],
                          "sections": [{"analysis": [{"text": "Synthetic", "evidence_refs": [str(evidence.id)]}]}]}
        connection = Connection(owner_id=owner.id, name="Synthetic", kind="model", endpoint="https://example.invalid",
                                protocol="openai_chat", model="synthetic", enabled=False)
        ai_job = Job(owner_id=owner.id, kind="ai.generate", state="succeeded", attempts=1, payload={})
        self.db.add_all([connection, ai_job])
        self.db.flush()
        ai = AiRun(owner_id=owner.id, job_id=ai_job.id, feature="plan_generation", state="succeeded", stage="Synthetic",
                   connection_id=connection.id, model="synthetic", trade_day=PREVIOUS, as_of_time=CREATED,
                   config_snapshot={"billing_source": "api"}, input_hash="a" * 64, dedup_key=uuid.uuid4().hex * 2,
                   input_data={"not_read": "MUST_NOT_APPEAR"}, evidence_refs=[], limitations=[], created_at=CREATED,
                   completed_at=CREATED + timedelta(seconds=1))
        self.db.add(ai)
        self.db.flush()
        run.stages = [{"ai_run_id": str(ai.id), "role": "market_events"}]
        for cost in (Decimal("0.125"), None):
            self.db.add(AiAttempt(run_id=ai.id, owner_id=owner.id, connection_id=connection.id, purpose="generation",
                                  service_name="Synthetic", model="synthetic", state="succeeded", started_at=CREATED,
                                  sent_at=CREATED, completed_at=CREATED + timedelta(seconds=1), elapsed_ms=1000,
                                  input_tokens=100, output_tokens=10, cost_cny=cost, request_count=1, safe_details={}))
        self.db.flush()
        with patch.object(ev, "now", return_value=CUTOFF), patch.object(ev, "available_trade_days", return_value=[DAY]):
            result = ev.shadow_evaluation(self.db, owner.id)
        self.assertEqual(result["model_usage"]["actual_request_count"], 2)
        self.assertIsNone(result["model_usage"]["cost_cny"]["total"])
        self.assertEqual(result["model_usage"]["cost_cny"]["known_total"], "0.125")
        self.assertEqual(result["evidence"]["narrative_reference_coverage_pct"], "100")
        self.assertNotIn("MUST_NOT_APPEAR", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
