"""Frontend release provenance checks; no downloads or real build processes."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch


spec = importlib.util.spec_from_file_location(
    'tianyan_windows_builder', Path(__file__).resolve().parents[1] / 'build_windows.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WindowsBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'frontend'
        self.source.mkdir()
        (self.source / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3}), encoding='utf-8')
        self.dist = self.source / 'dist'
        self.dist.mkdir()
        (self.dist / 'index.html').write_text('stale webpage', encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(builder, 'ROOT', self.root).start()
        patch.object(builder.sys, 'platform', 'win32').start()
        self.which = patch.object(builder.shutil, 'which', side_effect=lambda name: name).start()
        self.node_version = patch.object(builder.subprocess, 'check_output', return_value='v24.13.0\n').start()
        self.run = patch.object(builder.subprocess, 'run').start()
        self.input_guard = patch.object(builder, 'check_frontend_release_inputs').start()

    def test_release_installs_locked_tools_then_rebuilds_instead_of_reusing_dist(self):
        def completed(command, **kwargs):
            if 'run' in command:
                (self.dist / 'index.html').write_text('newly built webpage', encoding='utf-8')

        self.run.side_effect = completed
        output, proof = builder.prepare_frontend()

        self.input_guard.assert_called_once_with()
        self.assertEqual(output, self.dist)
        self.assertEqual((output / 'index.html').read_text(encoding='utf-8'), 'newly built webpage')
        self.node_version.assert_called_once_with(['node', '--version'], cwd=self.root, text=True)
        self.assertEqual(self.run.call_args_list, [
            call(['npm.cmd', 'ci', '--include=dev', '--no-audit', '--no-fund'], check=True, cwd=self.source),
            call(['npm.cmd', 'run', 'build', '--', '--emptyOutDir'], check=True, cwd=self.source),
        ])
        self.assertEqual(proof, {'method': 'npm-ci-and-production-build', 'node_version': 'v24.13.0',
                                 'package_lock_sha256': builder.digest(self.source / 'package-lock.json')})

    def test_failed_production_build_cannot_fall_back_to_existing_dist(self):
        self.run.side_effect = [None, subprocess.CalledProcessError(1, 'npm build')]
        with self.assertRaises(subprocess.CalledProcessError):
            builder.prepare_frontend()
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual((self.dist / 'index.html').read_text(encoding='utf-8'), 'stale webpage')

    def test_failed_locked_install_never_runs_build_or_uses_existing_dist(self):
        self.run.side_effect = subprocess.CalledProcessError(1, 'npm ci')
        with self.assertRaises(subprocess.CalledProcessError):
            builder.prepare_frontend()
        self.assertEqual(self.run.call_count, 1)

    def test_wrong_node_major_is_rejected_before_install(self):
        for version in ('v23.12.0', 'v25.0.0', 'v24.0.0-rc.1', 'unknown'):
            with self.subTest(version=version):
                self.node_version.return_value = version
                with self.assertRaisesRegex(ValueError, 'node_24_builder_required'):
                    builder.prepare_frontend()
        self.run.assert_not_called()

    def test_missing_node_or_npm_fails_before_build(self):
        for missing in ('node', 'npm.cmd'):
            with self.subTest(missing=missing):
                self.which.side_effect = lambda name: None if name == missing else name
                with self.assertRaisesRegex(ValueError, 'node_24_and_npm_builder_required'):
                    builder.prepare_frontend()
        self.run.assert_not_called()

    def test_external_frontend_is_refused_for_release_before_downloads(self):
        arguments = SimpleNamespace(frontend=self.dist, development=False)
        with patch.object(builder.sys, 'version_info', (3, 14, 0)), \
                patch.object(builder, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'external_frontend_requires_development'):
                builder.build(arguments)
        download.assert_not_called()
        self.run.assert_not_called()
        self.node_version.assert_not_called()

    def test_build_refuses_directories_resolving_outside_source_before_npm_cleanup(self):
        original_resolve = Path.resolve
        for name in ('node_modules', 'dist'):
            with self.subTest(name=name):
                def resolved(path, *args, **kwargs):
                    if path == self.source / name:
                        return self.root / 'outside-frontend'
                    return original_resolve(path, *args, **kwargs)

                with patch.object(Path, 'resolve', autospec=True, side_effect=resolved):
                    with self.assertRaisesRegex(ValueError, 'frontend_build_directory_outside_source'):
                        builder.prepare_frontend()
        self.run.assert_not_called()

    def test_external_development_frontend_is_explicitly_identified(self):
        output, proof = builder.prepare_frontend(self.dist, development=True)
        self.assertEqual(output, self.dist)
        self.assertEqual(proof['method'], 'development-external')
        self.run.assert_called_once_with(
            ['npm.cmd', 'ci', '--include=dev', '--no-audit', '--no-fund'], check=True, cwd=self.source)

    def test_successful_command_without_web_output_is_not_packaged(self):
        (self.dist / 'index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'production_frontend_dist_required'):
            builder.prepare_frontend()


if __name__ == '__main__':
    unittest.main()
