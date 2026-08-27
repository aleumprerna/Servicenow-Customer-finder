from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROJECT_ROOT = Path(__file__).resolve().parent


class Settings(BaseModel):
    """Validated application configuration loaded from environment variables."""

    model_config = ConfigDict(frozen=True)

    apollo_api_key: str = Field(min_length=1)
    apollo_base_url: str = "https://api.apollo.io/api/v1"
    chrome_cdp_url: str = "http://localhost:9222"
    servicenow_username: str | None = None
    servicenow_password: str | None = None
    headless: bool = False
    input_csv: Path = Path("companies.csv")
    output_csv: Path = Path("companies_checked.csv")
    search_timeout_seconds: float = Field(default=20.0, gt=0)
    delay_between_companies_seconds: float = Field(default=2.0, ge=0)
    match_threshold: int = Field(default=85, ge=1, le=100)
    review_threshold: int = Field(default=70, ge=0, le=100)
    apollo_match_threshold: int = Field(default=80, ge=1, le=100)
    save_screenshots: bool = False
    debug_dir: Path = Path("debug")
    apollo_timeout_seconds: float = Field(default=20.0, gt=0)
    apollo_max_retries: int = Field(default=3, ge=1, le=8)
    result_selectors: tuple[str, ...] = ()
    n8n_webhook_url: str | None = None
    app_base_url: str = "http://localhost:8000"

    @field_validator("input_csv", "output_csv", "debug_dir", mode="before")
    @classmethod
    def resolve_path(cls, value: Any) -> Path:
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    @model_validator(mode="after")
    def validate_thresholds(self) -> "Settings":
        if self.review_threshold >= self.match_threshold:
            raise ValueError("REVIEW_THRESHOLD must be lower than MATCH_THRESHOLD")
        return self


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _parse_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _parse_result_selectors() -> tuple[str, ...]:
    raw = os.getenv("SERVICENOW_RESULT_SELECTORS", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SERVICENOW_RESULT_SELECTORS must be a JSON array") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("SERVICENOW_RESULT_SELECTORS must be a JSON array of CSS selectors")
    return tuple(item.strip() for item in parsed if item.strip())


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(dotenv_path=env_file or PROJECT_ROOT / ".env")
    data: dict[str, Any] = {
        "apollo_api_key": os.getenv("APOLLO_API_KEY", "").strip(),
        "apollo_base_url": os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1").strip(),
        "chrome_cdp_url": os.getenv("CHROME_CDP_URL", "http://localhost:9222").strip(),
        "servicenow_username": _optional("SERVICENOW_USERNAME"),
        "servicenow_password": _optional("SERVICENOW_PASSWORD"),
        "headless": _parse_bool("HEADLESS", False),
        "input_csv": os.getenv("INPUT_CSV", "companies.csv"),
        "output_csv": os.getenv("OUTPUT_CSV", "companies_checked.csv"),
        "search_timeout_seconds": os.getenv("SEARCH_TIMEOUT_SECONDS", "20"),
        "delay_between_companies_seconds": os.getenv("DELAY_BETWEEN_COMPANIES_SECONDS", "2"),
        "match_threshold": os.getenv("MATCH_THRESHOLD", "85"),
        "review_threshold": os.getenv("REVIEW_THRESHOLD", "70"),
        "apollo_match_threshold": os.getenv("APOLLO_MATCH_THRESHOLD", "80"),
        "save_screenshots": _parse_bool("SAVE_SCREENSHOTS", False),
        "debug_dir": os.getenv("DEBUG_DIR", "debug"),
        "apollo_timeout_seconds": os.getenv("APOLLO_TIMEOUT_SECONDS", "20"),
        "apollo_max_retries": os.getenv("APOLLO_MAX_RETRIES", "3"),
        "result_selectors": _parse_result_selectors(),
        "n8n_webhook_url": _optional("N8N_WEBHOOK_URL"),
        "app_base_url": os.getenv("APP_BASE_URL", "http://localhost:8000").strip(),
    }
    return Settings.model_validate(data)
