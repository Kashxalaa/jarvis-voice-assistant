from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from brain.gemini_client import GeminiConfigError, GeminiUnavailableError, ToolReply
from brain.memory import (
    init_db,
    count_user_messages,
    get_recent_messages,
)
from brain.profile import get_profile
from brain.conversation import (
    run_turn,
    UNAVAILABLE_MESSAGE,
    MEMORY_UNAVAILABLE_MESSAGE,
    _is_explicit_remember_request,
)


def _plain_reply(text):
    return ToolReply(text=text, function_name=None, function_args=None)


def test_run_turn_saves_messages_and_returns_reply(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Hi! How can I help?")

    reply = run_turn(conn, client, "hello")

    assert reply == "Hi! How can I help?"
    assert count_user_messages(conn) == 1
    client.review_memory.assert_not_called()


def test_run_turn_triggers_the_profile_review_every_fifth_turn(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")
    client.review_memory.return_value = {"profile": [{"section": "Setup", "action": "add", "new": "Editor: VS Code"}], "lessons": []}
    for _ in range(5):
        run_turn(conn, client, "I use VS Code")

    client.review_memory.assert_called_once()
    assert "- Editor: VS Code" in get_profile(conn)
    client.extract_facts.assert_not_called()


def test_run_turn_returns_friendly_message_and_keeps_user_message_when_gemini_unavailable(conn):
    client = Mock()
    client.generate_with_tools.side_effect = GeminiUnavailableError("down")

    reply = run_turn(conn, client, "hello")

    assert reply == UNAVAILABLE_MESSAGE
    assert count_user_messages(conn) == 1
    messages = get_recent_messages(conn)
    assert [m["role"] for m in messages] == ["user"]


def test_run_turn_logs_gemini_unavailable_error_for_diagnosability(conn, caplog):
    client = Mock()
    client.generate_with_tools.side_effect = GeminiUnavailableError(
        "all models exhausted: model-a, model-b"
    )

    with caplog.at_level("WARNING"):
        run_turn(conn, client, "hello")

    assert "all models exhausted" in caplog.text


def test_run_turn_ignores_fact_extraction_failure(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")
    client.review_memory.side_effect = GeminiUnavailableError("down")

    reply = None
    for _ in range(5):
        reply = run_turn(conn, client, "hi")

    assert reply == "ok"
    assert get_profile(conn) == ""


def test_run_turn_returns_config_message_and_keeps_user_message_when_gemini_misconfigured(conn):
    client = Mock()
    client.generate_with_tools.side_effect = GeminiConfigError("404 model not found")

    reply = run_turn(conn, client, "hello")

    assert "check your GEMINI_API_KEY" in reply.lower() or "gemini_api_key" in reply.lower()
    assert "404 model not found" in reply
    assert count_user_messages(conn) == 1
    messages = get_recent_messages(conn)
    assert [m["role"] for m in messages] == ["user"]


def test_run_turn_surfaces_config_message_from_a_real_gemini_client_on_bad_api_key(conn):
    from google.genai import errors as genai_errors

    from brain.gemini_client import GeminiClient

    fake_transport = Mock()
    fake_transport.models.generate_content.side_effect = genai_errors.ClientError(
        401, {"error": {"message": "invalid API key", "status": "UNAUTHENTICATED"}}
    )
    real_client = GeminiClient(
        api_key="unused", models=["model-a", "model-b"], client=fake_transport
    )

    reply = run_turn(conn, real_client, "hello")

    assert "gemini_api_key" in reply.lower()
    assert "invalid API key" in reply
    assert fake_transport.models.generate_content.call_count == 1


def test_run_turn_ignores_fact_extraction_config_error(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")
    client.review_memory.side_effect = GeminiConfigError("404 model not found")

    reply = None
    for _ in range(5):
        reply = run_turn(conn, client, "hi")

    assert reply == "ok"
    assert get_profile(conn) == ""


def test_run_turn_returns_friendly_message_when_memory_read_fails(conn):
    conn.close()
    client = Mock()

    reply = run_turn(conn, client, "hello")

    assert reply == MEMORY_UNAVAILABLE_MESSAGE
    client.generate_with_tools.assert_not_called()


class _FlakyConn:
    """Wraps a real psycopg.Connection, failing its Nth execute() call."""

    def __init__(self, real_conn, fail_on_call):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._count = 0

    def execute(self, *args, **kwargs):
        self._count += 1
        if self._count == self._fail_on_call:
            import psycopg

            raise psycopg.OperationalError("simulated connection failure")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_run_turn_returns_reply_even_when_memory_write_fails_after_generation(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("here you go")

    # 1=get_recent_messages, 2=add_message(user), 3=get_profile,
    # 4=get_pending_proposal, 5=list_actions (both checked in run_turn
    # before generating); fail on the 6th execute, which is
    # add_message(assistant) after the reply has already been generated.
    flaky = _FlakyConn(conn, fail_on_call=6)

    reply = run_turn(flaky, client, "hello")

    assert reply == "here you go"


def test_is_explicit_remember_request_true_for_remember_that():
    assert _is_explicit_remember_request("remember that I use VS Code") is True


def test_is_explicit_remember_request_true_for_dont_forget():
    assert _is_explicit_remember_request("Don't forget my birthday is in May") is True


def test_is_explicit_remember_request_true_for_keep_in_mind():
    assert _is_explicit_remember_request("Keep in mind I'm vegetarian") is True


def test_is_explicit_remember_request_false_for_unrelated_use_of_remember():
    assert _is_explicit_remember_request("I don't remember where I put my keys") is False


def test_is_explicit_remember_request_false_for_unrelated_use_of_note_that():
    assert _is_explicit_remember_request("note that the API returns a 404 here") is False


def test_is_explicit_remember_request_false_for_regular_message():
    assert _is_explicit_remember_request("What's the weather like today?") is False


def test_run_turn_reviews_the_profile_immediately_on_explicit_remember_request(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Got it.")
    client.review_memory.return_value = {"profile": [{"section": "Preferences", "action": "add", "new": "Favorite color: blue"}], "lessons": []}
    reply = run_turn(conn, client, "remember that my favorite color is blue")

    assert reply == "Got it.\n📝 Profile: + Favorite color: blue"
    client.review_memory.assert_called_once()
    assert "- Favorite color: blue" in get_profile(conn)
    assert count_user_messages(conn) == 1


def test_run_turn_does_not_double_extract_when_explicit_request_lands_on_fifth_turn(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")
    client.review_memory.return_value = {"profile": [], "lessons": []}
    for i in range(4):
        run_turn(conn, client, f"message {i}")
    run_turn(conn, client, "remember that I use VS Code")

    client.review_memory.assert_called_once()


def test_run_turn_skips_fact_extraction_when_create_note_handles_a_remember_phrased_request(
    conn, monkeypatch
):
    import brain.tools as tools

    monkeypatch.setattr(
        tools, "create_note", lambda conn, client, title, content, tags, importance, source="explicit": 1
    )
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="create_note",
        function_args={"title": "T", "content": "C"},
    )

    run_turn(conn, client, "remember this as a note: we configured the journal")

    client.review_memory.assert_not_called()


def test_run_turn_passes_image_through_to_generate_with_tools(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("I see a cat.")

    reply = run_turn(
        conn,
        client,
        "what is this?",
        image_bytes=b"fake-bytes",
        image_mime_type="image/png",
    )

    assert reply == "I see a cat."
    call_kwargs = client.generate_with_tools.call_args.kwargs
    assert call_kwargs["image_bytes"] == b"fake-bytes"
    assert call_kwargs["image_mime_type"] == "image/png"


def test_run_turn_stores_placeholder_text_when_message_is_blank_but_image_given(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Nice photo.")

    run_turn(conn, client, "", image_bytes=b"fake-bytes", image_mime_type="image/png")

    messages = get_recent_messages(conn)
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["content"].strip() != ""


def test_run_turn_sends_placeholder_text_to_gemini_when_message_is_blank_but_image_given(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Nice photo.")

    run_turn(conn, client, "", image_bytes=b"fake-bytes", image_mime_type="image/png")

    call_args = client.generate_with_tools.call_args
    # Positional arg 3 (0-indexed) is user_message: system_prompt, facts, history, user_message, tools
    sent_user_message = call_args.args[3]
    assert sent_user_message.strip() != ""


def test_run_turn_includes_replied_to_context_in_what_gemini_sees(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Got it.")

    run_turn(
        conn,
        client,
        "do that one",
        replied_to="open my trading journal",
    )

    call_args = client.generate_with_tools.call_args
    sent_user_message = call_args.args[3]
    assert "open my trading journal" in sent_user_message
    assert "do that one" in sent_user_message


def test_run_turn_stores_replied_to_context_in_history(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Got it.")

    run_turn(conn, client, "do that one", replied_to="open my trading journal")

    messages = get_recent_messages(conn)
    user_messages = [m for m in messages if m["role"] == "user"]
    assert len(user_messages) == 1
    assert "open my trading journal" in user_messages[0]["content"]
    assert "do that one" in user_messages[0]["content"]


def test_run_turn_works_normally_when_not_a_reply(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    reply = run_turn(conn, client, "hello")

    assert reply == "hi"
    call_args = client.generate_with_tools.call_args
    assert call_args.args[3] == "hello"


def test_run_turn_executes_a_known_action_and_returns_synthesized_reply(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import upsert_action

    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={"name": "vscode"}
    )

    reply = run_turn(conn, client, "open vscode")

    assert opened == [("app", "C:/apps/code.exe")]
    assert "vscode" in reply.lower()


def test_run_turn_reports_when_known_action_name_does_not_exist(conn):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={"name": "ghost"}
    )

    reply = run_turn(conn, client, "open ghost")

    assert "ghost" in reply
    assert "set up" in reply.lower() or "don't have" in reply.lower()


def test_run_turn_reports_friendly_message_when_execution_fails(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import upsert_action

    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")

    def failing_open(kind, value):
        raise OSError("file not found")

    monkeypatch.setattr(automation, "_open", failing_open)

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={"name": "vscode"}
    )

    reply = run_turn(conn, client, "open vscode")

    assert "problem" in reply.lower() or "couldn't" in reply.lower()


def test_run_turn_executes_a_full_routine(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import upsert_action
    import json

    upsert_action(conn, "journal_folder", "folder", "E:/journal")
    upsert_action(conn, "broker_site", "url", "https://broker.example.com")
    upsert_action(
        conn, "trading_work", "routine",
        json.dumps([{"name": "journal_folder"}, {"name": "broker_site"}]),
    )
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={"name": "trading_work"}
    )

    reply = run_turn(conn, client, "let's work on trading")

    assert opened == [
        ("folder", "E:/journal"),
        ("url", "https://broker.example.com"),
    ]
    assert "trading_work" in reply.lower() or "journal_folder" in reply.lower()


def test_run_turn_proposes_a_new_action_without_executing_it(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import get_pending_proposal

    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="propose_new_action",
        function_args={
            "kind": "url",
            "suggested_name": "quickbrew_site",
            "suggested_value": "https://quickbrew.com",
            "reasoning": "sounds like a website",
        },
    )

    reply = run_turn(conn, client, "open quickbrew.com")

    assert opened == []
    assert "quickbrew" in reply.lower()
    proposal = get_pending_proposal(conn)
    assert proposal["suggested_name"] == "quickbrew_site"


def test_run_turn_reports_friendly_message_when_proposal_missing_required_fields(conn):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="propose_new_action",
        function_args={"kind": "url"},  # missing suggested_name/suggested_value
    )

    reply = run_turn(conn, client, "open something")

    assert "sure" in reply.lower() or "again" in reply.lower()
    from brain.memory import get_pending_proposal
    assert get_pending_proposal(conn) is None


def test_run_turn_executes_one_off_action_immediately_without_saving_or_confirming(
    conn, monkeypatch
):
    import brain.automation as automation
    from brain.memory import get_pending_proposal, list_actions

    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="run_one_off_action",
        function_args={
            "kind": "url",
            "value": "https://search.example.com/?q=zed+build",
        },
    )

    reply = run_turn(conn, client, "google zed build for league of legends")

    assert opened == [("url", "https://search.example.com/?q=zed+build")]
    assert get_pending_proposal(conn) is None
    assert list_actions(conn) == []
    assert reply


def test_run_turn_reports_friendly_message_when_one_off_action_fails(conn, monkeypatch):
    import brain.automation as automation

    def failing_open(kind, value):
        raise OSError("boom")

    monkeypatch.setattr(automation, "_open", failing_open)

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="run_one_off_action",
        function_args={"kind": "url", "value": "https://search.example.com"},
    )

    reply = run_turn(conn, client, "google something")

    assert "problem" in reply.lower()


def test_run_turn_reports_friendly_message_when_one_off_action_missing_fields(conn):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_one_off_action", function_args={"kind": "url"}
    )

    reply = run_turn(conn, client, "google something")

    assert "sure" in reply.lower() or "specific" in reply.lower()


def test_run_turn_reports_friendly_message_when_known_action_call_missing_name(conn):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_known_action", function_args={}  # missing "name"
    )

    reply = run_turn(conn, client, "open something")

    assert "specific" in reply.lower() or "sure" in reply.lower()


def test_run_turn_offers_confirm_tool_when_a_proposal_is_pending(conn):
    from brain.automation import propose_action

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("noted")

    run_turn(conn, client, "yes that's right")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "confirm_pending_action" in tool_names


def test_run_turn_does_not_offer_confirm_tool_when_nothing_pending(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "confirm_pending_action" not in tool_names


def test_run_turn_confirms_pending_action_executes_and_saves(conn, monkeypatch):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_action, get_pending_proposal

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="confirm_pending_action", function_args={"confirmed": True}
    )

    reply = run_turn(conn, client, "yes that's right")

    assert opened == [("url", "https://quickbrew.com")]
    assert get_action(conn, "quickbrew_site") is not None
    assert get_pending_proposal(conn) is None
    assert "quickbrew_site" in reply


def test_run_turn_clears_pending_action_without_executing_when_denied(conn, monkeypatch):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_action, get_pending_proposal

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="confirm_pending_action", function_args={"confirmed": False}
    )

    run_turn(conn, client, "no that's not right")

    assert opened == []
    assert get_action(conn, "quickbrew_site") is None
    assert get_pending_proposal(conn) is None


def test_run_turn_decrements_pending_proposal_when_ignored(conn):
    from brain.automation import propose_action
    from brain.memory import get_pending_proposal

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    before = get_pending_proposal(conn)["turns_remaining"]

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("sure, here's the weather...")

    run_turn(conn, client, "what's the weather like")

    after = get_pending_proposal(conn)["turns_remaining"]
    assert after == before - 1


def test_run_turn_reports_friendly_message_when_confirm_execution_fails(conn, monkeypatch):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_action, get_pending_proposal

    propose_action(conn, "folder", "ghost_folder", "E:/does/not/exist", "reason")

    def failing_open(kind, value):
        raise OSError("path not found")

    monkeypatch.setattr(automation, "_open", failing_open)

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="confirm_pending_action", function_args={"confirmed": True}
    )

    reply = run_turn(conn, client, "yes")

    assert "problem" in reply.lower() or "couldn't" in reply.lower()
    assert get_action(conn, "ghost_folder") is None
    assert get_pending_proposal(conn) is None


def test_run_turn_reproposes_with_corrected_value_when_denied_with_a_correction(conn):
    from brain.automation import propose_action
    from brain.memory import get_pending_proposal

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="confirm_pending_action",
        function_args={"confirmed": False, "corrected_value": "https://quickbrew.net"},
    )

    reply = run_turn(conn, client, "no, it's quickbrew.net")

    assert "quickbrew.net" in reply
    proposal = get_pending_proposal(conn)
    assert proposal is not None
    assert proposal["suggested_value"] == "https://quickbrew.net"
    assert proposal["suggested_name"] == "quickbrew_site"


def test_run_turn_includes_known_action_names_in_the_system_prompt(conn):
    from brain.memory import upsert_action

    upsert_action(conn, "journal_folder", "folder", "E:/journal")

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")

    run_turn(conn, client, "hello")

    system_prompt_sent = client.generate_with_tools.call_args.args[0]
    assert "journal_folder" in system_prompt_sent


def test_run_turn_clears_expired_pending_proposal_instead_of_offering_confirm_tool(conn):
    from datetime import datetime, timezone

    from brain.memory import get_pending_proposal, set_pending_proposal

    set_pending_proposal(
        conn, "url", "quickbrew_site", "https://quickbrew.com", "reason",
        datetime.now(timezone.utc).isoformat(), turns_remaining=0,
    )

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "confirm_pending_action" not in tool_names
    assert get_pending_proposal(conn) is None


def test_run_turn_executes_ad_hoc_placement_immediately_without_confirming(conn, monkeypatch):
    import json

    import brain.automation as automation
    from brain.memory import upsert_action

    upsert_action(conn, "tradingview", "url", "https://tradingview.com")
    placed_calls = []
    monkeypatch.setattr(
        automation.window_manager,
        "open_placed",
        lambda kind, value, new_window, monitor, **kwargs: placed_calls.append(
            (kind, value, new_window, monitor, kwargs)
        ),
    )

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="run_ad_hoc_placement",
        function_args={
            "steps": json.dumps([{"name": "tradingview", "monitor": 1, "size": [0.5, 0.5]}])
        },
    )

    reply = run_turn(conn, client, "open tradingview on my main monitor at 1/4 size")

    assert placed_calls == [
        ("url", "https://tradingview.com", False, 1, {"maximized": False, "size": [0.5, 0.5], "position": None})
    ]
    assert "tradingview" in reply.lower()
    from brain.memory import get_pending_proposal
    assert get_pending_proposal(conn) is None


def test_run_turn_reports_when_ad_hoc_placement_references_unknown_action(conn):
    import json

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="run_ad_hoc_placement",
        function_args={"steps": json.dumps([{"name": "ghost_action"}])},
    )

    reply = run_turn(conn, client, "open ghost_action on my second monitor")

    assert "ghost_action" in reply
    assert "set up" in reply.lower() or "don't have" in reply.lower()


def test_run_turn_reports_friendly_message_when_ad_hoc_placement_steps_missing(conn):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="run_ad_hoc_placement", function_args={}
    )

    reply = run_turn(conn, client, "open something differently")

    assert "specific" in reply.lower() or "sure" in reply.lower()


def test_run_turn_omits_automation_tools_when_platform_does_not_support_them(conn, monkeypatch):
    import brain.automation as automation

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "run_known_action" not in tool_names
    assert "propose_new_action" not in tool_names
    assert "run_ad_hoc_placement" not in tool_names
    assert "run_one_off_action" not in tool_names


def test_run_turn_still_offers_confirm_tool_when_pending_even_without_automation_support(
    conn, monkeypatch
):
    import brain.automation as automation
    from brain.automation import propose_action

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("noted")

    run_turn(conn, client, "yes that's right")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "confirm_pending_action" in tool_names


def test_run_turn_omits_known_actions_from_prompt_when_platform_does_not_support_them(
    conn, monkeypatch
):
    import brain.automation as automation
    from brain.memory import upsert_action

    upsert_action(conn, "journal_folder", "folder", "E:/journal")
    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)

    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    system_prompt_sent = client.generate_with_tools.call_args.args[0]
    assert "journal_folder" not in system_prompt_sent
    assert "cannot control" in system_prompt_sent.lower()


def test_run_turn_confirms_and_saves_without_opening_when_automation_not_supported(
    conn, monkeypatch
):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_action, get_pending_proposal

    propose_action(conn, "folder", "journal_folder", "E:/journal", "reason")
    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)

    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="confirm_pending_action", function_args={"confirmed": True}
    )

    reply = run_turn(conn, client, "yes that's right")

    assert get_action(conn, "journal_folder") is not None
    assert get_pending_proposal(conn) is None
    assert "journal_folder" in reply
    assert "back on" in reply.lower()


def test_run_turn_always_offers_memory_tools_even_without_automation_support(conn, monkeypatch):
    import brain.automation as automation

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "search_memory" in tool_names
    assert "create_note" in tool_names


def test_run_turn_handles_search_memory_call_and_reports_results(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(
        tools,
        "search_notes",
        lambda conn, client, query, limit=3: [
            {"id": 1, "title": "Trading setup", "content": "Journal automation.",
             "tags": "trading", "importance": 7, "use_count": 1, "similarity": 0.9}
        ],
    )
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="search_memory", function_args={"query": "trading"}
    )

    reply = run_turn(conn, client, "what did we set up for trading?")

    assert "Trading setup" in reply


def test_run_turn_includes_search_memory_results_even_when_model_emits_preamble_text(
    conn, monkeypatch
):
    import brain.tools as tools

    monkeypatch.setattr(
        tools,
        "search_notes",
        lambda conn, client, query, limit=3: [
            {"id": 1, "title": "Trading setup", "content": "Journal automation.",
             "tags": "trading", "importance": 7, "use_count": 1, "similarity": 0.9}
        ],
    )
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Let me check my notes on that.",
        function_name="search_memory",
        function_args={"query": "trading"},
    )

    reply = run_turn(conn, client, "what did we set up for trading?")

    assert "Trading setup" in reply


def test_run_turn_handles_search_memory_call_with_no_results(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(
        tools, "search_notes", lambda conn, client, query, limit=3: []
    )
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="search_memory", function_args={"query": "anything"}
    )

    reply = run_turn(conn, client, "do you remember anything about that?")

    assert "don't have anything" in reply.lower() or "nothing" in reply.lower()


def test_run_turn_handles_create_note_call_and_saves_it(conn, monkeypatch):
    import brain.tools as tools

    created = []
    monkeypatch.setattr(
        tools,
        "create_note",
        lambda conn, client, title, content, tags, importance, source="explicit": (
            created.append((title, content, tags, importance)) or 1
        ),
    )
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="create_note",
        function_args={
            "title": "Trading setup",
            "content": "Configured the journal automation.",
            "tags": "trading",
            "importance": 7,
        },
    )

    reply = run_turn(conn, client, "remember this as a note: we configured the journal")

    assert created == [("Trading setup", "Configured the journal automation.", "trading", 7)]
    assert "Trading setup" in reply


def test_run_turn_reports_friendly_message_when_create_note_missing_fields(conn):
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_note", function_args={"title": "Only a title"}
    )

    reply = run_turn(conn, client, "remember this")

    assert "sure" in reply.lower() or "again" in reply.lower()


def test_run_turn_reports_friendly_message_when_create_note_fails(conn, monkeypatch):
    import brain.tools as tools
    from brain.notes import NoteCreationError

    def _raise(*args, **kwargs):
        raise NoteCreationError("embedding failed")

    monkeypatch.setattr(tools, "create_note", _raise)
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="create_note",
        function_args={"title": "T", "content": "C"},
    )

    reply = run_turn(conn, client, "remember this")

    assert "couldn't save" in reply.lower() or "try again" in reply.lower()


def test_run_turn_defaults_create_note_importance_when_out_of_range(conn, monkeypatch):
    import brain.tools as tools

    captured = {}
    monkeypatch.setattr(
        tools,
        "create_note",
        lambda conn, client, title, content, tags, importance, source="explicit": (
            captured.update(importance=importance) or 1
        ),
    )
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="create_note",
        function_args={"title": "T", "content": "C", "importance": 999},
    )

    run_turn(conn, client, "remember this")

    assert captured["importance"] == 5


def test_run_turn_routes_read_webpage_tool_call(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_webpage", lambda url: f"content of {url}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="read_webpage", function_args={"url": "https://example.com"}
    )

    reply = run_turn(conn, client, "what's on https://example.com")

    assert reply == "content of https://example.com"


def test_run_turn_routes_read_webpage_tool_call_with_missing_url(conn):
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="read_webpage", function_args={}
    )

    reply = run_turn(conn, client, "read that page")

    assert "share the link" in reply.lower() or "which page" in reply.lower()


def test_run_turn_routes_get_youtube_info_tool_call(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_youtube_info", lambda url: f"info for {url}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None,
        function_name="get_youtube_info",
        function_args={"url": "https://youtube.com/watch?v=abc"},
    )

    reply = run_turn(conn, client, "what's this video about?")

    assert reply == "info for https://youtube.com/watch?v=abc"


def test_run_turn_routes_get_github_repo_tool_call(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_github_repo", lambda owner_repo: f"repo {owner_repo}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="get_github_repo", function_args={"owner_repo": "torvalds/linux"}
    )

    reply = run_turn(conn, client, "tell me about torvalds/linux")

    assert reply == "repo torvalds/linux"


def test_run_turn_routes_read_rss_feed_tool_call(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_rss_feed", lambda url: f"feed {url}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="read_rss_feed", function_args={"url": "https://example.com/feed.xml"}
    )

    reply = run_turn(conn, client, "what's new on that feed?")

    assert reply == "feed https://example.com/feed.xml"


def test_run_turn_routes_get_weather_tool_call_passing_conn(conn, monkeypatch):
    import brain.tools as tools

    captured = {}

    def fake_get_weather(conn_arg, location):
        captured["conn"] = conn_arg
        captured["location"] = location
        return "sunny"

    monkeypatch.setattr(tools, "get_weather", fake_get_weather)
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="get_weather", function_args={"location": "Tokyo"}
    )

    reply = run_turn(conn, client, "weather in Tokyo?")

    assert reply == "sunny"
    assert captured["conn"] is conn
    assert captured["location"] == "Tokyo"


def test_run_turn_routes_get_weather_tool_call_with_no_location_given(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_weather", lambda conn_arg, location: f"weather for {location}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="get_weather", function_args={}
    )

    reply = run_turn(conn, client, "what's the weather like?")

    assert reply == "weather for None"


def test_run_turn_routes_web_search_tool_call(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "web_search", lambda query: f"results for {query}")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="web_search", function_args={"query": "best pizza"}
    )

    reply = run_turn(conn, client, "search for best pizza")

    assert reply == "results for best pizza"


def test_run_turn_routes_web_search_tool_call_with_missing_query(conn):
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="web_search", function_args={}
    )

    reply = run_turn(conn, client, "search for something")

    assert "rephrase" in reply.lower() or "sure" in reply.lower()


def test_run_turn_offers_web_search_tool_when_exa_api_key_configured(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "resolve_exa_api_key", lambda: "exa-key")
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "web_search" in tool_names


def test_run_turn_omits_web_search_tool_when_exa_api_key_not_configured(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "resolve_exa_api_key", lambda: None)
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = [t["name"] for t in tools_passed]
    assert "web_search" not in tool_names


def test_run_turn_always_offers_the_five_unconditional_web_tools(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "resolve_exa_api_key", lambda: None)
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hello")

    tools_passed = client.generate_with_tools.call_args.args[4]
    tool_names = {t["name"] for t in tools_passed}
    assert tool_names >= {
        "read_webpage", "get_youtube_info", "get_github_repo",
        "read_rss_feed", "get_weather",
    }


def test_run_turn_folds_gemini_text_before_read_webpage_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_webpage", lambda url: "PAGE CONTENT")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Here's that page:", function_name="read_webpage", function_args={"url": "https://example.com"}
    )

    reply = run_turn(conn, client, "what's on https://example.com")

    assert reply == "Here's that page:\n\nPAGE CONTENT"


def test_run_turn_folds_gemini_text_before_get_youtube_info_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_youtube_info", lambda url: "VIDEO INFO")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Here's what I found:",
        function_name="get_youtube_info",
        function_args={"url": "https://youtube.com/watch?v=abc"},
    )

    reply = run_turn(conn, client, "what's this video about?")

    assert reply == "Here's what I found:\n\nVIDEO INFO"


def test_run_turn_folds_gemini_text_before_get_github_repo_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_github_repo", lambda owner_repo: "REPO INFO")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Sure, here's that repo:",
        function_name="get_github_repo",
        function_args={"owner_repo": "torvalds/linux"},
    )

    reply = run_turn(conn, client, "tell me about torvalds/linux")

    assert reply == "Sure, here's that repo:\n\nREPO INFO"


def test_run_turn_folds_gemini_text_before_read_rss_feed_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_rss_feed", lambda url: "FEED ENTRIES")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Here's what's new:",
        function_name="read_rss_feed",
        function_args={"url": "https://example.com/feed.xml"},
    )

    reply = run_turn(conn, client, "what's new on that feed?")

    assert reply == "Here's what's new:\n\nFEED ENTRIES"


def test_run_turn_folds_gemini_text_before_get_weather_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "get_weather", lambda conn_arg, location: "sunny")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Let me check:", function_name="get_weather", function_args={"location": "Tokyo"}
    )

    reply = run_turn(conn, client, "weather in Tokyo?")

    assert reply == "Let me check:\n\nsunny"


def test_run_turn_folds_gemini_text_before_web_search_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "web_search", lambda query: "SEARCH RESULTS")
    client = Mock()
    client.answer_with_tool_result.side_effect = GeminiUnavailableError("down")
    client.generate_with_tools.return_value = ToolReply(
        text="Searching now:", function_name="web_search", function_args={"query": "best pizza"}
    )

    reply = run_turn(conn, client, "search for best pizza")

    assert reply == "Searching now:\n\nSEARCH RESULTS"


def test_run_turn_tells_gemini_the_current_local_date_and_time(conn, monkeypatch):
    from datetime import datetime, timezone
    import brain.conversation as conversation

    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Tbilisi")
    monkeypatch.setattr(
        conversation, "_utc_now", lambda: datetime(2026, 9, 23, 18, 52, tzinfo=timezone.utc)
    )
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")

    run_turn(conn, client, "how many days since May 1 2026?")

    system_prompt_sent = client.generate_with_tools.call_args.args[0]
    # 18:52 UTC is 22:52 in Tbilisi (UTC+4).
    assert "Wednesday, 23 September 2026" in system_prompt_sent
    assert "22:52" in system_prompt_sent
    assert "Asia/Tbilisi" in system_prompt_sent


def test_run_turn_uses_tbilisi_time_when_no_timezone_configured(conn, monkeypatch):
    from datetime import datetime, timezone
    import brain.conversation as conversation

    monkeypatch.delenv("JARVIS_TIMEZONE", raising=False)
    monkeypatch.setattr(
        conversation, "_utc_now", lambda: datetime(2026, 9, 23, 22, 30, tzinfo=timezone.utc)
    )
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")

    run_turn(conn, client, "what day is it?")

    system_prompt_sent = client.generate_with_tools.call_args.args[0]
    # 22:30 UTC on the 23rd is already 02:30 on the 24th in Tbilisi.
    assert "Thursday, 24 September 2026" in system_prompt_sent


def test_run_turn_tells_gemini_to_answer_directly_instead_of_searching_memory(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")

    run_turn(conn, client, "hi")

    system_prompt_sent = client.generate_with_tools.call_args.args[0].lower()
    assert "own knowledge" in system_prompt_sent
    assert "search_memory" in system_prompt_sent


@pytest.mark.parametrize(
    "function_name, function_args, patch_name, fake_tool, expected_result",
    [
        ("read_webpage", {"url": "https://example.com"}, "read_webpage",
         lambda url: "PAGE CONTENT", "PAGE CONTENT"),
        ("get_youtube_info", {"url": "https://youtu.be/abc"}, "get_youtube_info",
         lambda url: "VIDEO INFO", "VIDEO INFO"),
        ("get_github_repo", {"owner_repo": "a/b"}, "get_github_repo",
         lambda owner_repo: "REPO INFO", "REPO INFO"),
        ("read_rss_feed", {"url": "https://example.com/feed"}, "read_rss_feed",
         lambda url: "FEED ENTRIES", "FEED ENTRIES"),
        ("get_weather", {"location": "Tokyo"}, "get_weather",
         lambda conn_arg, location: "Tokyo: 24C", "Tokyo: 24C"),
        ("web_search", {"query": "pizza"}, "web_search",
         lambda query: "SEARCH RESULTS", "SEARCH RESULTS"),
        ("search_memory", {"query": "trading"}, "search_notes",
         lambda conn_arg, client_arg, query, limit=3: [
             {"id": 1, "title": "Trading setup", "content": "Journal automation.",
              "tags": "", "importance": 7, "use_count": 1, "similarity": 0.9}
         ], "Trading setup"),
    ],
)
def test_run_turn_lets_gemini_answer_from_information_tool_results(
    conn, monkeypatch, function_name, function_args, patch_name, fake_tool, expected_result
):
    import brain.tools as tools

    monkeypatch.setattr(tools, patch_name, fake_tool)
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name=function_name, function_args=function_args
    )
    client.answer_with_tool_result.return_value = "Here's the gist, sir."

    reply = run_turn(conn, client, "tell me")

    assert reply == "Here's the gist, sir."
    kwargs = client.answer_with_tool_result.call_args.kwargs
    assert kwargs["tool_name"] == function_name
    assert expected_result in kwargs["tool_result"]
    assert kwargs["user_message"] == "tell me"


def test_run_turn_lets_gemini_answer_itself_when_memory_search_finds_nothing(
    conn, monkeypatch
):
    import brain.tools as tools

    monkeypatch.setattr(
        tools, "search_notes", lambda conn, client, query, limit=3: []
    )
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="search_memory", function_args={"query": "days since may 1"}
    )
    client.answer_with_tool_result.return_value = "It's been 145 days, sir."

    reply = run_turn(conn, client, "How many days since May 1 2026 has passed")

    assert reply == "It's been 145 days, sir."
    assert "no" in client.answer_with_tool_result.call_args.kwargs["tool_result"].lower()


def _at_tbilisi(day, hour, minute=0):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo("Asia/Tbilisi"))


@pytest.fixture
def saturday_morning(monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Tbilisi")
    monkeypatch.setattr(conversation, "_utc_now", lambda: _at_tbilisi(26, 10))


def test_run_turn_offers_the_reminder_tools(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hi")

    tool_names = [t["name"] for t in client.generate_with_tools.call_args.args[4]]
    assert {"create_reminder", "add_todo", "list_agenda", "finish_item"} <= set(tool_names)


def test_run_turn_creates_a_reminder_and_states_the_exact_time(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={"text": "Close trades", "when": "2026-09-26T21:00"},
    )

    reply = run_turn(conn, client, "remind me at 9pm to close my trades")

    assert reply == "⏰ Reminder set — Sat 26 Sep, 21:00: Close trades"
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
    client.answer_with_tool_result.assert_not_called()


def test_run_turn_creates_a_reminder_with_category_and_early_reminder(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Giorgi's birthday", "when": "2027-04-10T10:00",
            "repeat": "FREQ=YEARLY", "category": "birthday", "remind_days_before": 1,
        },
    )

    reply = run_turn(conn, client, "remind me every year for Giorgi's birthday")

    assert reply == (
        "⏰ Reminder set — Sat 10 Apr, 10:00: Giorgi's birthday (repeats) "
        "(+ early reminder Fri 9 Apr, 10:00)"
    )
    category = conn.execute(
        "SELECT category FROM reminders WHERE parent_id IS NULL"
    ).fetchone()[0]
    assert category == "birthday"
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 2


def test_run_turn_notes_when_the_early_reminder_time_has_already_passed(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Close trades", "when": "2026-09-26T21:00", "remind_days_before": 1,
        },
    )

    reply = run_turn(conn, client, "remind me at 9pm and the day before too")

    assert reply == (
        "⏰ Reminder set — Sat 26 Sep, 21:00: Close trades "
        "(too late for an early reminder)"
    )
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_run_turn_rejects_an_out_of_range_remind_days_before(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Close trades", "when": "2026-09-26T21:00", "remind_days_before": 31,
        },
    )

    reply = run_turn(conn, client, "remind me at 9pm, and 31 days before too")

    assert "between 1 and 30 days early" in reply
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_run_turn_marks_recurring_reminders_in_the_reply(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={"text": "Check market", "when": "2026-09-26T09:00",
                       "repeat": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"},
    )

    reply = run_turn(conn, client, "every weekday at 9 remind me to check the market")

    assert reply == "⏰ Reminder set — Mon 28 Sep, 09:00: Check market (repeats)"


def test_run_turn_explains_a_bad_reminder_time(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={"text": "Too late", "when": "2026-09-26T08:00"},
    )

    reply = run_turn(conn, client, "remind me at 8am")

    assert "already passed" in reply
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_run_turn_rejects_a_repeat_rule_that_includes_dtstart(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Check market", "when": "2026-09-26T21:00",
            "repeat": "DTSTART:20260926T210000\nRRULE:FREQ=DAILY",
        },
    )

    reply = run_turn(conn, client, "every day at 9pm remind me to check the market")

    assert "Could you tell me again?" in reply
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_run_turn_adds_a_todo(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="add_todo", function_args={"text": "Gym"},
    )

    assert run_turn(conn, client, "add gym to my list") == "📝 Added to today's list: Gym"


def test_run_turn_adds_a_todo_with_a_category(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="add_todo",
        function_args={"text": "Read a chapter", "category": "learning"},
    )

    assert run_turn(conn, client, "add read a chapter to my list") == (
        "📝 Added to today's list: Read a chapter"
    )
    category = conn.execute("SELECT category FROM todos").fetchone()[0]
    assert category == "learning"


def test_run_turn_adds_a_dated_todo(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="add_todo",
        function_args={"text": "Dentist", "due_date": "2026-09-28"},
    )

    reply = run_turn(conn, client, "add dentist for monday")

    assert reply == "📝 Added to your list for Mon 28 Sep: Dentist"


def test_run_turn_lists_the_agenda_through_gemini(conn, saturday_morning):
    from brain.reminders import add_todo
    from datetime import date

    add_todo(conn, "Gym", None, date(2026, 9, 26))
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="list_agenda", function_args={},
    )
    client.answer_with_tool_result.return_value = "Just the gym today, sir."

    reply = run_turn(conn, client, "what's on today?")

    assert reply == "Just the gym today, sir."
    assert "Gym" in client.answer_with_tool_result.call_args.kwargs["tool_result"]


def test_run_turn_finishes_an_item_by_id(conn, saturday_morning):
    from brain.reminders import add_todo
    from datetime import date

    tid = add_todo(conn, "Gym", None, date(2026, 9, 26))["id"]
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="finish_item",
        function_args={"item_id": f"t{tid}", "action": "done"},
    )

    assert run_turn(conn, client, "done with the gym") == "✅ Done: Gym"


def test_run_turn_reports_an_unknown_item(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="finish_item",
        function_args={"item_id": "t999", "action": "cancel"},
    )

    assert "couldn't find" in run_turn(conn, client, "cancel that").lower()


def test_run_turn_puts_todays_items_in_the_system_prompt(conn, saturday_morning):
    from brain.reminders import add_todo
    from datetime import date

    tid = add_todo(conn, "Gym", None, date(2026, 9, 26))["id"]
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("ok")

    run_turn(conn, client, "hi")

    system_prompt_sent = client.generate_with_tools.call_args.args[0]
    assert f"[t{tid}] Gym" in system_prompt_sent


# --- untimed reminders + edit_reminder --------------------------------------


def test_run_turn_offers_the_edit_reminder_tool(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("hi")

    run_turn(conn, client, "hi")

    tool_names = [t["name"] for t in client.generate_with_tools.call_args.args[4]]
    assert "edit_reminder" in tool_names


def test_run_turn_creates_an_untimed_reminder_and_states_no_ping(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Stretch", "when": "2026-09-26", "repeat": "FREQ=DAILY", "no_time": True,
        },
    )

    reply = run_turn(conn, client, "add a daily stretch to my list, no need to ping me")

    assert reply == "🗓️ On your list from Sat 26 Sep (repeats), no ping: Stretch"
    timed = conn.execute("SELECT timed FROM reminders").fetchone()[0]
    assert timed is False


def test_run_turn_creates_an_untimed_one_time_reminder_reply(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={"text": "Return library book", "when": "2026-09-28", "no_time": True},
    )

    reply = run_turn(conn, client, "add returning the library book to my list on Monday")

    assert reply == "🗓️ On your list from Mon 28 Sep, no ping: Return library book"


def test_run_turn_edits_a_reminders_time(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY",
        _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"),
    )
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder",
        function_args={"item_id": f"r{rid['id']}", "time": "15:00"},
    )

    reply = run_turn(conn, client, "move Duolingo to 15:00")

    assert reply == "✏️ Updated — Duolingo: next Sat 26 Sep, 15:00 (repeats)"


def test_run_turn_edits_a_reminder_to_untimed(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY",
        _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"),
    )
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder",
        function_args={"item_id": f"r{rid['id']}", "no_time": True},
    )

    reply = run_turn(conn, client, "stop pinging me for Duolingo, just put it on my list")

    assert reply == "✏️ Updated — Duolingo: on your list from Sat 26 Sep (repeats), no ping"


def test_run_turn_edit_reports_ambiguous_matches(conn, saturday_morning):
    from brain.reminders import create_reminder

    create_reminder(conn, "Pay rent", "2026-09-26T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"))
    create_reminder(conn, "Pay Giorgi back", "2026-09-27T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"))
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder",
        function_args={"name": "pay", "time": "15:00"},
    )

    reply = run_turn(conn, client, "move the pay reminder to 15:00")

    assert reply.startswith("Which one do you mean?")
    assert "Pay rent" in reply and "Pay Giorgi back" in reply


def test_run_turn_edit_reports_not_found(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder",
        function_args={"item_id": "r999", "time": "15:00"},
    )

    assert "couldn't find" in run_turn(conn, client, "move that to 15:00").lower()


def test_run_turn_edit_with_no_fields_asks_what_to_change(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Duolingo", "2026-09-26T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"),
    )["id"]
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder", function_args={"item_id": f"r{rid}"},
    )

    reply = run_turn(conn, client, "change duolingo")

    assert "Could you tell me again?" in reply


def test_run_turn_list_agenda_tool_result_includes_ids(conn, saturday_morning):
    from brain.reminders import add_todo
    from datetime import date

    tid = add_todo(conn, "Gym", None, date(2026, 9, 26))["id"]
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="list_agenda", function_args={},
    )
    client.answer_with_tool_result.return_value = "Just the gym today, sir."

    run_turn(conn, client, "what's on today?")

    tool_result = client.answer_with_tool_result.call_args.kwargs["tool_result"]
    assert f"[t{tid}]" in tool_result


# --- review fixes: stale untimed date in the reply (finding 5) -------------


def test_run_turn_edit_untimed_reply_uses_next_occurrence_not_a_stale_start(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Stretch", "2026-09-01T00:00", "FREQ=DAILY", _at_tbilisi(1, 10),
        ZoneInfo("Asia/Tbilisi"), timed=False,
    )["id"]
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="edit_reminder",
        function_args={"item_id": f"r{rid}", "category": "fitness"},
    )

    reply = run_turn(conn, client, "move stretch to the fitness category")

    assert reply == "✏️ Updated — Stretch: on your list from Sat 26 Sep (repeats), no ping"


def test_run_turn_creates_an_untimed_reminder_reply_uses_next_occurrence(conn, saturday_morning):
    client = Mock()
    client.generate_with_tools.return_value = ToolReply(
        text=None, function_name="create_reminder",
        function_args={
            "text": "Stretch", "when": "2026-09-01", "repeat": "FREQ=DAILY", "no_time": True,
        },
    )

    reply = run_turn(conn, client, "add a daily stretch, backdated, no ping")

    assert reply == "🗓️ On your list from Sat 26 Sep (repeats), no ping: Stretch"


# --- Task 5: routing + fallback + brain tag in run_turn --------------------

from brain.brains import BrainResult
from brain.conversation import Brains, BUSY_MESSAGE
from brain.router import RouteDecision


class FakeBrain:
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0
        self.last_ctx = None

    def respond(self, ctx):
        self.calls += 1
        self.last_ctx = ctx
        return self.result


def _brains(g, c, q, show=True):
    return Brains(gemini=g, claude=c, groq=q, groq_api_key="k", groq_model="m", show_tag=show)


def _route(monkeypatch, tier, reply=None):
    import brain.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "route",
        lambda message, has_image, history, has_pending, groq_api_key, groq_model, is_reply=False, continues_claude=False:
            RouteDecision(tier, "judge", message, reply),
    )


def test_hard_goes_to_claude_with_tag(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult("Plan ready.", "sonnet"))
    out = run_turn(
        conn, Mock(), "plan my week", brains=_brains(FakeBrain("gemini", None), c, None)
    )
    assert out == "Plan ready.\n— sonnet"


def test_claude_limit_falls_back_to_gemini_with_reason(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult(None, "sonnet", failure="limit", failure_detail="15:45"))
    g = FakeBrain("gemini", BrainResult("Here you go.", "gemini"))
    out = run_turn(conn, Mock(), "plan my week", brains=_brains(g, c, None))
    assert out == "Here you go.\n— gemini (Claude limit, resets 15:45)"


def test_claude_limit_without_reset_time_omits_it(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult(None, "sonnet", failure="limit", failure_detail=""))
    g = FakeBrain("gemini", BrainResult("Here you go.", "gemini"))
    out = run_turn(conn, Mock(), "plan my week", brains=_brains(g, c, None))
    assert out == "Here you go.\n— gemini (Claude limit)"


def test_no_fallback_after_a_tool_ran(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain(
        "sonnet",
        BrainResult(
            None, "sonnet", tools_ran=["create_reminder"],
            side_effect_tools_ran=["create_reminder"], failure="unavailable",
        ),
    )
    g = FakeBrain("gemini", BrainResult("dup", "gemini"))
    out = run_turn(conn, Mock(), "remind me x and plan y", brains=_brains(g, c, None))
    assert g.calls == 0 and out.startswith("Done:")


def test_no_fallback_after_a_tool_ran_humanizes_tool_names(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain(
        "sonnet",
        BrainResult(
            None, "sonnet", tools_ran=["create_reminder", "add_todo"],
            side_effect_tools_ran=["create_reminder", "add_todo"], failure="unavailable",
        ),
    )
    g = FakeBrain("gemini", BrainResult("dup", "gemini"))
    out = run_turn(conn, Mock(), "remind me x and plan y", brains=_brains(g, c, None))
    assert out.startswith(
        "Done: create reminder, add todo — but I lost my train of thought partway. "
        "Ask me again for the rest."
    )


def test_small_talk_uses_judge_reply(conn, monkeypatch):
    _route(monkeypatch, "small_talk", "Good night, sir.")
    g = FakeBrain("gemini", BrainResult("x", "gemini"))
    assert run_turn(
        conn, Mock(), "good night", brains=_brains(g, None, None)
    ) == "Good night, sir.\n— groq"
    assert g.calls == 0


def test_all_fail_is_busy_and_history_is_clean(conn, monkeypatch):
    _route(monkeypatch, "normal")
    fail = lambda n: FakeBrain(n, BrainResult(None, n, failure="unavailable"))
    out = run_turn(
        conn, Mock(), "hi", brains=_brains(fail("gemini"), fail("sonnet"), fail("groq"))
    )
    assert out == BUSY_MESSAGE
    messages = get_recent_messages(conn)
    assert [m["role"] for m in messages] == ["user"]


def test_tag_hidden_and_not_stored(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult("ok", "gemini"))
    assert run_turn(conn, Mock(), "hi", brains=_brains(g, None, None, show=False)) == "ok"
    assert get_recent_messages(conn)[-1]["content"] == "ok"


def test_normal_chain_falls_back_gemini_to_claude_with_reason(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    c = FakeBrain("sonnet", BrainResult("Here.", "sonnet"))
    out = run_turn(conn, Mock(), "hi", brains=_brains(g, c, None))
    assert out == "Here.\n— sonnet (Gemini unavailable)"


def test_normal_chain_falls_back_to_groq_with_both_unavailable_reason(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    c = FakeBrain("sonnet", BrainResult(None, "sonnet", failure="unavailable"))
    q = FakeBrain("groq", BrainResult("Groq answer.", "groq"))
    out = run_turn(conn, Mock(), "hi", brains=_brains(g, c, q))
    assert out == "Groq answer.\n— groq (Gemini and Claude unavailable)"


def test_config_failure_counts_as_unavailable_in_tag(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="config"))
    c = FakeBrain("sonnet", BrainResult("Here.", "sonnet"))
    out = run_turn(conn, Mock(), "hi", brains=_brains(g, c, None))
    assert out == "Here.\n— sonnet (Gemini unavailable)"


def test_none_brain_in_chain_is_skipped(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    q = FakeBrain("groq", BrainResult("Groq answer.", "groq"))
    out = run_turn(conn, Mock(), "hi", brains=_brains(g, None, q))
    assert out == "Groq answer.\n— groq (Gemini unavailable)"


def test_image_forces_gemini_first_chain_even_for_hard_tier(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult("should not be reached first", "sonnet"))
    g = FakeBrain("gemini", BrainResult("Saw it.", "gemini"))
    out = run_turn(
        conn, Mock(), "what's in this picture",
        image_bytes=b"fake", image_mime_type="image/png",
        brains=_brains(g, c, None),
    )
    assert out == "Saw it.\n— gemini"
    assert c.calls == 0


def test_image_reaches_claude_by_fallback_with_unseen_image_note(conn, monkeypatch):
    _route(monkeypatch, "hard")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    c = FakeBrain("sonnet", BrainResult("Text-only answer.", "sonnet"))
    out = run_turn(
        conn, Mock(), "what's in this picture",
        image_bytes=b"fake", image_mime_type="image/png",
        brains=_brains(g, c, None),
    )
    assert out == "Text-only answer.\n— sonnet (Gemini unavailable)"


def test_cleaned_override_stripped_message_reaches_the_brain(conn, monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "route",
        lambda message, has_image, history, has_pending, groq_api_key, groq_model, is_reply=False, continues_claude=False:
            RouteDecision("hard", "override", "plan my week", None),
    )
    c = FakeBrain("sonnet", BrainResult("Plan ready.", "sonnet"))
    run_turn(conn, Mock(), "use claude plan my week", brains=_brains(FakeBrain("gemini", None), c, None))

    assert c.last_ctx.message == "plan my week"


def test_brains_none_produces_no_tag(conn):
    client = Mock()
    client.generate_with_tools.return_value = _plain_reply("Hi! How can I help?")

    reply = run_turn(conn, client, "hello")

    assert reply == "Hi! How can I help?"
    assert "—" not in reply


# --- final-review fixes: run_turn with brains --------------------------------


def test_run_turn_passes_is_reply_to_route(conn, monkeypatch):
    import brain.conversation as conversation

    seen = []

    def fake_route(message, has_image, history, has_pending, key, model, is_reply=False,
                   continues_claude=False):
        seen.append(is_reply)
        return RouteDecision("normal", "judge", message, None)

    monkeypatch.setattr(conversation, "route", fake_route)
    g = FakeBrain("gemini", BrainResult("ok", "gemini"))
    run_turn(conn, Mock(), "yes", brains=_brains(g, None, None), replied_to="Shall I?")
    run_turn(conn, Mock(), "hello", brains=_brains(g, None, None))
    assert seen == [True, False]


def test_reply_prefix_reaches_brain_and_is_stored_in_history(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult("ok", "gemini"))
    run_turn(conn, Mock(), "yes", brains=_brains(g, None, None), replied_to="Shall I?")
    assert g.last_ctx.message == '[Replying to: "Shall I?"] yes'
    stored = [m for m in get_recent_messages(conn) if m["role"] == "user"][-1]
    assert stored["content"] == '[Replying to: "Shall I?"] yes'


def test_claude_failure_after_read_only_tool_falls_back(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain(
        "sonnet",
        BrainResult(None, "sonnet", tools_ran=["web_search"], failure="unavailable"),
    )
    g = FakeBrain("gemini", BrainResult("Here.", "gemini"))
    out = run_turn(conn, Mock(), "plan", brains=_brains(g, c, None))
    assert g.calls == 1
    assert out == "Here.\n— gemini (Claude unavailable)"


def test_claude_failure_after_side_effect_lists_only_side_effects(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain(
        "sonnet",
        BrainResult(
            None, "sonnet", tools_ran=["web_search", "create_reminder"],
            side_effect_tools_ran=["create_reminder"], failure="unavailable",
        ),
    )
    g = FakeBrain("gemini", BrainResult("dup", "gemini"))
    out = run_turn(conn, Mock(), "plan", brains=_brains(g, c, None))
    assert g.calls == 0
    assert out.startswith("Done: create reminder — but")
    assert "web search" not in out


def test_image_chain_drops_groq(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    q = FakeBrain("groq", BrainResult("blind", "groq"))
    out = run_turn(
        conn, Mock(), "what is this", image_bytes=b"x", image_mime_type="image/png",
        brains=_brains(g, None, q),
    )
    assert out == BUSY_MESSAGE and q.calls == 0


def test_pending_countdown_only_when_no_tool_ran_on_brains_path(conn, monkeypatch):
    from unittest.mock import patch as _patch

    _route(monkeypatch, "normal")
    with _patch("brain.conversation.get_pending_proposal", return_value={"created_at": None}), \
         _patch("brain.conversation.is_pending_proposal_expired", return_value=False), \
         _patch("brain.conversation.decrement_pending_proposal_turns") as dec:
        g = FakeBrain("gemini", BrainResult("ok", "gemini"))
        run_turn(conn, Mock(), "hi", brains=_brains(g, None, None))
        assert dec.call_count == 1
        g2 = FakeBrain("gemini", BrainResult("ok", "gemini", tools_ran=["x"]))
        run_turn(conn, Mock(), "hi", brains=_brains(g2, None, None))
        assert dec.call_count == 1


def test_handled_as_note_from_fallback_brain_skips_fact_extraction(conn, monkeypatch):
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult(None, "gemini", failure="unavailable"))
    c = FakeBrain("sonnet", BrainResult("Noted.", "sonnet", handled_as_note=True))
    client = Mock()
    run_turn(conn, client, "remember that I like tea", brains=_brains(g, c, None))
    client.review_memory.assert_not_called()


def test_system_prompt_points_reminder_questions_to_find_reminders():
    from brain.conversation import SYSTEM_PROMPT
    assert "find_reminders" in SYSTEM_PROMPT and "never search_memory" in SYSTEM_PROMPT


def test_find_reminders_tool_is_offered():
    from brain.reminders import REMINDER_TOOLS
    assert "find_reminders" in [t["name"] for t in REMINDER_TOOLS]


# --- Computer access: chat commands, per-turn computer brain, attachments ----

from datetime import datetime, timedelta, timezone

from brain import computer_state as cs


def _no_ai(monkeypatch):
    import brain.conversation as conversation

    def boom(*a, **k):
        raise AssertionError("a chat command must not call the router or any brain")

    monkeypatch.setattr(conversation, "route", boom)


def test_bypass_command_sets_bypass_and_replies_with_local_time(conn, monkeypatch):
    import brain.conversation as conversation

    _no_ai(monkeypatch)
    monkeypatch.setenv("JARVIS_TIMEZONE", "UTC")
    monkeypatch.setenv("BYPASS_HOURS", "12")
    now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(conversation, "_utc_now", lambda: now)
    client = Mock()

    reply = run_turn(conn, client, "/bypass", brains=_brains(FakeBrain("gemini", None), None, None))

    assert reply == "⚠️ Bypass on until 20:00 (local). Type safe to end it early."
    until = cs.get_bypass_until(conn, now)
    assert until == now + timedelta(hours=12) and until.tzinfo is not None
    client.generate_with_tools.assert_not_called()


def test_safe_command_clears_bypass(conn, monkeypatch):
    _no_ai(monkeypatch)
    cs.set_bypass(conn, datetime.now(timezone.utc) + timedelta(hours=3))

    reply = run_turn(conn, Mock(), "/safe", brains=_brains(FakeBrain("gemini", None), None, None))

    assert reply == "🛡️ Bypass off — I'll ask again before risky steps."
    assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is None


def test_commands_are_stored_in_history_like_normal_turns(conn):
    run_turn(conn, Mock(), "/safe")
    roles = [(m["role"], m["content"]) for m in get_recent_messages(conn)]
    assert roles[0] == ("user", "/safe") and roles[1][0] == "assistant"
    assert count_user_messages(conn) == 1


def test_queue_command_lists_or_says_empty(conn):
    assert run_turn(conn, Mock(), "/queue") == "No queued PC tasks."
    now = datetime.now(timezone.utc)
    first = cs.queue_pc_task(conn, "tidy my downloads", now)
    second = cs.queue_pc_task(conn, "zip the report", now + timedelta(seconds=1))

    reply = run_turn(conn, Mock(), "/queue")

    assert f"#{first}" in reply and "tidy my downloads" in reply
    assert f"#{second}" in reply and reply.index("tidy") < reply.index("zip")


def test_cancel_command_cancels_or_reports(conn):
    task = cs.queue_pc_task(conn, "x", datetime.now(timezone.utc))
    assert run_turn(conn, Mock(), f"/cancel {task}") == f"Cancelled queued task #{task}."
    assert cs.list_pc_tasks(conn) == []
    assert run_turn(conn, Mock(), f"/cancel {task}") == f"No queued task #{task}."
    assert run_turn(conn, Mock(), "/cancel") == "Usage: cancel <number> (see queue)."
    assert run_turn(conn, Mock(), "/cancel abc") == "Usage: cancel <number> (see queue)."


def test_log_command_shows_recent_activity_default_15(conn):
    assert run_turn(conn, Mock(), "/log") == "No activity logged yet."
    for i in range(20):
        cs.log_activity(conn, "pc", "t1", "Read", f"file{i}", "RUN", "auto", None)

    reply = run_turn(conn, Mock(), "/log")
    assert len(reply.splitlines()) == 15
    assert "file19" in reply and "file4" not in reply
    assert len(run_turn(conn, Mock(), "/log 3").splitlines()) == 3


def test_long_log_is_attached_as_activity_txt(conn):
    for i in range(60):
        cs.log_activity(conn, "pc", "t1", "Bash", "x" * 80 + str(i), "ALWAYS_ASK", "allowed", "Dad")
    files = []

    reply = run_turn(conn, Mock(), "/log 60", attachments_out=files)

    assert "activity.txt" in reply and len(reply) < 200
    assert files[0][0] == "activity.txt"
    assert files[0][1].decode().count("\n") == 59


def test_commands_are_ignored_when_not_allowed(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult("done", "sonnet"))

    out = run_turn(
        conn, Mock(), "/bypass", brains=_brains(FakeBrain("gemini", None), c, None),
        allow_commands=False,
    )

    assert out == "done\n— sonnet" and c.calls == 1
    assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is None


def test_tier_override_skips_the_judge(conn, monkeypatch):
    _no_ai(monkeypatch)
    c = FakeBrain("sonnet", BrainResult("ok", "sonnet"))
    g = FakeBrain("gemini", BrainResult("wrong", "gemini"))

    out = run_turn(conn, Mock(), "tidy", brains=_brains(g, c, None), tier_override="hard")

    assert out == "ok\n— sonnet" and g.calls == 0


def test_computer_factory_builds_a_fresh_claude_per_turn(conn, monkeypatch):
    _route(monkeypatch, "hard")
    built = []

    def factory():
        brain = FakeBrain("sonnet", BrainResult("ok", "sonnet"))
        built.append(brain)
        return brain

    brains = _brains(FakeBrain("gemini", None), FakeBrain("sonnet", None), None)
    brains.computer_claude_factory = factory
    brains.computer_machine = "pc"

    run_turn(conn, Mock(), "list my files", brains=brains)
    run_turn(conn, Mock(), "list my files", brains=brains)

    assert len(built) == 2 and all(b.calls == 1 for b in built)
    assert brains.claude.calls == 0


def test_no_claude_brain_means_no_computer_mode(conn, monkeypatch):
    _route(monkeypatch, "hard")
    factory = Mock()
    brains = _brains(FakeBrain("gemini", BrainResult("hi", "gemini")), None, None)
    brains.computer_claude_factory = factory
    brains.computer_machine = "server"

    out = run_turn(conn, Mock(), "list my files", brains=brains)

    assert out == "hi\n— gemini"
    factory.assert_not_called()


def test_on_the_server_gemini_never_gets_the_queue_pc_task_instructions(conn, monkeypatch):
    # Gemini has no queue_pc_task tool: told to call it, it wrote a fake tool call as text
    # and "Your PC is off" for a plain reminder. The queue instructions live in Claude's
    # own server prompt (brain/brains/claude.py) instead.
    _route(monkeypatch, "normal")
    g = FakeBrain("gemini", BrainResult("ok", "gemini"))
    brains = _brains(g, FakeBrain("sonnet", None), None)
    brains.computer_claude_factory = lambda: FakeBrain("sonnet", None)
    brains.computer_machine = "server"

    run_turn(conn, Mock(), "remind me tomorrow to reserve a hotel", brains=brains)

    prompt = g.last_ctx.system_prompt
    assert "queue_pc_task" not in prompt
    assert "Your PC is off" not in prompt


def test_brain_attachments_go_to_attachments_out(conn, monkeypatch):
    _route(monkeypatch, "hard")
    result = BrainResult("done", "sonnet", attachments=[("changes.diff", b"--- a")])
    c = FakeBrain("sonnet", result)
    files = []

    out = run_turn(
        conn, Mock(), "edit it", brains=_brains(FakeBrain("gemini", None), c, None),
        attachments_out=files,
    )

    assert out == "done\n— sonnet" and files == [("changes.diff", b"--- a")]
    # callers that don't ask for attachments are unaffected
    run_turn(conn, Mock(), "edit it", brains=_brains(FakeBrain("gemini", None), c, None))


def test_claude_only_override_has_no_fallback_and_reports_the_outcome(conn):
    g = FakeBrain("gemini", BrainResult("wrong", "gemini"))
    c = FakeBrain("sonnet", BrainResult(None, "sonnet", failure="unavailable"))
    outcome = {}

    out = run_turn(
        conn, Mock(), "tidy", brains=_brains(g, c, None), tier_override="claude_only",
        allow_commands=False, outcome_out=outcome,
    )

    assert out == BUSY_MESSAGE and g.calls == 0 and c.calls == 1
    assert outcome == {"ok": False, "brain": None, "tools": []}


def test_claude_only_override_reports_ok_for_a_clean_claude_answer(conn):
    c = FakeBrain("sonnet", BrainResult("done", "sonnet"))
    outcome = {}

    run_turn(
        conn, Mock(), "tidy", brains=_brains(FakeBrain("gemini", None), c, None),
        tier_override="claude_only", outcome_out=outcome,
    )

    assert outcome == {"ok": True, "brain": "sonnet", "tools": []}


def test_claude_only_without_a_claude_brain_is_busy(conn):
    g = FakeBrain("gemini", BrainResult("wrong", "gemini"))
    assert run_turn(
        conn, Mock(), "tidy", brains=_brains(g, None, None), tier_override="claude_only"
    ) == BUSY_MESSAGE and g.calls == 0


def test_humanize_tool_name_strips_the_mcp_prefix():
    from brain.conversation import _humanize_tool_name

    assert _humanize_tool_name("mcp__jarvis__move_to_trash") == "move to trash"
    assert _humanize_tool_name("create_note") == "create note"
    assert _humanize_tool_name("PowerShell") == "PowerShell"


def test_queued_task_is_stored_labelled_and_skips_fact_extraction(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult("done", "sonnet"))
    client = Mock()
    for _ in range(4):
        run_turn(conn, client, "hello", brains=_brains(FakeBrain("gemini", None), c, None))

    run_turn(
        conn, client, "tidy my desktop", brains=_brains(FakeBrain("gemini", None), c, None),
        queued_task=True,
    )  # the 5th user message would normally trigger fact extraction

    client.review_memory.assert_not_called()
    contents = [m["content"] for m in get_recent_messages(conn) if m["role"] == "user"]
    assert contents[-1] == "[queued task] tidy my desktop"
    assert c.last_ctx.message == "tidy my desktop"


def test_outcome_reports_the_tools_that_ran(conn, monkeypatch):
    _route(monkeypatch, "hard")
    c = FakeBrain("sonnet", BrainResult("ok", "sonnet", tools_ran=["web_search"]))
    outcome = {}
    run_turn(conn, Mock(), "x", brains=_brains(FakeBrain("gemini", None), c, None),
             outcome_out=outcome)
    assert outcome["tools"] == ["web_search"] and outcome["ok"] is True


@pytest.mark.parametrize("text", ["bypass", "Bypass", "!bypass", "/bypass", "  bypass  "])
def test_bypass_works_without_the_slash(conn, monkeypatch, text):
    _no_ai(monkeypatch)
    reply = run_turn(conn, Mock(), text, brains=_brains(FakeBrain("gemini", None), None, None))
    assert reply.startswith("⚠️ Bypass on until")
    assert cs.get_bypass_until(conn, datetime.now(timezone.utc)) is not None


@pytest.mark.parametrize("text", ["safe", "!safe", "queue", "!queue", "log", "log 5", "!log 5",
                                  "cancel 3", "!cancel 3"])
def test_other_commands_work_without_the_slash(text):
    from brain.conversation import is_command_word

    assert is_command_word(text)


@pytest.mark.parametrize("text", ["log my workout", "safe to say it's done", "queue up a song",
                                  "cancel my gym reminder", "bypass the traffic", "logs", "log five"])
def test_plain_words_in_normal_chat_are_not_commands(text):
    from brain.conversation import handle_command, is_command_word

    assert not is_command_word(text)
    assert handle_command(None, text) is None


def test_a_reply_right_after_claude_goes_back_to_claude(conn, monkeypatch):
    import brain.conversation as conversation

    seen = []

    def fake_route(message, has_image, history, has_pending, groq_api_key, groq_model,
                   is_reply=False, continues_claude=False):
        seen.append(continues_claude)
        return RouteDecision("hard" if continues_claude or message == "plan" else "normal", "judge", message)

    monkeypatch.setattr(conversation, "route", fake_route)
    c = FakeBrain("sonnet", BrainResult("Shall I save it?", "sonnet"))
    g = FakeBrain("gemini", BrainResult("ok", "gemini"))
    brains = _brains(g, c, None)
    run_turn(conn, Mock(), "plan", brains=brains)
    run_turn(conn, Mock(), "yes", brains=brains)
    assert seen == [False, True] and c.calls == 2 and g.calls == 0


def test_continuing_with_claude_expires_after_10_minutes(conn, monkeypatch):
    import brain.conversation as conversation
    from brain.memory import record_last_brain

    seen = []
    monkeypatch.setattr(conversation, "route",
                        lambda message, *a, continues_claude=False, **k:
                        seen.append(continues_claude) or RouteDecision("normal", "judge", message))
    record_last_brain(conn, "sonnet", datetime.now(timezone.utc) - timedelta(minutes=11))
    run_turn(conn, Mock(), "yes", brains=_brains(FakeBrain("gemini", BrainResult("ok", "gemini")), None, None))
    assert seen == [False]
