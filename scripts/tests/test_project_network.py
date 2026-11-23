"""Operator test networking stays confined to its disposable project."""
import argparse
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project


class TestNetworking(unittest.TestCase):
    def test_rejects_public_special_host_and_oversized_networks(self):
        for value in ('8.8.8.0/24', '127.0.0.0/24', '0.0.0.0/24',
                      '169.254.1.0/24', '10.1.1.1/24', '10.0.0.0/8',
                      '10.1.1.0/29', '::1/128', 'not-a-network'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                project.test_network(value)
        self.assertEqual(project.test_network('10.253.90.0/24'), '10.253.90.0/24')
        self.assertEqual(project.test_network('172.20.0.0/16'), '172.20.0.0/16')

    def test_override_is_temporary_and_used_for_failure_cleanup(self):
        seen = []
        override_paths = []

        def fake_compose(*args, envfile, extra_files, **kwargs):
            self.assertRegex(project.config(envfile)['COMPOSE_PROJECT_NAME'], r'^jlpm-check-[a-f0-9]{10}$')
            self.assertEqual(len(extra_files), 1)
            override_paths.extend(extra_files)
            self.assertIn('subnet: 10.253.90.0/24', extra_files[0].read_text())
            seen.append(args)
            if args[0] != 'down':
                raise RuntimeError('simulated-build-failure')

        with patch.object(project, 'require_locks'), patch.object(project, 'compose', fake_compose):
            with self.assertRaisesRegex(RuntimeError, 'simulated-build-failure'):
                project.test('10.253.90.0/24')
        self.assertEqual(seen, [('--profile', 'test', 'build'), ('down', '--volumes', '--remove-orphans')])
        self.assertEqual(len(set(override_paths)), 1)
        self.assertFalse(override_paths[0].exists())

    def test_default_retains_docker_automatic_network_allocation(self):
        def fake_compose(*args, envfile, extra_files, **kwargs):
            self.assertEqual(extra_files, ())
            if args[0] != 'down':
                raise RuntimeError('simulated-build-failure')
        with patch.object(project, 'require_locks'), patch.object(project, 'compose', fake_compose):
            with self.assertRaises(RuntimeError):
                project.test()


if __name__ == '__main__':
    unittest.main()
