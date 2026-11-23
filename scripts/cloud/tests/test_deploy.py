"""Host-only tests. No Docker daemon, public server or market API is touched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

FILE = Path(__file__).resolve().parents[1] / 'deploy.py'
SPEC = importlib.util.spec_from_file_location('cloud_deploy', FILE)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

REVISION = 'a' * 40


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='tianyan-cloud-unit-')
        self.addCleanup(self.folder.cleanup)
        self.state = Path(self.folder.name) / 'private'

    def initialize(self):
        return deploy.initialize(self.state, domain='research.example.com', email='ops@example.com',
                                 project='tianyan-unit', revision=REVISION)

    def test_install_state_is_private_unique_and_outside_code(self):
        first = self.initialize()
        second = deploy.initialize(Path(self.folder.name) / 'second', domain='other.example.com',
            email='ops@example.com', project='tianyan-other', revision=REVISION)
        self.assertEqual(first['JL_PUBLIC_ORIGIN'], 'https://research.example.com')
        self.assertEqual(first['JL_COOKIE_SECURE'], 'true')
        for key in ('DB_ADMIN_PASSWORD', 'DB_APP_PASSWORD', 'DB_MIGRATE_PASSWORD', 'DB_MAINTENANCE_PASSWORD'):
            self.assertEqual(len(first[key]), 64)
            self.assertNotEqual(first[key], second[key])
        self.assertEqual(len({first[key] for key in first if key.startswith('DB_')}), 4)
        self.assertFalse(any(key in first for key in ('JL_TUSHARE_TOKEN', 'JL_MODEL_API_KEY')))
        self.assertEqual(deploy.read_env(self.state / 'cloud.env'), first)
        if os.name != 'nt':
            self.assertEqual((self.state / 'cloud.env').stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(deploy.DeploymentError):
            deploy.state_path(deploy.ROOT / 'private-cloud')

    def test_repeated_install_never_overwrites_accounts_or_passwords(self):
        self.initialize()
        before = (self.state / 'cloud.env').read_bytes()
        with self.assertRaises(deploy.DeploymentError):
            self.initialize()
        self.assertEqual((self.state / 'cloud.env').read_bytes(), before)

    def test_domain_and_project_cannot_inject_caddy_or_compose(self):
        for domain in ('127.0.0.1', 'localhost', 'https://example.com', 'ok.example.com\n:80',
                       'ok.example.com/foo', 'bad_underscore.example.com', '*.example.com', 'x.local'):
            with self.subTest(domain=domain), self.assertRaises(deploy.DeploymentError):
                deploy.validate_domain(domain)
        self.assertEqual(deploy.validate_domain('RESEARCH.Example.Com.'), 'research.example.com')
        with self.assertRaises(deploy.DeploymentError):
            deploy.initialize(self.state, domain='research.example.com', email='ops@example.com',
                              project='../../another', revision=REVISION)

    def test_environment_rejects_substitution_and_control_characters(self):
        self.state.mkdir()
        for value in ('secret\nOTHER=x', '${OTHER}', 'x\x00y', '$(command)'):
            with self.subTest(value=value), self.assertRaises(deploy.DeploymentError):
                deploy.write_env(self.state / 'cloud.env', {'JL_EXAMPLE': value})

    def test_untrusted_ambient_environment_cannot_override_selected_instance(self):
        self.initialize()
        hostile = {'COMPOSE_PROJECT_NAME': 'production-other', 'JL_TUSHARE_TOKEN': 'should-not-pass',
                   'DB_APP_PASSWORD': 'wrong-password', 'CADDY_IMAGE': 'untrusted:latest',
                   'POSTGRES_IMAGE': 'untrusted:latest', 'PATH': os.environ.get('PATH', '')}
        with patch.dict(os.environ, hostile), patch.object(deploy.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            deploy.compose(self.state, 'config', '--quiet')
        sent = run.call_args.kwargs['env']
        self.assertFalse(any(key in sent for key in hostile if key != 'PATH'))
        self.assertEqual(sent['COMPOSE_DISABLE_ENV_FILE'], 'true')
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--project-name') + 1], 'tianyan-unit')
        self.assertIn(self.state / 'cloud.env', [Path(args[i + 1]) for i, arg in enumerate(args) if arg == '--env-file'])

    def test_backup_stream_is_not_accidentally_captured_as_text(self):
        self.initialize()
        with tempfile.TemporaryFile() as stream, patch.object(deploy.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0)) as run:
            deploy.compose(self.state, 'run', 'maintenance-worker', stdout=stream)
        self.assertNotIn('capture_output', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.PIPE)

    def test_failed_docker_command_never_prints_interpolated_configuration(self):
        self.initialize()
        error = subprocess.CompletedProcess([], 1, 'DB_APP_PASSWORD=synthetic-secret', 'Token=secret')
        with patch.object(deploy.subprocess, 'run', return_value=error):
            with self.assertRaises(deploy.DeploymentError) as failure:
                deploy.compose(self.state, 'up')
        self.assertNotIn('synthetic-secret', str(failure.exception))
        self.assertNotIn('Token=', str(failure.exception))

    def test_preflight_rejects_published_database_or_api(self):
        self.initialize()
        model = {'services': {'db': {'ports': [{'target': 5432}]}, 'edge': {'ports': [{'target': 443}]}}}
        response = subprocess.CompletedProcess([], 0, json.dumps(model), '')
        with patch.object(deploy, 'compose', return_value=response):
            with self.assertRaises(deploy.DeploymentError):
                deploy.preflight(self.state, online=False)
        model['services']['db'].pop('ports')
        response.stdout = json.dumps(model)
        with patch.object(deploy, 'compose', return_value=response):
            result = deploy.preflight(self.state, online=False)
        self.assertEqual(result['dns'], 'not_checked')

    def test_build_rejects_revision_drift_before_starting_docker(self):
        self.initialize()
        with patch.object(deploy, 'current_revision', return_value='b' * 40), \
                patch.object(deploy, 'compose') as compose:
            with self.assertRaises(deploy.DeploymentError):
                deploy.build(self.state)
        compose.assert_not_called()

    def test_build_rejects_missing_or_mixed_revision_labels(self):
        self.initialize()
        image = [{'Id': 'sha256:' + 'b' * 64, 'Config': {'Labels': {}}}]
        before = (self.state / 'cloud.env').read_bytes()
        with patch.object(deploy, 'current_revision', return_value=REVISION), \
                patch.object(deploy, 'compose'), \
                patch.object(deploy.subprocess, 'check_output', return_value=json.dumps(image)):
            with self.assertRaises(deploy.DeploymentError):
                deploy.build(self.state)
        self.assertEqual((self.state / 'cloud.env').read_bytes(), before)
        self.assertFalse((self.state / 'release.json').exists())

    def test_existing_build_receipt_is_never_overwritten(self):
        self.initialize()
        (self.state / 'release.json').write_text('{}', encoding='utf-8')
        with patch.object(deploy, 'compose') as compose:
            with self.assertRaises(deploy.DeploymentError):
                deploy.build(self.state)
        compose.assert_not_called()

    def test_restore_cannot_select_another_instances_environment(self):
        self.initialize()
        adapter = deploy.project_adapter(self.state)
        with self.assertRaises(deploy.DeploymentError):
            adapter.compose('up', envfile=Path(self.folder.name) / 'different.env')

    def test_interrupted_build_resumes_same_images_without_changing_database_secrets(self):
        original = self.initialize()
        images = {role: 'sha256:' + str(index) * 64 for index, role in enumerate(deploy.IMAGE_KEYS, 1)}
        receipt = {'revision': REVISION, 'images': images}
        values = {**original, **{key: images[role] for role, key in deploy.IMAGE_KEYS.items()}}
        replace = os.replace
        def lose_power(source, destination):
            if Path(destination).name == 'cloud.env':
                raise OSError('synthetic process interruption')
            return replace(source, destination)
        with patch.object(deploy.os, 'replace', side_effect=lose_power):
            with self.assertRaises(OSError):
                deploy.persist_release(self.state, values, receipt)
        self.assertTrue((self.state / 'release.pending.json').exists())
        self.assertEqual(deploy.read_env(self.state / 'cloud.env'), original)
        def inspect(args, **kwargs):
            return json.dumps([{'Id': args[-1], 'Config': {'Labels': {'org.opencontainers.image.revision': REVISION}}}])
        with patch.object(deploy, 'current_revision', return_value=REVISION), \
                patch.object(deploy.subprocess, 'check_output', side_effect=inspect), \
                patch.object(deploy, 'compose') as compose:
            restored = deploy.build(self.state)
        compose.assert_not_called()
        self.assertEqual(restored, receipt)
        self.assertFalse((self.state / 'release.pending.json').exists())
        self.assertTrue((self.state / 'release.json').exists())
        final = deploy.read_env(self.state / 'cloud.env')
        for key in original:
            if key.startswith('DB_'):
                self.assertEqual(final[key], original[key])

    def test_readiness_waits_for_database_and_each_worker_role(self):
        self.initialize()
        with patch.object(deploy, 'compose', side_effect=[deploy.DeploymentError('not_ready'), object()]) as compose, \
                patch.object(deploy.time, 'sleep'):
            deploy.wait_ready(self.state)
        self.assertEqual(compose.call_count, 2)
        self.assertIn('/health/ready', compose.call_args.args[-1])

    def test_release_receipt_does_not_accept_floating_tags(self):
        values = self.initialize()
        receipt = {'revision': REVISION, 'images': {role: 'latest' for role in deploy.IMAGE_KEYS}}
        with patch.object(deploy.subprocess, 'check_output') as inspect:
            with self.assertRaises(deploy.DeploymentError):
                deploy.verify_images(values, receipt)
        inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
