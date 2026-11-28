from types import SimpleNamespace

from app import config
from app.market import review_seat_labels
from test_data_sources import login


def test_seat_labels_read_is_authenticated_and_explicitly_enabled(client, monkeypatch):
    path = '/api/v1/review-data/seat-labels'
    params = {'trade_day': '2026-11-24', 'codes': '000001,600001,000001'}
    assert client.get(path, params=params).status_code == 401
    login(client)
    calls = []

    def lookup(day, codes):
        calls.append((day.isoformat(), codes))
        return {'day': day.isoformat(), 'rows': [], 'kpl_status': '0'}

    monkeypatch.setattr(review_seat_labels, 'review_seat_labels', lookup)
    assert client.get(path, params=params).json()['data']['kpl_status'] == 'unavailable'
    assert not calls

    from app.market import review_store
    monkeypatch.setattr(review_store, 'permitted_provider', lambda *args: True)
    monkeypatch.setattr(config, 'settings', lambda: SimpleNamespace(
        deployment_mode='local', app_mode='real', data_collection_enabled=True, seat_label_lookup_enabled=True))
    assert client.get(path, params=params).json()['data']['kpl_status'] == '0'
    assert calls == [('2026-11-24', ('000001', '600001'))]
    assert client.get(path, params={**params, 'codes': 'https://example.test'}).status_code == 422
    assert client.get(path, params={**params, 'trade_day': '2099-01-01'}).status_code == 422


def test_repair_routes_are_admin_only(client):
    headers = {**login(client, role='viewer'), 'Idempotency-Key': 'coverage-role-test'}
    assert client.get('/api/v1/data-center/coverage-repair').status_code == 403
    assert client.post('/api/v1/data-center/routes/configure', headers=headers, json={'expected_revision': 1}).status_code == 403
