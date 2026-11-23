"""Synthetic downside cohorts; no network, database or real-market fixtures."""
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch

from app.market.auction_down import at_lower, code_map, project_down, sell_seal, stage_rows


class AuctionDownTests(unittest.TestCase):
    def project(self, **changes):
        code = '600001.SH'
        args = dict(day=date(2026, 11, 6), stages={}, auctions={code: {'price': 9, 'pre_close': 10, 'amount': 12345}},
            auction_units={code: {'amount': '元'}}, limits={code: {'down_limit': 9}},
            prices={code: {'close': 9.5, 'pre_close': 10, 'name': '合成跌停观察', '_basis': 'daily_close'}}, prior={})
        args.update(changes)
        return project_down(**args)

    def test_exact_native_limit_handles_st_and_twenty_percent(self):
        self.assertTrue(at_lower('9.50', 9.5))
        self.assertTrue(at_lower(8, 8))
        for value in (None, True, 'NaN', float('inf'), 9.499):
            self.assertFalse(at_lower(value, 9.5))

    def test_reopened_stock_remains_in_auction_observation(self):
        row = self.project().rows[0]
        self.assertAlmostEqual(row.auction_pct, -10)
        self.assertAlmostEqual(row.pct_change, -5)
        self.assertEqual(row.auction_amount_cny, 12345)
        self.assertTrue(all(v is None for v in row.node_amounts.values()))

    def test_matched_money_does_not_fill_nodes_or_enable_percentage_fallback(self):
        view = self.project()
        self.assertIsNone(view.rows[0].pool_seal_cny)
        self.assertEqual(view.rows[0].node_pct, {})
        self.assertEqual(view.seal_coverage, 0)
        self.assertEqual(view.stage_coverage, {'09:15': 0, '09:20': 0, '09:25': 0})
        self.assertTrue(all(v is None for v in view.rows[0].node_amounts.values()))

    def test_native_units_and_both_sides_required_for_sell_seal(self):
        raw = {'close': 9, 'ask_price1': 9, 'ask_volume1': 20, 'bid_volume1': 0, 'amount': 123456}
        self.assertIsNone(sell_seal(raw, {}, 9))
        self.assertIsNone(sell_seal({**raw, 'bid_volume1': 1}, {'ask_volume1': '股', 'bid_volume1': '股'}, 9))
        self.assertIsNone(sell_seal({**raw, 'ask_price1': 9.01}, {'ask_volume1': '股', 'bid_volume1': '股'}, 9))
        self.assertEqual(sell_seal(raw, {'ask_volume1': '股', 'bid_volume1': '股'}, 9), 180)
        self.assertEqual(sell_seal(raw, {'ask_volume1': '手', 'bid_volume1': '手'}, 9), 18000)
        self.assertEqual(sell_seal({**raw, 'ask_volume1': 0}, {'ask_volume1': '股', 'bid_volume1': '股'}, 9), 0)

    def test_price_only_stage_or_closing_downside_does_not_widen_auction_cohort(self):
        row = {'ts_code': '600002.SH', 'name': '合成二', 'close': 8, 'pre_close': 10}
        view = self.project(stages={'09:20': ({'600002.SH': row}, {}, {'600002.SH': '2026-11-06T09:20:05+08:00'})},
                            prices={'600003.SH': {'close': 9, 'pre_close': 10, '_basis': 'daily_close'}},
                            limits={'600001.SH': {'down_limit': 9}, '600002.SH': {'down_limit': 8},
                                    '600003.SH': {'down_limit': 9}})
        self.assertEqual([row.code for row in view.rows], ['600001.SH'])

    def test_verified_auction_sell_seal_is_included_even_if_later_reopened(self):
        row = {'ts_code': '600002.SH', 'name': '合成二', 'close': 8, 'pre_close': 10,
               'ask_price1': 8, 'ask_volume1': 20, 'bid_volume1': 0}
        columns = {'ask_volume1': '手', 'bid_volume1': '手'}
        view = self.project(stages={'09:20': ({'600002.SH': row}, columns, {'600002.SH': '2026-11-06T09:20:05+08:00'})},
                            prices={'600002.SH': {'close': 9, 'pre_close': 10}},
                            limits={'600001.SH': {'down_limit': 9}, '600002.SH': {'down_limit': 8}})
        self.assertEqual(len(view.rows), 2)
        self.assertEqual(view.rows[1].node_amounts['09:20'], 16000)
        self.assertEqual(view.rows[1].node_pct, {})
        self.assertAlmostEqual(view.rows[1].pct_change, -10)
        self.assertEqual(view.stage_coverage['09:20'], 1)

    def test_zero_sell_queue_does_not_establish_a_downside_cohort(self):
        raw = {'close': 9, 'ask_price1': 9, 'ask_volume1': 0, 'bid_volume1': 0}
        self.assertEqual(self.project(auctions={}, stages={'09:20': ({'600001.SH': raw},
                         {'ask_volume1': '股', 'bid_volume1': '股'}, {})}).rows, [])

    def test_native_auction_uses_exact_lower_limit_including_st(self):
        view = self.project(auctions={'600001.SH': {'price': 9.5, 'pre_close': 10}},
                            limits={'600001.SH': {'down_limit': 9.5}})
        self.assertEqual(len(view.rows), 1)
        self.assertAlmostEqual(view.rows[0].auction_pct, -5)

    def test_late_receipt_cannot_put_post_0925_quote_into_final_node(self):
        from app.market.protocol import SHANGHAI
        from app.market.terminal_read import Meta
        from types import SimpleNamespace
        from uuid import uuid4
        start = datetime(2026, 11, 6, 9, 25, tzinfo=SHANGHAI)
        meta = Meta(uuid4(), uuid4(), 'tushare_market_rt', 'synthetic', start + timedelta(seconds=6), 'synthetic', frozenset())
        class Query:
            def where(self, *args): return self
            def with_only_columns(self, *args): return self
            def order_by(self, *args): return self
            def limit(self, *args): return self
        class Reader:
            cutoff = start + timedelta(minutes=5)
            db = SimpleNamespace(execute=lambda _: SimpleNamespace(first=lambda: (
                meta.id, meta.source_id, meta.family, meta.source_key, meta.received_at, meta.content_hash)))
            def query(self, *args): return Query()
            def at(self, row, fallback): return row['at']
            def evidence(self, meta): return SimpleNamespace(payload={})
        before, on_time = {'at': start - timedelta(seconds=1)}, {'at': start}
        stale, after = {'at': start - timedelta(seconds=91)}, {'at': start + timedelta(seconds=1)}
        with patch('app.market.auction_down.read_facets.with_facets', side_effect=lambda query: query), \
                patch('app.market.auction_down.current_rows', return_value=[before, on_time, stale, after]):
            _meta, rows, _units = stage_rows(Reader(), date(2026, 11, 6), '09:25')
        self.assertEqual(rows, [before, on_time])

    def test_missing_limits_or_ambiguous_codes_do_not_become_downside(self):
        self.assertEqual(self.project(limits={}).rows, [])
        self.assertEqual(code_map([{'ts_code': '600001.SH'}, {'ts_code': '600001.SH'}]), {})

    def test_future_stage_never_queries_the_database(self):
        from datetime import datetime
        from app.market.protocol import SHANGHAI
        class Reader:
            cutoff = datetime(2026, 11, 6, 9, 19, tzinfo=SHANGHAI)
        self.assertEqual(stage_rows(Reader(), date(2026, 11, 6), '09:20'), (None, [], {}))


if __name__ == '__main__':
    unittest.main()
