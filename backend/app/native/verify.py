"""Repeatable native acceptance in a newly created disposable directory.

Uses only the bundle's Python/PostgreSQL and local HTTP. It never installs a
service, changes power settings, requests market data or opens an existing DB.
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from app.native.config import NativeConfig, atomic_json, require_free_port
from app.native.install import native_identity, readiness_probe, runtime_status, verify_program
from app.native.security import reject_reparse


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            item = socket.socket()
            item.bind(('127.0.0.1', 0))
            sockets.append(item)
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def parent_pid(identity):
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == wintypes.HANDLE(-1).value:
        raise RuntimeError('native_process_snapshot_failed')
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID == identity:
                return entry.th32ParentProcessID
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return None


def local_request(config, path, *, body=None, headers=None):
    if not path.startswith('/'):
        raise ValueError('local_path_required')
    request = urllib.request.Request(config.origin + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'Origin': config.origin, **(headers or {})})
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read()
        value = json.loads(content) if response.headers.get('Content-Type', '').startswith('application/json') else None
        return response.status, value, response.headers


def ready(config, process, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('native_acceptance_runtime_exited')
        try:
            if runtime_status(config).get('pid') == process.pid and readiness_probe(config, native_identity(config)):
                return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(0.5)
    raise RuntimeError('native_acceptance_ready_timeout')


def helper(config, code, *, role='app', args=()):
    result = subprocess.run([str(config.python), '-I', '-B', '-c', code, *args], cwd=config.program / 'backend',
        env=config.environment(role), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    if result.returncode:
        raise RuntimeError('native_acceptance_helper_failed')
    return json.loads(result.stdout.strip().splitlines()[-1])


def login(config, password):
    status, _, headers = local_request(config, '/api/v1/session', body={'username': 'native-acceptance', 'password': password})
    if status != 201:
        raise RuntimeError('native_acceptance_login_failed')
    return headers['Set-Cookie'].split(';')[0]


def role_recovered(state, supervisor_pid, old_pid, killed_at, heartbeat, *, slot='primary', not_before=None):
    name = {'primary': 'realtime', 'secondary': 'realtime-secondary'}.get(slot)
    if name is None:
        return False
    role = state.get('children', {}).get(name, {})
    child = role.get('pid')
    if (state.get('running') is not True or state.get('pid') != supervisor_pid
            or role.get('running') is not True or type(child) is not int or child <= 0 or child == old_pid
            or parent_pid(child) != supervisor_pid):
        return False
    try:
        if heartbeat.get('worker_id') != 'market:realtime:' + slot + ':' + socket.gethostname():
            return False
        threshold = max(killed_at, not_before or killed_at)
        times = [datetime.fromisoformat(heartbeat[key]) for key in ('seen_at', 'progress_at')]
        return all(value.tzinfo is not None and value.astimezone(UTC) > threshold for value in times)
    except (KeyError, TypeError, ValueError):
        return False


def verify_slot_recovery(config, process, slot):
    name = {'primary': 'realtime', 'secondary': 'realtime-secondary'}.get(slot)
    if name is None:
        raise ValueError('native_acceptance_unknown_realtime_slot')
    state = json.loads((config.data / 'control' / 'runtime-status.json').read_text(encoding='utf-8'))
    child = state['children'][name]['pid']
    if state['pid'] != process.pid or parent_pid(child) != process.pid:
        raise RuntimeError('native_acceptance_child_identity_mismatch:' + slot)
    killed_at = datetime.now(UTC)
    os.kill(child, signal.SIGTERM)
    deadline = time.monotonic() + 30
    observed_pid, observed_at = None, None
    while time.monotonic() < deadline:
        state = json.loads((config.data / 'control' / 'runtime-status.json').read_text(encoding='utf-8'))
        current = state.get('children', {}).get(name, {}).get('pid')
        if type(current) is int and current != child and current != observed_pid and parent_pid(current) == process.pid:
            observed_pid, observed_at = current, datetime.now(UTC)
        beat = helper(config, "from sqlalchemy import text;from app.db import engine;import json,socket,sys\n"
            "identity='market:realtime:'+sys.argv[1]+':'+socket.gethostname()\n"
            "with engine().connect() as db:\n row=db.execute(text(\"SELECT worker_id,seen_at,progress_at FROM worker_heartbeats WHERE worker_id=:identity\"),{'identity':identity}).first(); result={'worker_id':row[0],**{key:value.isoformat() if value else None for key,value in zip(('seen_at','progress_at'),row[1:])}} if row else {}\n"
            "print(json.dumps(result))", args=(slot,))
        if observed_at and current == observed_pid and role_recovered(state, process.pid, child, killed_at,
                beat, slot=slot, not_before=observed_at):
            ready(config, process)
            return {'slot': slot, 'old_pid': child, 'new_pid': current,
                'killed_at': killed_at.isoformat(), 'new_pid_observed_at': observed_at.isoformat(), **beat}
        time.sleep(0.5)
    raise RuntimeError('native_acceptance_child_not_recovered:' + slot)


def cleanup_owned(processes, stop):
    """Try every owned instance and check both ports even after a stop failure."""
    failures = []
    for name in reversed(list(processes)):
        process, config = processes[name]
        try:
            stop(name)
            if process.poll() != 0:
                raise RuntimeError('native_acceptance_supervisor_shutdown_failed')
        except Exception as error:
            failures.append({'instance': name, 'step': 'stop', 'kind': type(error).__name__})
        for port in (config.http_port, config.database_port):
            try:
                require_free_port(port)
            except OSError as error:
                failures.append({'instance': name, 'step': 'port-not-released', 'kind': type(error).__name__})
    return failures


def write_receipt(workspace, program, checks, details, failure, cleanup_failures, *, role_recoveries=()):
    passed = failure is None and not cleanup_failures
    receipt = {'format': 1, 'status': 'passed' if passed else 'failed',
        'verified_at': datetime.now(UTC).isoformat(),
        'build': json.loads((program / 'release.json').read_text()), 'checks': checks,
        'databases': details, 'role_recoveries': list(role_recoveries),
        'cleanup': {'status': 'passed' if not cleanup_failures else 'failed',
                                         'failures': cleanup_failures},
        'not_verified': ['clean-Windows-VM', 'Windows-service-registration',
            'OS-reboot', 'Microsoft-runtime-installation', 'real-trading-day']}
    if failure is not None:
        receipt['failure_type'] = type(failure).__name__
    atomic_json(workspace / 'native-acceptance-receipt.json', receipt)
    return receipt


def verify(program, workspace):
    if os.name != 'nt':
        raise RuntimeError('windows_native_acceptance_required')
    program = reject_reparse(program).resolve(strict=True)
    workspace = reject_reparse(workspace).resolve()
    verify_program(program)
    if not str(program).isascii() or not str(workspace).isascii():
        raise ValueError('native_acceptance_requires_ascii_paths')
    if program in workspace.parents or workspace in program.parents or workspace == Path(workspace.anchor):
        raise ValueError('native_acceptance_workspace_invalid')
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / '.tianyan-disposable-native').write_text('new-acceptance-only\n', encoding='ascii')
    ports = free_ports(4)
    configs = []
    for index, name in enumerate(('source', 'restored')):
        folder = workspace / name
        folder.mkdir()
        file = folder / 'runtime.json'
        atomic_json(file, {'format': 1, 'program': str(program), 'data': str(folder / 'data'),
            'http_port': ports[index * 2], 'database_port': ports[index * 2 + 1],
            'collection_enabled': False, 'mode': 'demo'})
        configs.append(NativeConfig.read(file))
    source, restored = configs
    processes = {}
    checks = []
    role_recoveries = []

    def start(config, name):
        windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
        env = {key: value for key, value in os.environ.items() if not key.startswith(('JL_', 'PYTHON'))}
        env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
            PATH=os.pathsep.join(map(str, (program / 'runtime', config.pg_bin, windows / 'System32', windows))))
        with (workspace / (name + '-supervisor.log')).open('ab') as output:
            process = subprocess.Popen([str(config.python), '-I', '-B', '-m', 'app.native.runtime', 'run', '--config', str(config.file)],
                env=env, cwd=program, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        processes[name] = (process, config)
        ready(config, process)
        return process

    def stop(name):
        process, config = processes[name]
        if process.poll() is None:
            result = subprocess.run([str(config.python), '-I', '-B', '-m', 'app.native.runtime', 'stop', '--config', str(config.file)],
                env=config.environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=130)
            process.wait(timeout=15)
            if result.returncode:
                raise RuntimeError('native_acceptance_stop_command_failed')

    password = secrets.token_urlsafe(24)
    failure, details = None, []
    try:
        process = start(source, 'source')
        checks.append('bundled-runtime-and-native-postgres-with-minimal-PATH')
        local_request(source, '/native/setup', body={'username': 'native-acceptance', 'password': password},
            headers={'X-Tianyan-Setup': source.private()['setup_token']})
        cookie = login(source, password)
        for path in ('/', '/api/v1/market-session', '/api/v1/data-recovery'):
            if local_request(source, path, headers={'Cookie': cookie})[0] != 200:
                raise RuntimeError('native_acceptance_http_failed')
        checks.extend(['first-admin-and-login', 'frontend-and-application-HTTP'])
        helper(source, "from sqlalchemy.orm import Session; from sqlalchemy import select; from app.db import engine; from app.models import User; from app.ai.models import Connection; from app.ai.vault import write_secret; import json\n"
            "with Session(engine()) as db,db.begin():\n user=db.scalar(select(User)); reference=write_secret('native-acceptance-fixture-not-a-provider-key',db=db); db.add(Connection(owner_id=user.id,name='Native encrypted fixture',kind='market',template='tushare',endpoint='https://example.invalid/',enabled=False,secret_ref=reference,source_ids=[],options={}))\n"
            "print(json.dumps({'seeded':True}))")
        for slot in ('primary', 'secondary'):
            role_recoveries.append(verify_slot_recovery(source, process, slot))
            checks.append('owned-realtime-' + slot + '-process-exit-automatically-recovered')
        backup = helper(source, 'from app.maintenance.cli import main;main()', role='maintenance', args=('backup',))
        if backup['integrity_state'] != 'passed':
            raise RuntimeError('native_acceptance_backup_failed')
        checks.append('native-format-2-consistent-backup-with-encrypted-credential')
        stop('source')
        process = start(source, 'source')
        login(source, password)
        checks.append('full-native-stop-start-preserved-user-and-configuration')
        result = subprocess.run([str(source.python), '-I', '-B', '-m', 'app.native.restore', '--config', str(restored.file),
            '--backup', str(source.data / 'backups' / backup['location_key'])], env=source.environment(),
            cwd=program, capture_output=True, timeout=240)
        if result.returncode:
            raise RuntimeError('native_acceptance_empty_restore_failed')
        start(restored, 'restored')
        login(restored, password)
        checks.append('empty-native-restore-and-restored-user-login')
        query = "from sqlalchemy import text;from app.db import engine;from app.ai.vault import read_secret;import json\n" \
            "with engine().connect() as db:\n ref=db.scalar(text(\"SELECT secret_ref FROM service_connections WHERE name='Native encrypted fixture'\")); result={'schema':db.scalar(text('SELECT version_num FROM alembic_version')),'requests':db.scalar(text('SELECT COALESCE(sum(request_count),0) FROM data_fetch_runs')),'users':db.scalar(text('SELECT count(*) FROM users')),'append_only':not db.scalar(text(\"SELECT has_table_privilege('jlpm_app','evidence_versions','UPDATE')\")),'credential_matches':read_secret(ref)=='native-acceptance-fixture-not-a-provider-key'}\n" \
            "print(json.dumps(result))"
        details = [helper(config, query) for config in configs]
        if any(item['requests'] != 0 or item['users'] != 1 or not item['append_only'] or not item['credential_matches'] for item in details):
            raise RuntimeError('native_acceptance_data_preservation_failed')
        checks.extend(['encrypted-credential-decryption-after-restore', 'append-only-evidence-privileges', 'zero-market-requests'])
    except BaseException as error:
        failure = error
    finally:
        cleanup_failures = cleanup_owned(processes, stop)
        write_receipt(workspace, program, checks, details, failure, cleanup_failures, role_recoveries=role_recoveries)
    if failure is not None:
        raise failure
    if cleanup_failures:
        raise RuntimeError('native_acceptance_cleanup_failed')
    print(json.dumps({'status': 'passed', 'checks': len(checks), 'receipt': str(workspace / 'native-acceptance-receipt.json')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    verify(args.program, args.workspace)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('native_acceptance_failed kind=' + type(error).__name__ + ' code=' + str(error)
              if isinstance(error, RuntimeError) else 'native_acceptance_failed kind=' + type(error).__name__)
        raise SystemExit(1) from None
