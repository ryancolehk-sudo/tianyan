"""Short page-spacing waits never skip the durable supplier permit."""
import pytest
from app.market import budget
from app.market.protocol import SourceFailure


def test_page_set_rechecks_shared_permit_after_short_clock_wait(monkeypatch):
    calls=[];sleeps=[]
    def permit(job,**kwargs):
        calls.append(kwargs['wait_for_spacing'])
        if len(calls)==1:
            raise SourceFailure('source_spacing_wait',retry_after=2)
        return True
    monkeypatch.setattr(budget,'reserve_tushare_request',permit)
    monkeypatch.setattr('time.sleep',lambda seconds:sleeps.append(seconds))
    assert budget.reserve_tushare_batch_request(object()) is True
    assert calls==[True,True] and sleeps==[2.05]


@pytest.mark.parametrize('reason',['rate_limited','configuration_changed','lease_lost'])
def test_page_wait_never_retries_permission_or_quota_failures(monkeypatch,reason):
    calls=[]
    def permit(job,**kwargs):
        calls.append(kwargs)
        raise SourceFailure(reason,retry_after=60)
    monkeypatch.setattr(budget,'reserve_tushare_request',permit)
    monkeypatch.setattr('time.sleep',lambda _:pytest.fail('must not wait through a supplier or permission failure'))
    with pytest.raises(SourceFailure,match=reason):
        budget.reserve_tushare_batch_request(object())
    assert len(calls)==1


def test_persistent_clock_wait_is_bounded_and_returns_to_durable_queue(monkeypatch):
    calls=[]
    def permit(job,**kwargs):
        waiting=kwargs.get('wait_for_spacing',False);calls.append(waiting)
        if waiting:
            raise SourceFailure('source_spacing_wait',retry_after=1)
        return False
    monkeypatch.setattr(budget,'reserve_tushare_request',permit)
    monkeypatch.setattr('time.sleep',lambda _:None)
    assert budget.reserve_tushare_batch_request(object()) is False
    assert calls==[True]*6+[False]
