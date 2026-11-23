import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project
import source_distribution as source


class SourceDistributionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.outer = Path(scratch.name)
        self.root = self.outer / "extracted"
        self.root.mkdir()
        files = {}
        for name in source.REQUIRED:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture\n")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root / source.MANIFEST).write_bytes(json.dumps({"format": 1, "files": files}).encode())

    def test_download_uses_its_own_files_even_inside_another_git_repo(self):
        subprocess.run(["git", "init", "-q", str(self.outer)], check=True)
        (self.root / ".env").write_text("synthetic instance")
        (self.root / "backups").mkdir()
        (self.root / "backups/private-data").write_text("instance backup")
        self.assertRegex(source.source_revision(self.root), r"^[a-f0-9]{40}$")

    def test_missing_modified_and_unlisted_source_are_rejected(self):
        script = self.root / "scripts/project.py"
        for state in ("changed", "missing", "extra"):
            with self.subTest(state=state):
                script.write_bytes(b"fixture\n")
                if state == "changed":
                    script.write_text("changed")
                elif state == "missing":
                    script.unlink()
                else:
                    (self.root / "scripts/unlisted.py").write_text("extra")
                with self.assertRaises(SystemExit):
                    source.verify_download(self.root)

    def test_manifest_comes_from_index_and_git_start_requires_clean_source(self):
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.root), *args])
        git("init", "-q")
        git("config", "core.autocrlf", "false")
        git("add", "--", *sorted(source.REQUIRED))
        script = self.root / "scripts/project.py"
        script.write_text("unstaged")
        source.write_manifest(self.root)
        with self.assertRaises(SystemExit):
            source.verify_download(self.root)
        script.write_bytes(b"fixture\n")
        git("add", "--", source.MANIFEST)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
        self.assertEqual(source.source_revision(self.root), git("rev-parse", "HEAD").decode().strip())
        script.write_text("modified")
        with self.assertRaises(SystemExit):
            source.source_revision(self.root)

    def test_compose_uses_instance_config_and_filters_ambient_credentials(self):
        envfile = self.root / ".env"
        envfile.write_text("COMPOSE_PROJECT_NAME=unit-instance\n")
        ambient = dict(os.environ, JL_MODEL_API_KEY="synthetic", JL_MODEL_ENABLED="true",
                       DB_APP_PASSWORD="synthetic", COMPOSE_FILE="synthetic", WEB_BIND="0.0.0.0")
        with patch.object(project, "ROOT", self.root), patch.object(project, "run") as run:
            project.compose("config", envfile=envfile, env=ambient)
        argv = run.call_args.args[0]
        self.assertIn(str(envfile), argv)
        self.assertIn("unit-instance", argv)
        passed = run.call_args.kwargs["env"]
        self.assertTrue(all(name not in passed for name in ("JL_MODEL_API_KEY", "JL_MODEL_ENABLED",
                            "DB_APP_PASSWORD", "COMPOSE_FILE", "WEB_BIND")))


if __name__ == "__main__":
    unittest.main()
