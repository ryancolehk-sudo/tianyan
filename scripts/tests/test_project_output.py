import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project


class RedirectedOutput(unittest.TestCase):
    def test_successful_browser_diagnostics_survive_windows_code_page(self):
        output, errors = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding='gbk')
        stderr = io.TextIOWrapper(errors, encoding='gbk')
        with patch.object(project.sys, 'stdout', stdout), patch.object(project.sys, 'stderr', stderr):
            project.configure_output()
            print('✓ 页面检查通过', flush=True)
            print('⚠ 合成诊断', file=project.sys.stderr, flush=True)
        self.assertEqual(output.getvalue().decode('utf-8').splitlines(), ['✓ 页面检查通过'])
        self.assertEqual(errors.getvalue().decode('utf-8').splitlines(), ['⚠ 合成诊断'])


if __name__ == '__main__':
    unittest.main()
