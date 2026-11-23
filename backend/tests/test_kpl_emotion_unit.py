import json
import unittest
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from app.market.kpl_emotion import parse, project
from app.market.protocol import SourceFailure
from app.market.direction_sources import _request
from app.market.realtime_schedule import align_realtime

DAY=date(2026,11,5)
CLOCK=datetime.fromisoformat('2026-11-05T10:00:00+08:00')

def body(value='45', day=str(DAY)):
    return json.dumps(dict(errcode='0',info=[dict(Day=day,strong=value)])).encode()

def record(clock=CLOCK,value='45',day=DAY):
    return SimpleNamespace(received_at=clock,payload=parse(body(value,str(day)),clock).records[0][1].model_dump(mode='json'))

class EmotionTests(unittest.TestCase):
    def test_raw_scalar_and_unchanged_receipts(self):
        first=record();second=record(CLOCK+timedelta(minutes=1))
        self.assertNotEqual(first.payload,second.payload)
        result=project([second,first],DAY,CLOCK+timedelta(minutes=1))
        self.assertEqual([p['value'] for p in result['points']],['45','45'])
    def test_zero_and_range_validation(self):
        self.assertEqual(record(value='0').payload['rows'][0]['value'],'0')
        for v in ['-1','101','NaN','Infinity',None,True]:
            with self.assertRaises(SourceFailure): parse(body(v),CLOCK)
    def test_no_after_close_lunch_cross_day_or_future_points(self):
        for stamp in ['2026-11-05T12:00:00+08:00','2026-11-05T15:01:00+08:00','2026-11-06T10:00:00+08:00']:
            clock=datetime.fromisoformat(stamp)
            result=project([record(clock)],DAY,clock)
            self.assertFalse(result['points']);self.assertIsNotNone(result['latest'])
        self.assertIsNone(project([record(CLOCK+timedelta(minutes=1))],DAY,CLOCK)['latest'])
    def test_source_day_does_not_become_receipt_day(self):
        older=record(day=DAY-timedelta(days=1))
        self.assertFalse(project([older],DAY,CLOCK)['points'])
        self.assertFalse(project([older],DAY-timedelta(days=1),CLOCK)['points'])
        with self.assertRaises(SourceFailure):parse(body(day='2026-11-06'),CLOCK)
    def test_fixed_request_and_trading_windows(self):
        request=_request('direction_kpl_emotion',None)
        self.assertEqual(request.full_url,'https://apphwhq.longhuvip.com/w1/api/index.php?a=ChangeStatistics&st=10&c=HomeDingPan')
        self.assertEqual(request.get_method(),'GET')
        noon=CLOCK.replace(hour=12)
        self.assertEqual(align_realtime('direction_kpl_emotion',noon).hour,13)
        self.assertEqual(align_realtime('direction_kpl_emotion',CLOCK),CLOCK)

if __name__=='__main__':unittest.main()
