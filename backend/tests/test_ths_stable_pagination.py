"""A moving price rank must not change the membership of a paged fund capture."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest

from app.market import ths_funds as funds


STAMP = datetime(2026, 11, 4, 5, tzinfo=UTC)
ITEMS = [
    ('881100', 'A板块', '4.00', '0.10'),
    ('881101', 'B板块', '3.00', '0.20'),
    ('881102', 'C板块', '2.00', '0.30'),
    ('881103', 'D板块', '1.00', '0.40'),
]


def native_page(items, category, page):
    headings = ['序号', '行业', '行业指数', '涨跌幅', '流入资金(亿)',
                '流出资金(亿)', '净额(亿)', '公司家数', '领涨股', '涨跌幅', '当前价(元)']
    branch = 'thshy' if category == 'hyzjl' else 'gn'
    rows = []
    for identity, name, change, net in items:
        cells = ['1', f'<a href="http://q.10jqka.com.cn/{branch}/detail/code/{identity}/">{name}</a>',
                 '100', change + '%', '1.50', '2.00', net, '12', '测试', '2%', '10']
        rows.append('<tr>' + ''.join('<td>' + cell + '</td>' for cell in cells) + '</tr>')
    return ('<table><tr>' + ''.join('<th>' + heading + '</th>' for heading in headings)
            + '</tr>' + ''.join(rows) + f'</table><span class="page_info">{page}/2</span>').encode('gbk')


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body[:size]


class MovingRankingServer:
    def __init__(self, category, ignore_name_sort):
        self.category = category
        self.ignore_name_sort = ignore_name_sort
        self.requests = []

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        assert (url.scheme, url.hostname, timeout) == ('https', 'data.10jqka.com.cn', 10)
        parts = url.path.strip('/').split('/')
        assert parts[:2] == ['funds', self.category]
        fields = dict(zip(parts[2::2], parts[3::2]))
        index = int(fields['page'])
        assert index == len(self.requests) + 1
        self.requests.append(request.full_url)
        # Between HTTP pages C becomes the price leader and B's amount changes.
        # Price-ranked page 2 repeats B while omitting C; stable names retain C.
        items = ITEMS if index == 1 else [
            ('881102', 'C板块', '5.00', '0.30'), ITEMS[0],
            ('881101', 'B板块', '3.00', '0.25'), ITEMS[3],
        ]
        if fields['field'] == 'tradename' and not self.ignore_name_sort:
            items = sorted(items, key=lambda item: item[1], reverse=fields['order'] == 'desc')
        else:
            # This scenario ignores the requested sort and returns the default ranking.
            descending = True if self.ignore_name_sort else fields['order'] == 'desc'
            items = sorted(items, key=lambda item: float(item[2]), reverse=descending)
        return Response(native_page(items[(index - 1) * 2:index * 2], self.category, index))


@pytest.mark.parametrize('family,category', [
    ('ths_industry_funds', 'hyzjl'), ('ths_concept_funds', 'gnzjl'),
])
@pytest.mark.parametrize('ignore_name_sort', [False, True])
def test_moving_price_rank_keeps_complete_membership_or_honest_partial(
        monkeypatch, family, category, ignore_name_sort):
    server = MovingRankingServer(category, ignore_name_sort)
    monkeypatch.setattr(funds.urllib.request, 'build_opener', lambda *handlers: server)
    monkeypatch.setattr(funds.time, 'sleep', lambda seconds: None)
    permits = []

    def permit():
        permits.append(True)
        return True

    with patch.object(funds, 'datetime', wraps=datetime) as clock:
        clock.now.side_effect = [STAMP, STAMP + timedelta(seconds=1), STAMP + timedelta(seconds=2)]
        response, batch = funds.fetch(family, permit)
    rows = batch.records[0][1].rows
    by_code = {row['code']: row for row in rows}
    assert len(server.requests) == len(permits) == response.request_count == 2
    assert batch.records[0][1].observed_day is None
    assert response.received_at == STAMP + timedelta(seconds=2)
    assert all('source_time' not in row for row in rows)
    assert all(row['missing_pages'] == '' and row['rejected_rows'] == '0' for row in rows)
    assert by_code['881100']['received_at'] == STAMP.isoformat()
    assert by_code['881103']['received_at'] == (STAMP + timedelta(seconds=1)).isoformat()
    if ignore_name_sort:
        assert set(by_code) == {'881100', '881101', '881103'}
        assert all(row['complete'] == '0' and row['cross_page_duplicates'] == '1' for row in rows)
        assert by_code['881101']['net_cny'] == '25000000.00'
        assert by_code['881101']['received_at'] == (STAMP + timedelta(seconds=1)).isoformat()
        assert batch.rejected == 1
    else:
        assert set(by_code) == {item[0] for item in ITEMS}
        assert all(row['complete'] == '1' and row['cross_page_duplicates'] == '0' for row in rows)
        assert by_code['881101']['net_cny'] == '20000000.00'
        assert by_code['881101']['received_at'] == STAMP.isoformat()
        assert by_code['881102']['received_at'] == (STAMP + timedelta(seconds=1)).isoformat()
        assert by_code['881102']['pct_change'] == '5.00'
        assert batch.rejected == 0
