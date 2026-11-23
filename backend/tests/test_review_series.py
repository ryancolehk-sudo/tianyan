"""Bounded history contracts over explicit synthetic fixtures, without I/O."""

import copy
import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from app.market import review_read as read
from app.market import review_series as series

DAY = date(2026, 10, 23)
PREVIOUS = date(2026, 10, 22)
PRIOR = date(2026, 10, 21)


def quote(day, **changes):
    return {"ts_code": "600001.SH", "trade_date": day.strftime("%Y%m%d"), "open": "10",
            "close": "10.1", "high": "10.2", "pre_close": "10", "pct_chg": "1", "amount": "10", **changes}


class ReviewSeriesTests(unittest.TestCase):
    def setUp(self):
        self.db = object()  # No real database method can be invoked.
        self.days = [PRIOR, PREVIOUS, DAY]
        self.tables = {}
        self.enterContext(patch.object(read, "_current_snapshot", return_value=None))
        self.enterContext(patch.object(read, "_current_trade_day", return_value=None))
        self.full = False
        self.revoked = False
        self.calendar = self.enterContext(patch.object(read, "available_trade_days", side_effect=self.open_days))
        self.load = self.enterContext(patch.object(read, "load_dataset", side_effect=self.fetch))
        self.enterContext(patch('app.market.review_fallback.pool_table', return_value=None))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")))

    def open_days(self, db, limit=63, end_day=None, as_of_time=None):
        self.assertIs(db, self.db)
        return [day for day in self.days if end_day is None or day <= end_day][-limit:]

    def put(self, dataset, day, rows, scope="all", state="complete"):
        table = {"state": state, "rows": rows, "row_count": len(rows), "source_id": f"source-{dataset}",
                 "received_at": "2026-10-24T10:00:00+08:00", "requested_day": day.isoformat(),
                 "evidence_refs": [{"id": f"{dataset}:{day}:{scope}", "content_hash": "fixture-hash",
                                    "source_id": f"source-{dataset}", "received_at": "2026-10-24T10:00:00+08:00",
                                    "start_row": 0, "row_count": len(rows)}]}
        self.tables[(dataset, day, scope)] = table
        return table

    def default_rows(self, dataset, day, scope):
        if dataset == "daily":
            return [quote(day)]
        if dataset == "limits":
            return [{"ts_code": "600001.SH", "trade_date": day.strftime("%Y%m%d"),
                     "up_limit": "11", "down_limit": "9"}]
        if dataset == "indices":
            return [{"ts_code": scope, "trade_date": day.strftime("%Y%m%d"), "close": "100", "pct_chg": "1"}]
        return []

    def fetch(self, db, dataset, day, scope="all", as_of_time=None, include_rows=True):
        self.assertIs(db, self.db)
        key = (dataset, day, scope)
        if key not in self.tables and self.full:
            self.put(dataset, day, self.default_rows(dataset, day, scope), scope)
        table = copy.deepcopy(self.tables.get(key, {"state": "missing", "rows": []}))
        if self.revoked:
            table["state"] = "permission_denied"
        if as_of_time and table.get("received_at") and datetime.fromisoformat(table["received_at"]) > as_of_time:
            return {"state": "missing", "reason": "not_received_by_cutoff"}
        return table

    def test_lookback_is_bounded_before_any_read(self):
        for lookback in (0, 19, 21, 61, True, "20"):
            with self.subTest(lookback=lookback), self.assertRaises(ValueError):
                series.series_view(self.db, DAY, lookback)
        self.calendar.assert_not_called()
        self.load.assert_not_called()

    def test_unknown_requested_or_not_yet_closed_day_never_uses_another_day(self):
        for target in (DAY + timedelta(days=1), DAY):
            self.days = [PRIOR, PREVIOUS]
            result = series.series_view(self.db, target)
            self.assertEqual(result["shared_date"], target.isoformat())
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["records"], [])
            self.assertEqual(result["trade_days"], [])
        self.load.assert_not_called()

    def test_no_calendar_returns_no_guessed_default_date(self):
        self.days = []
        result = series.series_view(self.db)
        self.assertIsNone(result["shared_date"])
        self.assertEqual(result["records"], [])
        self.load.assert_not_called()

    def test_twenty_days_are_ascending_and_have_three_calendar_warmup_days(self):
        # This deliberately irregular fixture is the calendar authority; the
        # projection must not manufacture intervening weekdays.
        self.days = sorted(DAY - timedelta(days=2 * index) for index in range(23))
        self.full = True
        result = series.series_view(self.db, DAY)
        self.assertEqual(self.calendar.call_args.kwargs["limit"], 23)
        self.assertEqual(result["returned_day_count"], 20)
        self.assertEqual(result["trade_days"], [day.isoformat() for day in self.days[-20:]])
        self.assertEqual(result["warmup_trade_days"], [day.isoformat() for day in self.days[:3]])
        self.assertTrue(result["warmup_calendar_complete"])
        self.assertTrue(result["calendar_window_complete"])
        first = result["records"][0]
        self.assertEqual(first["metrics"]["calendar"]["previous_trade_day"], self.days[2].isoformat())
        self.assertEqual(first["metrics"]["calendar"]["prior_trade_day"], self.days[1].isoformat())
        self.assertEqual(result["state"], "complete")
        self.assertTrue(all(call.args[2] in self.days for call in self.load.call_args_list))

    def test_sixty_days_read_each_dependency_once_with_bounded_raw_cache(self):
        self.days = sorted(DAY - timedelta(days=2 * index) for index in range(63))
        self.full = True
        cache_sizes = []

        class TrackingReader(read._Reader):
            def get(self, *args, **kwargs):
                result = super().get(*args, **kwargs)
                cache_sizes.append(len(self.cache))
                return result

        with patch.object(series, "_Reader", TrackingReader):
            result = series.series_view(self.db, DAY, 60)
        keys = [(call.args[1], call.args[2], call.kwargs["scope"]) for call in self.load.call_args_list]
        self.assertEqual(len(result["records"]), 60)
        self.assertEqual(self.calendar.call_args.kwargs["limit"], 63)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({key[0] for key in keys}, {"daily", "limits", "pools", "indices", "themes"})
        self.assertLessEqual(max(cache_sizes), 14)
        self.assertEqual(len(keys), 60 * 10 + 4)
        # Includes per-date dependency coverage and immutable references, but
        # must not grow into raw-table/sample-array response sizes.
        self.assertLess(len(json.dumps(result)), 2_000_000)

    def test_missing_date_stays_in_series_with_unknown_metrics_and_short_window(self):
        self.full = True
        self.put("daily", PREVIOUS, [], state="missing")
        result = series.series_view(self.db, DAY)
        self.assertEqual([record["trade_day"] for record in result["records"]], [day.isoformat() for day in self.days])
        missing = result["records"][1]["metrics"]
        self.assertIsNone(missing["sample"]["N"])
        self.assertIsNone(missing["breadth"]["up"])
        self.assertIsNone(missing["turnover"]["amount_yuan"])
        self.assertFalse(result["calendar_window_complete"])
        self.assertFalse(result["warmup_calendar_complete"])
        self.assertEqual(result["state"], "partial")

    def test_cutoff_reaches_every_read_and_late_data_has_no_fallback(self):
        self.full = True
        review = series.series_view(self.db, DAY)
        self.assertEqual(review["records"][-1]["metrics"]["sample"]["N"], 1)
        self.load.reset_mock()
        cutoff = datetime.fromisoformat("2026-10-23T20:00:00+08:00")
        observed = series.series_view(self.db, DAY, as_of_time=cutoff)
        self.assertEqual(self.calendar.call_args.kwargs["as_of_time"], cutoff)
        self.assertTrue(all(call.kwargs["as_of_time"] == cutoff for call in self.load.call_args_list))
        self.assertEqual(observed["knowledge_mode"], "observed")
        self.assertEqual(observed["as_of_time"], cutoff.isoformat())
        self.assertIsNone(observed["records"][-1]["metrics"]["sample"]["N"])
        self.assertTrue(all(not record["evidence_refs"] for record in observed["records"]))
        with self.assertRaises(ValueError):
            series.series_view(self.db, DAY, as_of_time=datetime(2026, 10, 23))

    def test_revocation_is_rechecked_without_cross_request_result_cache(self):
        self.full = True
        first = series.series_view(self.db, DAY)
        self.assertTrue(first["records"][-1]["evidence_refs"])
        self.revoked = True
        self.load.reset_mock()
        revoked = series.series_view(self.db, DAY)
        self.assertGreater(self.load.call_count, 0)
        self.assertEqual(revoked["state"], "unavailable")
        for record in revoked["records"]:
            self.assertIsNone(record["metrics"]["sample"]["N"])
            self.assertEqual(record["evidence_refs"], [])
            self.assertTrue(all(not table["evidence_refs"] for table in record["coverage"]))
            self.assertTrue(all(index["record"] is None for index in record["indices"]))

    def test_wrong_day_evidence_is_not_reused_for_selected_day(self):
        self.full = True
        table = self.put("daily", DAY, [quote(PREVIOUS)])
        result = series.series_view(self.db, DAY)["records"][-1]
        self.assertIsNone(result["metrics"]["sample"]["N"])
        current = next(item for item in result["coverage"] if item["dataset"] == "daily" and item["trade_day"] == DAY.isoformat())
        self.assertEqual(current["state"], "broken_lineage")
        self.assertNotIn(table["evidence_refs"][0]["id"], {ref["id"] for ref in result["evidence_refs"]})

    def test_compacting_does_not_walk_samples_or_discard_denominators_ladder_or_unknowns(self):
        class DoNotIterate(list):
            def __iter__(self):
                raise AssertionError("sample arrays must not be copied or serialized")

        samples = DoNotIterate([{"ts_code": "fixture"}] * 10000)
        metrics = {"state": "partial", "sample": {"N": 5000},
                   "relay": {"first_board": {"state": "partial", "N": 2, "valid_n": 1, "missing_n": 1,
                                              "promotion_pct": None, "samples": samples},
                             "failed_classification": {"state": "partial", "N": 3, "unknown_n": 1, "samples": samples}},
                   "limits": {"supplier_reported": {"ladder": {"1": 5, "2": 1}}}, "quality_flags": []}
        with patch.object(series, "build_daily_metrics", return_value=metrics):
            result = series.series_view(self.db, DAY)
        json.dumps(result)
        compact = result["records"][-1]["metrics"]
        self.assertEqual(compact["sample"]["N"], 5000)
        self.assertEqual(compact["relay"]["first_board"]["N"], 2)
        self.assertEqual(compact["relay"]["first_board"]["missing_n"], 1)
        self.assertIsNone(compact["relay"]["first_board"]["promotion_pct"])
        self.assertEqual(compact["relay"]["failed_classification"]["unknown_n"], 1)
        self.assertEqual(compact["limits"]["supplier_reported"]["ladder"], {"1": 5, "2": 1})
        self.assertTrue(all("samples" not in group for group in compact["relay"].values()))
        self.assertIs(metrics["relay"]["first_board"]["samples"], samples)
        self.assertNotIn("calendar", metrics)

    def test_previous_limit_price_is_required_to_confirm_failure(self):
        self.full = True
        self.put("pools", PRIOR, [{"ts_code": "600001.SH", "trade_date": PRIOR.strftime("%Y%m%d"),
                                   "limit": "U", "limit_times": "1"}])
        record = series.series_view(self.db, DAY)["records"][-1]
        classification = record["metrics"]["relay"]["failed_classification"]
        self.assertEqual(classification["confirmed_failed_n"], 1)
        self.assertEqual(record["metrics"]["relay"]["failed"]["N"], 1)
        self.assertTrue(any(ref["id"] == f"limits:{PREVIOUS}:all" for ref in record["evidence_refs"]))
        self.tables[("limits", PREVIOUS, "all")]["state"] = "permission_denied"
        uncertain = series.series_view(self.db, DAY)["records"][-1]["metrics"]["relay"]
        self.assertEqual(uncertain["failed_classification"]["unknown_n"], 1)
        self.assertEqual(uncertain["failed_classification"]["confirmed_failed_n"], 0)
        self.assertEqual(uncertain["failed"]["classification_unknown_n"], 1)
        self.assertFalse(uncertain["failed"]["membership_complete"])

    def test_five_index_identities_and_original_range_row_references(self):
        self.full = True
        for offset, code in enumerate(read.INDICES):
            table = self.put("indices", DAY, self.default_rows("indices", DAY, code), code)
            table["evidence_refs"][0]["selected_rows"] = [42 + offset]
        indices = series.series_view(self.db, DAY)["records"][-1]["indices"]
        self.assertEqual([item["ts_code"] for item in indices], list(read.INDICES))
        self.assertEqual([item["record"]["row_reference"]["source_row"] for item in indices], list(range(42, 48)))
        self.assertTrue(all(item["record"]["row_reference"]["content_hash"] == "fixture-hash" for item in indices))
        code = next(iter(read.INDICES))
        self.put("indices", DAY, self.default_rows("indices", PREVIOUS, code), code)
        bad = series.series_view(self.db, DAY)["records"][-1]["indices"][0]
        self.assertIsNone(bad["record"])
        self.assertEqual(bad["coverage"]["state"], "broken_lineage")

    def test_theme_top_three_use_same_day_supplier_catalog_and_original_row_order(self):
        self.full = True
        rows = [{"ts_code": f"BK{1000 + index}.DC", "trade_date": DAY.strftime("%Y%m%d"), "pct_change": value}
                for index, value in enumerate(("1", None, "3", "9", "3", "NaN", "-2"))]
        table = self.put("themes", DAY, rows)
        ref = table["evidence_refs"][0]
        table["evidence_refs"] = [{**ref, "id": "themes-a", "row_count": 3},
                                  {**ref, "id": "themes-b", "start_row": 3, "row_count": len(rows) - 3}]
        result = series.series_view(self.db, DAY)["records"][-1]["themes"]
        self.assertEqual([row["ts_code"] for row in result["records"]], ["BK1003.DC", "BK1002.DC", "BK1004.DC"])
        self.assertEqual([(row["row_reference"]["id"], row["row_reference"]["source_row"])
                          for row in result["records"]], [("themes-b", 0), ("themes-a", 2), ("themes-b", 1)])
        self.assertEqual(result["unknown_change_n"], 2)
        self.assertEqual(result["state"], "partial")
        self.assertIn("供应商", result["scope"])
        self.assertNotIn("members", {call.args[1] for call in self.load.call_args_list})


if __name__ == "__main__":
    unittest.main()
