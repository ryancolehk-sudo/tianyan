#!/usr/bin/env python3
"""Offline Git safety preflight; never prints matching content.

--staged (the default) scans the complete index, not working-tree files.
--history scans all locally enumerable objects, including unreachable objects,
and the trees of local commits. It does not fetch remote refs or LFS content.
Exit codes: 0 = no rule hit; 1 = findings; 2 = incomplete/error.
This bounded heuristic scan does not replace image/OCR, data-rights, or review.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import zlib
from collections import Counter, defaultdict
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
MAX_TREE_PATHS = 200_000
ALLOWLIST = ".security-allowlist.json"
RULES = {"provider-key", "private-key", "jwt", "credential-url", "secret-literal"}
PATTERNS = (
    ("provider-key", re.compile(
        rb"(?<![A-Za-z0-9_-])(?:sk-(?:proj-|ant-(?:api[0-9]{2}-)?)?[A-Za-z0-9_-]{24,}"
        rb"|gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,}"
        rb"|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{35}|glpat-[A-Za-z0-9_-]{20,}"
        rb"|xox[baprs]-[A-Za-z0-9-]{20,})(?![A-Za-z0-9_-])")),
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}")),
    ("credential-url", re.compile(
        rb"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|https?)://"
        rb"[^\s:/@\x22\x27<>]{0,100}:[^\s/@\x22\x27<>]{1,200}@[^\s\x22\x27<>]+")),
)
LITERAL = re.compile(
    rb"(?i)(?<![A-Za-z0-9_])(?P<key>[A-Za-z_][A-Za-z0-9_]*)[\x22\x27]?"
    rb"[ \t]*[:=][ \t]*(?P<quote>[\x22\x27])(?P<value>[^\x22\x27\r\n]{1,512})(?P=quote)"
)
ENV_LITERAL = re.compile(
    rb"(?m)^[ \t]*(?:export[ \t]+)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*="
    rb"[ \t]*(?P<value>[^\s\x22\x27#][^\r\n#]*?)[ \t]*$"
)
SECRET_FIELD = re.compile(rb"(?i)(?:^|_)(?:api_key|access_token|refresh_token|token|password|passwd|secret|authorization|cookie)$")
PLACEHOLDER = re.compile(
    rb"(?i)^(?:<[^>]+>|\$\{[^}]+\}|\$[A-Z_][A-Z0-9_]*|"
    rb"YOUR_API_KEY|YOUR_TOKEN|CHANGE_ME|CHANGEME|REPLACE_ME)$"
)


class ScanError(Exception):
    """A diagnostic category only: never include Git stderr or file content."""


class Git:
    def __init__(self, repo: Path):
        self.repo = repo
        self.env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                        GIT_NO_LAZY_FETCH="1")
        self.process = None

    def run(self, *args: str) -> bytes:
        try:
            result = subprocess.run(["git", "--no-optional-locks", "-C", str(self.repo), *args],
                                    capture_output=True, env=self.env, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScanError("git-unavailable-or-timeout") from exc
        if result.returncode:
            raise ScanError("git-operation-failed")
        return result.stdout

    def read(self, oid: str) -> tuple[str, bytes]:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ScanError("invalid-object-id")
        if self.process is None:
            self.process = subprocess.Popen(
                ["git", "--no-optional-locks", "-C", str(self.repo), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.env,
            )
        try:
            self.process.stdin.write((oid + "\n").encode("ascii"))
            self.process.stdin.flush()
            header = self.process.stdout.readline().split()
            if len(header) != 3 or header[0].decode("ascii") != oid:
                raise ScanError("missing-or-unreadable-object")
            size = int(header[2])
            if size > MAX_BYTES:
                raise ScanError("object-size-limit")
            data = self.process.stdout.read(size)
            if len(data) != size or self.process.stdout.read(1) != b"\n":
                raise ScanError("truncated-object")
            return header[1].decode("ascii"), data
        except (OSError, ValueError, UnicodeError) as exc:
            raise ScanError("object-read-failed") from exc

    def close(self):
        if self.process is not None:
            self.process.stdin.close()
            self.process.stdout.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def private_path(path: str) -> str | None:
    parts = path.replace("\\", "/").casefold().split("/")
    name = parts[-1]
    if any(p in {"private", ".private"} or p.endswith((".private", "-private"))
           or ".private." in p for p in parts):
        return "private-path"
    if name in {"ai_master_key", "credentials.tar", "auth.json", "debug.log"} or name.endswith(".bundle"):
        return "private-runtime-data"
    if name.endswith(".env") and path not in {"infrastructure/images.lock.env", "infrastructure/cloud/images.lock.env"}:
        return "private-env"
    if name == ".env" or name.startswith(".env.") and name != ".env.example":
        return "private-env"
    if (parts[0] in {"credentials", "secrets", "logs", "cache"}
            or len(parts) > 1 and parts[0] == "data"
            and parts[1] in {"local", "cache", "market", "real", "accounts", "research"}):
        return "private-runtime-data"
    if any(p in {"backups", "backup", "output", "artifacts", "playwright-report", "test-results"}
           for p in parts[:-1]):
        return "generated-or-backup-path"
    if "/".join(parts).startswith(("docs/design/references/", "docs/design/evidence/")):
        return "private-design-material"
    if re.search(r"\.(?:db|db3|sqlite|sqlite3|dump|bak|backup)(?:\.(?:gz|zip|bz2|xz))?$", name):
        return "database-or-backup-file"
    if re.search(r"\.(?:pem|key|p12|pfx)$", name) or name in {"id_rsa", "id_ed25519"}:
        return "private-key-material"
    if re.fullmatch(r"(?:backup|dump|pg_dump)(?:[-_.].*)?\.sql(?:\.gz)?", name):
        return "database-or-backup-file"
    return None


def display_path(path: str) -> str:
    # JSON escaping also prevents filenames from injecting terminal control text.
    raw = path.encode("utf-8", errors="replace")
    for _, pattern in PATTERNS:
        raw = pattern.sub(b"[REDACTED]", raw)
    return raw.decode("utf-8", errors="replace")


def index_entries(git: Git) -> list[tuple[str, str, str]]:
    result = []
    for item in git.run("ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        metadata, raw_path = item.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise ScanError("unmerged-index")
        result.append((raw_path.decode("utf-8", errors="replace"), oid, mode))
    return result


def load_allowlist(git: Git, entries) -> set[tuple[str, str, str]]:
    """Use reviewed index policy in both modes, never an unstaged policy file."""
    oid = next((oid for path, oid, _ in entries if path == ALLOWLIST), None)
    if oid is None:
        return set()
    try:
        _, data = git.read(oid)
        value = json.loads(data)
        if value.get("version") != 1 or not isinstance(value.get("entries"), list):
            raise ValueError
        allowed = set()
        for item in value["entries"]:
            path, rule, digest, reason = (item[k] for k in ("path", "rule", "sha256", "reason"))
            if not all(isinstance(v, str) for v in (path, rule, digest, reason)):
                raise ValueError
            if not path or any(c in path for c in "*?[]\\") or rule not in RULES:
                raise ValueError
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or len(reason.strip()) < 8:
                raise ValueError
            allowed.add((path, rule, digest))
        return allowed
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise ScanError("invalid-staged-allowlist") from exc


def literal_matches(data: bytes):
    for pattern in (LITERAL, ENV_LITERAL):
        for match in pattern.finditer(data):
            key, value = match.group("key"), match.group("value").strip()
            if not SECRET_FIELD.search(key) or not value or PLACEHOLDER.fullmatch(value):
                continue
            if key.lower() == b"authorization":
                if re.fullmatch(rb"(?i)(?:Bearer|Basic)[ \t]+[A-Za-z0-9_+/=.-]{16,}", value):
                    yield "secret-literal", match.start("value"), value
                continue
            if key.lower() == b"cookie":
                if re.search(rb"(?i)(?:session|auth|token|sid)[A-Za-z0-9_.-]*=[A-Za-z0-9_+/=.-]{16,}", value):
                    yield "secret-literal", match.start("value"), value
                continue
            prefix = data[max(0, match.start() - 100):match.start()]
            delimiter = data[match.end("key"):match.start("value")]
            if (key.lower() == b"password" and b":" in delimiter
                    and re.search(rb"(?i)\b(?:LOGIN|ROLE)\b", prefix)
                    and re.fullmatch(rb"[A-Za-z_][A-Za-z0-9_]*", value)):
                continue  # psql :'variable' substitution, not a literal password.
            # Unquoted Python expressions, SQL references, and cookie names are
            # not credential literals. Strong formats above are always scanned.
            if pattern is ENV_LITERAL and not re.fullmatch(rb"[A-Za-z0-9_+/=.!@%^-]+", value):
                continue
            if not value.isascii() or b" " in value or value.startswith((b"$", b"{", b"/", b"http")):
                continue
            minimum = 8 if re.search(rb"(?i)(password|passwd)$", key) else 24
            if len(value) < minimum or len(set(value)) < 4:
                continue
            yield "secret-literal", match.start("value"), value


def content_matches(data: bytes, counts: Counter):
    for depth in range(3):
        if not data.startswith(b"\x1f\x8b"):
            break
        if depth == 2:
            raise ScanError("gzip-depth-limit")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(MAX_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ScanError("gzip-unreadable") from exc
        if len(data) > MAX_BYTES:
            raise ScanError("gzip-size-limit")
        counts["gzip_decoded"] += 1
    binary = b"\0" in data[:8192]
    counts["binary_text_only" if binary else "text_objects"] += 1
    for rule, pattern in PATTERNS:
        for match in pattern.finditer(data):
            yield rule, None if binary else data.count(b"\n", 0, match.start()) + 1, match.group()
    if not binary:
        for rule, position, value in literal_matches(data):
            yield rule, data.count(b"\n", 0, position) + 1, value


def history_objects(git: Git, counts: Counter):
    records = {}
    for line in git.run("cat-file", "--batch-all-objects",
                        "--batch-check=%(objectname) %(objecttype) %(objectsize)", "--unordered").splitlines():
        oid, kind, size = line.decode("ascii").split()
        records[oid] = (kind, int(size))
    counts["all_local_objects"] = len(records)
    counts["local_refs"] = len(git.run("for-each-ref", "--format=%(refname)").splitlines())
    trees = {}
    roots = set()
    child_trees = set()
    for oid, (kind, size) in records.items():
        if kind not in {"tree", "commit"}:
            continue
        if size > MAX_BYTES:
            raise ScanError("object-size-limit")
        _, data = git.read(oid)
        if kind == "commit":
            first = data.split(b"\n", 1)[0]
            if not first.startswith(b"tree "):
                raise ScanError("invalid-commit-tree")
            roots.add(first[5:].decode("ascii"))
            continue
        entries = []
        offset, hash_bytes = 0, len(oid) // 2
        while offset < len(data):
            nul = data.find(b"\0", offset)
            if nul < 0 or nul + 1 + hash_bytes > len(data):
                raise ScanError("invalid-tree-object")
            mode, name = data[offset:nul].split(b" ", 1)
            child = data[nul + 1:nul + 1 + hash_bytes].hex()
            entries.append((name.decode("utf-8", errors="replace"), child, mode))
            if mode == b"40000":
                child_trees.add(child)
            offset = nul + 1 + hash_bytes
        trees[oid] = entries
    roots.update(set(trees) - child_trees)
    paths = defaultdict(set)
    visited = set()
    stack = [(root, "", 0) for root in roots]
    while stack:
        tree, prefix, depth = stack.pop()
        if (tree, prefix) in visited:
            continue
        visited.add((tree, prefix))
        if depth > 128 or len(visited) > MAX_TREE_PATHS:
            raise ScanError("tree-traversal-limit")
        if tree not in trees:
            raise ScanError("missing-tree-object")
        for name, child, mode in trees[tree]:
            path = prefix + name
            if mode == b"40000":
                stack.append((child, path + "/", depth + 1))
            elif mode == b"160000":
                counts["submodule_entries_not_scanned"] += 1
            else:
                paths[child].add(path)
    return records, paths


def scan(repo: Path, history: bool) -> int:
    git = Git(repo)
    findings = set()
    counts = Counter()
    errors = set()
    try:
        git.run("rev-parse", "--git-dir")
        entries = index_entries(git)
        allowed = load_allowlist(git, entries)
        if history:
            if git.run("rev-parse", "--is-shallow-repository").strip() == b"true":
                errors.add("shallow-history")
            git.run("fsck", "--connectivity-only", "--no-dangling")
            records, paths = history_objects(git, counts)
            if counts["submodule_entries_not_scanned"]:
                errors.add("submodule-not-scanned")
        else:
            records, paths = {}, defaultdict(set)
            for path, oid, mode in entries:
                if mode == "160000":
                    errors.add("submodule-not-scanned")
                    continue
                records[oid] = ("blob", 0)
                paths[oid].add(path)
            counts["index_files"] = len(entries)
        for oid, (kind, size) in records.items():
            if kind not in {"blob", "commit", "tag"}:
                continue
            names = paths.get(oid) or {"<unmapped-" + kind + ">"}
            for path in names:
                if rule := private_path(path):
                    findings.add((path, 0, oid[:12], rule))
            if size > MAX_BYTES:
                errors.add("object-size-limit")
                continue
            _, data = git.read(oid)
            counts["content_objects"] += 1
            try:
                for rule, line, value in content_matches(data, counts):
                    digest = hashlib.sha256(value).hexdigest()
                    for path in names:
                        if (path, rule, digest) in allowed:
                            counts["exact_allowlist_matches"] += 1
                        else:
                            findings.add((path, line or 0, oid[:12], rule))
            except ScanError as exc:
                errors.add(str(exc))
                findings.add((next(iter(sorted(names))), 0, oid[:12], str(exc)))
    except (ScanError, OSError, ValueError, UnicodeError) as exc:
        errors.add(str(exc) if isinstance(exc, ScanError) else "scan-incomplete")
    finally:
        git.close()
    for path, line, oid, rule in sorted(findings):
        print(json.dumps({"path": display_path(path), "line": line or None,
                          "object": oid, "category": rule}, ensure_ascii=False))
    for category in sorted(errors):
        print(json.dumps({"category": category, "status": "incomplete"}))
    print(json.dumps({"mode": "history" if history else "staged", "findings": len(findings),
                      "incomplete": bool(errors), "counts": dict(counts),
                      "limits": "offline heuristic; no OCR, external LFS, remote-only refs, or non-gzip archive extraction"},
                     ensure_ascii=False))
    return 2 if errors else 1 if findings else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--staged", action="store_true", help="scan the complete staged index (default)")
    modes.add_argument("--history", action="store_true", help="scan all locally enumerable Git objects")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository or worktree directory")
    args = parser.parse_args()
    return scan(args.repo, args.history)


if __name__ == "__main__":
    raise SystemExit(main())
