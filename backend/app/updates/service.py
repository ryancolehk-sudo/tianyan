import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from app.auth import APIError
from app.config import settings
from app.evidence.common import canonical
from app.system_settings.models import MaintenanceJob
from app.system_settings.service import audit, connected, create_job, get_system_state, get_update_state, job_dto
from app.updates import schemas as s
from app.updates.manifest import UpdateFailure, verified, version_tuple
from app.updates.transport import destination, fetch
from app.version import APP_VERSION, SCHEMA_REVISION


def configured():
    return bool(settings().update_feed_url and settings().update_public_key)


def release(state):
    if not state.manifest or not state.signature or not configured():
        raise APIError(409, 'release_unavailable', '请先检查并选择可用的官方更新。')
    try:
        return verified(state.manifest, state.signature, settings().update_public_key)
    except UpdateFailure as failure:
        raise APIError(409, failure.code, failure.message) from None


def unsupported(manifest, system):
    if not manifest.automatic_supported:
        return manifest.manual_guide or '这个版本需要按迁移指引手动升级。'
    if SCHEMA_REVISION not in manifest.database_schema.from_revisions:
        return '当前数据库不在此版本支持的直接升级范围内，请按迁移指引操作。'
    if settings().update_installation != 'managed':
        return '当前为源码安装，请先保存本地修改并按手动升级指引操作。'
    if not connected(system.updater_heartbeat_at):
        return '本机升级器未连接；启动升级器后才能安装。'
    if not connected(system.backup_heartbeat_at):
        return '本机备份服务未连接，暂时无法完成升级前备份。'
    return None


def status(db):
    state, system = get_update_state(db), get_system_state(db)
    is_configured = configured()
    item, message, phase = None, state.message, state.status
    if not is_configured:
        phase, message = 'unconfigured', '更新源未配置，请等待提供官方更新地址与校验公钥。'
    elif state.manifest:
        try:
            manifest = release(state)
            reason = unsupported(manifest, system)
            phase = 'manual' if reason and state.status == 'available' else state.status
            item = s.Release(id=manifest.id, version=manifest.version, published_at=manifest.published_at,
                notes=manifest.notes, channel=manifest.channel, automatic_supported=reason is None,
                reason=reason, downloaded=bool(state.downloaded_at and state.package_name), size_bytes=manifest.size_bytes)
        except APIError as failure:
            phase, message = 'error', failure.message
    def recent(kind):
        row = db.scalar(select(MaintenanceJob).where(MaintenanceJob.kind == kind)
            .order_by(MaintenanceJob.created_at.desc(), MaintenanceJob.id.desc()).limit(1))
        return job_dto(row) if row else None
    return s.Updates(configured=is_configured, state=phase, message=message, checked_at=state.checked_at,
        current_version=APP_VERSION, release=item, download=recent('update.download'), install=recent('update.install'))


def check(db, *, owner=None, request_id=None, automatic=False):
    state = get_update_state(db)
    if not configured():
        db.commit()
        return status(db)
    now = datetime.now(UTC)
    # A committed reservation prevents every page/worker from querying the feed.
    db.refresh(state, with_for_update=True)
    spacing = timedelta(hours=24) if automatic else timedelta(seconds=60)
    if state.checked_at and state.checked_at > now - spacing:
        db.commit()
        return status(db)
    state.checked_at, state.status, state.message = now, 'checking', '正在检查官方更新说明。'
    db.commit()
    outcome = 'succeeded'
    try:
        raw = fetch(settings().update_feed_url, feed=settings().update_feed_url,
                    allowed_hosts=settings().update_allowed_hosts, limit=256 * 1024)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.keys() != {'manifest', 'signature'}:
            raise ValueError()
        manifest = verified(envelope['manifest'], envelope['signature'], settings().update_public_key)
        destination(manifest.package_url, settings().update_feed_url, settings().update_allowed_hosts)
        if manifest.channel != settings().update_channel or manifest.published_at > now + timedelta(minutes=5):
            raise UpdateFailure('update_release_invalid', '更新频道或发布时间无效。')
        db.refresh(state)
        if state.release_id != manifest.id or state.manifest != envelope['manifest']:
            state.downloaded_at = state.package_name = None
        state.manifest, state.signature, state.release_id = envelope['manifest'], envelope['signature'], manifest.id
        state.status = 'available' if version_tuple(manifest.version) > version_tuple(APP_VERSION) else 'current'
        state.message = '发现可用新版，请查看说明并选择下载时间。' if state.status == 'available' else '官方更新源暂无更高的稳定版本。'
        state.error_code = None
    except (UpdateFailure, ValueError, TypeError, KeyError, UnicodeError) as failure:
        db.refresh(state)
        state.status, state.error_code = 'error', getattr(failure, 'code', 'update_feed_invalid')
        state.message = getattr(failure, 'message', '官方更新说明格式无效，请稍后重试。')
        outcome = 'failed'
    if owner is not None:
        audit(db, owner, 'update.check', request_id=request_id, outcome=outcome, context={'error_code': state.error_code})
    db.commit()
    return status(db)


def maybe_check(db):
    if not configured():
        return
    from app.maintenance_control import paused
    if not paused(db):
        check(db, automatic=True)
        db.rollback()


def queue_download(db, owner, body, key, request_id):
    system = get_system_state(db)
    if not settings().maintenance_enabled or not connected(system.backup_heartbeat_at):
        raise APIError(503, 'maintenance_worker_unavailable', '本机维护服务未连接，请启动后再下载更新。')
    state = get_update_state(db)
    manifest = release(state)
    if manifest.id != body.release_id or version_tuple(manifest.version) <= version_tuple(APP_VERSION):
        raise APIError(409, 'release_changed', '版本选择已变化，请重新检查更新。')
    return create_job(db, owner, 'update.download', {'release_id': manifest.id,
        'manifest': state.manifest, 'signature': state.signature}, key=key, request_id=request_id)


def package_path(name):
    if not isinstance(name, str) or len(name) != 68 or not name.endswith('.tar'):
        raise UpdateFailure('update_package_invalid', '更新包标识无效，请重新下载。')
    try:
        int(name[:-4], 16)
    except ValueError:
        raise UpdateFailure('update_package_invalid', '更新包标识无效，请重新下载。') from None
    root = Path(settings().update_directory)
    if not root.is_absolute() or root.is_symlink():
        raise UpdateFailure('update_storage_invalid', '更新保存目录配置无效。')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / name
    if path.is_symlink():
        raise UpdateFailure('update_storage_invalid', '更新保存目录不可用。')
    return path


def hash_file(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run_download(db, job):
    document, signature = job.params['manifest'], job.params['signature']
    manifest = verified(document, signature, settings().update_public_key)
    if job.params.get('release_id') != manifest.id:
        raise UpdateFailure('update_release_invalid', '下载任务的版本校验失败。')
    target = package_path(manifest.package_sha256 + '.tar')
    temporary = target.with_suffix('.' + str(job.id) + '.part')
    def stage(value, message):
        job.stage, job.message = value, message
        db.commit()
    try:
        if not target.exists() or target.stat().st_size != manifest.size_bytes or hash_file(target) != manifest.package_sha256:
            if shutil.disk_usage(target.parent).free < manifest.size_bytes + 256 * 1024**2:
                raise UpdateFailure('update_disk_full', '保存更新包的空间不足，请释放空间后重试。')
            stage('downloading', '正在下载更新包，当前程序继续运行。')
            if temporary.is_symlink():
                raise UpdateFailure('update_storage_invalid', '更新临时文件不可用。')
            with temporary.open('wb') as output:
                size = fetch(manifest.package_url, feed=settings().update_feed_url,
                    allowed_hosts=settings().update_allowed_hosts, limit=manifest.size_bytes, output=output, timeout=1800)
                output.flush()
                os.fsync(output.fileno())
            stage('verifying', '正在核对更新包签名、长度和完整性。')
            if size != manifest.size_bytes or hash_file(temporary) != manifest.package_sha256:
                raise UpdateFailure('update_package_invalid', '更新包完整性校验失败，请重新下载。')
            os.replace(temporary, target)
        state = get_update_state(db)
        db.refresh(state, with_for_update=True)
        if state.release_id == manifest.id and state.manifest == document:
            state.package_name, state.downloaded_at = target.name, datetime.now(UTC)
        audit(db, job.owner_id, 'update.download', context={'release_id': manifest.id, 'version': manifest.version})
        db.commit()
        return {'release_id': manifest.id, 'version': manifest.version, 'size_bytes': manifest.size_bytes}
    finally:
        temporary.unlink(missing_ok=True)


def queue_install(db, owner, body, key, request_id):
    state, system = get_update_state(db), get_system_state(db)
    manifest = release(state)
    reason = unsupported(manifest, system)
    if reason:
        raise APIError(409, 'manual_upgrade_required', reason)
    if manifest.id != body.release_id or version_tuple(manifest.version) <= version_tuple(APP_VERSION):
        raise APIError(409, 'release_changed', '可安装版本已变化，请重新检查。')
    if not state.downloaded_at or state.package_name != manifest.package_sha256 + '.tar':
        raise APIError(409, 'download_required', '请先下载并校验更新包。')
    return create_job(db, owner, 'update.install', {'release_id': manifest.id, 'manifest': state.manifest,
        'signature': state.signature, 'package_name': state.package_name, 'confirmed': True, 'drafts_saved': True}, key=key, request_id=request_id)
