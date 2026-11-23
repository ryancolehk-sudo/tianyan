"""Isolate the official anonymous SDK; bounded serial requests and quiet output."""
import calendar
import contextlib
import json
import os
import socket
import sys
from datetime import date, datetime, timedelta

from app.market.baostock_reader import rows


def read(request):
    import baostock as bs
    import baostock.util.socketutil as transport
    now = datetime.fromisoformat(request['now'])
    family, selection = request['family'], request['symbols']
    if family not in {'baostock_history', 'baostock_fundamental', 'baostock_classification'} or len(selection) > 3:
        raise ValueError('invalid SDK request')
    calls = 0
    original = transport.send_msg
    def counted(*args, **kwargs):
        nonlocal calls
        if calls >= 60:
            raise RuntimeError('request_budget_exceeded')
        calls += 1
        return original(*args, **kwargs)
    transport.send_msg = counted
    socket.setdefaulttimeout(30)
    logged_in = False
    datasets, errors = [], []
    def query(key, title, call, *, symbol=None, adjustment=None, period=None):
        try:
            result = call()
            fields = list(result.fields)
            values = rows(result, 8000)
            datasets.append({'dataset': key, 'title': title, 'fields': fields, 'rows': values,
                'symbol': symbol, 'adjustment': adjustment, 'period': period})
        except Exception:
            errors.append(key + (':' + symbol if symbol else ''))
    try:
        if bs.login().error_code != '0':
            raise RuntimeError('upstream_unavailable')
        logged_in = True
        end = str(now.date())
        start = str(now.date() - timedelta(days=180))
        if family == 'baostock_history':
            for symbol in selection:
                for flag, adjustment in [('3', 'unadjusted'), ('2', 'forward'), ('1', 'backward')]:
                    query('bars.daily.' + adjustment, '历史日K · ' + {'unadjusted':'不复权','forward':'前复权','backward':'后复权'}[adjustment],
                        lambda s=symbol, f=flag: bs.query_history_k_data_plus(s,
                            'date,code,open,high,low,close,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST',
                            start_date=start, end_date=end, frequency='d', adjustflag=f), symbol=symbol, adjustment=adjustment)
                if symbol.startswith(('sh.6', 'sz.0', 'sz.3')):
                    query('corporate.factors', '复权因子', lambda s=symbol: bs.query_adjust_factor(s, start_date=start, end_date=end), symbol=symbol)
        elif family == 'baostock_fundamental':
            # Ask the last four report periods; the source may not have published
            # a period yet. Empty is preserved, never interpreted as zero profit.
            quarter = (now.month - 1) // 3
            year = now.year
            if quarter == 0:
                year, quarter = year - 1, 4
            for symbol in selection:
                query('company.basic', '证券基本资料', lambda s=symbol: bs.query_stock_basic(code=s), symbol=symbol)
                for dataset, title, method in [('profit','利润指标',bs.query_profit_data),
                        ('balance','资产负债指标',bs.query_balance_data),('cashflow','现金流指标',bs.query_cash_flow_data),
                        ('growth','成长指标',bs.query_growth_data)]:
                    for offset in range(4):
                        position = year * 4 + quarter - 1 - offset
                        y, q = position // 4, position % 4 + 1
                        query('financial.'+dataset, title, lambda s=symbol,m=method,y=y,q=q: m(code=s,year=y,quarter=q),symbol=symbol,period=f'{y}-Q{q}')
                query('corporate.dividends','分红送配披露',lambda s=symbol:bs.query_dividend_data(code=s,year=str(now.year-1),yearType='report'),symbol=symbol)
        else:
            query('classification.industry','行业分类（供应商口径）',bs.query_stock_industry)
            for key, name, method in [('hs300','沪深300',bs.query_hs300_stocks),('sz50','上证50',bs.query_sz50_stocks),('zz500','中证500',bs.query_zz500_stocks)]:
                query('index.'+key, name+'成分', method)
        answer = {'datasets':datasets,'errors':errors}
    except RuntimeError:
        answer = {'error':'upstream_unavailable'}
    finally:
        if logged_in:
            try:bs.logout()
            except Exception:pass
        transport.send_msg = original
    answer['request_count'] = calls
    return answer


if __name__ == '__main__':
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    except ImportError:
        pass
    try:
        request = json.loads(sys.stdin.read(3000))
        with open(os.devnull,'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            answer = read(request)
        sys.stdout.write(json.dumps(answer, ensure_ascii=False))
    except Exception:
        sys.stdout.write('{"error":"upstream_unavailable","request_count":0}')
