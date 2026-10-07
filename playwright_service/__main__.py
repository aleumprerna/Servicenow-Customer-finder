from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import uvicorn
from playwright.async_api import Error as PlaywrightError

from .api import create_app
from .browser.errors import PreparationError
from .config import load_settings
from .service import AutomationService, clean_error


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the standalone ServiceNow Playwright API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--prepare-only", action="store_true",
        help="Select ritik.d and Implementation, continue to Customer Information, then exit.",
    )
    args = parser.parse_args()
    # Playwright needs subprocess support on Windows; a single Proactor loop
    # avoids the Selector loop used by some server reload/worker configurations.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    settings = load_settings(args.env_file)
    if args.prepare_only:
        def publish(status: str, detail: str, tone: str) -> None:
            print(f"{status}: {detail}", file=sys.stderr, flush=True)

        try:
            result = asyncio.run(AutomationService(settings).prepare_session(status_callback=publish))
        except (ConnectionError, PreparationError, PlaywrightError, OSError) as exc:
            print(f"Preparation failed: {clean_error(exc)}", file=sys.stderr)
            return 1
        print(result.model_dump_json(indent=2))
        return 0
    uvicorn.run(create_app(settings), host=args.host, port=args.port, loop="asyncio", workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
