"""Public post-close feeds. Keep source thresholds separate from local estimates."""
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://dycalchis.eastmoney.com/price-anomaly/"
REPORT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
MONITOR = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
RULES = {1: "主板十日内四次同向异常波动", 2: "创业板十日内三次同向异常波动",
         3: "科创板十日内三次同向异常波动", 8: "北交所十日内三次同向异常波动"}
for key, window, threshold in [(4,10,100),(5,10,-50),(6,30,200),(7,30,-70),
                                (40,10,150),(50,10,-60),(60,30,300),(70,30,-75)]:
    RULES[key] = f"连续{window}个交易日内收盘价涨跌幅偏离值累计达到{threshold:+d}%"


def get(url, **params):
    address = url + ("?" + urlencode(params) if params else "")
    req = Request(address, headers={"Referer": "https://vipmoney.eastmoney.com/", "User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=12) as response:
        payload = response.read(8_000_001)
    if len(payload) > 8_000_000:
        raise ValueError("异动数据超过接收上限")
    return json.loads(payload.decode("utf-8-sig"))


def live(kind):
    rows = []
    stamp = None
    for page in range(1, 101):
        data = get(BASE + kind, team="h5", product="EastMoney", client="WAP", version="9001",
                   name="WAP", user="123", pageSize=200, pageNo=page, showBJS=1)
        if data.get("result") != 0 or data.get("open") != 0:
            raise ValueError("盘后数据尚未发布")
        day = date.fromisoformat(str(data["date"]))
        if stamp and day != stamp:
            raise ValueError("交易日已切换")
        stamp = day
        rows.extend(data["data"])
        if page >= data["pages"]:
            return day, rows
    raise ValueError("异动分页未完成")


def report(name, condition, order):
    rows = []
    for page in range(1, 101):
        data = get(REPORT, reportName=name, columns="ALL", filter=condition,
                   pageNumber=page, pageSize=500, sortColumns=order, sortTypes=",".join("-1" for _ in order.split(",")))
        if data.get("code") == 9201 and data.get("result") is None:  # 来源明确返回无记录
            return rows
        if not data.get("success"):
            raise ValueError("公告或历史记录不可用")
        result = data["result"]
        rows.extend(result["data"])
        if page >= result["pages"]:
            return rows
    raise ValueError("历史分页未完成")


def closes(symbol, day):
    data = get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
               param=f"{symbol},day,,{day},6,qfq")["data"][symbol]
    bars = (data.get("qfqday") or data["day"])[-4:]
    return [(x[0], float(x[2])) for x in bars]


def three_days(rows, day):
    symbols = {}
    for row in rows:
        code = row["code"]
        if code.startswith(("4", "8", "92")):
            continue
        sh = code.startswith("6")
        stock = ("sh" if sh else "sz") + code
        index = ("sh000688" if code.startswith("688") else "sh000002" if sh else
                 "sz399102" if code.startswith(("300","301")) else "sz399107")
        symbols[code] = stock, index
    def read(symbol):
        try:
            return symbol, closes(symbol, day)
        except (OSError, ValueError, KeyError, TypeError):
            return symbol, []
    with ThreadPoolExecutor(max_workers=4) as workers:
        prices = dict(workers.map(read, sorted({s for pair in symbols.values() for s in pair})))
    for row in rows:
        if row["code"] not in symbols:
            continue
        a, b = (prices[s] for s in symbols[row["code"]])
        if len(a) == len(b) == 4 and a[-1][0] == day and [x[0] for x in a] == [x[0] for x in b] and all(
                math.isfinite(price) and price > 0 for _, price in a+b):
            row["three"] = round(100 * (a[-1][1]/a[0][1] - b[-1][1]/b[0][1]), 2)


def fetch_snapshot():
    day, raw = live("list")
    repeated_day, counts = live("count")
    if repeated_day != day:
        raise ValueError("两个行情接口的交易日不一致")
    predictions = []
    for r in raw:
        key = r["e"]
        if r["s"] == 6 and key in (4,5,6,7):
            key *= 10
        predictions.append(dict(code=r["c"], name=r["n"], change=r["a"], deviation=r["x"],
                                days=r["d"], target=r["t"], state=r["o"], rule=RULES.get(key, "来源规则未识别")))
    repeated = [dict(code=r["c"],name=r["n"],change=r["a"],times=r["t"],days=r["d"],three=None)
                for r in counts if r["t"] >= 2]
    three_days(repeated, day.isoformat())
    start = day - timedelta(days=90)
    records = report("RPT_WATCH_UNUSUAL_FLUCTUATE", f"(TRADE_DATE>='{start}')(TRADE_DATE<'{day}')", "TRADE_DATE,SECURITY_CODE")
    history = [dict(date=r["TRADE_DATE"][:10],code=r["SECURITY_CODE"],name=r["SECURITY_NAME_ABBR"],
                    change=r["CHANGE_RATE"],deviation=r["DEVUATION_VALUE"],days=r["MAX_DAYS"],
                    target=r["CHANGE_RATE_TARGET"],state=int(r["IS_HAPPEN"]),rule=r["UNUSUAL_TYPE"]) for r in records]
    pool = []
    def add(code, name, start, end, market, kind, link):
        # Include announced upcoming monitoring periods, as in the exchange list.
        if end >= day.isoformat():
            pool.append(dict(code=code,name=name,start=start,end=end,market=market,kind=kind,
                             remaining=(date.fromisoformat(end)-day).days,link=link if str(link).startswith("https://") else None))
    for r in get(MONITOR):
        add(r["STKCODE"],r["STKNAME"],r["VALIDATESTARTDATE"],r["VALIDATEENDDATE"],
            {"1":"上交所","0":"深交所","B":"北交所"}.get(str(r["MARKET"]),"未知"),"交易所风险提示",r.get("LINK_URL"))
    for r in report("RPT_APP_UNUSUALBASIC", f"(UNUSUAL_TYPE=\"002\")(PREDICT_END_DATE>='{day}')", "END_DATE,SECURITY_CODE"):
        add(r["SECURITY_CODE"],r["SECURITY_NAME_ABBR"],r["PREDICT_START_DATE"][:10],r["PREDICT_END_DATE"][:10],
            r["MRAKET_TYPE"],"严重异常波动",f"https://data.eastmoney.com/notices/detail/{r['SECURITY_CODE']}/{r['INFO_CODE']}.html" if r.get("INFO_CODE") else None)
    return dict(day=day.isoformat(), predictions=predictions, repeated=repeated, history=history, pool=pool)
