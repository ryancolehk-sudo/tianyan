"""Source-labelled leaders; never derive dragon ranks from stock returns."""
import hashlib
import re

from app.evidence.schemas import ReferencePayload
from app.market import direction_sources as source
from app.market.protocol import SourceFailure

DATASET='market.direction.kpl_leaders_v1'
LABELS=('龙一','龙二','龙三','龙四','龙五')


def parse(response, plate_code, expected_day, *, daily=False):
    data=None
    try:
        data=source._unpack('direction_kpl_strength',response.body)
        if not isinstance(data,dict) or str(data.get('errcode'))!='0':
            raise SourceFailure('upstream_unavailable')
        if data.get('Day')!=[expected_day.isoformat()]:
            raise SourceFailure('source_date_mismatch')
        raw=data.get('list')
        if not isinstance(raw,list) or len(raw)>1000:
            raise SourceFailure('schema_changed')
        count=int(source._number(data.get('Count'),integer=True,signed=False))
        if count<len(raw):
            raise SourceFailure('schema_changed')
        leaders={};seen=set()
        for row in raw:
            if not isinstance(row,list) or len(row)!=70 or not re.fullmatch(r'\d{6}',str(row[0])) or row[0] in seen:
                raise SourceFailure('schema_changed')
            seen.add(row[0])
            label=row[24]
            if label not in LABELS:
                continue
            if label in leaders or not isinstance(row[1],str) or not 1<=len(row[1])<=40:
                raise SourceFailure('schema_changed')
            leaders[label]={'stock_code':row[0],'name':row[1],'label':label,'rank':str(LABELS.index(label)+1)}
        complete=len(leaders)==5 or count==len(raw)
        meta=dict(plate_code=plate_code,source_day=expected_day.isoformat(),
            archive_kind='daily' if daily else 'intraday',coverage='complete' if complete else 'returned_markers',
            returned_count=str(len(raw)),source_count=str(count),body_sha256=hashlib.sha256(response.body).hexdigest(),
            page_received_at=response.received_at.isoformat())
        rows=[{**meta,**leaders[label]} for label in LABELS if label in leaders]
        if not rows:
            rows=[{**meta,'stock_code':None,'name':None,'label':None,'rank':None}]
        return ReferencePayload(dataset=DATASET,title='开盘啦来源领涨标记',
            source_url=source.KPL_HISTORY_URL if daily else source.KPL_URL,
            attribution='开盘啦公开板块成分接口；本人本机研究',observed_day=expected_day,
            scope='ZhiShuStockList_W8/Type=6，原始第25字段龙一至龙五。保留来源名次，不按涨幅自造。'
                  '最多读取1000项；取得全部前五标记或Count项全部返回才认定覆盖完整。Time不是经核实的行情时刻，不映射为源时间。',
            columns=[dict(key=k,label=k) for k in rows[0]],rows=rows)
    except SourceFailure as error:
        error.direction_diagnostic=source._diagnostic('direction_kpl_strength',response.body,data,error)
        raise
    except (ValueError,TypeError,KeyError) as error:
        failure=SourceFailure('schema_changed')
        failure.direction_diagnostic=source._diagnostic('direction_kpl_strength',response.body,data,error)
        raise failure from None
