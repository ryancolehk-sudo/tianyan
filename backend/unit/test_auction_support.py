"""Pure projections: no database fixtures, network or collection."""
import unittest

from app.market.auction_support import build_rows, integer, market, native_clock, ratio, unique
from app.market.terminal_schemas import TerminalStock


class AuctionSupportTests(unittest.TestCase):
    def rows(self, **changes):
        code = '600001.SH'
        args = dict(stocks=[TerminalStock(code=code, name='合成股票', price=11, pct_change=10,
                                         price_basis='intraday', amount_cny=2000, seal_amount_cny=500)],
            previous={code: {'limit': 'U', 'limit_times': 2, 'open_times': 0}}, current_daily={},
            previous_daily={code: {'vol': 100, 'amount': 1000, 'close': 10}},
            auctions={code: {'vol': 5000, 'price': 11, 'pre_close': 10, 'amount': 55000, 'turnover_rate': 1.5}},
            auction_units={code: {'vol': '股', 'amount': '元'}}, basics={}, previous_basics={},
            limits={code: {'up_limit': 11, 'down_limit': 9}}, old_limits={}, details={}, node_tags={}, lhb=set())
        args.update(changes)
        return build_rows(**args)[0]

    def test_support_uses_shares_not_monetary_amount_or_lots(self):
        row = self.rows()
        self.assertEqual(row.support_pct, 50)
        self.assertAlmostEqual(row.auction_pct, 10)
        self.assertEqual(row.auction_amount_cny, 55000)
        self.assertEqual(row.previous_amount_cny, 1_000_000)

    def test_missing_wrong_units_and_unmatched_stage_are_not_results(self):
        self.assertIsNone(self.rows(auction_units={}).support_pct)
        self.assertIsNone(self.rows(auction_available=False).auction_pct)
        self.assertIsNone(self.rows(auction_available=False).auction_amount_cny)
        self.assertIsNone(self.rows(previous_daily={}).support_pct)

    def test_zero_is_observed_but_zero_denominator_is_not_a_ratio(self):
        self.assertEqual(ratio(0, 10), 0)
        self.assertIsNone(ratio(10, 0))
        self.assertEqual(self.rows().previous_open_times, 0)
        for invalid in [None, True, 'NaN', float('inf'), -1]:
            self.assertIsNone(ratio(invalid, 10))

    def test_popularity_and_net_flow_are_not_manufactured(self):
        row = self.rows()
        self.assertIsNone(row.popularity_rank)
        self.assertIsNone(row.auction_net_cny)

    def test_auction_limit_flags_use_auction_price_not_latest_quote(self):
        row = self.rows(stocks=[TerminalStock(code='600001.SH', name='合成', price=9)])
        self.assertTrue(row.auction_at_limit)
        self.assertFalse(row.auction_at_down_limit)
        self.assertTrue(row.at_down_limit)
        self.assertIsNone(row.current_bid_cny)

    def test_volume_comparison_requires_a_valid_current_volume(self):
        self.assertIsNone(self.rows().volume_change_pct)
        self.assertEqual(self.rows(live_volumes={'600001.SH': 2500}).volume_change_pct, 25)
        self.assertEqual(self.rows(current_daily={'600001.SH': {'vol': 200}}, current_daily_available=True).volume_change_pct, 200)

    def test_free_float_uses_declared_ten_thousand_shares_and_current_price(self):
        row = self.rows(previous_basics={'600001.SH': {'free_share': 500, 'float_share': 1000}})
        self.assertEqual(row.actual_float_cny, 55_000_000)
        self.assertEqual(row.float_mv_cny, 110_000_000)

    def test_native_theme_and_board_type_are_independent(self):
        row = self.rows(node_tags={'600001.SH': ['芯片', '2板', '', '芯片']},
            previous={'600001.SH': {'limit': 'U', 'limit_type': '一字', 'change_reason': '芯片+算力'}})
        self.assertEqual(row.themes, ['芯片'])
        self.assertEqual(row.previous_type, '一字')
        self.assertEqual(row.change_reason, '芯片+算力')

    def test_absence_from_pool_is_not_proof_of_broken_limit(self):
        self.assertFalse(self.rows(previous={}).previous_broken)
        self.assertFalse(self.rows(previous={'600001.SH': {'_prior_limit': True}}).previous_broken)
        self.assertTrue(self.rows(previous={'600001.SH': {'_prior_limit': True}},
            old_limits={'600001.SH': {'up_limit': 11}}).previous_broken)

    def test_ambiguous_rows_are_excluded_and_count_requires_integral_values(self):
        self.assertEqual(unique([{'ts_code': '600001.SH'}, {'ts_code': '600001.SH'}]), {})
        self.assertEqual(integer('0'), 0)
        self.assertIsNone(integer(1.5))
        self.assertIsNone(integer(True))

    def test_actual_listing_status_is_not_inferred_from_missing_archive(self):
        self.assertFalse(self.rows(lhb={'600001.SH'}).previous_lhb)
        self.assertTrue(self.rows(lhb={'600001.SH'}, previous_lhb_available=True).previous_lhb)
        self.assertEqual(self.rows().lhb_status, 'unavailable')

    def test_markets_follow_code_exchange(self):
        for code, expected in [('688001.SH', '科创板'), ('301001.SZ', '创业板'), ('600001.SH', '沪主板'), ('000001.SZ', '深主板'), ('920001.BJ', '北交所')]:
            self.assertEqual(market(code), expected)

    def test_source_times_without_leading_zero_are_normalized_without_guessing(self):
        self.assertEqual(native_clock('95145'), '09:51:45')
        self.assertEqual(native_clock('130102'), '13:01:02')
        self.assertIsNone(native_clock('256000'))
        self.assertIsNone(native_clock('0'))
        self.assertIsNone(native_clock(True))


if __name__ == '__main__':
    unittest.main()
