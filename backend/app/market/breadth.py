"""Supplier-reported breadth; absent source dates never become today's facts."""
import json
import re

from app.evidence.schemas import ReferencePayload
from app.market.protocol import SourceFailure
from app.market.reference import ReferenceBatch

URL = 'https://api.ashareapi.com/v1/changedist'
DATASET = 'market.ashare.breadth'
FIELDS = {'advancing': '上涨', 'declining': '下跌', 'unchanged': '平盘', 'suspended': '停牌', 'limit_up': '涨停', 'limit_down': '跌停'}
BANDS = {'涨': {'涨停', '>7%', '7~5%', '5~2%', '2~0%'}, '平': {'平'}, '跌': {'0~2%', '2~5%', '5~7%', '7%<', '跌停'}}


def count(value):
    if value in {None, '', '—', '--'}:
        return None
    if type(value) not in {str, int} or not re.fullmatch(r'(?:0|[1-9]\d{0,5})', str(value)):
        raise ValueError('invalid supplier count')
    return str(value)


def parse_breadth(body, received_at):
    try:
        if received_at.utcoffset() is None:
            raise ValueError('unaware receipt')
        value = json.loads(body)
        if not isinstance(value, dict) or value.get('endpoint') != 'changedist' or value.get('tier') != 'free':
            raise ValueError('unexpected endpoint/tier')
        if value.get('ok') is not True:
            raise SourceFailure('upstream_unavailable')
        if not isinstance(value.get('data'), str) or value['data'].splitlines()[0] != '### 市场涨跌分布（沪深A股）':
            raise ValueError('unverified market scope')
        tables = value.get('tables')
        summary = value.get('structured')
        if not isinstance(tables, list) or len(tables) != 2 or not isinstance(summary, list) or len(summary) != 1 or tables[0] != summary:
            raise ValueError('unexpected summary tables')
        if not isinstance(summary[0], dict) or not isinstance(tables[1], list) or len(tables[1]) > 20:
            raise ValueError('unexpected rows')
        row = {key: count(summary[0].get(label)) for key, label in FIELDS.items()}
        if all(item is None for item in row.values()):
            raise SourceFailure('instrument_unavailable')
        share = summary[0].get('上涨占比')
        if share not in {None, '', '—', '--'} and (not isinstance(share, str) or not re.fullmatch(r'(?:\d{1,2}(?:\.\d{1,3})?|100(?:\.0{1,3})?)%', share)):
            raise ValueError('invalid reported share')
        row['advancing_share'] = share if share not in {'', '—', '--'} else None
        for limit, total in [('limit_up', 'advancing'), ('limit_down', 'declining')]:
            if row[limit] is not None and row[total] is not None and int(row[limit]) > int(row[total]):
                raise ValueError('inconsistent limit count')
        bins, seen = [], set()
        for item in tables[1]:
            if not isinstance(item, dict) or set(item) != {'区间', '家数', '方向'}:
                raise ValueError('invalid distribution row')
            band, side = item['区间'], item['方向']
            if side not in BANDS or band not in BANDS[side] or (band, side) in seen:
                raise ValueError('unknown or repeated distribution band')
            seen.add((band, side))
            bins.append({'band': band, 'count': count(item['家数']), 'direction': side})
        source = value.get('source')
        if not isinstance(source, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,60}', source):
            raise ValueError('missing attribution')
        scope = '供应商标称沪深A股；未提供行情日期、时分秒及完整分母，不能核验全市场覆盖或实时性。不是本地规则结果；炸板、晋级、溢价及成交额未知，不补零。'
        shared = {'source_url': URL, 'attribution': 'AShareAPI / ' + source, 'scope': scope, 'observed_day': None}
        headline = ReferencePayload(dataset=DATASET, title='供应商涨跌家数参考', columns=[
            *[{'key': key, 'label': label + '（家）'} for key, label in FIELDS.items()],
            {'key': 'advancing_share', 'label': '供应商上涨占比（%）'}], rows=[row], **shared)
        histogram = ReferencePayload(dataset='market.ashare.distribution', title='供应商涨跌幅区间分布', columns=[
            {'key': 'band', 'label': '供应商区间'}, {'key': 'count', 'label': '家数'}, {'key': 'direction', 'label': '方向'}], rows=bins, **shared)
        return ReferenceBatch(records=[(headline.dataset, headline, None), (histogram.dataset, histogram, None)], expected=2, requests=1)
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
        raise SourceFailure('schema_changed') from None
