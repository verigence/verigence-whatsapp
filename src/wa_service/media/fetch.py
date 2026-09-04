"""Streaming media download from Meta Cloud API.

Key design decisions (VAC-WA-SD-001 §6.2):
- SHA-256 computed while streaming; mismatch → quarantine, never retry
- Attempt ceiling is 4 (Meta blocks the NUMBER at 5 failures/hour)
- A 404 on the media URL means the URL expired: re-resolve and retry
- Backoff applies to the contact, not the individual file
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)
_META_GRAPH_BASE = "https://graph.facebook.com/v19.0"
_BACKOFF_BEFORE_META_LIMIT = 4  # Meta blocks at 5 — we stop at 4


class MediaFetchError(Exception):
    def __init__(self, *, code: str, retryable: bool, quarantine: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.quarantine = quarantine


@dataclass
class FetchResult:
    content: bytes
    sha256: str
    byte_size: int
    mime: str


def _resolve_media_url(*, media_id: str, access_token: str, client: httpx.Client) -> str:
    resp = client.get(
        f"{_META_GRAPH_BASE}/{media_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code == 404:
        raise MediaFetchError(code="MEDIA_NOT_FOUND", retryable=False)
    if resp.status_code != 200:
        raise MediaFetchError(code=f"MEDIA_URL_HTTP_{resp.status_code}", retryable=True)
    url = resp.json().get("url")
    if not url:
        raise MediaFetchError(code="MEDIA_URL_MISSING", retryable=True)
    return url


def _stream_download(
    *, url: str, access_token: str, client: httpx.Client, declared_sha256: str | None
) -> FetchResult:
    hasher = hashlib.sha256()
    buf = io.BytesIO()
    mime = "application/octet-stream"
    try:
        with client.stream(
            "GET", url, headers={"Authorization": f"Bearer {access_token}"}
        ) as response:
            if response.status_code == 404:
                raise MediaFetchError(code="MEDIA_URL_EXPIRED", retryable=False)
            if response.status_code != 200:
                raise MediaFetchError(
                    code=f"MEDIA_DL_HTTP_{response.status_code}",
                    retryable=response.status_code >= 500,
                )
            mime = response.headers.get("content-type", mime)
            for chunk in response.iter_bytes(chunk_size=65_536):
                hasher.update(chunk)
                buf.write(chunk)
    except httpx.HTTPError as exc:
        raise MediaFetchError(code="MEDIA_NETWORK_ERROR", retryable=True) from exc

    content = buf.getvalue()
    computed = hasher.hexdigest()
    if declared_sha256 and declared_sha256.lower() != computed:
        logger.error("wa_media_hash_mismatch", declared=declared_sha256, computed=computed)
        raise MediaFetchError(code="MEDIA_HASH_MISMATCH", retryable=False, quarantine=True)
    return FetchResult(content=content, sha256=computed, byte_size=len(content), mime=mime)


def fetch_media(
    *,
    media_id: str,
    access_token: str,
    declared_sha256: str | None,
    attempt: int = 0,
    transport=None,
) -> FetchResult:
    """Fetch a media object from Meta. Raises MediaFetchError on failure."""
    if attempt >= _BACKOFF_BEFORE_META_LIMIT:
        raise MediaFetchError(code="MEDIA_ATTEMPT_LIMIT", retryable=False)
    kwargs: dict = {"timeout": 60.0}
    if transport:
        kwargs["transport"] = transport
    with httpx.Client(**kwargs) as client:
        url = _resolve_media_url(media_id=media_id, access_token=access_token, client=client)
        try:
            return _stream_download(
                url=url, access_token=access_token, client=client,
                declared_sha256=declared_sha256,
            )
        except MediaFetchError as exc:
            if exc.code == "MEDIA_URL_EXPIRED" and attempt < 3:
                url = _resolve_media_url(
                    media_id=media_id, access_token=access_token, client=client
                )
                return _stream_download(
                    url=url, access_token=access_token, client=client,
                    declared_sha256=declared_sha256,
                )
            raise
