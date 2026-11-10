"""Every brain reads the owner's profile (not the old facts list)."""
from unittest.mock import Mock

import psycopg

from brain.brains import BrainResult, TurnContext
from brain.brains.claude import ClaudeBrain
from brain.brains.groq import GroqBrain
from brain.conversation import Brains, run_turn
from brain.gemini_client import _build_conversation_prompt
from brain.profile import set_profile
from brain.router import RouteDecision


def _ctx(profile):
    return TurnContext(conn=Mock(), system_prompt="SYS", profile=profile, history=[], message="hi",
                       image_bytes=None, image_mime_type=None, tools=[], executor=Mock())


def test_conversation_prompt_includes_the_profile():
    p = _build_conversation_prompt("SYS", "# About Alex\n- Lives in Lisbon", [], "hi")
    assert "About the owner (their profile):\n# About Alex\n- Lives in Lisbon" in p
    assert "Known facts" not in p


def test_conversation_prompt_without_a_profile():
    assert "About the owner (their profile):\n(no profile yet)" in _build_conversation_prompt("SYS", "", [], "hi")


def test_claude_prompt_includes_the_profile():
    text = ClaudeBrain._build_prompt(ClaudeBrain.__new__(ClaudeBrain), _ctx("- Lives in Lisbon"))
    assert "About the owner (their profile):\n- Lives in Lisbon" in text


def test_groq_prompt_includes_the_profile(monkeypatch):
    sent = {}

    def fake_chat(api_key, model, messages, json_mode, timeout):
        sent["prompt"] = messages[0]["content"]
        return "hello"

    monkeypatch.setattr("brain.brains.groq.groq_chat", fake_chat)
    GroqBrain("k", "m").respond(_ctx("- Lives in Lisbon"))
    assert "About the owner (their profile):\n- Lives in Lisbon" in sent["prompt"]


class _Brain:
    name = "gemini"

    def __init__(self):
        self.last_ctx = None

    def respond(self, ctx):
        self.last_ctx = ctx
        return BrainResult("ok", "gemini")


def _route_normal(monkeypatch):
    import brain.conversation as conversation

    monkeypatch.setattr(conversation, "route",
                        lambda message, *a, **k: RouteDecision("normal", "judge", message, None))


def _brains(g):
    return Brains(gemini=g, claude=None, groq=None, groq_api_key="k", groq_model="m")


def test_turn_passes_the_profile_to_the_brain(conn, monkeypatch):
    set_profile(conn, "- Lives in Lisbon", "seed", "seed")
    _route_normal(monkeypatch)
    g = _Brain()
    run_turn(conn, Mock(), "hi", brains=_brains(g))
    assert g.last_ctx.profile == "- Lives in Lisbon"


def test_profile_read_failure_does_not_break_the_turn(conn, monkeypatch):
    import brain.conversation as conversation

    def boom(conn):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(conversation, "get_profile", boom)
    _route_normal(monkeypatch)
    g = _Brain()
    assert run_turn(conn, Mock(), "hi", brains=_brains(g)).startswith("ok")
    assert g.last_ctx.profile == ""
