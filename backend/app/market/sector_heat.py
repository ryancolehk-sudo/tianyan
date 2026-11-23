"""Bounded supplier attention rankings; no inferred market or quote timestamps."""
import json
import re
from datetime import datetime

from app.evidence.schemas import ReferencePayload
from app.market.protocol import MAX_BODY, SourceFailure
from app.market.reference import ReferenceBatch

URL = 'https://api.ashareapi.com/v1/hot?type=sector&limit=30'
DATASET = 'market.ashare.sector_heat'


def parse_sector_heat(body, received_at):
    try:
        if received_at.utcoffset() is None or len(body) > MAX_BODY:
            raise ValueError('invalid envelope')
        value = json.loads(body)
        if not isinstance(value, dict) or value.get('endpoint') != 'hot' or value.get('tier') != 'free':
            raise ValueError('unexpected endpoint or tier')
        if value.get('ok') is not True:
            raise SourceFailure('upstream_unavailable')
        source = value.get('source')
        if not isinstance(source, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,60}', source):
            raise ValueError('missing attribution')
        data = value.get('data')
        if not isinstance(data, list) or len(data) > 30:
            raise ValueError('rank budget')
        rows, symbols, ranks = [], set(), set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError('invalid row')
            symbol, kind = item.get('symbol'), item.get('stock_type')
            if (not isinstance(symbol, str) or not re.fullmatch(r'(?:pt(?:\d{8}|\d{2}GN\d{4})|us\d{8})', symbol)
                    or kind not in {'BK', 'BK-HY-1', 'BK-HY-2'}):
                raise ValueError('unverified sector identity')
            name, rank, delta, updated = (item.get(key) for key in ['name', 'rank', 'rankdelta', 'date'])
            if (not isinstance(name, str) or not name.strip() or len(name) > 100
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or '<' in name or '>' in name):
                raise ValueError('invalid source name')
            if not isinstance(rank, str) or not re.fullmatch(r'(?:[1-9]|[1-4]\d|50)', rank):
                raise ValueError('invalid rank')
            if delta is not None and (not isinstance(delta, str) or not re.fullmatch(r'-?(?:0|[1-9]\d{0,5})', delta)):
                raise ValueError('invalid rank change')
            if updated is not None:
                if not isinstance(updated, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', updated):
                    raise ValueError('invalid supplier update text')
                datetime.strptime(updated, '%Y-%m-%d %H:%M:%S')
            if symbol in symbols or rank in ranks:
                raise ValueError('duplicate identity or rank')
            symbols.add(symbol); ranks.add(rank)
            rows.append({'rank': rank, 'symbol': symbol, 'name': name, 'source_type': kind,
                         'rank_delta': delta, 'ranking_updated_raw': updated})
        rows.sort(key=lambda row: int(row['rank']))
        payload = ReferencePayload(dataset=DATASET, title='供应商热门板块关注排名', source_url=URL,
            attribution='AShareAPI / ' + source, observed_day=None,
            scope='最多30条供应商关注排名，不是涨幅或资金排名，非全市场行业/概念/地域行情。保留pt/us混合命名空间与原始分类，不映射为本地标的或历史成分。榜单更新时间为源原文、时区未注明，不作为行情时点；价格/成交额/资金流与强度未核实，不填补。仅本机个人研究，禁止原样转售或向第三方建库。',
            columns=[{'key': 'rank', 'label': '供应商关注排名'}, {'key': 'name', 'label': '板块原名'},
                     {'key': 'symbol', 'label': '供应商代码'}, {'key': 'source_type', 'label': '供应商原始分类'},
                     {'key': 'rank_delta', 'label': '排名变化原值'},
                     {'key': 'ranking_updated_raw', 'label': '榜单更新时间原文（时区未明）'}], rows=rows)
        return ReferenceBatch(records=[(DATASET, payload, None)], expected=1, requests=1)
    except SourceFailure:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        raise SourceFailure('schema_changed') from None
