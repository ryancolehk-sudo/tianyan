import unittest
from app.market.review_group_fields import enrich_groups

class GroupFieldsTests(unittest.TestCase):
    def enrich(self, units=None, listings=None, state='complete', **values):
        row={'ts_code':'000001.SZ','row_reference':{'id':'r'},**values}
        enrich_groups([row], {}, {'r':units or {}}, {'state':state}, listings)
        return row

    def test_declared_units_convert_and_keep_zero(self):
        row=self.enrich({'fd_amount':'万元','float_mv':'亿元','total_mv':'元'}, [], fd_amount='0',float_mv='2',total_mv='300000000')
        self.assertEqual(row['seal_amount_yuan'],'0')
        self.assertEqual(row['float_market_value_yuan'],'200000000')
        self.assertEqual(row['total_market_value_yuan'],'300000000')

    def test_unknown_units_expose_raw_value_without_guessing(self):
        row=self.enrich(fd_amount='11720560')
        self.assertIsNone(row['seal_amount_yuan'])
        self.assertEqual(row['group_field_details']['seal_amount_yuan']['state'],'unit_unverified')
        self.assertEqual(row['group_field_details']['seal_amount_yuan']['source_value'],'11720560')

    def test_invalid_money_is_missing(self):
        for value in ['NaN','Infinity','-1',True,'n/a']:
            row=self.enrich({'fd_amount':'元'},fd_amount=value)
            self.assertIsNone(row['seal_amount_yuan'])
            self.assertEqual(row['group_field_details']['seal_amount_yuan']['state'],'missing')

    def test_listing_links_all_same_stock_reasons(self):
        row=self.enrich(listings=[{'ts_code':'000001.SZ','reason':'合成单日上榜原因'}, {'ts_code':'000001.SZ','reason':'合成多日上榜原因'}])
        self.assertEqual(row['lhb_status'],'listed')
        self.assertEqual(row['lhb_count'],2)
        self.assertIsNone(row['change_reason'])

    def test_complete_empty_is_not_unavailable(self):
        self.assertEqual(self.enrich(listings=[])['lhb_status'],'not_listed')
        for state in ['missing','permission_denied','broken_lineage']:
            self.assertEqual(self.enrich(listings=None,state=state)['lhb_status'],'unavailable')

    def test_no_cross_stock_listing(self):
        self.assertEqual(self.enrich(listings=[{'ts_code':'000002.SZ'}])['lhb_status'],'not_listed')

    def supplemental(self, detail=None, basic=None):
        row={'ts_code':'000001.SZ'}
        detail={'ts_code':'000001.SZ','status':'一字板','lu_desc':'合成测试原因','tag':'3天3板','price':'10','limit_order':'100','limit_amount':'1000',**(detail or {})}
        basic={'ts_code':'000001.SZ','close':'10','free_share':'50','circ_mv':'1000','total_mv':'2000',**(basic or {})}
        enrich_groups([row],{}, {}, {'state':'complete'},[],[detail],[basic])
        return row

    def test_supplemental_fields_and_actual_float_are_separate(self):
        row=self.supplemental()
        self.assertEqual(row['limit_type'],'一字板')
        self.assertEqual(row['change_reason'],'合成测试原因')
        self.assertEqual(row['board_stat_label'],'3天3板')
        self.assertEqual(row['actual_float_value_yuan'],'5000000')
        self.assertEqual(row['float_market_value_yuan'],'10000000')
        self.assertEqual(row['total_market_value_yuan'],'20000000')
        self.assertEqual(row['seal_amount_yuan'],'1000')
        self.assertEqual(row['group_field_details']['actual_float_value_yuan']['basis'],'derived')

    def test_inconsistent_seal_and_quote_are_not_used(self):
        for detail in [{'limit_amount':'100000'},{'price':'11'},{'limit_order':None}]:
            self.assertIsNone(self.supplemental(detail)['seal_amount_yuan'])

    def test_missing_basic_does_not_use_ths_free_float(self):
        row=self.supplemental({'free_float':'999999999'}, {'free_share':None})
        self.assertIsNone(row['actual_float_value_yuan'])

    def test_duplicate_supplemental_stock_is_ambiguous(self):
        row={'ts_code':'000001.SZ'}
        detail={'ts_code':'000001.SZ','status':'一字板'}
        enrich_groups([row],{}, {}, {'state':'complete'},[],[detail,detail],[])
        self.assertIsNone(row['limit_type'])

    def test_new_archive_parameters_and_strict_dates(self):
        import json
        from app.market.review_fetch import parameters, validate_table
        from app.market.protocol import SourceFailure
        for dataset in ('pool_details','group_basics'):
            ctx={'dataset':dataset,'day':'2026-11-03','start_day':'2026-11-03','scope':'all'}
            api,params,_=parameters(ctx,offset=5000)
            self.assertEqual(params['trade_date'],'20261103')
            self.assertEqual(params['offset'],5000)
            if dataset=='pool_details':self.assertEqual(params['limit_type'],'涨停池')
            from app.market.review_fetch import REQUIRED
            fields=sorted(REQUIRED[dataset]); row={key:'1' for key in fields}
            row.update(ts_code='000001.SZ',trade_date='20261102')
            body=json.dumps({'code':0,'data':{'fields':fields,'items':[[row[k] for k in fields]]}}).encode()
            with self.assertRaises(SourceFailure):validate_table(body,ctx)

    def test_native_adapter_registered_and_bound(self):
        import json
        from datetime import datetime
        from app.market.tushare import parameters, parse_reference
        from app.market.tushare_specs import DATED_FAMILIES
        from app.market.review_fetch import REQUIRED
        clock=datetime.fromisoformat('2026-11-03T18:00:00+08:00')
        params=parameters('tushare_limit_list_ths',[],clock)
        self.assertNotIn('trade_date',params)  # collector supplies the proved session
        self.assertEqual(params['limit_type'],'涨停池')
        self.assertIn('tushare_limit_list_ths',DATED_FAMILIES)
        fields=sorted(REQUIRED['pool_details'])
        row={key:'1' for key in fields};row.update(ts_code='000001.SZ',trade_date='20261103')
        body=json.dumps({'code':0,'data':{'fields':fields,'items':[[row[k] for k in fields]]}}).encode()
        self.assertEqual(parse_reference(body,'tushare_limit_list_ths',clock).expected,1)

    def test_analysis_rejects_wrong_day_supplements(self):
        from test_review_expansion import Reader, table, DAY
        from test_review_metrics import pool
        from app.market.review_analysis import pool_groups
        source=pool(1);source['trade_date']=DAY.strftime('%Y%m%d')
        wrong={'ts_code':source['ts_code'],'trade_date':'20260101','status':'一字板'}
        reader=Reader({'pools':table('pools',[source]),'pool_details':table('pool_details',[wrong])})
        self.assertIsNone(pool_groups(reader,DAY)['records'][0]['limit_type'])
