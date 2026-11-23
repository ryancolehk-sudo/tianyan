"""Projection tests use synthetic evidence and never open a database."""
from datetime import date, datetime
from unittest.mock import Mock

import pytest
from app.market import sector_atlas as atlas


@pytest.fixture(autouse=True)
def isolated_database():
    yield


def test_source_identity_cutoff_and_reverse_calendar(monkeypatch):
    days = [date(2026, 10, 30), date(2026, 11, 2)]
    cutoff = datetime.fromisoformat('2026-11-02T15:00:00+08:00')
    context = Mock(return_value=({'calendar_state': 'complete'}, days, days[-1]))
    snapshot = Mock(side_effect=lambda db, family, dataset, label, day, time, **kw:
                    {'requested_day': str(day), 'rows': [{'code': 'K1', 'strength': '0'}]})
    monkeypatch.setattr(atlas, '_context', context)
    monkeypatch.setattr(atlas, 'snapshot_panel', snapshot)
    monkeypatch.setattr(atlas, '_tables', lambda *args, **kw: {})
    monkeypatch.setattr(atlas, '_leaders', lambda *args: {})
    db = object()
    result = atlas.sector_atlas(db, days[-1], 10, 'kpl', cutoff)
    context.assert_called_once_with(db, days[-1], 10, cutoff)
    assert [p['requested_day'] for p in result['days']] == ['2026-11-02', '2026-10-30']
    assert result['days'][0]['rows'][0]['strength'] == '0'
    for call in snapshot.call_args_list:
        assert call.args[1:3] == ('direction_kpl_strength', 'kpl_strength')
        assert call.args[-1] == cutoff and call.kwargs == {'strength': True}


def test_ths_never_borrows_stock_popularity_or_kpl(monkeypatch):
    day = date(2026, 11, 2)
    monkeypatch.setattr(atlas, '_context', lambda *args: ({}, [day], day))
    read = Mock(side_effect=AssertionError('must not borrow another source'))
    monkeypatch.setattr(atlas, 'snapshot_panel', read)
    monkeypatch.setattr(atlas, '_tables', lambda *args, **kw: None)
    monkeypatch.setattr(atlas, '_directory', lambda *args: None)
    result = atlas.sector_atlas(object(), day, source='ths')
    assert result['days'][0]['status'] == 'disabled'
    assert result['days'][0]['rows'] == []
    read.assert_not_called()


def test_unknown_calendar_does_not_invent_history(monkeypatch):
    monkeypatch.setattr(atlas, '_context', lambda *args: ({}, [], None))
    read = Mock(side_effect=AssertionError('must not manufacture dates'))
    monkeypatch.setattr(atlas, 'snapshot_panel', read)
    assert atlas.sector_atlas(object())['days'] == []
    read.assert_not_called()
