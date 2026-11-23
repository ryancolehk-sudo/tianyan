"""Restore the project's verified backup into a new, stopped native instance."""
import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from app.native.config import NativeConfig, atomic_json, prepare_directories, require_free_port
from app.native.database import initialize_cluster, provision_database, stop_database, wait_database
from app.native.runtime import instance_lock, start_process


IMMUTABLE = ('source_versions', 'evidence_versions', 'source_health', 'calendar_versions',
    'trading_sessions', 'snapshot_items', 'document_revisions', 'revision_links', 'publication_events',
    'plan_scenarios', 'auction_observations', 'data_fetch_runs', 'review_archives', 'validations',
    'news_editions', 'news_edition_items', 'screen_rule_versions', 'screen_runs', 'screen_events',
    'tomorrow_snapshots', 'tomorrow_reports', 'tomorrow_confirmations', 'tomorrow_validations')


def extract_tree(archive_file, root, expected):
    with tarfile.open(archive_file) as archive:
        members = archive.getmembers()
        for item in members:
            parts = item.name.split('/')
            if (not parts or parts[0] != expected or any(part in {'..', ''} for part in parts)
                    or '\\' in item.name or ':' in item.name or not (item.isfile() or item.isdir())):
                raise ValueError('native_backup_archive_invalid')
        archive.extractall(root, members=members, filter='data')


def restore_empty(config, folder):
    # A populated target is never cleared, even if the caller is an admin. To
    # recover an interrupted import the operator selects another empty data dir.
    if (config.data / 'database' / 'PG_VERSION').exists():
        raise ValueError('native_restore_requires_empty_instance')
    folder = Path(folder).resolve(strict=True)
    prepare_directories(config)
    for key, value in config.environment('maintenance').items():
        if key.startswith('JL_'):
            os.environ[key] = value
    from app.maintenance.backup import verify_bundle, sha256, _check_credentials
    from app.config import settings
    settings.cache_clear()
    manifest = verify_bundle(folder)
    if manifest['app_mode'] != config.mode:
        raise ValueError('native_backup_mode_mismatch')
    if not str(manifest.get('postgres_version', '')).startswith('17'):
        # Core backups use a PostgreSQL version string (e.g. 17.11). Never
        # silently restore a dump produced by a newer major client.
        version = str(manifest.get('postgres_major', ''))
        if version != '17':
            # Historical format-2 bundles identify their PostgreSQL version via
            # pg_dump's header instead; pg_restore verifies compatibility below.
            if 'postgres_version' in manifest or 'postgres_major' in manifest:
                raise ValueError('native_backup_postgres_major_mismatch')
    for name in ('artifacts', 'credentials'):
        if any((config.data / name).iterdir()):
            raise ValueError('native_restore_requires_empty_volumes')
    require_free_port(config.database_port)
    with instance_lock(config.data / 'control' / 'runtime.lock'):
        if (config.data / 'database' / 'PG_VERSION').exists():
            raise ValueError('native_restore_requires_empty_instance')
        initialize_cluster(config)
        database = start_process(config, 'restore-postgres', [str(config.pg_bin / 'postgres.exe'),
            '-D', str(config.data / 'database'), '-p', str(config.database_port), '-h', '127.0.0.1'])
        try:
            wait_database(config, database)
            provision_database(config, migrate=False)
            env = config.environment('admin')
            env['PGPASSWORD'] = config.private()['passwords']['admin']
            command = [str(config.pg_bin / 'pg_restore.exe'), '--host=127.0.0.1',
                '--port=' + str(config.database_port), '--username=postgres', '--dbname=jiulong',
                '--role=jlpm_migrate', '--no-owner', '--single-transaction', str(folder / 'database.dump')]
            result = subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=3600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError('native_restore_database_failed_target_preserved')
            extract_tree(folder / 'artifacts.tar', config.data, 'artifacts')
            extract_tree(folder / 'credentials.tar', config.data, 'credentials')
            master_folder = folder.with_name(folder.name + '-master-key')
            shutil.copyfile(master_folder / 'ai_master_key', config.data / 'master' / 'ai_master_key')
            # Current migration's revocations apply to append-only evidence;
            # restoring GRANTs must not reintroduce broad default privileges.
            import psycopg
            from psycopg import sql
            with psycopg.connect(host='127.0.0.1', port=config.database_port, dbname='jiulong', user='postgres',
                password=config.private()['passwords']['admin'], autocommit=True) as db:
                for table in IMMUTABLE:
                    if db.execute('SELECT to_regclass(%s)', ('public.' + table,)).fetchone()[0] is not None:
                        db.execute(sql.SQL('REVOKE UPDATE, DELETE ON TABLE {} FROM jlpm_app').format(sql.Identifier(table)))
                for table in ('instrument_origins', 'screen_rules', 'tomorrow_settings', 'tomorrow_runs'):
                    if db.execute('SELECT to_regclass(%s)', ('public.' + table,)).fetchone()[0] is not None:
                        db.execute(sql.SQL('REVOKE DELETE ON TABLE {} FROM jlpm_app').format(sql.Identifier(table)))
                db.execute('UPDATE sessions SET revoked_at=now()')
                db.execute('DELETE FROM worker_heartbeats')
                db.execute("DO $$ BEGIN IF to_regclass('public.chatgpt_installation') IS NOT NULL THEN "
                    "UPDATE chatgpt_installation SET host_id='urn:uuid:' || gen_random_uuid()::text; "
                    "UPDATE chatgpt_authorizations SET consumed_at=COALESCE(consumed_at,now()),secret_ref=NULL; "
                    "UPDATE chatgpt_accounts SET state=CASE WHEN secret_ref IS NULL THEN 'disconnected' ELSE 'reauthorization_required' END, "
                    "revision=revision+1,refresh_started_at=NULL,updated_at=now(); "
                    "UPDATE service_connections SET enabled=false,revision=revision+1,last_test=NULL,tested_revision=NULL,updated_at=now() "
                    "WHERE template='chatgpt_plan'; END IF; END $$")
                db.execute("DO $$ BEGIN IF to_regclass('public.maintenance_jobs') IS NOT NULL THEN "
                    "UPDATE maintenance_jobs SET status='cancelled',stage='restored',message='恢复到新实例，原维护任务已取消。', "
                    "lease_token=NULL,lease_until=NULL,completed_at=now() WHERE status IN ('queued','running'); "
                    "UPDATE system_settings_state SET active=false,upgrade_job_id=NULL,stage='idle',started_at=NULL, "
                    "updater_heartbeat_at=NULL,backup_heartbeat_at=NULL; "
                    "UPDATE system_update_state SET status='unchecked',message='恢复到新实例，请重新检查更新。', "
                    "checked_at=NULL,manifest=NULL,signature=NULL,downloaded_at=NULL,package_name=NULL,release_id=NULL; "
                    "END IF; END $$")
            provision_database(config)
            from sqlalchemy import create_engine
            with create_engine(config.database_url()).connect() as connection:
                _check_credentials(connection, config.data / 'credentials', config.data / 'master' / 'ai_master_key')
            atomic_json(config.data / 'restore-receipt.json', {
                'format': 1, 'status': 'passed', 'restored_at': datetime.now(UTC).isoformat(),
                'manifest_sha256': sha256(folder / 'manifest.json'),
                'checks': ['backup-hashes', 'empty-instance-only', 'postgres-restore',
                           'append-only-privileges', 'credential-decryption', 'schema-upgrade'],
                'collection_enabled': config.collection_enabled,
            })
        finally:
            if database.poll() is None:
                stop_database(config)
                database.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    args = parser.parse_args()
    restore_empty(NativeConfig.read(args.config), args.backup)
    print('native_empty_instance_restore_passed')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('native_restore_failed kind=' + type(error).__name__)
        raise SystemExit(1) from None
