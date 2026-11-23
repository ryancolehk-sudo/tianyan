"""Notifications wake a worker only after the durable transaction commits."""
from datetime import timedelta

from sqlalchemy.orm import Session

from app.db import engine
from app.read_cache_wakeup import ReadCacheWakeup
from test_read_cache import seed
from test_terminal_workbench import AT,save
from test_tushare_market import quote


def notifications(wakeup,timeout=1):
    return list(wakeup.connection.notifies(timeout=timeout,stop_after=1))


def test_cold_queue_and_committed_ingestion_wake_without_payload_or_rollback_event(client,monkeypatch):
    from app.config import settings
    seed(client,monkeypatch);monkeypatch.setattr(settings(),'read_cache_enabled',True)
    wakeup=ReadCacheWakeup()
    try:
        assert wakeup.connect()
        assert client.get('/api/v1/workbench-view?view=overview').status_code==503
        notice=notifications(wakeup)
        assert len(notice)==1 and notice[0].channel=='tianyan_read_dirty' and notice[0].payload==''
        with Session(engine()) as db:
            save(db,'tushare_market_rt',[quote('000100.SZ',price='11',at=AT+timedelta(minutes=1))],AT+timedelta(minutes=1))
            db.rollback()
        assert notifications(wakeup,.05)==[]
        with Session(engine()) as db,db.begin():
            for minute in (1,2):
                stamp=AT+timedelta(minutes=minute)
                save(db,'tushare_market_rt',[quote('000100.SZ',price=str(11+minute),at=stamp)],stamp)
        notice=notifications(wakeup)
        assert len(notice)==1 and notice[0].payload==''
        old=wakeup.connection;old.close()
        wakeup.wait()
        assert wakeup.connection is not old and not wakeup.connection.closed
    finally:wakeup.close()


def test_listener_failure_uses_bounded_polling_and_reconnects(monkeypatch,caplog):
    from app import read_cache_wakeup as module
    wakeup=ReadCacheWakeup();calls=[];sleeps=[]
    def broken():calls.append('connect');raise RuntimeError('private connection diagnostics')
    monkeypatch.setattr(wakeup,'connect',broken)
    monkeypatch.setattr(module.time,'sleep',lambda seconds:sleeps.append(seconds))
    wakeup.wait();wakeup.wait()
    assert calls==['connect'] and sleeps==[1,1]
    assert 'private connection diagnostics' not in caplog.text
    wakeup.retry_after=0
    monkeypatch.setattr(wakeup,'connect',lambda:calls.append('reconnected') or True)
    wakeup.wait()
    assert calls==['connect','reconnected']
