"""Fake Meta Cloud API server for use in tests.

Provides:
  - GET  /<media_id>         → {url: <download_url>}
  - GET  /download/<media_id> → raw bytes + X-Sha256 header
  - POST /<phone_number_id>/messages → {messages: [{id: wamid}]}
  - sign_payload(body)       → dict of headers with X-Hub-Signature-256
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx


class FakeMetaTransport(httpx.MockTransport):
    """httpx transport that simulates Meta Graph API v19 responses."""

    def __init__(self, *, app_secret: str = "test_secret") -> None:
        self.app_secret = app_secret
        self._media: dict[str, bytes] = {}
        self._sent_messages: list[dict[str, Any]] = []
        self._wamid_counter = 0

    def add_media(self, media_id: str, content: bytes) -> None:
        self._media[media_id] = content

    def sign_payload(self, body: bytes) -> dict[str, str]:
        sig = hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": f"sha256={sig}"}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Media URL resolution: GET /v19.0/<media_id>
        if request.method == "GET" and "/download/" not in path:
            media_id = path.split("/")[-1]
            if media_id in self._media:
                base = str(request.url).rsplit("/", 1)[0]
                return httpx.Response(
                    200,
                    json={"url": f"{base}/download/{media_id}", "mime_type": "application/pdf"},
                )
            return httpx.Response(404, json={"error": {"message": "not found"}})

        # Media download
        if request.method == "GET" and "/download/" in path:
            media_id = path.split("/")[-1]
            if media_id in self._media:
                content = self._media[media_id]
                sha = hashlib.sha256(content).hexdigest()
                return httpx.Response(
                    200,
                    content=content,
                    headers={"content-type": "application/pdf", "X-Sha256": sha},
                )
            return httpx.Response(404)

        # Send message
        if request.method == "POST" and path.endswith("/messages"):
            self._wamid_counter += 1
            wamid = f"wamid_{self._wamid_counter:04d}"
            body = json.loads(request.content)
            self._sent_messages.append(body)
            return httpx.Response(200, json={"messages": [{"id": wamid}]})

        return httpx.Response(404)

    def sent_to(self, to: str) -> list[dict[str, Any]]:
        return [m for m in self._sent_messages if m.get("to") == to]


def make_inbound_payload(
    *,
    phone_number_id: str,
    sender_wa_id: str,
    messages: list[dict[str, Any]],
    object_type: str = "whatsapp_business_account",
) -> dict[str, Any]:
    """Build a minimal Meta webhook payload."""
    return {
        "object": object_type,
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "+919999999999",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Test PC"},
                                    "wa_id": sender_wa_id,
                                }
                            ],
                            "messages": messages,
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def make_document_message(media_id: str, wamid: str, mime: str = "application/pdf") -> dict:
    return {
        "id": wamid,
        "type": "document",
        "timestamp": "1700000000",
        "document": {
            "id": media_id,
            "mime_type": mime,
            "filename": "booking_form.pdf",
            "sha256": "",
        },
    }


def make_image_message(media_id: str, wamid: str) -> dict:
    return {
        "id": wamid,
        "type": "image",
        "timestamp": "1700000000",
        "image": {
            "id": media_id,
            "mime_type": "image/jpeg",
            "sha256": "",
        },
    }
