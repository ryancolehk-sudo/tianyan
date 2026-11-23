"""Run directly with Python; no application startup, database, or test fixtures."""

import io
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.access_logging import PrivateAccessLog


class AccessLoggingTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        handler = logging.StreamHandler(self.output)
        handler.setFormatter(logging.Formatter("%(message)s"))
        # A private logger exercises the actual logging pipeline without changing
        # application loggers or importing app.main and its database dependencies.
        self.logger = logging.Logger("uvicorn.access")
        self.logger.addHandler(handler)
        self.logger.addFilter(PrivateAccessLog())

    def emit(self, target, method="GET", status=200):
        self.logger.info('%s - "%s %s HTTP/%s" %d',
                         "127.0.0.1:1234", method, target, "1.1", status)
        return self.output.getvalue().strip()

    def test_oauth_callback_query_is_absent_from_formatted_log(self):
        value = self.emit("/api/v1/chatgpt/callback?code=synthetic-code&state=synthetic-state", status=303)
        self.assertEqual(value, '127.0.0.1:1234 - "GET /api/v1/chatgpt/callback HTTP/1.1" 303')

    def test_business_and_error_requests_never_log_query_parameters(self):
        for method, status, target in [
            ("GET", 200, "/api/v1/instruments?q=synthetic-search&filter=synthetic-private"),
            ("GET", 403, "/api/v1/review-data/notes?day=2099-01-01&cursor=synthetic-cursor"),
            ("POST", 422, "/api/v1/integrations?api_key=synthetic-only&api_key=synthetic-again"),
            ("GET", 404, "/missing?value=synthetic%3Fencoded%26data"),
        ]:
            with self.subTest(method=method, status=status):
                self.output.seek(0)
                self.output.truncate(0)
                value = self.emit(target, method, status)
                self.assertEqual(value, f'127.0.0.1:1234 - "{method} {target.split("?", 1)[0]} HTTP/1.1" {status}')

    def test_queryless_and_percent_encoded_paths_are_preserved(self):
        for target in ["/health/live", "/api/v1/items/a%3Fb", "/api/v1/items/a%3Fb?"]:
            with self.subTest(target=target):
                self.output.seek(0)
                self.output.truncate(0)
                value = self.emit(target)
                self.assertEqual(value, f'127.0.0.1:1234 - "GET {target.split("?", 1)[0]} HTTP/1.1" 200')

    def test_unrecognized_log_shapes_fail_closed(self):
        self.logger.info("unstructured /api/v1/items?value=synthetic-private")
        self.logger.info("%s", "/api/v1/items?value=synthetic-private")
        self.logger.info("%(target)s", {"target": "/api/v1/items?value=synthetic-private"})
        self.emit(None)
        self.assertEqual(self.output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
