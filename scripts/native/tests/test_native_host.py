"""Host safety checks; no services, Docker, database reset or network requests."""
import io
import json
import os
from pathlib import Path
from datetime import UTC, datetime, timedelta
import stat
import socket
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend'))
from app.native.config import NativeConfig, atomic_json, prepare_directories, require_free_port
from app.native.database import initialize_cluster
from app.native.install import (service_xml, verify_program, native_identity, readiness_probe, install,
                               record_installation, adopt_restored)
from app.native.install import backup_stopped_service, run_stopped_backup
from app.native.restore import extract_tree, restore_empty
from app.native.runtime import child_commands, instance_lock
from app.native import security
from app.native.verify import role_recovered, verify_slot_recovery, cleanup_owned, write_receipt


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.program = self.root / 'versions' / 'current'
        self.program.mkdir(parents=True)
        (self.program / 'release.json').write_text(json.dumps({'build_revision': 'a' * 40}), encoding='utf-8')
        (self.program / 'program-manifest.json').write_text(json.dumps({'format': 1, 'files': {}}))
        self.file = self.root / 'runtime.json'
        self.raw = {'format': 1, 'program': str(self.program), 'data': str(self.root / 'data'),
                    'http_port': 18048, 'database_port': 55448, 'collection_enabled': False}
        atomic_json(self.file, self.raw)
        self.config = NativeConfig.read(self.file)

    def changed(self, **values):
        atomic_json(self.file, {**self.raw, **values})
        return NativeConfig.read(self.file)

    def test_initial_private_state_is_random_stable_and_never_in_public_config(self):
        prepare_directories(self.config)
        first = self.config.private()
        prepare_directories(self.config)
        self.assertEqual(first, self.config.private())
        self.assertEqual(4, len(set(first['passwords'].values())))
        public = self.file.read_text(encoding='utf-8')
        self.assertNotIn(first['setup_token'], public)
        for value in first['passwords'].values():
            self.assertNotIn(value, public)

    def test_existing_database_without_host_secrets_never_creates_replacement_passwords(self):
        folder = self.config.data / 'database'
        folder.mkdir(parents=True)
        (folder / 'PG_VERSION').write_text('17')
        with self.assertRaisesRegex(ValueError, 'private_state_missing'):
            prepare_directories(self.config)

    def test_ciphertexts_without_master_never_get_new_key(self):
        prepare_directories(self.config)
        (self.config.data / 'master' / 'ai_master_key').unlink()
        (self.config.data / 'credentials' / 'old.key').write_bytes(b'existing-ciphertext')
        with self.assertRaisesRegex(ValueError, 'master_missing'):
            prepare_directories(self.config)

    def test_rejects_program_or_data_overlap(self):
        with self.assertRaisesRegex(ValueError, 'must_be_separate'):
            self.changed(data=str(self.program / 'data'))

    def test_rejects_relative_program(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.changed(program='relative-program')

    def test_rejects_conflicting_ports(self):
        with self.assertRaisesRegex(ValueError, 'ports_conflict'):
            self.changed(database_port=18048)

    def test_rejects_unknown_configuration_fields(self):
        with self.assertRaisesRegex(ValueError, 'configuration_invalid'):
            self.changed(database_url='postgresql://external-machine')

    def test_ambient_credentials_database_and_python_path_cannot_select_another_instance(self):
        prepare_directories(self.config)
        with patch.dict(os.environ, {'JL_DATABASE_URL': 'postgresql://production', 'JL_TUSHARE_TOKEN': 'private-provider-key',
                                     'JL_MODEL_API_KEY': 'private-model-key', 'PYTHONPATH': 'other-project'}):
            env = self.config.environment()
        self.assertNotIn('JL_TUSHARE_TOKEN', env)
        self.assertNotIn('JL_MODEL_API_KEY', env)
        self.assertIn('127.0.0.1:55448/jiulong', env['JL_DATABASE_URL'])
        self.assertEqual(str(self.program / 'backend'), env['PYTHONPATH'])
        self.assertEqual('false', env['JL_DATA_COLLECTION_ENABLED'])
        self.assertEqual(['scheduler', 'realtime', 'recovery', 'general'], json.loads(env['JL_REQUIRED_WORKER_ROLES']))

    def test_maintenance_uses_separate_database_role(self):
        prepare_directories(self.config)
        self.assertIn('jlpm_maintenance:', self.config.environment('maintenance')['JL_DATABASE_URL'])
        self.assertIn('jlpm_app:', self.config.environment()['JL_DATABASE_URL'])

    def test_busy_port_is_rejected_without_killing_other_program(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaises(OSError):
                require_free_port(port)
            self.assertGreater(listener.fileno(), 0)

    def test_same_data_directory_cannot_run_two_supervisors(self):
        with instance_lock(self.root / 'runtime.lock'):
            with self.assertRaisesRegex(RuntimeError, 'already_running'):
                with instance_lock(self.root / 'runtime.lock'):
                    self.fail('second owner acquired lock')

    def test_bad_postgres_major_preserved_not_reinitialized(self):
        folder = self.config.data / 'database'
        folder.mkdir(parents=True)
        (folder / 'PG_VERSION').write_text('18')
        with self.assertRaisesRegex(RuntimeError, 'major_mismatch'):
            initialize_cluster(self.config)
        self.assertEqual('18', (folder / 'PG_VERSION').read_text())

    def test_initdb_failure_removes_temporary_password(self):
        prepare_directories(self.config)
        with patch('app.native.database.require_free_port'), patch('app.native.database.checked', side_effect=RuntimeError('init-failed')):
            with self.assertRaisesRegex(RuntimeError, 'init-failed'):
                initialize_cluster(self.config)
        self.assertFalse((self.config.data / 'private' / 'init-password.pending').exists())

    def test_service_is_independent_of_gui_with_local_service_identity_and_stop_command(self):
        node = ET.fromstring(service_xml(self.program, self.file))
        self.assertEqual('Automatic', node.findtext('startmode'))
        self.assertEqual('LocalService', node.findtext('serviceaccount/user'))
        self.assertEqual('NT AUTHORITY', node.findtext('serviceaccount/domain'))
        self.assertNotIn('launcher', node.findtext('arguments'))
        self.assertIn('app.native.runtime run', node.findtext('arguments'))
        self.assertIn('app.native.runtime stop', node.findtext('stoparguments'))
        self.assertTrue(node.findtext('arguments').startswith('-I -B '))
        self.assertTrue(node.findtext('stoparguments').startswith('-I -B '))
        self.assertEqual('3', node.findtext('log/keepFiles'))
        self.assertEqual(3, len(node.findall('onfailure')))

    def test_four_collection_roles_and_loopback_api_do_not_depend_on_window(self):
        commands = child_commands(self.config)
        for command in commands.values():
            self.assertEqual(['-I', '-B'], command[1:3])
        for role in ('scheduler', 'recovery', 'general'):
            self.assertEqual(['--role', role], commands[role][-2:])
        self.assertEqual(['--role', 'realtime', '--slot', 'primary'], commands['realtime'][-4:])
        self.assertEqual(['--role', 'realtime', '--slot', 'secondary'], commands['realtime-secondary'][-4:])
        self.assertIn('127.0.0.1', commands['api'])
        self.assertNotIn('0.0.0.0', commands['api'])

    def test_program_tampering_rejected_before_service_install(self):
        import hashlib
        original = (self.program / 'release.json').read_bytes()
        (self.program / 'program-manifest.json').write_text(json.dumps({'format': 1,
            'files': {'release.json': hashlib.sha256(original).hexdigest()}}))
        verify_program(self.program)
        (self.program / 'release.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'integrity_failed'):
            verify_program(self.program)

    def test_backup_import_never_overwrites_an_existing_cluster(self):
        folder = self.config.data / 'database'
        folder.mkdir(parents=True)
        (folder / 'PG_VERSION').write_text('17')
        with self.assertRaisesRegex(ValueError, 'requires_empty_instance'):
            restore_empty(self.config, self.root / 'unread-backup')

    def test_backup_archive_cannot_write_outside_target(self):
        archive = self.root / 'malicious.tar'
        with tarfile.open(archive, 'w') as output:
            item = tarfile.TarInfo('credentials/../../outside.txt')
            item.size = 4
            output.addfile(item, io.BytesIO(b'evil'))
        with self.assertRaisesRegex(ValueError, 'archive_invalid'):
            extract_tree(archive, self.root / 'restore', 'credentials')
        self.assertFalse((self.root / 'outside.txt').exists())

    def readiness_fixture(self, run_id='1' * 32):
        prepare_directories(self.config)
        identity = native_identity(self.config)
        atomic_json(self.config.data / 'control' / 'runtime-status.json', {
            'running': True, 'identity': identity, 'run_id': run_id, 'pid': 222,
            'children': {'api': {'pid': 223, 'running': True}}})
        headers = {'X-Tianyan-Native-Run': run_id, 'X-Tianyan-Native-Pid': '223',
            'X-Tianyan-Native-Instance': identity['instance_id'],
            'X-Tianyan-Native-Config': identity['configuration_id'],
            'X-Tianyan-Native-Build': identity['build_revision'],
            'X-Tianyan-Native-Program': identity['program_manifest_sha256']}
        response = MagicMock()
        response.status = 200
        response.headers = headers
        response.__enter__.return_value = response
        return identity, response

    def test_another_http_200_service_cannot_satisfy_native_readiness(self):
        identity, response = self.readiness_fixture()
        response.headers = {}
        with patch('app.native.install.urllib.request.urlopen', return_value=response):
            self.assertFalse(readiness_probe(self.config, identity))

    def test_native_ready_requires_matching_config_build_run_and_api_pid(self):
        identity, response = self.readiness_fixture()
        with patch('app.native.install.urllib.request.urlopen', return_value=response):
            self.assertTrue(readiness_probe(self.config, identity))
            for field in ('X-Tianyan-Native-Config', 'X-Tianyan-Native-Build',
                          'X-Tianyan-Native-Run', 'X-Tianyan-Native-Pid'):
                value = response.headers[field]
                response.headers[field] = 'another-instance'
                self.assertFalse(readiness_probe(self.config, identity))
                response.headers[field] = value
            self.assertFalse(readiness_probe(self.config, identity, previous_run_id='1' * 32))

    def upgrade_fixture(self, *, old_schema='0021', new_schema='0021'):
        data = self.root / 'upgrade-data'
        data.mkdir()
        old = self.root / 'versions' / 'old-version'
        old.mkdir()
        (old / 'release.json').write_text(json.dumps({'build_revision': 'c' * 40, 'schema_revision': old_schema}))
        (old / 'TianyanService.exe').write_bytes(b'old-wrapper')
        (old / 'program-manifest.json').write_text('{}')
        (self.program / 'TianyanService.exe').write_bytes(b'new-wrapper')
        release = {'build_revision': 'd' * 40, 'schema_revision': new_schema}
        (self.program / 'release.json').write_text(json.dumps(release))
        prior = {'format': 1, 'program': str(old), 'data': str(data), 'http_port': 18048,
                 'database_port': 55448, 'collection_enabled': False}
        atomic_json(data / 'runtime.json', prior)
        prepare_directories(NativeConfig.read(data / 'runtime.json'))
        record_installation(self.root, data, 'S-1-5-21-1-2-3-1000')
        wrapper = self.root / 'TianyanService.exe'
        wrapper.write_bytes(b'old-wrapper')
        return data, old, release, prior, wrapper

    def test_service_start_failure_rolls_back_configuration_xml_and_wrapper(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        starts = []
        def command(args, **kwargs):
            if 'app.maintenance.cli' in args:
                return json.dumps({'integrity_state': 'passed', 'location_key': 'fixture-backup'})
            if args[-1] == 'start':
                starts.append(args)
                if len(starts) == 1:
                    raise RuntimeError('native_command_failed:TianyanService.exe:1053')
            return ''
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program', return_value=release), \
             patch('app.native.install.owner_sid', return_value='S-1-5-21-1-2-3-1000'), \
             patch('app.native.install.protect_data'), patch('app.native.install.service_exists', return_value=True), \
             patch('app.native.install.service_running', return_value=True), \
             patch('app.native.install.require_free_port'), patch('app.native.install.checked', side_effect=command), \
             patch('app.native.install.wait_ready') as ready:
            with self.assertRaisesRegex(RuntimeError, '1053'):
                install(self.program, data, wrapper)
        self.assertEqual(prior, json.loads((data / 'runtime.json').read_text()))
        self.assertEqual(b'old-wrapper', wrapper.read_bytes())
        self.assertIn(str(old), wrapper.with_suffix('.xml').read_text(encoding='utf-8'))
        self.assertEqual('program_rolled_back', json.loads((data / 'upgrade-journal.json').read_text())['stage'])
        self.assertEqual(2, len(starts))
        self.assertEqual(old, ready.call_args.args[0].program)

    def test_schema_change_start_failure_keeps_backup_without_booting_old_binary(self):
        data, old, release, prior, wrapper = self.upgrade_fixture(new_schema='0022')
        starts = []
        def command(args, **kwargs):
            if 'app.maintenance.cli' in args:
                return json.dumps({'integrity_state': 'passed', 'location_key': 'fixture-backup'})
            if args[-1] == 'start':
                starts.append(args)
                raise RuntimeError('start-failed')
            return ''
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program', return_value=release), \
             patch('app.native.install.owner_sid', return_value='S-1-5-21-1-2-3-1000'), \
             patch('app.native.install.protect_data'), patch('app.native.install.service_exists', return_value=True), \
             patch('app.native.install.service_running', return_value=True), \
             patch('app.native.install.require_free_port'), patch('app.native.install.checked', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'start-failed'):
                install(self.program, data, wrapper)
        journal = json.loads((data / 'upgrade-journal.json').read_text())
        self.assertEqual('manual_restore_required', journal['stage'])
        self.assertEqual('fixture-backup', journal['backup']['location_key'])
        self.assertEqual(prior, journal['previous_config'])
        self.assertEqual(1, len(starts))

    def test_initial_port_conflict_does_not_write_config_or_start_a_service(self):
        data = self.root / 'new-data'
        data.mkdir()
        wrapper = self.root / 'TianyanService.exe'
        release = {'build_revision': 'a' * 40, 'schema_revision': '0021'}
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program', return_value=release), \
             patch('app.native.install.owner_sid', return_value='S-1-5-21-1-2-3-1000'), \
             patch('app.native.install.protect_data'), patch('app.native.install.service_exists', return_value=False), \
             patch('app.native.install.require_free_port', side_effect=OSError('port-busy')), \
             patch('app.native.install.checked') as commands:
            with self.assertRaisesRegex(OSError, 'port-busy'):
                install(self.program, data, wrapper)
        self.assertFalse((data / 'runtime.json').exists())
        self.assertFalse(wrapper.exists())
        commands.assert_not_called()

    def test_unknown_prepopulated_data_cannot_execute_an_elevated_old_program(self):
        data = self.root / 'planted-data'
        data.mkdir()
        (data / 'runtime.json').write_text(json.dumps(self.raw))
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program'), patch('app.native.install.checked') as commands, \
             patch('app.native.install.protect_data') as protect:
            with self.assertRaisesRegex(ValueError, 'unrecognized_nonempty'):
                install(self.program, data, self.root / 'TianyanService.exe')
        commands.assert_not_called()
        protect.assert_not_called()

    def test_old_program_must_stay_inside_protected_versions_before_backup(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        prior['program'] = str(self.root / 'user-writeable-program')
        atomic_json(data / 'runtime.json', prior)
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program'), patch('app.native.install.checked') as commands:
            with self.assertRaisesRegex(ValueError, 'previous_program_outside'):
                install(self.program, data, wrapper)
        commands.assert_not_called()

    def test_previous_program_is_verified_before_any_old_interpreter_execution(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        verified = []
        def verify(path):
            verified.append(path)
            return release
        def command(args, **kwargs):
            self.assertIn(old, verified)
            raise RuntimeError('intentional-stop-before-backup')
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program', side_effect=verify), \
             patch('app.native.install.protect_data'), patch('app.native.install.service_exists', return_value=True), \
             patch('app.native.install.service_running', return_value=True), \
             patch('app.native.install.checked', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, 'intentional-stop'):
                install(self.program, data, wrapper)

    def test_installation_witness_binds_private_passwords_but_not_consumed_setup_token(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        host_file = data / 'private' / 'host.json'
        host = json.loads(host_file.read_text())
        host.pop('setup_token', None)
        atomic_json(host_file, host)
        from app.native.install import existing_installation
        self.assertIsNotNone(existing_installation(self.root, data))
        host['passwords']['app'] = 'f' * 64
        atomic_json(host_file, host)
        with self.assertRaisesRegex(ValueError, 'witness_mismatch'):
            existing_installation(self.root, data)

    def test_uninstalled_or_stopped_old_instance_uses_localservice_backup_before_switch(self):
        for registered in (False, True):
            with self.subTest(registered=registered):
                if (self.root / 'upgrade-data').exists():
                    import shutil
                    shutil.rmtree(self.root / 'upgrade-data')
                    shutil.rmtree(self.root / 'versions' / 'old-version')
                data, old, release, prior, wrapper = self.upgrade_fixture()
                def backup(program, config, root):
                    self.assertEqual(prior, json.loads((data / 'runtime.json').read_text()))
                    self.assertEqual(old, config.program)
                    return {'integrity_state': 'passed', 'location_key': 'stopped-fixture'}
                with patch('app.native.install.installation_root', return_value=self.root), \
                     patch('app.native.install.verify_program', return_value=release), \
                     patch('app.native.install.protect_data'), \
                     patch('app.native.install.service_exists', return_value=registered), \
                     patch('app.native.install.service_running', return_value=False), \
                     patch('app.native.install.backup_stopped_service', side_effect=backup) as stopped_backup, \
                     patch('app.native.install.require_free_port'), patch('app.native.install.wait_ready'), \
                     patch('app.native.install.checked') as commands:
                    install(self.program, data, wrapper)
                stopped_backup.assert_called_once()
                self.assertEqual('stopped-fixture', json.loads((data / 'upgrade-journal.json').read_text())['backup']['location_key'])
                self.assertFalse(any('app.maintenance.cli' in item.args[0] for item in commands.call_args_list))

    def test_stopped_instance_backup_failure_preserves_old_config_and_witness(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        original = {path: path.read_bytes() for path in (data / 'runtime.json', self.root / 'installation.json', data / 'installation.json')}
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program', return_value=release), \
             patch('app.native.install.protect_data'), patch('app.native.install.service_exists', return_value=False), \
             patch('app.native.install.backup_stopped_service', side_effect=RuntimeError('scm-start-denied')), \
             patch('app.native.install.checked') as commands:
            with self.assertRaisesRegex(RuntimeError, 'scm-start-denied'):
                install(self.program, data, wrapper)
        commands.assert_not_called()
        for path, value in original.items():
            self.assertEqual(value, path.read_bytes())
        self.assertEqual(b'old-wrapper', wrapper.read_bytes())

    def test_stopped_backup_service_is_temporary_localservice_and_removed_after_receipt(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        config = NativeConfig.read(data / 'runtime.json')
        commands = []
        def command(args, **kwargs):
            commands.append(args[-1])
            if args[-1] == 'start':
                node = ET.fromstring(Path(args[0]).with_suffix('.xml').read_text(encoding='utf-8'))
                self.assertEqual('LocalService', node.findtext('serviceaccount/user'))
                self.assertEqual('Manual', node.findtext('startmode'))
                self.assertEqual([], node.findall('onfailure'))
                self.assertNotIn('worker', node.findtext('arguments'))
                request = node.findtext('arguments').split('--backup-request ')[-1]
                atomic_json(data / 'control' / ('upgrade-backup-' + request + '.json'),
                    {'format': 1, 'request': request, 'status': 'passed',
                     'backup': {'integrity_state': 'passed', 'location_key': 'localservice-fixture'}})
            return ''
        with patch('app.native.install.require_free_port'), patch('app.native.install.checked', side_effect=command):
            receipt = backup_stopped_service(self.program, config, self.root)
        self.assertEqual('localservice-fixture', receipt['location_key'])
        self.assertEqual(['install', 'start', 'stopwait', 'uninstall'], commands)
        self.assertEqual([], list(self.root.glob('TianyanBackup-*.exe')))
        self.assertEqual(prior, json.loads((data / 'runtime.json').read_text()))

    def test_stopped_backup_cannot_launch_postgres_with_elevated_admin_identity(self):
        with patch('app.native.install.current_sid', return_value='S-1-5-21-1-2-3-500'), \
             patch('app.native.runtime.start_process') as start:
            with self.assertRaisesRegex(RuntimeError, 'requires_localservice'):
                run_stopped_backup(self.program, self.config.data, self.root / 'TianyanService.exe', '1' * 32)
        start.assert_not_called()

    def test_unavailable_service_registration_fails_without_admin_postgres_fallback(self):
        data, old, release, prior, wrapper = self.upgrade_fixture()
        config = NativeConfig.read(data / 'runtime.json')
        with patch('app.native.install.require_free_port'), \
             patch('app.native.install.service_exists', return_value=False), \
             patch('app.native.install.checked', side_effect=PermissionError('scm-denied')) as commands, \
             patch('app.native.runtime.start_process') as postgres:
            with self.assertRaisesRegex(PermissionError, 'scm-denied'):
                backup_stopped_service(self.program, config, self.root)
        self.assertEqual('install', commands.call_args.args[0][-1])
        postgres.assert_not_called()
        self.assertEqual(prior, json.loads((data / 'runtime.json').read_text()))
        self.assertEqual([], list(self.root.glob('TianyanBackup-*.exe')))

    def test_stopped_backup_helper_always_stops_owned_pg_and_never_starts_a_worker(self):
        for failed in (False, True):
            with self.subTest(backup_failed=failed):
                if (self.root / 'upgrade-data').exists():
                    import shutil
                    shutil.rmtree(self.root / 'upgrade-data')
                    shutil.rmtree(self.root / 'versions' / 'old-version')
                data, old, release, prior, wrapper = self.upgrade_fixture()
                database = MagicMock()
                database.poll.return_value = None
                result = RuntimeError('backup-failed') if failed else json.dumps({'integrity_state': 'passed', 'location_key': 'fixture'})
                with patch('app.native.install.current_sid', return_value='S-1-5-19'), \
                     patch('app.native.install.installation_root', return_value=self.root), \
                     patch('app.native.install.verify_program'), patch('app.native.install.require_free_port'), \
                     patch('app.native.runtime.start_process', return_value=database) as start, \
                     patch('app.native.database.wait_database'), patch('app.native.database.stop_database') as stop, \
                     patch('app.native.install.checked', side_effect=[result]):
                    if failed:
                        with self.assertRaisesRegex(RuntimeError, 'backup-failed'):
                            run_stopped_backup(self.program, data, wrapper, '2' * 32)
                    else:
                        run_stopped_backup(self.program, data, wrapper, '2' * 32)
                start.assert_called_once()
                self.assertTrue(start.call_args.args[2][0].endswith('postgres.exe'))
                stop.assert_called_once()
                database.wait.assert_called_once()
                self.assertEqual(prior, json.loads((data / 'runtime.json').read_text()))
                receipt = json.loads((data / 'control' / ('upgrade-backup-' + '2' * 32 + '.json')).read_text())
                self.assertEqual('failed' if failed else 'passed', receipt['status'])

    def restored_adoption_fixture(self):
        import hashlib
        prepare_directories(self.config)
        (self.config.data / 'database' / 'PG_VERSION').write_text('17')
        backup = self.root / 'backup-fixture'
        backup.mkdir()
        (backup / 'manifest.json').write_text('{}')
        manifest = {'app_mode': 'real', 'ai_master_sha256': hashlib.sha256(
            (self.config.data / 'master' / 'ai_master_key').read_bytes()).hexdigest()}
        atomic_json(self.config.data / 'restore-receipt.json', {'format': 1, 'status': 'passed',
            'collection_enabled': False, 'manifest_sha256': hashlib.sha256(b'{}').hexdigest(),
            'checks': ['backup-hashes', 'empty-instance-only', 'postgres-restore',
                       'append-only-privileges', 'credential-decryption', 'schema-upgrade']})
        return backup, manifest

    def test_restore_adoption_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'explicit_confirmation'):
            adopt_restored(self.program, self.config.data, self.root / 'TianyanService.exe',
                           restored_config=self.file, backup=self.root / 'unread-backup')
        self.assertFalse((self.root / 'installation.json').exists())

    def test_verified_restore_can_be_explicitly_adopted_without_enabling_collection(self):
        backup, manifest = self.restored_adoption_fixture()
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program'), patch('app.native.install.service_exists', return_value=False), \
             patch('app.native.install.owner_sid', return_value='S-1-5-21-1-2-3-1000'), \
             patch('app.native.install.protect_data'), patch('app.native.install.require_free_port'), \
             patch('app.native.install.verify_restore_backup', return_value=manifest) as verified, \
             patch('app.native.install.install') as register:
            adopt_restored(self.program, self.config.data, self.root / 'TianyanService.exe',
                           restored_config=self.file, backup=backup, confirmed=True)
        verified.assert_called_once_with(backup)
        register.assert_called_once()
        self.assertFalse(NativeConfig.read(self.config.data / 'runtime.json').collection_enabled)
        self.assertTrue((self.root / 'installation.json').is_file())

    def test_restore_adoption_rejects_a_receipt_for_different_backup_before_trusting_data(self):
        backup, manifest = self.restored_adoption_fixture()
        (backup / 'manifest.json').write_text('{"changed":true}')
        with patch('app.native.install.installation_root', return_value=self.root), \
             patch('app.native.install.verify_program'), patch('app.native.install.service_exists', return_value=False), \
             patch('app.native.install.verify_restore_backup', return_value=manifest), \
             patch('app.native.install.protect_data') as protect, patch('app.native.install.install') as register:
            with self.assertRaisesRegex(ValueError, 'evidence_invalid'):
                adopt_restored(self.program, self.config.data, self.root / 'TianyanService.exe',
                               restored_config=self.file, backup=backup, confirmed=True)
        protect.assert_not_called()
        register.assert_not_called()
        self.assertFalse((self.root / 'installation.json').exists())

    def test_recovery_requires_new_owned_child_and_post_kill_execution_heartbeat(self):
        killed = datetime.now(UTC)
        state = {'running': True, 'pid': 222, 'children': {'realtime': {'pid': 224, 'running': True}}}
        fresh = (killed + timedelta(seconds=1)).isoformat()
        stale = (killed - timedelta(seconds=1)).isoformat()
        identity = 'market:realtime:primary:' + socket.gethostname()
        def beat(seen, progress):
            return {'worker_id': identity, 'seen_at': seen, 'progress_at': progress}
        with patch('app.native.verify.parent_pid', return_value=222):
            self.assertTrue(role_recovered(state, 222, 223, killed, beat(fresh, fresh)))
            self.assertFalse(role_recovered(state, 222, 223, killed, beat(stale, stale)))
            self.assertFalse(role_recovered(state, 222, 223, killed, beat(fresh, stale)))
            self.assertFalse(role_recovered(state, 222, 224, killed, beat(fresh, fresh)))
        with patch('app.native.verify.parent_pid', return_value=999):
            self.assertFalse(role_recovered(state, 222, 223, killed, beat(fresh, fresh)))

    def test_each_slot_requires_its_own_post_replacement_execution_heartbeat(self):
        killed = datetime.now(UTC)
        observed = killed + timedelta(seconds=2)
        fresh = (observed + timedelta(seconds=1)).isoformat()
        old_process_beat = (killed + timedelta(seconds=1)).isoformat()
        state = {'running': True, 'pid': 222, 'children': {
            'realtime': {'pid': 224, 'running': True},
            'realtime-secondary': {'pid': 226, 'running': True}}}
        with patch('app.native.verify.parent_pid', return_value=222):
            for slot, old_pid, other in [('primary', 223, 'secondary'), ('secondary', 225, 'primary')]:
                with self.subTest(slot=slot):
                    beat = {'worker_id': 'market:realtime:' + slot + ':' + socket.gethostname(),
                            'seen_at': fresh, 'progress_at': fresh}
                    self.assertTrue(role_recovered(state, 222, old_pid, killed, beat, slot=slot, not_before=observed))
                    wrong_slot = {**beat, 'worker_id': 'market:realtime:' + other + ':' + socket.gethostname()}
                    self.assertFalse(role_recovered(state, 222, old_pid, killed, wrong_slot, slot=slot, not_before=observed))
                    stale = {**beat, 'seen_at': old_process_beat, 'progress_at': old_process_beat}
                    self.assertFalse(role_recovered(state, 222, old_pid, killed, stale, slot=slot, not_before=observed))

    def test_slot_probe_kills_only_each_owned_child_and_keeps_separate_proof(self):
        state_file = self.config.data / 'control' / 'runtime-status.json'
        state_file.parent.mkdir(parents=True)
        state = {'running': True, 'pid': 222, 'children': {
            'realtime': {'pid': 223, 'running': True},
            'realtime-secondary': {'pid': 225, 'running': True}}}
        atomic_json(state_file, state)
        process = SimpleNamespace(pid=222)
        def terminate(identity, sig):
            for child in state['children'].values():
                if child['pid'] == identity:
                    child['pid'] += 100
            atomic_json(state_file, state)
        def heartbeat(config, code, *, args):
            value = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
            self.assertEqual(self.config, config)
            self.assertEqual(1, len(args))
            return {'worker_id': 'market:realtime:' + args[0] + ':' + socket.gethostname(),
                    'seen_at': value, 'progress_at': value}
        with patch('app.native.verify.parent_pid', return_value=222), \
             patch('app.native.verify.os.kill', side_effect=terminate) as kill, \
             patch('app.native.verify.helper', side_effect=heartbeat) as probe, \
             patch('app.native.verify.ready'):
            proofs = [verify_slot_recovery(self.config, process, slot) for slot in ('primary', 'secondary')]
        self.assertEqual([223, 225], [call.args[0] for call in kill.call_args_list])
        self.assertEqual([('primary',), ('secondary',)], [call.kwargs['args'] for call in probe.call_args_list])
        self.assertEqual([('primary', 223, 323), ('secondary', 225, 325)],
                         [(item['slot'], item['old_pid'], item['new_pid']) for item in proofs])
        receipt = write_receipt(self.root, self.program, [], [], None, [], role_recoveries=proofs)
        self.assertEqual(proofs, receipt['role_recoveries'])

    def test_cleanup_failure_never_writes_passed_and_still_stops_other_instances(self):
        process = MagicMock()
        process.poll.return_value = 0
        processes = {'source': (process, self.config), 'restored': (process, self.config)}
        stopped = []
        def stop(name):
            stopped.append(name)
            if name == 'restored':
                raise RuntimeError('shutdown-failed')
        with patch('app.native.verify.require_free_port') as ports:
            errors = cleanup_owned(processes, stop)
        self.assertEqual(['restored', 'source'], stopped)
        self.assertEqual(4, ports.call_count)
        receipt = write_receipt(self.root, self.program, ['otherwise-passed'], [], None, errors)
        self.assertEqual('failed', receipt['status'])
        self.assertEqual('failed', json.loads((self.root / 'native-acceptance-receipt.json').read_text())['status'])

    def test_recovery_receipt_passes_only_after_all_owned_ports_are_released(self):
        process = MagicMock()
        process.poll.return_value = 0
        with patch('app.native.verify.require_free_port', side_effect=[None, OSError('still-listening')]):
            errors = cleanup_owned({'source': (process, self.config)}, lambda name: None)
        self.assertEqual('port-not-released', errors[0]['step'])
        self.assertEqual('failed', write_receipt(self.root, self.program, [], [], None, errors)['status'])


class NativeSecurityTests(unittest.TestCase):
    def test_other_user_uac_preserves_original_shell_owner_access(self):
        with patch.object(security, 'shell_owner_sid', return_value='S-1-5-21-1-2-3-1001'), \
             patch.object(security, 'current_sid', return_value='S-1-5-21-1-2-3-500') as current:
            self.assertEqual('S-1-5-21-1-2-3-1001', security.owner_sid())
        current.assert_not_called()

    def test_headless_install_falls_back_but_shell_lookup_failure_never_does(self):
        with patch.object(security, 'shell_owner_sid', return_value=None), \
             patch.object(security, 'current_sid', return_value='S-1-5-21-1-2-3-500'):
            self.assertEqual('S-1-5-21-1-2-3-500', security.owner_sid())
        with patch.object(security, 'shell_owner_sid', side_effect=PermissionError('shell-token-denied')), \
             patch.object(security, 'current_sid') as current:
            with self.assertRaises(PermissionError):
                security.owner_sid()
        current.assert_not_called()

    def test_reparse_ancestor_is_rejected_before_resolving_it(self):
        root = Path(tempfile.gettempdir()) / 'junction-parent-fixture'
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            if path == root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', metadata):
            with self.assertRaisesRegex(ValueError, 'reparse'):
                security.reject_reparse(root / 'nested' / 'private' / 'host.json')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL API')
    def test_exact_acl_removes_preexisting_everyone_grant_on_root_and_secret(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / 'private'
            folder.mkdir()
            secret = folder / 'secret.txt'
            secret.write_text('synthetic-fixture')
            with security._descriptor('D:P(A;OICI;FA;;;WD)') as (_, advapi, descriptor):
                advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
                for item in (folder, secret):
                    self.assertTrue(advapi.SetFileSecurityW(str(item), 0x80000004, descriptor))
            owner = security.current_sid()
            security.protect_data(folder, owner)
            kernel, advapi = security._libraries()
            advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p,
                wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
            for item in (folder, secret):
                descriptor = ctypes.c_void_p()
                self.assertEqual(0, advapi.GetNamedSecurityInfoW(str(item), 1, 4, None, None, None, None, ctypes.byref(descriptor)))
                value = wintypes.LPWSTR()
                try:
                    self.assertTrue(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                        descriptor, 1, 4, ctypes.byref(value), None))
                    self.assertIn('D:P', value.value)
                    self.assertNotIn(';;;WD)', value.value)
                    self.assertNotIn(';;;BU)', value.value)
                    self.assertIn(';;;' + owner + ')', value.value)
                    self.assertIn(';;;LS)', value.value)
                finally:
                    if value:
                        kernel.LocalFree(value)
                    kernel.LocalFree(descriptor)


if __name__ == '__main__':
    unittest.main()
