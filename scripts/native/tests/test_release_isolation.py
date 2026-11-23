"""Release boundaries exercised with a disposable Git repository and fake keys."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'release_isolation_builder', Path(__file__).resolve().parents[1] / 'build_windows.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch_root = patch.object(builder, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.git('init', '-q')
        self.source = self.root / 'backend/app'
        self.source.mkdir(parents=True)
        (self.source / 'main.py').write_text('VALUE = 1\n', encoding='utf-8')
        self.git('add', 'backend/app/main.py')

    def git(self, *args, **kwargs):
        return subprocess.check_output(['git', *args], cwd=self.root, **kwargs)

    def test_ignored_and_untracked_private_files_do_not_enter_program(self):
        (self.root / '.gitignore').write_text('.env\n*.key\n', encoding='utf-8')
        for name in ('.env', 'operator.key', 'local-notes.txt'):
            (self.source / name).write_text('synthetic private fixture', encoding='utf-8')
        destination = self.root / 'package'
        builder.copy_tracked_tree('backend/app', destination)
        self.assertEqual([p.name for p in destination.iterdir()], ['main.py'])
        self.assertEqual((destination / 'main.py').read_bytes(), (self.source / 'main.py').read_bytes())

    def test_git_symlink_entry_is_rejected_even_if_worktree_file_is_regular(self):
        oid = self.git('hash-object', '-w', '--stdin', input=b'../../private.env').decode().strip()
        self.git('update-index', '--add', '--cacheinfo', f'120000,{oid},backend/app/link.py')
        (self.source / 'link.py').write_text('synthetic fixture', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unsafe_tracked_program_input'):
            builder.copy_tracked_tree('backend/app', self.root / 'package')

    def test_untracked_source_directory_cannot_be_shipped(self):
        with self.assertRaisesRegex(ValueError, 'tracked_program_inputs_missing'):
            builder.copy_tracked_tree('database', self.root / 'package')

    def test_ignored_public_private_file_is_rejected(self):
        source = self.root / 'frontend/public'
        source.mkdir(parents=True)
        (self.root / '.gitignore').write_text('*.key\n', encoding='utf-8')
        (source / 'operator.key').write_text('synthetic private fixture', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unreviewed_frontend_input_not_allowed_in_release'):
            builder.check_frontend_release_inputs()

    def test_untracked_public_file_is_rejected(self):
        source = self.root / 'frontend/public'
        source.mkdir(parents=True)
        (source / 'local-notes.txt').write_text('synthetic private fixture', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unreviewed_frontend_input_not_allowed_in_release'):
            builder.check_frontend_release_inputs()

    def test_tracked_sources_and_replaced_build_directories_are_allowed(self):
        source = self.root / 'frontend'
        source.mkdir()
        (source / 'index.html').write_text('<div>synthetic</div>', encoding='utf-8')
        self.git('add', 'frontend/index.html')
        (self.root / '.gitignore').write_text('node_modules/\ndist/\n', encoding='utf-8')
        for name in ('node_modules', 'dist'):
            (source / name).mkdir()
            (source / name / 'generated.txt').write_text('generated fixture', encoding='utf-8')
        builder.check_frontend_release_inputs()

    def frontend_check(self):
        with patch.object(builder.shutil, 'which', side_effect=lambda name: name), \
                patch.object(builder.subprocess, 'check_output', return_value='v24.13.0\n'), \
                patch.object(builder.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'private_frontend_configuration_not_allowed_in_release'):
                builder.prepare_frontend()
            run.assert_not_called()

    def test_ignored_frontend_dotenv_is_rejected_before_npm(self):
        source = self.root / 'frontend'
        source.mkdir()
        (source / '.env.production.local').write_text('VITE_TEST=synthetic\n', encoding='utf-8')
        self.frontend_check()

    def test_shell_vite_configuration_is_rejected_before_npm(self):
        with patch.dict(os.environ, {'VITE_TEST': 'synthetic'}):
            self.frontend_check()


if __name__ == '__main__':
    unittest.main()
