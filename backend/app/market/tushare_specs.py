"""Implemented, bounded Tushare-compatible endpoints; never an entitlement list."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TushareSpec:
    api: str
    title: str
    mode: str = 'table'
    selected: bool = False
    fields: str = ''
    minimum_interval: int = 21600
    exchange: str | None = None
    sector_type: str = 'concept'


SPECS = {
    'tushare_moneyflow_mkt_dc': TushareSpec('moneyflow_mkt_dc', '沪深盘后主力资金', 'dated_table',
        fields='trade_date,net_amount', minimum_interval=900),
    'tushare_stock_basic': TushareSpec('stock_basic', '股票目录', 'catalog', fields='ts_code,symbol,name,exchange,list_status,list_date'),
    'tushare_calendar_sse': TushareSpec('trade_cal', '沪市交易日历', 'calendar', exchange='SSE'),
    'tushare_calendar_szse': TushareSpec('trade_cal', '深市交易日历', 'calendar', exchange='SZSE'),
    'tushare_rt_k': TushareSpec('rt_k', '个股行情快照', 'snapshot', True,
        'ts_code,name,pre_close,high,open,low,close,vol,amount,num,trade_time', 15),
    'tushare_market_rt': TushareSpec('rt_k', '沪深京批量行情', 'market_snapshot', fields='ts_code,name,pre_close,high,open,low,close,vol,amount,trade_time,bid_price1,bid_volume1,ask_price1,ask_volume1', minimum_interval=15),
    'tushare_rt_indices': TushareSpec('rt_idx_k', '四指数实时快照', 'fixed_indices', fields='ts_code,name,pre_close,high,open,low,close,vol,amount,trade_time', minimum_interval=15),
    'tushare_market_limits': TushareSpec('stk_limit', '当日全市场限制价', 'limit_snapshot', fields='trade_date,ts_code,pre_close,up_limit,down_limit', minimum_interval=900),
    'tushare_daily': TushareSpec('daily', '个股日线', 'daily', True),
    'tushare_index_daily': TushareSpec('index_daily', '指数日线', 'daily', True),
    'tushare_daily_basic': TushareSpec('daily_basic', '估值与换手率', selected=True),
    'tushare_adj_factor': TushareSpec('adj_factor', '复权因子', selected=True),
    'tushare_moneyflow': TushareSpec('moneyflow', '个股资金流', selected=True),
    'tushare_limit_list_ths': TushareSpec('limit_list_ths', '同花顺涨停明细', minimum_interval=900),
    'tushare_limit_list_d': TushareSpec('limit_list_d', '涨跌停与炸板榜', minimum_interval=900),
    'tushare_income': TushareSpec('income', '利润表字段', selected=True,
        fields='ts_code,ann_date,f_ann_date,end_date,report_type,basic_eps,total_revenue,revenue,operate_profit,total_profit,n_income,n_income_attr_p,update_flag'),
    'tushare_balancesheet': TushareSpec('balancesheet', '资产负债表字段', selected=True,
        fields='ts_code,ann_date,f_ann_date,end_date,report_type,total_assets,total_liab,total_hldr_eqy_exc_min_int,update_flag'),
    'tushare_cashflow': TushareSpec('cashflow', '现金流量表字段', selected=True,
        fields='ts_code,ann_date,f_ann_date,end_date,report_type,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,n_incr_cash_cash_equ,update_flag'),
    'tushare_fina_indicator': TushareSpec('fina_indicator', '财务指标', selected=True,
        fields='ts_code,ann_date,end_date,eps,dt_eps,bps,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,update_flag'),
    'tushare_top_list': TushareSpec('top_list', '龙虎榜', minimum_interval=900),
    'tushare_top_inst': TushareSpec('top_inst', '龙虎榜买卖席位', fields='trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason', minimum_interval=900),
    'tushare_block_trade': TushareSpec('block_trade', '大宗交易'),
    'tushare_margin_detail': TushareSpec('margin_detail', '融资融券明细'),
    'tushare_ths_index': TushareSpec('ths_index', '同花顺概念目录'),
    'tushare_ths_member': TushareSpec('ths_member', '同花顺概念成分', 'ths_members',
        fields='ts_code,con_code,con_name', minimum_interval=21600),
    'tushare_ths_daily': TushareSpec('ths_daily', '同花顺板块日行情', 'dated_table',
        fields='ts_code,trade_date,close,open,high,low,pre_close,avg_price,change,pct_change,vol,turnover_rate', minimum_interval=3600),
    'tushare_shibor': TushareSpec('shibor', 'SHIBOR利率'),
    'tushare_cn_cpi': TushareSpec('cn_cpi', '月度CPI'),
    'tushare_stk_auction': TushareSpec('stk_auction', '集合竞价成交参考', minimum_interval=65),
    'tushare_dc_index': TushareSpec('dc_index', 'DC概念板块目录（日频）', 'dated_table',
        fields='ts_code,trade_date,name,pct_change,total_mv,turnover_rate,up_num,down_num,idx_type'),
    'tushare_dc_member': TushareSpec('dc_member', 'DC概念板块历史成分', 'dated_members',
        fields='trade_date,ts_code,con_code,name'),
    'tushare_direction_industry': TushareSpec('dc_index', '行业板块每日涨幅', 'dated_table',
        fields='ts_code,trade_date,name,pct_change,idx_type', sector_type='industry'),
    'tushare_direction_industry_funds': TushareSpec('moneyflow_ind_dc', '行业板块盘后主力资金', 'dated_table',
        fields='trade_date,content_type,ts_code,name,pct_change,net_amount', sector_type='industry'),
    'tushare_moneyflow_ind_dc': TushareSpec('moneyflow_ind_dc', 'DC概念板块盘后资金', 'dated_table',
        fields='trade_date,content_type,ts_code,name,pct_change,close,net_amount,net_amount_rate,rank'),
    'tushare_moneyflow_dc': TushareSpec('moneyflow_dc', 'DC个股盘后资金', 'dated_table',
        fields='trade_date,ts_code,name,pct_change,close,net_amount,net_amount_rate'),
    'tushare_limit_step': TushareSpec('limit_step', '收盘连板天梯', 'dated_table',
        fields='ts_code,name,trade_date,nums', minimum_interval=900),
    'tushare_stk_premarket': TushareSpec('stk_premarket', '当日盘前股本', 'dated_table',
        fields='trade_date,ts_code,total_share,float_share,pre_close,up_limit,down_limit', minimum_interval=3600),
    'tushare_index_basic_sse': TushareSpec('index_basic', '沪市指数基础资料', exchange='SSE',
        fields='ts_code,name,market,publisher,list_date,base_date,base_point'),
    'tushare_index_basic_szse': TushareSpec('index_basic', '深市指数基础资料', exchange='SZSE',
        fields='ts_code,name,market,publisher,list_date,base_date,base_point'),
    'tushare_stk_limit': TushareSpec('stk_limit', '所选股票每日涨跌停价格', selected=True,
        fields='trade_date,ts_code,pre_close,up_limit,down_limit'),
    'tushare_dividend': TushareSpec('dividend', '所选股票分红送股资料', selected=True,
        fields='ts_code,end_date,ann_date,div_proc,stk_div,cash_div,cash_div_tax,record_date,ex_date,pay_date,imp_ann_date'),
}
FAMILIES = frozenset(SPECS)
DATED_FAMILIES = frozenset(f for f, spec in SPECS.items() if spec.mode in {'dated_table','dated_members'}) | frozenset({'tushare_limit_list_d','tushare_limit_list_ths','tushare_stk_auction'})
SELECTED_FAMILIES = frozenset(f for f, spec in SPECS.items() if spec.selected)
CALENDAR_FAMILIES = ('tushare_calendar_sse', 'tushare_calendar_szse')
OFFICIAL_ENDPOINT = 'https://api.tushare.pro/'


def allowed_endpoints():
    from app.config import settings
    return frozenset({OFFICIAL_ENDPOINT, settings().tushare_endpoint})


DOCS = 'https://tushare.pro/document/1?doc_id=40'
API_DOCS = {'rt_k':'https://tushare.pro/document/2?doc_id=372','rt_idx_k':'https://tushare.pro/document/2?doc_id=403','index_basic':'https://tushare.pro/document/2?doc_id=94',
            'moneyflow_mkt_dc':'https://tushare.pro/document/2?doc_id=345',
            'ths_daily':'https://tushare.pro/document/2?doc_id=260',
            'ths_member':'https://tushare.pro/document/2?doc_id=261',
            'dc_index':'https://tushare.pro/document/2?doc_id=362',
            'dc_member':'https://tushare.pro/document/2?doc_id=363',
            'moneyflow_ind_dc':'https://tushare.pro/document/2?doc_id=344',
            'moneyflow_dc':'https://tushare.pro/document/2?doc_id=349',
            'limit_step':'https://tushare.pro/document/2?doc_id=356',
            'stk_premarket':'https://tushare.pro/document/2?doc_id=329',
            'stk_auction':'https://tushare.pro/document/2?doc_id=369',
            'stk_limit':'https://tushare.pro/document/2?doc_id=183',
            'dividend':'https://tushare.pro/document/2?doc_id=103',
            'top_inst':'https://tushare.pro/document/2?doc_id=107',
            'top_list':'https://tushare.pro/document/2?doc_id=106',
            'moneyflow':'https://tushare.pro/document/2?doc_id=170',
            'daily_basic':'https://tushare.pro/document/2?doc_id=32',
            'limit_list_ths':'https://tushare.pro/document/2?doc_id=355',
            'limit_list_d':'https://tushare.pro/document/2?doc_id=298'}
