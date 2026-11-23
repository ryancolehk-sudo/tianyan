"""Deterministic checks for the disposable acceptance clock, with no DB or app."""
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import acceptance_runtime as clock


class FixtureDateTime(datetime):
    wall = datetime(2026, 10, 20, 12, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.wall.astimezone(tz) if tz else cls.wall.replace(tzinfo=None)


class AcceptanceClockChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'clock.json'
        self.real = datetime(2026, 10, 20, 12, tzinfo=UTC)
        self.business = self.real + timedelta(days=2)
        self.project = 'jlpm-check-1234567890-acceptance-source'
        FixtureDateTime.wall = self.real
        self.enterContext(patch.object(clock, 'CLOCK', self.path))
        self.enterContext(patch.object(clock, 'datetime', FixtureDateTime))
        self.enterContext(patch.object(clock, 'last_seen', None))
        self.enterContext(patch.object(clock, 'clock_anchor', None, create=True))
        self.tick = self.enterContext(patch.object(clock, 'monotonic', return_value=100.0, create=True))
        self.enterContext(patch.dict(clock.os.environ, JL_TEST_PROJECT=self.project))
        self.write(self.business, self.real)

    def write(self, business, real, **extra):
        self.path.write_text(json.dumps({
            'project': self.project, 'business_at': business.isoformat(),
            'real_at': real.isoformat(), **extra,
        }))

    def read(self):
        return clock.business_now()

    def test_backward_wall_adjustment_does_not_rewind_business_time(self):
        first = self.read()
        FixtureDateTime.wall -= timedelta(seconds=10)
        self.tick.return_value = 101.0
        self.assertEqual(self.read(), first + timedelta(seconds=1))

    def test_forward_wall_adjustment_does_not_skip_business_time(self):
        first = self.read()
        FixtureDateTime.wall += timedelta(seconds=10)
        self.tick.return_value = 101.0
        self.assertEqual(self.read(), first + timedelta(seconds=1))

    def test_explicit_forward_fixture_starts_a_new_elapsed_anchor(self):
        self.read()
        FixtureDateTime.wall += timedelta(seconds=2)
        self.tick.return_value = 102.0
        later = self.business + timedelta(hours=1)
        self.write(later, FixtureDateTime.wall)
        self.assertEqual(self.read(), later)
        self.tick.return_value = 103.0
        self.assertEqual(self.read(), later + timedelta(seconds=1))

    def test_explicit_fixture_rewind_is_still_rejected(self):
        self.read()
        self.write(self.business - timedelta(hours=1), self.real)
        with self.assertRaisesRegex(RuntimeError, 'cannot rewind'):
            self.read()

    def test_project_timezone_and_fixture_range_guards_remain(self):
        cases = [
            (self.business, self.real, {'project': 'another-project'}, 'another disposable project'),
            (self.business.replace(tzinfo=None), self.real, {}, 'timezone aware'),
            (self.business + timedelta(days=15), self.real, {}, 'bounded fixture range'),
        ]
        for business, real, extra, reason in cases:
            with self.subTest(reason=reason):
                self.write(business, real, **extra)
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.read()


if __name__ == '__main__':
    unittest.main()
