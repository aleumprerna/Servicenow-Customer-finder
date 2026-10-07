from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from playwright.async_api import Error as PlaywrightError

from .browser.connection import FormNotFoundError
from .browser.errors import PreparationError
from .config import AutomationSettings, load_settings
from .schemas import CheckCompaniesRequest, CheckCompaniesResponse, PreparationResponse
from .service import AutomationService, clean_error


def create_app(settings: AutomationSettings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.automation = AutomationService(settings or load_settings())
        application.state.browser_lock = asyncio.Lock()
        yield

    application = FastAPI(
        title="ServiceNow Playwright API", version="1.0.0",
        description="Check supplied companies against ServiceNow using an authenticated Chrome session.",
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "busy": application.state.browser_lock.locked()}

    async def with_browser(action):
        lock = application.state.browser_lock
        if lock.locked():
            raise HTTPException(status_code=409, detail="Browser automation is busy. Retry after the active request finishes.")
        async with lock:
            try:
                return await action()
            except ConnectionError as exc:
                raise HTTPException(status_code=503, detail=clean_error(exc)) from exc
            except FormNotFoundError as exc:
                raise HTTPException(status_code=409, detail=clean_error(exc)) from exc
            except PreparationError as exc:
                raise HTTPException(status_code=409, detail=f"{exc.step}: {exc.detail}") from exc
            except (PlaywrightError, OSError) as exc:
                raise HTTPException(status_code=503, detail=clean_error(exc)) from exc

    @application.get("/api/session")
    async def session() -> dict[str, object]:
        return await with_browser(application.state.automation.inspect_session)

    @application.post("/api/prepare", response_model=PreparationResponse)
    async def prepare() -> PreparationResponse:
        return await with_browser(application.state.automation.prepare_session)

    @application.post("/api/check-companies", response_model=CheckCompaniesResponse)
    async def check_companies(body: CheckCompaniesRequest) -> CheckCompaniesResponse:
        results = await with_browser(lambda: application.state.automation.check_companies(body.companies))
        return CheckCompaniesResponse.from_results(results)

    return application


app = create_app()
