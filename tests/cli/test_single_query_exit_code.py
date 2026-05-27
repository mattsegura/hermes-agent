import pytest


class _FakeConsole:
    def print(self, *args, **kwargs):
        pass


class _BaseFakeCLI:
    console = _FakeConsole()
    tool_progress_mode = None
    session_id = "test-session"
    agent = None

    def __init__(self, *args, **kwargs):
        self._last_chat_result = None

    def _show_security_advisories(self):
        pass

    def _print_exit_summary(self):
        pass


class _FailedChatCLI(_BaseFakeCLI):
    def chat(self, query, images=None):
        self._last_chat_result = {
            "final_response": "API call failed after 3 retries: HTTP 429",
            "failed": True,
        }
        return "API call failed after 3 retries: HTTP 429"


class _SuccessfulChatCLI(_BaseFakeCLI):
    def chat(self, query, images=None):
        self._last_chat_result = {"final_response": "ok", "failed": False}
        return "ok"


def test_single_query_exits_nonzero_when_chat_result_failed(monkeypatch):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "HermesCLI", _FailedChatCLI)

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(query="hello")

    assert exc.value.code == 1


def test_single_query_success_returns_without_system_exit(monkeypatch):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "HermesCLI", _SuccessfulChatCLI)

    assert cli_mod.main(query="hello") is None
