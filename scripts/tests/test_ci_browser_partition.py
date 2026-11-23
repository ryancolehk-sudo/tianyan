from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_browser_partition import selected_specs
from project import browser_specs


class BrowserPartitionTests(unittest.TestCase):
    def test_every_active_file_runs_exactly_once_with_no_empty_group(self):
        expected = [path.name for path in browser_specs()]
        for total in (1, 2, 3, 4):
            with self.subTest(total=total):
                parts = [selected_specs(index, total) for index in range(total)]
                self.assertTrue(all(parts))
                flattened = [name for part in parts for name in part]
                self.assertEqual(len(flattened), len(set(flattened)))
                self.assertEqual(sorted(flattened), expected)
                self.assertIn('terminal.spec.ts', flattened)
                self.assertNotIn('workbench-v2.spec.ts', flattened)

    def test_invalid_partition_cannot_silently_run_zero_checks(self):
        for index, total in ((-1, 2), (2, 2), (0, 0), (0, 17), (True, 2), (0, False)):
            with self.subTest(index=index, total=total), self.assertRaises(ValueError):
                selected_specs(index, total)


if __name__ == '__main__':
    unittest.main()
