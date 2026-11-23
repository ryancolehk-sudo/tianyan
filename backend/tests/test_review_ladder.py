"""Saved close-ladder boundaries with synthetic inputs, no supplier access."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import uuid

from app.market import review_analysis as analysis, review_ladder as ladder
from app.market.review_read import _referenced

DAY = date(2026, 10, 30)
CUTOFF = datetime.fromisoformat('2026-10-30T17:00:00+08:00')


def raw(code='001219.SZ', name='合成甲', height='5'):
    return dict(ts_code=code, name=name, nums=height, trade_date='20261030')


class SavedLadderTests(TestCase):
    def read(self, rows, cutoff=CUTOFF, permitted=True):
        provider = SimpleNamespace(id=uuid.uuid4())
        evidence = SimpleNamespace(id=uuid.uuid4(), source_id=provider.id, content_hash='a'*64,
            received_at=datetime.fromisoformat('2026-10-30T16:30:00+08:00'), payload={'rows': rows})
        db = Mock()
        db.scalar.return_value = evidence
        with patch.object(ladder, 'permitted_provider', return_value=provider if permitted else None) as permission:
            value = ladder.saved_ladder_table(db, DAY, cutoff)
        return value, db, permission

    def test_original_heights_and_offsets_survive_st_and_exchange_filtering(self):
        value, _, _ = self.read([raw(name='ST合成'), raw(name='合成乙', code='603131.SH', height='3'),
                               raw(code='920001.BJ')])
        self.assertEqual(value['row_count'], 1)
        row = _referenced(value['rows'][0], value, 0)
        self.assertEqual((row['ts_code'], row['board_height']), ('603131.SH', 3))
        self.assertEqual(row['row_reference']['source_row'], 1)
        self.assertEqual(value['basis'], 'supplier_ladder')

    def test_current_and_observed_permissions_are_forwarded(self):
        value, _, permission = self.read([raw()])
        self.assertIsNotNone(value)
        self.assertEqual(permission.call_args.args[1:], (ladder.FAMILY, CUTOFF))
        value, db, _ = self.read([raw()], permitted=False)
        self.assertIsNone(value)
        db.scalar.assert_not_called()

    def test_preclose_cutoff_cannot_show_final_ladder(self):
        value, db, permission = self.read([raw()], datetime.fromisoformat('2026-10-30T14:59:59+08:00'))
        self.assertIsNone(value)
        permission.assert_not_called()
        db.scalar.assert_not_called()

    def test_query_checks_date_receipt_visibility_and_provenance(self):
        _, db, _ = self.read([raw()])
        statement = db.scalar.call_args.args[0]
        sql = str(statement)
        self.assertIn('received_at >=', sql)
        self.assertIn('received_at <=', sql)
        self.assertIn('provenance', sql)
        self.assertIn('owner_id IS NULL', sql)
        values = list(statement.compile().params.values())
        self.assertIn([{'trade_date': '20261030'}], values)
        self.assertIn('tushare.limit_step', values)

    def test_wrong_day_duplicates_bad_height_and_full_cap_do_not_become_a_ladder(self):
        for rows in ([{**raw(), 'trade_date': '20261029'}], [raw(), raw()],
                     [raw(height='2.5')], [raw(height='NaN')], [raw(height=None)],
                     [raw(height='1')], [raw(code='bad')], [], [raw()]*2000):
            with self.subTest(rows=str(rows)[:70]):
                self.assertIsNone(self.read(rows)[0])

    def test_missing_or_empty_pool_uses_original_saved_ladder_without_changing_pool_coverage(self):
        saved = self.read([raw()])[0]
        for state in ('missing', 'complete'):
            pool = dict(state=state, dataset='pools', trade_day=str(DAY), scope='all',
                        rows=[], row_count=0, evidence_refs=[])
            reader = SimpleNamespace(cache={}, get=lambda *args: pool)
            with patch.object(analysis, '_context', return_value=({}, [DAY], DAY)), \
                    patch.object(analysis, 'ArchiveReader', return_value=reader), \
                    patch.object(analysis, 'saved_ladder_table', return_value=saved):
                result = analysis.analysis_view(object(), DAY, kind='ladder', as_of_time=CUTOFF)
            row = result['records'][0]
            self.assertEqual((row['state'], row['basis']), ('partial', 'supplier_ladder'))
            self.assertEqual(row['stocks'][0]['board_height'], 5)
            self.assertEqual(row['stocks'][0]['row_reference']['content_hash'], 'a'*64)
            self.assertEqual(next(c for c in result['coverage'] if c['dataset'] == 'pools')['row_count'],
                             0 if state == 'complete' else None)
            self.assertEqual(len(result['evidence_refs']), 1)

    def test_nonempty_or_broken_or_denied_pool_does_not_silently_change_universe(self):
        for state, rows in [('complete', [dict(ts_code='001219.SZ', trade_date='20261030', limit='D')]),
                            ('permission_denied', []), ('broken_lineage', [])]:
            pool = dict(state=state, dataset='pools', trade_day=str(DAY), scope='all',
                        rows=rows, row_count=len(rows), evidence_refs=[])
            reader = SimpleNamespace(cache={}, get=lambda *args: pool)
            with patch.object(analysis, '_context', return_value=({}, [DAY], DAY)), \
                    patch.object(analysis, 'ArchiveReader', return_value=reader), \
                    patch.object(analysis, 'saved_ladder_table') as fallback:
                result = analysis.analysis_view(object(), DAY, kind='ladder', as_of_time=CUTOFF)
            fallback.assert_not_called()
            self.assertEqual(result['records'][0]['stocks'], [])
