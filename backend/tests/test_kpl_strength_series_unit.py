import unittest
from datetime import date, datetime, timedelta, UTC
from types import SimpleNamespace
from app.market.kpl_strength_api import project

DAY = date(2026,11,4)
CLOCK = datetime(2026,11,4,5,10,tzinfo=UTC)

def batch(stamp, values=('12','0',None), **override):
    rows=[dict(code=str(800000+i),name='合成'+str(i),strength=value,source_day=str(DAY),
               archive_kind='intraday',coverage='complete_pages',total_count=str(len(values)),**override)
          for i,value in enumerate(values)]
    return SimpleNamespace(received_at=stamp,payload=dict(observed_day=str(DAY),rows=rows))

class KplSeriesTests(unittest.TestCase):
    def test_original_values_order_and_missing(self):
        value=project([batch(CLOCK,('-4','0','12',None))],DAY,CLOCK)
        self.assertEqual([r['strength'] for r in value['rows']],['12','0','-4',None])
        self.assertIsNone(value['rows'][0]['limit_up_count'])
        self.assertIsNone(value['source_time'])
        self.assertEqual(value['history'][0]['values']['800001'],'0')
    def test_history_preserves_null_and_chronology(self):
        value=project([batch(CLOCK,('7',None)),batch(CLOCK-timedelta(minutes=5),('2','3'))],DAY,CLOCK)
        self.assertEqual(len(value['history']),2)
        self.assertEqual(value['history'][0]['values']['800001'],'3')
        self.assertIsNone(value['history'][1]['values']['800001'])
    def test_future_and_late_receipts_are_not_historical_points(self):
        self.assertFalse(project([batch(CLOCK+timedelta(minutes=1))],DAY,CLOCK)['rows'])
        later=CLOCK+timedelta(days=1)
        self.assertFalse(project([batch(later)],DAY,later)['rows'])
    def test_daily_wrong_day_incomplete_and_duplicate_are_excluded(self):
        for key,value in [('archive_kind','daily'),('source_day','2026-11-03'),('coverage','partial'),('total_count','99')]:
            record=batch(CLOCK)
            record.payload['rows'][0][key]=value
            self.assertFalse(project([record],DAY,CLOCK)['history'])
        record=batch(CLOCK);record.payload['rows'][1]['code']=record.payload['rows'][0]['code']
        self.assertFalse(project([record],DAY,CLOCK)['rows'])
    def test_verified_partial_batches_are_fresh_without_filling_missing_codes(self):
        record=batch(CLOCK,('10','20'))
        for row in record.payload['rows']:
            row.update(coverage='partial_pages',total_count='3',sampled_count='2',rank_basis='sampled_strength')
        result=project([record,batch(CLOCK-timedelta(minutes=5))],DAY,CLOCK)
        self.assertEqual(result['coverage'],'partial_pages')
        self.assertEqual(result['total_count'],3)
        self.assertEqual(len(result['rows']),2)
        self.assertNotIn('800002',result['history'][-1]['values'])
        self.assertEqual(result['state'],'available')
        record.payload['rows'][0]['sampled_count']='3'
        self.assertFalse(project([record],DAY,CLOCK)['history'])

    def test_scope_truncation_and_staleness_are_explicit(self):
        records=[batch(CLOCK-timedelta(seconds=11+i)) for i in range(601)]
        value=project(records,DAY,CLOCK)
        self.assertTrue(value['history_truncated']);self.assertEqual(value['state'],'available')
        self.assertEqual(len(value['history']),600)

    def test_full_350_minute_session_keeps_every_real_point(self):
        end=CLOCK.replace(hour=7,minute=4)
        records=[batch(end-timedelta(minutes=i),values=(str(i),)) for i in range(350)]
        value=project(records,DAY,end)
        self.assertFalse(value['history_truncated'])
        self.assertEqual(len(value['history']),350)
        self.assertEqual(value['history'][0]['received_at'],records[-1].received_at.isoformat())

if __name__ == '__main__': unittest.main()
