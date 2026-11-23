"""Build a source manifest from the staged release and verify extracted downloads."""
import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "source-manifest.json"
REQUIRED = {"compose.yaml", "scripts/project.py", "dependencies/lock-manifest.json"}
RUNTIME_DIRS = {"backups", "updates", "maintenance-status", ".updater", "artifacts", "output"}
BUILD_DIRS = {"__pycache__", "node_modules", "dist", ".venv", ".pytest_cache"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def write_manifest(root):
    if not (root / ".git").exists():
        raise SystemExit("生成清单需要在本项目的 Git 工作区暂存发布文件。")
    tree = git(root, "write-tree").decode().strip()
    payload = git(root, "archive", "--format=zip", tree)
    with zipfile.ZipFile(io.BytesIO(payload)) as staged:
        files = {item.filename: hashlib.sha256(staged.read(item)).hexdigest()
                 for item in staged.infolist() if not item.is_dir() and item.filename != MANIFEST}
    if not REQUIRED.issubset(files):
        raise SystemExit("暂存区缺少发布所需文件。")
    body = json.dumps({"format": 1, "files": files}, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    (root / MANIFEST).write_text(body, encoding="utf-8", newline="\n")


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def verify_download(root):
    manifest = root / MANIFEST
    if not manifest.is_file() or is_link(manifest):
        raise SystemExit("缺少有效的源码清单，请重新下载完整源码包。")
    raw = manifest.read_bytes()
    try:
        data = json.loads(raw)
        files = data["files"]
        if data["format"] != 1 or not isinstance(files, dict) or not REQUIRED.issubset(files):
            raise ValueError()
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in files.values()):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SystemExit("源码清单格式无效。") from None
    seen = set()
    for folder, dirs, names in os.walk(root):
        base = Path(folder)
        for name in list(dirs):
            if name in BUILD_DIRS or base == root and name in RUNTIME_DIRS | {".git"}:
                dirs.remove(name)
            elif is_link(base / name):
                raise SystemExit("源码目录包含链接：" + (base / name).relative_to(root).as_posix())
        for name in names:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if rel in {MANIFEST, ".env", ".git"}:
                continue
            if rel not in files or is_link(path) or not path.is_file():
                raise SystemExit("源码包包含额外文件或链接：" + rel)
            if hashlib.sha256(path.read_bytes()).hexdigest() != files[rel]:
                raise SystemExit("源码内容与清单不符：" + rel)
            seen.add(rel)
    missing = files.keys() - seen
    if missing:
        raise SystemExit("源码包缺少文件：" + min(missing))
    # The runtime's revision field uses 40 hexadecimal characters.
    return hashlib.sha256(raw).hexdigest()[:40]


def source_revision(root):
    if not (root / ".git").exists():
        return verify_download(root)
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise SystemExit("Git 工作区有改动，请先提交需要部署的源码或使用完整源码包。")
    return git(root, "rev-parse", "--verify", "HEAD").decode().strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["write", "check"])
    if parser.parse_args().action == "write":
        write_manifest(ROOT)
    else:
        print("源码清单校验通过", verify_download(ROOT))
