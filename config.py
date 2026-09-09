from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv
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
    llm_provider: str = "kie"
    kie_api_key: str | None = None
    kie_base_url: str = "https://api.kie.ai/codex/v1"
    kie_model: str = "gpt-6-astra"
    kie_reasoning_effort: str = "high"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-3-flash-preview"
    gemini_max_retries: int = Field(default=4, ge=0, le=8)
    gemini_retry_base_seconds: float = Field(default=2.0, gt=0, le=60)
    glm_api_key: str | None = None
    glm_base_url: str = "https://api.tokenrouter.com/v1"
    glm_model: str = "z-ai/glm-5.3"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    deep_research_model: str = "gpt-4o"
    deep_research_max_pages: int = Field(default=12, ge=1, le=40)
    deep_research_page_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    deep_research_request_timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    deep_research_max_content_chars: int = Field(default=250_000, ge=10_000, le=2_000_000)
    deep_research_cache_days: int = Field(default=30, ge=1, le=365)

    @field_validator("input_csv", "output_csv", "debug_dir", mode="before")
    @classmethod
    def resolve_path(cls, value: Any) -> Path:
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    @model_validator(mode="after")
    def validate_thresholds(self) -> "Settings":
        if self.review_threshold >= self.match_threshold:
            raise ValueError("REVIEW_THRESHOLD must be lower than MATCH_THRESHOLD")
        if self.llm_provider not in {"kie", "gemini", "glm", "openai"}:
            raise ValueError("LLM_PROVIDER must be 'kie', 'gemini', 'glm', or 'openai'")
        return self

    @property
    def llm_api_key(self) -> str | None:
        if self.llm_provider == "kie":
            return self.kie_api_key
        if self.llm_provider == "gemini":
            return self.gemini_api_key
        return self.glm_api_key if self.llm_provider == "glm" else self.openai_api_key

    @property
    def llm_base_url(self) -> str:
        if self.llm_provider == "kie":
            return self.kie_base_url
        if self.llm_provider == "gemini":
            return self.gemini_base_url
        return self.glm_base_url if self.llm_provider == "glm" else self.openai_base_url

    @property
    def llm_model(self) -> str:
        if self.llm_provider == "kie":
            return self.kie_model
        if self.llm_provider == "gemini":
            return self.gemini_model
        return self.glm_model if self.llm_provider == "glm" else self.openai_model

    @property
    def llm_supports_hosted_web_search(self) -> bool:
        return self.llm_provider in {"kie", "gemini", "openai"}

    @property
    def llm_reasoning_effort(self) -> str | None:
        return self.kie_reasoning_effort if self.llm_provider == "kie" else None


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
    dotenv_path = env_file or PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=dotenv_path)
    file_values = dotenv_values(dotenv_path)

    def dynamic_value(name: str, default: str = "") -> str:
        # The UI can stay running while the user updates webhook settings. Read
        # these values from disk every time instead of retaining an earlier blank
        # value in the long-lived server process.
        value = file_values.get(name)
        return str(value).strip() if value is not None else os.getenv(name, default).strip()

    llm_provider = dynamic_value("LLM_PROVIDER", "kie").casefold()
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
        "save_screenshots": _parse_bool("SAVE_SCREENSHOTS", True),
        "debug_dir": os.getenv("DEBUG_DIR", "debug"),
        "apollo_timeout_seconds": os.getenv("APOLLO_TIMEOUT_SECONDS", "20"),
        "apollo_max_retries": os.getenv("APOLLO_MAX_RETRIES", "3"),
        "result_selectors": _parse_result_selectors(),
        "n8n_webhook_url": dynamic_value("N8N_WEBHOOK_URL") or None,
        "app_base_url": dynamic_value("APP_BASE_URL", "http://localhost:8000"),
        "llm_provider": llm_provider,
        "kie_api_key": dynamic_value("KIE_API_KEY") or _optional("KIE_API_KEY"),
        "kie_base_url": dynamic_value("KIE_BASE_URL", "https://api.kie.ai/codex/v1"),
        "kie_model": dynamic_value("KIE_MODEL", "gpt-6-astra"),
        "kie_reasoning_effort": dynamic_value("KIE_REASONING_EFFORT", "high"),
        "gemini_api_key": (
            dynamic_value("GEMINI_API_KEY")
            or dynamic_value("GEMINI_KEY")
            or _optional("GEMINI_API_KEY")
            or _optional("GEMINI_KEY")
        ),
        "gemini_base_url": dynamic_value(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
        ),
        "gemini_model": dynamic_value("GEMINI_MODEL", "gemini-3-flash-preview"),
        "gemini_max_retries": dynamic_value("GEMINI_MAX_RETRIES", "4"),
        "gemini_retry_base_seconds": dynamic_value("GEMINI_RETRY_BASE_SECONDS", "2"),
        "glm_api_key": dynamic_value("GLM_KEY") or _optional("GLM_KEY"),
        "glm_base_url": dynamic_value("GLM_BASE_URL", "https://api.tokenrouter.com/v1"),
        "glm_model": dynamic_value("GLM_MODEL", "z-ai/glm-5.3"),
        "openai_api_key": dynamic_value("OPENAI_API_KEY") or _optional("OPENAI_API_KEY"),
        "openai_base_url": dynamic_value("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "openai_model": dynamic_value("OPENAI_MODEL", "gpt-4o"),
        "deep_research_model": dynamic_value("DEEP_RESEARCH_MODEL", "gpt-4o"),
        "deep_research_max_pages": dynamic_value("DEEP_RESEARCH_MAX_PAGES", "12"),
        "deep_research_page_timeout_seconds": dynamic_value(
            "DEEP_RESEARCH_PAGE_TIMEOUT_SECONDS", "8"
        ),
        "deep_research_request_timeout_seconds": dynamic_value(
            "DEEP_RESEARCH_REQUEST_TIMEOUT_SECONDS", "180"
        ),
        "deep_research_max_content_chars": dynamic_value(
            "DEEP_RESEARCH_MAX_CONTENT_CHARS", "250000"
        ),
        "deep_research_cache_days": dynamic_value("DEEP_RESEARCH_CACHE_DAYS", "30"),
    }
    return Settings.model_validate(data)
