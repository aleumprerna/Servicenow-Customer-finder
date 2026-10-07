from __future__ import annotations

import re


def safe_filename(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_").lower()
    return (normalized or "company")[:80]
