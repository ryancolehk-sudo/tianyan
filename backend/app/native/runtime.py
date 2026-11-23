"""Native supervisor. Its lifetime is independent of every browser window."""
import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from app.native.config import NativeConfig, ROLES, atomic_json, prepare_directories, require_free_port
from app.native.database import initialize_cluster, provision_database, stop_database, wait_database
from app.native.install import native_identity


def child_commands(config):
    python = [str(config.python), '-I', '-B']
    commands = {role: [*python, '-m', 'app.worker', '--role', role] for role in ROLES}
    commands['realtime'].extend(['--slot', 'primary'])
    commands['realtime-secondary'] = [*python, '-m', 'app.worker', '--role', 'realtime', '--slot', 'secondary']
    commands.update({
        'api': [*python, '-m', 'uvicorn', 'app.native.web:create_app', '--factory', '--host', '127.0.0.1',
                '--port', str(config.http_port), '--no-access-log', '--workers', '1'],
        'read-cache': [*python, '-m', 'app.read_cache_worker'],
        'maintenance': [*python, '-m', 'app.maintenance.worker'],
        'ai': [*python, '-m', 'app.ai.worker'],
    })
    return commands


@contextmanager
def instance_lock(path):
    path = Path(path)
    handle = path.open('a+b')
    handle.seek(0)
    if path.stat().st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise RuntimeError('native_instance_already_running') from None
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('native_instance_already_running') from None
    try:
        yield
    finally:
        if os.name == 'nt':
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _pump(pipe, path, values):
    logger = logging.getLogger('native.' + path.stem)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    logger.addHandler(handler)
    try:
        while line := pipe.readline(8192):
            content = line.decode('utf-8', errors='replace').strip()
            for value in values:
                if value:
                    content = content.replace(value, '[private]')
            content = re.sub(r'postgresql(?:\+psycopg)?://[^\s]+', '[database]', content)
            logger.info(content)
    finally:
        logger.removeHandler(handler)
        handler.close()
        pipe.close()


def start_process(config, name, command, *, role='app', run_id=None):
    private = config.private()
    environment = config.environment(role)
    if run_id is not None:
        environment['JL_NATIVE_RUN_ID'] = run_id
    process = subprocess.Popen(command, cwd=config.program / 'backend', env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    threading.Thread(target=_pump, args=(process.stdout, config.data / 'logs' / f'{name}.log',
        [*private['passwords'].values(), private.get('setup_token', '')]), daemon=True).start()
    return process


def _stop_children(children):
    for process in reversed(list(children.values())):
        if process.poll() is None:
            try:
                if os.name == 'nt':
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 15
    for process in children.values():
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def run(config):
    prepare_directories(config)
    run_id, identity = uuid.uuid4().hex, native_identity(config)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if os.name == 'nt':
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    stop_file = config.data / 'control' / 'stop.request'
    stop_file.unlink(missing_ok=True)
    children, database = {}, None
    with instance_lock(config.data / 'control' / 'runtime.lock'):
        require_free_port(config.http_port)
        require_free_port(config.database_port)
        initialize_cluster(config)
        try:
            database = start_process(config, 'postgres', [str(config.pg_bin / 'postgres.exe'),
                '-D', str(config.data / 'database'), '-p', str(config.database_port), '-h', '127.0.0.1'])
            wait_database(config, database)
            provision_database(config)
            commands = child_commands(config)
            retry_at = {name: 0.0 for name in commands}
            failures = {name: 0 for name in commands}
            started_at = {}
            while not stop.is_set() and not stop_file.exists():
                if database.poll() is not None:
                    # Restarting the supervisor restarts the entire dependent
                    # process group; leases recover against the original DB.
                    raise RuntimeError('native_database_exited')
                for name, command in commands.items():
                    process = children.get(name)
                    if process is not None and process.poll() is not None:
                        failures[name] = 0 if time.monotonic() - started_at[name] > 300 else failures[name] + 1
                        retry_at[name] = time.monotonic() + min(300, 2 ** min(failures[name], 8))
                        children.pop(name)
                    if name not in children and time.monotonic() >= retry_at[name]:
                        children[name] = start_process(config, name, command, role='maintenance' if name == 'maintenance' else 'app', run_id=run_id)
                        started_at[name] = time.monotonic()
                atomic_json(config.data / 'control' / 'runtime-status.json', {
                    'format': 1, 'pid': os.getpid(), 'running': True, 'run_id': run_id, 'identity': identity,
                    'updated_at': datetime.now(UTC).isoformat(),
                    'origin': config.origin, 'children': {name: {'running': process.poll() is None,
                        'pid': process.pid, 'restarts': failures[name]} for name, process in children.items()},
                })
                stop.wait(2)
        finally:
            _stop_children(children)
            if database is not None and database.poll() is None:
                try:
                    stop_database(config)
                except RuntimeError:
                    database.terminate()
                database.wait(timeout=15)
            stop_file.unlink(missing_ok=True)
            atomic_json(config.data / 'control' / 'runtime-status.json', {'format': 1, 'running': False,
                'run_id': run_id, 'identity': identity, 'pid': os.getpid(),
                'updated_at': datetime.now(UTC).isoformat()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'stop', 'check'])
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    config = NativeConfig.read(args.config)
    if args.command == 'run':
        run(config)
    elif args.command == 'stop':
        (config.data / 'control' / 'stop.request').touch()
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            try:
                status = json.loads((config.data / 'control' / 'runtime-status.json').read_text(encoding='utf-8'))
                if status.get('running') is False:
                    return
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.5)
        raise RuntimeError('native_shutdown_timeout')
    else:
        for path in (config.python, config.pg_bin / 'postgres.exe', config.program / 'frontend' / 'index.html'):
            if not path.is_file():
                raise RuntimeError('native_program_incomplete')
        print(json.dumps({'status': 'ok', 'origin': config.origin, 'roles': list(ROLES)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('native_runtime_failed kind=' + type(error).__name__ + ' code=' +
              str(error).split('\n', 1)[0] if isinstance(error, RuntimeError) else
              'native_runtime_failed kind=' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
