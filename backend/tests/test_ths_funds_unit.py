import unittest
from datetime import UTC, datetime, timedelta
import json
from unittest.mock import patch
from app.market.ths_funds import parse_page, number, fetch
from app.market.protocol import SourceFailure

STAMP=datetime(2026,11,4,5,tzinfo=UTC)
def page(identity='881121',pager='1/1',net='-0.50',unit='亿'):
    heads=['序号','行业','行业指数','涨跌幅',f'流入资金({unit})','流出资金(亿)','净额(亿)','公司家数','领涨股','涨跌幅','当前价(元)']
    cells=['1',f'<a href="http://q.10jqka.com.cn/thshy/detail/code/{identity}/">半导体</a>','100','1.23%','1.50','2.00',net,'12','测试','2%','10']
    return ('<table><tr>'+''.join('<th>'+x+'</th>' for x in heads)+'</tr><tr>'+''.join('<td>'+x+'</td>' for x in cells)+'</tr></table><span class="page_info">'+pager+'</span>').encode('gbk')

class ThsFundsTests(unittest.TestCase):
    def test_units_and_receipt(self):
        total,rows,bad=parse_page(page(),'hyzjl',1,STAMP)
        self.assertEqual((total,bad,len(rows)),(1,0,1));self.assertEqual(rows[0]['net_cny'],'-50000000.00')
        self.assertEqual(rows[0]['received_at'],STAMP.isoformat());self.assertNotIn('source_time',rows[0])
    def test_missing_identity_rejected(self):self.assertEqual(parse_page(page(identity=' '),'hyzjl',1,STAMP)[2],1)
    def test_category_cannot_mix(self):self.assertEqual(parse_page(page(),'gnzjl',1,STAMP)[2],1)
    def test_inconsistent_page_rejected(self):
        for p in ['7/2','1/100','2/2','']:
            with self.subTest(p=p),self.assertRaises(SourceFailure):parse_page(page(pager=p),'hyzjl',1,STAMP)
    def test_wrong_unit_rejected(self):
        with self.assertRaises(SourceFailure):parse_page(page(unit='万'),'hyzjl',1,STAMP)
    def test_nonfinite_rejected(self):
        for value in ['NaN','Infinity','1e100']:
            with self.subTest(value=value),self.assertRaises(SourceFailure):number(value)
    def test_missing_is_not_zero(self):self.assertIsNone(number('--'));self.assertEqual(number('0'),'0')
    def test_partial_page_keeps_valid_rows(self):
        with patch('app.market.ths_funds.request',side_effect=[(page(pager='1/2'),STAMP),SourceFailure('transport_error'),SourceFailure('transport_error')]),patch('app.market.ths_funds.time.sleep'):
            response,batch=fetch('ths_industry_funds',lambda:True)
        self.assertEqual(response.request_count,3);self.assertEqual(batch.rejected,1)
        row=batch.records[0][1].rows[0];self.assertEqual(row['missing_pages'],'2');self.assertEqual(row['complete'],'0')
        self.assertIsNone(batch.records[0][1].observed_day)
    def test_permission_checked_before_every_page(self):
        calls=iter([True,False])
        with patch('app.market.ths_funds.request',return_value=(page(pager='1/2'),STAMP)) as req,patch('app.market.ths_funds.time.sleep'):
            with self.assertRaises(SourceFailure):fetch('ths_industry_funds',lambda:next(calls))
        self.assertEqual(req.call_count,1)
    def test_429_stops_without_retry(self):
        with patch('app.market.ths_funds.request',side_effect=SourceFailure('rate_limited',retry_after=600)) as req:
            with self.assertRaises(SourceFailure) as caught:fetch('ths_industry_funds',lambda:True)
        self.assertEqual(req.call_count,1);self.assertEqual(caught.exception.retry_after,600)
    def test_transient_page_is_retried_once_and_keeps_actual_receipt(self):
        later=STAMP+timedelta(seconds=6)
        with patch('app.market.ths_funds.request',side_effect=[(page(pager='1/2'),STAMP),SourceFailure('transport_error'),(page(identity='881122',pager='2/2'),later)]),patch('app.market.ths_funds.time.sleep'):
            response,batch=fetch('ths_industry_funds',lambda:True)
        rows=batch.records[0][1].rows
        self.assertEqual(response.request_count,3);self.assertEqual(batch.rejected,0)
        self.assertEqual(rows[1]['received_at'],later.isoformat());self.assertEqual(rows[0]['recovered_pages'],'2')
        self.assertEqual(rows[0]['complete'],'1');self.assertEqual(rows[0]['missing_pages'],'')
    def test_retry_budget_is_shared_across_the_whole_page_set(self):
        def failed(category,index,token):
            if index==1:return page(pager='1/4'),STAMP
            raise SourceFailure('transport_error')
        with patch('app.market.ths_funds.request',side_effect=failed) as req,patch('app.market.ths_funds.time.sleep'):
            response,batch=fetch('ths_industry_funds',lambda:True)
        row=batch.records[0][1].rows[0]
        self.assertEqual(req.call_count,6);self.assertEqual(response.request_count,6)
        self.assertEqual(row['missing_pages'],'2,3,4')
        self.assertEqual(json.loads(row['missing_page_reasons']),{str(i):'transport_error' for i in (2,3,4)})
    def test_cross_page_repeat_retains_newer_valid_observation_and_partial_coverage(self):
        later=STAMP+timedelta(seconds=3)
        with patch('app.market.ths_funds.request',side_effect=[(page(pager='1/2'),STAMP),(page(pager='2/2',net='0.75'),later)]),patch('app.market.ths_funds.time.sleep'):
            response,batch=fetch('ths_industry_funds',lambda:True)
        rows=batch.records[0][1].rows
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['net_cny'],'75000000.00')
        self.assertEqual(rows[0]['received_at'],later.isoformat());self.assertEqual(rows[0]['complete'],'0')
        self.assertEqual(rows[0]['cross_page_duplicates'],'1');self.assertEqual(rows[0]['rejected_rows'],'0')
        self.assertEqual(batch.rejected,1)
    def test_permission_lost_before_next_page_counts_only_the_http_already_sent(self):
        calls=iter([True,False])
        with patch('app.market.ths_funds.request',return_value=(page(pager='1/2'),STAMP)) as req,patch('app.market.ths_funds.time.sleep'):
            with self.assertRaises(SourceFailure) as caught:fetch('ths_industry_funds',lambda:next(calls))
        self.assertEqual(req.call_count,1);self.assertEqual(caught.exception.request_count,1)
    def test_invalid_text_is_a_controlled_schema_failure(self):
        with self.assertRaises(SourceFailure):parse_page(b'\xff','hyzjl',1,STAMP)

if __name__=='__main__':unittest.main()
