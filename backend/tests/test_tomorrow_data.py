"""Dossier boundaries; all market values here are synthetic test fixtures.

Pure tests use unittest and do not access a database or a vendor. The repository
can also run them in its guarded disposable PostgreSQL pytest suite.
"""

import copy
import json
import os
import unittest
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.tomorrow import calculations as calc
from app.tomorrow import data

DAY = date(2026, 10, 23)
CUTOFF = datetime.fromisoformat("2026-10-23T22:30:00+08:00")
REF_ID = str(uuid.UUID(int=1))
SOURCE_ID = str(uuid.UUID(int=2))
REF = {"id": REF_ID, "content_hash": "a" * 64, "source_id": SOURCE_ID,
       "source_name": "Synthetic fixture", "received_at": "2026-10-23T20:00:00+08:00",
       "source_row": 0, "row_index_base": 0}


def quote(code="600001.SH", day=DAY, close="10", change="0"):
    return {"ts_code": code, "trade_date": day.strftime("%Y%m%d"), "open": "10", "high": "11",
            "low": "9", "close": close, "pre_close": "10", "pct_chg": change,
            "amount": "100", "vol": "100", "row_reference": copy.deepcopy(REF)}


class CalculationTests(unittest.TestCase):
    def test_missing_window_session_does_not_collapse_to_last_observations(self):
        days = [DAY - timedelta(days=i) for i in range(6, -1, -1)]
        history = {day: quote(day=day) for day in days}
        del history[days[-3]]
        result = calc.price_returns(history, days)
        self.assertEqual(result["1"]["value"], "0")
        self.assertIsNone(result["5"]["value"])
        self.assertEqual(result["5"]["observed_sessions"], 5)
        self.assertEqual(result["5"]["required_sessions"], 6)
        self.assertIsNone(result["20"]["value"])

    def test_factor_adjustment_requires_all_required_factor_sessions(self):
        days = [DAY - timedelta(days=i) for i in range(5, -1, -1)]
        history = {day: quote(day=day, close="20" if day != DAY else "10") for day in days}
        factors = {day: {"adj_factor": "1" if day != DAY else "2", "row_reference": REF} for day in days}
        result = calc.price_returns(history, days, factors=factors)["5"]
        self.assertEqual(result["value"], "0")
        self.assertEqual(result["adjustment"], "factor_adjusted_price")
        del factors[days[2]]
        result = calc.price_returns(history, days, factors=factors)["5"]
        self.assertEqual(result["value"], "-50")
        self.assertEqual(result["adjustment"], "unadjusted")
        self.assertIn("未复权", result["limitation"])

    def test_support_reference_excludes_current_session(self):
        days = [DAY - timedelta(days=i) for i in range(20, -1, -1)]
        history = {day: quote(day=day) for day in days}
        history[DAY].update(high="100", low="1")
        result = calc.support_resistance(history, days)
        self.assertEqual(result["support_candidate"], "9")
        self.assertEqual(result["resistance_candidate"], "11")
        self.assertIn("未批准", result["usage"])

    def test_exact_limits_and_missing_quotes_are_not_synthetic_prices(self):
        current = quote(close="10.5", change="5")
        record = calc.stock_record("600001.SH", None, {DAY: current}, [DAY],
                                   cap={"up_limit": "10.5", "down_limit": "9.5", "row_reference": REF})
        self.assertTrue(record["calculations"]["price_verified_limit_up"])
        missing = calc.stock_record("600002.SH", None, {}, [DAY])
        self.assertIsNone(missing["facts"]["close"]["value"])
        self.assertIsNone(missing["calculations"]["price_verified_limit_up"])
        self.assertIsNone(missing["calculations"]["volume_to_prior5_mean"]["value"])

    def test_invalid_numbers_never_enter_json(self):
        for value in (None, True, "NaN", "Infinity", "--", "invalid"):
            self.assertIsNone(calc.number(value))
        self.assertEqual(calc.text(calc.number("0")), "0")

    def test_sector_returns_compound_disclosed_changes(self):
        days = [DAY - timedelta(days=i) for i in range(4, -1, -1)]
        history = {day: {"pct_change": "10", "row_reference": REF} for day in days}
        result = calc.compounded_theme_returns(history, days)
        self.assertEqual(result["5"]["value"], "61.051")
        self.assertIsNone(result["20"]["value"])
        del history[days[2]]
        self.assertIsNone(calc.compounded_theme_returns(history, days)["5"]["value"])

    def test_relative_strength_reuses_canonical_windows_without_expanding_sources(self):
        metric = {"value": "12", "evidence": [{**REF, "source_row": row} for row in range(60)]}
        sector = {"value": "5", "evidence": [{**REF, "source_row": row} for row in range(60)]}
        result = calc.relative_strength({"60": metric}, {"60": sector})["60"]
        self.assertEqual(result["value"], "7")
        self.assertEqual(result["input_windows"], {"stock": "calculations.returns_pct.60", "sector": "calculations.returns_pct.60"})
        self.assertNotIn("evidence_refs", result)
        self.assertNotIn("evidence", result)
        self.assertLess(len(json.dumps(result)), 400)

    def test_duplicate_identity_is_not_silently_selected(self):
        indexed, conflicts = calc.index_rows([quote(), quote(close="11")])
        self.assertEqual(indexed, {})
        self.assertEqual(conflicts, ["600001.SH"])

    def test_market_reuses_yesterday_fixed_cohort_and_yuan_units(self):
        previous, prior = DAY - timedelta(days=1), DAY - timedelta(days=2)
        cap = {"ts_code": "600001.SH", "trade_date": DAY.strftime("%Y%m%d"), "up_limit": "11", "down_limit": "9", "row_reference": REF}
        tables = {
            ("daily", DAY): {"state": "complete", "rows": [quote(close="11", change="10")]},
            ("daily", previous): {"state": "complete", "rows": [quote(day=previous)]},
            ("limits", DAY): {"state": "complete", "rows": [cap]},
            ("pools", DAY): {"state": "complete", "rows": []},
            ("pools", previous): {"state": "complete", "rows": [{"ts_code": "600001.SH", "trade_date": previous.strftime("%Y%m%d"), "limit": "U", "limit_times": "1", "row_reference": REF}]},
        }
        result = calc.market_metrics(DAY, tables, [prior, previous, DAY])
        self.assertEqual(result["turnover"]["amount_yuan"], "100000")
        self.assertEqual(result["relay"]["yesterday_limit_up"]["N"], 1)
        self.assertEqual(result["relay"]["yesterday_limit_up"]["mean_close_pct"], "10")
        self.assertEqual(result["breadth"]["up_share_pct"], "100")


class DossierTests(unittest.TestCase):
    def setUp(self):
        self.db, self.owner = object(), uuid.uuid4()
        self.days = [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY]
        self.tables = {}
        self.enterContext(patch.object(data, "_permitted_providers", return_value={}))
        self.enterContext(patch.object(data, "available_trade_days", side_effect=lambda *args, **kwargs: self.days))
        self.enterContext(patch.object(data, "_target_day", return_value=(date(2026, 10, 26), True)))
        self.enterContext(patch.object(data, "_calendar_refs", return_value=[]))
        self.enterContext(patch.object(data, "_ordinary", return_value=[]))
        self.enterContext(patch.object(data, "_manual_context", return_value=(None, [], [])))
        self.loader = self.enterContext(patch.object(data, "load_dataset", side_effect=self.fetch))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")))

    def fetch(self, db, dataset, day, scope="all", as_of_time=None):
        self.assertIs(db, self.db)
        self.assertEqual(as_of_time, CUTOFF)
        return copy.deepcopy(self.tables.get((dataset, day, scope), {"state": "missing", "rows": []}))

    def put(self, dataset, rows, day=DAY, scope="all"):
        self.tables[(dataset, day, scope)] = {"state": "complete", "rows": rows,
             "row_count": len(rows), "source_id": SOURCE_ID, "received_at": REF["received_at"],
             "evidence_refs": [{**REF, "start_row": 0, "row_count": len(rows)}]}

    def core(self, n=1):
        rows = [quote(f"{600001+i:06d}.SH") for i in range(n)]
        caps = [{"ts_code": row["ts_code"], "trade_date": DAY.strftime("%Y%m%d"), "up_limit": "11", "down_limit": "9"} for row in rows]
        pools = [{"ts_code": row["ts_code"], "name": "Synthetic", "trade_date": DAY.strftime("%Y%m%d"), "limit": "U", "limit_times": "1", "amount": "100", "fd_amount": "1"} for row in rows]
        self.put("daily", rows)
        self.put("limits", caps)
        self.put("pools", pools)

    def test_full_pool_is_not_workbench_fifty_stock_slice(self):
        self.core(75)
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["stocks"]), 75)
        self.assertEqual(result["market"]["sample"]["N"], 75)
        self.assertTrue(result["stocks"][0]["evidence_refs"])
        self.assertTrue(result["market"]["evidence_refs"])
        self.assertEqual(result["as_of_time"], "2026-10-23T14:30:00Z")
        json.dumps(result, allow_nan=False)

    def test_missing_core_blocks_instead_of_inventing_empty_market(self):
        self.put("daily", [quote()])
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertFalse(result["ready"])
        self.assertIsNone(result["market"]["limits"]["supplier_reported"]["up"])
        self.assertTrue(any(item["dataset"] == "review.limits" and item["state"] == "missing" for item in result["coverage"]))

    def test_unverified_calendar_does_not_guess_weekdays(self):
        self.days = []
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertFalse(result["ready"])
        self.loader.assert_not_called()

    def test_cutoff_is_required_and_future_day_is_rejected(self):
        with self.assertRaises(ValueError):
            data.build_bundle(self.db, self.owner, DAY, None, datetime(2026, 10, 23))
        with self.assertRaises(ValueError):
            data.build_bundle(self.db, self.owner, DAY + timedelta(days=1), None, CUTOFF)
        with self.assertRaises(ValueError):
            data.build_bundle(self.db, self.owner, DAY, DAY, CUTOFF)

    def test_one_transaction_reader_does_not_reload_same_dataset(self):
        reader = data._Reader(self.db, CUTOFF, {})
        reader.table("daily", DAY)
        reader.table("daily", DAY)
        self.assertEqual(self.loader.call_count, 1)

    def test_selected_history_preserves_original_part_row_and_full_coverage_count(self):
        rows = [quote(f"{600001+i:06d}.SH") for i in range(10)]
        self.put("daily", rows)
        part1, part2 = str(uuid.UUID(int=91)), str(uuid.UUID(int=92))
        self.tables[("daily", DAY, "all")]["evidence_refs"] = [
            {**REF, "id": part1, "start_row": 0, "row_count": 3},
            {**REF, "id": part2, "start_row": 3, "row_count": 7},
        ]
        reader = data._Reader(self.db, CUTOFF, {})
        result = reader.table("daily", DAY, symbols={"600002.SH", "600009.SH"})
        self.assertEqual([row["ts_code"] for row in result["rows"]], ["600002.SH", "600009.SH"])
        self.assertEqual(result["rows"][1]["row_reference"]["id"], part2)
        self.assertEqual(result["rows"][1]["row_reference"]["source_row"], 5)
        self.assertEqual(reader.coverage[0]["row_count"], 10)
        self.assertEqual(reader.coverage[0]["retained_row_count"], 2)
        self.assertEqual(reader.coverage[0]["selection_scope"], "candidate_symbols")
        self.assertEqual(len(reader.table("daily", DAY)["rows"]), 10)

    def test_selected_history_validates_nonselected_duplicate_rows(self):
        self.put("daily", [quote(), quote("600002.SH"), quote("600002.SH")])
        result = data._Reader(self.db, CUTOFF, {}).table("daily", DAY, symbols={"600001.SH"})
        self.assertEqual(result["state"], "broken_lineage")
        self.assertEqual(result["reason"], "duplicate_identity")
        self.assertEqual(result["rows"], [])

    def test_full_candidates_are_recalled_before_older_history_is_retained(self):
        self.days = [DAY - timedelta(days=i) for i in range(62, -1, -1)]
        self.core(75)
        original_fetch = self.fetch

        def generate(db, dataset, day, scope="all", as_of_time=None):
            if dataset == "daily":
                rows = [{k: value for k, value in quote(f"{600001+i:06d}.SH", day=day).items() if k != "row_reference"} for i in range(500)]
                return {"state": "complete", "rows": rows, "row_count": 500, "source_id": SOURCE_ID,
                        "evidence_refs": [{**REF, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, str(day))), "start_row": 0, "row_count": 500}]}
            if dataset == "limits":
                return {"state": "complete", "rows": [{"ts_code": f"{600001+i:06d}.SH", "trade_date": day.strftime("%Y%m%d"), "up_limit": "11", "down_limit": "9"} for i in range(500)],
                        "row_count": 500, "source_id": SOURCE_ID, "evidence_refs": [{**REF, "start_row": 0, "row_count": 500}]}
            return original_fetch(db, dataset, day, scope, as_of_time)

        self.loader.side_effect = generate
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertEqual(len(result["stocks"]), 75)
        coverage = [item for item in result["coverage"] if item["dataset"] == "review.daily"]
        full = [item for item in coverage if item["selection_scope"] == "full_stored_table"]
        selected = [item for item in coverage if item["selection_scope"] == "candidate_symbols"]
        self.assertEqual(len(full), 3)
        self.assertEqual(len(selected), 60)
        self.assertTrue(all(item["retained_row_count"] == 75 and item["row_count"] == 500 for item in selected))
        self.assertEqual(result["metrics"]["retained_daily_rows"], 3 * 500 + 60 * 75)
        self.assertEqual(result["stocks"][0]["calculations"]["returns_pct"]["60"]["state"], "complete")

    def test_unknown_or_revoked_source_rights_are_not_readable(self):
        rights = {"display": "allowed", "cache": "allowed"}
        provider = SimpleNamespace(configured=True, enabled=True, entitlement_status="verified", rights=rights)
        descriptor = {"configured": True, "enabled": True, "entitlement_status": "verified", "rights": dict(rights)}
        self.assertTrue(data._permission(provider, descriptor))
        descriptor["rights"]["cache"] = "unknown"
        self.assertFalse(data._permission(provider, descriptor))
        descriptor["rights"]["cache"] = "allowed"
        provider.enabled = False
        self.assertFalse(data._permission(provider, descriptor))

    def test_news_segments_are_joined_and_date_only_announcements_remain_date_only(self):
        rows = [{"id": "news1", "published_at": "2026-10-23T18:00:00+08:00", "title": "Synthetic news",
                 "text": "甲" * 50, "part_number": "0", "row_reference": REF},
                {"id": "news1", "published_at": "2026-10-23T18:00:00+08:00", "title": "Synthetic news",
                 "text": "乙" * 50, "part_number": "1", "row_reference": {**REF, "source_row": 1}}]
        announcement = {"id": "announcement1", "published_at": None, "published_day": "2026-10-23",
                        "title": "Synthetic filing", "text": "丙" * 100, "text_status": "extracted",
                        "part_number": "0", "document_sha256": "b" * 64, "row_reference": REF}
        events = data._events([], {}, [], {"tomorrow.news_window": rows, "tomorrow.announcement_text": [announcement]}, CUTOFF, {})
        self.assertEqual(len(events), 2)
        news = next(event for event in events if event["kind"] == "news")
        self.assertEqual(news["content"], "甲" * 50 + "乙" * 50)
        self.assertTrue(news["content_sufficient"])
        self.assertEqual(news["evidence"][0]["source_row_ranges"], [[0, 1]])
        event = next(event for event in events if event["kind"] == "announcement")
        self.assertIsNone(event["published_at"])
        self.assertEqual(event["time_precision"], "date_only")

    def test_empty_announcement_metadata_does_not_duplicate_downloaded_pdf(self):
        metadata = {"id": "filing1", "published_day": str(DAY), "title": "Synthetic filing",
                    "text": None, "text_status": "not_fetched", "part_number": "0", "document_sha256": None,
                    "row_reference": {**REF, "id": str(uuid.UUID(int=4))}}
        downloaded = {**metadata, "text": "正文" * 100, "text_status": "extracted", "document_sha256": "b" * 64,
                      "row_reference": {**REF, "id": str(uuid.UUID(int=5))}}
        events = data._events([], {}, [], {"tomorrow.announcement_text": [metadata, downloaded]}, CUTOFF, {})
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["content_sufficient"])
        self.assertEqual(events[0]["evidence_refs"], [str(uuid.UUID(int=5))])

    def test_a_new_shorter_article_version_never_inherits_old_trailing_parts(self):
        old_ref, new_ref = {**REF, "id": str(uuid.UUID(int=4)), "received_at": "2026-10-23T18:00:00+08:00"}, {**REF, "id": str(uuid.UUID(int=5)), "received_at": "2026-10-23T19:00:00+08:00"}
        base = {"id": "news1", "published_at": "2026-10-23T18:00:00+08:00", "title": "Synthetic news"}
        rows = [{**base, "text": "旧" * 100, "part_number": "0", "row_reference": old_ref},
                {**base, "text": "旧尾" * 100, "part_number": "1", "row_reference": {**old_ref, "source_row": 1}},
                {**base, "text": "新" * 100, "part_number": "0", "row_reference": new_ref}]
        events = data._events([], {}, [], {"tomorrow.news_window": rows}, CUTOFF, {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["content"], "新" * 100)

    def test_missing_or_future_news_and_title_only_text_cannot_support_interpretation(self):
        raw = {"id": "n", "title": "Synthetic headline", "summary": "Synthetic headline", "published_at": "2026-10-23T21:00:00+08:00"}
        event = data._event(raw, REF, cutoff=CUTOFF)
        self.assertFalse(event["content_sufficient"])
        self.assertIsNone(data._event({**raw, "published_at": "2026-10-24T01:00:00+08:00"}, REF, cutoff=CUTOFF))
        self.assertIsNone(data._event({**raw, "modified_at": "2026-10-24T01:00:00+08:00"}, REF, cutoff=CUTOFF))

    def test_financial_rows_need_actual_announcement_before_cutoff(self):
        source = uuid.UUID(SOURCE_ID)
        provider = SimpleNamespace(name="Synthetic")
        record = SimpleNamespace(kind="reference", payload={"dataset": "tushare.income", "columns": [], "rows": [
            {"ts_code": "600001.SH", "end_date": "20260630", "ann_date": "20261024"},
            {"ts_code": "600002.SH", "end_date": "20260630", "ann_date": "20261020", "f_ann_date": "20261024"},
            {"ts_code": "600003.SH", "end_date": "20260630", "ann_date": "20261020"},
            {"ts_code": "600004.SH", "end_date": "20260630"},
        ]}, source_id=source, id=uuid.UUID(REF_ID), content_hash="a" * 64, received_at=CUTOFF,
        published_at=None, available_at=None, source_key="income", source_revision="1", provenance="source_reported")
        rows = data._reference_rows([record], {source: provider}, CUTOFF, DAY, {})["tushare.income"]
        self.assertEqual([row["ts_code"] for row in rows], ["600003.SH"])
        self.assertEqual(rows[0]["announced_day"], "2026-10-20")

    def test_nonlimit_sector_candidate_requires_actual_price_limits(self):
        self.core(2)
        self.put("themes", [{"ts_code": "BK0001.DC", "name": "Synthetic sector", "trade_date": DAY.strftime("%Y%m%d"), "pct_change": "1"}])
        self.put("members", [{"ts_code": "BK0001.DC", "con_code": "600003.SH", "name": "Third", "trade_date": DAY.strftime("%Y%m%d")}])
        self.tables[("daily", DAY, "all")]["rows"].append(quote("600003.SH", close="11", change="10"))
        # Not in the vendor U pool, but actual limit-price evidence says sealed.
        self.tables[("limits", DAY, "all")]["rows"].append({"ts_code": "600003.SH", "trade_date": DAY.strftime("%Y%m%d"), "up_limit": "11", "down_limit": "9"})
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertNotIn("600003.SH", [stock["code"] for stock in result["stocks"]])

    def test_future_calendar_uses_announced_schedule_fields_not_reporting_periods(self):
        supplemental = {"tushare.dividend": [{"ts_code": "600001.SH", "end_date": "20271231", "announced_day": "2026-10-22",
                                             "record_date": "20261026", "ex_date": "20261027", "pay_date": "20261028",
                                             "row_reference": REF}]}
        events = data._future_events(supplemental, CUTOFF)
        self.assertEqual([event["date"] for event in events], ["2026-10-26", "2026-10-27", "2026-10-28"])
        self.assertTrue(all(event["status"] == "announced_schedule" for event in events))
        self.assertTrue(all(event["time_precision"] == "date_only" for event in events))

    def test_lhb_and_seats_share_stock_identity_and_evidence(self):
        self.core()
        seat_ref = str(uuid.UUID(int=88))
        self.put("lhb", [{"ts_code": "600001.SH", "trade_date": DAY.strftime("%Y%m%d"), "net_amount": "100", "reason": "Synthetic"}])
        self.put("seats", [{"ts_code": "600001.SH", "trade_date": DAY.strftime("%Y%m%d"), "exalter": "Synthetic seat", "buy": "100", "sell": "0"}])
        self.tables[("seats", DAY, "all")]["evidence_refs"][0]["id"] = seat_ref
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        stock = result["stocks"][0]
        self.assertEqual(len(stock["lhb"]), 1)
        self.assertEqual(len(stock["lhb_seats"]), 1)
        self.assertIn(seat_ref, stock["evidence_refs"])

    def test_compact_fund_series_and_relative_windows_keep_exact_provenance(self):
        self.core()
        theme_ids, fund_ids = [], []
        for index, day in enumerate(self.days):
            self.put("themes", [{"ts_code": "BK0001.DC", "name": "Synthetic sector", "trade_date": day.strftime("%Y%m%d"), "pct_change": "2"}], day)
            self.put("theme_funds", [{"ts_code": "BK0001.DC", "trade_date": day.strftime("%Y%m%d"), "net_amount": str(100 + index)}], day)
            theme_ids.append(str(uuid.UUID(int=101 + index)))
            fund_ids.append(str(uuid.UUID(int=201 + index)))
            self.tables[("themes", day, "all")]["evidence_refs"][0]["id"] = theme_ids[-1]
            self.tables[("theme_funds", day, "all")]["evidence_refs"][0]["id"] = fund_ids[-1]
        self.put("members", [{"ts_code": "BK0001.DC", "con_code": "600001.SH", "name": "Synthetic", "trade_date": DAY.strftime("%Y%m%d")}])
        result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        sector, stock = result["sectors"][0], result["stocks"][0]
        series = sector["calculations"]["net_amount_series"]
        self.assertEqual(series["columns"], ["trade_day", "net_amount", "evidence_id", "source_row"])
        self.assertEqual(series["rows"], [[str(day), str(100 + index), fund_ids[index], 0] for index, day in enumerate(self.days)])
        self.assertTrue(set(fund_ids).issubset(sector["evidence_refs"]))
        referenced_theme = sector["calculations"]["returns_pct"]["1"]["evidence"][0]
        self.assertEqual(referenced_theme["id"], theme_ids[-1])
        self.assertIn(theme_ids[-1], stock["evidence_refs"])
        root_refs = {ref["id"]: ref for ref in result["evidence_refs"]}
        self.assertEqual(root_refs[referenced_theme["id"]]["content_hash"], REF["content_hash"])
        self.assertEqual(stock["calculations"]["relative_to_sectors"][0]["sector_code"], sector["code"])

    def test_recompacting_ranges_is_lossless_and_keeps_source_metadata_only_at_root(self):
        first = data._compact([{**REF, "source_row": 0}, {**REF, "source_row": 1}])
        second = data._compact(first + [{**REF, "source_row": 3}])
        self.assertEqual(second[0]["source_row_ranges"], [[0, 1], [3, 3]])
        self.assertEqual(second[0]["row_index_base"], 0)
        self.assertNotIn("content_hash", second[0])
        self.assertEqual(data._compact(second), second)

    def test_duplicate_articles_register_all_sources_and_preserve_complete_text(self):
        source = uuid.UUID(SOURCE_ID)
        providers = {source: SimpleNamespace(name="Synthetic")}
        body = "完整缓存正文" * 100
        records = [SimpleNamespace(kind="news", source_id=source, source_key="news",
                    id=uuid.UUID(int=301 + index), content_hash=str(index) * 64, received_at=CUTOFF,
                    published_at=CUTOFF, available_at=None, source_revision="1", provenance="source_reported",
                    payload={"title": "Synthetic duplicate", "text": body}) for index in range(2)]
        root_refs = {}
        result = data._events(records, providers, [], {}, CUTOFF, root_refs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], body)
        self.assertEqual(set(result[0]["evidence_refs"]), set(root_refs))
        self.assertTrue(all(ref["content_hash"] for ref in root_refs.values()))
        self.assertTrue(all("content_hash" not in ref for ref in result[0]["evidence"]))

    def test_consumed_segment_inputs_keep_original_coverage_and_joined_content(self):
        self.core()
        segments = [{"id": "fixture-article", "published_at": CUTOFF.isoformat(), "title": "Synthetic segment",
                     "text": part, "part_number": str(index), "row_reference": {**REF, "source_row": index}}
                    for index, part in enumerate(["甲" * 100, "乙" * 100])]
        with patch.object(data, "_reference_rows", side_effect=lambda *args: {"tomorrow.news_window": copy.deepcopy(segments)}):
            result = data.build_bundle(self.db, self.owner, DAY, None, CUTOFF)
        self.assertEqual(result["events"][0]["content"], "甲" * 100 + "乙" * 100)
        coverage = next(item for item in result["coverage"] if item["dataset"] == "tomorrow.news_window")
        self.assertEqual(coverage["row_count"], 2)
        self.assertEqual(coverage["evidence_refs"], [REF_ID])


@unittest.skipUnless(os.getenv("JL_ALLOW_TEST_DATABASE_RESET") == "yes", "requires guarded disposable PostgreSQL")
class PostgreSQLDossierTests(unittest.TestCase):
    """Integration checks use only the repository's explicitly disposable DB."""

    def setUp(self):
        from sqlalchemy.orm import Session

        from app.db import engine

        self.db = self.enterContext(Session(engine()))
        self.transaction = self.db.begin()
        self.addCleanup(self.transaction.rollback)

    def source(self, family):
        from datetime import UTC

        from app.evidence.models import SourceVersion
        from app.market.review_store import family_id
        from app.models import Provider

        identity = family_id(family)
        provider = self.db.get(Provider, identity)
        if provider is None:
            provider = Provider(id=identity, family=family, name="Synthetic " + family, kind="api")
            self.db.add(provider)
        provider.configured = provider.enabled = True
        provider.entitlement_status = "verified"
        provider.adapter_version = "synthetic-test"
        provider.capabilities = []
        provider.rights = {"display": "allowed", "cache": "allowed", "export": "unknown", "redistribute": "unknown"}
        self.db.flush()
        descriptor = {"configured": True, "enabled": True, "entitlement_status": "verified", "rights": provider.rights}
        self.db.add(SourceVersion(source_id=identity, recorded_at=datetime(1970, 1, 1, tzinfo=UTC), descriptor=descriptor, content_hash="b" * 64))
        self.db.flush()
        return provider

    def evidence(self, provider, kind, payload, stamp, key, *, owner_id=None, published_at=None):
        from app.evidence.common import content_hash
        from app.evidence.models import EvidenceVersion

        row = EvidenceVersion(source_id=provider.id, owner_id=owner_id,
                              visibility="private" if owner_id else "shared", source_key=key,
                              source_revision=uuid.uuid4().hex, version_number=1, kind=kind,
                              event_at=None, published_at=published_at, received_at=stamp, available_at=None,
                              availability="unknown", provenance="source_reported", historical_eligible=False,
                              rights_basis="Synthetic isolated test only", payload=payload, content_hash=content_hash(payload))
        self.db.add(row)
        self.db.flush()
        return row

    def dataset(self, dataset, rows, stamp):
        provider = self.source(data.DATASET_FAMILIES[dataset])
        payload = {"dataset": "review." + dataset, "title": "Synthetic", "source_url": "https://example.invalid/fixture",
                   "attribution": "Synthetic", "scope": "Synthetic isolated fixture", "columns": [], "rows": rows}
        part = self.evidence(provider, "reference", payload, stamp, "part:" + dataset)
        entry = {"dataset": dataset, "scope": "all", "part_number": "0", "row_count": str(len(rows)),
                 "part_id": str(part.id), "part_hash": part.content_hash, "requested_day": str(DAY), "start_day": str(DAY)}
        manifest = {**payload, "dataset": "review.manifest", "rows": [entry]}
        self.evidence(provider, "reference", manifest, stamp, f"review.manifest:{dataset}:{DAY}:all")

    def base(self):
        from app.models import User

        owner = User(username="synthetic-dossier-" + uuid.uuid4().hex, password_hash="disabled-fixture", role="admin")
        self.db.add(owner)
        self.db.flush()
        stamp = CUTOFF - timedelta(hours=2)
        for family, exchange in (("tushare_calendar_sse", "SSE"), ("tushare_calendar_szse", "SZSE")):
            source = self.source(family)
            for day in [DAY - timedelta(days=2), DAY - timedelta(days=1), DAY, date(2026, 10, 26)]:
                self.evidence(source, "calendar", {"exchange": exchange, "trade_day": str(day), "is_open": True, "segments": []}, stamp, f"calendar:{exchange}:{day}")
        self.dataset("daily", [{k: v for k, v in quote().items() if k != "row_reference"}], stamp)
        self.dataset("limits", [{"ts_code": "600001.SH", "trade_date": DAY.strftime("%Y%m%d"), "up_limit": "11", "down_limit": "9"}], stamp)
        self.dataset("pools", [], stamp)
        return owner, stamp

    def test_real_manifest_cutoff_and_read_only_bundle(self):
        from sqlalchemy import func, select

        from app.evidence.models import EvidenceVersion

        owner, stamp = self.base()
        count = self.db.scalar(select(func.count()).select_from(EvidenceVersion))
        result = data.build_bundle(self.db, owner.id, DAY, None, CUTOFF)
        self.assertTrue(result["ready"])
        self.assertEqual(result["target_trade_day"], "2026-10-26")
        self.assertEqual(result["market"]["turnover"]["amount_yuan"], "100000")
        self.assertTrue(result["evidence_refs"])
        before = data.build_bundle(self.db, owner.id, DAY, None, stamp - timedelta(seconds=1))
        self.assertFalse(before["ready"])
        self.assertEqual(count, self.db.scalar(select(func.count()).select_from(EvidenceVersion)))

    def test_real_cache_excludes_foreign_private_and_late_news(self):
        from app.models import User

        owner, stamp = self.base()
        other = User(username="synthetic-other-" + uuid.uuid4().hex, password_hash="disabled-fixture", role="admin")
        self.db.add(other)
        self.db.flush()
        provider = self.source("synthetic_news")
        payload = {"title": "Synthetic article", "summary": "正文" * 100, "source_url": "https://example.invalid/news"}
        own = self.evidence(provider, "news", payload, stamp, "own", owner_id=owner.id, published_at=stamp)
        self.evidence(provider, "news", {**payload, "title": "Foreign private"}, stamp, "foreign", owner_id=other.id, published_at=stamp)
        self.evidence(provider, "news", {**payload, "title": "Received too late"}, CUTOFF + timedelta(seconds=1), "late", published_at=stamp)
        result = data.build_bundle(self.db, owner.id, DAY, None, CUTOFF)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["evidence_refs"], [str(own.id)])
        self.assertNotIn("Foreign private", json.dumps(result))

    def test_real_revocation_hides_cache_and_blocks_core(self):
        from app.models import Provider

        owner, _ = self.base()
        provider = self.db.get(Provider, data.family_id(data.DATASET_FAMILIES["daily"]))
        provider.rights = {**provider.rights, "cache": "unknown"}
        self.db.flush()
        result = data.build_bundle(self.db, owner.id, DAY, None, CUTOFF)
        self.assertFalse(result["ready"])
        self.assertEqual(result["stocks"], [])
        self.assertTrue(any(item["dataset"] == "review.daily" and item["state"] == "permission_denied" for item in result["coverage"]))


if __name__ == "__main__":
    unittest.main()
