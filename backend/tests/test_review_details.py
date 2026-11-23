"""Bounded stock/theme projections over fixtures; no database or HTTP access."""

import copy
import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.market import review_details as details
from app.market import review_read as read

DAY = date(2026, 10, 23)
PREVIOUS = date(2026, 10, 22)


def row(code, day=DAY, **values):
    return {"ts_code": code, "trade_date": day.strftime("%Y%m%d"), **values}


class ReviewDetailsTests(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.days = [date(2026, 10, 13), date(2026, 10, 16), date(2026, 10, 19), PREVIOUS, DAY]
        self.tables = {}
        self.revoked = False
        self.calendar = self.enterContext(patch.object(read, "available_trade_days", side_effect=self.open_days))
        self.load = self.enterContext(patch.object(read, "load_dataset", side_effect=self.fetch))
        self.enterContext(patch('app.market.review_fallback.pool_table', return_value=None))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")))

    def open_days(self, db, limit=63, end_day=None, as_of_time=None):
        self.assertIs(db, self.db)
        return [day for day in self.days if end_day is None or day <= end_day][-limit:]

    def put(self, dataset, rows, day=DAY, state="complete"):
        table = {"state": state, "rows": rows, "row_count": len(rows), "source_id": f"source-{dataset}",
                 "requested_day": day.isoformat(), "received_at": "2026-10-24T10:00:00+08:00",
                 "evidence_refs": [{"id": f"{dataset}:{day}", "content_hash": "fixture-hash",
                                    "source_id": f"source-{dataset}", "received_at": "2026-10-24T10:00:00+08:00",
                                    "start_row": 0, "row_count": len(rows)}]}
        self.tables[(dataset, day)] = table
        return table

    def fetch(self, db, dataset, day, scope="all", as_of_time=None, include_rows=True):
        self.assertIs(db, self.db)
        self.assertEqual(scope, "all")
        table = copy.deepcopy(self.tables.get((dataset, day), {"state": "missing", "rows": []}))
        if self.revoked:
            table["state"] = "permission_denied"
        if as_of_time and table.get("received_at") and datetime.fromisoformat(table["received_at"]) > as_of_time:
            return {"state": "missing", "reason": "not_received_by_cutoff"}
        return table

    def fill_history(self, codes=("BK1234.DC", "BK5678.DC")):
        for day in self.days:
            self.put("themes", [row(code, day, name=f"原日名称-{day}", pct_change="1") for code in codes], day)
            self.put("theme_funds", [row(code, day, net_amount="-10") for code in reversed(codes)], day)

    def test_stock_codes_deduplicate_in_request_order_and_only_read_two_datasets(self):
        self.put("daily", [row("600001.SH", close="10", name="不能作为名称来源"), row("000002.SZ", close="20")])
        self.put("pools", [row("000002.SZ", name="原日池名称", limit="U")])
        result = details.stocks_view(self.db, DAY, ["000002.SZ", "600001.SH", "000002.SZ"])
        self.assertEqual(result["codes"], ["000002.SZ", "600001.SH"])
        self.assertEqual([item["ts_code"] for item in result["items"]], result["codes"])
        self.assertEqual(result["items"][0]["name"], "原日池名称")
        self.assertIsNone(result["items"][1]["name"])
        self.assertIsNone(result["items"][1]["pool_record"])
        self.assertEqual(result["items"][1]["pool_state"], "not_disclosed")
        self.assertEqual(result["items"][1]["state"], "complete")
        self.assertEqual(result["state"], "complete")
        self.assertEqual({call.args[1] for call in self.load.call_args_list}, {"daily", "pools"})
        self.assertEqual(self.load.call_count, 2)
        self.assertEqual(self.calendar.call_args.kwargs["limit"], 1)

    def test_stock_code_validation_and_distinct_limit_happen_before_reads(self):
        bad_inputs = ("600001.SH", None, ["830001.BJ"], ["600001.sh"], ["60001.SH"], [None],
                      [f"{600000 + n}.SH" for n in range(31)])
        for codes in bad_inputs:
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                details.stocks_view(self.db, DAY, codes)
        self.calendar.assert_not_called()
        self.load.assert_not_called()
        deduplicated = details.stocks_view(self.db, DAY, ["600001.SH"] * 40)
        self.assertEqual(len(deduplicated["items"]), 1)
        maximum = details.stocks_view(self.db, DAY, [f"{600000 + n}.SH" for n in range(30)])
        self.assertEqual(len(maximum["items"]), 30)

    def test_selected_day_is_required_for_both_projections(self):
        with self.assertRaises(ValueError):
            details.stocks_view(self.db, None, ["600001.SH"])
        with self.assertRaises(ValueError):
            details.theme_history_view(self.db, None, ["BK1234.DC"])
        self.calendar.assert_not_called()
        self.load.assert_not_called()

    def test_unknown_day_does_not_fall_back_or_load_any_dataset(self):
        target = date(2026, 10, 24)
        stocks = details.stocks_view(self.db, target, ["600001.SH"])
        history = details.theme_history_view(self.db, target, ["BK1234.DC"])
        for result in (stocks, history):
            self.assertEqual(result["shared_date"], target.isoformat())
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["evidence_refs"], [])
        self.assertIsNone(stocks["items"][0]["quote"])
        self.assertEqual(history["items"][0]["records"], [])
        self.assertEqual(history["trade_days"], [])
        self.load.assert_not_called()

    def test_empty_selections_are_empty_without_dataset_reads(self):
        stocks = details.stocks_view(self.db, DAY, [])
        history = details.theme_history_view(self.db, DAY, [])
        self.assertEqual(stocks["state"], "empty")
        self.assertEqual(history["state"], "empty")
        self.assertEqual(stocks["items"], [])
        self.assertEqual(history["items"], [])
        self.load.assert_not_called()

    def test_stock_detail_is_bounded_and_keeps_original_part_row_offsets(self):
        rows = [row(f"{600000 + n}.SH", close=str(n)) for n in range(10000)]
        daily = self.put("daily", rows)
        ref = daily["evidence_refs"][0]
        daily["evidence_refs"] = [{**ref, "id": "daily-a", "row_count": 5000},
                                  {**ref, "id": "daily-b", "start_row": 5000, "row_count": 5000}]
        self.put("pools", [row("833000.BJ", name="不选取"), row("609999.SH", name="原日池名")])
        with patch.object(details, "_referenced", wraps=details._referenced) as referenced:
            result = details.stocks_view(self.db, DAY, ["609999.SH", "600001.SH"])
        self.assertEqual(referenced.call_count, 3)
        first, second = result["items"]
        first_ref, second_ref = first["quote"]["row_reference"], second["quote"]["row_reference"]
        self.assertEqual((first_ref["id"], first_ref["source_row"]), ("daily-b", 4999))
        self.assertEqual((second_ref["id"], second_ref["source_row"]), ("daily-a", 1))
        self.assertEqual(first["pool_record"]["row_reference"]["source_row"], 1)
        self.assertEqual(first["quote"]["close"], "9999")
        self.assertLess(len(json.dumps(result)), 10000)

    def test_missing_quotes_are_not_filled_from_pool_or_another_date(self):
        self.put("daily", [row("600001.SH", PREVIOUS, close="88")], PREVIOUS)
        self.put("pools", [row("600001.SH", name="原日池名", close="77")])
        result = details.stocks_view(self.db, DAY, ["600001.SH", "000002.SZ"])
        self.assertIsNone(result["items"][0]["quote"])
        self.assertEqual(result["items"][0]["name"], "原日池名")
        self.assertEqual(result["items"][0]["state"], "partial")
        self.assertEqual(result["items"][0]["quote_state"], "missing")
        self.assertIsNone(result["items"][0]["quote_match_count"])
        self.assertEqual(result["items"][1]["state"], "unavailable")
        self.assertTrue(all(call.args[2] == DAY for call in self.load.call_args_list))

    def test_null_raw_price_stays_null(self):
        self.put("daily", [row("600001.SH", close=None, pct_chg=None)])
        self.put("pools", [])
        item = details.stocks_view(self.db, DAY, ["600001.SH"])["items"][0]
        self.assertIsNone(item["quote"]["close"])
        self.assertIsNone(item["quote"]["pct_chg"])

    def test_duplicate_stock_identity_does_not_choose_first_or_last(self):
        self.put("daily", [row("600001.SH", close="10"), row("600001.SH", close="11")])
        self.put("pools", [row("600001.SH", name="名称")])
        item = details.stocks_view(self.db, DAY, ["600001.SH"])["items"][0]
        self.assertIsNone(item["quote"])
        self.assertEqual(item["quote_state"], "ambiguous_duplicate")
        self.assertEqual(item["quote_match_count"], 2)
        self.assertEqual(item["state"], "partial")
        self.put("daily", [row("600001.SH", close="10")])
        self.put("pools", [row("600001.SH", name="名称一"), row("600001.SH", name="名称二")])
        item = details.stocks_view(self.db, DAY, ["600001.SH"])["items"][0]
        self.assertIsNone(item["pool_record"])
        self.assertIsNone(item["name"])
        self.assertEqual(item["pool_state"], "ambiguous_duplicate")

    def test_theme_code_and_five_day_bounds_are_checked_before_reads(self):
        for codes in ("BK1234.DC", ["BK123.DC"], ["BK1234.SH"], [None],
                      [f"BK{1000 + n}.DC" for n in range(11)]):
            with self.subTest(codes=codes), self.assertRaises(ValueError):
                details.theme_history_view(self.db, DAY, codes)
        for lookback in (1, 4, 6, 20, True, 5.0, "5"):
            with self.subTest(lookback=lookback), self.assertRaises(ValueError):
                details.theme_history_view(self.db, DAY, ["BK1234.DC"], lookback)
        self.calendar.assert_not_called()
        self.load.assert_not_called()
        result = details.theme_history_view(self.db, DAY, ["BK1234.DC"] * 11)
        self.assertEqual(len(result["items"]), 1)

    def test_history_uses_exact_original_five_days_only_two_datasets_and_no_stages(self):
        self.fill_history()
        result = details.theme_history_view(self.db, DAY, ["BK5678.DC", "BK1234.DC", "BK5678.DC"])
        self.assertEqual(result["sector_codes"], ["BK5678.DC", "BK1234.DC"])
        self.assertEqual(result["trade_days"], [day.isoformat() for day in self.days])
        self.assertTrue(result["calendar_window_complete"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(self.calendar.call_args.kwargs["limit"], 5)
        self.assertEqual(self.load.call_count, 10)
        self.assertEqual({call.args[1] for call in self.load.call_args_list}, {"themes", "theme_funds"})
        for item in result["items"]:
            self.assertEqual(len(item["records"]), 5)
            for record, day in zip(item["records"], self.days, strict=True):
                self.assertEqual(record["trade_day"], day.isoformat())
                self.assertEqual(record["catalog_record"]["name"], f"原日名称-{day}")
                self.assertEqual(record["fund_record"]["net_amount"], "-10")
                self.assertEqual(record["catalog_record"]["trade_date"], day.strftime("%Y%m%d"))
                self.assertNotIn("stage", record)
                self.assertNotIn("phase", record)
        first = result["items"][0]["records"][0]
        self.assertEqual(first["catalog_record"]["row_reference"]["source_row"], 1)
        self.assertEqual(first["fund_record"]["row_reference"]["source_row"], 0)

    def test_short_calendar_and_historical_gaps_remain_explicit(self):
        self.days = self.days[-3:]
        self.fill_history()
        self.put("themes", [], PREVIOUS)
        self.put("theme_funds", [], PREVIOUS, state="missing")
        result = details.theme_history_view(self.db, DAY, ["BK1234.DC"])
        self.assertEqual(result["returned_day_count"], 3)
        self.assertFalse(result["calendar_window_complete"])
        self.assertEqual(result["state"], "partial")
        middle = result["items"][0]["records"][1]
        self.assertEqual(middle["trade_day"], PREVIOUS.isoformat())
        self.assertIsNone(middle["catalog_record"])
        self.assertIsNone(middle["fund_record"])
        self.assertEqual(middle["catalog_state"], "not_disclosed")
        self.assertEqual(middle["fund_state"], "missing")
        self.assertEqual(middle["state"], "unavailable")

    def test_duplicate_theme_rows_are_bounded_and_not_arbitrarily_selected(self):
        self.fill_history()
        self.put("theme_funds", [row("BK1234.DC", net_amount=str(index)) for index in range(10000)])
        result = details.theme_history_view(self.db, DAY, ["BK1234.DC"])
        record = result["items"][0]["records"][-1]
        self.assertIsNone(record["fund_record"])
        self.assertEqual(record["fund_state"], "ambiguous_duplicate")
        self.assertEqual(record["fund_match_count"], 10000)
        self.assertEqual(record["state"], "partial")
        self.assertLess(len(json.dumps(result)), 30000)

    def test_wrong_date_rows_are_rejected_without_original_evidence_leakage(self):
        daily = self.put("daily", [row("600001.SH", PREVIOUS, close="88")])
        stocks = details.stocks_view(self.db, DAY, ["600001.SH"])
        self.assertIsNone(stocks["items"][0]["quote"])
        self.assertEqual(stocks["items"][0]["quote_state"], "broken_lineage")
        self.assertNotIn(daily["evidence_refs"][0]["id"], {ref["id"] for ref in stocks["evidence_refs"]})
        self.fill_history()
        self.put("themes", [row("BK1234.DC", PREVIOUS, name="不能套用")])
        history = details.theme_history_view(self.db, DAY, ["BK1234.DC"])
        current = history["items"][0]["records"][-1]
        self.assertIsNone(current["catalog_record"])
        self.assertEqual(current["catalog_state"], "broken_lineage")
        self.assertFalse(any(ref["id"] == f"themes:{DAY}" for ref in current["evidence_refs"]))

    def test_cutoff_is_forwarded_and_late_payloads_never_fall_back(self):
        self.put("daily", [row("600001.SH", close="10")])
        self.put("pools", [row("600001.SH", name="原日池名")])
        self.fill_history()
        cutoff = datetime.fromisoformat("2026-10-23T20:00:00+08:00")
        stocks = details.stocks_view(self.db, DAY, ["600001.SH"], cutoff)
        history = details.theme_history_view(self.db, DAY, ["BK1234.DC"], as_of_time=cutoff)
        self.assertTrue(all(call.kwargs["as_of_time"] == cutoff for call in self.calendar.call_args_list))
        self.assertTrue(all(call.kwargs["as_of_time"] == cutoff for call in self.load.call_args_list))
        self.assertIsNone(stocks["items"][0]["quote"])
        self.assertTrue(all(record["catalog_record"] is None for record in history["items"][0]["records"]))
        for result in (stocks, history):
            self.assertEqual(result["knowledge_mode"], "observed")
            self.assertEqual(result["as_of_time"], cutoff.isoformat())
            self.assertEqual(result["evidence_refs"], [])
        with self.assertRaises(ValueError):
            details.stocks_view(self.db, DAY, ["600001.SH"], datetime(2026, 10, 23))
        with self.assertRaises(ValueError):
            details.theme_history_view(self.db, DAY, ["BK1234.DC"], as_of_time=datetime(2026, 10, 23))

    def test_revocation_hides_previous_payload_and_rechecks_new_requests(self):
        self.put("daily", [row("600001.SH", close="10")])
        self.put("pools", [row("600001.SH", name="原日池名")])
        self.fill_history()
        self.assertTrue(details.stocks_view(self.db, DAY, ["600001.SH"])["evidence_refs"])
        self.assertTrue(details.theme_history_view(self.db, DAY, ["BK1234.DC"])["evidence_refs"])
        self.revoked = True
        self.load.reset_mock()
        stocks = details.stocks_view(self.db, DAY, ["600001.SH"])
        history = details.theme_history_view(self.db, DAY, ["BK1234.DC"])
        self.assertEqual(self.load.call_count, 12)
        for result in (stocks, history):
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["evidence_refs"], [])
            self.assertTrue(all(not entry["evidence_refs"] for entry in result["coverage"]))
        self.assertIsNone(stocks["items"][0]["name"])
        self.assertIsNone(stocks["items"][0]["quote"])
        self.assertTrue(all(record["catalog_record"] is None and record["fund_record"] is None
                            for record in history["items"][0]["records"]))


if __name__ == "__main__":
    unittest.main()
