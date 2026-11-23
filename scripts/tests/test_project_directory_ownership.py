"""Test cleanup cannot change a real instance or follow a private-folder symlink."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project


class TestDirectoryOwnership(unittest.TestCase):
    def test_writers_stop_before_ownership_repair_and_project_cleanup_runs_if_repair_fails(self):
        calls = []
        def compose(*args, envfile, **kwargs):
            self.assertRegex(project.config(envfile)['COMPOSE_PROJECT_NAME'], r'^jlpm-check-[a-f0-9]{10}$')
            self.assertEqual(project.config(envfile)['JL_APP_MODE'], 'demo')
            calls.append(args[0])
            if args == ('up', '-d', 'db'):
                raise RuntimeError('synthetic-startup-failure')
        def repair(*args, **kwargs):
            calls.append('ownership-repair')
            raise RuntimeError('synthetic-repair-failure')
        with patch.object(project.os, 'getuid', return_value=1001, create=True), \
                patch.object(project, 'require_locks'), patch.object(project, 'compose', compose), \
                patch.object(project, 'restore_test_bind_ownership', repair), \
                self.assertRaisesRegex(RuntimeError, 'synthetic-repair-failure'):
            project.test()
        self.assertEqual(calls, ['--profile', 'up', 'stop', 'ownership-repair', 'down'])

    def prepare(self, root, mode='demo'):
        envfile = root / '.env'
        project.initialize(envfile, mode, 'jlpm-check-a123456789', 49152)
        with envfile.open('a', encoding='utf-8') as stream:
            for key, child in [('JL_BACKUP_HOST_DIRECTORY', 'backups'), ('JL_UPDATE_HOST_DIRECTORY', 'updates')]:
                stream.write(key + '=' + (root / child).as_posix() + '\n')
        return envfile

    def test_real_instance_is_rejected_before_docker(self):
        with tempfile.TemporaryDirectory(prefix='jlpm-check-') as temporary:
            root = Path(temporary); envfile = self.prepare(root, 'real')
            with patch.object(project.os, 'getuid', return_value=1001, create=True), \
                    patch.object(project, 'compose') as docker, self.assertRaises(SystemExit):
                project.restore_test_bind_ownership(root, 'jlpm-check-a123456789', envfile=envfile, compose_command=docker)
            docker.assert_not_called()

    def test_real_or_changed_actual_bind_source_is_rejected_before_root_helper(self):
        with tempfile.TemporaryDirectory(prefix='jlpm-check-') as temporary:
            root = Path(temporary); envfile = self.prepare(root); (root / 'updates').mkdir()
            rendered = {'name': 'jlpm-check-a123456789', 'services': {'credential-init': {'volumes': [
                {'type': 'bind', 'source': str(root.parent / 'unrelated-private-updates'), 'target': '/workspace/updates'},
                {'type': 'bind', 'source': str(root / 'backups'), 'target': '/workspace/backups'}]}}}
            calls = []
            def compose(*args, **kwargs):
                calls.append(args)
                return SimpleNamespace(stdout=json.dumps(rendered))
            with patch.object(project.os, 'getuid', return_value=1001, create=True), self.assertRaises(SystemExit):
                project.restore_test_bind_ownership(root, 'jlpm-check-a123456789', envfile=envfile, compose_command=compose)
            self.assertEqual(calls, [('config', '--format', 'json')])

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlinked_bind_root_is_rejected_before_docker(self):
        with tempfile.TemporaryDirectory(prefix='jlpm-check-') as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary); envfile = self.prepare(root)
            try:
                (root / 'updates').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlink creation requires host permission')
            with patch.object(project.os, 'getuid', return_value=1001, create=True), \
                    patch.object(project, 'compose') as docker, self.assertRaises(SystemExit):
                project.restore_test_bind_ownership(root, 'jlpm-check-a123456789', envfile=envfile, compose_command=docker)
            docker.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'getuid') and os.getuid() == 0, 'root POSIX fixture requires isolated Linux container')
    def test_original_caller_can_remove_0700_application_directories_without_following_symlinks(self):
        # A real unprivileged subprocess must fail before the helper and succeed
        # afterwards; the target of an internal symlink keeps its owner/content.
        with tempfile.TemporaryDirectory(prefix='jlpm-check-') as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary); os.chown(root, 1001, 1001)
            updates = root / 'updates'; updates.mkdir(mode=0o700); os.chown(updates, 10001, 10001)
            private = updates / 'state'; private.write_text('synthetic-only'); private.chmod(0o600); os.chown(private, 10001, 10001)
            external = Path(outside) / 'unchanged'; external.write_text('outside-fixture'); external.chmod(0o600)
            (updates / 'link').symlink_to(external)
            def caller():
                os.setgroups([]); os.setgid(1001); os.setuid(1001)
            argv = [sys.executable, '-c', 'import shutil,sys;shutil.rmtree(sys.argv[1])', str(root)]
            self.assertNotEqual(subprocess.run(argv, preexec_fn=caller, capture_output=True).returncode, 0)
            subprocess.run([sys.executable, '-c', project.TEST_BIND_OWNERSHIP_CODE, '1001', '1001', str(updates)], check=True)
            self.assertEqual(updates.stat().st_mode & 0o777, 0o700)
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)
            self.assertEqual(external.stat().st_uid, 0); self.assertEqual(external.read_text(), 'outside-fixture')
            self.assertEqual(subprocess.run(argv, preexec_fn=caller, capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
