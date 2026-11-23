"""Credentials never enter PostgreSQL: only encrypted, randomly named vault objects do."""
import hashlib
import hmac
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import APIError
from app.config import settings

# Shared by credential writers and the independent backup process. Writers hold
# a transaction lock through the caller's DB commit, rather than just a file IO
# critical section. Backup acquires the exclusive counterpart before snapshotting.
CREDENTIAL_GATE = 804368692414571038


def transaction_gate(db):
    db.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'), {'key': CREDENTIAL_GATE})


@contextmanager
def credential_write(db=None):
    if db is not None:
        transaction_gate(db)
        yield
    else:
        # Standalone cleanup (e.g. a rolled-back OAuth callback) still orders with
        # backup. Application writes always supply the transaction they publish in.
        from app.db import engine
        with Session(engine()) as local, local.begin():
            transaction_gate(local)
            yield


def paths():
    config = settings()
    directory = Path(getattr(config, "ai_secret_directory", os.getenv("JL_AI_SECRET_DIRECTORY", "/private/ai-credentials")))
    master = Path(getattr(config, "ai_master_key_file", os.getenv("JL_AI_MASTER_KEY_FILE", "/run/secrets/ai_master_key")))
    if not directory.is_absolute() or not master.is_absolute() or directory == master or directory in master.parents:
        raise APIError(503, "credential_store_unavailable", "密文目录与独立主密钥文件配置无效。")
    return directory, master


def cipher():
    try:
        _, master = paths()
        key = master.read_bytes().strip()
        return Fernet(key)
    except (OSError, ValueError, TypeError):
        raise APIError(503, "credential_store_unavailable", "本机凭据存储尚未就绪，请检查独立主密钥挂载。") from None


def ready():
    try:
        directory, _ = paths()
        cipher()
        return directory.is_dir() and os.access(directory, os.R_OK | os.W_OK)
    except APIError:
        return False


def secret_fingerprint(value):
    # Keyed digest is safe for proof/idempotency matching and cannot be used to guess short credentials offline.
    _, master = paths()
    try:
        key = master.read_bytes().strip()
        Fernet(key)
    except (OSError, ValueError):
        raise APIError(503, "credential_store_unavailable", "本机凭据存储尚未就绪。") from None
    return hmac.new(key, b"tianyan.ai.secret.v1\0" + value.encode(), hashlib.sha256).hexdigest()


def write_secret(value, *, db=None):
    with credential_write(db):
        return _write_secret(value)


def _write_secret(value):
    directory, _ = paths()
    token = cipher().encrypt(value.encode("utf-8"))
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        identity = uuid.uuid4()
        path = directory / f"{identity}.key"
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        return f"vault:{identity}"
    except OSError:
        raise APIError(503, "credential_store_unavailable", "凭据密文保存失败，配置未生效。") from None


def read_secret(reference):
    if not reference:
        return None
    if reference.startswith("env:model:"):
        name = reference.removeprefix("env:model:")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name):
            raise APIError(503, "credential_reference_invalid", "保存的环境凭据引用无效。")
        value = os.getenv(name, "")
        if not value:
            return None
        if len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise APIError(503, "credential_unreadable", "旧环境模型凭据格式无效，请重新保存。")
        return value
    if reference.startswith("env:"):
        attr = {"env:tushare": "tushare_token", "env:mairui": "mairui_licence", "env:tickflow": "tickflow_api_key"}.get(reference)
        value = getattr(settings(), attr, None) if attr else None
        return value.get_secret_value() if value else None
    if not reference.startswith("vault:"):
        raise APIError(503, "credential_reference_invalid", "保存的凭据引用无效。")
    try:
        identity = uuid.UUID(reference.removeprefix("vault:"))
        directory, _ = paths()
        return cipher().decrypt((directory / f"{identity}.key").read_bytes()).decode("utf-8")
    except (OSError, ValueError, InvalidToken, UnicodeError):
        raise APIError(503, "credential_unreadable", "凭据无法解密，请检查配套主密钥或重新保存。") from None
