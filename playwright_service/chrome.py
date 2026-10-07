import os
import subprocess
from pathlib import Path

def chrome_executable() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Google Chrome was not found in a standard installation path")


def launch_chrome() -> None:
    subprocess.Popen(
        [
            str(chrome_executable()),
            "--remote-debugging-port=9222",
            "--user-data-dir=C:\\playwright-servicenow-profile",
            "https://partnerportal.servicenow.com/partnerhome?id=deployment_registration&spa=1",
        ],
        cwd=Path(__file__).resolve().parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
