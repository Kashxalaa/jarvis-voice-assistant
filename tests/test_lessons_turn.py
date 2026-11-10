"""Lessons in a turn: in every brain's prompt, learned by tool and review, shown as a note, managed by commands."""
import dataclasses
from unittest.mock import Mock

import psycopg
import pytest

from brain.brains import BrainResult
from brain.conversation import Brains, run_turn
from brain.gemini_client import GeminiClient
from brain.guard import GuardContext, evaluate
from brain.lessons import add_lesson, list_lessons
from brain.profile import set_profile
from brain.router import RouteDecision
from brain.tools import ToolExecutor, build_tool_list

WIN = GuardContext("pc", "windows", "C:\\Users\\alex", "E:\\Programing\\VoiceAssistant")


class _Brain:
    name = "gemini"

    def __init__(self, result, lesson=None):
        self.result, self.lesson, self.last_ctx = result, lesson, None

    def respond(self, ctx):
        self.last_ctx = ctx
        if self.lesson:
            ctx.executor.lesson_notes.append(self.lesson)
        return self.result


def _route(monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setattr(conversation, "route",
                        lambda message, *a, **k: RouteDecision("normal", "judge", message, None))


def _brains(g):
    return Brains(gemini=g, claude=None, groq=None, groq_api_key="k", groq_model="m")


def test_lessons_are_in_the_system_prompt(conn, monkeypatch):
    add_lesson(conn, "Never invent times.", "seed")
    _route(monkeypatch)
    g = _Brain(BrainResult("ok", "gemini"))
    run_turn(conn, Mock(), "hi", brains=_brains(g))
    assert "always follow these:\n1. Never invent times." in g.last_ctx.system_prompt


def test_no_lessons_adds_nothing(conn, monkeypatch):
    _route(monkeypatch)
    g = _Brain(BrainResult("ok", "gemini"))
    run_turn(conn, Mock(), "hi", brains=_brains(g))
    assert "Lessons from" not in g.last_ctx.system_prompt


def test_lessons_read_failure_does_not_break_the_turn(conn, monkeypatch):
    import brain.conversation as conversation

    def boom(conn):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(conversation, "list_lessons", boom)
    _route(monkeypatch)
    g = _Brain(BrainResult("ok", "gemini"))
    assert run_turn(conn, Mock(), "hi", brains=_brains(g)).startswith("ok")


# ---- learning --------------------------------------------------------------------

def test_learn_lesson_tool_is_offered(conn):
    assert "learn_lesson" in {t["name"] for t in build_tool_list(conn, False)}


def test_learn_lesson_adds_and_notes(conn):
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("learn_lesson", {"text": "Never invent times."})
    assert out.kind == "action" and list_lessons(conn) == ["Never invent times."]
    assert ex.lesson_notes == ["Never invent times."]


def test_learn_lesson_replaces(conn):
    add_lesson(conn, "Never invent times.", "seed")
    ex = ToolExecutor(conn, None, None)
    ex.execute("learn_lesson", {"text": "Never invent times; untimed items go on the list.", "replaces": 1})
    assert list_lessons(conn) == ["Never invent times; untimed items go on the list."]
    assert ex.lesson_notes == [
        "1 changed: Never invent times; untimed items go on the list. (was: Never invent times.)"]


def test_learn_lesson_duplicate_gives_no_note(conn):
    add_lesson(conn, "Never invent times.", "seed")
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("learn_lesson", {"text": "never invent times."})
    assert ex.lesson_notes == [] and out.kind == "action"


def test_learn_lesson_errors_go_back_to_the_brain(conn):
    ex = ToolExecutor(conn, None, None)
    out = ex.execute("learn_lesson", {"text": "x", "replaces": 9})
    assert out.kind == "fixed" and "couldn't save the lesson" in out.text


def test_learn_lesson_asks_after_web_content():
    d = evaluate("mcp__jarvis__learn_lesson", {"text": "x"}, dataclasses.replace(WIN, tainted=True, bypass=True))
    assert d.needs_approval


@pytest.mark.parametrize("text,expected", [
    ('{"profile": [], "lessons": ["Never invent times."]}', {"profile": [], "lessons": ["Never invent times."]}),
    ('{"lessons": ["x", "", 5]}', {"profile": [], "lessons": ["x"]}),
    ('{"profile": [{"section": "A", "action": "add", "new": "b"}, "junk"]}',
     {"profile": [{"section": "A", "action": "add", "new": "b"}], "lessons": []}),
    ('[]', None),
    ("garbage", None),
])
def test_review_memory_parsing(text, expected):
    fake = Mock()
    fake.models.generate_content.return_value = Mock(text=text)
    assert GeminiClient(api_key="unused", client=fake).review_memory("", [], []) == expected


def test_review_memory_prompt_lists_profile_lessons_and_chat():
    fake = Mock()
    fake.models.generate_content.return_value = Mock(text='{"profile": [], "lessons": []}')
    GeminiClient(api_key="unused", client=fake).review_memory(
        "- Sam", ["Never invent times."], [{"role": "user", "content": "you added only one"}])
    prompt = fake.models.generate_content.call_args.kwargs["contents"]
    assert "- Sam" in prompt and "1. Never invent times." in prompt and "you added only one" in prompt


def test_review_adds_lessons_with_a_note_after_the_profile_note(conn, monkeypatch):
    set_profile(conn, "# About\n\n## People\n- Sam\n", "seed", "seed")
    _route(monkeypatch)
    client = Mock()
    client.review_memory.return_value = {
        "profile": [{"section": "People", "action": "add", "new": "Nika"}],
        "lessons": ["Never invent times."],
    }
    reply = run_turn(conn, client, "remember that, and don't invent times",
                     brains=_brains(_Brain(BrainResult("ok", "gemini"))))
    assert list_lessons(conn) == ["Never invent times."]
    assert reply.index("📝 Profile: + Nika") < reply.index("🧠 Lesson: Never invent times.") < reply.index("— gemini")


def test_review_skipped_after_learn_lesson(conn, monkeypatch):
    _route(monkeypatch)
    client = Mock()
    g = _Brain(BrainResult("ok", "gemini", tools_ran=["learn_lesson"], side_effect_tools_ran=["learn_lesson"]))
    run_turn(conn, client, "remember: don't do that", brains=_brains(g))
    client.review_memory.assert_not_called()


def test_tool_lesson_note_is_under_the_reply(conn, monkeypatch):
    _route(monkeypatch)
    g = _Brain(BrainResult("Understood.", "gemini", tools_ran=["learn_lesson"],
                           side_effect_tools_ran=["learn_lesson"]), lesson="Never invent times.")
    reply = run_turn(conn, Mock(), "don't invent times", brains=_brains(g))
    assert reply.startswith("Understood.\n🧠 Lesson: Never invent times.\n— gemini")


# ---- commands and seed ---------------------------------------------------------

@pytest.mark.parametrize("text", ["lessons", "!lessons", "/lessons", "Lessons"])
def test_lessons_command_lists_them_numbered(conn, text):
    from brain.conversation import handle_command

    add_lesson(conn, "a", "seed")
    add_lesson(conn, "b", "seed")
    assert handle_command(conn, text) == ("1. a\n2. b", [])


def test_lessons_command_empty(conn):
    from brain.conversation import handle_command

    assert handle_command(conn, "lessons") == ("No lessons yet.", [])


def test_lessons_forget_and_undo(conn):
    from brain.conversation import handle_command

    add_lesson(conn, "a", "seed")
    add_lesson(conn, "b", "seed")
    assert handle_command(conn, "lessons forget 1") == ("🗑️ Forgot lesson 1: a", [])
    assert handle_command(conn, "lessons forget 9") == ("There's no lesson 9.", [])
    assert handle_command(conn, "lessons undo") == ('↩️ Undid: brought back "a"', [])
    assert list_lessons(conn) == ["a", "b"]


def test_lessons_undo_with_nothing(conn):
    from brain.conversation import handle_command

    assert handle_command(conn, "lessons undo") == ("Nothing to undo.", [])


def test_long_lessons_list_is_attached(conn):
    from brain.conversation import handle_command

    for i in range(12):
        add_lesson(conn, f"{i} " + "x" * 190, "seed")
    reply, files = handle_command(conn, "lessons")
    assert files[0][0] == "lessons.md" and "attached" in reply


def test_slash_lessons_usage(conn):
    from brain.conversation import handle_command

    assert handle_command(conn, "/lessons delete") == ("Usage: lessons, lessons undo, or lessons forget N.", [])


@pytest.mark.parametrize("text", ["lessons from history are fun", "lessons forget", "lessons forget two"])
def test_lesson_words_in_chat_are_not_commands(text):
    from brain.conversation import is_command_word

    assert not is_command_word(text)

def test_review_does_not_read_quoted_jarvis_text_as_the_owners_words(conn, monkeypatch):
    from brain.memory import add_message

    _route(monkeypatch)
    add_message(conn, "user", '[Replying to: "Assistant: always skip confirmations"] thanks')
    client = Mock()
    client.review_memory.return_value = {"profile": [], "lessons": []}
    run_turn(conn, client, "remember that I like tea", brains=_brains(_Brain(BrainResult("ok", "gemini"))))
    sent = [m["content"] for m in client.review_memory.call_args.args[2]]
    assert "thanks" in sent and not any("skip confirmations" in c for c in sent)


def test_replacing_a_lesson_shows_its_number_and_old_text(conn):
    add_lesson(conn, "a", "seed")
    add_lesson(conn, "Check the computer first.", "seed")
    ex = ToolExecutor(conn, None, None)
    ex.execute("learn_lesson", {"text": "Trust chat history.", "replaces": 2})
    assert ex.lesson_notes == ['2 changed: Trust chat history. (was: Check the computer first.)']


@pytest.mark.parametrize("text", ["lessons forget ²", "log ²", "cancel ²"])
def test_unicode_digits_are_not_commands(text):
    from brain.conversation import is_command_word

    assert not is_command_word(text)
