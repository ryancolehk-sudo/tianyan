from datetime import date

from app.market.market_radar import summarize
from tests.test_ai_backend import login


def test_radar_preserves_missing_prior_and_uses_known_quotes():
    sample = {
        '600001.SH': {'ts_code': '600001.SH', 'pct_chg': '10', 'close': '11', 'pre_close': '10', 'amount': '123'},
        '000001.SZ': {'ts_code': '000001.SZ', 'pct_chg': '-5', 'close': '9.5', 'pre_close': '10', 'amount': '456'},
    }
    pool = {'600001.SH': {'limit': 'U'}, '600002.SH': {'limit': 'U'}}
    point = summarize(date(2026, 11, 24), '14:54', sample, pool, 2, 4, '鍚堟垚')
    assert point['raw'] == [50.0, 579000.0, 50.0, None, 50.0]
    assert point['cohort_n'] == 2
    pool.pop('600002.SH')
    assert summarize(date(2026, 11, 24), '14:54', sample, pool, 2, 4, '鍚堟垚')['raw'][3] == 10.0


def test_radar_and_news_auth_contract(client):
    paths = {
        '/api/v1/market-status?trade_day=2026-11-24': ('current', 'history'),
        '/api/v1/market-radar/minute?sample_at=2026-11-24T14%3A54%3A00%2B08%3A00': ('current', 'history'),
        '/api/v1/news/overview': ('hotspots', 'industries', 'concepts', 'sources'),
        '/api/v1/news/topic-stream': ('items', 'next_cursor'),
        '/api/v1/news/topic-picks?day=2026-11-24': ('edition',),
    }
    for path in paths:
        assert client.get(path).status_code == 401
    login(client)
    for path, keys in paths.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert set(keys) <= response.json()['data'].keys(), (path, response.json()['data'].keys())
    earlier = client.get('/api/v1/market-status', params={
        'trade_day': '2026-11-24', 'as_of_time': '2026-11-23T14:54:00+08:00',
    })
    assert earlier.status_code == 422
