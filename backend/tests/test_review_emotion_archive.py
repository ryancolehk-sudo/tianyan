"""New emotion curves use the same archives as ladder history, without I/O."""
import unittest
from datetime import date, datetime
from unittest.mock import patch

from app.market import review_analysis as analysis
from app.market.review_read import _Reader
from test_review_metrics import cap, pool, quote


DAYS = [date(2026, 10, value) for value in (21, 22, 23)]
CUTOFF = datetime.fromisoformat('2026-10-24T18:00:00+08:00')


def saved(dataset, day, rows=None, state='complete'):
    return dict(dataset=dataset, trade_day=day.isoformat(), scope='all', state=state,
                rows=[] if rows is None else rows, row_count=len(rows or []),
                evidence_refs=[], source_id=None)


def fixture():
    values = {}
    for index, day in enumerate(DAYS):
        row = quote(1, '20', day=day.strftime('%Y%m%d'))
        values[('daily', day)] = saved('daily', day, [row])
        values[('limits', day)] = saved('limits', day, [cap(row)])
        values[('pools', day)] = saved('pools', day, [pool(1,
            day=day.strftime('%Y%m%d'), height=str(index+1))])
    return values


class EmotionArchiveTests(unittest.TestCase):
    def read(self, values, *, kind='emotion'):
        calls = []

        def load(db, dataset, day, **kwargs):
            calls.append((dataset, day, kwargs))
            return values.get((dataset, day), saved(dataset, day, state='missing'))

        with patch.object(analysis, '_context', return_value=(
                {'shared_date': DAYS[-1].isoformat()}, DAYS, DAYS[-1])) as context, \
                patch.object(analysis, 'load_dataset', side_effect=load), \
                patch.object(analysis, 'saved_ladder_table', return_value=None), \
                patch.object(_Reader, 'get', side_effect=AssertionError('legacy reader used')), \
                patch('app.market.review_fallback.pool_table', side_effect=AssertionError('fallback used')), \
                patch('app.market.review_read.snapshot_table', side_effect=AssertionError('snapshot used')):
            result = analysis.analysis_view(object(), DAYS[-1], kind=kind, lookback=20, as_of_time=CUTOFF)
        return result, calls, context.call_args

    def test_missing_and_complete_empty_pool_never_use_legacy_supplier(self):
        for state in ('missing', 'complete'):
            with self.subTest(state=state):
                values = fixture()
                values[('pools', DAYS[-1])] = saved('pools', DAYS[-1], state=state)
                emotion, _, _ = self.read(values)
                ladder, _, _ = self.read(values, kind='ladder')
                point = emotion['records'][-1]['metrics']['limits']['supplier_reported']
                self.assertEqual(point['up'], None if state == 'missing' else 0)
                self.assertIsNone(point['max_height'])
                self.assertEqual(ladder['records'][-1]['state'], state)
                self.assertEqual(ladder['records'][-1]['stocks'], [])

    def test_every_date_preserves_only_disjoint_height_samples(self):
        result, _, _ = self.read(fixture())
        earlier, selected = result['records'][-2:]
        for record, key in ((earlier, 'one_to_two'), (selected, 'two_to_three')):
            relay = record['metrics']['relay']
            self.assertTrue(record['by_height_samples_included'])
            self.assertEqual(relay['by_height'][key]['N'], 1)
            self.assertEqual(relay['by_height'][key]['promotion_pct'], '100')
            self.assertEqual(relay['by_height'][key]['samples'][0]['ts_code'], '000001.SZ')
            self.assertNotIn('samples', relay['first_board'])
            self.assertNotIn('samples', relay['consecutive'])
        self.assertEqual(result['source_policy'], 'purchased_archives')

    def test_warmup_and_every_archive_read_retain_receipt_cutoff(self):
        result, calls, context_args = self.read(fixture())
        self.assertEqual(context_args.args[2:], (23, CUTOFF))
        self.assertTrue(calls)
        self.assertTrue(all(kwargs['as_of_time'] == CUTOFF for _, _, kwargs in calls))
        self.assertTrue(all(dataset in {'daily', 'limits', 'pools'} for dataset, _, _ in calls))
        self.assertFalse(result['calendar_window_complete'])
        self.assertEqual(result['records'][-1]['metrics']['calendar']['previous_trade_day'], '2026-10-22')

    def test_missing_current_quote_keeps_previous_height_denominator(self):
        values = fixture()
        values[('daily', DAYS[-1])] = saved('daily', DAYS[-1], state='missing')
        result, _, _ = self.read(values)
        group = result['records'][-1]['metrics']['relay']['by_height']['two_to_three']
        self.assertEqual(group['N'], 1)
        self.assertEqual(group['promotion_valid_n'], 0)
        self.assertIsNone(group['promotion_pct'])

    def test_invalid_metric_input_is_unavailable_without_aborting_history(self):
        with patch.object(analysis, 'build_daily_metrics', side_effect=ValueError('invalid dated table')):
            result, _, _ = self.read(fixture())
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(len(result['records']), len(DAYS))
        for record in result['records']:
            self.assertEqual(record['state'], 'unavailable')
            self.assertEqual(record['metrics']['state'], 'unavailable')
            self.assertEqual(record['metrics']['relay']['by_height'], {})
            self.assertFalse(record['by_height_samples_included'])
            self.assertEqual(record['metrics']['quality_flags'], [{'code': 'invalid_metric_input_table'}])
            self.assertTrue(record['coverage'])


if __name__ == '__main__':
    unittest.main()
