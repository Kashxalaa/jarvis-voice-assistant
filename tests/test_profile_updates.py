"""Keeping the profile updated: the update_profile tool, the background review and the note."""
import dataclasses
from unittest.mock import Mock

import pytest

from brain.brains import BrainResult
from brain.conversation import Brains, run_turn
from brain.gemini_client import GeminiClient
from brain.guard import GuardContext, Verdict, evaluate
from brain.profile import get_profile, set_profile
from brain.router import RouteDecision
from brain.tools import ToolExecutor, build_tool_list

WIN = GuardContext("pc", "windows", "C:\\Users\\alex", "E:\\Programing\\VoiceAssistant")


# ---- the tool ---------------------------------------------------------------

def test_update_profile_tool_is_offered(conn):
    assert "update_profile" in {t["name"] for t in build_tool_list(conn, False)}


def test_update_profile_applies_edits_and_records_the_note(conn):
    set_profile(conn, "# About\n\n## Typical week\n- boxing 06:00\n", "seed", "seed")
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("update_profile", {"edits": [
        {"section": "Typical week", "action": "replace", "old": "06:00", "new": "boxing 07:00"}]})
    assert out.kind == "action"
    assert "- boxing 07:00" in get_profile(conn)
    assert ex.profile_notes == ["boxing 06:00 → boxing 07:00"]


def test_update_profile_that_changes_nothing_says_why(conn):
    set_profile(conn, "# About\n\n## People\n- Sam\n", "seed", "seed")
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("update_profile", {"edits": [{"section": "Nope", "action": "remove", "old": "x"}]})
    assert out.kind == "fixed" and "couldn't update" in out.text and ex.profile_notes == []
    assert get_profile(conn) == "# About\n\n## People\n- Sam\n"


def test_update_profile_that_would_be_too_long_is_refused(conn):
    set_profile(conn, "# About\n\n## People\n- Sam\n", "seed", "seed")
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("update_profile", {"edits": [{"section": "People", "action": "add", "new": "x" * 5000}]})
    assert out.kind == "fixed" and "couldn't update" in out.text and ex.profile_notes == []


# ---- the guard --------------------------------------------------------------

def test_update_profile_runs_but_asks_after_web_content():
    assert evaluate("mcp__jarvis__update_profile", {"edits": []}, WIN).verdict == Verdict.RUN
    d = evaluate("mcp__jarvis__update_profile", {"edits": []}, dataclasses.replace(WIN, tainted=True, bypass=True))
    assert d.needs_approval


# ---- the review -------------------------------------------------------------

def _gemini(text):
    fake = Mock()
    fake.models.generate_content.return_value = Mock(text=text)
    return GeminiClient(api_key="unused", client=fake), fake


def test_review_memory_parses_an_edit_list_and_sends_profile_and_chat():
    client, fake = _gemini(
        '```json\n{"profile": [{"section": "People", "action": "add", "new": "Nika"}], "lessons": []}\n```')
    review = client.review_memory("## People\n- Sam", [], [{"role": "user", "content": "my brother Nika"}])
    assert review == {"profile": [{"section": "People", "action": "add", "new": "Nika"}], "lessons": []}
    prompt = fake.models.generate_content.call_args.kwargs["contents"]
    assert "- Sam" in prompt and "my brother Nika" in prompt


@pytest.mark.parametrize("text", ["not json", '[{"a": 1}]'])
def test_review_memory_returns_none_for_unusable_output(text):
    client, _ = _gemini(text)
    assert client.review_memory("", [], []) is None


# ---- in a turn ----------------------------------------------------------------

class _Brain:
    name = "gemini"

    def __init__(self, result, note=None):
        self.result, self.note = result, note

    def respond(self, ctx):
        if self.note:
            ctx.executor.profile_notes.append(self.note)
        return self.result


def _route(monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setattr(conversation, "route",
                        lambda message, *a, **k: RouteDecision("normal", "judge", message, None))


def _brains(g):
    return Brains(gemini=g, claude=None, groq=None, groq_api_key="k", groq_model="m")


def test_review_every_5_messages_updates_the_profile_and_adds_the_note(conn, monkeypatch):
    set_profile(conn, "# About\n\n## People\n- Sam\n", "seed", "seed")
    _route(monkeypatch)
    client = Mock()
    client.review_memory.return_value = {"profile": [{"section": "People", "action": "add", "new": "Nika: brother"}], "lessons": []}
    g = _Brain(BrainResult("ok", "gemini"))
    replies = [run_turn(conn, client, f"m{i}", brains=_brains(g)) for i in range(5)]
    assert client.review_memory.call_count == 1
    assert "- Nika: brother" in get_profile(conn)
    assert "📝 Profile: + Nika: brother" in replies[-1]
    assert replies[-1].index("📝") < replies[-1].index("— gemini")


def test_review_runs_right_away_on_remember(conn, monkeypatch):
    _route(monkeypatch)
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    run_turn(conn, client, "remember that I hate mornings", brains=_brains(_Brain(BrainResult("ok", "gemini"))))
    client.review_memory.assert_called_once()
    client.extract_facts.assert_not_called()


def test_review_is_skipped_when_the_tool_already_updated_the_profile(conn, monkeypatch):
    _route(monkeypatch)
    client = Mock()
    g = _Brain(BrainResult("ok", "gemini", tools_ran=["update_profile"], side_effect_tools_ran=["update_profile"]))
    for i in range(5):
        run_turn(conn, client, f"m{i}", brains=_brains(g))
    client.review_memory.assert_not_called()


def test_review_failure_changes_nothing(conn, monkeypatch):
    set_profile(conn, "doc", "seed", "seed")
    _route(monkeypatch)
    client = Mock()
    client.review_memory.return_value = None
    g = _Brain(BrainResult("ok", "gemini"))
    replies = [run_turn(conn, client, f"m{i}", brains=_brains(g)) for i in range(5)]
    assert get_profile(conn) == "doc" and "📝" not in replies[-1]


def test_tool_notes_are_added_under_the_reply_and_stored(conn, monkeypatch):
    from brain.memory import get_recent_messages

    _route(monkeypatch)
    g = _Brain(BrainResult("Got it, sir.", "gemini", tools_ran=["update_profile"],
                           side_effect_tools_ran=["update_profile"]), note="boxing 06:00 → boxing 07:00")
    reply = run_turn(conn, Mock(), "I box at 7 now", brains=_brains(g))
    assert reply.startswith("Got it, sir.\n📝 Profile: boxing 06:00 → boxing 07:00\n— gemini")
    assert get_recent_messages(conn)[-1]["content"].endswith("📝 Profile: boxing 06:00 → boxing 07:00")


def test_review_only_reads_the_owners_own_messages(conn, monkeypatch):
    from brain.memory import add_message

    _route(monkeypatch)
    add_message(conn, "assistant", "That page says Alex loves pineapple pizza.")
    add_message(conn, "user", "[queued task] note that I hate coffee")
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    run_turn(conn, client, "remember that I like tea", brains=_brains(_Brain(BrainResult("ok", "gemini"))))
    sent = client.review_memory.call_args.args[2]
    assert all(m["role"] == "user" for m in sent)
    assert [m["content"] for m in sent] == ["remember that I like tea"]
