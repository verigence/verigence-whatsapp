"""Pydantic-settings configuration for the WhatsApp service.

All values are read from environment variables. The three WA secrets
(app_secret, access_token, verify_token) are required at start-up;
everything else has a safe default.

Governance gate D-08: WA_REDACTION_ENABLED defaults to false.
Do NOT flip to true until the Aadhaar redactor is verified end-to-end.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---- Database ---------------------------------------------------------
    database_url: str = Field(..., description="PostgreSQL DSN (psycopg3 format)")

    # ---- WhatsApp Cloud API secrets (required) ----------------------------
    wa_app_secret: SecretStr = Field(...)
    wa_access_token: SecretStr = Field(...)
    wa_verify_token: SecretStr = Field(...)

    # ---- Upstream service URLs --------------------------------------------
    # HTTP endpoint of verigence-audit-core for evidence upload
    audit_core_base_url: str = Field(default="http://localhost:8000")
    # Service-to-service bearer token for internal calls to audit-core
    audit_core_internal_token: SecretStr = Field(default=SecretStr(""))

    # ---- Behaviour tuning -------------------------------------------------
    wa_debounce_seconds: int = Field(default=90, ge=10, le=600)
    wa_media_concurrent_per_contact: int = Field(default=3, ge=1, le=10)
    wa_media_concurrent_global: int = Field(default=20, ge=1, le=100)
    wa_di_poll_interval_seconds: int = Field(default=30, ge=5, le=300)
    wa_di_sla_minutes: int = Field(default=15, ge=1, le=120)
    wa_flush_scheduler_interval_seconds: int = Field(default=60)
    wa_default_locale: str = Field(default="en")

    # ---- Governance gate --------------------------------------------------
    wa_redaction_enabled: bool = Field(default=False)

    model_config = {"env_prefix": "", "case_sensitive": False}

    @model_validator(mode="after")
    def _secrets_non_empty(self) -> "Settings":
        for name in ("wa_app_secret", "wa_access_token", "wa_verify_token"):
            if not getattr(self, name).get_secret_value().strip():
                raise ValueError(f"{name} must not be empty")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
