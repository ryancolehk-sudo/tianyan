"""Pure projections with mocked storage; never require HTTP or a database."""

import copy
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.market import review_read as read

DAY = date(2026, 10, 23)
PREVIOUS = date(2026, 10, 22)
PRIOR = date(2026, 10, 21)
CODE = "000001.SH"


def daily(code="600001.SH", *, day=DAY, change="1", amount="10"):
    return {"ts_code": code, "trade_date": day.strftime("%Y%m%d"), "open": "10", "close": "10.1",
            "high": "10.2", "pre_close": "10", "pct_chg": change, "amount": amount}


def minute(code=CODE, slot="09:31", *, day=DAY, close="10"):
    return {"ts_code": code, "trade_time": f"{day.isoformat()}T{slot}:00+08:00",
            "open": close, "high": close, "low": close, "close": close,
            "volume_raw": "1", "amount_raw": None}


class ReviewReadTests(unittest.TestCase):
    def setUp(self):
        self.db = object()  # Any accidental database-method access fails.
        self.days = [PRIOR, PREVIOUS, DAY]
        self.tables = {}
        self.enterContext(patch.object(read, "_current_snapshot", return_value=None))
        self.enterContext(patch.object(read, "_current_trade_day", return_value=None))
        self.load = self.enterContext(patch.object(read, "load_dataset", side_effect=self.fetch))
        self.enterContext(patch('app.market.review_fallback.pool_table', return_value=None))
        self.calendar = self.enterContext(patch.object(read, "available_trade_days", side_effect=self.open_days))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP")))

    def open_days(self, db, limit=63, end_day=None, as_of_time=None):
        self.assertIs(db, self.db)
        return [d for d in self.days if end_day is None or d <= end_day][-limit:]

    def fetch(self, db, dataset, day, scope="all", as_of_time=None, include_rows=True):
        self.assertIs(db, self.db)
        result = copy.deepcopy(self.tables.get((dataset, day, scope), {"state": "missing", "rows": []}))
        if not include_rows:
            result.pop("rows", None)
        return result

    def put(self, dataset, rows, *, day=DAY, scope="all", state="complete"):
        result = {"state": state, "rows": rows, "row_count": len(rows), "source_id": f"source-{dataset}",
                  "received_at": "2026-10-24T10:00:00+08:00", "requested_day": day.isoformat(),
                  "evidence_refs": [{"id": f"{dataset}-{day}-{scope}", "content_hash": "abc",
                                     "source_id": f"source-{dataset}", "received_at": "2026-10-24T10:00:00+08:00",
                                     "start_row": 0, "row_count": len(rows)}]}
        self.tables[(dataset, day, scope)] = result
        return result

    def base(self):
        self.put("daily", [daily(), daily("000002.SZ", change="-1", amount="20")])
        self.put("limits", [{"ts_code": r["ts_code"], "trade_date": DAY.strftime("%Y%m%d"),
                             "up_limit": "11", "down_limit": "9"} for r in [daily(), daily("000002.SZ")]])
        self.put("pools", [])
        self.put("daily", [daily(day=PREVIOUS)], day=PREVIOUS)
        self.put("pools", [], day=PREVIOUS)
        self.put("pools", [], day=PRIOR)

    def test_previous_days_come_only_from_verified_calendar(self):
        self.days = [date(2026, 10, 13), date(2026, 10, 16), date(2026, 10, 19)]
        target = self.days[-1]
        with patch.object(read, "build_daily_metrics", return_value={"state": "unavailable"}) as calculate:
            result = read.daily_view(self.db, target)
        self.assertEqual(result["metrics"]["calendar"]["previous_trade_day"], "2026-10-16")
        self.assertEqual(result["metrics"]["calendar"]["prior_trade_day"], "2026-10-13")
        requested = {(c.args[1], c.args[2]) for c in self.load.call_args_list}
        self.assertIn(("daily", date(2026, 10, 16)), requested)
        self.assertIn(("pools", date(2026, 10, 13)), requested)
        self.assertNotIn(("pools", date(2026, 10, 15)), requested)
        self.assertIsNone(calculate.call_args.kwargs["previous_daily"])

    def test_absent_calendar_never_guesses_a_date_or_loads_data(self):
        self.days = []
        result = read.daily_view(self.db)
        self.assertIsNone(result["shared_date"])
        self.assertEqual(result["metrics"]["state"], "unavailable")
        self.load.assert_not_called()

    def test_unverified_requested_day_does_not_fall_back_to_a_different_day(self):
        result = read.daily_view(self.db, date(2026, 10, 24))
        self.assertEqual(result["shared_date"], "2026-10-24")
        self.assertEqual(result["calendar_state"], "unavailable")
        self.load.assert_not_called()

    def test_as_of_propagates_to_every_calendar_and_dataset_read(self):
        cutoff = datetime.fromisoformat("2026-10-23T19:00:00+08:00")
        read.daily_view(self.db, DAY, as_of_time=cutoff)
        self.assertEqual(self.calendar.call_args.kwargs["as_of_time"], cutoff)
        self.assertTrue(all(c.kwargs["as_of_time"] == cutoff for c in self.load.call_args_list))
        with self.assertRaises(ValueError):
            read.daily_view(self.db, DAY, as_of_time=datetime(2026, 10, 23))

    def test_stock_filter_does_not_change_market_metrics(self):
        self.base()
        full = read.daily_view(self.db, DAY)
        selected = read.daily_view(self.db, DAY, stock="600001.SH")
        self.assertEqual(full["metrics"], selected["metrics"])
        self.assertEqual(selected["metrics"]["sample"]["N"], 2)
        self.assertEqual(len(selected["selection"]["stock_records"]), 1)
        self.assertFalse(selected["selection"]["market_metrics_filtered"])

    def test_permission_and_broken_lineage_rows_never_leak(self):
        self.put("daily", [daily()], state="permission_denied")
        self.put("lhb", [{"ts_code": "600001.SH", "trade_date": "20261023", "reason": "private"}], state="broken_lineage")
        result = read.daily_view(self.db, DAY)
        self.assertIsNone(result["metrics"]["sample"]["N"])
        self.assertFalse(result["evidence_refs"])
        self.assertEqual(read.lhb_view(self.db, DAY)["records"], [])

    def test_wrong_day_table_is_rejected_not_used_for_today(self):
        self.put("daily", [daily(day=PREVIOUS)])
        result = read.daily_view(self.db, DAY)
        self.assertIsNone(result["metrics"]["sample"]["N"])
        meta = next(item for item in result["coverage"] if item["dataset"] == "daily" and item["trade_day"] == DAY.isoformat())
        self.assertEqual(meta["state"], "broken_lineage")

    def test_complete_tencent_selected_as_whole_series_with_open_extra(self):
        self.put("tencent_minutes", [minute(slot=slot) for slot in ("09:30", *read.STANDARD_MINUTES)])
        self.put("sina_minutes", [minute(slot=slot, close="20") for slot in read.STANDARD_MINUTES])
        item = read.daily_view(self.db, DAY)["intraday"][0]
        self.assertEqual(item["selected_source"], "tencent_minutes")
        self.assertEqual(item["standard_point_count"], 240)
        self.assertEqual(len(item["extra_points"]), 1)
        self.assertEqual(len(item["segments"]), 2)
        self.assertEqual({p["close"] for p in item["points"]}, {"10"})
        self.assertFalse(item["source_mixed"])

    def test_partial_series_chooses_more_points_without_patching(self):
        self.put("tencent_minutes", [minute(slot="09:31"), minute(slot="09:32")])
        self.put("sina_minutes", [minute(slot=s, close="20") for s in ("09:31", "09:33", "13:01")])
        item = read.daily_view(self.db, DAY)["intraday"][0]
        self.assertEqual(item["selected_source"], "sina_minutes")
        self.assertIn("09:32", item["missing_standard_minutes"])
        self.assertEqual(len(item["segments"]), 3)
        self.assertEqual({p["close"] for p in item["points"]}, {"20"})

    def test_other_date_and_duplicate_minutes_cannot_fill_selected_day(self):
        self.put("tencent_minutes", [minute(day=PREVIOUS), minute(slot="09:32"), minute(slot="09:32"),
                                     minute(slot="12:00"), minute(slot="09:33")])
        item = read.daily_view(self.db, DAY)["intraday"][0]
        self.assertEqual(item["standard_point_count"], 1)
        self.assertIn("09:31", item["missing_standard_minutes"])
        self.assertIn("09:32", item["duplicate_minutes"])
        self.assertEqual(item["invalid_row_count"], 1)
        self.assertEqual(item["ignored_other_date_count"], 1)

    def test_six_index_days_keep_identity_and_evidence(self):
        for code in read.INDICES:
            self.put("indices", [{"trade_date": "20261023", "ts_code": code, "close": "100"}], scope=code)
        result = read.daily_view(self.db, DAY)
        self.assertEqual([i["ts_code"] for i in result["indices"]], list(read.INDICES))
        self.assertTrue(all(i["state"] == "complete" for i in result["indices"]))
        self.assertEqual(len(result["evidence_refs"]), 6)

    def test_range_row_references_restore_original_part_offsets(self):
        index = self.put("indices", [{"trade_date": "20261023", "ts_code": CODE, "close": "100"}], scope=CODE)
        index["evidence_refs"][0]["selected_rows"] = [62]
        minutes = self.put("tencent_minutes", [minute(slot="09:31"), minute(slot="09:32"), minute(slot="09:33")])
        first = {**minutes["evidence_refs"][0], "id": "first", "row_count": 1, "selected_rows": [1731]}
        second = {**first, "id": "second", "start_row": 1, "row_count": 2, "selected_rows": [21, 27]}
        minutes["evidence_refs"] = [first, second]
        result = read.daily_view(self.db, DAY)
        self.assertEqual(result["indices"][0]["record"]["row_reference"]["source_row"], 62)
        refs = [p["row_reference"] for p in result["intraday"][0]["points"]]
        self.assertEqual([(r["id"], r["source_row"]) for r in refs], [("first", 1731), ("second", 21), ("second", 27)])
        self.assertTrue(all(r["row_index_base"] == 0 and r["content_hash"] == "abc" for r in refs))

    def test_regular_rows_use_part_local_offsets_even_after_stock_filter(self):
        data = self.put("lhb", [self.listing(ts_code="600002.SH"), self.listing()])
        data["evidence_refs"] = [{**data["evidence_refs"][0], "id": "part-a", "row_count": 1},
                                 {**data["evidence_refs"][0], "id": "part-b", "start_row": 1, "row_count": 1}]
        record = read.lhb_view(self.db, DAY, stock="600001.SH")["records"][0]
        reference = record["source_rows"][0]["row_reference"]
        self.assertEqual((reference["id"], reference["source_row"]), ("part-b", 0))

    def test_theme_original_scope_and_member_sample_stay_separate(self):
        self.base()
        self.put("themes", [{"trade_date": "20261023", "ts_code": "BK1234.DC", "name": "例题材",
                             "pct_change": "5", "up_num": "100"}])
        self.put("members", [{"trade_date": "20261023", "ts_code": "BK1234.DC", "con_code": "600001.SH"},
                              {"trade_date": "20261023", "ts_code": "BK1234.DC", "con_code": "600999.SH"}])
        self.put("theme_funds", [])
        item = read.daily_view(self.db, DAY, sector="BK1234.DC")["themes"]["items"][0]
        self.assertEqual(item["provider_record"]["up_num"], "100")
        self.assertEqual(item["member_sample"]["known_up"], 1)
        self.assertEqual(item["member_sample"]["missing_quote_n"], 1)
        self.assertEqual(item["member_sample"]["known_turnover_yuan"], "10000")
        self.assertIsNone(item["member_sample"]["turnover_yuan"])
        self.assertFalse(item["member_sample"]["membership_complete_verified"])

    def test_missing_members_are_unknown_not_zero(self):
        self.put("themes", [{"trade_date": "20261023", "ts_code": "BK1234.DC"}])
        item = read.daily_view(self.db, DAY)["themes"]["items"][0]
        self.assertIsNone(item["member_sample"]["known_up"])
        self.assertIsNone(item["member_sample"]["disclosed_member_n"])
        self.assertIsNone(item["disclosed_total"])
        self.assertFalse(item["detail_available"])
        self.assertEqual(item["returned_member_count"], 0)

    def test_large_member_tables_are_grouped_once_with_original_row_references(self):
        class CountingRows(list):
            def __init__(self, values):
                super().__init__(values)
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        codes = [f"BK{1000 + n}.DC" for n in range(516)]
        catalog = [{"trade_date": "20261023", "ts_code": code} for code in codes]
        # Interleave sector rows and split their evidence into two parts. A
        # grouped row's position must never replace its source-table position.
        members = CountingRows({"trade_date": "20261023", "ts_code": code,
                                "con_code": f"{600000 + offset}.SH"}
                               for offset in range(200) for code in codes)
        funds = CountingRows({"trade_date": "20261023", "ts_code": code, "net_amount": "1"}
                             for code in reversed(codes))
        tables = {"themes": self.put("themes", catalog), "members": self.put("members", members),
                  "theme_funds": self.put("theme_funds", funds)}
        for dataset, table in tables.items():
            table.update(dataset=dataset, trade_day=DAY.isoformat(), scope="all")
        ref = tables["members"]["evidence_refs"][0]
        tables["members"]["evidence_refs"] = [
            {**ref, "id": "members-a", "row_count": 5000},
            {**ref, "id": "members-b", "start_row": 5000, "row_count": len(members) - 5000},
        ]
        with patch.object(read, "_referenced", wraps=read._referenced) as reference:
            result = read._themes(tables, DAY, None, None)
        self.assertFalse(any(call.args[1] is tables["members"] for call in reference.call_args_list))
        self.assertEqual(len(result["items"]), 516)
        # One date-validation pass plus one grouping pass, not one scan per board.
        self.assertEqual((members.iterations, funds.iterations), (2, 2))
        for item in result["items"]:
            self.assertEqual(item["member_records"], [])
            self.assertEqual(item["stock_records"], [])
            self.assertTrue(item["detail_available"])
            self.assertFalse(item["detail_requested"])
            self.assertEqual(item["returned_member_count"], 0)
            self.assertEqual(item["disclosed_total"], 200)
            self.assertEqual(item["member_sample"]["disclosed_member_n"], 200)
        item = read._themes(tables, DAY, codes[1], None)["items"][0]
        self.assertEqual((members.iterations, funds.iterations), (4, 4))
        self.assertEqual(len(item["member_records"]), 200)
        self.assertEqual(item["member_sample"], result["items"][1]["member_sample"])
        first = item["member_records"][0]["row_reference"]
        last = item["member_records"][-1]["row_reference"]
        self.assertEqual((first["id"], first["source_row"]), ("members-a", 1))
        self.assertEqual((last["id"], last["source_row"]), ("members-b", 199 * 516 + 1 - 5000))
        self.assertEqual(item["fund_records"][0]["row_reference"]["source_row"], 514)
        self.assertIsNone(item["member_sample"]["turnover_yuan"])
        self.assertFalse(item["member_sample"]["membership_complete_verified"])

    def test_selected_theme_detail_cap_keeps_full_statistics_and_original_daily_offsets(self):
        self.base()
        sector = "BK1234.DC"
        self.put("themes", [{"trade_date": "20261023", "ts_code": sector}])
        members = [{"trade_date": "20261023", "ts_code": "BKOTHER.DC", "con_code": "600123.SH"},
                   {"trade_date": "20261023", "ts_code": sector, "con_code": "833000.BJ"}]
        members += [{"trade_date": "20261023", "ts_code": sector, "con_code": f"{600000 + n}.SH"}
                    for n in range(1502)]
        member_table = self.put("members", members)
        member_ref = member_table["evidence_refs"][0]
        member_table["evidence_refs"] = [
            {**member_ref, "id": "members-a", "row_count": 1000},
            {**member_ref, "id": "members-b", "start_row": 1000, "row_count": len(members) - 1000},
        ]
        # Daily order differs from membership order and one returned member has
        # no daily quote. Preserve source offsets; never invent that quote.
        quotes = [daily("833000.BJ")]
        quotes += [daily(f"{600000 + n}.SH") for n in reversed(range(1502)) if n != 500]
        daily_table = self.put("daily", quotes)
        daily_ref = daily_table["evidence_refs"][0]
        daily_table["evidence_refs"] = [
            {**daily_ref, "id": "daily-a", "row_count": 10},
            {**daily_ref, "id": "daily-b", "start_row": 10, "row_count": len(quotes) - 10},
        ]
        self.put("theme_funds", [])
        summary = read.daily_view(self.db, DAY)["themes"]["items"][0]
        result = read.daily_view(self.db, DAY, sector=sector, stock="601501.SH")
        item = result["themes"]["items"][0]
        self.assertEqual(item["member_sample"], summary["member_sample"])
        self.assertEqual(item["member_sample"]["disclosed_member_n"], 1502)
        self.assertEqual(item["member_sample"]["matched_daily_n"], 1501)
        self.assertEqual(item["member_sample"]["known_up"], 1501)
        self.assertEqual(item["member_sample"]["missing_quote_n"], 1)
        self.assertEqual(item["disclosed_total"], 1503)
        self.assertEqual(item["returned"], 1500)
        self.assertEqual(item["returned_member_count"], 1500)
        self.assertTrue(item["truncated"])
        self.assertEqual(len(item["member_records"]), 1500)
        self.assertEqual(len(item["stock_records"]), 1498)
        self.assertFalse(any(r["ts_code"] in {"833000.BJ", "600500.SH", "601501.SH"} for r in item["stock_records"]))
        for record in item["member_records"]:
            ref = record["row_reference"]
            original_index = ref["source_row"] + (1000 if ref["id"] == "members-b" else 0)
            self.assertEqual({k: v for k, v in record.items() if k != "row_reference"}, members[original_index])
        for record in item["stock_records"]:
            ref = record["row_reference"]
            original_index = ref["source_row"] + (10 if ref["id"] == "daily-b" else 0)
            self.assertEqual({k: v for k, v in record.items() if k != "row_reference"}, quotes[original_index])
        self.assertEqual(item["member_records"][-1]["con_code"], "601498.SH")
        self.assertEqual(item["stock_records"][0]["row_reference"]["source_row"], 4)
        selected_stock = result["selection"]["stock_records"]
        self.assertEqual(len(selected_stock), 1)
        self.assertEqual(selected_stock[0]["ts_code"], "601501.SH")
        self.assertEqual(selected_stock[0]["row_reference"]["source_row"], 1)

    def test_pool_projection_filters_exchange_caps_details_and_keeps_raw_row_references(self):
        self.base()
        rows = [{"trade_date": "20261023", "ts_code": "833000.BJ", "limit": "U", "limit_times": "1"}]
        rows += [{"trade_date": "20261023", "ts_code": f"{600000 + n}.SH", "limit": "U", "limit_times": "1"}
                 for n in range(502)]
        table = self.put("pools", rows)
        reference = table["evidence_refs"][0]
        table["evidence_refs"] = [
            {**reference, "id": "pools-a", "row_count": 400},
            {**reference, "id": "pools-b", "start_row": 400, "row_count": len(rows) - 400},
        ]
        pools = read.daily_view(self.db, DAY, stock="600001.SH")["pools"]
        self.assertEqual(pools["coverage"]["state"], "complete")
        self.assertEqual(pools["source_total"], 503)
        self.assertEqual(pools["disclosed_total"], 502)
        self.assertEqual(pools["returned"], 500)
        self.assertEqual(len(pools["records"]), 500)
        self.assertTrue(pools["truncated"])
        self.assertEqual(pools["records"][0]["ts_code"], "600000.SH")
        self.assertEqual(pools["records"][-1]["ts_code"], "600499.SH")
        for record in pools["records"]:
            ref = record["row_reference"]
            original_index = ref["source_row"] + (400 if ref["id"] == "pools-b" else 0)
            self.assertEqual({k: v for k, v in record.items() if k != "row_reference"}, rows[original_index])

    def test_missing_pool_projection_keeps_unknown_total(self):
        pools = read.daily_view(self.db, DAY)["pools"]
        self.assertEqual(pools["coverage"]["state"], "missing")
        self.assertIsNone(pools["source_total"])
        self.assertIsNone(pools["disclosed_total"])
        self.assertEqual(pools["records"], [])
        self.assertFalse(pools["truncated"])

    def test_coverage_distinguishes_manifest_from_minute_observations(self):
        self.put("daily", [])
        self.put("tencent_minutes", [minute(day=PREVIOUS)])
        result = read.coverage_view(self.db, DAY, lookback=1)
        minute_meta = next(e for e in result["entries"] if e["dataset"] == "tencent_minutes")
        self.assertEqual(minute_meta["state"], "complete")
        self.assertEqual(minute_meta["observations_state"], "unavailable")
        self.assertIn(minute_meta, result["gaps"])
        self.assertEqual(result["state"], "partial")
        self.assertTrue(any(not call.kwargs["include_rows"] for call in self.load.call_args_list))

    def listing(self, reason="单日原因", **changes):
        return {"trade_date": "20261023", "ts_code": "600001.SH", "reason": reason,
                "l_buy": "900", "l_sell": "100", "net_amount": "777", **changes}

    def test_selected_board_can_read_an_individually_backfilled_historical_scope(self):
        self.base()
        board = "BK1090.DC"
        self.put("themes", [{"ts_code": board, "trade_date": "20261023", "name": "Synthetic board", "pct_change": "1"}])
        self.put("members", [{"ts_code": board, "con_code": "600001.SH", "trade_date": "20261023", "name": "Synthetic member"}], scope=board)
        result = read.daily_view(self.db, DAY, sector=board)
        item = result["themes"]["items"][0]
        self.assertEqual(item["member_sample"]["disclosed_member_n"], 1)
        self.assertEqual(item["member_records"][0]["row_reference"]["source_row"], 0)
        self.assertEqual(item["stock_records"][0]["ts_code"], "600001.SH")

    def test_lhb_default_scope_matches_the_sh_sz_stock_selection_contract(self):
        self.put("lhb", [self.listing(ts_code="920001.BJ"), self.listing()])
        self.put("seats", [{"trade_date": "20261023", "ts_code": "920001.BJ", "reason": "单日原因", "side": "0", "buy": "10"}])
        result = read.lhb_view(self.db, DAY)
        self.assertEqual(result["market_scope"], "SH_SZ_A")
        self.assertEqual(result["source_listing_count"], 2)
        self.assertEqual(result["returned_listing_count"], 1)
        self.assertEqual(result["records"][0]["source_rows"][0]["row_reference"]["source_row"], 1)
        self.assertEqual(result["unmatched_seat_records"], [])

    def test_named_seat_history_keeps_reasons_and_original_rows_without_aggregating(self):
        self.put("lhb", [self.listing(), self.listing("三日原因")])
        rows = [{"ts_code": "600001.SH", "trade_date": "20261023", "exalter": "同名营业部", "side": side, "buy": amount, "reason": reason}
                for side, amount, reason in [("0", "100", "单日原因"), ("0", "200", "三日原因"), ("1", "999", "单日原因")]]
        self.put("seats", rows)
        self.put("seats", [], day=PREVIOUS)
        self.put("seats", [], day=PRIOR)
        result = read.seat_disclosures_view(self.db, DAY, "同名营业部", lookback=3)
        self.assertEqual(result["state"], "complete")
        self.assertFalse(result["unique_identity_verified"])
        self.assertEqual([r["amount_yuan"] for r in result["records"]], ["100", "200"])
        self.assertEqual([r["seat_record"]["row_reference"]["source_row"] for r in result["records"]], [0, 1])
        self.assertNotEqual(result["records"][0]["lhb_record_key"], result["records"][1]["lhb_record_key"])
        filtered = read.seat_disclosures_view(self.db, DAY, "同名营业部", lookback=3, min_amount_yuan="150")
        self.assertEqual(len(filtered["records"]), 1)
        self.assertEqual(filtered["records"][0]["seat_record"]["row_reference"]["source_row"], 1)

    def test_missing_named_seat_history_is_not_proof_of_no_buying(self):
        self.put("seats", [], day=DAY)
        result = read.seat_disclosures_view(self.db, DAY, "机构专用", lookback=3)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["records"], [])

    def test_lhb_preserves_reasons_sides_and_same_named_seats(self):
        self.put("lhb", [self.listing(), self.listing("三日原因", net_amount="888")])
        seats = [{"trade_date": "20261023", "ts_code": "600001.SH", "reason": "单日原因",
                  "side": side, "exalter": "机构专用", "source_order": str(i + 1),
                  "buy": "10", "sell": "2", "net_buy": "8"}
                 for side in ("0", "1") for i in range(5)]
        seats.append({**seats[0], "reason": "三日原因", "buy": "500"})
        self.put("seats", seats)
        result = read.lhb_view(self.db, DAY, stock="600001.SH")
        self.assertEqual(len(result["records"]), 2)
        self.assertIsNone(result["selected_record"])
        one, multi = result["records"]
        self.assertNotEqual(one["record_key"], multi["record_key"])
        self.assertEqual(one["headline"]["net_amount"], "777")
        self.assertEqual(one["buy_top5"]["amount_yuan"], "50")
        self.assertEqual(one["sell_top5"]["amount_yuan"], "10")
        self.assertEqual(len(one["buy_top5"]["records"]), 5)
        self.assertIsNone(multi["period"]["start_day"])
        self.assertIsNone(multi["period"]["end_day"])
        chosen = read.lhb_view(self.db, DAY, stock="600001.SH", record_key=multi["record_key"])
        self.assertEqual(chosen["selected_record"]["headline"]["net_amount"], "888")

    def test_lhb_duplicate_events_and_missing_seats_are_not_summed(self):
        self.put("lhb", [self.listing(), self.listing(net_amount="999")])
        self.put("seats", [{"trade_date": "20261023", "ts_code": "600001.SH", "reason": "单日原因",
                            "side": "0", "exalter": "机构专用", "buy": "10", "sell": "0"} for _ in range(5)])
        record = read.lhb_view(self.db, DAY)["records"][0]
        self.assertEqual(len(record["source_rows"]), 2)
        self.assertEqual(record["headline_state"], "ambiguous_duplicate_event")
        self.assertIsNone(record["headline"]["net_amount"])
        self.assertIsNone(record["buy_top5"]["amount_yuan"])
        self.assertIsNone(record["buy_top5"]["known_amount_yuan"])
        self.assertEqual(record["buy_top5"]["state"], "ambiguous_disclosure")
        self.assertEqual(len(record["buy_top5"]["records"]), 5)

    def test_lhb_history_only_lists_dates_with_disclosed_stock_records(self):
        self.put("lhb", [self.listing()])
        self.put("lhb", [self.listing(trade_date="20261022")], day=PREVIOUS)
        self.put("lhb", [self.listing(trade_date="20261021", ts_code="600002.SH")], day=PRIOR)
        result = read.lhb_view(self.db, DAY, stock="600001.SH")
        self.assertEqual(result["history_dates"], [PREVIOUS.isoformat(), DAY.isoformat()])
        self.assertEqual([row["trade_day"] for row in result["history_records"]], [DAY.isoformat(), PREVIOUS.isoformat()])
        self.assertEqual(result["history_records"][0]["net_amount"], "777")
        self.assertEqual(result["history_records"][1]["source_rows"][0]["row_reference"]["source_row"], 0)


if __name__ == "__main__":
    unittest.main()
