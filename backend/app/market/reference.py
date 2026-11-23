"""Normalize public reference tables with explicit scope, units and provenance."""
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.evidence.schemas import BarPayload, ReferencePayload
from app.market.protocol import InstrumentKey, Response, SHANGHAI, SourceFailure

MAX_REFERENCE_BODY = 8_000_000
LABELS = {'date':'交易日期','code':'证券代码','code_name':'名称','open':'开盘','high':'最高','low':'最低','close':'收盘',
    'volume':'成交量','amount':'成交额','adjustflag':'复权标志','turn':'换手率','tradestatus':'交易状态','pctChg':'涨跌幅',
    'peTTM':'市盈率TTM','pbMRQ':'市净率','psTTM':'市销率TTM','pcfNcfTTM':'市现率TTM','isST':'ST标记',
    'pubDate':'公告日期','statDate':'报告期','updateDate':'来源更新日期','industry':'行业','industryClassification':'分类口径',
    'ipoDate':'上市日期','outDate':'退市日期','type':'证券类型','status':'上市状态','roeAvg':'平均净资产收益率',
    'npMargin':'净利率','gpMargin':'毛利率','netProfit':'净利润','epsTTM':'每股收益TTM','MBRevenue':'主营收入',
    'totalShare':'总股本','liqaShare':'流通股本','dividOperateDate':'除权除息日','foreAdjustFactor':'前复权因子',
    'backAdjustFactor':'后复权因子','adjustFactor':'本次复权因子','dividCashPsBeforeTax':'每股税前派息',
    'dividCashPsAfterTax':'每股税后派息','dividPreNoticeDate':'预披露日期','dividPlanAnnounceDate':'方案公告日期'}
UNITS = {'open':'元','high':'元','low':'元','close':'元','volume':'股','amount':'元','turn':'%','pctChg':'%',
    'netProfit':'元','MBRevenue':'元','totalShare':'股','liqaShare':'股','epsTTM':'元/股','dividCashPsBeforeTax':'元/股','dividCashPsAfterTax':'元/股'}


@dataclass
class ReferenceBatch:
    records: list[tuple[str, ReferencePayload, InstrumentKey | None]] = field(default_factory=list)
    bars: list[tuple[InstrumentKey, BarPayload]] = field(default_factory=list)
    expected: int = 0
    rejected: int = 0
    requests: int = 0
    # Auction batches only: pairs the summary table with its detail table.
    batch_hash: str | None = None


def fetch_references(family, selection):
    request = {'family':family,'symbols':[('sh.' if item.exchange=='SSE' else 'sz.')+item.symbol for item in selection],
        'now':datetime.now(UTC).astimezone(SHANGHAI).isoformat()}
    started=time.monotonic()
    try:
        result=subprocess.run([sys.executable,'-m','app.market.reference_reader'],input=json.dumps(request),
            capture_output=True,text=True,timeout=150,env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'})
    except subprocess.TimeoutExpired:
        raise SourceFailure('timeout') from None
    if result.returncode or len(result.stdout)>MAX_REFERENCE_BODY:
        raise SourceFailure('upstream_unavailable')
    return Response(result.stdout.encode(),datetime.now(UTC),round((time.monotonic()-started)*1000),200)


def numeric(value, *, integer=False):
    if value in {'','-', '--', None}:
        return None
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,16}(?:\.\d{1,10})?',value):
        raise ValueError('invalid unsigned numeric value')
    number=Decimal(value)
    if integer and number != number.to_integral_value():
        raise ValueError('nonintegral stock volume')
    return str(int(number)) if integer else format(number,'f').rstrip('0').rstrip('.') if '.' in value else value


def parse_references(body, received_at, selection):
    try:
        if len(body)>MAX_REFERENCE_BODY or received_at.utcoffset() is None:
            raise ValueError('invalid envelope')
        data=json.loads(body)
        if data.get('error'):
            raise SourceFailure('upstream_unavailable')
        datasets=data['datasets']
        if not isinstance(datasets,list) or len(datasets)>60:
            raise ValueError('dataset budget')
        requested={('sh.' if item.exchange=='SSE' else 'sz.')+item.symbol:item for item in selection}
        result=ReferenceBatch(requests=data['request_count'])
        if type(result.requests) is not int or not 1<=result.requests<=60:
            raise ValueError('request budget')
        errors=data.get('errors',[])
        if not isinstance(errors,list) or len(errors)>60:
            raise ValueError('error budget')
        result.rejected=len(errors);result.expected=len(errors)
        for item in datasets:
            name=item['dataset'];fields=item['fields'];raw=item['rows'];symbol=item.get('symbol')
            if symbol is not None and symbol not in requested:
                raise ValueError('unexpected symbol')
            key=requested.get(symbol)
            if not isinstance(fields,list) or not fields or len(fields)>60 or len(set(fields))!=len(fields) or len(raw)>8000:
                raise ValueError('invalid table')
            if any(not isinstance(row,dict) or set(row)!=set(fields) for row in raw):
                raise ValueError('invalid table rows')
            result.expected+=1
            period=item.get('period') if name.startswith('financial.') else None
            if name.startswith('financial.'):
                if not isinstance(period,str) or not re.fullmatch(r'\d{4}-Q[1-4]',period):
                    raise ValueError('financial request period absent')
                if any(row.get('statDate') and f"{date.fromisoformat(row['statDate']).year}-Q{(date.fromisoformat(row['statDate']).month-1)//3+1}"!=period for row in raw):
                    raise ValueError('financial report period mismatch')
            payload=ReferencePayload(dataset=name,title=item['title']+(' · '+period if period else ''),source_url='https://www.baostock.com/',
                attribution='BaoStock 官方匿名API',scope='供应商参考资料；当前采集的样本范围，不代表完整市场或当时已知。财务为指标，并非完整原始三表；未知单位保留为空。',
                columns=[{'key':f,'label':LABELS.get(f,f),'unit':('点' if key and key.asset_type=='index' and f in {'open','high','low','close'} else UNITS.get(f))} for f in fields],
                rows=[{f:None if row[f]=='' else row[f] for f in fields} for row in raw],observed_day=received_at.astimezone(SHANGHAI).date())
            if name.startswith('bars.daily.'):
                if key is None:raise ValueError('bar identity absent')
                seen=set()
                valid_rows=[]
                for row in raw:
                    result.expected+=1
                    try:
                        day=date.fromisoformat(row['date'])
                        if day in seen or day>received_at.astimezone(SHANGHAI).date() or row['code']!=symbol:
                            raise ValueError('invalid bar identity/date')
                        seen.add(day)
                        # Suspended/empty price rows are not zero-valued candles.
                        if not all(row[f] and Decimal(row[f])>0 for f in ['open','high','low','close']):
                            valid_rows.append({f:None if row[f]=='' else row[f] for f in fields})
                            result.rejected+=1
                            continue
                        bar=BarPayload(open=numeric(row['open']),high=numeric(row['high']),low=numeric(row['low']),close=numeric(row['close']),
                            volume_shares=numeric(row['volume'],integer=True) if key.asset_type=='stock' else None,
                            interval='1d',bar_end_at=None,trade_day=day,price_unit='point' if key.asset_type=='index' else 'CNY',
                            adjustment=item['adjustment'],amount_cny=numeric(row['amount']) if key.asset_type=='stock' else None)
                        result.bars.append((key,bar))
                        valid_rows.append({f:None if row[f]=='' else row[f] for f in fields})
                    except (ValueError,InvalidOperation):
                        result.rejected+=1
                payload=payload.model_copy(update={'rows':valid_rows})
            elif key and 'code' in fields and any(row['code']!=symbol for row in raw):
                raise ValueError('reference identity mismatch')
            result.records.append((name+(':'+key.identity if key else '')+(':'+period if period else ''),payload,key))
        return result
    except SourceFailure:
        raise
    except (KeyError,TypeError,ValueError,AttributeError):
        raise SourceFailure('schema_changed') from None
