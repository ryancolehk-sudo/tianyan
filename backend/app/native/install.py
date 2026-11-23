"""Elevated installer adapter. No market calls, Docker CLI or scheduled tasks."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from xml.etree import ElementTree as ET

from app.native.config import NativeConfig, atomic_json, prepare_directories, require_free_port
from app.native.database import checked
from app.native.security import current_sid, installation_root, owner_sid, protect_data, reject_reparse, tree_entries


def verify_program(program):
    program = reject_reparse(program).resolve(strict=True)
    manifest = json.loads((program / 'program-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('native_program_manifest_invalid')
    for name, expected in manifest['files'].items():
        if (not isinstance(name, str) or not isinstance(expected, str) or '\\' in name or ':' in name
                or name.startswith('/') or any(part in {'..', ''} for part in name.split('/'))
                or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise ValueError('native_program_manifest_invalid')
        target = reject_reparse(program / name)
        if not target.is_file():
            raise ValueError('native_program_manifest_invalid')
        with target.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != expected:
                raise ValueError('native_program_integrity_failed')
    actual = {path.relative_to(program).as_posix() for path in tree_entries(program)
              if path.is_file() and path != program / 'program-manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('native_program_manifest_incomplete')
    return json.loads((program / 'release.json').read_text(encoding='utf-8'))


def validate_layout(program, data, wrapper):
    program, data, wrapper = (reject_reparse(value) for value in (program, data, wrapper))
    root = installation_root(wrapper)
    program, data, wrapper = program.resolve(strict=True), data.resolve(), wrapper.resolve()
    if program.parent != root / 'versions':
        raise ValueError('native_program_requires_protected_version_directory')
    if program == data or program in data.parents or data in program.parents or data == Path(data.anchor):
        raise ValueError('native_data_directory_invalid')
    return program, data, wrapper, root


def secure_config(path):
    reject_reparse(path)
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    for name in ('program', 'data'):
        reject_reparse(raw[name])
    return NativeConfig.read(path)


def host_fingerprint(data):
    value = json.loads(reject_reparse(Path(data) / 'private' / 'host.json').read_text(encoding='utf-8'))
    passwords = value.get('passwords')
    if (not isinstance(passwords, dict) or set(passwords) != {'admin', 'migrate', 'app', 'maintenance'}
            or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item) for item in passwords.values())):
        raise ValueError('native_install_private_identity_invalid')
    return hashlib.sha256(json.dumps(passwords, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def existing_installation(root, data):
    """Public Program Files witness binds an installation to its private host."""
    witness = reject_reparse(root / 'installation.json')
    if not witness.exists():
        if data.exists() and any(data.iterdir()):
            raise ValueError('native_unrecognized_nonempty_data_requires_explicit_restore_adoption')
        return None
    value = json.loads(witness.read_text(encoding='utf-8'))
    marker = json.loads(reject_reparse(data / 'installation.json').read_text(encoding='utf-8'))
    if (value.get('format') != 1 or value.get('data') != str(data)
            or not isinstance(value.get('installation_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', value['installation_id'])
            or not re.fullmatch(r'S-1-\d+(?:-\d+)+', value.get('owner_sid', ''))
            or marker != {'format': 1, 'installation_id': value['installation_id']}
            or not hmac.compare_digest(value.get('host_fingerprint', ''), host_fingerprint(data))):
        raise ValueError('native_installation_witness_mismatch')
    return value


def record_installation(root, data, sid):
    witness = {'format': 1, 'data': str(data), 'installation_id': uuid.uuid4().hex,
               'owner_sid': sid, 'host_fingerprint': host_fingerprint(data)}
    atomic_json(data / 'installation.json', {'format': 1, 'installation_id': witness['installation_id']})
    atomic_json(root / 'installation.json', witness)
    return witness


def verify_restore_backup(folder):
    # Lazy import: routine installer safety checks do not initialize application
    # models or require database access. The shared verifier checks every file.
    from app.maintenance.backup import verify_bundle
    return verify_bundle(folder)


def service_xml(program, config_file, *, service_id='TianyanRuntime'):
    program, config_file = Path(program), Path(config_file)
    node = ET.Element('service')
    values = {
        'id': service_id, 'name': '天衍系统后台服务',
        'description': '按交易时段采集数据、自动补漏并提供本机界面。关闭页面不会停止此服务。',
        'executable': str(program / 'runtime/python.exe'),
        'arguments': '-I -B -m app.native.runtime run --config "' + str(config_file) + '"',
        'workingdirectory': str(program / 'backend'),
        'startmode': 'Automatic', 'delayedAutoStart': 'true',
        'stopexecutable': str(program / 'runtime/python.exe'),
        'stoparguments': '-I -B -m app.native.runtime stop --config "' + str(config_file) + '"',
        'stoptimeout': '120sec',
        'logpath': str(config_file.parent / 'logs'),
    }
    for name, value in values.items():
        ET.SubElement(node, name).text = value
    account = ET.SubElement(node, 'serviceaccount')
    ET.SubElement(account, 'domain').text = 'NT AUTHORITY'
    ET.SubElement(account, 'user').text = 'LocalService'
    ET.SubElement(node, 'onfailure', {'action': 'restart', 'delay': '10sec'})
    ET.SubElement(node, 'onfailure', {'action': 'restart', 'delay': '30sec'})
    ET.SubElement(node, 'onfailure', {'action': 'restart', 'delay': '120sec'})
    ET.SubElement(node, 'resetfailure').text = '1hour'
    logs = ET.SubElement(node, 'log', {'mode': 'roll-by-size'})
    ET.SubElement(logs, 'sizeThreshold').text = '10240'
    ET.SubElement(logs, 'keepFiles').text = '3'
    return ET.tostring(node, encoding='unicode')


def native_identity(config):
    """Public correlation identity, never a credential or a raw local path."""
    release = json.loads((config.program / 'release.json').read_text(encoding='utf-8'))
    public = {'program': str(config.program), 'data': str(config.data), 'config': str(config.file),
        'http_port': config.http_port, 'database_port': config.database_port,
        'mode': config.mode, 'collection_enabled': config.collection_enabled}
    packed = json.dumps(public, sort_keys=True, separators=(',', ':')).encode()
    key = bytes.fromhex(config.private()['passwords']['app'])
    return {'format': 1, 'configuration_id': hashlib.sha256(packed).hexdigest(),
        'instance_id': hmac.new(key, b'tianyan-native-instance-v1', hashlib.sha256).hexdigest(),
        'build_revision': release['build_revision'],
        'program_manifest_sha256': hashlib.sha256((config.program / 'program-manifest.json').read_bytes()).hexdigest()}


def runtime_status(config):
    try:
        value = json.loads((config.data / 'control' / 'runtime-status.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def readiness_probe(config, expected, previous_run_id=None):
    state = runtime_status(config)
    run_id = state.get('run_id')
    api = state.get('children', {}).get('api', {})
    if (state.get('running') is not True or state.get('identity') != expected
            or not isinstance(run_id, str) or not re.fullmatch('[0-9a-f]{32}', run_id)
            or run_id == previous_run_id or api.get('running') is not True
            or type(api.get('pid')) is not int or api['pid'] <= 0):
        return False
    with urllib.request.urlopen(config.origin + '/health/ready', timeout=3) as response:
        headers = response.headers
        if response.status != 200 or any(headers.get(header) != expected[field] for header, field in (
            ('X-Tianyan-Native-Instance', 'instance_id'), ('X-Tianyan-Native-Config', 'configuration_id'),
            ('X-Tianyan-Native-Build', 'build_revision'), ('X-Tianyan-Native-Program', 'program_manifest_sha256'))):
            return False
        return headers.get('X-Tianyan-Native-Run') == run_id and headers.get('X-Tianyan-Native-Pid') == str(api['pid'])


def wait_ready(config, timeout=180, *, previous_run_id=None):
    expected = native_identity(config)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if readiness_probe(config, expected, previous_run_id):
                return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(1)
    raise RuntimeError('native_service_not_ready')


def service_exists(service_id='TianyanRuntime'):
    result = subprocess.run(['sc.exe', 'query', service_id], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode not in {0, 1060}:
        raise RuntimeError('native_service_status_unavailable')
    return result.returncode == 0


def service_running():
    class Status(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ('type', 'state', 'accepted', 'win32_exit',
                                                        'service_exit', 'checkpoint', 'wait_hint')]
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenSCManagerW.restype = wintypes.HANDLE
    api.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    api.OpenServiceW.restype = wintypes.HANDLE
    api.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(Status)]
    api.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    manager = api.OpenSCManagerW(None, None, 1)
    if not manager:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        service = api.OpenServiceW(manager, 'TianyanRuntime', 4)
        if not service:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            status = Status()
            if not api.QueryServiceStatus(service, ctypes.byref(status)):
                raise ctypes.WinError(ctypes.get_last_error())
            return status.state == 4
        finally:
            api.CloseServiceHandle(service)
    finally:
        api.CloseServiceHandle(manager)


def backup_stopped_service(program, prior, root):
    """Use LocalService rather than trying to launch PostgreSQL as UAC admin."""
    require_free_port(prior.http_port)
    require_free_port(prior.database_port)
    request = uuid.uuid4().hex
    wrapper = root / ('TianyanBackup-' + request + '.exe')
    receipt_file = prior.data / 'control' / ('upgrade-backup-' + request + '.json')
    shutil.copy2(program / 'TianyanService.exe', wrapper)
    node = ET.fromstring(service_xml(program, prior.file, service_id='TianyanBackup' + request))
    node.find('name').text = '天衍升级前临时备份'
    node.find('startmode').text = 'Manual'
    node.find('arguments').text = ('-I -B -m app.native.install backup-stopped --program "' + str(program)
        + '" --data "' + str(prior.data) + '" --wrapper "' + str(root / 'TianyanService.exe')
        + '" --backup-request ' + request)
    for tag in ('stopexecutable', 'stoparguments', 'delayedAutoStart', 'onfailure', 'resetfailure'):
        for item in node.findall(tag):
            node.remove(item)
    wrapper.with_suffix('.xml').write_text(ET.tostring(node, encoding='unicode'), encoding='utf-8')
    registered = False
    try:
        checked([str(wrapper), 'install'], timeout=60)
        registered = True
        checked([str(wrapper), 'start'], timeout=60)
        deadline = time.monotonic() + 3700
        while time.monotonic() < deadline:
            if receipt_file.exists():
                result = json.loads(receipt_file.read_text(encoding='utf-8'))
                if (result.get('format') != 1 or result.get('request') != request
                        or result.get('status') != 'passed'
                        or result.get('backup', {}).get('integrity_state') != 'passed'):
                    raise RuntimeError('native_stopped_instance_backup_failed')
                return result['backup']
            time.sleep(1)
        raise RuntimeError('native_stopped_instance_backup_timeout')
    finally:
        if registered or service_exists('TianyanBackup' + request):
            checked([str(wrapper), 'stopwait'], timeout=140)
            checked([str(wrapper), 'uninstall'], timeout=60)
        # These names were generated inside the protected program root solely
        # for this call; retained database/configuration files are never removed.
        wrapper.unlink(missing_ok=True)
        wrapper.with_suffix('.xml').unlink(missing_ok=True)
        require_free_port(prior.http_port)
        require_free_port(prior.database_port)


def run_stopped_backup(program, data, wrapper, request):
    if not isinstance(request, str) or not re.fullmatch('[0-9a-f]{32}', request):
        raise ValueError('native_backup_request_invalid')
    if current_sid() != 'S-1-5-19':
        raise RuntimeError('native_stopped_backup_requires_localservice')
    program, data, wrapper, root = validate_layout(program, data, wrapper)
    verify_program(program)
    if existing_installation(root, data) is None:
        raise ValueError('native_stopped_backup_requires_known_installation')
    config = secure_config(data / 'runtime.json')
    if config.data != data or config.program.parent != root / 'versions':
        raise ValueError('native_existing_instance_mismatch')
    verify_program(config.program)
    receipt_file = data / 'control' / ('upgrade-backup-' + request + '.json')
    from app.native.database import stop_database, wait_database
    from app.native.runtime import instance_lock, start_process
    try:
        with instance_lock(data / 'control' / 'runtime.lock'):
            require_free_port(config.http_port)
            require_free_port(config.database_port)
            database = start_process(config, 'upgrade-backup-postgres', [str(config.pg_bin / 'postgres.exe'),
                '-D', str(data / 'database'), '-p', str(config.database_port), '-h', '127.0.0.1'])
            try:
                wait_database(config, database)
                output = checked([str(config.python), '-I', '-B', '-m', 'app.maintenance.cli', 'backup'],
                    env=config.environment('maintenance'), cwd=config.program / 'backend', timeout=3600)
                backup = json.loads(output.strip().splitlines()[-1])
                if backup.get('integrity_state') != 'passed':
                    raise RuntimeError('native_upgrade_backup_not_verified')
            finally:
                if database.poll() is None:
                    stop_database(config)
                    database.wait(timeout=15)
        atomic_json(receipt_file, {'format': 1, 'request': request, 'status': 'passed', 'backup': backup})
    except Exception as error:
        atomic_json(receipt_file, {'format': 1, 'request': request, 'status': 'failed',
                                  'failure_type': type(error).__name__})
        raise


def install(program, data, wrapper, *, http_port=8088, database_port=55438):
    program, data, wrapper, root = validate_layout(program, data, wrapper)
    release = verify_program(program)
    witness = existing_installation(root, data)
    config_file = data / 'runtime.json'
    prior = secure_config(config_file) if witness and config_file.exists() else None
    if prior is not None and prior.data != data:
        raise ValueError('native_existing_instance_mismatch')
    if witness and prior is None:
        raise ValueError('native_existing_configuration_missing')
    if prior:
        if prior.program.parent != root / 'versions':
            raise ValueError('native_previous_program_outside_protected_versions')
        verify_program(prior.program)  # Must precede every elevated old-Python call.
    sid = witness['owner_sid'] if witness else owner_sid()
    protect_data(data, sid)
    journal = {'format': 1, 'stage': 'preflight', 'build_revision': release['build_revision'],
               'started_at': datetime.now(UTC).isoformat(), 'previous_config': None}
    installed = service_exists()
    if installed and prior is None:
        raise RuntimeError('native_existing_service_configuration_missing')
    if prior and prior.program != program:
        # A backup is mandatory before selecting a new program. The core backup
        # implementation coordinates DB and encrypted vault writes.
        if installed and service_running():
            output = checked([str(prior.python), '-I', '-B', '-m', 'app.maintenance.cli', 'backup'],
                             env=prior.environment('maintenance'), cwd=prior.program / 'backend', timeout=3700)
            receipt = json.loads(output.strip().splitlines()[-1])
        else:
            receipt = backup_stopped_service(program, prior, root)
        if receipt.get('integrity_state') != 'passed':
            raise RuntimeError('native_upgrade_backup_not_verified')
        journal['backup'] = receipt
        journal['previous_config'] = json.loads(config_file.read_text(encoding='utf-8'))
        journal['previous_schema'] = json.loads((prior.program / 'release.json').read_text(encoding='utf-8'))['schema_revision']
    elif prior:
        journal['previous_config'] = json.loads(config_file.read_text(encoding='utf-8'))
        journal['previous_schema'] = release['schema_revision']
        journal['operation'] = 'repair_same_program'
    atomic_json(root / 'installation-attempt.json', journal)
    if witness:
        atomic_json(data / 'upgrade-journal.json', journal)
    configuration = {'format': 1, 'program': str(program), 'data': str(data),
        'http_port': prior.http_port if prior else http_port,
        'database_port': prior.database_port if prior else database_port,
        'collection_enabled': prior.collection_enabled if prior else True, 'mode': prior.mode if prior else 'real'}
    previous_run_id = runtime_status(prior).get('run_id') if prior else None
    switched = False
    try:
        if installed:
            checked([str(wrapper), 'stopwait'], timeout=140)
        # Check before creating a new configuration or service. An unrelated
        # Docker/desktop instance on 8088 must never satisfy native readiness.
        require_free_port(configuration['http_port'])
        require_free_port(configuration['database_port'])
        journal['stage'] = 'switching'
        atomic_json(root / 'installation-attempt.json', journal)
        atomic_json(config_file, configuration)
        switched = True
        config = NativeConfig.read(config_file)
        prepare_directories(config)
        protect_data(data, sid)
        if witness is None:
            witness = record_installation(root, data, sid)
        atomic_json(data / 'upgrade-journal.json', journal)
        shutil.copy2(program / 'TianyanService.exe', wrapper)
        wrapper.with_suffix('.xml').write_text(service_xml(program, config_file), encoding='utf-8')
        if not installed:
            checked([str(wrapper), 'install'], timeout=60)
        checked([str(wrapper), 'start'], timeout=60)
        wait_ready(config, previous_run_id=previous_run_id)
    except Exception as failure:
        journal['failure_type'] = type(failure).__name__
        try:
            if switched and service_exists():
                checked([str(wrapper), 'stopwait'], timeout=140)
            if prior and (not switched or journal.get('previous_schema') == release['schema_revision']):
                failed_run_id = runtime_status(prior).get('run_id')
                atomic_json(config_file, journal['previous_config'])
                shutil.copy2(prior.program / 'TianyanService.exe', wrapper)
                wrapper.with_suffix('.xml').write_text(service_xml(prior.program, config_file), encoding='utf-8')
                if not service_exists():
                    checked([str(wrapper), 'install'], timeout=60)
                checked([str(wrapper), 'start'], timeout=60)
                wait_ready(prior, previous_run_id=failed_run_id)
                journal['stage'] = 'program_rolled_back'
            else:
                # Never boot an old binary against a possibly newer schema.
                journal['stage'] = 'manual_restore_required' if prior else 'installation_failed'
        except Exception as recovery_failure:
            journal['stage'] = 'manual_restore_required' if prior else 'installation_failed'
            journal['recovery_failure_type'] = type(recovery_failure).__name__
        atomic_json(root / 'installation-attempt.json', journal)
        if witness:
            atomic_json(data / 'upgrade-journal.json', journal)
        raise
    journal['stage'] = 'completed'
    journal['completed_at'] = datetime.now(UTC).isoformat()
    atomic_json(data / 'upgrade-journal.json', journal)
    atomic_json(root / 'installation-attempt.json', journal)
    return config


def adopt_restored(program, data, wrapper, *, restored_config, backup, confirmed=False):
    """Explicit administrator-only bridge from a verified, stopped restore.

    Ordinary installation never calls this path or accepts its flags implicitly.
    The operator supplies the original format-2 backup again, including its
    separate master-key directory. No source database or backup is modified.
    """
    if not confirmed:
        raise ValueError('native_restore_adoption_requires_explicit_confirmation')
    program, data, wrapper, root = validate_layout(program, data, wrapper)
    verify_program(program)
    if (root / 'installation.json').exists() or service_exists():
        raise ValueError('native_restore_adoption_requires_unregistered_instance')
    config = secure_config(restored_config)
    if config.data != data or config.program != program or config.collection_enabled:
        raise ValueError('native_restore_adoption_configuration_mismatch')
    for path in tree_entries(data):
        reject_reparse(path)
    folder = reject_reparse(backup).resolve(strict=True)
    for path in tree_entries(folder):
        reject_reparse(path)
    master = reject_reparse(folder.with_name(folder.name + '-master-key'))
    reject_reparse(master / 'ai_master_key')
    manifest = verify_restore_backup(folder)
    receipt = json.loads((data / 'restore-receipt.json').read_text(encoding='utf-8'))
    required = {'backup-hashes', 'empty-instance-only', 'postgres-restore',
                'append-only-privileges', 'credential-decryption', 'schema-upgrade'}
    if (receipt.get('format') != 1 or receipt.get('status') != 'passed'
            or receipt.get('collection_enabled') is not False
            or not required.issubset(receipt.get('checks', []))
            or receipt.get('manifest_sha256') != hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
            or manifest.get('app_mode') != config.mode
            or hashlib.sha256((data / 'master' / 'ai_master_key').read_bytes()).hexdigest() != manifest['ai_master_sha256']
            or (data / 'database' / 'PG_VERSION').read_text().strip() != '17'):
        raise ValueError('native_restore_adoption_evidence_invalid')
    from app.native.runtime import instance_lock
    with instance_lock(data / 'control' / 'runtime.lock'):
        require_free_port(config.http_port)
        require_free_port(config.database_port)
        sid = owner_sid()
        protect_data(data, sid)
        if config.file != data / 'runtime.json':
            atomic_json(data / 'runtime.json', json.loads(config.file.read_text(encoding='utf-8')))
        record_installation(root, data, sid)
    return install(program, data, wrapper)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'adopt-restored', 'backup-stopped', 'uninstall', 'open'])
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--wrapper', type=Path, required=True)
    parser.add_argument('--restored-config', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--confirm-adopt-restored', action='store_true')
    parser.add_argument('--backup-request')
    args = parser.parse_args()
    if args.command == 'install':
        install(args.program, args.data, args.wrapper)
    elif args.command == 'adopt-restored':
        if args.restored_config is None or args.backup is None:
            parser.error('adopt-restored requires --restored-config and --backup')
        adopt_restored(args.program, args.data, args.wrapper, restored_config=args.restored_config,
                      backup=args.backup, confirmed=args.confirm_adopt_restored)
    elif args.command == 'backup-stopped':
        run_stopped_backup(args.program, args.data, args.wrapper, args.backup_request)
    elif args.command == 'uninstall':
        if args.wrapper.is_file():
            if not service_exists():
                return
            checked([str(args.wrapper), 'stopwait'], timeout=140)
            checked([str(args.wrapper), 'uninstall'], timeout=60)
    else:
        # The installer runs elevated, so it opens only the public local page.
        # The normal desktop shortcut supplies the one-time setup fragment.
        import webbrowser
        webbrowser.open(NativeConfig.read(args.data / 'runtime.json').origin)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('native_install_failed kind=' + type(error).__name__)
        raise SystemExit(1) from None
