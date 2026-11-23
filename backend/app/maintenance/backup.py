"""Consistent format-2 local backup. No Docker socket, shell, or network APIs.

Credential writers share a PostgreSQL transaction gate. We take the exclusive
gate before exporting a repeatable-read snapshot and copying encrypted files.
After that copy the gate can be released: pg_dump imports the original snapshot,
so later credential rotation cannot split its DB references from the saved tar.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.ai.vault import CREDENTIAL_GATE
from app.config import settings
from app.db import engine

CHUNK = 1024 * 1024
FILES = ('database.dump', 'artifacts.tar', 'credentials.tar')


class BackupFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def backup_root():
    root = Path(getattr(settings(), 'backup_directory', os.getenv('JL_BACKUP_DIRECTORY', '/workspace/backups')))
    if not root.is_absolute() or root.is_symlink():
        raise BackupFailure('backup_directory_invalid', '本机备份目录配置无效。')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root.resolve()


def location(key):
    if not isinstance(key, str) or not key.startswith('backup-'):
        raise BackupFailure('backup_location_invalid', '备份标识无效。')
    try:
        identity = uuid.UUID(key.removeprefix('backup-'))
    except ValueError:
        raise BackupFailure('backup_location_invalid', '备份标识无效。') from None
    if key != 'backup-' + str(identity):
        raise BackupFailure('backup_location_invalid', '备份标识无效。')
    target = backup_root() / key
    if target.is_symlink():
        raise BackupFailure('backup_location_invalid', '备份目录不可用。')
    return target


def _sync(path):
    with Path(path).open('rb+' if os.name == 'nt' else 'rb') as source:
        os.fsync(source.fileno())


def _remove_staging(path):
    path = Path(path)
    if path.resolve().parent != backup_root().resolve() or not path.name.startswith('.backup-') or not path.name.endswith('.partial'):
        raise BackupFailure('backup_location_invalid', '备份临时目录不可用。')
    _regular_tree(path)
    def writable_retry(function, name, error):
        if os.name != 'nt' or not isinstance(error, PermissionError):
            raise error
        os.chmod(name, 0o700)
        function(name)
    shutil.rmtree(path, onexc=writable_retry)


def _sync_directory(path):
    # Windows does not expose POSIX directory fsync. The bundle's individual
    # files are flushed before atomic rename and verified again on restore.
    if os.name == 'nt':
        if not Path(path).is_dir():
            raise BackupFailure('backup_location_invalid', '备份目录不可用。')
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_tree(path):
    if not path.is_dir() or path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
        raise BackupFailure('backup_source_unavailable', '本机资料卷不可读取。')
    for child in path.rglob('*'):
        if child.is_symlink() or (hasattr(child,'is_junction') and child.is_junction()) or not (child.is_dir() or child.is_file()):
            raise BackupFailure('backup_source_invalid', '资料卷含不支持的文件，请检查后重新备份。')


def _pack(path, destination, name):
    _regular_tree(path)
    with tarfile.open(destination, 'w', dereference=False) as archive:
        archive.add(path, arcname=name, recursive=True)
    _sync(destination)


@contextmanager
def frozen_credentials(bind):
    """Yield snapshot connection and an idempotent early-unlock operation."""
    with bind.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        # Autocommit is deliberate: a transaction opened before waiting for the
        # gate could snapshot DB state older than the credential files we copy.
        connection.execute(text('SET statement_timeout=0'))
        connection.execute(text('SELECT pg_advisory_lock(:key)'), {'key': CREDENTIAL_GATE})
        connection.commit()
        locked = True
        def unlock():
            nonlocal locked
            if locked:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': CREDENTIAL_GATE})
                locked = False
        try:
            connection.execution_options(isolation_level='REPEATABLE READ')
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                snapshot = connection.scalar(text('SELECT pg_export_snapshot()'))
                yield connection, snapshot, unlock
        finally:
            if connection.in_transaction():
                connection.rollback()
            if locked:
                connection.execution_options(isolation_level='AUTOCOMMIT')
                unlock()
                connection.commit()


def _references(connection):
    result = set()
    for table in ('service_connections', 'chatgpt_accounts', 'chatgpt_authorizations'):
        exists = connection.scalar(text('SELECT to_regclass(:table)'), {'table': 'public.' + table})
        if exists:
            # Identifiers above are constants, never user input.
            result.update(connection.scalars(text(f"SELECT secret_ref FROM {table} WHERE secret_ref LIKE 'vault:%'")))
    return result


def _check_credentials(connection, directory, master):
    _regular_tree(directory)
    if not master.is_file() or master.is_symlink():
        raise BackupFailure('backup_master_missing', '独立主密钥不可读取，本次备份不会发布。')
    try:
        key = master.read_bytes()
        cipher = Fernet(key.strip())
        for reference in _references(connection):
            identity = uuid.UUID(reference.removeprefix('vault:'))
            path = directory / f'{identity}.key'
            if path.is_symlink() or not path.is_file():
                raise ValueError()
            cipher.decrypt(path.read_bytes())
        return key
    except (ValueError, OSError, InvalidToken):
        raise BackupFailure('backup_credentials_incomplete', '凭据与独立主密钥不配套，本次备份不会发布。') from None


def _dump(snapshot, destination, database_url):
    url = make_url(database_url)
    if url.drivername not in {'postgresql', 'postgresql+psycopg'}:
        raise BackupFailure('backup_database_invalid', '备份仅支持本机 PostgreSQL 数据库。')
    environment = dict(os.environ)
    environment.update(PGHOST=url.host or 'db', PGPORT=str(url.port or 5432),
                       PGDATABASE=url.database or 'jiulong', PGUSER=url.username or 'jlpm_maintenance',
                       PGPASSWORD=url.password or '', PGCONNECT_TIMEOUT='10',
                       PGOPTIONS='-c statement_timeout=0')
    # No credentials in command arguments, logs, manifests, or subprocess output.
    with destination.open('xb') as output:
        result = subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--snapshot', snapshot],
                                stdout=output, stderr=subprocess.DEVNULL, env=environment, check=False)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode:
        raise BackupFailure('backup_export_failed', '数据库导出未完成，未发布备份。')
    check = subprocess.run(['pg_restore', '--list', str(destination)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
    if check.returncode:
        raise BackupFailure('backup_dump_invalid', '数据库备份文件校验失败。')


def verify_bundle(folder, *, master_folder=None):
    folder = Path(folder)
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('format') != 2 or set(manifest.get('sha256', {})) != set(FILES):
            raise ValueError()
        for name in FILES:
            if (folder / name).is_symlink() or sha256(folder / name) != manifest['sha256'][name]:
                raise ValueError()
        master = Path(master_folder) if master_folder is not None else folder.with_name(folder.name + '-master-key')
        key_file = master / 'ai_master_key'
        if key_file.is_symlink() or sha256(key_file) != manifest['ai_master_sha256']:
            raise ValueError()
        for name in ('artifacts.tar', 'credentials.tar'):
            expected = name.removesuffix('.tar')
            with tarfile.open(folder / name) as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if not parts or parts[0] != expected or '..' in parts or member.name.startswith('/'):
                        raise ValueError()
                    if not (member.isdir() or member.isfile()):
                        raise ValueError()
        return manifest
    except (ValueError, OSError, KeyError, tarfile.TarError):
        raise BackupFailure('backup_integrity_failed', '备份文件校验失败，请保留原实例并重新备份。') from None


def create_backup(identity, *, progress=lambda stage, message: None, bind=None,
                  artifact_directory=None, credential_directory=None, master_file=None,
                  database_url=None):
    """Return durable metadata. Publish only complete, verified format-2 bundles."""
    from app.ai import vault
    config = settings()
    bind = bind or engine()
    identity = uuid.UUID(str(identity))
    key = 'backup-' + str(identity)
    target = location(key)
    master_target = target.with_name(target.name + '-master-key')
    if target.exists():
        manifest = verify_bundle(target)
        return bundle_metadata(target, manifest)
    work = target.with_name('.' + target.name + '.partial')
    master_work = master_target.with_name('.' + master_target.name + '.partial')
    # These deterministic private staging names belong to this job alone. A
    # replacement lease restarts an incomplete export, never an existing backup.
    for partial in (work, master_work):
        if partial.is_symlink():
            raise BackupFailure('backup_location_invalid', '备份临时目录不可用。')
        if partial.exists():
            _remove_staging(partial)
        partial.mkdir(mode=0o700)
    credential_path, master_path = vault.paths()
    credential_path = Path(credential_directory or credential_path)
    master_path = Path(master_file or master_path)
    artifacts = Path(artifact_directory or getattr(config, 'artifact_directory', '/workspace/artifacts'))
    progress('checking', '正在检查本机资料和配套凭据。')
    try:
        with frozen_credentials(bind) as (connection, snapshot, unlock):
            schema = connection.scalar(text('SELECT version_num FROM alembic_version'))
            mode = connection.scalar(text('SELECT app_mode FROM instance_state WHERE id=1'))
            if mode != config.app_mode:
                raise BackupFailure('backup_mode_mismatch', '实例模式不一致，备份已停止。')
            key = _check_credentials(connection, credential_path, master_path)
            progress('packing_credentials', '正在保存配套密文和独立主密钥。')
            _pack(credential_path, work / 'credentials.tar', 'credentials')
            key_file = master_work / 'ai_master_key'
            with key_file.open('xb') as output:
                output.write(key)
                output.flush()
                os.fsync(output.fileno())
            key_file.chmod(0o400)
            _sync_directory(master_work)
            # Everything tied to file replacement is already immutable here.
            # Keep the exported DB snapshot alive, but let OAuth writes continue.
            unlock()
            progress('exporting', '正在导出数据库一致性快照。')
            _dump(snapshot, work / 'database.dump', database_url or config.database_url)
        progress('packing', '正在打包本机制品。')
        _pack(artifacts, work / 'artifacts.tar', 'artifacts')
        progress('verifying', '正在校验备份文件；尚未进行恢复演练。')
        hashes = {name: sha256(work / name) for name in FILES}
        manifest = {'format': 2, 'job_id': str(identity), 'app_mode': mode, 'build_revision': config.build_revision,
                    'schema_revision': schema, 'database_acl': True,
                    'created_at': datetime.now(UTC).isoformat(), 'sha256': hashes,
                    'ai_master_sha256': sha256(key_file)}
        manifest_file = work / 'manifest.json'
        with manifest_file.open('x', encoding='utf-8') as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        verify_bundle(work, master_folder=master_work)
        _sync_directory(work)
        if master_target.exists():
            # A crash between these two renames left only this job's key copy.
            if master_target.is_symlink() or sha256(master_target / 'ai_master_key') != manifest['ai_master_sha256']:
                raise BackupFailure('backup_location_conflict', '备份主密钥目录已存在不匹配资料。')
            _remove_staging(master_work)
        else:
            master_work.rename(master_target)
        work.rename(target)
        _sync_directory(target.parent)
        return bundle_metadata(target, manifest)
    except BackupFailure:
        raise
    except OSError as error:
        raise BackupFailure('backup_space_insufficient' if error.errno == 28 else 'backup_io_failed',
                            '本机空间不足，备份未完成。' if error.errno == 28 else '本机文件读取或写入失败，备份未发布。') from None


def bundle_metadata(folder, manifest):
    return {'location_key': folder.name, 'name': folder.name,
            'size_bytes': sum((folder / name).stat().st_size for name in (*FILES, 'manifest.json')),
            'sha256': sha256(folder / 'manifest.json'), 'app_mode': manifest['app_mode'],
            'schema_revision': manifest['schema_revision'], 'build_revision': manifest['build_revision'],
            'integrity_state': 'passed', 'recovery_state': 'unverified'}
