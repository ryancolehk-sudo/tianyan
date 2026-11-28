from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JL_", extra="ignore", hide_input_in_errors=True)
    database_url: str = "postgresql+psycopg://localhost/jiulong_unconfigured"
    app_mode: Literal["real", "demo"] = "demo"
    deployment_mode: Literal['local', 'cloud'] = 'local'
    public_origin: str = "http://127.0.0.1:5173"
    session_hours: int = Field(default=8, ge=1, le=24)
    session_cookie: str = Field(default="jl_session", min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    cookie_secure: bool = False
    lease_seconds: int = Field(default=30, ge=5, le=300)
    poll_seconds: float = Field(default=2, ge=0.1, le=30)
    build_revision: str = "development"
    data_collection_enabled: bool = False
    anomaly_collection_enabled: bool = False
    required_worker_roles: list[Literal['scheduler', 'realtime', 'recovery', 'general']] = Field(default_factory=list)
    recovery_enabled: bool = True
    recovery_interval_seconds: int = Field(default=300, ge=30, le=3600)
    recovery_jobs_per_round: int = Field(default=12, ge=1, le=100)
    read_cache_enabled: bool = True
    artifact_directory: str = '/workspace/artifacts'
    runtime_directory: str = '/workspace'
    static_directory: str | None = None
    native_config_file: str | None = None
    backup_directory: str = '/workspace/backups'
    maintenance_status_directory: str = '/workspace/maintenance-status'
    update_directory: str = '/workspace/updates'
    update_feed_url: str | None = None
    update_public_key: str | None = None
    update_allowed_hosts: list[str] = Field(default_factory=list)
    update_channel: Literal['stable'] = 'stable'
    update_installation: Literal['source', 'managed'] = 'source'
    maintenance_enabled: bool = True
    tickflow_api_key: SecretStr | None = Field(default=None, repr=False)
    mairui_licence: SecretStr | None = Field(default=None, repr=False)
    tushare_token: SecretStr | None = Field(default=None, repr=False)
    tushare_endpoint: str = 'https://api.tushare.pro/'
    ai_secret_directory: str = '/workspace/credentials'
    ai_master_key_file: str = '/run/secrets/ai_master_key'

    @field_validator('update_feed_url', mode='before')
    @classmethod
    def official_update_address(cls, value):
        if value is None or value == '':
            return None
        u = urlsplit(value)
        if (u.scheme != 'https' or not u.hostname or u.port not in {None, 443} or u.username or u.password
            or u.query or u.fragment or '\\' in value or any(ord(c) < 33 for c in value)):
            raise ValueError('official update feed must be a plain HTTPS address')
        return value

    @field_validator('update_public_key', mode='before')
    @classmethod
    def official_update_key(cls, value):
        import base64
        if value is None or value == '':
            return None
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError('official update public key must be Ed25519')
        return value

    @field_validator('update_allowed_hosts')
    @classmethod
    def official_update_hosts(cls, value):
        import re
        if len(value) > 8 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host) for host in value):
            raise ValueError('invalid official download host allowlist')
        return value

    @field_validator('tushare_endpoint')
    @classmethod
    def fixed_tushare_destination(cls, value):
        from app.ai.protocols import ModelFailure, validate_endpoint
        try:
            parts = validate_endpoint(value)
            if '@' in parts.netloc or '?' in value or '#' in value:
                raise ValueError('unsupported Tushare destination')
        except ModelFailure:
            raise ValueError('Tushare requires a public HTTPS endpoint without embedded credentials') from None
        return value

    @field_validator('tushare_token', mode='before')
    @classmethod
    def private_tushare_token(cls, value):
        import re
        if value is None or value == '':
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', raw):
            raise ValueError('invalid private Tushare credential format')
        return raw

    @field_validator('mairui_licence', mode='before')
    @classmethod
    def private_reference_licence(cls, value):
        import re
        if value is None or value == '':
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', raw):
            raise ValueError('invalid private reference credential format')
        return raw

    @field_validator('tickflow_api_key', mode='before')
    @classmethod
    def private_quote_key(cls, value):
        if value is None or value == '':
            return None
        raw=value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw,str) or not 8 <= len(raw) <= 512 or any(ord(c)<33 or ord(c)>126 for c in raw):
            raise ValueError('invalid private quote credential format')
        return raw

    @field_validator("public_origin")
    @classmethod
    def origin_only(cls, value: str) -> str:
        u = urlsplit(value)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            raise ValueError("public_origin must be an HTTP(S) origin")
        if u.path not in {"", "/"} or u.query or u.fragment:
            raise ValueError("public_origin cannot include a path")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_secure_remote(self):
        u = urlsplit(self.public_origin)
        if u.hostname not in {"localhost", "127.0.0.1", "::1"}:
            if u.scheme != "https" or not self.cookie_secure:
                raise ValueError("remote instances require HTTPS and secure cookies")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
