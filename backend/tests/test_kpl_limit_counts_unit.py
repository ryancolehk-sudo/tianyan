import json
import unittest
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from app.market.direction_sources import _request
from app.market.kpl_limit_counts import parse
from app.market.kpl_strength_api import limit_count
from app.market.protocol import Response, SourceFailure

DAY = date(2026, 11, 4)
CLOCK = datetime(2026, 11, 4, 6, tzinfo=UTC)


def response(count=21, **extra):
    data = dict(errcode='0', Date=str(DAY), List=[1, 59000, 700000, 2000, 5.2, count, 100, 50])
    data.update(extra)
    return Response(json.dumps(data).encode(), CLOCK, 1, 200)


class CountTests(unittest.TestCase):
    def test_same_theme_endpoint_and_original_count(self):
        query = parse_qs(urlsplit(_request('direction_kpl_strength', None,
            plate_code='801001', plate_summary=True).full_url).query)
        self.assertEqual(query['a'], ['GetPlate_Info_QJ'])
        self.assertEqual(query['PlateID'], ['801001'])
        self.assertEqual(parse(response(), DAY), '21')
        self.assertEqual(parse(response(0), DAY), '0')

    def test_unknown_invalid_and_other_index_layout_not_zero(self):
        for n in (None, '--', -1, 1.5, True, 10001):
            with self.subTest(n=n), self.assertRaises(SourceFailure):
                parse(response(n), DAY)
        for row in ([0, 1, 2, 3, 4, 0, 6, 7], ['--', 1, 2, 3, 4, 0, 6, 7], [1]*7):
            with self.assertRaises(SourceFailure):
                parse(response(List=row), DAY)
        with self.assertRaises(SourceFailure): parse(response(Date='2026-11-03'), DAY)

    def test_count_receipt_cannot_cross_cutoff_or_day(self):
        row = dict(limit_up_count='0', limit_up_received_at=CLOCK.isoformat())
        self.assertEqual(limit_count(row, DAY, CLOCK), (0, CLOCK.isoformat()))
        self.assertEqual(limit_count(row, DAY, CLOCK-timedelta(seconds=1)), (None, None))
        self.assertEqual(limit_count(row, date(2026,11,5), CLOCK), (None, None))
        self.assertEqual(limit_count(dict(limit_up_count='0'), DAY, CLOCK), (None, None))

    def test_optional_failure_preserves_ranking_and_limits_count_requests(self):
        from app.market.kpl_atlas_source import fetch
        calls = []
        def upstream(family, **kwargs):
            calls.append(kwargs)
            if kwargs.get('plate_summary'):
                if kwargs['plate_code'] == '801001': raise SourceFailure('schema_changed')
                return response(0)
            rows = [[str(801000+i),'合成题材'+str(i),100-i,1,0,100,*([0]*13)] for i in range(12)]
            return Response(json.dumps(dict(errcode='0',Day=[str(DAY)],Count=12,list=rows)).encode(), CLOCK,1,200)
        with patch('app.market.kpl_atlas_source.source.fetch',side_effect=upstream), patch('app.market.kpl_atlas_source.time.sleep'):
            _, batch = fetch(before_request=lambda:True, include_counts=True)
        rows = batch.records[0][1].rows
        self.assertEqual(len(calls),11)
        self.assertEqual(rows[0]['limit_up_count'],'0')
        self.assertIsNone(rows[1]['limit_up_count'])
        self.assertIsNone(rows[10]['limit_up_count'])
        self.assertEqual(rows[0]['page_count'],'1')


if __name__ == '__main__': unittest.main()
