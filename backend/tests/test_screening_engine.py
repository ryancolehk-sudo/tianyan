"""Synthetic pure-engine tests; no database, network, or live market claims.

Also runnable with standard-library unittest without loading database fixtures.
"""

import copy
import json
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

from app.screening.engine import ENGINE_VERSION, METRICS, evaluate, transitions

AT = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def row(code="600001.SH", **metrics):
    return {
        "code": code,
        "name": "合成标的",
        "instrument_id": UUID(int=1),
        "source_time": AT,
        "received_at": AT + timedelta(seconds=1),
        **metrics,
    }


def definition(*conditions, sort_field="price", sort_direction="asc"):
    return {
        "conditions": [
            {"field": field, "operator": operator, "value": value}
            for field, operator, value in conditions
        ],
        "sort_field": sort_field,
        "sort_direction": sort_direction,
    }


def observation(state, code="600001.SH", **changes):
    return {
        "code": code,
        "name": "合成标的",
        "source_time": AT.isoformat(),
        "state": state,
        **changes,
    }


class ScreeningEvaluationTests(unittest.TestCase):
    def test_equality_boundaries_for_every_allowed_operator(self):
        expected = {
            "gt": "not_matched",
            "gte": "matched",
            "lt": "not_matched",
            "lte": "matched",
            "eq": "matched",
        }
        for operator, state in expected.items():
            with self.subTest(operator=operator):
                result = evaluate([row(price="10.00")], definition(("price", operator, "10")))
                item = result["items"][0]
                self.assertEqual(item["state"], state)
                self.assertEqual(item["checks"][0]["state"], state)
                self.assertEqual(result["counts"][state], 1)

    def test_decimal_thresholds_do_not_round_to_float_or_context_precision(self):
        actual = "1.0000000000000000000000000000000000000001"
        with localcontext() as context:
            context.prec = 3
            result = evaluate([row(price=actual)], definition(("price", "gt", "1")))
        self.assertEqual(result["items"][0]["state"], "matched")
        self.assertEqual(result["items"][0]["metrics"]["price"], actual)

    def test_a_false_condition_wins_over_missing_in_either_order(self):
        conditions = [("price", "gt", "10"), ("volume_ratio", "gte", "1")]
        for ordered in [conditions, list(reversed(conditions))]:
            with self.subTest(order=ordered):
                item = evaluate([row(price="9")], definition(*ordered))["items"][0]
                self.assertEqual(item["state"], "not_matched")
                self.assertEqual(
                    {check["state"] for check in item["checks"]}, {"not_matched", "insufficient"}
                )

    def test_missing_is_unknown_even_when_the_threshold_is_zero(self):
        result = evaluate([row(), row("000001.SZ", price="0")], definition(("price", "eq", "0")))
        by_code = {item["code"]: item for item in result["items"]}
        self.assertEqual(by_code["600001.SH"]["state"], "insufficient")
        self.assertIsNone(by_code["600001.SH"]["checks"][0]["actual"])
        self.assertEqual(by_code["000001.SZ"]["state"], "matched")
        self.assertEqual(
            result["counts"], {"total": 2, "matched": 1, "not_matched": 0, "insufficient": 1}
        )

    def test_true_and_unknown_is_unknown(self):
        item = evaluate(
            [row(price="10")], definition(("price", "gte", "10"), ("amount_cny", "gt", "0"))
        )["items"][0]
        self.assertEqual(item["state"], "insufficient")
        self.assertEqual([check["state"] for check in item["checks"]], ["matched", "insufficient"])

    def test_zero_and_negative_values_are_real_values(self):
        item = evaluate(
            [row(price=0, pct_change="-3.5", speed_5m=Decimal("-0.25"), amount_cny=0.0)],
            definition(
                ("price", "eq", "0"),
                ("pct_change", "lt", "0"),
                ("speed_5m", "gte", "-0.25"),
                ("amount_cny", "lte", "0"),
            ),
        )["items"][0]
        self.assertEqual(item["state"], "matched")
        self.assertEqual(item["metrics"]["pct_change"], "-3.5")
        self.assertEqual(item["metrics"]["speed_5m"], "-0.25")

    def test_nonfinite_boolean_and_invalid_targets_are_rejected(self):
        for value in [
            "NaN",
            "sNaN",
            "Infinity",
            "-Infinity",
            float("nan"),
            float("inf"),
            Decimal("NaN"),
            True,
            False,
            None,
            "",
            "12%",
            {"value": 1},
        ]:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    evaluate([row(price="10")], definition(("price", "gt", value)))

    def test_nonfinite_boolean_and_invalid_observations_are_insufficient(self):
        for value in [
            "NaN",
            "Infinity",
            float("nan"),
            -float("inf"),
            Decimal("sNaN"),
            True,
            False,
            "",
            "--",
            "12%",
            [],
            {},
        ]:
            with self.subTest(value=repr(value)):
                item = evaluate([row(price=value)], definition(("price", "gte", "0")))["items"][0]
                self.assertEqual(item["state"], "insufficient")
                self.assertIsNone(item["metrics"]["price"])
                self.assertIsNone(item["checks"][0]["actual"])
                self.assertIn("有限数值", item["checks"][0]["reason"])

    def test_unknown_metric_operator_and_sort_never_execute_expressions(self):
        invalid = [
            definition(("__import__('os')", "gt", "1")),
            definition(("price", "eval", "1")),
            definition(sort_field="missing"),
            definition(sort_direction="sideways"),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                evaluate([row(price="1")], value)

    def test_malformed_definitions_and_rows_fail_instead_of_matching(self):
        for value in [
            None,
            [],
            {},
            {"conditions": "price > 0"},
            {**definition(), "conditions": [None]},
            definition(([], "gt", "1")),
            definition(("price", [], "1")),
        ]:
            with self.subTest(definition=value), self.assertRaises(ValueError):
                evaluate([row(price=1)], value)
        for rows in [None, (), [None], [{}], [row(code="")], [row(code=True)]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                evaluate(rows, definition(("price", "gt", "0")))

    def test_sort_is_numeric_null_last_and_code_stable_in_both_directions(self):
        rows = [
            row("600005.SH", price=None),
            row("600003.SH", price="2"),
            row("600004.SH", price="10"),
            row("600002.SH", price="2"),
            row("600001.SH", price="-1"),
            row("600006.SH", price="NaN"),
        ]
        expected = {"asc": [1, 2, 3, 4, 5, 6], "desc": [4, 2, 3, 1, 5, 6]}
        for direction in expected:
            with self.subTest(direction=direction):
                result = evaluate(rows, definition(sort_direction=direction))
                self.assertEqual(
                    [item["code"] for item in result["items"]],
                    [f"60000{suffix}.SH" for suffix in expected[direction]],
                )

    def test_all_rows_and_only_whitelisted_metrics_are_returned_without_mutation(self):
        rows = [row(price="1", custom_score="99"), row("000001.SZ", price="11")]
        rules = definition(("price", "gt", "10"))
        before_rows, before_rules = copy.deepcopy(rows), copy.deepcopy(rules)
        result = evaluate(rows, rules)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(
            set(result["items"][0]["metrics"]),
            {
                "price",
                "pct_change",
                "speed_5m",
                "amount_cny",
                "turnover_rate",
                "float_mv_cny",
                "open_pct",
                "previous_height",
                "volume_ratio",
            },
        )
        self.assertNotIn("custom_score", result["items"][0])
        self.assertTrue(all(set(metadata) == {"label", "unit"} for metadata in METRICS.values()))
        self.assertEqual(result["items"][0]["source_time"], AT.isoformat())
        self.assertEqual(result["items"][0]["instrument_id"], str(UUID(int=1)))
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(rows, before_rows)
        self.assertEqual(rules, before_rules)

    def test_empty_rows_have_zero_counts_and_an_empty_and_is_true(self):
        self.assertEqual(
            evaluate([], definition()),
            {
                "items": [],
                "counts": {"total": 0, "matched": 0, "not_matched": 0, "insufficient": 0},
            },
        )
        self.assertEqual(evaluate([row()], definition())["items"][0]["state"], "matched")


class ScreeningTransitionTests(unittest.TestCase):
    def step(self, previous, states, second=0, cooldown=60):
        return transitions(previous, states, AT + timedelta(seconds=second), cooldown)

    def test_first_round_records_baselines_without_alerts(self):
        state, events = self.step(
            {},
            [
                observation("matched"),
                observation("not_matched", "000001.SZ"),
                observation("insufficient", "000002.SZ"),
            ],
        )
        self.assertEqual(events, [])
        self.assertEqual(state["version"], ENGINE_VERSION)
        self.assertTrue(state["initialized"])
        self.assertEqual(state["instruments"]["600001.SH"]["state"], "matched")
        self.assertEqual(state["instruments"]["000001.SZ"]["state"], "not_matched")
        self.assertIsNone(state["instruments"]["000002.SZ"]["state"])

    def test_known_edges_alert_once_and_unchanged_samples_do_not_alert(self):
        state, _ = self.step({}, [observation("not_matched")])
        state, events = self.step(state, [observation("matched")], 1)
        self.assertEqual(
            events,
            [
                {
                    "code": "600001.SH",
                    "direction": "entered",
                    "source_time": AT.isoformat(),
                    "name": "合成标的",
                }
            ],
        )
        state, events = self.step(state, [observation("matched")], 2)
        self.assertEqual(events, [])
        state, events = self.step(state, [observation("not_matched")], 3)
        self.assertEqual([event["direction"] for event in events], ["exited"])
        self.assertEqual(state["instruments"]["600001.SH"]["state"], "not_matched")

    def test_insufficient_and_missing_rows_do_not_fake_exits_or_entries(self):
        state, _ = self.step({}, [observation("matched")])
        state, events = self.step(state, [observation("insufficient")], 1)
        self.assertEqual(events, [])
        self.assertEqual(state["instruments"]["600001.SH"]["state"], "matched")
        absent, events = self.step(state, [], 2)
        self.assertEqual(events, [])
        self.assertEqual(absent, state)
        state, events = self.step(absent, [observation("matched")], 3)
        self.assertEqual(events, [])
        state, events = self.step(state, [observation("not_matched")], 4)
        self.assertEqual(events[0]["direction"], "exited")

    def test_first_definite_result_after_unknown_is_still_a_baseline(self):
        state, _ = self.step({}, [observation("insufficient")])
        state, events = self.step(state, [observation("matched")], 1)
        self.assertEqual(events, [])
        _, events = self.step(state, [observation("not_matched")], 2)
        self.assertEqual(events[0]["direction"], "exited")

    def test_new_instrument_baselines_do_not_suppress_existing_edges(self):
        state, _ = self.step({}, [observation("not_matched")])
        state, events = self.step(
            state, [observation("matched"), observation("matched", "000001.SZ")], 1
        )
        self.assertEqual([event["code"] for event in events], ["600001.SH"])
        self.assertEqual(state["instruments"]["000001.SZ"]["state"], "matched")

    def test_cooldown_is_per_code_and_direction_and_only_updates_on_delivery(self):
        state, _ = self.step(
            {}, [observation("not_matched"), observation("not_matched", "000001.SZ")]
        )
        state, _ = self.step(state, [observation("matched")], 1)
        state, events = self.step(state, [observation("not_matched")], 2)
        self.assertEqual(events[0]["direction"], "exited")
        state, events = self.step(
            state, [observation("matched"), observation("matched", "000001.SZ")], 3
        )
        self.assertEqual([event["code"] for event in events], ["000001.SZ"])
        entry = state["instruments"]["600001.SH"]
        self.assertEqual(entry["state"], "matched")
        self.assertEqual(entry["last_event_at"]["entered"], (AT + timedelta(seconds=1)).isoformat())
        state, events = self.step(state, [observation("not_matched")], 4)
        self.assertEqual(events, [])
        state, events = self.step(state, [observation("matched")], 61)
        self.assertEqual([event["direction"] for event in events], ["entered"])

    def test_suppressed_edges_are_not_delayed_notifications(self):
        state, _ = self.step({}, [observation("not_matched")])
        state, _ = self.step(state, [observation("matched")], 1)
        state, _ = self.step(state, [observation("not_matched")], 2)
        state, events = self.step(state, [observation("matched")], 3)
        self.assertEqual(events, [])
        _, events = self.step(state, [observation("matched")], 100)
        self.assertEqual(events, [])

    def test_zero_cooldown_allows_every_real_edge(self):
        state, _ = self.step({}, [observation("not_matched")], cooldown=0)
        for second, value in enumerate(["matched", "not_matched", "matched"], 1):
            state, events = self.step(state, [observation(value)], second, cooldown=0)
            self.assertEqual(len(events), 1)

    def test_json_roundtrip_preserves_restart_deduplication_and_cooldown(self):
        state, _ = self.step({}, [observation("not_matched")])
        state, _ = self.step(state, [observation("matched")], 1)
        restored = json.loads(json.dumps(state))
        state, events = self.step(restored, [observation("matched")], 2)
        self.assertEqual(events, [])
        state, events = self.step(state, [observation("not_matched")], 3)
        self.assertEqual(events[0]["direction"], "exited")
        _, events = self.step(json.loads(json.dumps(state)), [observation("matched")], 4)
        self.assertEqual(events, [])

    def test_state_and_inputs_are_not_mutated_and_times_are_iso_utc(self):
        state, _ = self.step({}, [observation("not_matched")])
        observations = [observation("matched", source_time=AT)]
        before_state, before_rows = copy.deepcopy(state), copy.deepcopy(observations)
        offset_at = (AT + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
        new_state, events = transitions(state, observations, offset_at, 60)
        self.assertEqual(state, before_state)
        self.assertEqual(observations, before_rows)
        self.assertEqual(
            new_state["instruments"]["600001.SH"]["last_seen_at"],
            (AT + timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(events[0]["source_time"], AT.isoformat())
        json.dumps(new_state)

    def test_event_order_is_stable_by_code_and_missing_metadata_stays_missing(self):
        baseline = [observation("not_matched", code) for code in ["600003.SH", "600001.SH"]]
        state, _ = self.step({}, baseline)
        _, events = self.step(
            state, [{"code": code, "state": "matched"} for code in ["600003.SH", "600001.SH"]], 1
        )
        self.assertEqual([event["code"] for event in events], ["600001.SH", "600003.SH"])
        self.assertTrue(
            all(event["source_time"] is None and event["name"] is None for event in events)
        )

    def test_naive_time_invalid_cooldown_and_clock_regression_are_rejected(self):
        with self.assertRaises(ValueError):
            transitions({}, [], AT.replace(tzinfo=None), 60)
        for cooldown in [True, False, -1, 1.5, "60", None]:
            with self.subTest(cooldown=cooldown), self.assertRaises(ValueError):
                transitions({}, [], AT, cooldown)
        state, _ = self.step({}, [observation("matched")], 10)
        with self.assertRaisesRegex(ValueError, "evaluation_time_regression"):
            self.step(state, [observation("not_matched")], 9)

    def test_invalid_or_duplicate_evaluations_are_rejected_without_partial_state(self):
        for values in [
            None,
            [observation("other")],
            [observation([])],
            [{"state": "matched"}],
            [observation("matched"), observation("not_matched")],
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.step({}, values)

    def test_corrupt_persisted_state_is_not_silently_reset(self):
        state, _ = self.step({}, [observation("matched")])
        bad_version = {**state, "version": "other"}
        bad_stamp = copy.deepcopy(state)
        bad_stamp["instruments"]["600001.SH"]["last_seen_at"] = "2026-10-25T09:30:00"
        bad_event = copy.deepcopy(state)
        bad_event["instruments"]["600001.SH"]["last_event_at"] = {"entered": "invalid"}
        bad_state = copy.deepcopy(state)
        bad_state["instruments"]["600001.SH"]["state"] = []
        for previous in [[], {"initialized": False}, bad_version, bad_stamp, bad_event, bad_state]:
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                self.step(previous, [observation("not_matched")], 1)


if __name__ == "__main__":
    unittest.main()
