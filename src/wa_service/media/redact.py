"""Aadhaar redaction (governance gate D-08).

Masking runs BEFORE any durable write and before forwarding to audit-core.
Disabled by default (WA_REDACTION_ENABLED=false). Do NOT enable until the
full pipeline is verified end-to-end on synthetic data.

Two controls:
  1. Mask first 8 digits of 12-digit Aadhaar numbers in extracted text
  2. Blank the QR code block (which contains the full number)
"""
from __future__ import annotations

import re

import structlog

logger = structlog.get_logger(__name__)
_AADHAAR_RE = re.compile(r"\b(\d{4})\s*(\d{4})\s*(\d{4})\b")
_AADHAAR_QR_RE = re.compile(r'uid="?\d{12}"?', re.IGNORECASE)


def mask_aadhaar_text(text: str) -> str:
    """Replace first 8 digits: '9876 5432 1098' → 'XXXX XXXX 1098'."""
    return _AADHAAR_RE.sub(lambda m: f"XXXX XXXX {m.group(3)}", text)


def needs_redaction(document_type_key: str) -> bool:
    return document_type_key.upper() in {
        "AADHAAR", "AADHAAR_FRONT", "AADHAAR_BACK", "AADHAAR_MASKED"
    }


class RedactionError(Exception):
    pass


def redact_image_bytes(
    content: bytes, *, mime: str, document_type_key: str
) -> tuple[bytes, bool]:
    """Return (possibly redacted bytes, was_redacted). Requires Pillow + pyzbar."""
    if not needs_redaction(document_type_key):
        return content, False
    try:
        import io as _io
        from PIL import Image, ImageDraw  # type: ignore[import]

        image = Image.open(_io.BytesIO(content)).convert("RGB")
        draw = ImageDraw.Draw(image)
        try:
            from pyzbar import pyzbar  # type: ignore[import]
            for sym in pyzbar.decode(image):
                if sym.type == "QRCODE":
                    qr_data = sym.data.decode("utf-8", errors="replace")
                    if _AADHAAR_QR_RE.search(qr_data):
                        x, y, w, h = sym.rect
                        draw.rectangle(
                            [x - 10, y - 10, x + w + 10, y + h + 10],
                            fill=(255, 255, 255),
                        )
                        logger.info("wa_aadhaar_qr_blanked", doc_type=document_type_key)
        except ImportError:
            logger.warning("wa_redact_pyzbar_unavailable", doc_type=document_type_key)
        out = _io.BytesIO()
        fmt = "JPEG" if mime in ("image/jpeg", "image/jpg") else "PNG"
        image.save(out, format=fmt)
        return out.getvalue(), True
    except ImportError:
        raise RedactionError("Pillow required for Aadhaar redaction")
    except Exception as exc:
        raise RedactionError(f"Redaction failed: {exc}") from exc
