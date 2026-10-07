from playwright_service.config import MODULE_ROOT, load_settings


def test_copied_module_imports_without_parent_application(tmp_path):
    import shutil
    import subprocess
    import sys

    isolated = tmp_path / "standalone"
    isolated.mkdir()
    shutil.copytree(
        MODULE_ROOT, isolated / "playwright_service",
        ignore=shutil.ignore_patterns("__pycache__", ".env", "debug", "build", "*.egg-info"),
    )
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from playwright_service.api import create_app; "
        "from playwright_service.csv_runner import automate_indices; "
        "from playwright_service.config import load_settings; "
        "from fastapi.testclient import TestClient; "
        "client = TestClient(create_app(load_settings())); "
        "client.__enter__(); "
        "assert client.get('/health').json() == {'status': 'ok', 'busy': False}; "
        "client.__exit__(None, None, None); "
        "assert not any(name in sys.modules for name in "
        "['app', 'main', 'config', 'clients', 'services', 'workflow', 'models', 'utils', 'browser']); "
        "print('Standalone module verified')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(isolated)], cwd=isolated,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Standalone module verified" in result.stdout


def test_module_configuration_needs_no_parent_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROME_CDP_URL", raising=False)
    monkeypatch.delenv("DEBUG_DIR", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("CHROME_CDP_URL=http://localhost:9333\nDEBUG_DIR=artifacts\n", encoding="utf-8")
    settings = load_settings(env_file)
    assert settings.chrome_cdp_url == "http://localhost:9333"
    assert settings.debug_dir == MODULE_ROOT / "artifacts"
    monkeypatch.setenv("CHROME_CDP_URL", "http://localhost:9444")
    assert load_settings(env_file).chrome_cdp_url == "http://localhost:9444"
