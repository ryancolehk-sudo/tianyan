"""Standard-library offline regression tests for the Git safety preflight."""

import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_security.py"


def generated_key():
    # Runtime assembly prevents the fixture source itself containing a key.
    return "sk-" + "Pr4n8Sm2C7x5K9v6Q3b1Z0aD" * 2


class SecurityCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tianyan-security-test-")
        self.repo = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, data=None):
        result = subprocess.run(["git", "-C", str(self.repo), *args], input=data,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, "temporary repository operation failed")
        return result.stdout

    def stage(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        self.git("add", "--", path)

    def check(self, history=False):
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                                 "--history" if history else "--staged"], capture_output=True,
                                text=True, encoding="utf-8")
        self.assertFalse(bool(result.stderr), "scanner must not print raw diagnostic stderr")
        return result.returncode, result.stdout

    def assert_no_leak(self, value, output):
        self.assertFalse(value in output, "diagnostic contained a matching value")

    def test_staged_snapshot_not_unstaged_worktree(self):
        key = generated_key()
        self.stage("settings.json", '{"mode":"demo"}')
        (self.repo / "settings.json").write_text(json.dumps({"api_key": key}))
        self.assertEqual(self.check()[0], 0)
        self.git("add", "settings.json")
        (self.repo / "settings.json").write_text('{"mode":"demo"}')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("provider-key", output)
        self.assert_no_leak(key, output)

    def test_private_and_generated_paths_are_blocked(self):
        names = ["settings.private.json", "instance.private/settings.json", "private/settings.json",
                 ".env", ".env.production", "personal.sqlite3", "database.dump.gz",
                 "backups/receipt.json", "output/result.json", "docs/design/references/private.png",
                 "logs/current.log", "data/accounts/records.json", "credentials/local.json", "auth.pfx",
                 "ai_master_key", "nested/credentials.tar", "auth.json", "debug.log", "private-copy.bundle",
                 "raw.env", "docs/design/evidence/capture.log"]
        for name in names:
            self.stage(name, "{}")
        code, output = self.check()
        self.assertEqual(code, 1)
        for name in names:
            self.assertIn(name, output)

    def test_normal_json_csv_fixtures_and_env_example_allowed(self):
        self.stage("settings.json", '{"mode":"demo","session_cookie":"research_demo_session"}')
        self.stage("sample.csv", "symbol,value\nSYNTHETIC,1\n")
        self.stage("backend/tests/fixtures/input.json", '{"synthetic":true}')
        self.stage(".env.example", "DB_PASSWORD=\nMODEL_API_KEY=\n")
        self.stage("infrastructure/images.lock.env", "PYTHON_IMAGE=example.invalid/python\n")
        self.stage("dependencies/report.json.gz", gzip.compress(b'{"sha256":"' + b"a1" * 32 + b'"}'))
        self.stage("init.sql", "CREATE ROLE synthetic LOGIN PASSWORD :'app_password';\n")
        self.assertEqual(self.check()[0], 0)

    def test_deleted_secret_is_still_found_in_history(self):
        key = generated_key()
        self.stage("removed.txt", key)
        self.git("commit", "--quiet", "-m", "synthetic fixture")
        self.git("rm", "--quiet", "removed.txt")
        self.git("commit", "--quiet", "-m", "remove synthetic fixture")
        self.assertEqual(self.check()[0], 0)
        code, output = self.check(history=True)
        self.assertEqual(code, 1)
        self.assertIn("removed.txt", output)
        self.assert_no_leak(key, output)

    def test_unreachable_blob_is_scanned(self):
        key = generated_key()
        self.stage("readme.txt", "synthetic")
        self.git("commit", "--quiet", "-m", "fixture")
        self.git("hash-object", "-w", "--stdin", data=key.encode())
        code, output = self.check(history=True)
        self.assertEqual(code, 1)
        self.assertIn("unmapped-blob", output)
        self.assert_no_leak(key, output)

    def test_gzip_is_scanned_and_values_never_reported(self):
        key = generated_key()
        self.stage("report.log.gz", gzip.compress(("first line\n" + key).encode()))
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn('"line": 2', output)
        self.assert_no_leak(key, output)

    def test_invalid_gzip_is_incomplete(self):
        self.stage("report.log.gz", b"\x1f\x8b" + b"invalid")
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn("gzip-unreadable", output)

    def test_invalid_deflate_is_incomplete(self):
        data = bytearray(gzip.compress(b"synthetic"))
        data[10] = 0x07  # Invalid DEFLATE block type, after the gzip header.
        self.stage("report.log.gz", bytes(data))
        code, output = self.check()
        self.assertEqual(code, 2)
        self.assertIn("gzip-unreadable", output)

    def test_allowlist_is_exact_and_uses_only_staged_policy(self):
        path = "synthetic.py"
        url = "https://" + "user:password" + "@example.invalid/api"
        self.stage(path, 'endpoint = "' + url + '"\n')
        self.assertEqual(self.check()[0], 1)
        policy = {"version": 1, "entries": [{"path": path, "rule": "credential-url",
                  "sha256": hashlib.sha256(url.encode()).hexdigest(),
                  "reason": "Synthetic rejection case; exact fixture only."}]}
        (self.repo / ".security-allowlist.json").write_text(json.dumps(policy))
        self.assertEqual(self.check()[0], 1, "unstaged policy must not bypass the check")
        self.git("add", ".security-allowlist.json")
        self.assertEqual(self.check()[0], 0)
        changed = url.replace("password", "N8p2Q5c7X9r3B6z1")
        self.stage(path, 'endpoint = "' + changed + '"\n')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assert_no_leak(changed, output)

    def test_env_example_still_has_content_checks(self):
        key = generated_key()
        self.stage(".env.example", "MODEL_API_KEY=" + key + "\n")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assert_no_leak(key, output)

    def test_plain_password_literal_is_blocked(self):
        value = "Lo7n" + "gPassword!39"
        self.stage("settings.json", json.dumps({"db_password": value}))
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("secret-literal", output)
        self.assert_no_leak(value, output)

    def test_literal_login_headers_are_blocked(self):
        value = "N8p2" + "Q5c7X9r3B6z1D4m8"
        self.stage("headers.json", json.dumps({"Authorization": "Bearer " + value,
                   "Cookie": "session=" + value}))
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("secret-literal", output)
        self.assert_no_leak(value, output)


if __name__ == "__main__":
    unittest.main()
