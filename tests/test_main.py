from unittest.mock import Mock

import pytest

from main import repl


def test_repl_skips_blank_input_and_does_not_call_generate_with_tools(conn):
    client = Mock()
    get_input = Mock(side_effect=["", "exit"])
    emit = Mock()

    repl(conn, client, get_input=get_input, emit=emit)

    client.generate_with_tools.assert_not_called()
    emit.assert_any_call("\nGoodbye.")


def test_repl_handles_keyboard_interrupt_cleanly(conn):
    client = Mock()
    get_input = Mock(side_effect=KeyboardInterrupt())
    emit = Mock()

    repl(conn, client, get_input=get_input, emit=emit)

    emit.assert_any_call("\nGoodbye.")


def test_repl_relays_reply_from_run_turn(conn):
    from brain.gemini_client import ToolReply

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text="Hello!", function_name=None, function_args=None
    )
    get_input = Mock(side_effect=["hi", "exit"])
    emit = Mock()

    repl(conn, client, get_input=get_input, emit=emit)

    emit.assert_any_call("jarvis: Hello!")


def test_repl_skips_whitespace_only_input_and_does_not_store_it(conn):
    from brain.memory import count_user_messages

    client = Mock()
    get_input = Mock(side_effect=["   ", "exit"])
    emit = Mock()

    repl(conn, client, get_input=get_input, emit=emit)

    client.generate_with_tools.assert_not_called()
    assert count_user_messages(conn) == 0


def test_repl_handles_eof_cleanly(conn):
    client = Mock()
    get_input = Mock(side_effect=EOFError())
    emit = Mock()

    repl(conn, client, get_input=get_input, emit=emit)

    emit.assert_any_call("\nGoodbye.")


import main as main_module


def test_main_exits_with_clear_message_when_api_key_missing(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "resolve_api_key", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "GEMINI_API_KEY is not set" in captured.out


def test_main_uses_gemini_models_env_var_when_set(monkeypatch):
    monkeypatch.setattr(main_module, "resolve_api_key", lambda: "key123")
    monkeypatch.setenv("GEMINI_MODELS", "custom-a,custom-b")
    monkeypatch.setattr(main_module, "resolve_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(main_module, "init_db", lambda url: "conn")

    captured = {}

    def fake_repl(conn, client, get_input=input, emit=print, brains=None):
        captured["models"] = client.models

    monkeypatch.setattr(main_module, "repl", fake_repl)

    main_module.main()

    assert captured["models"] == ["custom-a", "custom-b"]


def test_main_falls_back_to_default_model_chain_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.setattr(main_module, "resolve_api_key", lambda: "key123")
    monkeypatch.setattr(main_module, "resolve_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(main_module, "init_db", lambda url: "conn")

    captured = {}

    def fake_repl(conn, client, get_input=input, emit=print, brains=None):
        captured["models"] = client.models

    monkeypatch.setattr(main_module, "repl", fake_repl)

    main_module.main()

    import config

    assert captured["models"] == config.DEFAULT_MODEL_CHAIN


def _patch_main_prereqs(monkeypatch):
    monkeypatch.setattr(main_module, "resolve_api_key", lambda: "key123")
    monkeypatch.setattr(main_module, "resolve_database_url", lambda: "postgresql://fake")
    monkeypatch.setattr(main_module, "init_db", lambda url: "conn")


def test_main_builds_brains_and_forwards_to_repl(monkeypatch):
    _patch_main_prereqs(monkeypatch)
    monkeypatch.setattr(main_module, "resolve_groq_api_key", lambda: "groq-key")
    monkeypatch.setattr(main_module, "resolve_groq_model", lambda: "groq-model")
    monkeypatch.setattr(main_module, "resolve_claude_model", lambda: "sonnet")
    monkeypatch.setattr(main_module, "resolve_claude_oauth_token", lambda: "oauth-tok")
    monkeypatch.setattr(main_module, "resolve_claude_cli_path", lambda: "C:/claude.exe")
    monkeypatch.setattr(main_module, "resolve_show_brain", lambda: False)

    captured = {}

    def fake_repl(conn, client, get_input=input, emit=print, brains=None):
        captured["brains"] = brains

    monkeypatch.setattr(main_module, "repl", fake_repl)

    main_module.main()

    from brain.brains.claude import ClaudeBrain
    from brain.brains.gemini import GeminiBrain
    from brain.brains.groq import GroqBrain
    from brain.conversation import Brains

    brains = captured["brains"]
    assert isinstance(brains, Brains)
    assert isinstance(brains.gemini, GeminiBrain)
    assert isinstance(brains.claude, ClaudeBrain)
    assert brains.claude.oauth_token == "oauth-tok"
    assert brains.claude.model == "sonnet"
    assert brains.claude.cli_path == "C:/claude.exe"
    assert isinstance(brains.groq, GroqBrain)
    assert brains.groq_api_key == "groq-key"
    assert brains.groq_model == "groq-model"
    assert brains.show_tag is False


def test_main_leaves_groq_none_when_key_missing(monkeypatch):
    _patch_main_prereqs(monkeypatch)
    monkeypatch.setattr(main_module, "resolve_groq_api_key", lambda: None)
    monkeypatch.setattr(main_module, "resolve_groq_model", lambda: "groq-model")
    monkeypatch.setattr(main_module, "resolve_claude_model", lambda: "sonnet")
    monkeypatch.setattr(main_module, "resolve_claude_oauth_token", lambda: None)
    monkeypatch.setattr(main_module, "resolve_show_brain", lambda: True)

    captured = {}

    def fake_repl(conn, client, get_input=input, emit=print, brains=None):
        captured["brains"] = brains

    monkeypatch.setattr(main_module, "repl", fake_repl)

    main_module.main()

    brains = captured["brains"]
    assert brains.groq is None
    assert brains.claude.oauth_token is None
    assert brains.show_tag is True


def test_repl_forwards_brains_to_run_turn(conn, monkeypatch):
    from brain.brains import BrainResult
    from brain.conversation import Brains
    import brain.conversation as conversation
    from brain.router import RouteDecision

    monkeypatch.setattr(
        conversation,
        "route",
        lambda message, has_image, history, has_pending, groq_api_key, groq_model, is_reply=False, continues_claude=False:
            RouteDecision("normal", "judge", message, None),
    )

    class _FakeBrain:
        def __init__(self, name, text):
            self.name = name
            self._text = text

        def respond(self, ctx):
            return BrainResult(self._text, self.name)

    brains = Brains(
        gemini=_FakeBrain("gemini", "Hi from gemini."),
        claude=None, groq=None, groq_api_key="k", groq_model="m", show_tag=True,
    )

    get_input = Mock(side_effect=["hi", "exit"])
    emit = Mock()

    from main import repl

    repl(conn, Mock(), get_input=get_input, emit=emit, brains=brains)

    emit.assert_any_call("jarvis: Hi from gemini.\n— gemini")


def test_main_exits_with_clear_message_when_models_config_invalid(monkeypatch, capsys):
    monkeypatch.setattr(main_module, "resolve_api_key", lambda: "key123")

    def raise_config_error():
        raise ValueError("GEMINI_MODELS is set but contains no model names")

    monkeypatch.setattr(main_module, "resolve_models", raise_config_error)

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "GEMINI_MODELS" in captured.out
