"""Synthetic, deterministic boundary cases. No suppliers or databases are used."""
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.market import review_analysis as analysis, direction_read
from app.market.review_metrics import build_daily_metrics
from app.market.review_series import _compact_metrics
from app.screening import schemas, service
from app.screening.engine import AUCTION_METRICS, evaluate
from test_review_metrics import quote, pool, cap, TODAY, PREVIOUS

DAY = date(2026, 10, 23)
CLOCK = datetime.fromisoformat('2026-10-24T18:00:00+08:00')


def table(dataset, rows=None, state='complete'):
    rows = rows or []
    return dict(dataset=dataset, trade_day=str(DAY), scope='all', state=state, rows=rows,
                row_count=len(rows), source_id=None, evidence_refs=[])


class Reader:
    def __init__(self, values):
        self.values, self.cache = values, {}

    def get(self, key, day):
        self.cache[(key, day)] = self.values.get(key, table(key, state='missing'))
        return self.cache[(key, day)]

    def stock(self, key, day, code):
        return self.get(key, day)


class ReviewExpansionTests(unittest.TestCase):
    def metrics(self, missing=False, unknown_height=False):
        rows = [quote(i, '20' if i in (1,3,4,5) else '0') for i in range(1,7) if not (missing and i==5)]
        previous = [pool(i, day=PREVIOUS, height=h) for i,h in enumerate(['1','1','2','3','4',None if unknown_height else '5'],1)]
        return build_daily_metrics(trade_day=TODAY, daily=rows, price_limits=[cap(r) for r in rows],
            pools=[pool(1,height='5'),pool(3,height='5'),pool(4,height='3')], previous_pools=previous)

    def test_fixed_previous_height_groups_have_distinct_denominators(self):
        groups = self.metrics()['relay']['by_height']
        self.assertEqual([groups[k]['N'] for k in groups], [2,1,1,2])
        self.assertEqual(groups['one_to_two']['promotion_pct'], '50')
        self.assertEqual(groups['four_plus']['promotion_pct'], '50')
        self.assertEqual(groups['two_to_three']['samples'][0]['ts_code'], '000003.SZ')

    def test_missing_quote_keeps_denominator_and_withholds_rate(self):
        group = self.metrics(missing=True)['relay']['by_height']['four_plus']
        self.assertEqual((group['N'],group['promotion_valid_n']), (2,1))
        self.assertIsNone(group['promotion_pct'])

    def test_unknown_previous_height_cannot_establish_group_membership(self):
        for group in self.metrics(unknown_height=True)['relay']['by_height'].values():
            self.assertFalse(group['membership_complete'])
            self.assertIsNone(group['promotion_pct'])

    def test_second_height_is_next_distinct_height(self):
        self.assertEqual(self.metrics()['limits']['supplier_reported']['second_height'], 3)

    def test_compact_series_removes_nested_samples_without_mutating_day(self):
        full = self.metrics()
        compact = _compact_metrics(full)
        self.assertNotIn('samples',compact['relay']['by_height']['one_to_two'])
        self.assertTrue(full['relay']['by_height']['one_to_two']['samples'])

    def test_missing_historical_members_does_not_use_current_membership(self):
        reader = Reader({'pools': table('pools',[pool(1)]), 'daily':table('daily',[quote(1,amount='3')])})
        result = analysis.pool_groups(reader,DAY)
        self.assertEqual(result['themes'],[])
        self.assertEqual(result['membership_state'],'missing')
        self.assertEqual(result['unassigned_codes'],['000001.SZ'])
        self.assertEqual(result['records'][0]['turnover_yuan'],'3000')
        self.assertTrue(all(d==DAY for _,d in reader.cache))

    def test_one_stock_can_have_multiple_original_day_themes(self):
        members = [dict(trade_date=TODAY,con_code='000001.SZ',ts_code=k) for k in ['BK1001.DC','BK1002.DC']]
        r=analysis.pool_groups(Reader({'pools':table('pools',[pool(1)]),'members':table('members',members)}),DAY)
        self.assertEqual(len(r['records']),1)
        self.assertEqual(len(r['themes']),2)

    def test_duplicate_pool_identity_is_not_counted_twice(self):
        r=analysis.pool_groups(Reader({'pools':table('pools',[pool(1),pool(1)])}),DAY)
        self.assertEqual(r['ambiguous_n'],2)
        self.assertEqual(r['records'],[])

    def test_archive_reader_has_no_fallback_and_forwards_cutoff(self):
        with patch.object(analysis,'load_dataset',return_value=table('members',state='missing')) as load:
            r=analysis.ArchiveReader(object(),CLOCK)
            self.assertEqual(r.get('members',DAY)['state'],'missing')
            r.get('members',DAY)
            self.assertEqual(load.call_count,1)
            self.assertEqual(load.call_args.kwargs['as_of_time'],CLOCK)

    def test_stock_marker_keeps_multiple_reasons_and_missing_day(self):
        fake = Reader({'daily':table('daily',[quote(1)]),'lhb':table('lhb',[
            dict(ts_code='000001.SZ',trade_date=TODAY,reason='原因一'),
            dict(ts_code='000001.SZ',trade_date=TODAY,reason='原因二')])})
        with patch.object(analysis,'_context',return_value=({'shared_date':str(DAY)},[DAY],DAY)),patch.object(analysis,'ArchiveReader',return_value=fake):
            value=analysis.analysis_view(object(),DAY,kind='stock',stock='000001.SZ',as_of_time=CLOCK)
        self.assertEqual(len(value['records'][0]['listings']),2)
        self.assertNotEqual(*[r['record_key'] for r in value['records'][0]['listings']])
        self.assertEqual(value['records'][0]['adjustment'],'unadjusted')

    def test_rotation_finds_rank_beyond_displayed_top_ten(self):
        rows=[dict(ts_code=f'BK{1000+i}.DC',trade_date=TODAY,name=f'合成{i}',pct_change=str(20-i)) for i in range(12)]
        with patch('app.market.review_read._context',return_value=({},[DAY],DAY)),patch.object(direction_read,'_table',side_effect=lambda db,ds,day,cutoff:table(ds,rows if ds=='themes' else [])):
            result=direction_read.rotation_view(object(),DAY,sector_code='BK1011.DC')
        row=result['records'][0]
        self.assertEqual(row['rank'],12)
        self.assertFalse(row['in_top_ten'])
        self.assertEqual(len(row['top_ten']),10)

    def test_rotation_missing_day_keeps_unknown_rank(self):
        with patch('app.market.review_read._context',return_value=({},[DAY],DAY)),patch.object(direction_read,'_table',return_value=table('themes',state='permission_denied')):
            result=direction_read.rotation_view(object(),DAY,sector_code='BK1001.DC')
        self.assertIsNone(result['records'][0]['rank'])
        self.assertEqual(result['records'][0]['selection_state'],'unavailable')

    def test_rotation_empty_archive_is_not_a_missing_day(self):
        with patch('app.market.review_read._context',return_value=({},[DAY],DAY)),patch.object(direction_read,'_table',return_value=table('themes')):
            result=direction_read.rotation_view(object(),DAY,sector_code='BK1001.DC')
        self.assertIsNone(result['records'][0]['rank'])
        self.assertEqual(result['records'][0]['selection_state'],'not_disclosed')
        self.assertEqual(result['coverage']['ranking_days'],1)

    def test_partial_daily_ranking_cannot_claim_final_rank(self):
        rows=[dict(ts_code='BK1001.DC',trade_date=TODAY,name='合成甲',pct_change='1'),
              dict(ts_code='BK1002.DC',trade_date=TODAY,name='合成乙',pct_change=None)]
        with patch('app.market.review_read._context',return_value=({},[DAY],DAY)),patch.object(direction_read,'_table',return_value=table('themes',rows)):
            result=direction_read.rotation_view(object(),DAY,sector_code='BK1001.DC')
        self.assertIsNone(result['records'][0]['rank'])
        self.assertEqual(result['records'][0]['selection_state'],'unavailable')

    def test_auction_metrics_do_not_accept_market_turnover_or_time_rules(self):
        result=evaluate([dict(code='000001.SZ',turnover_rate='50',source_time=None)],
            dict(conditions=[dict(field='auction_turnover_rate',operator='gte',value='1')],sort_field='auction_amount_cny',sort_direction='desc'),metrics=AUCTION_METRICS)
        self.assertEqual(result['counts']['insufficient'],1)

    def test_auction_preview_filters_before_paginating_and_includes_201st(self):
        rows=[dict(code=f'{i:06d}.SZ',name='合成',auction_amount_cny=str(i),source_time=None) for i in range(1,252)]
        ctx=dict(rows=rows,source_refs=[],trade_day=DAY,as_of_time=CLOCK,data_revision='fixture',
            mode='review',created_at=CLOCK,reasons=[],screen_context='auction',coverage={'source_rows':251})
        body=schemas.PreviewInput(day=DAY,screen_context='auction',definition=dict(
            conditions=[dict(field='auction_amount_cny',operator='gte',value='201')],sort_field='auction_amount_cny',sort_direction='desc'),limit=10)
        with patch('app.screening.auction.auction_context',return_value=ctx),patch.object(service,'context',side_effect=AssertionError('continuous market reader used')):
            value=service.preview(object(),None,body)
        self.assertEqual((value.counts.total,value.counts.matched),(251,51))
        self.assertEqual(value.items[0].code,'000251.SZ')
        self.assertTrue(value.has_more)

    def test_missing_yesterday_group_is_insufficient_not_no_match(self):
        value=evaluate([dict(code='000001.SZ',yesterday_up=None)],dict(
            conditions=[dict(field='yesterday_up',operator='eq',value='1')],sort_field='auction_amount_cny',sort_direction='desc'),metrics=AUCTION_METRICS)
        self.assertEqual(value['counts']['insufficient'],1)

    def test_auction_conditions_cannot_enable_continuous_monitor(self):
        with self.assertRaises(ValueError):
            schemas.RuleSave(name='合成规则',enabled=True,definition=dict(conditions=[dict(field='auction_pct',operator='gte',value='2')]))

    def test_empty_preview_fingerprint_includes_auction_context(self):
        ctx=dict(rows=[],source_refs=[],trade_day=DAY,as_of_time=CLOCK,mode='review')
        content=dict(scope='market',definition=dict(conditions=[dict(field='previous_height',operator='gte',value='1')],sort_field='previous_height',sort_direction='desc'))
        _,market=service.evaluate_context(ctx,content)
        _,auction=service.evaluate_context({**ctx,'screen_context':'auction'},content)
        self.assertNotEqual(market,auction)


if __name__ == '__main__':
    unittest.main()
