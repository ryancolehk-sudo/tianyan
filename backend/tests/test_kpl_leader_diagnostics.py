"""Retain supplier error evidence without copying their messages or stock rows."""
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.evidence.models import EvidenceVersion
from app.market import direction_sources, kpl_count_jobs as details
from app.market.kpl_leaders import DATASET, parse
from app.market.models import DataFetchRun, SourceSettings
from app.market.protocol import Response, SourceFailure
from app.models import Job
from test_direction_archive import DAY
from test_kpl_detail_cadence import setup, take


@pytest.mark.parametrize('code',[1020,'1020'])
def test_numeric_supplier_parameter_error_retains_only_safe_shape(code):
    body=json.dumps(dict(errcode=code,errmsg='参数出错',private_extra='PRIVATE-DIAGNOSTIC')).encode()
    response=Response(body,datetime.now(UTC),1,200)
    with pytest.raises(SourceFailure) as failed:parse(response,'801000',DAY)
    assert failed.value.reason=='upstream_unavailable'
    diagnostic=failed.value.direction_diagnostic
    assert diagnostic['source_error_code']=='1020' and diagnostic['message_category']=='parameter_error'
    assert diagnostic['body_sha256']==hashlib.sha256(body).hexdigest()
    assert diagnostic['checkpoint']=='source_rejected_or_empty'
    assert diagnostic['other_field_count']==1 and 'PRIVATE' not in json.dumps(diagnostic)
    assert '参数出错' not in json.dumps(diagnostic,ensure_ascii=False)


@pytest.mark.parametrize('body',[
    b'{"errcode":"PRIVATE-CODE","errmsg":"PRIVATE-MESSAGE","list":[["PRIVATE-STOCK"]]}',
    b'PRIVATE-invalid-json',
    b'{"errcode":0,"errcode":"PRIVATE-DUPLICATE"}',
])
def test_rejected_supplier_content_never_enters_diagnostics(body):
    response=Response(body,datetime.now(UTC),1,200)
    with pytest.raises(SourceFailure) as failed:parse(response,'801000',DAY)
    diagnostic=failed.value.direction_diagnostic
    assert failed.value.reason in {'upstream_unavailable','schema_changed'}
    assert len(diagnostic['body_sha256'])==64
    assert 'PRIVATE' not in json.dumps(diagnostic)
    assert 'source_error_code' not in diagnostic and 'message_category' not in diagnostic


def test_real_packet_records_each_failed_board_and_keeps_success_and_receipts(client,monkeypatch):
    ident,clock,reserved,calls=setup(client,monkeypatch,3)
    fetch=direction_sources.fetch
    rejected={
        '801000':json.dumps(dict(errcode=1020,errmsg='参数出错',private_extra='PRIVATE-BODY')).encode(),
        '801001':b'PRIVATE-invalid-json',
    }
    def upstream(family,**kwargs):
        response=fetch(family,**kwargs)
        return Response(rejected[kwargs['plate_code']],response.received_at,7,200) if kwargs['plate_code'] in rejected else response
    monkeypatch.setattr(direction_sources,'fetch',upstream)
    details.schedule(leaders=True);task=take('kpl_leaders');details.process_job(task)
    with Session(engine()) as db:
        saved=db.get(Job,task.id)
        assert saved.state=='failed' and saved.error_code=='leader_packet_partial'
        assert saved.result_ref['leader_failed']==['801000','801001']
        assert saved.result_ref['leader_completed']==['801002']
        diagnostics=saved.result_ref['leader_diagnostics']
        assert [row['plate_code'] for row in diagnostics]==['801000','801001']
        assert diagnostics[0]['source_error_code']=='1020' and diagnostics[1]['checkpoint']=='invalid_json'
        assert 'PRIVATE' not in json.dumps(saved.result_ref)
        runs=list(db.scalars(select(DataFetchRun).where(DataFetchRun.job_id==task.id)))
        assert len(runs)==3 and sum(row.request_count for row in runs)==len(calls)==len(reserved)==3
        assert sorted(row.status for row in runs)==['complete','error','error']
        assert {row.reason for row in runs if row.status=='error'}=={'schema_changed','upstream_unavailable'}
        assert all(row.body_hash for row in runs)
        assert len(list(db.scalars(select(EvidenceVersion).where(EvidenceVersion.payload['dataset'].astext==DATASET))))==1
        assert db.get(SourceSettings,ident).paused_reason is None


def test_yield_and_resume_preserve_prior_error_evidence_without_another_request(client,monkeypatch):
    ident,clock,reserved,calls=setup(client,monkeypatch,3)
    fetch=direction_sources.fetch
    def upstream(family,**kwargs):
        response=fetch(family,**kwargs)
        if kwargs['plate_code']=='801000':
            return Response(b'{"errcode":1020,"errmsg":"PRIVATE-MESSAGE"}',response.received_at,7,200)
        return response
    monkeypatch.setattr(direction_sources,'fetch',upstream)
    details.schedule(leaders=True);task=take('kpl_leaders')
    with Session(engine()) as db,db.begin():db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=20)
    details.process_job(task)
    with Session(engine()) as db:
        saved=db.get(Job,task.id)
        before=saved.result_ref['leader_diagnostics']
        assert saved.state=='queued' and saved.error_code=='live_collection_wait'
        assert len(before)==1 and len(calls)==1
    with Session(engine()) as db,db.begin():db.get(SourceSettings,ident).next_run_at=clock[0]+timedelta(seconds=60)
    details.process_job(take('kpl_leaders'))
    with Session(engine()) as db:
        saved=db.get(Job,task.id)
        assert saved.state=='failed' and saved.error_code=='leader_packet_partial'
        assert saved.result_ref['leader_diagnostics']==before
        assert saved.result_ref['leader_completed']==['801001','801002']
        assert saved.result_ref['leader_failed']==['801000']
        assert 'PRIVATE' not in json.dumps(saved.result_ref)
    assert len(calls)==len(reserved)==3 and calls.count(('leader','801000'))==1
