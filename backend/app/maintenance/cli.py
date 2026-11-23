"""Operator-only backup/export commands using the same core as the settings UI."""
import argparse
import json
import sys
import tarfile
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.maintenance.backup import BackupFailure, location, verify_bundle
from app.maintenance.worker import run_one
from app.models import User
from app.system_settings.models import BackupRecord
from app.system_settings.service import create_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('backup')
    commands.add_parser('export').add_argument('identity', type=uuid.UUID)
    args = parser.parse_args()
    with Session(engine(), expire_on_commit=False) as db:
        if args.command == 'backup':
            admin = db.scalar(select(User).where(User.enabled.is_(True), User.role == 'admin').order_by(User.id).limit(1))
            if admin is None:
                raise BackupFailure('backup_admin_required', '本实例尚无管理员，无法记录完整本机备份。')
            job = create_job(db, admin, 'backup')
            db.commit()
            deadline = time.monotonic() + 3600
            while job.status in {'queued', 'running'}:
                run_one(db, job_id=job.id)
                db.refresh(job)
                if job.status not in {'queued', 'running'}:
                    break
                if time.monotonic() >= deadline:
                    raise BackupFailure('backup_wait_timeout', '备份仍在运行；请在系统设置查看结果，不要重复提交。')
                time.sleep(1)
            if job.status != 'succeeded':
                raise BackupFailure(job.error_code or 'backup_failed', job.message)
            record = db.scalar(select(BackupRecord).where(BackupRecord.job_id == job.id))
            print(json.dumps({'id': str(record.id), 'location_key': record.location_key,
                              'sha256': record.sha256, 'integrity_state': record.integrity_state}))
        else:
            record = db.get(BackupRecord, args.identity)
            if record is None or record.integrity_state != 'passed':
                raise BackupFailure('backup_not_found', '未找到校验通过的备份。')
            folder = location(record.location_key)
            verify_bundle(folder)
            with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
                archive.add(folder, arcname='backup', recursive=True)
                archive.add(folder.with_name(folder.name + '-master-key'), arcname='backup-master-key', recursive=True)


if __name__ == '__main__':
    try:
        main()
    except BackupFailure as failure:
        print(failure.message, file=sys.stderr)
        raise SystemExit(1) from None
