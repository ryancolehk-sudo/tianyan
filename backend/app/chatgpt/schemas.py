from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool

from app.schemas import DTO


class ChatGPTLoginInput(DTO):
    mode: Literal["login", "connect"]
    account_id: UUID | None = None
    new_account: StrictBool = False


class ChatGPTAccountView(DTO):
    id: UUID
    connection_id: UUID
    email: str | None
    label: str
    state: Literal["connected", "identity_only", "disconnected", "refreshing", "refresh_unknown", "reauthorization_required", "client_error"]
    use_for_ai: bool
    sharing_granted: bool
    model: str | None
    enabled: bool
    revision: int
    tested: bool
    last_test: dict | None
    welcome_pending: bool
    reauthorization_required: bool
    remote_revocation_unconfirmed: bool
    created_at: datetime
    updated_at: datetime


class ChatGPTStatus(DTO):
    available: bool
    local_only: bool = True
    reason: str | None = None
    authenticated: bool = False
    accounts: list[ChatGPTAccountView] = Field(default_factory=list)


class ChatGPTAccountIndex(DTO):
    items: list[ChatGPTAccountView]


class ChatGPTLoginResult(DTO):
    authorization_url: str


class ChatGPTDisconnectResult(DTO):
    account: ChatGPTAccountView
    remote_revocation_unconfirmed: bool
