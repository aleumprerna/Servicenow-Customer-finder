import sys

import pytest

from playwright_service import __main__ as cli
from playwright_service.browser.errors import PreparationError
from playwright_service.schemas import PreparationResponse


@pytest.mark.parametrize("fail", [False, True])
def test_prepare_only_command_exits_without_starting_server(monkeypatch, capsys, fail):
    monkeypatch.setattr(sys, "argv", ["playwright_service", "--prepare-only"])
    calls = []

    class Service:
        def __init__(self, _settings):
            pass

        async def prepare_session(self, *, status_callback):
            calls.append("prepare")
            status_callback("Selecting Engagement Manager", "Selecting ritik.d", "working")
            if fail:
                raise PreparationError("Waiting for Login", "Please log in")
            return PreparationResponse()

    def unexpected_server(*_args, **_kwargs):
        pytest.fail("--prepare-only must not start an API server")

    monkeypatch.setattr(cli, "AutomationService", Service)
    monkeypatch.setattr(cli.uvicorn, "run", unexpected_server)
    assert cli.main() == (1 if fail else 0)
    output = capsys.readouterr()
    assert calls == ["prepare"]
    assert "Selecting ritik.d" in output.err
    if fail:
        assert "Preparation failed: Please log in" in output.err
    else:
        assert '"ready": true' in output.out
