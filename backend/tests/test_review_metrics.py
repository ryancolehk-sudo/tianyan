"""Pure metric tests: no database, HTTP, credentials, or synthetic fallback.

Fixtures below are deliberately small arithmetic examples, not claimed market
observations. They can also be run with the standard-library unittest runner.
"""

import copy
import json
import unittest
from decimal import Decimal

from app.market.review_metrics import build_daily_metrics

TODAY = "20261023"
PREVIOUS = "20261022"
PRIOR = "20261021"


def quote(index, pct="0", *, day=TODAY, amount="1", **changes):
    close = Decimal("10") * (1 + Decimal(pct or "0") / 100)
    row = {
        "ts_code": f"{index:06d}.SZ", "trade_date": day, "pre_close": "10",
        "open": "10", "close": str(close), "high": str(max(close, Decimal("10"))),
        "pct_chg": pct, "amount": amount,
    }
    row.update(changes)
    return row


def cap(row, *, up="12", down="8"):
    return {"ts_code": row["ts_code"], "trade_date": row["trade_date"],
            "up_limit": up, "down_limit": down}


def pool(index, kind="U", *, day=TODAY, height="1", seal="500000"):
    return {"ts_code": f"{index:06d}.SZ", "trade_date": day, "limit": kind,
            "limit_times": height, "fd_amount": seal}


class DailyReviewMetricsTests(unittest.TestCase):
    def calculate(self, rows, pools=None, **kwargs):
        return build_daily_metrics(trade_day=TODAY, daily=rows,
                                   price_limits=[cap(row) for row in rows],
                                   pools=[] if pools is None else pools, **kwargs)

    def test_up_turnover_uses_same_day_daily_units_and_retains_missing_members(self):
        rows = [quote(1, "20", amount="2"), quote(2, "0", amount="8")]
        result = self.calculate(rows, [pool(1)])
        verified = result["limits"]["price_verified"]["up_turnover"]
        self.assertEqual((verified["N"], verified["amount_yuan"], verified["share_pct"]), (1, "2000", "20"))
        missing = self.calculate(rows, [pool(1), pool(3)])["limits"]["supplier_reported"]["up_turnover"]
        self.assertEqual((missing["N"], missing["valid_n"], missing["missing_n"]), (2, 1, 1))
        self.assertIsNone(missing["amount_yuan"])
        self.assertIsNone(missing["share_pct"])

    def test_histogram_boundaries_missing_values_and_bse_scope(self):
        returns = ["-8", "-7", "-6", "-5", "-4", "-3", "-2", "-.001", "0",
                   ".001", "2.999", "3", "4.999", "5", "6.999", "7", "8", None]
        rows = [quote(i + 1, value) for i, value in enumerate(returns)]
        rows.append(quote(999, "8", ts_code="830001.BJ"))
        result = self.calculate(rows)
        self.assertEqual(list(result["breadth"]["bins"].values()), [2, 2, 2, 2, 1, 2, 2, 2, 2])
        self.assertEqual(result["sample"]["N"], 18)
        self.assertEqual(result["breadth"]["valid_n"], 17)
        self.assertEqual(result["breadth"]["missing_n"], 1)
        self.assertEqual((result["breadth"]["up"], result["breadth"]["down"], result["breadth"]["flat"]), (8, 8, 1))
        self.assertEqual(result["universe"]["excluded_bse_n"], 1)

    def test_actual_twenty_percent_limits_disagree_with_supplier_and_seal_units(self):
        rows = [quote(1, "20"), quote(2, "10"), quote(3, "-20"), quote(4, "0", high="12")]
        pools = [pool(1, height="2", seal="2500000"), pool(2, seal="9000000"),
                 pool(3, "D"), pool(4, "Z")]
        result = self.calculate(rows, pools)
        limits = result["limits"]
        self.assertEqual((limits["price_verified"]["up"], limits["price_verified"]["down"],
                          limits["price_verified"]["broken"]), (1, 1, 1))
        supplier = limits["supplier_reported"]
        self.assertEqual((supplier["up"], supplier["down"], supplier["broken"]), (2, 1, 1))
        self.assertEqual(supplier["ladder"], {"1": 1, "2": 1})
        self.assertEqual(supplier["broken_rate_pct"], str(Decimal(100) / 3))
        self.assertIsNone(supplier["max_disclosed_seal_amount_yuan"])
        self.assertEqual(supplier["seal_amount_state"], "unit_unverified")
        self.assertTrue(any(f["code"] == "supplier_price_disagreement" for f in result["quality_flags"]))
        verified = self.calculate(rows, pools, seal_amount_unit_verified=True)["limits"]["supplier_reported"]
        self.assertEqual(verified["max_disclosed_seal_amount_yuan"], "9000000")
        self.assertIn("不是盘中峰值", verified["seal_amount_scope"])

    def test_amount_units_top20_and_previous_total_change(self):
        rows = [quote(i, amount=str(i)) for i in range(1, 22)]
        previous = [quote(i, day=PREVIOUS, amount="2") for i in range(1, 22)]
        result = self.calculate(rows, previous_daily=previous)
        turnover = result["turnover"]
        self.assertEqual(turnover["amount_yuan"], "231000")
        self.assertEqual(turnover["previous"]["amount_yuan"], "42000")
        self.assertEqual(turnover["change_pct"], "450")
        self.assertEqual(Decimal(turnover["top20_share_pct"]), Decimal(230) * 100 / 231)
        rows[0]["amount"] = None
        incomplete = self.calculate(rows)["turnover"]
        self.assertIsNone(incomplete["amount_yuan"])
        self.assertEqual(incomplete["known_amount_yuan"], "230000")
        self.assertEqual(incomplete["missing_n"], 1)
        self.assertIsNone(incomplete["top20_share_pct"])

    def test_frozen_yesterday_cohorts_keep_missing_today_and_exclude_new_names(self):
        rows = [quote(1, "20", open="10.5"), quote(3, "-20", open="9"),
                quote(4, "10"), quote(5), quote(6, "20")]
        previous = [pool(1, day=PREVIOUS), pool(2, day=PREVIOUS),
                    pool(3, day=PREVIOUS, height="2"), pool(4, "Z", day=PREVIOUS)]
        previous_quote = quote(5, day=PREVIOUS)
        result = self.calculate(rows, [pool(1, height="2"), pool(3, "D"), pool(6)],
                                previous_daily=[previous_quote], previous_price_limits=[cap(previous_quote)],
                                previous_pools=previous, prior_pools=[pool(5, day=PRIOR)])
        first = result["relay"]["first_board"]
        self.assertEqual((first["N"], first["valid_n"], first["missing_n"]), (2, 1, 1))
        self.assertEqual(first["promotion_n"], 1)
        self.assertIsNone(first["promotion_pct"])
        self.assertEqual((first["open_median_pct"], first["close_median_pct"]), ("5", "20"))
        self.assertIsNone(first["open_mean_pct"])
        self.assertIsNone(first["close_mean_pct"])
        self.assertEqual((first["known_open_mean_pct"], first["known_close_mean_pct"]), ("5", "20"))
        self.assertEqual([s["ts_code"] for s in first["samples"]], ["000001.SZ", "000002.SZ"])
        consecutive = result["relay"]["consecutive"]
        self.assertEqual((consecutive["N"], consecutive["down_limit_n"], consecutive["down_limit_pct"]), (1, 1, "100"))
        self.assertEqual(result["relay"]["broken"]["samples"][0]["ts_code"], "000004.SZ")
        self.assertEqual(result["relay"]["failed"]["samples"][0]["ts_code"], "000005.SZ")
        all_up = result["relay"]["yesterday_limit_up"]
        self.assertEqual((all_up["N"], all_up["close_valid_n"]), (3, 2))
        self.assertIsNone(all_up["mean_close_pct"])
        self.assertEqual(all_up["known_mean_close_pct"], "0")

    def test_failed_classification_uses_all_prior_u_prices_and_keeps_unknown_candidates(self):
        previous = [quote(1, day=PREVIOUS, amount=None, vol="100"),
                    quote(2, "5", day=PREVIOUS),
                    quote(4, day=PREVIOUS, amount="0", vol="0"),
                    quote(5, day=PREVIOUS), quote(6, day=PREVIOUS),
                    quote(7, "30", day=PREVIOUS)]
        previous_caps = [cap(previous[0]), cap(previous[1], up="10.5", down="9.5"),
                         cap(quote(3, day=PREVIOUS)), cap(previous[2]),
                         cap(previous[4], up="0", down="0"), cap(previous[5])]
        result = self.calculate([quote(1, "2"), quote(9, "20")], [pool(9)],
                                previous_daily=previous, previous_price_limits=previous_caps,
                                previous_pools=[pool(1, day=PREVIOUS)],
                                prior_pools=[pool(i, day=PRIOR) for i in range(1, 8)])
        classification = result["relay"]["failed_classification"]
        self.assertEqual((classification["N"], classification["confirmed_failed_n"],
                          classification["continued_n"], classification["unknown_n"]), (7, 1, 1, 5))
        self.assertEqual(classification["valid_n"] + classification["unknown_n"], classification["N"])
        self.assertEqual(classification["source_conflict_n"], 1)
        samples = {row["ts_code"]: row for row in classification["samples"]}
        # A contrary explicit U record cannot hide an actual failure.
        self.assertEqual(samples["000001.SZ"]["classification"], "confirmed_failed")
        self.assertTrue(samples["000001.SZ"]["source_conflict"])
        # Absence from the supplier pool, including changed ST scope, does not
        # override an actual closing seal at the day's five-percent limit.
        self.assertEqual(samples["000002.SZ"]["classification"], "continued")
        self.assertTrue(samples["000002.SZ"]["pool_membership_difference"])
        self.assertIsNone(samples["000002.SZ"]["source_conflict"])
        expected_reasons = {3: "previous_quote_missing", 4: "previous_trading_unverified",
                            5: "previous_price_limit_missing", 6: "previous_price_limit_not_applicable",
                            7: "previous_close_above_limit"}
        for symbol, reason in expected_reasons.items():
            self.assertEqual(samples[f"{symbol:06d}.SZ"]["classification"], "unknown")
            self.assertIn(reason, samples[f"{symbol:06d}.SZ"]["reasons"])
        failed = result["relay"]["failed"]
        self.assertEqual((failed["N"], failed["candidate_N"], failed["classification_unknown_n"]), (1, 7, 5))
        self.assertEqual([s["ts_code"] for s in failed["samples"]], ["000001.SZ"])
        self.assertFalse(failed["membership_complete"])
        self.assertIsNone(failed["close_mean_pct"])
        self.assertEqual(failed["known_close_mean_pct"], "2")
        self.assertEqual(result["relay"]["previous_pool_exits"]["N"], 6)

    def test_failed_without_previous_limits_stays_unverified_and_missing_today_keeps_n(self):
        previous = quote(1, day=PREVIOUS)
        arguments = {"previous_daily": [previous], "previous_pools": [],
                     "prior_pools": [pool(1, day=PRIOR)]}
        missing_caps = self.calculate([quote(1)], **arguments)["relay"]
        self.assertEqual(missing_caps["failed_classification"]["unknown_n"], 1)
        self.assertEqual(missing_caps["failed"]["N"], 0)
        self.assertEqual(missing_caps["failed"]["state"], "partial")
        self.assertEqual(missing_caps["previous_pool_exits"]["N"], 1)
        # Price verification works even if the T-1 supplier pool itself was not
        # loaded; neither that absence nor today's missing quote changes N.
        arguments.update(previous_pools=None, previous_price_limits=[cap(previous)])
        confirmed = self.calculate([], **arguments)["relay"]
        self.assertEqual(confirmed["failed_classification"]["confirmed_failed_n"], 1)
        self.assertEqual((confirmed["failed"]["N"], confirmed["failed"]["valid_n"],
                          confirmed["failed"]["missing_n"]), (1, 0, 1))
        self.assertTrue(confirmed["failed"]["membership_complete"])

    def test_equal_weight_means_differ_from_medians_and_use_each_observation_denominator(self):
        rows = [quote(1), quote(2), quote(3, "12", open="10.9")]
        previous = [pool(i, day=PREVIOUS) for i in (1, 2, 3)]
        result = self.calculate(rows, previous_pools=previous)
        first = result["relay"]["first_board"]
        self.assertEqual((first["N"], first["open_valid_n"], first["close_valid_n"]), (3, 3, 3))
        self.assertEqual((first["open_mean_pct"], first["close_mean_pct"]), ("3", "4"))
        self.assertEqual((first["open_median_pct"], first["close_median_pct"]), ("0", "0"))
        all_up = result["relay"]["yesterday_limit_up"]
        self.assertEqual(all_up["close_mean_pct"], "4")
        self.assertEqual(all_up["mean_close_pct"], all_up["close_mean_pct"])
        rows[2]["open"] = None
        partial = self.calculate(rows, previous_pools=previous)["relay"]["yesterday_limit_up"]
        self.assertEqual((partial["N"], partial["open_valid_n"], partial["close_valid_n"]), (3, 2, 3))
        self.assertIsNone(partial["open_mean_pct"])
        self.assertEqual(partial["known_open_mean_pct"], "0")
        self.assertEqual(partial["close_mean_pct"], "4")
        missing = self.calculate(rows[:2], previous_pools=previous)["relay"]["yesterday_limit_up"]
        self.assertEqual((missing["N"], missing["close_valid_n"], missing["missing_n"]), (3, 2, 1))
        self.assertIsNone(missing["close_mean_pct"])
        self.assertEqual(missing["known_close_mean_pct"], "0")

    def test_missing_limits_and_unrestricted_shares_are_not_confirmed_zero(self):
        rows = [quote(1, "20")]
        missing = build_daily_metrics(trade_day=TODAY, daily=rows, price_limits=None,
                                      pools=[pool(1)])
        self.assertIsNone(missing["limits"]["price_verified"]["up"])
        self.assertEqual(missing["limits"]["price_verified"]["unverified_n"], 1)
        self.assertEqual(missing["limits"]["supplier_reported"]["up"], 1)
        unrestricted = build_daily_metrics(trade_day=TODAY, daily=rows,
                                           price_limits=[cap(rows[0], up="0", down="0")], pools=[])
        verified = unrestricted["limits"]["price_verified"]
        self.assertEqual(verified["no_price_limit_n"], 1)
        self.assertEqual(verified["valid_n"], 0)
        self.assertIsNone(verified["up"])

    def test_unknown_yesterday_height_keeps_group_rates_unconfirmed(self):
        result = self.calculate([quote(1, "20"), quote(2, "20")],
                                previous_pools=[pool(1, day=PREVIOUS),
                                                pool(2, day=PREVIOUS, height=None)],
                                previous_daily=[], prior_pools=[])
        self.assertEqual(result["relay"]["unclassified_previous_limit_up_n"], 1)
        first = result["relay"]["first_board"]
        self.assertEqual((first["N"], first["promotion_n"]), (1, 1))
        self.assertEqual(first["state"], "partial")
        self.assertIsNone(first["promotion_pct"])
        self.assertEqual(result["relay"]["yesterday_limit_up"]["N"], 2)
        self.assertEqual(result["relay"]["yesterday_limit_up"]["mean_close_pct"], "20")

    def test_not_loaded_and_successfully_empty_tables_are_distinct(self):
        missing = build_daily_metrics(trade_day=TODAY, daily=None, price_limits=None, pools=None)
        empty = build_daily_metrics(trade_day=TODAY, daily=[], price_limits=[], pools=[],
                                    previous_daily=[], previous_price_limits=[], previous_pools=[], prior_pools=[])
        self.assertIsNone(missing["sample"]["N"])
        self.assertIsNone(missing["breadth"]["up"])
        self.assertIsNone(missing["limits"]["supplier_reported"]["up"])
        self.assertEqual(empty["sample"]["N"], 0)
        self.assertEqual(empty["breadth"]["up"], 0)
        self.assertEqual(empty["limits"]["supplier_reported"]["up"], 0)
        self.assertIsNone(empty["turnover"]["amount_yuan"])
        self.assertIsNone(empty["limits"]["supplier_reported"]["broken_rate_pct"])
        self.assertEqual(empty["relay"]["first_board"]["state"], "empty")
        self.assertEqual(empty["relay"]["failed_classification"]["N"], 0)
        self.assertEqual(empty["relay"]["failed_classification"]["unknown_n"], 0)
        for result in (missing, empty):
            for key in ("first_board", "consecutive", "broken", "failed", "yesterday_limit_up"):
                for field in ("open_mean_pct", "close_mean_pct", "known_open_mean_pct", "known_close_mean_pct"):
                    self.assertIsNone(result["relay"][key][field])

    def test_date_pollution_and_duplicate_identity_are_rejected(self):
        row = quote(1)
        invalid = [
            {"daily": [quote(1, day="20261026")]},
            {"daily": [row, quote(2, day=PREVIOUS)]},
            {"daily": [row, copy.deepcopy(row)]},
            {"price_limits": [cap(row), cap(row)]},
            {"pools": [pool(1), pool(1, "Z")]},
            {"previous_daily": [quote(1)]},
            {"previous_price_limits": [cap(quote(1))]},
            {"previous_price_limits": [cap(quote(1, day=PREVIOUS)), cap(quote(1, day=PREVIOUS))]},
            {"previous_daily": [quote(1, day=PREVIOUS)], "previous_price_limits": [cap(quote(1, day=PRIOR))]},
            {"previous_daily": [quote(1, day=PREVIOUS)], "previous_pools": [pool(1, day=PRIOR)]},
            {"previous_pools": [pool(1, day=PREVIOUS)], "prior_pools": [pool(1, day=PREVIOUS)]},
        ]
        for updates in invalid:
            with self.subTest(updates=updates):
                arguments = {"trade_day": TODAY, "daily": [row], "price_limits": [cap(row)], "pools": []}
                arguments.update(updates)
                with self.assertRaises(ValueError):
                    build_daily_metrics(**arguments)

    def test_invalid_numbers_do_not_become_zero_and_calculation_does_not_mutate_input(self):
        rows = [quote(1, "0", amount="NaN"), quote(2, "0", amount="0")]
        rows[0]["pct_chg"] = "Infinity"
        pools = [pool(1, height="1.5")]
        original = copy.deepcopy((rows, pools))
        result = self.calculate(rows, pools)
        self.assertEqual((rows, pools), original)
        self.assertEqual(result["breadth"]["flat"], 1)
        self.assertEqual(result["turnover"]["known_amount_yuan"], "0")
        self.assertIsNone(result["turnover"]["amount_yuan"])
        self.assertEqual(result["limits"]["supplier_reported"]["height_missing_n"], 1)
        self.assertEqual(result, self.calculate(rows, pools))
        json.dumps(result, allow_nan=False)


class DailyReviewDistributionTests(unittest.TestCase):
    bands = (
        "limit_down", "down_ge7", "down_5_7", "down_3_5", "down_0_3", "flat",
        "up_0_3", "up_3_5", "up_5_7", "up_ge7", "limit_up",
    )

    def assert_conservation(self, result):
        breadth = result["breadth"]
        distribution = breadth["distribution"]
        bins = distribution["bins"]
        self.assertEqual(tuple(bins), self.bands)
        self.assertEqual(distribution["version"], "absolute-limits-v1")
        self.assertEqual(distribution["valid_n"], breadth["valid_n"])
        self.assertEqual(distribution["missing_n"], breadth["missing_n"])
        self.assertEqual(sum(bins.values()), breadth["valid_n"])
        self.assertEqual(sum(bins[key] for key in self.bands[:5]), breadth["down"])
        self.assertEqual(bins["flat"], breadth["flat"])
        self.assertEqual(sum(bins[key] for key in self.bands[6:]), breadth["up"])
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in bins.values()))

    def test_symmetric_boundaries_and_one_percent_stays_in_zero_to_three(self):
        cases = [
            ("-7", "down_ge7"), ("-6.999", "down_5_7"), ("-5", "down_5_7"),
            ("-4.999", "down_3_5"), ("-3", "down_3_5"), ("-2.999", "down_0_3"),
            ("-1", "down_0_3"), ("-.001", "down_0_3"), ("-0", "flat"), ("0", "flat"),
            (".001", "up_0_3"), ("1", "up_0_3"), ("2.999", "up_0_3"),
            ("3", "up_3_5"), ("4.999", "up_3_5"), ("5", "up_5_7"),
            ("6.999", "up_5_7"), ("7", "up_ge7"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                row = quote(1, value)
                result = build_daily_metrics(trade_day=TODAY, daily=[row],
                                             price_limits=[cap(row)], pools=[])
                self.assertEqual(result["breadth"]["distribution"]["bins"][expected], 1)
                self.assertEqual(result["breadth"]["distribution"]["state"], "complete")
                self.assert_conservation(result)
        rows = [quote(i + 1, value) for i, (value, _) in enumerate(cases)]
        rows.append(quote(999, "20", ts_code="830001.BJ"))
        result = build_daily_metrics(trade_day=TODAY, daily=rows,
                                     price_limits=[cap(row) for row in rows], pools=[])
        self.assertEqual(list(result["breadth"]["bins"].values()), [1, 2, 2, 3, 2, 3, 2, 2, 1])
        self.assertEqual(list(result["breadth"]["distribution"]["bins"].values()),
                         [0, 1, 2, 2, 3, 2, 3, 2, 2, 1, 0])
        self.assertEqual(result["universe"]["excluded_bse_n"], 1)
        self.assert_conservation(result)

    def test_actual_ten_twenty_five_and_rounded_prices_override_return_bands(self):
        rows = [quote(1, "10"), quote(2, "-10"), quote(3, "20"), quote(4, "-20"),
                quote(5, "5"), quote(6, "-5"),
                quote(7, "9.970674", pre_close="3.41", close="3.75", high="3.75"),
                quote(8, "-9.970674", pre_close="3.41", close="3.07", high="3.41"),
                quote(9, "10"), quote(10, "-10")]
        caps = [cap(rows[0], up="11", down="9"), cap(rows[1], up="11", down="9"),
                cap(rows[2]), cap(rows[3]),
                cap(rows[4], up="10.5", down="9.5"), cap(rows[5], up="10.5", down="9.5"),
                cap(rows[6], up="3.75", down="3.07"), cap(rows[7], up="3.75", down="3.07"),
                cap(rows[8]), cap(rows[9])]
        # The supplier omits actual seals and labels two non-seals as U/D.
        # Neither omission nor pool membership determines distribution endpoints.
        result = build_daily_metrics(trade_day=TODAY, daily=rows, price_limits=caps,
                                     pools=[pool(9), pool(10, "D")])
        distribution = result["breadth"]["distribution"]
        self.assertEqual(distribution["state"], "complete")
        self.assertEqual(list(distribution["bins"].values()), [4, 1, 0, 0, 0, 0, 0, 0, 0, 1, 4])
        self.assertEqual(distribution["limit_unverified_n"], 0)
        self.assertEqual(result["limits"]["supplier_reported"]["up"], 1)
        self.assertEqual(result["limits"]["price_verified"]["up"], 4)
        self.assert_conservation(result)

    def test_unknown_limits_and_closes_keep_returns_and_only_valid_no_limit_rows_count(self):
        rows = [quote(1, "10"), quote(2, "-7"), quote(3, "3"),
                quote(4, "-3", close=None), quote(5, "7", close="13"),
                quote(6, "1"), quote(7, "-1", close="NaN"), quote(8),
                quote(9, "30"), quote(10, None), quote(11, None)]
        caps = [cap(rows[1], up=None), cap(rows[2], up="8", down="12"),
                cap(rows[3]), cap(rows[4]), cap(rows[5], up="0", down="12"),
                cap(rows[6]), cap(rows[7]), cap(rows[8], up="0", down="0"),
                cap(rows[9], up="0", down="0")]
        result = build_daily_metrics(trade_day=TODAY, daily=rows, price_limits=caps, pools=[])
        distribution = result["breadth"]["distribution"]
        self.assertEqual((distribution["state"], distribution["valid_n"], distribution["missing_n"]),
                         ("partial", 9, 2))
        self.assertEqual((distribution["limit_unverified_n"], distribution["no_limit_n"]), (7, 1))
        self.assertEqual(list(distribution["bins"].values()), [0, 1, 0, 1, 1, 1, 1, 1, 0, 3, 0])
        self.assertEqual(result["limits"]["price_verified"]["no_price_limit_n"], 2)
        self.assert_conservation(result)

    def test_missing_limit_table_and_unrestricted_returns_are_distinct(self):
        rows = [quote(1, "20"), quote(2, "-20")]
        missing = build_daily_metrics(trade_day=TODAY, daily=rows, price_limits=None,
                                      pools=[pool(1), pool(2, "D")])
        unrestricted = build_daily_metrics(
            trade_day=TODAY, daily=rows, pools=[],
            price_limits=[cap(row, up="0", down="0") for row in rows])
        for result, state, unverified, no_limit in (
                (missing, "partial", 2, 0), (unrestricted, "complete", 0, 2)):
            distribution = result["breadth"]["distribution"]
            self.assertEqual((distribution["state"], distribution["limit_unverified_n"],
                              distribution["no_limit_n"]), (state, unverified, no_limit))
            self.assertEqual(list(distribution["bins"].values()), [0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0])
            self.assert_conservation(result)

    def test_not_loaded_empty_and_all_missing_returns_have_distinct_states(self):
        missing = build_daily_metrics(trade_day=TODAY, daily=None, price_limits=None, pools=None)
        distribution = missing["breadth"]["distribution"]
        self.assertEqual(tuple(distribution["bins"]), self.bands)
        self.assertEqual(distribution, {
            "version": "absolute-limits-v1", "state": "not_loaded",
            "bins": dict.fromkeys(self.bands), "valid_n": 0, "missing_n": None,
            "limit_unverified_n": None, "no_limit_n": None,
        })
        rows = [quote(i + 1) for i in range(6)]
        for row, value in zip(rows, [None, "", "--", "NaN", "Infinity", False], strict=True):
            row["pct_chg"] = value
        for sample, state in (([], "empty"), (rows, "partial")):
            result = build_daily_metrics(trade_day=TODAY, daily=sample,
                                         price_limits=[cap(row) for row in sample], pools=[])
            distribution = result["breadth"]["distribution"]
            self.assertEqual((distribution["state"], distribution["valid_n"], distribution["missing_n"]),
                             (state, 0, len(sample)))
            self.assertEqual((distribution["limit_unverified_n"], distribution["no_limit_n"]), (0, 0))
            self.assertEqual(list(distribution["bins"].values()), [0] * 11)
            self.assert_conservation(result)

    def test_price_seal_direction_conflicts_keep_breadth_sign_and_report_quality(self):
        rows = [quote(1, "-5", close="11", high="11"), quote(2, "5", close="9", high="10"),
                quote(3, "0", close="11", high="11"), quote(4, "-0", close="9", high="10")]
        result = build_daily_metrics(trade_day=TODAY, daily=rows, pools=[],
                                     price_limits=[cap(row, up="11", down="9") for row in rows])
        distribution = result["breadth"]["distribution"]
        self.assertEqual((distribution["state"], distribution["limit_unverified_n"]), ("partial", 4))
        self.assertEqual(list(distribution["bins"].values()), [0, 0, 1, 0, 0, 2, 0, 0, 1, 0, 0])
        self.assertEqual((result["limits"]["price_verified"]["up"],
                          result["limits"]["price_verified"]["down"]), (2, 2))
        conflicts = [flag for flag in result["quality_flags"] if flag["code"] == "limit_pct_direction_mismatch"]
        self.assertEqual(conflicts, [{
            "code": "limit_pct_direction_mismatch", "table": "daily", "field": "pct_chg", "count": 4,
            "sample_ts_codes": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
        }])
        self.assert_conservation(result)

    def test_unverified_high_does_not_hide_price_verified_closing_distribution(self):
        row = quote(1, "2", high=None)
        result = build_daily_metrics(trade_day=TODAY, daily=[row], price_limits=[cap(row)], pools=[])
        distribution = result["breadth"]["distribution"]
        self.assertEqual((distribution["state"], distribution["limit_unverified_n"]), ("complete", 0))
        self.assertEqual(result["limits"]["price_verified"]["state"], "partial")
        self.assertEqual(distribution["bins"]["up_0_3"], 1)
        self.assert_conservation(result)


class DailyReviewIntradayLimitsTests(unittest.TestCase):
    def row(self, index, **changes):
        return quote(index, **{"low": "9.5", **changes})

    def calculate(self, rows, caps=None, pools=None):
        return build_daily_metrics(
            trade_day=TODAY, daily=rows,
            price_limits=[cap(row) for row in rows] if caps is None else caps,
            pools=[] if pools is None else pools)

    def assert_counts(self, intraday, *, up, down, touched_up, touched_down,
                      close_up, close_down):
        self.assertEqual(intraday["version"], "ohlc-limits-v1")
        self.assertEqual(intraday["state"], "complete")
        self.assertEqual((intraday["opened_up"], intraday["opened_down"]), (up, down))
        self.assertEqual((intraday["touched_up"], intraday["touched_down"]),
                         (touched_up, touched_down))
        self.assertEqual((intraday["close_up"], intraday["close_down"]),
                         (close_up, close_down))
        for side in ("up", "down"):
            self.assertEqual(intraday[f"{side}_valid_n"], intraday["N"])
            self.assertEqual(intraday[f"{side}_unknown_n"], 0)
            self.assertEqual(intraday[f"opened_{side}"] + intraday[f"close_{side}"],
                             intraday[f"touched_{side}"])
            for field in ("opened", "touched", "close"):
                self.assertEqual(intraday[f"known_{field}_{side}"],
                                 intraday[f"{field}_{side}"])

    def test_opened_up_down_closing_seals_and_both_sides_are_distinct(self):
        rows = [self.row(1, high="12"), self.row(2, low="8"),
                self.row(3, close="12", high="12"),
                self.row(4, close="8", low="8"),
                self.row(5, high="12", low="8"), self.row(6)]
        result = self.calculate(rows, pools=[pool(6), pool(1, "D")])
        intraday = result["limits"]["intraday"]
        self.assert_counts(intraday, up=2, down=2, touched_up=3, touched_down=3,
                           close_up=1, close_down=1)
        self.assertEqual(intraday["N"], 6)
        self.assertEqual(intraday["quality_flags"], [])
        # Contrary supplier membership is not an intraday touch observation.
        self.assertEqual(result["limits"]["supplier_reported"]["up"], 1)
        self.assertEqual(result["limits"]["supplier_reported"]["down"], 1)

    def test_real_twenty_five_and_rounded_limit_prices_replace_fixed_percentages(self):
        rows = [self.row(1, high="12", low="8"),
                self.row(2, high="10.5", low="9.5"),
                self.row(3, close="10.5", high="10.5", low="10"),
                self.row(4, pre_close="3.41", open="3.41", close="3.41", high="3.75", low="3.07")]
        caps = [cap(rows[0]), cap(rows[1], up="10.5", down="9.5"),
                cap(rows[2], up="10.5", down="9.5"),
                cap(rows[3], up="3.75", down="3.07")]
        intraday = self.calculate(rows, caps)["limits"]["intraday"]
        self.assert_counts(intraday, up=3, down=3, touched_up=4, touched_down=3,
                           close_up=1, close_down=0)

    def test_closing_endpoint_proves_a_touch_when_matching_extreme_is_missing(self):
        rows = [self.row(1, close="12", high=None),
                self.row(2, close="8", low=None)]
        intraday = self.calculate(rows)["limits"]["intraday"]
        self.assert_counts(intraday, up=0, down=0, touched_up=1, touched_down=1,
                           close_up=1, close_down=1)
        self.assertEqual({flag["field"] for flag in intraday["quality_flags"]},
                         {"high", "low"})

    def test_missing_extremes_are_unknown_per_side_and_withhold_complete_totals(self):
        rows = [self.row(1, high="12"), self.row(2, high=None),
                self.row(3, low=None)]
        intraday = self.calculate(rows)["limits"]["intraday"]
        self.assertEqual(intraday["state"], "partial")
        self.assertEqual((intraday["N"], intraday["up_valid_n"], intraday["down_valid_n"]),
                         (3, 2, 2))
        self.assertEqual((intraday["up_unknown_n"], intraday["down_unknown_n"]), (1, 1))
        self.assertEqual((intraday["known_opened_up"], intraday["known_opened_down"]), (1, 0))
        for side in ("up", "down"):
            for field in ("opened", "touched", "close"):
                self.assertIsNone(intraday[f"{field}_{side}"])
        # Incomplete intraday evidence never changes old closing-price coverage.
        result = self.calculate(rows)
        self.assertEqual(result["limits"]["price_verified"]["valid_n"], 3)
        self.assertFalse(any(flag["field"] == "low" for flag in result["quality_flags"]))

    def test_missing_caps_close_and_invalid_ranges_do_not_become_zero(self):
        rows = [self.row(i) for i in range(1, 9)]
        rows[1]["close"] = None
        rows[2]["close"] = "NaN"
        rows[3]["close"] = "13"
        caps = [cap(row) for row in rows[1:]]
        caps[3]["up_limit"] = None
        caps[4].update(up_limit="8", down_limit="12")
        caps[5].update(up_limit="0", down_limit="8")
        caps[6]["down_limit"] = "Infinity"
        intraday = self.calculate(rows, caps)["limits"]["intraday"]
        self.assertEqual((intraday["state"], intraday["up_valid_n"], intraday["down_valid_n"]),
                         ("partial", 0, 0))
        self.assertEqual((intraday["up_unknown_n"], intraday["down_unknown_n"]), (8, 8))
        self.assertEqual((intraday["known_opened_up"], intraday["known_opened_down"]), (0, 0))
        self.assertIsNone(intraday["opened_up"])
        self.assertIsNone(intraday["opened_down"])
        self.assertTrue({"missing_price_limits", "missing_value", "invalid_value",
                         "invalid_price_limit_range", "close_outside_price_limits"}.issubset(
                             {flag["code"] for flag in intraday["quality_flags"]}))

    def test_supplied_ohlc_contradictions_invalidate_only_the_affected_side(self):
        cases = [
            ({"high": "13"}, 0, 1, "high_outside_price_limits"),
            ({"high": "9.75"}, 0, 1, "high_outside_price_limits"),
            ({"low": "7"}, 1, 0, "low_outside_price_limits"),
            ({"low": "11"}, 0, 0, "invalid_ohlc_range"),
            ({"high": "9", "low": "11"}, 0, 0, "invalid_ohlc_range"),
            ({"high": "NaN"}, 0, 1, "invalid_value"),
            ({"low": False}, 1, 0, "invalid_value"),
            ({"close": "12", "high": "11"}, 0, 1, "high_outside_price_limits"),
            ({"close": "8", "low": "9"}, 1, 0, "low_outside_price_limits"),
        ]
        for changes, up_valid, down_valid, code in cases:
            with self.subTest(changes=changes):
                intraday = self.calculate([self.row(1, **changes)])["limits"]["intraday"]
                self.assertEqual((intraday["up_valid_n"], intraday["down_valid_n"]),
                                 (up_valid, down_valid))
                self.assertEqual(intraday["state"], "partial")
                self.assertIn(code, {flag["code"] for flag in intraday["quality_flags"]})

    def test_unrestricted_prices_are_not_touches_and_bse_is_excluded(self):
        rows = [self.row(1, close="30", high="40", low="1"),
                self.row(2, ts_code="830001.BJ", high="12", low="8")]
        caps = [cap(rows[0], up="0", down="0"), cap(rows[1])]
        intraday = self.calculate(rows, caps)["limits"]["intraday"]
        self.assert_counts(intraday, up=0, down=0, touched_up=0, touched_down=0,
                           close_up=0, close_down=0)
        self.assertEqual((intraday["N"], intraday["no_price_limit_n"]), (1, 1))

    def test_strict_zero_trade_placeholders_confirm_no_touch_without_claiming_suspension(self):
        row = self.row(1, pre_close="3.57", close="3.57", open="0.0", high="0.0",
                       low="0.0", vol="0.0", amount="0.0", pct_chg="0.0")
        original = copy.deepcopy(row)
        result = self.calculate([row], [cap(row, up="3.93", down="3.21")])
        intraday = result["limits"]["intraday"]
        self.assert_counts(intraday, up=0, down=0, touched_up=0, touched_down=0,
                           close_up=0, close_down=0)
        self.assertEqual(intraday["no_trade_n"], 1)
        self.assertEqual(intraday["quality_flags"], [])
        self.assertEqual(row, original)
        # Old price verification retains its existing high-value quality flag.
        self.assertEqual(result["limits"]["price_verified"]["state"], "partial")
        self.assertTrue(any(flag["field"] == "high" for flag in result["quality_flags"]))

    def test_zero_high_low_alone_or_inconsistent_zero_trade_fields_stay_unknown(self):
        baseline = {"open": "0", "high": "0", "low": "0", "vol": "0",
                    "amount": "0", "pct_chg": "0", "close": "10", "pre_close": "10"}
        cases = [{"vol": "1"}, {"amount": "1"}, {"pct_chg": "0.001"}, {"open": "1"},
                 {"vol": None}, {"vol": ""}, {"vol": "NaN"}, {"vol": "Infinity"},
                 {"vol": False}, {"amount": "-1"}, {"pre_close": "9.99"},
                 {"pre_close": "NaN"}, {"pre_close": None}, {"pre_close": False}]
        for changes in cases:
            with self.subTest(changes=changes):
                row = self.row(1, **{**baseline, **changes})
                intraday = self.calculate([row])["limits"]["intraday"]
                self.assertEqual((intraday["state"], intraday["no_trade_n"]), ("partial", 0))
                self.assertEqual((intraday["up_unknown_n"], intraday["down_unknown_n"]), (1, 1))
                self.assertIsNone(intraday["touched_up"])
                self.assertIsNone(intraday["touched_down"])
        for field in ("open", "high", "low", "vol", "amount", "pct_chg", "pre_close"):
            with self.subTest(missing_field=field):
                row = self.row(1, **baseline)
                del row[field]
                intraday = self.calculate([row])["limits"]["intraday"]
                self.assertEqual((intraday["state"], intraday["no_trade_n"]), ("partial", 0))
                self.assertEqual((intraday["up_unknown_n"], intraday["down_unknown_n"]), (1, 1))

    def test_zero_trade_placeholder_requires_usable_close_and_price_limit_range(self):
        row = self.row(1, pre_close="10", close="10", open="0", high="0", low="0",
                       vol="0", amount="0", pct_chg="0")
        cases = [({"close": None}, {}), ({"close": "NaN"}, {}),
                 ({"close": "13", "pre_close": "13"}, {}),
                 ({}, {"up": "8", "down": "12"}), ({}, {"up": None})]
        for changes, limits in cases:
            with self.subTest(changes=changes, limits=limits):
                candidate = {**row, **changes}
                intraday = self.calculate([candidate], [cap(candidate, **limits)])["limits"]["intraday"]
                self.assertEqual((intraday["state"], intraday["no_trade_n"]), ("partial", 0))
                self.assertEqual((intraday["up_unknown_n"], intraday["down_unknown_n"]), (1, 1))

    def test_not_loaded_empty_and_missing_limit_table_are_distinct(self):
        missing = build_daily_metrics(trade_day=TODAY, daily=None,
                                      price_limits=None, pools=None)["limits"]["intraday"]
        empty = self.calculate([])["limits"]["intraday"]
        uncapped = build_daily_metrics(trade_day=TODAY, daily=[self.row(1)],
                                       price_limits=None, pools=[])["limits"]["intraday"]
        self.assertEqual((missing["state"], missing["N"]), ("not_loaded", None))
        self.assertEqual((empty["state"], empty["N"]), ("empty", 0))
        self.assertEqual((uncapped["state"], uncapped["N"]), ("partial", 1))
        self.assertIsNone(missing["no_trade_n"])
        self.assertEqual(empty["no_trade_n"], 0)
        for side in ("up", "down"):
            self.assertEqual(missing[f"{side}_valid_n"], 0)
            self.assertIsNone(missing[f"{side}_unknown_n"])
            self.assertIsNone(missing[f"known_opened_{side}"])
            self.assertIsNone(missing[f"opened_{side}"])
            self.assertEqual(empty[f"{side}_valid_n"], 0)
            self.assertEqual(empty[f"{side}_unknown_n"], 0)
            self.assertEqual(empty[f"opened_{side}"], 0)
            self.assertEqual(uncapped[f"{side}_unknown_n"], 1)
            self.assertIsNone(uncapped[f"opened_{side}"])


if __name__ == "__main__":
    unittest.main()
