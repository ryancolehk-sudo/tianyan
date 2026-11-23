from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project


class SuiteSelection(unittest.TestCase):
    def test_default_keeps_every_active_legacy_and_current_browser_file(self):
        expected = sorted(path.name for path in (project.ROOT / 'frontend/tests/e2e').glob('*.spec.ts')
                          if path.name != 'workbench-v2.spec.ts')
        self.assertEqual([path.name for path in project.browser_specs()], expected)
        self.assertIn('terminal.spec.ts', expected)
        self.assertIn('auction-repair.spec.ts', expected)

    def test_rejects_path_traversal_missing_or_unmounted_tests_before_docker(self):
        for names in ([], ['../terminal.spec.ts'], ['/terminal.spec.ts'],
                      ['missing.spec.ts'], ['workbench-v2.spec.ts']):
            with self.subTest(names=names), self.assertRaises(SystemExit):
                project.browser_specs(names)
        with patch.object(project, 'compose') as docker, self.assertRaises(SystemExit):
            project.test(suite='backend', specs=['terminal.spec.ts'])
        docker.assert_not_called()

    def test_explicit_files_are_stable_and_deduplicated(self):
        names = ['terminal.spec.ts', 'auction-repair.spec.ts', 'terminal.spec.ts']
        self.assertEqual([path.name for path in project.browser_specs(names)],
                         ['auction-repair.spec.ts', 'terminal.spec.ts'])

    def test_missing_browser_inputs_cannot_silently_pass_zero_checks(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(project, 'ROOT', Path(folder)):
            with self.assertRaises(SystemExit):
                project.browser_specs()


if __name__ == '__main__':
    unittest.main()
