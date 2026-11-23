from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from app.auth import csrf, principal, require_role
from app.db import db_session
from app.evidence.api import business_key
from app.evidence.common import write_once
from app.http import envelope
from app.schemas import Envelope
from app.system_settings.api import READ_ERRORS, WRITE_ERRORS
from app.system_settings.schemas import MaintenanceJob
from app.system_settings.service import job_dto
from app.updates import schemas as s, service

router = APIRouter()


@router.get('/api/v1/system/updates', response_model=Envelope[s.Updates], responses=READ_ERRORS, operation_id='getSystemUpdates')
def get_updates(request: Request, identity=Depends(principal), db: Session = Depends(db_session)):
    require_role(identity[0], 'admin')
    result = service.status(db)
    db.commit()
    return envelope(request, result)


@router.post('/api/v1/system/updates/check', response_model=Envelope[s.Updates], responses=WRITE_ERRORS, operation_id='checkSystemUpdates')
def check_updates(request: Request, identity=Depends(csrf), key=Depends(business_key), db: Session = Depends(db_session)):
    require_role(identity[0], 'admin')
    # The check is a bounded idempotent read of the official feed; do not retain
    # a stale 24h response in write_once when another worker has a newer result.
    return envelope(request, service.check(db, owner=identity[0], request_id=request.state.request_id))


@router.post('/api/v1/system/updates/download', response_model=Envelope[MaintenanceJob], status_code=202, responses=WRITE_ERRORS, operation_id='downloadSystemUpdate')
def download_update(body: s.DownloadInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def create(db):
        return job_dto(service.queue_download(db, identity[0], body, key, request.state.request_id)), 'complete', []
    return write_once(request, identity[0], key, body, create, status=202)


@router.post('/api/v1/system/updates/install', response_model=Envelope[MaintenanceJob], status_code=202, responses=WRITE_ERRORS, operation_id='installSystemUpdate')
def install_update(body: s.InstallInput, request: Request, identity=Depends(csrf), key=Depends(business_key)):
    require_role(identity[0], 'admin')
    def create(db):
        return job_dto(service.queue_install(db, identity[0], body, key, request.state.request_id)), 'complete', []
    return write_once(request, identity[0], key, body, create, status=202)
