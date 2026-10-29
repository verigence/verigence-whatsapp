"""Service-to-service authentication for the /v1 API.

Callers send ``Authorization: Bearer <token>``. Tokens come from the
WA_API_TOKENS setting ("caller:token,caller2:token2"), so each calling module
has its own revocable token and every request is attributable to a caller.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from wa_service.config import Settings, get_settings


@dataclass(frozen=True)
class Caller:
    name: str


def require_caller(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Caller:
    callers = settings.api_callers
    if not callers:
        raise HTTPException(status_code=503, detail="WA_API_NOT_CONFIGURED")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401, detail="UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"}
        )
    # Constant-time comparison against every configured token.
    match: str | None = None
    for known, name in callers.items():
        if hmac.compare_digest(known.encode(), token.encode()):
            match = name
    if match is None:
        raise HTTPException(
            status_code=401, detail="UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"}
        )
    return Caller(name=match)
