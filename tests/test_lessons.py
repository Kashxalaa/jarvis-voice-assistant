import pytest

from brain.lessons import (
    MAX_LESSONS, LessonError, add_lesson, forget_lesson, format_lesson_note, lessons_prompt,
    list_lessons, replace_lesson, undo_last_lesson_change,
)


def test_add_and_list_in_order(conn):
    assert add_lesson(conn, "Never invent times.", "tool") == "Never invent times."
    add_lesson(conn, "Use real paths.", "tool")
    assert list_lessons(conn) == ["Never invent times.", "Use real paths."]


def test_duplicates_are_skipped(conn):
    add_lesson(conn, "Never invent times.", "tool")
    assert add_lesson(conn, "  never   INVENT times. ", "review") is None
    assert len(list_lessons(conn)) == 1


def test_text_is_one_line_and_clamped(conn):
    text = add_lesson(conn, "a\nb  c" + "x" * 300, "tool")
    assert "\n" not in text and len(text) == 200 and text.startswith("a b c")


def test_empty_text_is_refused(conn):
    with pytest.raises(LessonError):
        add_lesson(conn, "   ", "tool")


def test_cap(conn):
    for i in range(MAX_LESSONS):
        add_lesson(conn, f"rule {i}", "seed")
    with pytest.raises(LessonError, match="full"):
        add_lesson(conn, "one more", "tool")


def test_forget_uses_the_shown_numbering(conn):
    for t in ("a", "b", "c"):
        add_lesson(conn, t, "seed")
    assert forget_lesson(conn, 1) == "a"
    assert forget_lesson(conn, 2) == "c"      # shown list was [b, c]
    assert list_lessons(conn) == ["b"]
    with pytest.raises(LessonError):
        forget_lesson(conn, 5)


def test_replace(conn):
    add_lesson(conn, "old rule", "seed")
    assert replace_lesson(conn, 1, "new rule") == ("old rule", "new rule")
    assert list_lessons(conn) == ["new rule"]


def test_undo_reverses_add_forget_and_replace(conn):
    add_lesson(conn, "a", "seed")
    add_lesson(conn, "b", "tool")
    replace_lesson(conn, 1, "a2")
    forget_lesson(conn, 2)
    assert list_lessons(conn) == ["a2"]
    assert undo_last_lesson_change(conn) == 'brought back "b"'
    assert list_lessons(conn) == ["a2", "b"]
    assert undo_last_lesson_change(conn) == 'changed back to "a"'
    assert list_lessons(conn) == ["a", "b"]
    assert undo_last_lesson_change(conn) == 'removed "b"'
    assert list_lessons(conn) == ["a"]


def test_undo_with_nothing_to_undo(conn):
    assert undo_last_lesson_change(conn) is None


def test_undo_log_keeps_the_last_50(conn):
    for i in range(30):
        add_lesson(conn, f"r{i}", "seed")
        forget_lesson(conn, 1)
    assert conn.execute("SELECT count(*) FROM lesson_changes").fetchone()[0] == 50


def test_prompt_and_note():
    assert lessons_prompt([]) == ""
    assert lessons_prompt(["a", "b"]) == (
        "Lessons from the owner's past corrections — always follow these:\n1. a\n2. b")
    assert format_lesson_note([]) == ""
    assert format_lesson_note(["a", "b", "c", "d"]) == "🧠 Lesson: a; b; c; +1 more"
