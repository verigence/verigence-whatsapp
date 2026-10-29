"""Request / response models for the /v1 API."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
Locale = Literal["en", "hi", "pa"]
ContactStatus = Literal["pending", "active", "suspended", "revoked"]


def _check_e164(v: str) -> str:
    if not _E164.match(v):
        raise ValueError("must be E.164, e.g. +919876543210")
    return v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- messages ---------------------------------------------------------------
class Button(_Model):
    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=20)  # Meta limit


class SendMessageRequest(_Model):
    """Exactly one of text / template / buttons must be given."""

    to: str
    text: str | None = Field(default=None, min_length=1, max_length=4096)
    template: TemplateBody | None = None
    buttons: ButtonsBody | None = None

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        return _check_e164(v)

    def kind(self) -> Literal["text", "template", "buttons"]:
        given = [
            k for k, v in (("text", self.text), ("template", self.template), ("buttons", self.buttons))
            if v is not None
        ]
        if len(given) != 1:
            raise ValueError("provide exactly one of: text, template, buttons")
        return given[0]  # type: ignore[return-value]


class TemplateBody(_Model):
    name: str = Field(min_length=1, max_length=512)
    language: str = Field(default="en", min_length=2, max_length=16)
    components: list[dict[str, Any]] | None = None


class ButtonsBody(_Model):
    body: str = Field(min_length=1, max_length=1024)
    header: str | None = Field(default=None, max_length=60)
    footer: str | None = Field(default=None, max_length=60)
    buttons: list[Button] = Field(min_length=1, max_length=3)


SendMessageRequest.model_rebuild()


class MessageOut(_Model):
    id: UUID
    to: str
    kind: str
    state: Literal["pending", "sent", "failed"]
    wamid: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    requested_by: str
    created_at: datetime
    sent_at: datetime | None = None


# ---- contacts ---------------------------------------------------------------
class ContactIn(_Model):
    """Bind (or re-bind) a phone number to a Verigence user."""

    phone_e164: str
    user_id: UUID
    org_unit_id: UUID
    locale: Locale = "en"
    status: ContactStatus = "active"

    @field_validator("phone_e164")
    @classmethod
    def _phone(cls, v: str) -> str:
        return _check_e164(v)


class ContactPatch(_Model):
    status: ContactStatus | None = None
    locale: Locale | None = None
    org_unit_id: UUID | None = None


class ContactOut(_Model):
    id: UUID
    phone_e164: str
    user_id: UUID
    tenant_id: UUID
    org_unit_id: UUID | None
    status: ContactStatus
    locale: Locale
    last_seen_at: datetime | None = None
    created_at: datetime


# ---- routes -----------------------------------------------------------------
class RouteIn(_Model):
    phone_number_id: str = Field(min_length=1, max_length=64)
    display_number: str = Field(min_length=1, max_length=64)
    intake_journey_id: UUID | None = None


class RoutePatch(_Model):
    active: bool | None = None
    display_number: str | None = Field(default=None, min_length=1, max_length=64)
    intake_journey_id: UUID | None = None


class RouteOut(_Model):
    id: UUID
    tenant_id: UUID
    phone_number_id: str
    display_number: str
    active: bool
    intake_journey_id: UUID | None = None
    created_at: datetime


# ---- sessions (read-only) ----------------------------------------------------
class SessionOut(_Model):
    id: UUID
    contact_id: UUID
    deal_id: UUID | None
    state: str
    file_count: int
    note: str | None = None
    created_at: datetime
    submitted_at: datetime | None = None
    completed_at: datetime | None = None


class Page(_Model):
    limit: int
    offset: int
    items: list[Any]
