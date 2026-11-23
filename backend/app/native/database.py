"""Private native PostgreSQL bootstrap; never connects to the Docker instance."""
import os
from pathlib import Path
import subprocess
import time

from app.native.config import require_free_port


def checked(command, *, env=None, cwd=None, timeout=120, input_text=None):
    result = subprocess.run(command, env=env, cwd=cwd, input=input_text, text=True, encoding='utf-8',
                            errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        # psql/Alembic diagnostics can contain a full SQL statement or DSN.
        raise RuntimeError(f'native_command_failed:{Path(command[0]).name}:{result.returncode}')
    return result.stdout


def initialized(config):
    stamp = config.data / 'database' / 'PG_VERSION'
    if stamp.exists() and stamp.read_text(encoding='ascii').strip() != '17':
        raise RuntimeError('native_postgres_major_mismatch')
    return stamp.exists()


def initialize_cluster(config):
    # PostgreSQL 17's Windows bootstrap emits paths using the system ANSI code
    # page. A non-ASCII installation/share or data path can corrupt bootstrap
    # SQL on a Chinese Windows host. The installer uses fixed ASCII paths.
    if os.name == 'nt' and any(not str(path).isascii() for path in (config.program, config.data)):
        raise RuntimeError('native_postgres_requires_ascii_installation_and_data_paths')
    if initialized(config):
        return
    folder = config.data / 'database'
    if any(folder.iterdir()):
        raise RuntimeError('native_cluster_incomplete_manual_recovery_required')
    require_free_port(config.database_port)
    password = config.data / 'private' / 'init-password.pending'
    try:
        with password.open('x', encoding='ascii') as output:
            output.write(config.private()['passwords']['admin'])
        checked([str(config.pg_bin / 'initdb.exe'), '-D', str(folder), '-U', 'postgres',
                 '--pwfile', str(password), '--auth-local=scram-sha-256', '--auth-host=scram-sha-256',
                 '--encoding=UTF8', '--locale=C'], timeout=120)
        with (folder / 'postgresql.conf').open('a', encoding='ascii') as output:
            output.write("\n# Tianyan private instance\nlisten_addresses='127.0.0.1'\n")
            output.write(f'port={config.database_port}\nmax_connections=100\n')
            output.write("timezone='UTC'\nlog_timezone='UTC'\n")
            output.write("logging_collector=off\nlog_statement='none'\n")
    finally:
        password.unlink(missing_ok=True)


def wait_database(config, process, *, seconds=60):
    import psycopg
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError('native_database_exited')
        try:
            with psycopg.connect(host='127.0.0.1', port=config.database_port, dbname='postgres',
                                 user='postgres', password=config.private()['passwords']['admin'],
                                 connect_timeout=2):
                return
        except psycopg.OperationalError:
            time.sleep(0.5)
    raise RuntimeError('native_database_start_timeout')


def provision_database(config, *, migrate=True):
    import psycopg
    from psycopg import sql
    passwords = config.private()['passwords']
    credentials = dict(host='127.0.0.1', port=config.database_port, user='postgres',
                       password=passwords['admin'], connect_timeout=5)
    with psycopg.connect(dbname='postgres', autocommit=True, **credentials) as db:
        for key in ('migrate', 'app', 'maintenance'):
            role = 'jlpm_' + key
            if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(passwords[key])))
        if not db.execute("SELECT 1 FROM pg_database WHERE datname='jiulong'").fetchone():
            db.execute('CREATE DATABASE jiulong OWNER jlpm_migrate')
    with psycopg.connect(dbname='jiulong', autocommit=True, **credentials) as db:
        db.execute('GRANT CONNECT ON DATABASE jiulong TO jlpm_app, jlpm_migrate, jlpm_maintenance')
        db.execute('ALTER SCHEMA public OWNER TO jlpm_migrate')
        db.execute('GRANT USAGE ON SCHEMA public TO jlpm_app, jlpm_maintenance')
        db.execute('ALTER DEFAULT PRIVILEGES FOR ROLE jlpm_migrate IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jlpm_app')
        db.execute('ALTER DEFAULT PRIVILEGES FOR ROLE jlpm_migrate IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO jlpm_app')
        db.execute('GRANT pg_read_all_data TO jlpm_maintenance')
    if migrate:
        checked([str(config.python), '-I', '-B', '-m', 'alembic', '-c', str(config.program / 'backend' / 'alembic.ini'),
                 'upgrade', 'head'], env=config.environment('migrate'), cwd=config.program / 'backend', timeout=300)
    with psycopg.connect(dbname='jiulong', autocommit=True, **credentials) as db:
        for table in ('maintenance_jobs', 'backup_records', 'system_settings_state', 'system_update_state'):
            if db.execute('SELECT to_regclass(%s)', ('public.' + table,)).fetchone()[0] is not None:
                db.execute(sql.SQL('GRANT INSERT, UPDATE, DELETE ON TABLE {} TO jlpm_maintenance').format(sql.Identifier(table)))
        if db.execute("SELECT to_regclass('public.audit_events')").fetchone()[0] is not None:
            db.execute('GRANT INSERT, UPDATE ON TABLE audit_events TO jlpm_maintenance')


def stop_database(config):
    if initialized(config):
        checked([str(config.pg_bin / 'pg_ctl.exe'), '-D', str(config.data / 'database'),
                 'stop', '-m', 'fast', '-w', '-t', '60'], timeout=70)
