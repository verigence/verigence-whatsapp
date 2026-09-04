"""WaClient — async Meta Cloud API v19 send adapter.

One instance is created in lifespan startup and shared across all
requests / workers. The client is async (httpx.AsyncClient) because
the outbox drain runs from the Procrastinate async worker loop.

Methods:
    send_text            — plain-text or caption message
    send_interactive_buttons — up to 3 quick-reply buttons
    send_interactive_list    — up to 10 list rows
    send_template        — pre-approved HSM template
    send_audio           — WhatsApp-hosted audio reply
    mark_read            — blue-tick acknowledgement (best-effort)

All send methods return SendResult(wamid) on success.
On failure they raise WaApiError with .retryable for outbox backoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from wa_service.config import Settings

logger = structlog.get_logger(__name__)

_BASE = "https://graph.facebook.com/v19.0"
_TIMEOUT = httpx.Timeout(timeout=15.0, connect=5.0)


class WaApiError(Exception):
    def __init__(self, *, status_code: int, code: str, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class SendResult:
    wamid: str


class WaClient:
    def __init__(self, settings: Settings) -> None:
        self._token = settings.wa_access_token.get_secret_value()
        self._client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_text(
        self,
        *,
        phone_number_id: str,
        to: str,
        body: str,
        preview_url: bool = False,
    ) -> SendResult:
        return await self._post(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": preview_url, "body": body},
            },
        )

    async def send_interactive_buttons(
        self,
        *,
        phone_number_id: str,
        to: str,
        header_text: str | None,
        body_text: str,
        footer_text: str | None,
        buttons: list[dict[str, str]],
    ) -> SendResult:
        if len(buttons) > 3:
            raise ValueError("Meta allows at most 3 quick-reply buttons")
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        return await self._post(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
            },
        )

    async def send_interactive_list(
        self,
        *,
        phone_number_id: str,
        to: str,
        header_text: str | None,
        body_text: str,
        footer_text: str | None,
        button_label: str,
        sections: list[dict[str, Any]],
    ) -> SendResult:
        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_label, "sections": sections},
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}
        if footer_text:
            interactive["footer"] = {"text": footer_text}
        return await self._post(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": interactive,
            },
        )

    async def send_template(
        self,
        *,
        phone_number_id: str,
        to: str,
        template_name: str,
        language_code: str,
        components: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        return await self._post(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code},
                    "components": components or [],
                },
            },
        )

    async def send_audio(
        self,
        *,
        phone_number_id: str,
        to: str,
        media_id: str,
    ) -> SendResult:
        return await self._post(
            phone_number_id,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "audio",
                "audio": {"id": media_id},
            },
        )

    async def mark_read(self, *, phone_number_id: str, wamid: str) -> None:
        """Best-effort — errors are swallowed."""
        try:
            resp = await self._client.post(
                f"{_BASE}/{phone_number_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": wamid,
                },
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wa_mark_read_failed", wamid=wamid, error=str(exc))

    async def _post(self, phone_number_id: str, payload: dict[str, Any]) -> SendResult:
        url = f"{_BASE}/{phone_number_id}/messages"
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise WaApiError(status_code=0, code="NETWORK_ERROR", message=str(exc), retryable=True) from exc

        if resp.status_code == 200:
            try:
                wamid: str = resp.json()["messages"][0]["id"]
                logger.info("wa_send_ok", phone_number_id=phone_number_id,
                            to=payload.get("to"), kind=payload.get("type"), wamid=wamid)
                return SendResult(wamid=wamid)
            except (KeyError, IndexError, ValueError) as exc:
                raise WaApiError(status_code=resp.status_code, code="CONTRACT_ERROR",
                                 message=str(exc), retryable=False) from exc

        code, message = "HTTP_ERROR", resp.text
        try:
            err = resp.json().get("error", {})
            code, message = str(err.get("code", code)), err.get("message", message)
        except Exception:  # noqa: BLE001
            pass
        retryable = resp.status_code >= 500 or resp.status_code == 429
        logger.error("wa_send_failed", phone_number_id=phone_number_id,
                     status=resp.status_code, meta_code=code, meta_message=message)
        raise WaApiError(status_code=resp.status_code, code=code, message=message, retryable=retryable)


# Module-level singleton set during lifespan
_wa_client: WaClient | None = None


def set_wa_client(client: WaClient) -> None:
    global _wa_client  # noqa: PLW0603
    _wa_client = client


def get_wa_client() -> WaClient:
    if _wa_client is None:
        raise RuntimeError("WaClient not initialised — call set_wa_client() in lifespan")
    return _wa_client


def clear_wa_client() -> None:
    global _wa_client  # noqa: PLW0603
    _wa_client = None
