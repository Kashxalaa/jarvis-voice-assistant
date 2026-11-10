import json
from datetime import date
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from brain.tools import ToolExecutor, ToolOutcome, build_tool_list


def _at_tbilisi(day, hour, minute=0):
    from datetime import datetime

    return datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo("Asia/Tbilisi"))


@pytest.fixture
def saturday_morning(monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setenv("JARVIS_TIMEZONE", "Asia/Tbilisi")
    monkeypatch.setattr(conversation, "_utc_now", lambda: _at_tbilisi(26, 10))


# --- run_known_action --------------------------------------------------


def test_run_known_action_success_is_an_action_outcome(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "execute_action", lambda conn, name: "opened trading_journal")
    out = ToolExecutor(conn, Mock(), None).execute("run_known_action", {"name": "trading_journal"})
    assert out == ToolOutcome("Done — opened trading_journal.", "action")


def test_missing_name_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("run_known_action", {})
    assert out.kind == "fixed"


def test_run_known_action_not_found_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("run_known_action", {"name": "ghost"})
    assert out.kind == "fixed"
    assert "ghost" in out.text


def test_run_known_action_execution_error_is_fixed(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import upsert_action

    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")

    def failing_open(kind, value):
        raise OSError("file not found")

    monkeypatch.setattr(automation, "_open", failing_open)
    out = ToolExecutor(conn, Mock(), None).execute("run_known_action", {"name": "vscode"})
    assert out.kind == "fixed"
    assert "problem" in out.text.lower()


# --- run_ad_hoc_placement -----------------------------------------------


def test_run_ad_hoc_placement_success_is_an_action_outcome(conn, monkeypatch):
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
    steps = json.dumps([{"name": "tradingview", "monitor": 1, "size": [0.5, 0.5]}])
    out = ToolExecutor(conn, Mock(), None).execute("run_ad_hoc_placement", {"steps": steps})
    assert out.kind == "action"
    assert "tradingview" in out.text.lower()
    assert placed_calls


def test_run_ad_hoc_placement_missing_steps_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("run_ad_hoc_placement", {})
    assert out.kind == "fixed"


def test_run_ad_hoc_placement_unknown_action_is_fixed(conn):
    steps = json.dumps([{"name": "ghost_action"}])
    out = ToolExecutor(conn, Mock(), None).execute("run_ad_hoc_placement", {"steps": steps})
    assert out.kind == "fixed"
    assert "ghost_action" in out.text


# --- run_one_off_action ---------------------------------------------------


def test_run_one_off_action_success_is_an_action_outcome(conn, monkeypatch):
    import brain.automation as automation

    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))
    out = ToolExecutor(conn, Mock(), None).execute(
        "run_one_off_action", {"kind": "url", "value": "https://search.example.com"}
    )
    assert out.kind == "action"
    assert opened == [("url", "https://search.example.com")]


def test_run_one_off_action_missing_fields_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("run_one_off_action", {"kind": "url"})
    assert out.kind == "fixed"


def test_run_one_off_action_failure_is_fixed(conn, monkeypatch):
    import brain.automation as automation

    def failing_open(kind, value):
        raise OSError("boom")

    monkeypatch.setattr(automation, "_open", failing_open)
    out = ToolExecutor(conn, Mock(), None).execute(
        "run_one_off_action", {"kind": "url", "value": "https://x.y"}
    )
    assert out.kind == "fixed"
    assert "problem" in out.text.lower()


# --- confirm_pending_action ------------------------------------------------


def _pending(conn):
    from brain.automation import propose_action
    from brain.memory import get_pending_proposal

    propose_action(conn, "url", "quickbrew_site", "https://quickbrew.com", "reason")
    return get_pending_proposal(conn)


def test_confirm_pending_action_executes_and_is_action_outcome(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import get_action, get_pending_proposal

    pending = _pending(conn)
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    out = ToolExecutor(conn, Mock(), pending).execute(
        "confirm_pending_action", {"confirmed": True}
    )
    assert out.kind == "action"
    assert "quickbrew_site" in out.text
    assert opened == [("url", "https://quickbrew.com")]
    assert get_action(conn, "quickbrew_site") is not None
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_denied_clears_without_executing(conn, monkeypatch):
    import brain.automation as automation
    from brain.memory import get_action, get_pending_proposal

    pending = _pending(conn)
    opened = []
    monkeypatch.setattr(automation, "_open", lambda kind, value: opened.append((kind, value)))

    out = ToolExecutor(conn, Mock(), pending).execute(
        "confirm_pending_action", {"confirmed": False}
    )
    assert out.kind == "action"
    assert opened == []
    assert get_action(conn, "quickbrew_site") is None
    assert get_pending_proposal(conn) is None


def test_confirm_pending_action_denied_with_correction_reproposes(conn):
    pending = _pending(conn)
    out = ToolExecutor(conn, Mock(), pending).execute(
        "confirm_pending_action",
        {"confirmed": False, "corrected_value": "https://quickbrew.net"},
    )
    assert out.kind == "action"
    assert "quickbrew.net" in out.text


def test_confirm_pending_action_saved_but_not_executed_is_fixed(conn, monkeypatch):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_pending_proposal

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    propose_action(conn, "folder", "journal_folder", "E:/journal", "reason")
    pending = get_pending_proposal(conn)
    out = ToolExecutor(conn, Mock(), pending).execute(
        "confirm_pending_action", {"confirmed": True}
    )
    assert out.kind == "fixed"
    assert "journal_folder" in out.text
    assert "back on" in out.text.lower()


def test_confirm_pending_action_execution_error_is_fixed(conn, monkeypatch):
    import brain.automation as automation
    from brain.automation import propose_action
    from brain.memory import get_pending_proposal

    propose_action(conn, "folder", "ghost_folder", "E:/does/not/exist", "reason")
    pending = get_pending_proposal(conn)

    def failing_open(kind, value):
        raise OSError("path not found")

    monkeypatch.setattr(automation, "_open", failing_open)
    out = ToolExecutor(conn, Mock(), pending).execute(
        "confirm_pending_action", {"confirmed": True}
    )
    assert out.kind == "fixed"
    assert "problem" in out.text.lower()


def test_confirm_pending_action_not_offered_without_pending_is_unknown(conn):
    out = ToolExecutor(conn, Mock(), None).execute(
        "confirm_pending_action", {"confirmed": True}
    )
    assert out.kind == "fixed"
    assert out.text == "I don't know how to do that yet."


# --- propose_new_action ------------------------------------------------


def test_propose_new_action_success_is_action_outcome(conn):
    from brain.memory import get_pending_proposal

    out = ToolExecutor(conn, Mock(), None).execute(
        "propose_new_action",
        {
            "kind": "url",
            "suggested_name": "quickbrew_site",
            "suggested_value": "https://quickbrew.com",
            "reasoning": "sounds like a website",
        },
    )
    assert out.kind == "action"
    assert "quickbrew" in out.text.lower()
    assert get_pending_proposal(conn)["suggested_name"] == "quickbrew_site"


def test_propose_new_action_missing_fields_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute(
        "propose_new_action", {"kind": "url"}
    )
    assert out.kind == "fixed"


# --- search_memory --------------------------------------------------------


def test_search_memory_with_results_is_information_with_fold(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(
        tools,
        "search_notes",
        lambda conn, client, query, limit=3: [
            {"id": 1, "title": "Trading setup", "content": "Journal automation.",
             "tags": "", "importance": 7, "use_count": 1, "similarity": 0.9}
        ],
    )
    out = ToolExecutor(conn, Mock(), None).execute(
        "search_memory", {"query": "trading"}, model_text="Let me check:"
    )
    assert out.kind == "information"
    assert "Trading setup" in out.text
    assert out.fallback == f"Let me check:\n\n{out.text}"


def test_search_memory_no_model_text_fallback_prefixed(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(
        tools,
        "search_notes",
        lambda conn, client, query, limit=3: [
            {"id": 1, "title": "Trading setup", "content": "Journal automation.",
             "tags": "", "importance": 7, "use_count": 1, "similarity": 0.9}
        ],
    )
    out = ToolExecutor(conn, Mock(), None).execute("search_memory", {"query": "trading"})
    assert out.fallback == f"Here's what I remember:\n{out.text}"


def test_search_memory_no_results_is_information(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "search_notes", lambda conn, client, query, limit=3: [])
    out = ToolExecutor(conn, Mock(), None).execute("search_memory", {"query": "anything"})
    assert out.kind == "information"
    assert out.text == "(no saved notes matched)"
    assert "don't have anything" in out.fallback.lower()


def test_search_memory_missing_query_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("search_memory", {})
    assert out.kind == "fixed"


# --- create_note ------------------------------------------------------


def test_create_note_success_is_handled_as_note(conn, monkeypatch):
    import brain.tools as tools

    created = []
    monkeypatch.setattr(
        tools,
        "create_note",
        lambda conn, client, title, content, tags, importance, source="explicit": (
            created.append((title, content, tags, importance)) or 1
        ),
    )
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_note",
        {"title": "Trading setup", "content": "Configured.", "tags": "trading", "importance": 7},
    )
    assert out.kind == "action"
    assert out.handled_as_note is True
    assert "Trading setup" in out.text
    assert created == [("Trading setup", "Configured.", "trading", 7)]


def test_create_note_missing_fields_is_fixed(conn):
    out = ToolExecutor(conn, Mock(), None).execute("create_note", {"title": "Only a title"})
    assert out.kind == "fixed"
    assert out.handled_as_note is False


def test_create_note_failure_is_fixed_not_handled_as_note(conn, monkeypatch):
    import brain.tools as tools
    from brain.notes import NoteCreationError

    def _raise(*args, **kwargs):
        raise NoteCreationError("embedding failed")

    monkeypatch.setattr(tools, "create_note", _raise)
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_note", {"title": "T", "content": "C"}
    )
    assert out.kind == "fixed"
    assert out.handled_as_note is False
    assert "couldn't save" in out.text.lower()


def test_create_note_defaults_importance_when_out_of_range(conn, monkeypatch):
    import brain.tools as tools

    captured = {}
    monkeypatch.setattr(
        tools,
        "create_note",
        lambda conn, client, title, content, tags, importance, source="explicit": (
            captured.update(importance=importance) or 1
        ),
    )
    ToolExecutor(conn, Mock(), None).execute(
        "create_note", {"title": "T", "content": "C", "importance": 999}
    )
    assert captured["importance"] == 5


# --- web/info tools ---------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, args, patch_name, fake, expected",
    [
        ("read_webpage", {"url": "https://x.y"}, "read_webpage", lambda url: "PAGE", "PAGE"),
        ("get_youtube_info", {"url": "https://yt/1"}, "get_youtube_info",
         lambda url: "VIDEO INFO", "VIDEO INFO"),
        ("get_github_repo", {"owner_repo": "a/b"}, "get_github_repo",
         lambda owner_repo: "REPO INFO", "REPO INFO"),
        ("read_rss_feed", {"url": "https://x.y/feed"}, "read_rss_feed",
         lambda url: "FEED ENTRIES", "FEED ENTRIES"),
        ("web_search", {"query": "pizza"}, "web_search",
         lambda query: "SEARCH RESULTS", "SEARCH RESULTS"),
    ],
)
def test_web_tool_is_information_with_folded_fallback(
    conn, monkeypatch, tool_name, args, patch_name, fake, expected
):
    import brain.tools as tools

    monkeypatch.setattr(tools, patch_name, fake)
    out = ToolExecutor(conn, Mock(), None).execute(tool_name, args, model_text="Here:")
    assert out == ToolOutcome(expected, "information", fallback=f"Here:\n\n{expected}")


def test_web_tool_no_model_text_fallback_is_bare_result(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_webpage", lambda url: "PAGE")
    out = ToolExecutor(conn, Mock(), None).execute("read_webpage", {"url": "https://x.y"})
    assert out == ToolOutcome("PAGE", "information", fallback="PAGE")


@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("read_webpage", {}),
        ("get_youtube_info", {}),
        ("get_github_repo", {}),
        ("read_rss_feed", {}),
        ("web_search", {}),
    ],
)
def test_web_tool_missing_required_arg_is_fixed(conn, tool_name, args):
    out = ToolExecutor(conn, Mock(), None).execute(tool_name, args)
    assert out.kind == "fixed"


def test_get_weather_passes_conn_and_is_information(conn, monkeypatch):
    import brain.tools as tools

    captured = {}

    def fake_get_weather(conn_arg, location):
        captured["conn"] = conn_arg
        captured["location"] = location
        return "sunny"

    monkeypatch.setattr(tools, "get_weather", fake_get_weather)
    out = ToolExecutor(conn, Mock(), None).execute("get_weather", {"location": "Tokyo"})
    assert out == ToolOutcome("sunny", "information", fallback="sunny")
    assert captured["conn"] is conn
    assert captured["location"] == "Tokyo"


def test_get_weather_with_no_location_passes_none(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(
        tools, "get_weather", lambda conn_arg, location: f"weather for {location}"
    )
    out = ToolExecutor(conn, Mock(), None).execute("get_weather", {})
    assert out.text == "weather for None"


# --- unknown tool / unexpected exception -------------------------------


def test_unknown_tool_is_fixed_and_never_raises(conn):
    out = ToolExecutor(conn, Mock(), None).execute("does_not_exist", {})
    assert out.kind == "fixed"
    assert out.text == "I don't know how to do that yet."


def test_unexpected_exception_becomes_fixed_text(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "read_webpage", Mock(side_effect=RuntimeError("boom")))
    out = ToolExecutor(conn, Mock(), None).execute("read_webpage", {"url": "https://x.y"})
    assert out.kind == "fixed" and "problem" in out.text


# --- reminder tools -----------------------------------------------------


def test_create_reminder_states_exact_time(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder",
        {"text": "Close trades", "when": "2026-09-26T21:00"},
    )
    assert out == ToolOutcome("⏰ Reminder set — Sat 26 Sep, 21:00: Close trades", "fixed")
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_create_reminder_bad_time_is_fixed(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder", {"text": "Too late", "when": "2026-09-26T08:00"}
    )
    assert out.kind == "fixed"
    assert "already passed" in out.text


def test_create_reminder_with_category_and_early_reminder(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder",
        {
            "text": "Giorgi's birthday", "when": "2027-04-10T10:00",
            "repeat": "FREQ=YEARLY", "category": "birthday", "remind_days_before": 1,
        },
    )
    assert out.text == (
        "⏰ Reminder set — Sat 10 Apr, 10:00: Giorgi's birthday (repeats) "
        "(+ early reminder Fri 9 Apr, 10:00)"
    )


def test_create_reminder_rejects_out_of_range_lead_days(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder",
        {"text": "Close trades", "when": "2026-09-26T21:00", "remind_days_before": 31},
    )
    assert "between 1 and 30 days early" in out.text
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_create_reminder_untimed_states_no_ping(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder",
        {"text": "Stretch", "when": "2026-09-26", "repeat": "FREQ=DAILY", "no_time": True},
    )
    assert out.text == "🗓️ On your list from Sat 26 Sep (repeats), no ping: Stretch"


def test_create_reminder_rejects_repeat_rule_with_dtstart(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder",
        {
            "text": "Check market", "when": "2026-09-26T21:00",
            "repeat": "DTSTART:20260926T210000\nRRULE:FREQ=DAILY",
        },
    )
    assert "Could you tell me again?" in out.text
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_add_todo_default_today(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute("add_todo", {"text": "Gym"})
    assert out == ToolOutcome("📝 Added to today's list: Gym", "fixed")


def test_add_todo_dated(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "add_todo", {"text": "Dentist", "due_date": "2026-09-28"}
    )
    assert out.text == "📝 Added to your list for Mon 28 Sep: Dentist"


def test_list_agenda_is_information(conn, saturday_morning):
    from brain.reminders import add_todo

    add_todo(conn, "Gym", None, date(2026, 9, 26))
    out = ToolExecutor(conn, Mock(), None).execute("list_agenda", {})
    assert out.kind == "information"
    assert "Gym" in out.text
    assert out.fallback == out.text


def test_finish_item_done(conn, saturday_morning):
    from brain.reminders import add_todo

    tid = add_todo(conn, "Gym", None, date(2026, 9, 26))["id"]
    out = ToolExecutor(conn, Mock(), None).execute(
        "finish_item", {"item_id": f"t{tid}", "action": "done"}
    )
    assert out == ToolOutcome("✅ Done: Gym", "fixed")


def test_finish_item_unknown_is_fixed(conn, saturday_morning):
    out = ToolExecutor(conn, Mock(), None).execute(
        "finish_item", {"item_id": "t999", "action": "cancel"}
    )
    assert "couldn't find" in out.text.lower()


def test_edit_reminder_time_change(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY",
        _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"),
    )
    out = ToolExecutor(conn, Mock(), None).execute(
        "edit_reminder", {"item_id": f"r{rid['id']}", "time": "15:00"}
    )
    assert out.text == "✏️ Updated — Duolingo: next Sat 26 Sep, 15:00 (repeats)"


def test_edit_reminder_ambiguous_is_fixed(conn, saturday_morning):
    from brain.reminders import create_reminder

    create_reminder(conn, "Pay rent", "2026-09-26T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"))
    create_reminder(conn, "Pay Giorgi back", "2026-09-27T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"))
    out = ToolExecutor(conn, Mock(), None).execute(
        "edit_reminder", {"name": "pay", "time": "15:00"}
    )
    assert out.text.startswith("Which one do you mean?")


def test_edit_reminder_no_fields_asks_again(conn, saturday_morning):
    from brain.reminders import create_reminder

    rid = create_reminder(
        conn, "Duolingo", "2026-09-26T19:00", None, _at_tbilisi(26, 10), ZoneInfo("Asia/Tbilisi"),
    )["id"]
    out = ToolExecutor(conn, Mock(), None).execute(
        "edit_reminder", {"item_id": f"r{rid}"}
    )
    assert "Could you tell me again?" in out.text


def test_reminder_tool_psycopg_error_is_reminder_storage_failed(conn, monkeypatch, saturday_morning):
    import brain.tools as tools
    import psycopg

    def _raise(*args, **kwargs):
        raise psycopg.OperationalError("simulated failure")

    monkeypatch.setattr(tools, "create_reminder", _raise)
    out = ToolExecutor(conn, Mock(), None).execute(
        "create_reminder", {"text": "x", "when": "2026-09-26T21:00"}
    )
    assert out.kind == "fixed"
    assert out.text == tools.REMINDER_STORAGE_FAILED


# --- build_tool_list ----------------------------------------------------


def test_build_tool_list_includes_reminder_tools(conn):
    tool_names = [t["name"] for t in build_tool_list(conn, has_pending=False)]
    assert {"create_reminder", "add_todo", "list_agenda", "finish_item", "edit_reminder"} <= set(
        tool_names
    )


def test_build_tool_list_offers_confirm_tool_only_when_pending(conn):
    with_pending = [t["name"] for t in build_tool_list(conn, has_pending=True)]
    without_pending = [t["name"] for t in build_tool_list(conn, has_pending=False)]
    assert "confirm_pending_action" in with_pending
    assert "confirm_pending_action" not in without_pending


def test_build_tool_list_omits_automation_tools_when_unsupported(conn, monkeypatch):
    import brain.automation as automation

    monkeypatch.setattr(automation, "AUTOMATION_SUPPORTED", False)
    tool_names = [t["name"] for t in build_tool_list(conn, has_pending=False)]
    assert "run_known_action" not in tool_names
    assert "propose_new_action" not in tool_names


def test_build_tool_list_includes_web_search_when_exa_key_configured(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "resolve_exa_api_key", lambda: "exa-key")
    tool_names = [t["name"] for t in build_tool_list(conn, has_pending=False)]
    assert "web_search" in tool_names


def test_build_tool_list_omits_web_search_when_no_exa_key(conn, monkeypatch):
    import brain.tools as tools

    monkeypatch.setattr(tools, "resolve_exa_api_key", lambda: None)
    tool_names = [t["name"] for t in build_tool_list(conn, has_pending=False)]
    assert "web_search" not in tool_names


def test_find_reminders_tool_is_information(conn):
    from brain.reminders import create_reminder
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Tbilisi")
    create_reminder(conn, "🎂 Nino's birthday", "2027-06-03T10:00", "FREQ=YEARLY",
                    datetime.now(tz), tz, category="birthday")
    out = ToolExecutor(conn, Mock(), None).execute("find_reminders", {"query": "nino"})
    assert out.kind == "information" and "Nino's birthday" in out.text
