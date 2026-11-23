import base64
import re
from datetime import datetime
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.evidence.common import canonical

SERVICES = frozenset({'api', 'worker', 'scheduler', 'realtime-worker', 'realtime-worker-secondary', 'recovery-worker', 'ai-worker', 'read-cache-worker', 'maintenance-worker', 'migrate', 'credential-init', 'web'})
MAX_PACKAGE = 8 * 1024**3


class UpdateFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class SchemaRange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_revisions: list[str] = Field(min_length=1, max_length=30)
    to_revision: str = Field(pattern=r'^[a-zA-Z0-9_]{1,80}$')
    compatible_revisions: list[str] = Field(min_length=1, max_length=30)
    backward_compatible: bool


class Manifest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal[1]
    id: str = Field(pattern=r'^[a-zA-Z0-9._-]{1,100}$')
    version: str = Field(pattern=r'^\d{1,4}\.\d{1,4}\.\d{1,4}$')
    build_revision: str = Field(pattern=r'^[0-9a-f]{40}$')
    channel: Literal['stable']
    published_at: datetime
    notes: str = Field(min_length=1, max_length=16000)
    automatic_supported: bool
    manual_guide: str = Field(default='', max_length=4000)
    package_url: str = Field(max_length=2048)
    package_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(gt=0, le=MAX_PACKAGE)
    unpacked_bytes: int = Field(gt=0, le=32 * 1024**3)
    minimum_free_bytes: int = Field(ge=1024**3, le=128 * 1024**3)
    postgres_major: Literal[17]
    database_schema: SchemaRange = Field(alias='schema')
    images: dict[str, str]

    @field_validator('published_at')
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError('release time must include timezone')
        return value

    @field_validator('images')
    @classmethod
    def fixed_images(cls, value):
        if value.keys() != SERVICES or any(not re.fullmatch(r'sha256:[0-9a-f]{64}', image) for image in value.values()):
            raise ValueError('complete fixed service image set required')
        return value


def verified(document, signature, public_key):
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), canonical(document))
        return Manifest.model_validate(document)
    except (InvalidSignature, ValueError, TypeError, KeyError):
        raise UpdateFailure('update_signature_invalid', '更新说明的签名或格式校验失败，已保留当前版本。') from None


def version_tuple(value):
    return tuple(map(int, value.split('.')))
