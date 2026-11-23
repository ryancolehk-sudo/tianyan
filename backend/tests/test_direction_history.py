"""Dated archive adapter only; all responses synthetic and network forbidden."""
import json
from datetime import date, datetime
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.market import direction_history as history, direction_sources as sources
from app.market.protocol import Response, SourceFailure
from test_direction_sources import strength_fixture, install_opener, Reply

CLOCK = datetime.fromisoformat('2026-10-26T17:00:00+08:00')
DAY = date(2026, 10, 26)


@pytest.fixture(autouse=True)
def isolated_database():
    yield


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail('History adapter tests must not contact any upstream')
    monkeypatch.setattr(sources.urllib.request, 'build_opener', refused)


def response(day=DAY):
    data = strength_fixture()
    data['Day'] = [str(day)]
    return Response(json.dumps(data).encode(), CLOCK, 1, 200)


@pytest.mark.parametrize('day,clock,reason', [
    (DAY, CLOCK.replace(hour=14, minute=59), 'historical_day_not_closed'),
    (DAY, CLOCK.replace(hour=15, minute=4), 'historical_day_not_closed'),
    (date(2026, 10, 27), CLOCK, 'invalid_request_day'),
    (date(2025, 10, 25), CLOCK, 'invalid_request_day'),
    ('2026-10-26', CLOCK, 'invalid_request_day'),
    (DAY, CLOCK.replace(tzinfo=None), 'invalid_request_day'),
])
def test_open_future_invalid_and_unbounded_days_never_contact_source(monkeypatch, day, clock, reason):
    fetch = Mock()
    monkeypatch.setattr(sources, 'fetch', fetch)
    with pytest.raises(SourceFailure, match=reason):
        history.fetch_history(day, clock=clock)
    fetch.assert_not_called()


def test_closed_date_has_one_historical_request_and_retains_original_receipt(monkeypatch):
    raw = response()
    fetch = Mock(return_value=raw)
    monkeypatch.setattr(sources, 'fetch', fetch)
    result = history.fetch_history(DAY, clock=CLOCK)
    fetch.assert_called_once_with(history.FAMILY, day=DAY)
    assert result.response is raw and result.requested_day == DAY
    payload = result.batch.records[0][1]
    assert payload.observed_day == DAY and payload.source_url == sources.KPL_HISTORY_URL
    assert payload.rows[0]['strength'] == '9999'
    assert all(row['source_time'] is None for row in payload.rows)


def test_archive_transport_cannot_switch_to_current_endpoint_or_another_ranking(monkeypatch):
    opener = install_opener(monkeypatch, Reply(response().body))
    history.fetch_history(DAY, clock=CLOCK)
    assert len(opener.calls) == 1
    request, _ = opener.calls[0]
    params = parse_qs(urlsplit(request.full_url).query)
    assert request.full_url.split('?')[0] == sources.KPL_HISTORY_URL
    assert request.data is None and request.get_method() == 'GET'
    assert params['Date'] == [str(DAY)] and params['Type'] == ['1']
    assert params['Order'] == ['1'] and params['Index'] == ['0']
    assert params['st'] == ['30'] and params['ZSType'] == ['7']


def test_rejected_archive_preserves_failure_receipt_and_diagnostic_without_a_live_retry(monkeypatch):
    raw = Response(b'{"errcode":1020,"errmsg":"parameter rejected"}', CLOCK, 1, 200)
    fetch = Mock(return_value=raw)
    monkeypatch.setattr(sources, 'fetch', fetch)
    with pytest.raises(SourceFailure, match='upstream_unavailable') as failure:
        history.fetch_history(DAY, clock=CLOCK)
    assert failure.value.response is raw and failure.value.request_count == 1
    assert failure.value.direction_diagnostic['source_error_code'] == '1020'
    fetch.assert_called_once_with(history.FAMILY, day=DAY)


def test_previous_source_day_is_never_relabelled_as_the_requested_archive(monkeypatch):
    monkeypatch.setattr(sources, 'fetch', Mock(return_value=response(date(2026, 10, 23))))
    with pytest.raises(SourceFailure, match='source_date_mismatch') as failure:
        history.fetch_history(DAY, clock=CLOCK)
    assert failure.value.direction_diagnostic['checkpoint'] == 'source_date_mismatch'
