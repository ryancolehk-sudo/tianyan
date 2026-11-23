"""Independent THS board fund snapshots. Receipt clocks are never market clocks."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

from app.evidence.schemas import ReferenceColumn, ReferencePayload
from app.market.protocol import NoRedirect, Response, SourceFailure
from app.market.reference import ReferenceBatch

FAMILIES = {'ths_industry_funds': 'hyzjl', 'ths_concept_funds': 'gnzjl'}
LABELS = {'hyzjl': '同花顺行业', 'gnzjl': '同花顺概念'}
MAX_PAGES = 16
MAX_EXTRA_REQUESTS = 2
FIELDS = ('code','name','index_value','pct_change','inflow_cny','outflow_cny','net_cny','company_count','received_at','page','pages','complete','missing_pages','rejected_rows','capture_id','cross_page_duplicates','recovered_pages','missing_page_reasons')

class Table(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.link=None; self.page=None; self.pages=[]; self.headings=[]; self.heading=None
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='tr': self.row=[]
        if tag=='td': self.cell=[]; self.link=None
        if tag=='th': self.heading=[]
        if tag=='a' and self.cell is not None: self.link=attrs.get('href')
        if tag=='span' and 'page_info' in attrs.get('class','').split(): self.page=[]
    def handle_data(self, value):
        if self.cell is not None:self.cell.append(value)
        if self.heading is not None:self.heading.append(value)
        if self.page is not None:self.page.append(value)
    def handle_endtag(self,tag):
        if tag=='td' and self.cell is not None:
            if self.row is not None:self.row.append((''.join(self.cell).strip(),self.link))
            self.cell=None
        if tag=='th' and self.heading is not None:self.headings.append(''.join(self.heading).strip());self.heading=None
        if tag=='tr' and self.row:
            self.rows.append(self.row);self.row=None
        if tag=='span' and self.page is not None:self.pages.append(''.join(self.page).strip());self.page=None

def number(value, multiplier=1):
    if value in {'','--','-'}:return None
    try:
        n=Decimal(value.replace(',','').rstrip('%'))*multiplier
        if not n.is_finite() or abs(n)>Decimal('1e16'):raise ValueError()
        return format(n,'f')
    except (ValueError,InvalidOperation):raise SourceFailure('schema_changed') from None

def parse_page(body, category, page, received):
    table=Table()
    def fail(reason,checkpoint):
        from app.market.ths_page_diagnostic import shape
        error=SourceFailure(reason)
        error.ths_page_diagnostic=shape(body,table,category,page,checkpoint)
        raise error from None
    try:table.feed(body.decode('gbk'))
    except UnicodeError:fail('schema_changed','decode')
    if len(table.pages)!=1:fail('incomplete_page','pagination_count')
    if not re.fullmatch(r'\d+/\d+',table.pages[0]):fail('incomplete_page','pagination_format')
    actual,total=map(int,table.pages[0].split('/'))
    if actual!=page:fail('incomplete_page','page_mismatch')
    if not page<=total<=MAX_PAGES:fail('incomplete_page','page_total')
    if not 1<=len(table.rows)<=50:fail('incomplete_page','row_count')
    if len(table.headings)!=11 or table.headings[4:7]!=['流入资金(亿)','流出资金(亿)','净额(亿)']:fail('schema_changed','headings')
    result=[];rejected=0
    pattern=r'https?://q\.10jqka\.com\.cn/'+('thshy' if category=='hyzjl' else 'gn')+r'/detail/code/(\d{6})/'
    for cells in table.rows:
        if len(cells)!=11:fail('schema_changed','row_width')
        identity=re.fullmatch(pattern,cells[1][1] or '')
        if not identity or not cells[1][0] or len(cells[1][0])>100:rejected+=1;continue
        vals=[x[0] for x in cells]
        try:
            result.append(dict(code=identity[1],name=vals[1],index_value=number(vals[2]),pct_change=number(vals[3]),
                inflow_cny=number(vals[4],100000000),outflow_cny=number(vals[5],100000000),net_cny=number(vals[6],100000000),
                company_count=number(vals[7]),received_at=received.isoformat(),page=str(page)))
        except SourceFailure:fail('schema_changed','numeric_field')
    return total,result,rejected

def request(category,page,token):
    url=f'https://data.10jqka.com.cn/funds/{category}/field/tradename/order/asc/page/{page}/ajax/1/free/1/'
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.85 Safari/537.36',
        'Referer':f'https://data.10jqka.com.cn/funds/{category}/','hexin-v':token,'Accept-Encoding':'identity','X-Requested-With':'XMLHttpRequest'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=10) as response:
            body=response.read(2_000_001)
            if len(body)>2_000_000:raise SourceFailure('response_too_large')
            return body,datetime.now(UTC)
    except urllib.error.HTTPError as exc:
        reason='rate_limited' if exc.code==429 else 'credentials_rejected' if exc.code in {401,403} else 'upstream_unavailable'
        from app.market.ths_funds_cooldown import retry_after
        try:
            delay=retry_after((exc.headers or {}).get('Retry-After',''),datetime.now(UTC)) if exc.code in {429,503} else None
        except SourceFailure as error:
            error.http_status=exc.code
            raise error from None
        finally:exc.close()
        raise SourceFailure(reason,retry_after=delay,http_status=exc.code) from None
    except (OSError,urllib.error.URLError):raise SourceFailure('transport_error') from None

def fetch(family,before_request):
    from py_mini_racer import MiniRacer
    category=FAMILIES[family];started=time.monotonic();rows=[];missing=[];rejected=0;count=0;total=1;hashes=[]
    extra=0;recovered=[];page_errors={};page_failures=[]
    try:
        with MiniRacer() as js:
            js.eval(Path(__file__).with_name('ths_client.js').read_text(encoding='utf8'),timeout=5000)
            page=1
            while page<=total:
                for attempt in range(2):
                    if count:time.sleep(1)
                    if before_request() is False:raise SourceFailure('permission_missing')
                    token=js.call('v',timeout=1000)
                    count+=1
                    try:
                        body,received=request(category,page,token)
                        hashes.append(hashlib.sha256(body).hexdigest())
                        pages,items,bad=parse_page(body,category,page,received)
                        if page==1:total=pages
                        if pages!=total:raise SourceFailure('incomplete_page')
                        rows.extend(items);rejected+=bad
                        page_errors.pop(page,None)
                        if attempt:recovered.append(page)
                        break
                    except SourceFailure as exc:
                        diagnostic=getattr(exc,'ths_page_diagnostic',None)
                        if diagnostic is not None:
                            page_failures.append(dict(diagnostic,attempt=attempt+1,received_at=received.isoformat(),reason=exc.reason))
                        if exc.reason in {'rate_limited','retry_after_out_of_range','credentials_rejected','permission_missing','local_request_wait'} or exc.retry_after is not None:raise
                        page_errors[page]=exc.reason
                        if not attempt and extra<MAX_EXTRA_REQUESTS and exc.reason in {'transport_error','timeout','incomplete_page'}:
                            extra+=1
                            continue
                        missing.append(page)
                        break
                page+=1
    except SourceFailure as exc:
        exc.request_count=count
        if page_failures:exc.ths_page_diagnostics=dict(version=1,family=family,events=page_failures)
        raise
    if not rows:
        exc=SourceFailure('upstream_unavailable');exc.request_count=count
        if page_failures:exc.ths_page_diagnostics=dict(version=1,family=family,events=page_failures)
        raise exc
    latest={};overlaps=0
    for row in rows:
        previous=latest.get(row['code'])
        if previous is not None:overlaps+=1
        if previous is None or datetime.fromisoformat(row['received_at'])>=datetime.fromisoformat(previous['received_at']):
            latest[row['code']]=row
    rows=list(latest.values())
    capture=hashlib.sha256(json.dumps(hashes).encode()).hexdigest()
    for row in rows:row.update(pages=str(total),complete='1' if not missing and not rejected and not overlaps else '0',missing_pages=','.join(map(str,missing)),rejected_rows=str(rejected),capture_id=capture,
        cross_page_duplicates=str(overlaps),recovered_pages=','.join(map(str,recovered)),missing_page_reasons=json.dumps(page_errors,sort_keys=True))
    dataset='market.ths.'+category
    payload=ReferencePayload(dataset=dataset,title=LABELS[category]+'资金净额',source_url=f'https://data.10jqka.com.cn/funds/{category}/',
        attribution='同花顺公开资金表',scope='同花顺资金净额，非东方财富主力净额。供应商未提供行情日期或时刻；仅记录各页实际接收时间，非原子快照。跨页重复保留较新有效观察并标覆盖未齐；缺页与身份缺失分别披露，不与其他来源拼接。',
        columns=[ReferenceColumn(key=k,label=k) for k in FIELDS],rows=rows,observed_day=None)
    received=datetime.now(UTC)
    incomplete=len(missing)+int(bool(rejected))+int(bool(overlaps))
    batch=ReferenceBatch(records=[(dataset,payload,None)],expected=1+incomplete,rejected=incomplete,requests=count)
    if page_failures:batch.ths_page_diagnostics=dict(version=1,family=family,events=page_failures)
    return Response(json.dumps(hashes).encode(),received,round((time.monotonic()-started)*1000),200,count),batch
