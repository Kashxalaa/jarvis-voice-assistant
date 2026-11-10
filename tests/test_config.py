import pytest

import config


def test_resolve_api_key_returns_none_when_not_set(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda: None)

    assert config.resolve_api_key() is None


def test_resolve_api_key_returns_value_when_set(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "abc123")

    assert config.resolve_api_key() == "abc123"


def test_resolve_models_returns_env_var_as_comma_separated_list(monkeypatch):
    monkeypatch.setenv("GEMINI_MODELS", "model-a, model-b ,model-c")

    assert config.resolve_models() == ["model-a", "model-b", "model-c"]


def test_resolve_models_falls_back_to_default_chain_when_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    assert config.resolve_models() == config.DEFAULT_MODEL_CHAIN


def test_resolve_models_falls_back_to_legacy_gemini_model_when_set(monkeypatch):
    monkeypatch.delenv("GEMINI_MODELS", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")

    assert config.resolve_models() == ["gemini-3.6-flash"]


def test_resolve_models_raises_when_gemini_models_set_but_empty(monkeypatch):
    monkeypatch.setenv("GEMINI_MODELS", " , ,")

    with pytest.raises(ValueError, match="GEMINI_MODELS"):
        config.resolve_models()


def test_resolve_browser_executable_path_returns_none_when_unset(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("BROWSER_EXECUTABLE_PATH", raising=False)

    assert config.resolve_browser_executable_path() is None


def test_resolve_browser_executable_path_returns_none_when_blank(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "")

    assert config.resolve_browser_executable_path() is None


def test_resolve_browser_executable_path_returns_the_configured_value(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("BROWSER_EXECUTABLE_PATH", "C:/Brave/brave.exe")

    assert config.resolve_browser_executable_path() == "C:/Brave/brave.exe"


def test_resolve_database_url_returns_the_configured_value(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host:5432/db")

    assert config.resolve_database_url() == "postgresql://user:pass@host:5432/db"


def test_resolve_database_url_raises_when_unset(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        config.resolve_database_url()


def test_resolve_instance_role_defaults_to_local(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("JARVIS_ROLE", raising=False)

    assert config.resolve_instance_role() == "local"


def test_resolve_instance_role_returns_the_configured_value(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("JARVIS_ROLE", "cloud")

    assert config.resolve_instance_role() == "cloud"


def test_resolve_exa_api_key_returns_none_when_unset(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    assert config.resolve_exa_api_key() is None


def test_resolve_exa_api_key_returns_none_when_blank(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("EXA_API_KEY", "")

    assert config.resolve_exa_api_key() is None


def test_resolve_exa_api_key_returns_the_configured_value(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("EXA_API_KEY", "exa-abc123")

    assert config.resolve_exa_api_key() == "exa-abc123"


def test_resolve_timezone_defaults_to_tbilisi(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("JARVIS_TIMEZONE", raising=False)

    assert config.resolve_timezone().key == "Asia/Tbilisi"


def test_resolve_timezone_returns_the_configured_zone(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("JARVIS_TIMEZONE", "Europe/Berlin")

    assert config.resolve_timezone().key == "Europe/Berlin"


def test_resolve_timezone_falls_back_to_tbilisi_on_unknown_zone(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("JARVIS_TIMEZONE", "Not/AZone")

    assert config.resolve_timezone().key == "Asia/Tbilisi"


import datetime as _dt


def test_resolve_todo_list_time_defaults_to_nine(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("TODO_LIST_TIME", raising=False)

    assert config.resolve_todo_list_time() == _dt.time(9, 0)


def test_resolve_todo_list_time_reads_the_setting(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_TIME", "07:30")

    assert config.resolve_todo_list_time() == _dt.time(7, 30)


def test_resolve_todo_list_time_falls_back_on_garbage(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_TIME", "9am")

    assert config.resolve_todo_list_time() == _dt.time(9, 0)


def test_resolve_todo_list_lead_minutes_defaults_to_30(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("TODO_LIST_LEAD_MINUTES", raising=False)

    assert config.resolve_todo_list_lead_minutes() == 30


def test_resolve_todo_list_lead_minutes_reads_the_setting(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_LEAD_MINUTES", "45")

    assert config.resolve_todo_list_lead_minutes() == 45


def test_resolve_todo_list_lead_minutes_accepts_zero(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_LEAD_MINUTES", "0")

    assert config.resolve_todo_list_lead_minutes() == 0


def test_resolve_todo_list_lead_minutes_falls_back_on_garbage(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_LEAD_MINUTES", "soon")

    assert config.resolve_todo_list_lead_minutes() == 30


def test_resolve_todo_list_lead_minutes_falls_back_on_negative(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("TODO_LIST_LEAD_MINUTES", "-5")

    assert config.resolve_todo_list_lead_minutes() == 30


def test_groq_and_claude_resolvers_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    for var in ("GROQ_API_KEY", "GROQ_MODEL", "CLAUDE_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "JARVIS_SHOW_BRAIN"):
        monkeypatch.delenv(var, raising=False)
    assert config.resolve_groq_api_key() is None
    assert config.resolve_groq_model() == "openai/gpt-oss-120b"
    assert config.resolve_claude_model() == "sonnet"
    assert config.resolve_claude_oauth_token() is None
    assert config.resolve_show_brain() is True


def test_groq_and_claude_resolvers_read_settings(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    monkeypatch.setenv("JARVIS_SHOW_BRAIN", "Off")
    assert config.resolve_groq_api_key() == "gsk_x"
    assert config.resolve_groq_model() == "qwen/qwen3.8-27b"
    assert config.resolve_claude_model() == "claude-sonnet-5"
    assert config.resolve_claude_oauth_token() == "sk-ant-oat-x"
    assert config.resolve_show_brain() is False


def test_blank_secrets_are_none(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("GROQ_API_KEY", "  ")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    assert config.resolve_groq_api_key() is None
    assert config.resolve_claude_oauth_token() is None


# --- resolve_claude_cli_path -------------------------------------------------


def test_claude_cli_path_env_wins(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("CLAUDE_CLI_PATH", "  /opt/claude  ")
    assert config.resolve_claude_cli_path() == "/opt/claude"


def test_claude_cli_path_blank_env_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("CLAUDE_CLI_PATH", "   ")
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.resolve_claude_cli_path() is None


def test_claude_cli_path_windows_finds_npm_native_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    appdata = tmp_path / "appdata"
    exe = appdata / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    assert config.resolve_claude_cli_path() == str(exe)


def test_claude_cli_path_windows_falls_back_to_local_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    home = tmp_path / "home"
    exe = home / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("APPDATA", str(tmp_path / "noappdata"))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert config.resolve_claude_cli_path() == str(exe)


def test_claude_cli_path_windows_none_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "a"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "b"))
    assert config.resolve_claude_cli_path() is None


def test_claude_cli_path_non_windows_is_none(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.resolve_claude_cli_path() is None


def test_resolve_nudge_time_defaults_to_half_past_seven_pm(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("NUDGE_TIME", raising=False)
    assert config.resolve_nudge_time() == _dt.time(19, 30)


def test_resolve_nudge_time_reads_the_setting(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("NUDGE_TIME", "20:15")
    assert config.resolve_nudge_time() == _dt.time(20, 15)


def test_resolve_nudge_time_falls_back_on_garbage(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("NUDGE_TIME", "evening")
    assert config.resolve_nudge_time() == _dt.time(19, 30)


def test_computer_access_resolvers_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    for var in ("BYPASS_HOURS", "COMPUTER_TASK_MAX_STEPS", "COMPUTER_TASK_TIMEOUT_MINUTES"):
        monkeypatch.delenv(var, raising=False)
    assert config.resolve_bypass_hours() == 12
    assert config.resolve_computer_task_max_steps() == 60
    assert config.resolve_computer_task_timeout_minutes() == 30


def test_computer_access_resolvers_read_settings(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("BYPASS_HOURS", "4")
    monkeypatch.setenv("COMPUTER_TASK_MAX_STEPS", "100")
    monkeypatch.setenv("COMPUTER_TASK_TIMEOUT_MINUTES", "10")
    assert config.resolve_bypass_hours() == 4
    assert config.resolve_computer_task_max_steps() == 100
    assert config.resolve_computer_task_timeout_minutes() == 10


def test_computer_access_resolvers_fall_back_on_invalid(monkeypatch, caplog):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    for value in ("soon", "0", "-3"):
        monkeypatch.setenv("BYPASS_HOURS", value)
        monkeypatch.setenv("COMPUTER_TASK_MAX_STEPS", value)
        monkeypatch.setenv("COMPUTER_TASK_TIMEOUT_MINUTES", value)
        caplog.clear()
        assert config.resolve_bypass_hours() == 12
        assert config.resolve_computer_task_max_steps() == 60
        assert config.resolve_computer_task_timeout_minutes() == 30
        assert len(caplog.records) == 3
