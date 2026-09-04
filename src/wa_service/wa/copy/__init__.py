"""YAML copy loader with locale fallback to 'en'."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_COPY_DIR = Path(__file__).parent


@lru_cache(maxsize=8)
def load_copy(locale: str = "en") -> dict[str, Any]:
    path = _COPY_DIR / f"{locale}.yaml"
    if not path.exists():
        path = _COPY_DIR / "en.yaml"
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
