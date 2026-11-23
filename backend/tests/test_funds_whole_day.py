"""A complete saved trading-day curve must not lose its opening observations."""
from datetime import datetime, timedelta, UTC

from sqlalchemy.orm import Session

from app.db import engine
from test_intraday_funds import setup, save, read


def test_read_retains_every_saved_observation_in_this_day(client):
    setup(client)
    opening = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    stamps = [opening + timedelta(seconds=10 * n) for n in range(301)]
    with Session(engine()) as db, db.begin():
        for n, stamp in enumerate(stamps):
            save(db, [('BK0001', str(n))], stamp)
    result = read(stamps[-1])
    assert [point.time for point in result.points if point.sector_code == 'BK0001.DC'] == stamps
    assert result.sectors[0].main_net_cny == 300


def test_whole_day_read_still_respects_when_late_data_was_received(client):
    setup(client)
    opening = datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    cutoff = opening + timedelta(hours=1)
    with Session(engine()) as db, db.begin():
        save(db, [('BK0001', '1')], opening)
        save(db, [('BK0001', '2')], opening + timedelta(minutes=5), received=cutoff + timedelta(seconds=1))
        save(db, [('BK0002', '9')], opening - timedelta(days=1), received=cutoff)
    assert [(point.time, point.value) for point in read(cutoff).points] == [(opening, 1)]
    assert [(point.time, point.value) for point in read(cutoff + timedelta(seconds=1)).points] == [
        (opening, 1), (opening + timedelta(minutes=5), 2)]
