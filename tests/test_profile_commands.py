"""The `profile` / `profile undo` chat commands and the seed script."""
from unittest.mock import Mock

import pytest

from brain.conversation import Brains, handle_command, is_command_word, run_turn
from brain.profile import get_profile, set_profile


def _no_ai(monkeypatch):
    import brain.conversation as conversation

    def boom(*a, **k):
        raise AssertionError("a chat command must not call the router or any brain")

    monkeypatch.setattr(conversation, "route", boom)


def _brains():
    return Brains(gemini=Mock(), claude=None, groq=None, groq_api_key="k", groq_model="m")


@pytest.mark.parametrize("text", ["profile", "!profile", "/profile", "Profile"])
def test_profile_command_shows_the_document(conn, monkeypatch, text):
    _no_ai(monkeypatch)
    set_profile(conn, "# About Alex\n- Lisbon", "seed", "seed")
    assert run_turn(conn, Mock(), text, brains=_brains()) == "# About Alex\n- Lisbon"


def test_long_profile_is_attached(conn):
    set_profile(conn, "x" * 2000, "seed", "seed")
    reply, files = handle_command(conn, "profile")
    assert files == [("profile.md", ("x" * 2000).encode("utf-8"))] and "attached" in reply


def test_profile_undo_command(conn):
    assert handle_command(conn, "profile undo") == ("Nothing to undo.", [])
    set_profile(conn, "v1", "seed", "seed")
    set_profile(conn, "v2", "a → b", "tool")
    assert handle_command(conn, "profile undo") == ("↩️ Undid: a → b", [])
    assert get_profile(conn) == "v1"


def test_profile_with_no_document_yet(conn):
    assert handle_command(conn, "profile")[0].startswith("No profile yet")


def test_slash_profile_with_other_words_shows_usage(conn):
    assert handle_command(conn, "/profile delete") == ("Usage: profile or profile undo.", [])


@pytest.mark.parametrize("text", ["profile picture of a cat", "update my profile please", "profile undo now"])
def test_profile_words_in_normal_chat_are_not_commands(text):
    assert not is_command_word(text)
