"""Synthetic public-book wire records only; no market requests or database."""
from datetime import date, datetime, timedelta
import unittest

from app.market.auction_book import fetch_books, parse_page, seal
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure

DAY = date(2026, 10, 26)
AT = datetime(2026, 10, 26, 9, 20, 6, tzinfo=SHANGHAI)
KEY = InstrumentKey('SSE','600001','stock')


def wire(key=KEY, *, side='down', stamp='20261026092000', volume='20', changes=None):
    f=['']*88
    values={1:'合成竞价盘口',2:key.symbol,3:'10',4:'10',9:'0',10:'0',19:'9',20:volume,30:stamp,47:'11',48:'9'}
    if side=='up': values.update({9:'11',10:volume,19:'0',20:'0'})
    values.update(changes or {})
    for index,value in values.items(): f[index]=value
    return ('v_'+key.vendor_key+'="'+'~'.join(f)+'";').encode('gb18030')


class AuctionBookTests(unittest.TestCase):
    def test_native_book_works_without_treating_last_trade_as_auction_price(self):
        rows,returned,valid,stamp=parse_page(wire(),[KEY],AT,DAY)
        self.assertEqual((returned,valid),(1,1))
        self.assertEqual(rows[0]['close'],'10')
        self.assertEqual(rows[0]['ask_volume1'],'2000')
        self.assertEqual(seal(rows[0],'down'),18000)
        self.assertIsNone(seal(rows[0],'up'))
        self.assertEqual(stamp,AT-timedelta(seconds=6))

    def test_same_response_captures_both_sides_in_shares(self):
        other=InstrumentKey('SZSE','000002','stock')
        rows,*_=parse_page(wire()+b'\n'+wire(other,side='up',volume='0.5'),[KEY,other],AT,DAY)
        self.assertEqual(len(rows),2)
        self.assertEqual(seal(rows[1],'up'),550)

    def test_two_sided_zero_and_missing_quantities_do_not_invent_seals(self):
        for changes in ({10:'1'},{20:'0'},{20:''},{19:'9.01'}):
            self.assertEqual(parse_page(wire(changes=changes),[KEY],AT,DAY)[0],[])

    def test_exact_native_limits_include_st_and_ignore_unrestricted_quotes(self):
        rows,*_=parse_page(wire(changes={19:'9.5',48:'9.5'}),[KEY],AT,DAY)
        self.assertEqual(seal(rows[0],'down'),19000)
        self.assertEqual(parse_page(wire(changes={47:'-1',48:'-1'}),[KEY],AT,DAY)[0],[])

    def test_weekend_old_clock_expired_and_post_auction_are_not_current(self):
        for stamp in ('20261023092000','20261026091800'):
            self.assertEqual(parse_page(wire(stamp=stamp),[KEY],AT,DAY)[2],0)
        self.assertEqual(parse_page(wire(stamp='20261026093000'),[KEY],AT.replace(minute=30),DAY)[2],0)
        self.assertEqual(parse_page(wire(stamp='20261026092501'),[KEY],AT.replace(minute=25,second=6),DAY)[2],0)

    def test_future_duplicate_and_unrequested_records_are_rejected(self):
        for body in (wire(stamp='20261026092200'),wire()+b'\n'+wire(),wire(InstrumentKey('SZSE','000002','stock'))):
            with self.assertRaises(SourceFailure): parse_page(body,[KEY],AT,DAY)

    def test_malformed_or_nonintegral_book_never_coerces_to_zero(self):
        for value in ('NaN','-1','0.001'):
            with self.assertRaises(SourceFailure): parse_page(wire(volume=value),[KEY],AT,DAY)

    def test_no_permission_means_no_physical_request(self):
        calls=[]
        with self.assertRaises(SourceFailure): fetch_books([KEY],AT,lambda:False,read=lambda url:calls.append(url))
        self.assertEqual(calls,[])

    def test_immutable_batch_records_real_coverage_and_both_counts(self):
        other=InstrumentKey('SZSE','000002','stock')
        response,batch=fetch_books([KEY,other],AT,lambda:True,
            read=lambda url:Response(wire()+b'\n'+wire(other,side='up'),AT,1,200))
        self.assertEqual(response.request_count,1)
        self.assertEqual(batch.book_progress['down_count'],1)
        self.assertEqual(batch.book_progress['up_count'],1)
        payload=batch.records[0][1]
        self.assertEqual(payload.rows[0]['usable_count'],'2')
        self.assertEqual(payload.observed_day,DAY)

    def test_bounded_partial_sweep_resumes_at_first_unsampled_page(self):
        keys=[InstrumentKey('SSE',str(600000+i),'stock') for i in range(501)]
        def read(url):
            selected=[key for key in keys if key.vendor_key in url.split('q=')[1].split(',')]
            return Response(b'\n'.join(wire(key,changes={9:'9',10:'1',19:'9.01'}) for key in selected),AT,1,200)
        ticks=iter([0,51])
        _,batch=fetch_books(keys,AT,lambda:True,read=read,monotonic=lambda:next(ticks))
        self.assertEqual(batch.book_progress['requested'],500)
        self.assertEqual(batch.book_progress['next_offset'],500)
        self.assertEqual(batch.rejected,1)
        self.assertEqual(len(batch.records[0][1].rows),1) # Valid zero-candidate batch.

    def test_failed_page_is_counted_without_a_partial_success(self):
        def fail(url): raise SourceFailure('rate_limited')
        with self.assertRaises(SourceFailure) as caught: fetch_books([KEY],AT,lambda:True,read=fail)
        self.assertEqual(caught.exception.request_count,1)


if __name__=='__main__': unittest.main()
