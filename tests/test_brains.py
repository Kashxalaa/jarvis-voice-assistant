import asyncio
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from brain.brains import FAIL_CONFIG, FAIL_LIMIT, FAIL_UNAVAILABLE, BrainResult, TurnContext
from brain.brains.claude import (
    BUILTIN_TOOLS,
    SCRUBBED_ENV_VARS,
    ClaudeBrain,
    _build_sdk_tools,
    _deny_non_jarvis_tools_hook,
    _TurnState,
    parse_limit,
)
from brain.brains.gemini import GeminiBrain
from brain.brains.groq import GroqBrain, GroqUnavailable, groq_chat
from brain.gemini_client import GeminiConfigError, GeminiUnavailableError, ToolReply
from brain.tools import ToolOutcome


def _ctx(**overrides):
    defaults = dict(
        conn=Mock(),
        system_prompt="SYSTEM",
        profile="",
        history=[],
        message="hello",
        image_bytes=None,
        image_mime_type=None,
        tools=[],
        executor=Mock(),
    )
    defaults.update(overrides)
    return TurnContext(**defaults)


def test_no_tool_call_returns_text():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text="Hi there", function_name=None, function_args=None
    )
    ctx = _ctx()

    result = GeminiBrain(client).respond(ctx)

    assert result == BrainResult("Hi there", "gemini", tools_ran=[])


def test_no_tool_call_with_empty_text_is_failure_unavailable():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name=None, function_args=None
    )
    ctx = _ctx()

    result = GeminiBrain(client).respond(ctx)

    assert result.text is None
    assert result.brain == "gemini"
    assert result.failure == FAIL_UNAVAILABLE


def test_action_outcome_prefers_models_preface_text():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text="Sure, doing that now.", function_name="run_known_action",
        function_args={"name": "vscode"},
    )
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Done — opened vscode.", "action")
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.text == "Sure, doing that now."
    assert result.tools_ran == ["run_known_action"]
    executor.execute.assert_called_once_with(
        "run_known_action", {"name": "vscode"}, model_text="Sure, doing that now."
    )


def test_action_outcome_falls_back_to_default_text_without_preface():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={"name": "vscode"}
    )
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Done — opened vscode.", "action")
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.text == "Done — opened vscode."


def test_fixed_outcome_ignores_models_preface_text():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text="I'll do that right away!", function_name="run_known_action",
        function_args={"name": "ghost"},
    )
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        "I don't have an action called 'ghost' set up yet.", "fixed"
    )
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.text == "I don't have an action called 'ghost' set up yet."


def test_information_outcome_uses_answer_with_tool_result():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="read_webpage", function_args={"url": "https://x.y"}
    )
    client.answer_with_tool_result.return_value = "Here's the gist, sir."
    executor = Mock()
    executor.execute.return_value = ToolOutcome("PAGE CONTENT", "information", fallback="PAGE CONTENT")
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.text == "Here's the gist, sir."
    kwargs = client.answer_with_tool_result.call_args.kwargs
    assert kwargs["tool_name"] == "read_webpage"
    assert kwargs["tool_result"] == "PAGE CONTENT"
    assert kwargs["user_message"] == "hello"
    assert kwargs["system_prompt"] == "SYSTEM"


def test_information_outcome_falls_back_when_answer_call_fails():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="read_webpage", function_args={"url": "https://x.y"}
    )
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        "PAGE CONTENT", "information", fallback="fallback text"
    )
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.text == "fallback text"


def test_handled_as_note_is_reported_through_brain_result():
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_note", function_args={"title": "T", "content": "C"}
    )
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        'Noted — I\'ll remember "T."', "action", handled_as_note=True
    )
    ctx = _ctx(executor=executor)

    result = GeminiBrain(client).respond(ctx)

    assert result.handled_as_note is True


def test_gemini_unavailable_error_is_failure_unavailable():
    client = Mock()
    client.generate_with_tools.side_effect = GeminiUnavailableError("all models exhausted")
    ctx = _ctx()

    result = GeminiBrain(client).respond(ctx)

    assert result.text is None
    assert result.brain == "gemini"
    assert result.failure == FAIL_UNAVAILABLE
    assert "all models exhausted" in result.failure_detail


def test_gemini_config_error_is_failure_config():
    client = Mock()
    client.generate_with_tools.side_effect = GeminiConfigError("404 model not found")
    ctx = _ctx()

    result = GeminiBrain(client).respond(ctx)

    assert result.text is None
    assert result.failure == FAIL_CONFIG
    assert "404 model not found" in result.failure_detail


def test_brain_name_is_gemini():
    assert GeminiBrain(Mock()).name == "gemini"


# --- GroqBrain -------------------------------------------------------------


def test_groq_brain_returns_answer_text(monkeypatch):
    monkeypatch.setattr(
        "brain.brains.groq.groq_chat",
        lambda *a, **kw: "Good evening, sir.",
    )
    ctx = _ctx()

    result = GroqBrain("key", "model").respond(ctx)

    assert result == BrainResult("Good evening, sir.", "groq")


def test_groq_brain_unavailable_error_is_failure_unavailable(monkeypatch):
    def fake_groq_chat(*a, **kw):
        raise GroqUnavailable("down")

    monkeypatch.setattr("brain.brains.groq.groq_chat", fake_groq_chat)
    ctx = _ctx()

    result = GroqBrain("key", "model").respond(ctx)

    assert result.text is None
    assert result.brain == "groq"
    assert result.failure == FAIL_UNAVAILABLE


def test_groq_brain_unavailable_error_is_logged(monkeypatch, caplog):
    def fake_groq_chat(*a, **kw):
        raise GroqUnavailable("down")

    monkeypatch.setattr("brain.brains.groq.groq_chat", fake_groq_chat)
    ctx = _ctx()

    with caplog.at_level("WARNING", logger="brain.brains.groq"):
        GroqBrain("key", "model").respond(ctx)

    assert any("down" in record.message for record in caplog.records)


def test_groq_brain_empty_answer_is_failure_unavailable(monkeypatch):
    monkeypatch.setattr("brain.brains.groq.groq_chat", lambda *a, **kw: "")
    ctx = _ctx()

    result = GroqBrain("key", "model").respond(ctx)

    assert result.text is None
    assert result.failure == FAIL_UNAVAILABLE


def test_groq_brain_never_passes_tools(monkeypatch):
    seen = {}

    def fake_groq_chat(api_key, model, messages, *, json_mode, timeout, **kw):
        seen["messages"] = messages
        seen["json_mode"] = json_mode
        return "Hi there."

    monkeypatch.setattr("brain.brains.groq.groq_chat", fake_groq_chat)
    ctx = _ctx(tools=[{"name": "read_webpage"}])

    GroqBrain("key", "model").respond(ctx)

    assert seen["json_mode"] is False
    assert all("tool" not in str(m).lower() or "read_webpage" not in str(m) for m in seen["messages"])
    assert not any("read_webpage" in m.get("content", "") for m in seen["messages"])


def test_groq_brain_name_is_groq():
    assert GroqBrain("key", "model").name == "groq"


# --- groq_chat ---------------------------------------------------------


def test_groq_chat_uses_json_mode_and_no_retries():
    fake_response = Mock()
    fake_response.choices = [Mock(message=Mock(content='{"tier": "normal"}'))]
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = fake_response

    with patch("brain.brains.groq.Groq", return_value=fake_client) as fake_groq_cls:
        result = groq_chat(
            "key", "model", [{"role": "user", "content": "hi"}], json_mode=True, timeout=4
        )

    assert result == '{"tier": "normal"}'
    fake_groq_cls.assert_called_once_with(api_key="key", timeout=4, max_retries=0)
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["model"] == "model"


def test_groq_chat_without_json_mode_omits_response_format():
    fake_response = Mock()
    fake_response.choices = [Mock(message=Mock(content="hello"))]
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = fake_response

    with patch("brain.brains.groq.Groq", return_value=fake_client):
        groq_chat("key", "model", [{"role": "user", "content": "hi"}], json_mode=False, timeout=15)

    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert "response_format" not in kwargs


def test_groq_chat_sdk_error_becomes_groq_unavailable():
    fake_client = Mock()
    fake_client.chat.completions.create.side_effect = RuntimeError("boom")

    with patch("brain.brains.groq.Groq", return_value=fake_client):
        with pytest.raises(GroqUnavailable):
            groq_chat("key", "model", [{"role": "user", "content": "hi"}], json_mode=False, timeout=15)


def test_groq_chat_empty_content_raises_groq_unavailable():
    fake_response = Mock()
    fake_response.choices = [Mock(message=Mock(content=""))]
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = fake_response

    with patch("brain.brains.groq.Groq", return_value=fake_client):
        with pytest.raises(GroqUnavailable):
            groq_chat("key", "model", [{"role": "user", "content": "hi"}], json_mode=False, timeout=15)


# --- ClaudeBrain -------------------------------------------------------------


def _result_message(**overrides):
    defaults = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        result=None,
    )
    defaults.update(overrides)
    return ResultMessage(**defaults)


def _respond_and_capture_options(monkeypatch, ctx=None, result_text="hi"):
    """Run ClaudeBrain.respond() against a fake query() that just yields a
    canned ResultMessage, and return (captured, result) where captured holds
    the ClaudeAgentOptions and prompt text the brain built."""
    captured = {}

    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        captured["prompt"] = prompt
        yield _result_message(result=result_text)

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    result = ClaudeBrain("tok").respond(ctx or _ctx())
    return captured, result


def _patch_create_sdk_mcp_server(monkeypatch):
    """Replace create_sdk_mcp_server with a stub that just records the real
    SdkMcpTool list ClaudeBrain built, so a fake query() can invoke the real
    tool handlers directly instead of going through the MCP wire protocol."""
    captured = {}

    def fake_create_sdk_mcp_server(name, tools=None, **kwargs):
        captured["tools"] = tools or []
        return {"type": "sdk", "name": name, "instance": None}

    monkeypatch.setattr(
        "brain.brains.claude.create_sdk_mcp_server", fake_create_sdk_mcp_server
    )
    return captured


def _respond_with_real_tool_calls(
    monkeypatch, ctx, tool_calls, raise_after=None, result_text=None
):
    """Run ClaudeBrain.respond(ctx), invoking the REAL sdk-tool handlers (as
    constructed by _build_sdk_tools/_make_sdk_tool) for each (name, args) in
    tool_calls, then either yield a ResultMessage(result=result_text), raise
    raise_after, or both are left unset (caller's choice)."""
    captured = _patch_create_sdk_mcp_server(monkeypatch)

    async def fake_query(*, prompt, options=None, transport=None):
        by_name = {t.name: t for t in captured["tools"]}
        for tool_name, args in tool_calls:
            await by_name[tool_name].handler(args)
        if result_text is not None:
            yield _result_message(result=result_text)
        if raise_after is not None:
            raise raise_after

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    return ClaudeBrain("tok").respond(ctx)


def _tool_decl(name):
    return {"name": name, "description": "d", "parameters_json_schema": {"type": "object", "properties": {}}}


def test_parse_limit_variants():
    assert parse_limit("You've hit your session limit · resets 3:45pm") == (True, "3:45pm")
    assert parse_limit("You've hit your Sonnet limit · resets 15:45") == (True, "15:45")
    assert parse_limit("You've hit your weekly limit") == (True, "")
    assert parse_limit("Connection reset by peer") == (False, "")


def test_no_token_is_config_failure():
    r = ClaudeBrain(None).respond(_ctx())
    assert (r.text, r.failure) == (None, FAIL_CONFIG)


def test_only_jarvis_tools_allowed_and_builtins_disallowed(monkeypatch):
    ctx = _ctx(tools=[_tool_decl("create_reminder")])

    captured, _ = _respond_and_capture_options(monkeypatch, ctx=ctx)

    options = captured["options"]
    assert options.allowed_tools == ["mcp__jarvis__create_reminder"]
    for builtin in BUILTIN_TOOLS:
        assert builtin in options.disallowed_tools


def test_model_passed_through(monkeypatch):
    captured = {}

    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        yield _result_message(result="hi")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    ClaudeBrain("tok", model="sonnet").respond(_ctx())

    assert captured["options"].model == "sonnet"


# --- CRITICAL fix: built-in tools, filesystem settings, external MCP servers


def test_all_builtin_tools_disabled_via_tools_option(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert captured["options"].tools == []


def test_filesystem_settings_isolated(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert captured["options"].setting_sources == []


def test_strict_mcp_config_true(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert captured["options"].strict_mcp_config is True


# --- Important fix: handled_as_note propagated through BrainResult


def test_handled_as_note_reported_through_brain_result(monkeypatch):
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        'Noted — I\'ll remember "T."', "action", handled_as_note=True
    )
    ctx = _ctx(executor=executor, tools=[_tool_decl("create_note")])

    result = _respond_with_real_tool_calls(
        monkeypatch, ctx,
        tool_calls=[("create_note", {"title": "T", "content": "C"})],
        result_text="Noted that for you.",
    )

    assert result.handled_as_note is True
    assert result.text == "Noted that for you."
    assert result.tools_ran == ["create_note"]


# --- Important fix: auth env hardening


def test_env_blocks_alternate_auth_sources(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    captured, _ = _respond_and_capture_options(monkeypatch)

    env = captured["options"].env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "0"
    assert env["CLAUDE_CODE_USE_VERTEX"] == "0"
    assert env["CLAUDE_CODE_USE_FOUNDRY"] == "0"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == ""
    assert env["ANTHROPIC_UNIX_SOCKET"] == ""
    assert "ANTHROPIC_BASE_URL" not in env
    assert "HTTPS_PROXY" not in env


def test_env_overrides_custom_anthropic_base_url_when_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    captured, _ = _respond_and_capture_options(monkeypatch)

    assert captured["options"].env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


# --- Important fix: scrub Jarvis's own secrets from the subprocess env


def test_env_scrubs_jarvis_secrets(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)

    env = captured["options"].env
    assert SCRUBBED_ENV_VARS  # sanity: the constant isn't empty
    for var in SCRUBBED_ENV_VARS:
        assert env[var] == ""


# --- Important fix: prompt-injection chain (info tool -> PC action same turn)


def test_pc_action_refused_after_info_tool_ran_this_turn():
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        "PAGE CONTENT", "information", fallback="PAGE CONTENT"
    )
    ctx = _ctx(
        executor=executor,
        tools=[_tool_decl("read_webpage"), _tool_decl("run_one_off_action")],
    )
    state = _TurnState()
    sdk_tools = _build_sdk_tools(ctx, state)
    by_name = {t.name: t for t in sdk_tools}

    info_result = asyncio.run(by_name["read_webpage"].handler({"url": "https://x.y"}))
    action_result = asyncio.run(
        by_name["run_one_off_action"].handler({"kind": "app", "value": "notepad"})
    )

    assert info_result == {"content": [{"type": "text", "text": "PAGE CONTENT"}]}
    assert action_result == {
        "content": [{
            "type": "text",
            "text": (
                "For safety I don't open or run things in the same answer "
                "where I've read web or stored content. Ask me directly and "
                "I'll do it."
            ),
        }]
    }
    assert state.tools_ran == ["read_webpage"]
    executor.execute.assert_called_once_with("read_webpage", {"url": "https://x.y"})


def test_pc_action_executed_when_no_prior_info_tool_this_turn():
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Done.", "action")
    ctx = _ctx(executor=executor, tools=[_tool_decl("run_one_off_action")])
    state = _TurnState()
    sdk_tools = _build_sdk_tools(ctx, state)

    result = asyncio.run(sdk_tools[0].handler({"kind": "app", "value": "notepad"}))

    assert result == {"content": [{"type": "text", "text": "Done."}]}
    assert state.tools_ran == ["run_one_off_action"]
    executor.execute.assert_called_once_with(
        "run_one_off_action", {"kind": "app", "value": "notepad"}
    )


def test_confirm_pending_action_refused_after_info_tool_ran_this_turn():
    executor = Mock()
    executor.execute.return_value = ToolOutcome(
        "PAGE CONTENT", "information", fallback="PAGE CONTENT"
    )
    ctx = _ctx(
        executor=executor,
        tools=[_tool_decl("read_webpage"), _tool_decl("confirm_pending_action")],
    )
    state = _TurnState()
    sdk_tools = _build_sdk_tools(ctx, state)
    by_name = {t.name: t for t in sdk_tools}

    info_result = asyncio.run(by_name["read_webpage"].handler({"url": "https://x.y"}))
    confirm_result = asyncio.run(
        by_name["confirm_pending_action"].handler({"confirmed": True})
    )

    assert info_result == {"content": [{"type": "text", "text": "PAGE CONTENT"}]}
    assert confirm_result == {
        "content": [{
            "type": "text",
            "text": (
                "For safety I don't open or run things in the same answer "
                "where I've read web or stored content. Ask me directly and "
                "I'll do it."
            ),
        }]
    }
    assert state.tools_ran == ["read_webpage"]
    executor.execute.assert_called_once_with("read_webpage", {"url": "https://x.y"})


def test_confirm_pending_action_executed_when_no_prior_info_tool_this_turn():
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Done — saved and opened 'x'.", "action")
    ctx = _ctx(executor=executor, tools=[_tool_decl("confirm_pending_action")])
    state = _TurnState()
    sdk_tools = _build_sdk_tools(ctx, state)

    result = asyncio.run(sdk_tools[0].handler({"confirmed": True}))

    assert result == {"content": [{"type": "text", "text": "Done — saved and opened 'x'."}]}
    assert state.tools_ran == ["confirm_pending_action"]
    executor.execute.assert_called_once_with("confirm_pending_action", {"confirmed": True})


# --- Important fix: tool name recorded before the executor is awaited


def test_tool_name_recorded_before_execution_survives_cancellation():
    def slow_execute(name, args):
        time.sleep(0.2)
        return ToolOutcome("done", "action")

    executor = Mock()
    executor.execute.side_effect = slow_execute
    ctx = _ctx(executor=executor, tools=[_tool_decl("create_reminder")])
    state = _TurnState()
    sdk_tools = _build_sdk_tools(ctx, state)

    async def run():
        await asyncio.wait_for(sdk_tools[0].handler({"text": "x"}), timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())

    assert state.tools_ran == ["create_reminder"]


def test_token_and_empty_api_key_passed_to_env(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)

    env = captured["options"].env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_final_text_returned_from_result_message(monkeypatch):
    async def fake_query(*, prompt, options=None, transport=None):
        yield AssistantMessage(content=[TextBlock("draft")], model="claude-sonnet-5")
        yield _result_message(result="Final answer, sir.")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    result = ClaudeBrain("tok").respond(_ctx())

    assert result == BrainResult("Final answer, sir.", "sonnet", tools_ran=[])


def test_two_tool_calls_executed_via_executor_and_listed_in_tools_ran():
    executor = Mock()
    executor.execute.side_effect = [
        ToolOutcome("R1", "information"),
        ToolOutcome("R2", "information"),
    ]
    ctx = _ctx(executor=executor, tools=[_tool_decl("t1"), _tool_decl("t2")])
    state = _TurnState()

    sdk_tools = _build_sdk_tools(ctx, state)

    result1 = asyncio.run(sdk_tools[0].handler({"a": 1}))
    result2 = asyncio.run(sdk_tools[1].handler({"b": 2}))

    assert state.tools_ran == ["t1", "t2"]
    assert result1 == {"content": [{"type": "text", "text": "R1"}]}
    assert result2 == {"content": [{"type": "text", "text": "R2"}]}
    executor.execute.assert_any_call("t1", {"a": 1})
    executor.execute.assert_any_call("t2", {"b": 2})


def test_result_error_with_limit_text_is_fail_limit_with_reset(monkeypatch):
    from claude_agent_sdk import ResultError

    async def fake_query(*, prompt, options=None, transport=None):
        if False:
            yield  # pragma: no cover - makes this an async generator
        raise ResultError(
            "Claude Code returned an error result: You've hit your Sonnet limit · resets 3:45pm",
            data={"subtype": "error_during_execution"},
        )

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    result = ClaudeBrain("tok").respond(_ctx())

    assert result.text is None
    assert result.failure == FAIL_LIMIT
    assert result.failure_detail == "3:45pm"


def test_is_error_result_message_with_limit_text_is_fail_limit(monkeypatch):
    async def fake_query(*, prompt, options=None, transport=None):
        yield _result_message(
            is_error=True, result="You've hit your Sonnet limit · resets 3:45pm"
        )

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    result = ClaudeBrain("tok").respond(_ctx())

    assert result.text is None
    assert result.failure == FAIL_LIMIT
    assert result.failure_detail == "3:45pm"


def test_generic_exception_after_one_tool_ran_is_fail_unavailable_with_tools_ran(monkeypatch):
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Reminder set.", "fixed")
    ctx = _ctx(executor=executor, tools=[_tool_decl("create_reminder")])

    result = _respond_with_real_tool_calls(
        monkeypatch, ctx,
        tool_calls=[("create_reminder", {"text": "wake up"})],
        raise_after=RuntimeError("boom"),
    )

    assert result.text is None
    assert result.failure == FAIL_UNAVAILABLE
    assert result.tools_ran == ["create_reminder"]
    executor.execute.assert_called_once_with("create_reminder", {"text": "wake up"})


def test_timeout_is_fail_unavailable(monkeypatch):
    async def fake_query(*, prompt, options=None, transport=None):
        await asyncio.sleep(10)
        yield _result_message(result="too late")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    result = ClaudeBrain("tok", timeout=0.01).respond(_ctx())

    assert result.text is None
    assert result.failure == FAIL_UNAVAILABLE
    assert result.failure_detail  # str(exc) or type(exc).__name__ — never empty


def test_empty_answer_is_fail_unavailable(monkeypatch):
    async def fake_query(*, prompt, options=None, transport=None):
        yield _result_message(result=None)

    monkeypatch.setattr("brain.brains.claude.query", fake_query)

    result = ClaudeBrain("tok").respond(_ctx())

    assert result.text is None
    assert result.failure == FAIL_UNAVAILABLE


def test_brain_name_is_sonnet():
    assert ClaudeBrain("tok").name == "sonnet"


# --- Minor fix: PreToolUse hook replaces can_use_tool


def test_deny_non_jarvis_tools_hook_allows_jarvis_tools():
    result = asyncio.run(
        _deny_non_jarvis_tools_hook({"tool_name": "mcp__jarvis__create_reminder"}, "id1", {})
    )
    assert result == {}


def test_deny_non_jarvis_tools_hook_denies_others():
    result = asyncio.run(_deny_non_jarvis_tools_hook({"tool_name": "Bash"}, "id1", {}))

    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_hook_wired_into_options(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)

    matchers = captured["options"].hooks["PreToolUse"]
    assert any(_deny_non_jarvis_tools_hook in m.hooks for m in matchers)


# --- Minor fix: permission_mode / cwd / no session resume / no-session-persistence


def test_permission_mode_is_dont_ask(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert captured["options"].permission_mode == "dontAsk"


def test_cwd_is_jarvis_claude_scratch_dir(monkeypatch):
    from brain.brains import claude as claude_module

    captured, _ = _respond_and_capture_options(monkeypatch)
    # conftest swaps in a tmp dir so tests never touch the real home.
    assert captured["options"].cwd == str(claude_module._scratch_dir())

    fake_home = Path(captured["options"].cwd) / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    real = claude_module._scratch_dir._real()
    assert real == fake_home / ".jarvis" / "claude"
    assert real.is_dir()


def test_no_session_resume_options(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert captured["options"].resume is None
    assert captured["options"].continue_conversation is False


def test_no_session_persistence_extra_arg(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    assert "no-session-persistence" in captured["options"].extra_args
    assert captured["options"].extra_args["no-session-persistence"] is None


def test_image_note_appended_to_prompt_when_image_present(monkeypatch):
    ctx = _ctx(image_bytes=b"fake-bytes", image_mime_type="image/png")

    captured, _ = _respond_and_capture_options(monkeypatch, ctx=ctx)

    assert captured["prompt"].endswith("[The user sent an image you can't see.]")


# --- final-review fixes ------------------------------------------------------


def _groq_error(cls, status=429):
    import httpx

    request = httpx.Request("POST", "http://x")
    if cls.__name__ == "APITimeoutError":
        return cls(request=request)
    return cls("err", response=httpx.Response(status, request=request), body=None)


def test_groq_chat_classifies_errors():
    from groq import APITimeoutError, RateLimitError

    for exc, kind in (
        (_groq_error(RateLimitError), "rate_limit"),
        (_groq_error(APITimeoutError), "timeout"),
        (RuntimeError("x"), "other"),
    ):
        fake_client = Mock()
        fake_client.chat.completions.create.side_effect = exc
        with patch("brain.brains.groq.Groq", return_value=fake_client):
            with pytest.raises(GroqUnavailable) as info:
                groq_chat("k", "m", [{"role": "user", "content": "hi"}], json_mode=False, timeout=1)
        assert info.value.kind == kind


def test_groq_chat_passes_reasoning_effort():
    fake_response = Mock()
    fake_response.choices = [Mock(message=Mock(content="x"))]
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = fake_response
    with patch("brain.brains.groq.Groq", return_value=fake_client):
        groq_chat("k", "m", [], json_mode=False, timeout=1, reasoning_effort="low")
    assert fake_client.chat.completions.create.call_args.kwargs["reasoning_effort"] == "low"
    with patch("brain.brains.groq.Groq", return_value=fake_client):
        groq_chat("k", "m", [], json_mode=False, timeout=1)
    assert "reasoning_effort" not in fake_client.chat.completions.create.call_args.kwargs


def test_claude_brain_passes_cli_path_to_options(monkeypatch):
    captured = {}

    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        yield _result_message(result="hi")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    ClaudeBrain("tok", cli_path="C:/x/claude.exe").respond(_ctx())
    assert captured["options"].cli_path == "C:/x/claude.exe"
    ClaudeBrain("tok").respond(_ctx())
    assert captured["options"].cli_path is None


def test_claude_prompt_does_not_repeat_system_prompt(monkeypatch):
    captured, _ = _respond_and_capture_options(
        monkeypatch, ctx=_ctx(system_prompt="UNIQUE-SYSTEM", profile="- k: v", message="hey")
    )
    assert "UNIQUE-SYSTEM" not in captured["prompt"]
    assert "UNIQUE-SYSTEM" in captured["options"].system_prompt
    assert "- k: v" in captured["prompt"] and "user: hey" in captured["prompt"]


def test_claude_brain_sets_skip_version_check(monkeypatch):
    import os

    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    ClaudeBrain("tok")
    assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)


def test_information_tool_is_not_a_side_effect(monkeypatch):
    executor = Mock()
    executor.execute.return_value = ToolOutcome("results", "information")
    ctx = _ctx(executor=executor, tools=[_tool_decl("web_search")])
    result = _respond_with_real_tool_calls(
        monkeypatch, ctx, tool_calls=[("web_search", {})], raise_after=RuntimeError("boom")
    )
    assert result.tools_ran == ["web_search"]
    assert result.side_effect_tools_ran == []


def test_action_tool_is_a_side_effect(monkeypatch):
    executor = Mock()
    executor.execute.return_value = ToolOutcome("ok", "fixed")
    ctx = _ctx(executor=executor, tools=[_tool_decl("create_reminder")])
    result = _respond_with_real_tool_calls(
        monkeypatch, ctx, tool_calls=[("create_reminder", {})], raise_after=RuntimeError("boom")
    )
    assert result.side_effect_tools_ran == ["create_reminder"]


def test_gemini_brain_side_effect_tools():
    for kind, expected in (("action", ["x"]), ("fixed", ["x"]), ("information", [])):
        client = Mock()
        client.generate_with_tools.return_value = ToolReply(
            text="t", function_name="x", function_args={}
        )
        client.answer_with_tool_result.return_value = "answer"
        executor = Mock()
        executor.execute.return_value = ToolOutcome("o", kind)
        result = GeminiBrain(client).respond(_ctx(executor=executor))
        assert result.side_effect_tools_ran == expected


def _assistant_error(error):
    return AssistantMessage(content=[TextBlock("You have hit it")], model="m", error=error)


@pytest.mark.parametrize(
    "error,failure",
    [("rate_limit", FAIL_LIMIT), ("billing_error", FAIL_LIMIT), ("authentication_failed", FAIL_CONFIG)],
)
def test_assistant_message_error_is_failure(monkeypatch, error, failure):
    async def fake_query(*, prompt, options=None, transport=None):
        yield _assistant_error(error)
        yield _result_message(result="some text")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    result = ClaudeBrain("tok").respond(_ctx())
    assert result.text is None and result.failure == failure


def test_rejected_rate_limit_event_is_limit_with_local_reset_time(monkeypatch):
    from datetime import datetime

    from claude_agent_sdk import RateLimitEvent, RateLimitInfo

    ts = 1_800_000_000

    async def fake_query(*, prompt, options=None, transport=None):
        yield RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=ts), uuid="u", session_id="s"
        )
        yield _result_message(result=None, is_error=True)

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    result = ClaudeBrain("tok").respond(_ctx())
    assert result.failure == FAIL_LIMIT
    assert result.failure_detail == datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def test_limit_text_answer_is_never_returned_as_answer(monkeypatch):
    async def fake_query(*, prompt, options=None, transport=None):
        yield _result_message(result="You've hit your Sonnet limit · resets 3:45pm")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    result = ClaudeBrain("tok").respond(_ctx())
    assert result.text is None and result.failure == FAIL_LIMIT
    assert result.failure_detail == "3:45pm"


def _multi_reply(calls, text=None):
    reply = ToolReply(text=text, function_name=calls[0][0], function_args=calls[0][1])
    reply.calls = list(calls)
    return reply


def test_gemini_runs_every_tool_call_in_one_reply():
    client = Mock()
    client.generate_with_tools.return_value = _multi_reply([
        ("create_reminder", {"text": "Linux Administration Course"}),
        ("create_reminder", {"text": "Networking Fundamentals Course"}),
    ])
    executor = Mock()
    executor.execute.side_effect = [
        ToolOutcome("On your list: Linux Administration Course", "fixed"),
        ToolOutcome("On your list: Networking Fundamentals Course", "fixed"),
    ]

    result = GeminiBrain(client).respond(_ctx(executor=executor))

    assert [c.args[1]["text"] for c in executor.execute.call_args_list] == [
        "Linux Administration Course", "Networking Fundamentals Course",
    ]
    assert result.text == (
        "On your list: Linux Administration Course\n"
        "On your list: Networking Fundamentals Course"
    )
    assert result.tools_ran == ["create_reminder", "create_reminder"]
    assert result.side_effect_tools_ran == ["create_reminder", "create_reminder"]


def test_gemini_multi_call_with_information_phrases_it_after_the_actions():
    client = Mock()
    client.generate_with_tools.return_value = _multi_reply([
        ("create_reminder", {"text": "x"}),
        ("get_weather", {}),
    ])
    client.answer_with_tool_result.return_value = "Sunny, sir."
    executor = Mock()
    executor.execute.side_effect = [
        ToolOutcome("Reminder set: x", "fixed"),
        ToolOutcome("WEATHER DATA", "information", fallback="WEATHER DATA"),
    ]

    result = GeminiBrain(client).respond(_ctx(executor=executor))

    assert result.text == "Reminder set: x\nSunny, sir."
    assert client.answer_with_tool_result.call_args.kwargs["tool_result"] == "WEATHER DATA"
    assert result.side_effect_tools_ran == ["create_reminder"]


def test_gemini_caps_the_number_of_tool_calls_in_one_reply():
    client = Mock()
    client.generate_with_tools.return_value = _multi_reply(
        [("create_reminder", {"text": str(i)}) for i in range(15)]
    )
    executor = Mock()
    executor.execute.return_value = ToolOutcome("ok", "fixed")

    GeminiBrain(client).respond(_ctx(executor=executor))

    assert executor.execute.call_count == 10



# --- ClaudeBrain computer mode ------------------------------------------------

import os  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from brain import computer_state  # noqa: E402
from brain.approvals import ApprovalResult  # noqa: E402
from brain.brains import claude as claude_mod  # noqa: E402
from brain.brains.claude import ComputerConfig, load_protected_packages  # noqa: E402
from brain.computer_tools import ComputerTaskState  # noqa: E402

PRE = "PreToolUse"


class _NoClose:
    """Wraps the shared test connection so the brain's close() doesn't close it."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *a, **kw):
        return self._conn.execute(*a, **kw)

    def close(self):
        pass


class FakeBroker:
    def __init__(self, result=None, exc=None):
        self.result = result or ApprovalResult(True, "allowed", "owner")
        self.exc = exc
        self.requests = []
        self.loops = []

    async def request(self, req, timeout_s=600):
        self.requests.append((req, timeout_s))
        self.loops.append(asyncio.get_running_loop())
        if self.exc:
            raise self.exc
        return self.result


def _cfg(tmp_path, conn, broker=None, machine="pc", os_name="windows", **kw):
    home = tmp_path / "home"
    jarvis = tmp_path / "jarvis"
    home.mkdir(exist_ok=True)
    jarvis.mkdir(exist_ok=True)
    return ComputerConfig(
        machine=machine, os_name=os_name, home=str(home), jarvis_dir=str(jarvis),
        broker=broker or FakeBroker(), conn_factory=lambda: _NoClose(conn), **kw,
    )


def _run(monkeypatch, cfg, script, ctx=None, result_text="done", captured=None):
    """Run ClaudeBrain(computer=cfg).respond() against a fake SDK whose query() calls
    script(pre_hook, post_hook, options) and then yields a final result."""
    captured = captured if captured is not None else {}

    async def fake_query(*, prompt, options=None, transport=None):
        captured["options"] = options
        captured["prompt"] = prompt
        hooks = options.hooks
        captured["pre_matcher"] = hooks[PRE][0]
        await script(hooks[PRE][0].hooks[0], hooks["PostToolUse"][0].hooks[0], options)
        yield _result_message(result=result_text)

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    return ClaudeBrain("tok", computer=cfg).respond(ctx or _ctx())


def _call(pre, tool, tool_input, uid="u1"):
    return pre({"tool_name": tool, "tool_input": tool_input}, uid, None)


def _decision(out):
    return out.get("hookSpecificOutput", {}).get("permissionDecision")


def _outcomes(conn):
    return [(r["tool"], r["verdict"], r["outcome"]) for r in reversed(computer_state.recent_activity(conn, 50))]


def _bypass_on(conn):
    computer_state.set_bypass(conn, datetime.now(timezone.utc) + timedelta(hours=1))


@pytest.fixture(autouse=True)
def _fresh_protected_cache(monkeypatch):
    # the real installed-distribution list is covered by its own test below
    monkeypatch.setattr(claude_mod, "_installed_distributions", lambda: frozenset())
    claude_mod._protected_cache.clear()
    claude_mod._dirs_cache.clear()
    yield
    claude_mod._protected_cache.clear()
    claude_mod._dirs_cache.clear()


# toolkit / options -------------------------------------------------------------

def test_pc_toolkit_and_options(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn, max_steps=33, timeout_s=600)

    async def script(pre, post, options):
        pass

    result = _run(monkeypatch, cfg, script, captured=captured)
    opts = captured["options"]
    assert result.text == "done"
    assert opts.tools == ["Read", "Write", "Edit", "Glob", "Grep", "PowerShell"]
    assert opts.setting_sources == [] and opts.strict_mcp_config is True
    assert opts.max_turns == 33 and opts.cwd == cfg.home
    assert opts.extra_args == {"no-session-persistence": None}
    for banned in ("WebSearch", "WebFetch", "Bash"):
        assert banned in opts.disallowed_tools and banned not in opts.allowed_tools
    assert "PowerShell" not in opts.disallowed_tools
    for name in ("move_to_trash", "make_folder", "move_paths", "copy_paths", "install_software",
                 "open_path", "send_file", "open_in_editor"):
        assert f"mcp__jarvis__{name}" in opts.allowed_tools
    assert "mcp__jarvis__queue_pc_task" not in opts.allowed_tools  # server-only (M6)
    assert "move_to_trash, install_software and open_path rather than shell commands" in opts.system_prompt


def test_linux_pc_uses_bash(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn, os_name="linux")

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    assert captured["options"].tools == ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    assert "PowerShell" in captured["options"].disallowed_tools


def test_server_toolkit_has_no_file_tools_and_only_queue_pc_task(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn, machine="server", os_name="linux")
    sdk_tools = _patch_create_sdk_mcp_server(monkeypatch)

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    opts = captured["options"]
    assert opts.tools == ["Bash", "Read", "Glob", "Grep"]
    custom = {t.name for t in sdk_tools["tools"]}
    assert custom == {"queue_pc_task"}
    for denied in ("Write", "Edit", "WebSearch", "WebFetch"):
        assert denied in opts.disallowed_tools
    assert "mcp__jarvis__move_to_trash" not in opts.allowed_tools
    assert "move_to_trash" not in opts.system_prompt


def test_computer_env_scrubs_secrets_and_blanks_git_config(monkeypatch, tmp_path, conn):
    captured = {}

    async def script(pre, post, options):
        pass

    _run(monkeypatch, _cfg(tmp_path, conn), script, captured=captured)
    env = captured["options"].env
    for var in SCRUBBED_ENV_VARS:
        assert env[var] == ""
    assert env["ANTHROPIC_API_KEY"] == "" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("core.fsmonitor", "")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("diff.external", "")


def test_a1_env_has_no_git_config_and_computer_none_is_unchanged(monkeypatch):
    captured, _ = _respond_and_capture_options(monkeypatch)
    opts = captured["options"]
    assert "GIT_CONFIG_COUNT" not in opts.env
    assert opts.tools == [] and opts.max_turns == 8
    assert opts.hooks["PreToolUse"][0].hooks == [_deny_non_jarvis_tools_hook]
    assert "PostToolUse" not in opts.hooks


def test_pre_hook_timeout_outlasts_the_approval_wait(monkeypatch, tmp_path, conn):
    captured = {}

    async def script(pre, post, options):
        pass

    _run(monkeypatch, _cfg(tmp_path, conn), script, captured=captured)
    assert captured["pre_matcher"].timeout >= 660


# verdict handling --------------------------------------------------------------

def test_refuse_never_asks_and_denies_with_reason(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "PowerShell", {"command": "format D: /q"})
        _bypass_on(conn)
        out["r2"] = await _call(pre, "PowerShell", {"command": "format D: /q"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny" and _decision(out["r2"]) == "deny"
    assert out["r"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert broker.requests == []
    assert [o for _, v, o in _outcomes(conn)] == ["denied", "denied"]
    assert _outcomes(conn)[0][1] == "REFUSE"


def test_run_verdict_is_allowed_without_asking_and_logged_auto(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    (tmp_path / "home" / "a.txt").write_text("hi")
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Read", {"file_path": str(tmp_path / "home" / "a.txt")})

    _run(monkeypatch, cfg, script)
    assert out["r"] == {} and broker.requests == []
    assert _outcomes(conn) == [("Read", "RUN", "auto")]


def test_always_ask_asks_even_in_bypass(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    _bypass_on(conn)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "PowerShell", {"command": "git push origin main",
                                                   "description": "publish the work"})

    _run(monkeypatch, cfg, script)
    assert out["r"] == {}
    assert len(broker.requests) == 1
    req, timeout = broker.requests[0]
    assert req.machine == "pc" and req.detail == "git push origin main" and req.reason.endswith("Claude says: publish the work")
    assert timeout == 600
    assert _outcomes(conn) == [("PowerShell", "ALWAYS_ASK", "allowed")]


def test_ask_unless_bypass_is_skipped_in_bypass_on_pc(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    _bypass_on(conn)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["old.txt"]})

    _run(monkeypatch, cfg, script)
    assert out["r"] == {} and broker.requests == []
    assert _outcomes(conn) == [("mcp__jarvis__move_to_trash", "ASK_UNLESS_BYPASS", "bypass")]


def test_ask_unless_bypass_asks_without_bypass(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "mcp__jarvis__move_paths",
                               {"moves": [{"from": "a.txt", "to": "b.txt"}, {"from": "c.txt", "to": "d.txt"}]})

    _run(monkeypatch, cfg, script)
    assert out["r"] == {}
    req, _ = broker.requests[0]
    assert req.batch_size == 2 and "a.txt -> b.txt" in req.detail and "c.txt -> d.txt" in req.detail
    assert _outcomes(conn)[0][2] == "allowed"


def test_bypass_does_not_apply_on_the_server(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker, machine="server", os_name="linux")
    _bypass_on(conn)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Bash", {"command": "df -h"})

    _run(monkeypatch, cfg, script)
    assert len(broker.requests) == 1 and broker.requests[0][0].machine == "server"
    assert out["r"] == {}


@pytest.mark.parametrize("result,outcome", [
    (ApprovalResult(False, "denied", "owner"), "denied"),
    (ApprovalResult(False, "timeout", None), "timeout"),
    (ApprovalResult(False, "error", None), "error"),
    (ApprovalResult(False, "weird", None), "denied"),
])
def test_broker_denial_denies_the_tool_and_logs(monkeypatch, tmp_path, conn, result, outcome):
    cfg = _cfg(tmp_path, conn, FakeBroker(result))
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "PowerShell", {"command": "git push"})

    r = _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"
    assert _outcomes(conn) == [("PowerShell", "ALWAYS_ASK", outcome)]
    assert r.side_effect_tools_ran == []


def test_allowed_must_be_exactly_true(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, FakeBroker(SimpleNamespace(allowed="yes", outcome="allowed", approver="x")))
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "PowerShell", {"command": "git push"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"


def test_broker_exception_fails_closed(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, FakeBroker(exc=RuntimeError("discord is down")))
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "PowerShell", {"command": "git push"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"
    assert _outcomes(conn) == [("PowerShell", "ERROR", "error")]


def test_guard_exception_fails_closed(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(claude_mod, "evaluate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    cfg = _cfg(tmp_path, conn)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"
    assert _outcomes(conn)[0][2] == "error"


def test_database_failure_fails_closed(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)

    def broken():
        raise RuntimeError("db down")

    cfg.conn_factory = broken
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"


def test_unknown_and_out_of_toolkit_tools_are_denied(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker, machine="server", os_name="linux")
    out = {}

    async def script(pre, post, options):
        out["web"] = await _call(pre, "WebFetch", {"url": "https://x.y"})
        out["write"] = await _call(pre, "Write", {"file_path": "/tmp/x", "content": "x"})
        out["trash"] = await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a"]})
        out["nope"] = await _call(pre, "mcp__jarvis__nope", {})

    _run(monkeypatch, cfg, script)
    assert all(_decision(v) == "deny" for v in out.values())
    assert broker.requests == []


# bypass / safe mid-task (Review Focus) -------------------------------------------

def test_bypass_expiring_mid_task_makes_the_next_step_ask(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    computer_state.set_bypass(conn, datetime.now(timezone.utc) + timedelta(hours=1))

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a.txt"]}, "u1")
        assert broker.requests == []
        # the window closes between two steps
        computer_state.set_bypass(conn, datetime.now(timezone.utc) - timedelta(seconds=1))
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["b.txt"]}, "u2")

    _run(monkeypatch, cfg, script)
    assert len(broker.requests) == 1 and "b.txt" in broker.requests[0][0].detail
    assert [o for _, _, o in _outcomes(conn)] == ["bypass", "allowed"]


def test_safe_command_mid_task_makes_the_next_step_ask(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    _bypass_on(conn)

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a.txt"]}, "u1")
        computer_state.clear_bypass(conn)  # the owner types /safe
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["b.txt"]}, "u2")

    _run(monkeypatch, cfg, script)
    assert len(broker.requests) == 1


# taint -----------------------------------------------------------------------------

@pytest.mark.parametrize("web_tool", [
    "read_webpage", "web_search", "read_rss_feed", "get_youtube_info", "get_github_repo",
])
def test_web_tools_taint_the_task_and_suspend_bypass(monkeypatch, tmp_path, conn, web_tool):
    broker = FakeBroker()
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, broker, state=state)
    _bypass_on(conn)
    ctx = _ctx(tools=[_tool_decl(web_tool)])

    async def script(pre, post, options):
        await _call(pre, f"mcp__jarvis__{web_tool}", {"url": "https://x.y"}, "u1")
        assert state.tainted is True
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a.txt"]}, "u2")

    _run(monkeypatch, cfg, script, ctx=ctx)
    assert len(broker.requests) == 1  # bypass was suspended by the taint


def test_read_of_a_downloads_or_temp_file_taints(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, state=state)
    (tmp_path / "home" / "Downloads").mkdir()
    (tmp_path / "home" / "Documents").mkdir()
    (tmp_path / "home" / "Downloads" / "x.txt").write_text("x")
    (tmp_path / "home" / "Documents" / "y.txt").write_text("y")
    temp = tmp_path / "home" / "AppData" / "Local" / "Temp"
    temp.mkdir(parents=True)
    (temp / "t.txt").write_text("t")
    seen = {}

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": str(tmp_path / "home" / "Documents" / "y.txt")}, "u1")
        seen["after_docs"] = state.tainted
        await _call(pre, "Read", {"file_path": str(tmp_path / "home" / "Downloads" / "x.txt")}, "u2")
        seen["after_downloads"] = state.tainted
        state.tainted = False
        await _call(pre, "Read", {"file_path": str(temp / "t.txt")}, "u3")
        seen["after_temp"] = state.tainted

    _run(monkeypatch, cfg, script)
    assert seen == {"after_docs": False, "after_downloads": True, "after_temp": True}


def test_a_denied_web_tool_does_not_taint(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, state=state)

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__read_webpage", {"url": "x"})  # not in this turn's tool list -> denied

    _run(monkeypatch, cfg, script)
    assert state.tainted is False


def test_tainted_note_creation_asks(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    state = ComputerTaskState("t", tainted=True)
    cfg = _cfg(tmp_path, conn, broker, state=state)
    ctx = _ctx(tools=[_tool_decl("create_note")])

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__create_note", {"text": "x"})

    _run(monkeypatch, cfg, script, ctx=ctx)
    assert len(broker.requests) == 1


# stop / limits -----------------------------------------------------------------------

def test_stop_flag_denies_every_further_step(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker, state=state)
    out = {}

    async def script(pre, post, options):
        state.stopped = True
        out["r"] = await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"
    assert "Stopped by the owner" in out["r"]["hookSpecificOutput"]["permissionDecisionReason"]


def test_step_limit_denies_and_reports_the_limit_message(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, max_steps=2, timeout_s=1800)
    out = []

    async def script(pre, post, options):
        for i in range(3):
            out.append(await _call(pre, "Read", {"file_path": "a.txt"}, f"u{i}"))

    r = _run(monkeypatch, cfg, script, result_text="I did some things")
    assert [_decision(o) for o in out] == [None, None, "deny"]
    assert r.text.startswith("Stopped at the limit (2 steps / 30 min). Done so far:")
    assert r.text.endswith("Say 'continue' to finish.")
    assert r.failure is None


def test_max_turns_result_reports_the_limit_message(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, max_steps=60)

    async def fake_query(*, prompt, options=None, transport=None):
        await _call(options.hooks[PRE][0].hooks[0], "Read", {"file_path": "a.txt"})  # a computer task
        yield _result_message(subtype="error_max_turns", is_error=True, result=None)

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert r.failure is None
    assert r.text.startswith("Stopped at the limit (60 steps / 30 min). Done so far:")


def test_timeout_reports_the_limit_message_with_what_was_attached(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, max_steps=60, timeout_s=0.3)

    async def fake_query(*, prompt, options=None, transport=None):
        pre = options.hooks[PRE][0].hooks[0]
        post = options.hooks["PostToolUse"][0].hooks[0]
        await _call(pre, "Read", {"file_path": "a.txt"})
        await post({"tool_name": "Read", "tool_input": {}, "tool_response": "x" * 2000}, "u1", None)
        await asyncio.sleep(5)
        yield _result_message(result="never")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert r.failure is None
    assert r.text.startswith("Stopped at the limit (60 steps / 1 min). Done so far: 1 steps")
    assert [name for name, _ in r.attachments] == ["output.txt"]


# results ------------------------------------------------------------------------------

def test_write_and_edit_produce_a_unified_diff_attachment(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)
    f = tmp_path / "home" / "notes.txt"
    f.write_text("one\ntwo\n")
    new = tmp_path / "home" / "fresh.txt"

    async def script(pre, post, options):
        await _call(pre, "Edit", {"file_path": str(f), "old_string": "two", "new_string": "2"}, "e1")
        f.write_text("one\n2\n")  # the real tool does this between pre and post
        await post({"tool_name": "Edit", "tool_input": {"file_path": str(f)}, "tool_response": "ok"}, "e1", None)
        await _call(pre, "Write", {"file_path": str(new), "content": "hello\n"}, "w1")
        new.write_text("hello\n")
        await post({"tool_name": "Write", "tool_input": {"file_path": str(new)}, "tool_response": "ok"}, "w1", None)

    r = _run(monkeypatch, cfg, script)
    names = [n for n, _ in r.attachments]
    assert names == ["changes.diff"]
    diff = r.attachments[0][1].decode()
    assert "-two" in diff and "+2" in diff and "+hello" in diff
    assert str(f) in cfg.state.files_changed or os.path.realpath(str(f)) in cfg.state.files_changed


def test_diff_attachment_is_capped_at_8mb(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)
    cfg.state = ComputerTaskState("t", diffs=["+" + "x" * (9 * 1024 * 1024) + "\n"])

    async def script(pre, post, options):
        pass

    r = _run(monkeypatch, cfg, script)
    data = r.attachments[0][1]
    assert r.attachments[0][0] == "changes.diff"
    assert len(data) < 8 * 1024 * 1024 + 100 and data.endswith(b"[truncated at 8 MB]\n")


def test_binary_files_get_no_diff(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)
    f = tmp_path / "home" / "a.bin"
    f.write_bytes(b"\x00\x01\x02")

    async def script(pre, post, options):
        await _call(pre, "Write", {"file_path": str(f), "content": "x"}, "w1")
        f.write_bytes(b"\x00\x09")
        await post({"tool_name": "Write", "tool_input": {"file_path": str(f)}, "tool_response": "ok"}, "w1", None)

    r = _run(monkeypatch, cfg, script)
    assert r.attachments == []


def test_long_tool_output_goes_to_output_txt_and_short_does_not(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        await _call(pre, "PowerShell", {"command": "dir"}, "a")
        await _call(pre, "PowerShell", {"command": "dir"}, "b")
        await post({"tool_name": "PowerShell", "tool_input": {}, "tool_response": {"stdout": "y" * 2000}}, "a", None)
        await post({"tool_name": "PowerShell", "tool_input": {}, "tool_response": "short"}, "b", None)

    r = _run(monkeypatch, cfg, script)
    assert [n for n, _ in r.attachments] == ["output.txt"]
    assert r.attachments[0][1].count(b"y") == 2000


def test_no_attachments_by_default_and_in_a1(monkeypatch):
    assert BrainResult("x", "gemini").attachments == []
    _, result = _respond_and_capture_options(monkeypatch)
    assert result.attachments == []


def test_post_hook_never_raises(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)
    out = {}

    async def script(pre, post, options):
        out["r"] = await post({"tool_name": "Write", "tool_input": {"file_path": 5}, "tool_response": object()},
                              "z", None)

    _run(monkeypatch, cfg, script)
    assert out["r"] == {}


def test_progress_is_throttled_to_one_call_per_three_seconds(monkeypatch, tmp_path, conn):
    calls = []

    async def progress(text):
        calls.append(text)

    cfg = _cfg(tmp_path, conn, progress=progress)
    clock = {"t": 100.0}
    monkeypatch.setattr(claude_mod.time, "monotonic", lambda: clock["t"])

    async def script(pre, post, options):
        for i in range(4):
            await _call(pre, "Read", {"file_path": "a.txt"}, f"u{i}")      # t=100: first goes out
        clock["t"] = 102.9
        await _call(pre, "Read", {"file_path": "a.txt"}, "u4")              # still throttled
        clock["t"] = 103.1
        await _call(pre, "Read", {"file_path": "a.txt"}, "u5")              # goes out

    _run(monkeypatch, cfg, script)
    assert calls == ["⏳ Working… 1 steps done", "⏳ Working… 6 steps done"]


def test_progress_failure_does_not_break_the_task(monkeypatch, tmp_path, conn):
    async def progress(text):
        raise RuntimeError("discord hiccup")

    cfg = _cfg(tmp_path, conn, progress=progress)
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    assert out["r"] == {}


def test_allowed_computer_actions_count_as_side_effects(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": "a.txt"}, "r")
        await _call(pre, "mcp__jarvis__make_folder", {"path": "x"}, "m")

    r = _run(monkeypatch, cfg, script)
    assert r.tools_ran == ["Read", "mcp__jarvis__make_folder"]
    assert r.side_effect_tools_ran == ["mcp__jarvis__make_folder"]


# hook-supplied guard args ---------------------------------------------------------------

def _spy_evaluate(monkeypatch):
    seen = []
    real = claude_mod.evaluate

    def spy(tool, args, ctx):
        seen.append((tool, args, ctx))
        return real(tool, args, ctx)

    monkeypatch.setattr(claude_mod, "evaluate", spy)
    return seen


def test_guard_context_is_built_from_the_config(monkeypatch, tmp_path, conn):
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "requirements.txt").write_text(
        "discord.py\npsycopg[binary]>=3\n# comment\nPywin32; sys_platform == 'win32'\n-r other.txt\n\nrequests==2.0\n"
    )
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)
    _bypass_on(conn)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    _, _, ctx = seen[0]
    assert ctx.machine == "pc" and ctx.os_name == "windows" and ctx.cwd == cfg.home
    assert ctx.bypass is True and ctx.tainted is False
    assert ctx.protected_packages == frozenset({"discord.py", "psycopg", "pywin32", "requests"})


def test_protected_packages_are_parsed_once_and_fall_back_when_unreadable(tmp_path):
    d = tmp_path / "j"
    d.mkdir()
    (d / "requirements.txt").write_text("alpha\n")
    first = load_protected_packages(str(d))
    (d / "requirements.txt").write_text("beta\n")
    assert load_protected_packages(str(d)) is first == frozenset({"alpha"})
    fallback = load_protected_packages(str(tmp_path / "missing"))
    assert "discord.py" in fallback and "claude-agent-sdk" in fallback


def test_bypass_flag_is_false_on_server_even_if_the_db_says_on(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn, machine="server", os_name="linux")
    _bypass_on(conn)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": "a.txt"})

    _run(monkeypatch, cfg, script)
    assert seen[0][2].bypass is False


def test_write_gets_exists_and_real_path_without_env_expansion(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)
    home = tmp_path / "home"
    (home / "old.txt").write_text("x")
    monkeypatch.setenv("FOO", "expanded")

    async def script(pre, post, options):
        await _call(pre, "Write", {"file_path": str(home / "old.txt"), "content": "y"}, "a")
        await _call(pre, "Write", {"file_path": str(home / "new.txt"), "content": "y"}, "b")
        await _call(pre, "Read", {"file_path": "%FOO%\\x.txt"}, "c")
        await _call(pre, "Read", {"file_path": "$env:FOO/x.txt"}, "d")

    _run(monkeypatch, cfg, script)
    by_id = [a for _, a, _ in seen]
    assert by_id[0]["_exists"] is True and by_id[1]["_exists"] is False
    assert by_id[0]["_real_path"] == os.path.realpath(str(home / "old.txt"))
    assert by_id[2]["_real_path"] == os.path.realpath(os.path.join(str(home), "%FOO%\\x.txt"))
    assert "expanded" not in by_id[2]["_real_path"] and "expanded" not in by_id[3]["_real_path"]


def test_real_path_resolves_symlinks_and_falls_back_on_error(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)
    home = tmp_path / "home"
    target = tmp_path / "elsewhere.txt"
    target.write_text("x")
    link = home / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": str(link)}, "a")
        real_realpath = os.path.realpath

        def flaky(p, *a, **kw):
            if "weird" in str(p):
                raise ValueError("bad")
            return real_realpath(p, *a, **kw)

        monkeypatch.setattr(claude_mod.os.path, "realpath", flaky)
        await _call(pre, "Read", {"file_path": "weird-name"}, "b")

    _run(monkeypatch, cfg, script)
    assert seen[0][1]["_real_path"] == os.path.realpath(str(target))
    assert seen[1][1]["_real_path"] == "weird-name"


def test_copy_and_move_get_exists_dict_and_real_paths(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)
    home = tmp_path / "home"
    (home / "taken.txt").write_text("x")

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__copy_paths", {"copies": [
            {"from": "a.txt", "to": "taken.txt"}, {"from": "b.txt", "to": "free.txt"}]}, "a")
        await _call(pre, "mcp__jarvis__move_paths", {"moves": [{"from": "a.txt", "to": "taken.txt"}]}, "b")
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a.txt", "b.txt"]}, "c")
        await _call(pre, "mcp__jarvis__make_folder", {"path": "d/e"}, "d")

    _run(monkeypatch, cfg, script)
    copy_args = seen[0][1]
    assert copy_args["_exists"] == {"taken.txt": True, "free.txt": False}
    assert set(copy_args["_real_paths"]) == {"a.txt", "taken.txt", "b.txt", "free.txt"}
    assert copy_args["_real_paths"]["a.txt"] == os.path.realpath(str(home / "a.txt"))
    assert seen[1][1]["_exists"] == {"taken.txt": True}
    assert set(seen[2][1]["_real_paths"]) == {"a.txt", "b.txt"}
    assert seen[3][1]["_real_path"] == os.path.realpath(str(home / "d" / "e"))


def test_model_supplied_underscore_args_are_stripped(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    broker = FakeBroker(ApprovalResult(False, "denied", "owner"))
    cfg = _cfg(tmp_path, conn, broker)
    home = tmp_path / "home"
    (home / "exists.txt").write_text("x")
    out = {}

    async def script(pre, post, options):
        out["r"] = await _call(pre, "Write", {"file_path": str(home / "exists.txt"), "content": "y",
                                              "_exists": False, "_real_path": "C:\\harmless"})

    _run(monkeypatch, cfg, script)
    args = seen[0][1]
    assert args["_exists"] is True and args["_real_path"] == os.path.realpath(str(home / "exists.txt"))
    assert len(broker.requests) == 1  # overwrite still asks
    assert "_exists" not in broker.requests[0][0].detail


# cross-thread / cross-loop approval --------------------------------------------------------

class _LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def test_approval_runs_on_the_discord_loop_from_the_worker_thread(monkeypatch, tmp_path, conn):
    discord = _LoopThread()
    try:
        broker = FakeBroker()
        progress_loops = []

        async def progress(text):
            progress_loops.append(asyncio.get_running_loop())

        cfg = _cfg(tmp_path, conn, broker, discord_loop=discord.loop, progress=progress)
        brain_loops = []
        out = {}

        async def script(pre, post, options):
            brain_loops.append(asyncio.get_running_loop())
            out["r"] = await _call(pre, "PowerShell", {"command": "git push"})

        main_thread = threading.current_thread()
        result = _run(monkeypatch, cfg, script)
        assert out["r"] == {} and result.text == "done"
        assert broker.loops == [discord.loop]                 # the card was posted/awaited on Discord's loop
        assert brain_loops[0] is not discord.loop             # while the brain ran its own loop
        assert progress_loops == [discord.loop]
        assert threading.current_thread() is main_thread
    finally:
        discord.stop()


def test_a_slow_approval_does_not_block_the_discord_loop_and_cancels_on_timeout(monkeypatch, tmp_path, conn):
    discord = _LoopThread()
    try:
        state = {"started": threading.Event(), "cancelled": threading.Event()}

        class SlowBroker:
            async def request(self, req, timeout_s=600):
                state["started"].set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    state["cancelled"].set()
                    raise

        cfg = _cfg(tmp_path, conn, SlowBroker(), discord_loop=discord.loop, timeout_s=0.5)

        async def script(pre, post, options):
            await _call(pre, "PowerShell", {"command": "git push"})

        r = _run(monkeypatch, cfg, script)
        assert state["started"].is_set()
        assert state["cancelled"].wait(5)  # the brain's time limit cancelled the pending card
        assert r.text.startswith("Stopped at the limit")
    finally:
        discord.stop()


def test_send_file_and_queue_callbacks_run_on_the_discord_loop(monkeypatch, tmp_path, conn):
    discord = _LoopThread()
    try:
        loops = {}

        async def send_file(path):
            loops["send"] = asyncio.get_running_loop()
            return f"sent {path}"

        async def queue_task(text):
            loops["queue"] = asyncio.get_running_loop()
            return "queued"

        f = tmp_path / "home" / "a.txt"
        captured = _patch_create_sdk_mcp_server(monkeypatch)
        out = {}

        async def fake_query(*, prompt, options=None, transport=None):
            by_name = {t.name: t for t in captured["tools"]}
            if "send_file" in by_name:
                out["send"] = await by_name["send_file"].handler({"path": str(f)})
            if "queue_pc_task" in by_name:
                out["queue"] = await by_name["queue_pc_task"].handler({"text": "later"})
            yield _result_message(result="ok")

        monkeypatch.setattr("brain.brains.claude.query", fake_query)
        cfg = _cfg(tmp_path, conn, discord_loop=discord.loop, send_file=send_file, queue_task=queue_task)
        f.write_text("x")
        ClaudeBrain("tok", computer=cfg).respond(_ctx())
        scfg = _cfg(tmp_path, conn, machine="server", os_name="linux", discord_loop=discord.loop,
                    queue_task=queue_task)
        ClaudeBrain("tok", computer=scfg).respond(_ctx())
        assert loops == {"send": discord.loop, "queue": discord.loop}
        assert "sent" in out["send"]["content"][0]["text"]
    finally:
        discord.stop()


def test_queue_pc_task_defaults_to_the_database(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, machine="server", os_name="linux")
    captured = _patch_create_sdk_mcp_server(monkeypatch)
    out = {}

    async def fake_query(*, prompt, options=None, transport=None):
        out["r"] = await {t.name: t for t in captured["tools"]}["queue_pc_task"].handler({"text": "tidy up"})
        yield _result_message(result="ok")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    ClaudeBrain("tok", computer=cfg).respond(_ctx())
    tasks = computer_state.list_pc_tasks(conn)
    assert [t["text"] for t in tasks] == ["tidy up"]
    assert "Queued task" in out["r"]["content"][0]["text"]


def test_failure_after_a_computer_action_keeps_side_effects_for_no_fallback(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)

    async def fake_query(*, prompt, options=None, transport=None):
        pre = options.hooks[PRE][0].hooks[0]
        await _call(pre, "mcp__jarvis__make_folder", {"path": "x"})
        raise RuntimeError("cli crashed")
        yield  # pragma: no cover

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert r.failure == FAIL_UNAVAILABLE
    assert r.side_effect_tools_ran == ["mcp__jarvis__make_folder"]


# --- fix round 1 -------------------------------------------------------------

def _hook_input(tool, tool_input, cwd=None):
    d = {"tool_name": tool, "tool_input": tool_input}
    if cwd is not None:
        d["cwd"] = cwd
    return d


def test_i1_hook_uses_the_cli_reported_cwd_for_paths_and_context(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    broker = FakeBroker(ApprovalResult(False, "denied", "owner"))
    cfg = _cfg(tmp_path, conn, broker)
    sub = tmp_path / "home" / "proj"
    sub.mkdir()
    (sub / "old.txt").write_text("x")

    async def script(pre, post, options):
        await pre(_hook_input("Write", {"file_path": "old.txt", "content": "y"}, str(sub)), "u1", None)

    _run(monkeypatch, cfg, script)
    _, args, ctx = seen[0]
    assert ctx.cwd == str(sub)
    assert args["_exists"] is True and args["_real_path"] == os.path.realpath(str(sub / "old.txt"))


def test_i1_cwd_outside_home_is_denied(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    (tmp_path / "elsewhere").mkdir()
    out = {}

    async def script(pre, post, options):
        out["r"] = await pre(_hook_input("Read", {"file_path": "a.txt"}, str(tmp_path / "elsewhere")), "u", None)
        out["ok"] = await pre(_hook_input("Read", {"file_path": "a.txt"}, str(tmp_path / "home" / "SUB")), "u2", None)
        out["home"] = await pre(_hook_input("Read", {"file_path": "a.txt"}, str(tmp_path / "home")), "u3", None)

    _run(monkeypatch, cfg, script)
    assert _decision(out["r"]) == "deny"
    assert out["r"]["hookSpecificOutput"]["permissionDecisionReason"] == "working directory changed unexpectedly"
    assert out["ok"] == {} and out["home"] == {}  # under home (case-insensitive on Windows) is fine
    assert broker.requests == []


def test_i1_env_keeps_the_shell_in_the_project_dir(monkeypatch, tmp_path, conn):
    captured = {}

    async def script(pre, post, options):
        pass

    _run(monkeypatch, _cfg(tmp_path, conn), script, captured=captured)
    assert captured["options"].env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] == "1"


def test_i2_tilde_paths_mirror_the_cli_expansion(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    broker = FakeBroker(ApprovalResult(False, "denied", "owner"))
    cfg = _cfg(tmp_path, conn, broker)
    home = tmp_path / "home"
    (home / "Documents").mkdir()
    (home / "Documents" / "existing.txt").write_text("x")

    async def script(pre, post, options):
        await _call(pre, "Write", {"file_path": "~/Documents/existing.txt", "content": "y"}, "a")
        await _call(pre, "Write", {"file_path": "~\\Documents\\existing.txt", "content": "y"}, "b")
        await _call(pre, "Read", {"file_path": "~"}, "c")

    _run(monkeypatch, cfg, script)
    target = os.path.realpath(str(home / "Documents" / "existing.txt"))
    assert seen[0][1]["_exists"] is True and seen[0][1]["_real_path"] == target
    assert seen[1][1]["_exists"] is True and seen[1][1]["_real_path"] == target
    assert seen[2][1]["_real_path"] == os.path.realpath(str(home))
    assert len(broker.requests) == 2  # overwriting an existing file asks (no bypass)
    assert seen[0][0] == "Write"


@pytest.mark.skipif(os.name != "nt", reason="Git-Bash drive paths only map on Windows")
def test_i2_git_bash_drive_paths_map_to_windows_paths(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": "/c/Users/someone/a.txt"}, "a")

    _run(monkeypatch, cfg, script)
    assert seen[0][1]["_real_path"] == os.path.realpath("C:\\Users\\someone\\a.txt")


def test_i2_relative_paths_join_the_cwd(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": "a.txt"}, "a")

    _run(monkeypatch, cfg, script)
    assert seen[0][1]["_real_path"] == os.path.realpath(os.path.join(cfg.home, "a.txt"))


@pytest.mark.parametrize("command", [
    "dir C:\\Users\\x\\Downloads", "Get-ChildItem DOWNLOADS", "dir $env:TEMP", "ls /tmp/x",
    "dir C:\\Users\\x\\AppData\\Local\\Temp", "type %temp%\\a.txt",
])
def test_i3_shell_commands_mentioning_untrusted_places_taint(monkeypatch, tmp_path, conn, command):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, FakeBroker(), state=state)

    async def script(pre, post, options):
        await _call(pre, "PowerShell", {"command": command})

    _run(monkeypatch, cfg, script)
    assert state.tainted is True


def test_i3_ordinary_shell_commands_do_not_taint(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, FakeBroker(), state=state)

    async def script(pre, post, options):
        await _call(pre, "PowerShell", {"command": "dir C:\\Users\\x\\Documents\\templates"})

    _run(monkeypatch, cfg, script)
    assert state.tainted is False


def test_i3_grep_and_glob_under_untrusted_places_taint(monkeypatch, tmp_path, conn):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Documents").mkdir()
    results = {}
    for label, tool, inp in [
        ("grep_path", "Grep", {"pattern": "x", "path": str(home / "Downloads"), "glob": "*.py"}),
        ("glob_path", "Glob", {"pattern": "*.txt", "path": str(home / "DOWNLOADS")}),
        ("glob_pattern", "Glob", {"pattern": "Downloads/**/*.txt"}),
        ("grep_docs", "Grep", {"pattern": "x", "path": str(home / "Documents"), "glob": "*.py"}),
    ]:
        state = ComputerTaskState("t")
        cfg = _cfg(tmp_path, conn, FakeBroker(ApprovalResult(True, "allowed", "o")), state=state)

        async def script(pre, post, options, tool=tool, inp=inp):
            await _call(pre, tool, inp)

        _run(monkeypatch, cfg, script)
        results[label] = state.tainted
    assert results == {"grep_path": True, "glob_path": True, "glob_pattern": True, "grep_docs": False}


def test_i3_taint_regexes_are_case_insensitive():
    for text in ("/DownLoads/", "/TEMP/", "/Tmp/"):
        assert claude_mod._UNTRUSTED_DIR.search(text)
    assert claude_mod._UNTRUSTED_TEXT.search("get-childitem DOWNLOADS")


def test_i3_taint_arms_the_a1_gate_for_jarvis_pc_action_tools(monkeypatch, tmp_path, conn):
    executor = Mock()
    executor.execute.return_value = ToolOutcome("Done.", "action")
    ctx = _ctx(executor=executor, tools=[_tool_decl("run_one_off_action")])
    cfg = _cfg(tmp_path, conn, FakeBroker(), state=ComputerTaskState("t"))
    captured = _patch_create_sdk_mcp_server(monkeypatch)
    out = {}

    async def fake_query(*, prompt, options=None, transport=None):
        by_name = {t.name: t for t in captured["tools"]}
        pre = options.hooks[PRE][0].hooks[0]
        out["before"] = await by_name["run_one_off_action"].handler({"kind": "app", "value": "notepad"})
        await _call(pre, "PowerShell", {"command": "dir C:\\Users\\x\\Downloads"})
        out["after"] = await by_name["run_one_off_action"].handler({"kind": "app", "value": "calc"})
        yield _result_message(result="ok")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    ClaudeBrain("tok", computer=cfg).respond(ctx)
    assert out["before"]["content"][0]["text"] == "Done."
    assert out["after"]["content"][0]["text"].startswith("For safety I don't open or run things")
    executor.execute.assert_called_once()


def test_i4_secret_read_output_never_reaches_output_txt(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    secret = tmp_path / "home" / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY MATERIAL " * 200)

    async def script(pre, post, options):
        await _call(pre, "Read", {"file_path": str(secret)}, "r1")
        await post({"tool_name": "Read", "tool_input": {"file_path": str(secret)},
                    "tool_response": secret.read_text()}, "r1", None)

    r = _run(monkeypatch, cfg, script)
    assert len(broker.requests) == 1  # it was a secret, so it asked
    assert [n for n, _ in r.attachments] == ["output.txt"]
    data = r.attachments[0][1].decode()
    assert "(secret file, contents omitted)" in data and "PRIVATE" not in data


def test_i4_secret_write_diff_never_reaches_changes_diff(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, FakeBroker())
    secret = tmp_path / "home" / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("OLD PRIVATE\n")

    async def script(pre, post, options):
        await _call(pre, "Edit", {"file_path": str(secret), "old_string": "OLD", "new_string": "NEW"}, "e1")
        secret.write_text("NEW PRIVATE\n")
        await post({"tool_name": "Edit", "tool_input": {"file_path": str(secret)}, "tool_response": "ok"}, "e1", None)

    r = _run(monkeypatch, cfg, script)
    data = dict(r.attachments)["changes.diff"].decode()
    assert "(secret file, contents omitted)" in data and "PRIVATE" not in data


def test_i4_any_always_ask_step_output_is_omitted(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, FakeBroker())

    async def script(pre, post, options):
        await _call(pre, "PowerShell", {"command": "git push"}, "s1")   # ALWAYS_ASK
        await post({"tool_name": "PowerShell", "tool_input": {}, "tool_response": "TOKEN=abc " * 300}, "s1", None)

    r = _run(monkeypatch, cfg, script)
    data = r.attachments[0][1].decode()
    assert "omitted" in data and "TOKEN" not in data


def test_i4_a_result_for_an_unknown_step_is_omitted_fail_closed(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        await post({"tool_name": "PowerShell", "tool_input": {}, "tool_response": "TOKEN " * 500}, "never-seen", None)

    r = _run(monkeypatch, cfg, script)
    assert "TOKEN" not in r.attachments[0][1].decode()


def test_i7_card_reason_is_the_guards_and_claudes_words_are_separate_and_capped(monkeypatch, tmp_path, conn):
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)

    async def script(pre, post, options):
        await _call(pre, "PowerShell", {"command": "git push", "description": "x" * 500}, "a")
        await _call(pre, "PowerShell", {"command": "git push"}, "b")

    _run(monkeypatch, cfg, script)
    first, second = broker.requests[0][0], broker.requests[1][0]
    guard_reason = second.reason
    assert guard_reason and "Claude says" not in guard_reason
    assert first.reason.startswith(guard_reason + "\nClaude says: ")
    assert first.reason.split("Claude says: ", 1)[1] == "x" * 200


def test_m6_stop_cancels_a_pending_approval_card(monkeypatch, tmp_path, conn):
    discord = _LoopThread()
    try:
        started, cancelled = threading.Event(), threading.Event()

        class SlowBroker:
            async def request(self, req, timeout_s=600):
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        state = ComputerTaskState("t")
        cfg = _cfg(tmp_path, conn, SlowBroker(), discord_loop=discord.loop, state=state)
        out = {}

        def stopper():
            started.wait(5)
            state.stop()  # the Stop button, from another thread

        threading.Thread(target=stopper, daemon=True).start()

        async def script(pre, post, options):
            out["r"] = await _call(pre, "PowerShell", {"command": "git push"})

        _run(monkeypatch, cfg, script)
        assert cancelled.wait(5)
        assert _decision(out["r"]) == "deny"
        assert out["r"]["hookSpecificOutput"]["permissionDecisionReason"] == "Stopped by the owner."
        assert _outcomes(conn) == [("PowerShell", "ALWAYS_ASK", "denied")]
    finally:
        discord.stop()


def test_m7_windows_env_enables_the_powershell_tool_and_linux_does_not(monkeypatch, tmp_path, conn):
    captured = {}

    async def script(pre, post, options):
        pass

    _run(monkeypatch, _cfg(tmp_path, conn), script, captured=captured)
    assert captured["options"].env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] == "1"
    assert "PowerShell" in captured["options"].tools
    captured = {}
    _run(monkeypatch, _cfg(tmp_path, conn, os_name="linux"), script, captured=captured)
    assert "CLAUDE_CODE_USE_POWERSHELL_TOOL" not in captured["options"].env


def test_a_task_that_starts_tainted_asks_for_ask_unless_bypass_steps_even_in_bypass(
    monkeypatch, tmp_path, conn
):
    broker = FakeBroker()
    state = ComputerTaskState("queued", tainted=True)
    cfg = _cfg(tmp_path, conn, broker, state=state)
    _bypass_on(conn)

    async def script(pre, post, options):
        await _call(pre, "mcp__jarvis__move_to_trash", {"paths": ["a.txt"]}, "u1")

    _run(monkeypatch, cfg, script)
    assert len(broker.requests) == 1


# --- final review fixes ------------------------------------------------------------

import sys  # noqa: E402
import time as _time  # noqa: E402

_REAL_INSTALLED = claude_mod._installed_distributions


def test_c1_hook_fills_protected_dirs_and_denies_writes_into_jarvis_runtime(monkeypatch, tmp_path, conn):
    seen = _spy_evaluate(monkeypatch)
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    target = os.path.join(sys.prefix, "Lib", "site-packages", "claude_agent_sdk", "_internal", "query.py")
    out = {}

    async def script(pre, post, options):
        out["edit"] = await _call(pre, "Edit", {"file_path": target, "old_string": "a", "new_string": "b"}, "a")
        out["read"] = await _call(pre, "Read", {"file_path": target}, "b")

    _bypass_on(conn)
    _run(monkeypatch, cfg, script)
    ctx = seen[0][2]
    assert sys.prefix in ctx.protected_dirs
    assert _decision(out["edit"]) == "deny" and out["read"] == {}
    assert broker.requests == []


def test_c1_protected_dirs_cover_python_and_the_claude_cli(tmp_path):
    root = tmp_path / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
    exe = root / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    dirs = claude_mod.load_protected_dirs(str(exe))
    norm = [os.path.normcase(d) for d in dirs]
    import site

    for expected in (sys.prefix, sys.base_prefix, site.getusersitepackages(), os.path.dirname(site.getusersitepackages()),
                     str(exe.parent), str(root)):
        assert os.path.normcase(expected) in norm, expected
    for entry in sys.path:
        if entry and os.path.isdir(entry):
            assert os.path.normcase(os.path.abspath(entry)) in norm
    assert claude_mod.load_protected_dirs(str(exe)) is dirs  # computed once


def test_c1_every_installed_distribution_is_protected(tmp_path):
    names = _REAL_INSTALLED()
    assert "claude-agent-sdk" in names and "pytest" in names
    assert all(n == n.lower() and "_" not in n for n in names)


def test_c1_installed_distributions_are_merged_into_protected_packages(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_mod, "_installed_distributions", lambda: frozenset({"aiohttp", "yarl"}))
    d = tmp_path / "j"
    d.mkdir()
    (d / "requirements.txt").write_text("requests\n")
    assert claude_mod.load_protected_packages(str(d)) == frozenset({"requests", "aiohttp", "yarl"})
    assert "aiohttp" in claude_mod.load_protected_packages(str(tmp_path / "missing"))  # fallback keeps the union


def test_c1_pip_install_always_asks_even_in_bypass_and_protected_is_refused(monkeypatch, tmp_path, conn):
    monkeypatch.setattr(claude_mod, "_installed_distributions", lambda: frozenset({"aiohttp"}))
    broker = FakeBroker()
    cfg = _cfg(tmp_path, conn, broker)
    _bypass_on(conn)
    out = {}

    async def script(pre, post, options):
        out["new"] = await _call(pre, "mcp__jarvis__install_software",
                                 {"action": "install", "manager": "pip", "package": "somenewthing"}, "a")
        out["shadow"] = await _call(pre, "mcp__jarvis__install_software",
                                    {"action": "install", "manager": "pip", "package": "aiohttp==3.8.0"}, "b")

    _run(monkeypatch, cfg, script)
    assert out["new"] == {} and len(broker.requests) == 1
    assert _decision(out["shadow"]) == "deny"


# I1: normal chat keeps A1's limits ----------------------------------------------------------

def test_i1_chat_without_computer_tools_keeps_a1_wall_clock(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, timeout_s=1800)

    async def fake_query(*, prompt, options=None, transport=None):
        await asyncio.sleep(5)
        yield _result_message(result="never")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    t = _time.monotonic()
    r = ClaudeBrain("tok", timeout=0.3, computer=cfg).respond(_ctx())
    assert _time.monotonic() - t < 3
    assert r.text.startswith("Stopped at the limit (8 steps / 1 min)")


def test_i1_first_computer_tool_extends_the_deadline(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, timeout_s=3)

    async def fake_query(*, prompt, options=None, transport=None):
        await _call(options.hooks[PRE][0].hooks[0], "Read", {"file_path": "a.txt"})   # computer tool: now 3 s
        await asyncio.sleep(0.8)                                                      # past the chat 0.3 s
        yield _result_message(result="finished")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", timeout=0.3, computer=cfg).respond(_ctx())
    assert r.text == "finished"


def test_i1_extended_deadline_still_applies(monkeypatch, tmp_path, conn):
    cfg = _cfg(tmp_path, conn, timeout_s=0.5)

    async def fake_query(*, prompt, options=None, transport=None):
        await _call(options.hooks[PRE][0].hooks[0], "Read", {"file_path": "a.txt"})
        await asyncio.sleep(5)
        yield _result_message(result="never")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", timeout=0.1, computer=cfg).respond(_ctx())
    assert r.text.startswith("Stopped at the limit (60 steps / 1 min)")


def test_i1_chat_step_budget_is_eight_until_a_computer_tool_runs(monkeypatch, tmp_path, conn):
    ctx = _ctx(tools=[_tool_decl("list_agenda")])
    cfg = _cfg(tmp_path, conn)
    out = []

    async def script(pre, post, options):
        for i in range(10):
            out.append(await _call(pre, "mcp__jarvis__list_agenda", {}, f"u{i}"))

    r = _run(monkeypatch, cfg, script, ctx=ctx)
    assert [_decision(o) for o in out] == [None] * 8 + ["deny", "deny"]
    assert r.text.startswith("Stopped at the limit (8 steps / 2 min)")


def test_i1_computer_tool_lifts_the_step_budget_to_the_computer_limit(monkeypatch, tmp_path, conn):
    ctx = _ctx(tools=[_tool_decl("list_agenda")])
    cfg = _cfg(tmp_path, conn, max_steps=12)
    out = []

    async def script(pre, post, options):
        for i in range(6):
            out.append(await _call(pre, "mcp__jarvis__list_agenda", {}, f"a{i}"))
        out.append(await _call(pre, "Read", {"file_path": "a.txt"}, "r"))      # escalates
        for i in range(8):
            out.append(await _call(pre, "mcp__jarvis__list_agenda", {}, f"b{i}"))

    _run(monkeypatch, cfg, script, ctx=ctx)
    decisions = [_decision(o) for o in out]
    assert decisions[:7] == [None] * 7 and decisions[7:12] == [None] * 5  # steps 8..12 allowed
    assert decisions[12:] == ["deny"] * 3                                 # steps 13-15 are over the limit of 12


def test_i1_max_turns_stays_at_the_computer_limit(monkeypatch, tmp_path, conn):
    captured = {}

    async def script(pre, post, options):
        pass

    _run(monkeypatch, _cfg(tmp_path, conn, max_steps=60), script, captured=captured)
    assert captured["options"].max_turns == 60


# I4: Stop ends the task -----------------------------------------------------------------------

def test_i4_stop_ends_the_query_and_returns_a_stopped_result(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, state=state)
    closed = threading.Event()

    async def fake_query(*, prompt, options=None, transport=None):
        try:
            await _call(options.hooks[PRE][0].hooks[0], "Read", {"file_path": "a.txt"})
            threading.Timer(0.3, state.stop).start()
            await asyncio.sleep(30)                     # Claude is "thinking" and would not return
            yield _result_message(result="never")
        finally:
            closed.set()

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    t = _time.monotonic()
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert _time.monotonic() - t < 5
    assert r.text.startswith("\U0001F6D1 Stopped by you after 1 steps.")
    assert r.failure is None and closed.is_set()


def test_i4_stop_breaks_out_of_a_message_stream(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, state=state)
    consumed = []

    async def fake_query(*, prompt, options=None, transport=None):
        for i in range(5):
            if i == 2:
                state.stopped = True
            consumed.append(i)
            yield AssistantMessage(content=[TextBlock(f"msg {i}")], model="m")
        yield _result_message(result="never reached")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert r.text.startswith("\U0001F6D1 Stopped by you after 0 steps.")
    assert consumed == [0, 1, 2]  # nothing after the stop was read


def test_i4_stopped_result_keeps_the_attachments_and_side_effects(monkeypatch, tmp_path, conn):
    state = ComputerTaskState("t")
    cfg = _cfg(tmp_path, conn, state=state)

    async def fake_query(*, prompt, options=None, transport=None):
        pre = options.hooks[PRE][0].hooks[0]
        post = options.hooks["PostToolUse"][0].hooks[0]
        await _call(pre, "PowerShell", {"command": "dir"}, "a")
        await post({"tool_name": "PowerShell", "tool_input": {}, "tool_response": "z" * 2000}, "a", None)
        await _call(pre, "mcp__jarvis__make_folder", {"path": "x"}, "b")
        state.stopped = True
        yield AssistantMessage(content=[TextBlock("more")], model="m")

    monkeypatch.setattr("brain.brains.claude.query", fake_query)
    r = ClaudeBrain("tok", computer=cfg).respond(_ctx())
    assert r.text.startswith("\U0001F6D1 Stopped by you after 2 steps.")
    assert [n for n, _ in r.attachments] == ["output.txt"]
    assert r.side_effect_tools_ran == ["PowerShell", "mcp__jarvis__make_folder"]


def test_pc_computer_prompt_tells_claude_the_real_folder_paths(monkeypatch, tmp_path, conn):
    from brain import computer_tools as ct

    monkeypatch.setattr(ct, "known_folders", lambda os_name: {"Desktop": r"C:\Users\alex\OneDrive\Desktop"})
    captured = {}
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    assert r"Desktop = C:\Users\alex\OneDrive\Desktop" in captured["options"].system_prompt


def test_pc_computer_prompt_says_not_to_trust_history_about_files(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    assert "never trust the chat history about what exists" in captured["options"].system_prompt.lower()


def test_pc_computer_prompt_says_claude_can_place_windows(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn)

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    prompt = captured["options"].system_prompt
    assert "open_on_screen" in prompt and "move_window" in prompt
    assert "mcp__jarvis__open_on_screen" in captured["options"].allowed_tools


def test_server_claude_prompt_has_the_queue_instructions(monkeypatch, tmp_path, conn):
    captured = {}
    cfg = _cfg(tmp_path, conn, machine="server", os_name="linux")

    async def script(pre, post, options):
        pass

    _run(monkeypatch, cfg, script, captured=captured)
    prompt = captured["options"].system_prompt
    assert "queue_pc_task" in prompt
    assert "Your PC is off — I'll do this when it's back on." in prompt
    assert "reminders" in prompt.lower()  # says reminders etc. never need the PC
