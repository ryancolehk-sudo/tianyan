"""Source-qualified, dated alias observations. Amounts always remain on original seat rows."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache
from html import unescape
import json
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from app.market.protocol import NoRedirect

PUBLIC = "https://www.kanpanba.com"
KPL = "https://pclhb.kaipanla.com/w1/api/index.php"
SEATS = {
  "国泰海通证券股份有限公司上海长宁区江苏路证券营业部": "/lhb/seat/99853249d885e6ee",
  "国泰海通证券股份有限公司成都天府大道证券营业部": "/lhb/seat/f5b88787648423de",
  "国新证券股份有限公司北京分公司": "/lhb/seat/832befd92220f746",
  "国泰海通证券股份有限公司武汉紫阳东路证券营业部": "/lhb/seat/51ac83c7cf1038ab",
  "国泰海通证券股份有限公司湛江万豪世家证券营业部": "/lhb/seat/371c4dd535c01a96",
  "国泰海通证券股份有限公司三亚迎宾路证券营业部": "/lhb/seat/b93dbcd1eb84b149",
  "国泰海通证券股份有限公司南京太平南路证券营业部": "/lhb/seat/8244ec8a8300b7ac",
  "国泰海通证券股份有限公司上海徐汇区宜山路证券营业部": "/lhb/seat/62ed7baad0ae971f"
}
_lock = threading.Lock()


def read(url, form=None):
    req = Request(url, data=urlencode(form).encode() if form else None,
                  headers={"User-Agent": "Tianyan/1.0 (private research)"})
    with build_opener(NoRedirect()).open(req, timeout=5) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("response_size")
        return raw.decode("utf8")


def public_seat(item):
    seat, path = item
    try:
        body = read(PUBLIC + path)
        full = re.search(r'</h1>\s*<p class="ssr-muted">([^<]+)</p>\s*<p>([^<]+)</p>', body)
        if not full or unescape(full[1]).strip() != seat:
            return []
        alias = re.search(r'俗称\s*([^·<]+)', full[2])
        stamp = re.search(r'数据截至\s*(\d{4})年(\d{1,2})月(\d{1,2})日', full[2])
        if not alias or not stamp:
            return []
        day = '%04d-%02d-%02d' % tuple(map(int,stamp.groups()))
        # The profile label is dated as of the source's update. Never backfill
        # older activity using the current nickname.
        codes = set(re.findall(fr'<tr><td><a href="/lhb/{day}">{day}</a></td><td[^>]*><a href="/stock/(\d{{6}})"',body))
        return [dict(seat=seat, code=code, day=day, label=unescape(alias[1]).strip(),
                     source="看盘吧", url=PUBLIC+path) for code in sorted(codes)]
    except (OSError, ValueError):
        return []


@lru_cache(maxsize=2)
def public_rows(bucket):
    with ThreadPoolExecutor(max_workers=3) as workers:
        return [row for rows in workers.map(public_seat, SEATS.items()) for row in rows]


def kpl(form):
    return json.loads(read(KPL, {**form, "UserID":"", "Token":""}))


@lru_cache(maxsize=1024)
def kpl_stock(item):
    day, code, bucket = item
    try:
        obj = kpl(dict(c="Stock",a="GetNewOneStockInfo",StockID=code,Time=day))
        if str(obj.get("errcode"))!="0" or obj.get("Time")!=day or str(obj.get("StockID",code))!=code:
            return []
        pairs = set()
        for group in obj.get("List", []):
            for side in ("BuyList","SellList"):
                for row in group.get(side, []):
                    labels=row.get("GroupIcon")
                    if isinstance(row.get("Name"),str) and isinstance(labels,list):
                        pairs.update((row["Name"],label) for label in labels if isinstance(label,str) and 0<len(label)<65)
        return [dict(day=day,code=code,seat=seat,label=label,source="开盘啦",url="https://www.kaipanla.com/page/lhb.html") for seat,label in sorted(pairs)]
    except (OSError,ValueError,TypeError,AttributeError):
        return []


@lru_cache(maxsize=32)
def daily_kpl(day, bucket):
    try:
        obj=kpl(dict(c="LongHuBang", a="GetStockList", Index=0, st=300, Time=day))
        status=str(obj.get("errcode","unknown"))
        if status!="0":
            return status,set()
        if obj.get("Time")!=day or not isinstance(obj.get("list"),list):
            return "date_or_schema_mismatch",set()
        return status,{str(row["ID"]) for row in obj["list"] if isinstance(row,dict) and re.fullmatch(r"\d{6}",str(row.get("ID")))}
    except (OSError,ValueError,TypeError,AttributeError):
        return "unavailable",set()


@lru_cache(maxsize=24)
def collect(day, bucket, requested):
    fallback=[row for row in public_rows(bucket) if row["day"]==day]
    state,codes=daily_kpl(day,bucket)
    focus=sorted(codes.intersection(requested))
    with ThreadPoolExecutor(max_workers=2) as workers:
        primary=[row for rows in workers.map(kpl_stock, [(day,c,bucket) for c in focus]) for row in rows]
    return dict(day=day, checked_at=datetime.now(UTC).isoformat(), kpl_status=state, rows=primary+[row for row in fallback if row["code"] in requested])


def review_seat_labels(day, requested=()):
    if not _lock.acquire(blocking=False):
        # Single flight; wait for the existing retrieval to populate the cache.
        if not _lock.acquire(timeout=30):
            return dict(day=day.isoformat(),kpl_status="pending",rows=[])
    try:
        return collect(day.isoformat(),int(time.time()//1800),requested)
    finally:
        _lock.release()
