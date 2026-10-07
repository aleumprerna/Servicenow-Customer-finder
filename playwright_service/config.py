from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MODULE_ROOT = Path(__file__).resolve().parent


class AutomationSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    chrome_cdp_url: str = "http://localhost:9222"
    search_timeout_seconds: float = Field(default=20, gt=0, le=300)
    login_timeout_seconds: float = Field(default=30, gt=0, le=600)
    action_timeout_seconds: float = Field(default=60, gt=0, le=300)
    delay_between_companies_seconds: float = Field(default=2, ge=0, le=60)
    match_threshold: int = Field(default=85, ge=1, le=100)
    review_threshold: int = Field(default=70, ge=0, le=100)
    save_screenshots: bool = False
    debug_dir: Path = MODULE_ROOT / "debug"
    result_selectors: tuple[str, ...] = ()

    @field_validator("debug_dir", mode="before")
    @classmethod
    def resolve_debug_dir(cls, value: object) -> Path:
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else MODULE_ROOT / path

    @model_validator(mode="after")
    def validate_thresholds(self) -> "AutomationSettings":
        if self.review_threshold >= self.match_threshold:
            raise ValueError("REVIEW_THRESHOLD must be lower than MATCH_THRESHOLD")
        return self


def load_settings(env_file: Path | None = None) -> AutomationSettings:
    """Read only the module's .env file; process environment takes precedence."""
    values = dotenv_values(env_file or MODULE_ROOT / ".env")

    def value(name: str, default: str) -> str:
        return os.environ.get(name, str(values.get(name) or default)).strip()

    import json

    selectors = json.loads(value("SERVICENOW_RESULT_SELECTORS", "[]"))
    if not isinstance(selectors, list) or not all(isinstance(item, str) and item.strip() for item in selectors):
        raise ValueError("SERVICENOW_RESULT_SELECTORS must be a JSON array of nonempty CSS selectors")
    return AutomationSettings(
        chrome_cdp_url=value("CHROME_CDP_URL", "http://localhost:9222"),
        search_timeout_seconds=value("SEARCH_TIMEOUT_SECONDS", "20"),
        login_timeout_seconds=value("LOGIN_TIMEOUT_SECONDS", "30"),
        action_timeout_seconds=value("ACTION_TIMEOUT_SECONDS", "60"),
        delay_between_companies_seconds=value("DELAY_BETWEEN_COMPANIES_SECONDS", "2"),
        match_threshold=value("MATCH_THRESHOLD", "85"),
        review_threshold=value("REVIEW_THRESHOLD", "70"),
        save_screenshots=value("SAVE_SCREENSHOTS", "false"),
        debug_dir=value("DEBUG_DIR", "debug"),
        result_selectors=tuple(selectors),
    )
