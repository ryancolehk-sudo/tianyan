"""Small independent adapter for the public quote site's bounded ranking pages.

No SDK, token, proxy rotation, hidden retry, inferred market clock or invented
gross inflow/outflow. The main-order net is the provider's f62 definition; it is
not a measurement of all cash entering a company or a local trading signal.
"""
import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from app.evidence.schemas import ReferencePayload
from app.market.protocol import (MAX_BODY, Response, SHANGHAI, SourceFailure, SourceRedirect,
    eastmoney_allocation_url, fetch_eastmoney_allocation, fetch_https)
from app.market.reference import ReferenceBatch

FAMILIES = frozenset({'eastmoney_funds','eastmoney_boards'})
ENDPOINT = 'https://push2.eastmoney.com/api/qt/clist/get'
REFERENCE = 'https://data.eastmoney.com/zjlx/detail.html?mkt=all'
BOARD_REFERENCE = 'https://quote.eastmoney.com/center/boardlist.html'
VERSION = 'eastmoney-trading-v1'
FIELDS = 'f3,f6,f12,f13,f14,f62,f66,f72,f78,f84,f104,f105,f124,f128,f140'
FILTERS = {'stocks':'m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048',
    'industry':'m:90+t:2','industry_l2':'m:90+s:4','concept':'m:90+t:3'}
TABLES = {'eastmoney_funds':(('stock_funds','stocks','个股主力净流入'),('industry_funds','industry_l2','二级行业主力净流入'),
    ('concept_funds','concept','概念主力净流入')),
    'eastmoney_boards':(('industry','industry','行业涨幅榜'),('concept','concept','概念涨幅榜'))}
COLUMNS = [('symbol','代码',None),('code','原代码',None),('name','名称',None),('exchange','交易所',None),
    ('board_code','板块代码',None),('change_percent','涨跌幅','%'),('amount_cny','成交额','CNY'),
    ('net_inflow_cny','主力净流入','CNY'),('inflow_cny','流入（未提供）','CNY'),('outflow_cny','流出（未提供）','CNY'),
    ('super_large_net_cny','超大单净流入','CNY'),('large_net_cny','大单净流入','CNY'),
    ('medium_net_cny','中单净流入','CNY'),('small_net_cny','小单净流入','CNY'),
    ('up_count','上涨家数','家'),('down_count','下跌家数','家'),('lead_symbol','领涨股代码',None),
    ('lead_name','领涨股名称',None),('source_time','来源时点',None),('trade_day','源数据日',None),
    ('industry_level','行业层级',None),('source_total','供应商总数','条'),('returned_count','本批返回数','条')]


class EastmoneyFailure(SourceFailure):
    def __init__(self, reason, requests=0, *, retry_after=None):
        super().__init__(reason,retry_after=retry_after)
        self.request_count=requests


def ranking_url(category, *, funds=False, board_code=None, page=1, stable_codes=False):
    if category not in FILTERS or board_code is not None and not re.fullmatch(r'BK\d{4}',board_code):
        raise SourceFailure('endpoint_not_allowed')
    maximum = 80 if category == 'stocks' and funds and board_code is None else 50
    if type(page) is not int or not 1<=page<=maximum or page!=1 and (not funds or category not in {'concept','industry_l2','stocks'} or board_code is not None):
        raise SourceFailure('endpoint_not_allowed')
    if type(stable_codes) is not bool or stable_codes and (not funds or board_code is not None):
        raise SourceFailure('endpoint_not_allowed')
    return ENDPOINT+'?'+urlencode({'pn':page,'pz':200,'po':1,'np':1,'fltt':2,'invt':2,
        'fid':'f12' if stable_codes else 'f62' if funds else 'f3','fs':'b:'+board_code if board_code else FILTERS[category], 'fields':FIELDS})


def number(value, *, integer=False, nonnegative=False):
    if value in (None,'','-','--'):
        return None
    if type(value) not in (str,int,Decimal):
        raise SourceFailure('invalid_number')
    raw=format(value,'f') if isinstance(value,Decimal) else str(value)
    if not re.fullmatch(r'-?\d{1,16}'+('' if integer else r'(?:\.\d{1,12})?'),raw):
        raise SourceFailure('invalid_number')
    if nonnegative and Decimal(raw)<0:
        raise SourceFailure('invalid_number')
    return raw


def text(value, *, required=False):
    if value in (None,'','-','--') and not required:
        return None
    if not isinstance(value,str) or not value.strip() or len(value)>200 or any(ord(c)<32 or c in '<>' for c in value):
        raise SourceFailure('schema_changed')
    return value


def parse_ranking(body, received_at, dataset, title, *, board_code=None, industry_level=None, capture_page=None):
    try:
        if industry_level is not None and (industry_level != 2 or dataset != 'market.eastmoney.industry_funds'):
            raise SourceFailure('schema_changed')
        if len(body)>MAX_BODY:
            raise SourceFailure('response_too_large')
        raw=json.loads(body,parse_float=Decimal,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(raw.get('rc')) is not int or raw['rc']!=0 or not isinstance(raw.get('data'),dict):
            raise SourceFailure('upstream_unavailable')
        data=raw['data'];items=data.get('diff')
        if not isinstance(items,list) or len(items)>200 or type(data.get('total')) is not int or data['total']<len(items):
            raise SourceFailure('schema_changed')
        rows=[];seen=set();dates=set()
        for row_index, item in enumerate(items):
            if not isinstance(item,dict):
                raise SourceFailure('schema_changed')
            code=text(item.get('f12'),required=True)
            is_stock=dataset.endswith(('stock_funds','constituents'))
            if not re.fullmatch(r'\d{6}' if is_stock else r'BK\d{4}',code):
                raise SourceFailure('instrument_mismatch')
            market=item.get('f13')
            if is_stock and market not in (0,1):
                raise SourceFailure('instrument_mismatch')
            identity=(market,code)
            if identity in seen:
                raise SourceFailure('duplicate_instrument')
            seen.add(identity)
            clock=None
            epoch=number(item.get('f124'),integer=True,nonnegative=True)
            if epoch is not None and int(epoch)>0:
                clock=datetime.fromtimestamp(int(epoch),UTC)
                if clock>received_at+timedelta(seconds=60):
                    if capture_page is not None and dataset in {'market.eastmoney.'+name for name, _, _ in TABLES['eastmoney_funds']}:
                        from app.market.fund_time_diagnostic import rejection
                        raise rejection(stage='page_validation', table=dataset.removeprefix('market.eastmoney.'),
                            page=capture_page, row_index=row_index, code=code, source_time=clock,
                            page_received_at=received_at, body=body)
                    raise SourceFailure('future_source_time')
                dates.add(clock.astimezone(SHANGHAI).date())
            row={key:None for key,_,_ in COLUMNS}
            row.update(symbol=code,code=code,name=text(item.get('f14'),required=True),board_code=board_code,
                exchange=('SSE' if market==1 else 'BSE' if code.startswith(('4','8','92')) else 'SZSE') if is_stock else None,
                change_percent=number(item.get('f3')),amount_cny=number(item.get('f6'),nonnegative=True),
                net_inflow_cny=number(item.get('f62')),super_large_net_cny=number(item.get('f66')),
                large_net_cny=number(item.get('f72')),medium_net_cny=number(item.get('f78')),small_net_cny=number(item.get('f84')),
                up_count=number(item.get('f104'),integer=True,nonnegative=True),
                down_count=number(item.get('f105'),integer=True,nonnegative=True),
                lead_symbol=text(item.get('f140')),lead_name=text(item.get('f128')),
                source_time=clock.isoformat() if clock else None,trade_day=str(clock.astimezone(SHANGHAI).date()) if clock else None,
                industry_level=str(industry_level) if industry_level is not None else None,
                source_total=str(data['total']),returned_count=str(len(items)))
            rows.append(row)
        return ReferencePayload(dataset=dataset,title=title,source_url=REFERENCE if dataset.endswith('_funds') else BOARD_REFERENCE,
            attribution='东方财富公开行情页；个人研究，使用范围以当前核实配置为准',
            observed_day=next(iter(dates)) if len(dates)==1 else None,
            scope='每表仅一页、最多200行；按本页返回排序，不称全市场完整覆盖。金额为元，涨幅为%。主力净流入采用东方财富口径；不推导未提供的流入/流出。来源时间只用f124，缺失不借接收时点；历史仅为实际保存批次。',
            columns=[{'key':key,'label':label,'unit':unit} for key,label,unit in COLUMNS],rows=rows)
    except (ValueError,TypeError,KeyError,UnicodeError,OverflowError,OSError):
        raise SourceFailure('schema_changed') from None


CATALOG_FAILURES = frozenset({'timeout','transport_error','upstream_unavailable','permission_missing',
    'rate_limited','schema_changed','response_too_large','upstream_redirect','unsafe_upstream_response'})
CATALOG_DEFERRALS = frozenset({'rate_limited','intraday_window_wait','local_request_wait'})

TRANSIENT_TABLE_FAILURES = frozenset({'timeout','transport_error','upstream_unavailable'})
RECOVERABLE_TABLE_FAILURES = TRANSIENT_TABLE_FAILURES | {'upstream_redirect', 'schema_changed'}


def fetch_rankings(family, reserve, board_codes=(), *, include_catalog=False, full_boards=False):
    if family not in FAMILIES or family=='eastmoney_funds' and board_codes or len(board_codes)>3 or len(set(board_codes))!=len(board_codes):
        raise EastmoneyFailure('endpoint_not_allowed')
    if include_catalog and family!='eastmoney_funds':
        raise EastmoneyFailure('endpoint_not_allowed')
    if full_boards:
        if family!='eastmoney_funds' or board_codes:
            raise EastmoneyFailure('endpoint_not_allowed')
        from app.market.fund_universe import fetch
        return fetch(reserve,include_catalog=include_catalog)
    pieces=[];failures=[];allocated=[];requests=0;started=time.monotonic()
    selections=list(TABLES[family])
    for board in board_codes:
        if not re.fullmatch(r'BK\d{4}',board):
            raise EastmoneyFailure('endpoint_not_allowed')
        selections.append(('constituents','stocks','已配置板块成分 · '+board))
    def request(url, *, original_url=None, table_key=None):
        nonlocal requests
        # Every physical request, including an allocated address, rechecks
        # current rights/lease/window and consumes the original shared budget.
        reserve()
        requests+=1
        if original_url is None:
            return fetch_https(url)
        allocated.append(table_key)
        return fetch_eastmoney_allocation(url,original_url=original_url)

    try:
        for index,(table,category,title) in enumerate(selections):
            board=board_codes[index-len(TABLES[family])] if index>=len(TABLES[family]) else None
            url=ranking_url(category,funds=family=='eastmoney_funds',board_code=board)
            try:
                try:
                    response=request(url)
                except SourceRedirect as redirect:
                    target=eastmoney_allocation_url(url,redirect)
                    response=request(target,original_url=url,table_key=table+(':'+board if board else ''))
                parse_ranking(response.body,response.received_at,'market.eastmoney.'+table,title,board_code=board)
            except SourceFailure as error:
                # These are independent tables. A stock-page network failure
                # does not prove that either board page will also fail.
                # A failed table's schema stays rejected; permissions, permits
                # and actual throttling still stop this batch. No hidden retry.
                if family!='eastmoney_funds' or error.reason not in RECOVERABLE_TABLE_FAILURES:
                    raise
                failures.append({'table':table,'reason':error.reason})
                continue
            pieces.append({'table':table,'title':title,'board_code':board,'received_at':response.received_at.isoformat(),
                           'body':response.body.decode('utf-8'),
                           **({'industry_filter':FILTERS[category]} if category=='industry_l2' else {})})
        if not pieces:
            raise SourceFailure(failures[-1]['reason'] if failures else 'upstream_unavailable')
        metadata={}
        if include_catalog:
            from app.market.industry_catalog import fetch_catalog
            # A budget/window boundary may defer optional metadata after all
            # financial requests succeeded. Revoked permission and lost leases
            # still abort; collect_job rechecks both again before committing.
            try:
                reserve()
            except SourceFailure as error:
                if error.reason not in CATALOG_DEFERRALS:raise
                metadata['catalog_deferred']={'reason':error.reason,'retry_after':error.retry_after}
            else:
                requests+=1
                try:
                    catalog=fetch_catalog()
                    metadata['catalog']={'received_at':catalog.received_at.isoformat(),
                        'body':catalog.body.decode('utf-8')}
                except SourceFailure as error:
                    if error.reason not in CATALOG_FAILURES:raise
                    metadata['catalog_failure']={'reason':error.reason,'retry_after':error.retry_after}
        value={'family':family,'tables':pieces,'request_count':requests,**metadata}
        if failures:value['failed_tables']=failures
        if allocated:value['allocated_tables']=allocated
        body=json.dumps(value,ensure_ascii=False).encode()
        if len(body)>MAX_BODY:
            raise SourceFailure('response_too_large')
        return Response(body,datetime.now(UTC),round((time.monotonic()-started)*1000),200,requests)
    except SourceFailure as error:
        raise EastmoneyFailure(error.reason,requests,retry_after=error.retry_after) from None


def parse_rankings(body,received_at,family):
    try:
        from app.market.fund_universe import MAX_CAPTURE_BODY
        if len(body)>MAX_CAPTURE_BODY:
            raise SourceFailure('response_too_large')
        value=json.loads(body)
        if value.get('format')=='eastmoney-funds-pages-v1':
            if family!='eastmoney_funds':raise ValueError()
            from app.market.fund_universe import parse
            return parse(value,received_at)
        if value['family']!=family or family not in FAMILIES or not isinstance(value['tables'],list):
            raise ValueError()
        tables=value['tables'];failures=value.get('failed_tables',[]);allocated=value.get('allocated_tables',[])
        if (not isinstance(failures,list) or not isinstance(allocated,list)
                or any(not isinstance(item,str) for item in allocated)
                or len(set(allocated))!=len(allocated)
                or type(value['request_count']) is not int):raise ValueError()
        catalog=value.get('catalog');catalog_failure=value.get('catalog_failure')
        catalog_deferred=value.get('catalog_deferred')
        has_catalog=catalog is not None or catalog_failure is not None
        if sum(item is not None for item in (catalog,catalog_failure,catalog_deferred))>1:raise ValueError()
        if (has_catalog or catalog_deferred is not None) and family!='eastmoney_funds':raise ValueError()
        if family=='eastmoney_funds':
            if not 1<=len(tables)<=3 or value['request_count']!=3+len(allocated)+int(has_catalog) or len(failures)>2:
                raise ValueError()
        elif (failures or not len(TABLES[family])<=len(tables)<=len(TABLES[family])+3
                or value['request_count']!=len(tables)+len(allocated)):
            raise ValueError()
        records=[];seen=set()
        for piece in tables:
            name=piece['table'];board=piece.get('board_code')
            if name not in {row[0] for row in TABLES[family]}|({'constituents'} if family=='eastmoney_boards' else set()):
                raise ValueError()
            if (name,board) in seen or family=='eastmoney_funds' and board is not None:
                raise ValueError()
            seen.add((name,board))
            source_received=datetime.fromisoformat(piece['received_at'])
            if source_received.utcoffset() is None or source_received>received_at:
                raise ValueError()
            industry_filter=piece.get('industry_filter')
            if industry_filter is not None and (name!='industry_funds' or industry_filter!=FILTERS['industry_l2']):
                raise ValueError()
            payload=parse_ranking(piece['body'].encode(),source_received,'market.eastmoney.'+name,piece['title'],
                board_code=board,industry_level=2 if industry_filter else None)
            if name+(':'+board if board else '') in allocated:
                payload.scope+=' 本表经供应商同接口分配节点接收，时效仍以实际来源时点判断。'
            records.append((payload.dataset+(':'+board if board else ''),payload,None))
        if family=='eastmoney_funds':
            failed=set()
            for failure in failures:
                if (not isinstance(failure,dict) or set(failure)!={'table','reason'}
                        or failure['table'] in failed or failure['reason'] not in RECOVERABLE_TABLE_FAILURES):
                    raise ValueError()
                failed.add(failure['table'])
            success={name for name,_ in seen}
            if failed & success or success|failed!={name for name,_,_ in TABLES[family]}:
                raise ValueError()
        else:
            failed=set()
        requested={name+(':'+board if board else '') for name,board in seen}|failed
        if not set(allocated)<=requested:raise ValueError()
        if catalog is not None:
            from app.market.industry_catalog import parse_catalog
            stamp=datetime.fromisoformat(catalog['received_at'])
            if stamp.utcoffset() is None or stamp>received_at:raise ValueError()
            payload=parse_catalog(catalog['body'].encode(),stamp)
            records.append((payload.dataset,payload,None))
        for problem,allowed in ((catalog_failure,CATALOG_FAILURES),(catalog_deferred,CATALOG_DEFERRALS)):
            if problem is None:continue
            if (not isinstance(problem,dict) or set(problem)!={'reason','retry_after'}
                    or problem['reason'] not in allowed):raise ValueError()
            wait=problem['retry_after']
            if wait is not None and (type(wait) is not int or not 0<=wait<=86400):raise ValueError()
        rejected=len(failures)+int(catalog_failure is not None or catalog_deferred is not None)
        return ReferenceBatch(records=records,expected=len(records)+rejected,rejected=rejected,requests=value['request_count'])
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise SourceFailure('schema_changed') from None
