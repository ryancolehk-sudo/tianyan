"""Private legacy research output, separate from market evidence and active plans."""
import hashlib
from datetime import date
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from app.evidence.schemas import safe_material
from app.schemas import DTO

SHA40 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
SHA256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Repository = Annotated[str, StringConstraints(max_length=100, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$")]


class ArchiveArtifact(DTO):
    path: str = Field(min_length=1, max_length=500)
    blob: SHA40
    sha256: SHA256
    text: str = Field(min_length=1, max_length=200000)
    _safe_text = model_validator(mode="before")(safe_material)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("source path must be a relative Git path")
        return value

    @model_validator(mode="after")
    def exact_source_bytes(self):
        raw = self.text.encode("utf-8")
        git_blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if self.sha256 != hashlib.sha256(raw).hexdigest() or self.blob != git_blob:
            raise ValueError("source file hashes do not match UTF-8 bytes")
        return self


class ArchiveInput(DTO):
    source_repository: Repository
    source_commit: SHA40
    full: ArchiveArtifact
    summary: ArchiveArtifact | None = None
    rights_basis: str = Field(min_length=1, max_length=2000)
    _safe_text = model_validator(mode="before")(safe_material)

    @field_validator("rights_basis")
    @classmethod
    def meaningful_rights(cls, value):
        if not value.strip():
            raise ValueError("rights basis is required")
        return value


class ArchiveFileInfo(DTO):
    path: str
    blob: SHA40
    sha256: SHA256
    url: str


def file_info(repository, commit, value):
    return ArchiveFileInfo(path=value["path"], blob=value["blob"], sha256=value["sha256"],
        url=f"https://github.com/{repository}/blob/{commit}/" + quote(value["path"], safe="/"))


class ArchiveSummary(DTO):
    id: UUID
    based_on_trade_day: date
    target_trade_day: date
    source_version: int
    source_stage: str
    evidence_cutoff_at: AwareDatetime
    source_generated_at: AwareDatetime
    imported_at: AwareDatetime
    has_summary: bool
    source_repository: Repository
    source_commit: SHA40
    content_hash: SHA256


class ReviewArchive(ArchiveSummary):
    full_text: str
    summary_text: str | None
    full_source: ArchiveFileInfo
    summary_source: ArchiveFileInfo | None
    rights_basis: str
