"""Lessons: short behaviour rules Jarvis learned from the owner's corrections."""

MAX_LESSONS = 40
MAX_LESSON_CHARS = 200
CHANGES_KEEP = 50
_NOTE_MAX = 3


class LessonError(Exception):
    """A lesson change that can't be made (full list, unknown number, empty text)."""


def _clean(text) -> str:
    return " ".join(str(text or "").split())[:MAX_LESSON_CHARS]


def _active(conn) -> list[tuple[int, str]]:
    return conn.execute("SELECT id, text FROM lessons WHERE active ORDER BY id").fetchall()


def list_lessons(conn) -> list[str]:
    return [text for _, text in _active(conn)]


def _log(conn, kind: str, lesson_id: int, old_text: str | None = None) -> None:
    conn.execute(
        "INSERT INTO lesson_changes (kind, lesson_id, old_text) VALUES (%s, %s, %s)",
        (kind, lesson_id, old_text),
    )
    conn.execute(
        "DELETE FROM lesson_changes WHERE id NOT IN "
        "(SELECT id FROM lesson_changes ORDER BY id DESC LIMIT %s)",
        (CHANGES_KEEP,),
    )


def _by_number(conn, number) -> tuple[int, str]:
    """The lesson the owner sees as number N (1-based, active lessons in order)."""
    rows = _active(conn)
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= len(rows):
        raise LessonError(f"there's no lesson {number}")
    return rows[number - 1]


def add_lesson(conn, text, source: str) -> str | None:
    """The cleaned text, or None when the same lesson is already active."""
    clean = _clean(text)
    if not clean:
        raise LessonError("the lesson is empty")
    rows = _active(conn)
    if any(t.lower() == clean.lower() for _, t in rows):
        return None
    if len(rows) >= MAX_LESSONS:
        raise LessonError(f"Lessons are full ({MAX_LESSONS}) — remove one with 'lessons forget N'")
    lesson_id = conn.execute(
        "INSERT INTO lessons (text, source) VALUES (%s, %s) RETURNING id", (clean, source)
    ).fetchone()[0]
    _log(conn, "add", lesson_id)
    return clean


def replace_lesson(conn, number, text) -> tuple[str, str]:
    clean = _clean(text)
    if not clean:
        raise LessonError("the lesson is empty")
    lesson_id, old = _by_number(conn, number)
    conn.execute("UPDATE lessons SET text = %s WHERE id = %s", (clean, lesson_id))
    _log(conn, "replace", lesson_id, old)
    return old, clean


def forget_lesson(conn, number) -> str:
    lesson_id, text = _by_number(conn, number)
    conn.execute("UPDATE lessons SET active = FALSE WHERE id = %s", (lesson_id,))
    _log(conn, "remove", lesson_id)
    return text


def undo_last_lesson_change(conn) -> str | None:
    """Reverse the newest change; undo again to reverse the one before."""
    row = conn.execute(
        "SELECT id, kind, lesson_id, old_text FROM lesson_changes ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    change_id, kind, lesson_id, old_text = row
    text = conn.execute("SELECT text FROM lessons WHERE id = %s", (lesson_id,)).fetchone()[0]
    if kind == "add":
        conn.execute("UPDATE lessons SET active = FALSE WHERE id = %s", (lesson_id,))
        summary = f'removed "{text}"'
    elif kind == "remove":
        conn.execute("UPDATE lessons SET active = TRUE WHERE id = %s", (lesson_id,))
        summary = f'brought back "{text}"'
    else:
        conn.execute("UPDATE lessons SET text = %s WHERE id = %s", (old_text, lesson_id))
        summary = f'changed back to "{old_text}"'
    conn.execute("DELETE FROM lesson_changes WHERE id = %s", (change_id,))
    return summary


def lessons_prompt(texts: list[str]) -> str:
    if not texts:
        return ""
    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
    return f"Lessons from the owner's past corrections — always follow these:\n{lines}"


def format_lesson_note(texts: list[str]) -> str:
    if not texts:
        return ""
    from brain.profile import shorten

    more = len(texts) - _NOTE_MAX
    shown = "; ".join(shorten(t) for t in texts[:_NOTE_MAX])
    return "🧠 Lesson: " + shown + (f"; +{more} more" if more > 0 else "")


LEARN_LESSON_TOOL = {
    "name": "learn_lesson",
    "description": (
        "Save a lesson: a short rule about how YOU (Jarvis) should behave, learned when the owner "
        "corrects you or tells you how to act in future (e.g. 'Never invent a time for a reminder'). "
        "Never for facts about the owner - those go to update_profile. One short imperative sentence. "
        "If one of the lessons in your instructions already covers it, pass its number as 'replaces' "
        "to refine it instead of adding a near-copy."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The rule, one sentence"},
            "replaces": {"type": "integer", "description": "Number of the lesson this refines"},
        },
        "required": ["text"],
    },
}
