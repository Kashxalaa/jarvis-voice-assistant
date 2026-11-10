"""The owner's About-me document: storage, history/undo and line-level edits."""
from datetime import datetime, timezone

PROFILE_MAX_CHARS = 4000
HISTORY_KEEP = 50
_NOTE_MAX = 3


class ProfileEditError(Exception):
    """The whole edit set was rejected (e.g. the profile would get too long)."""


def get_profile(conn) -> str:
    row = conn.execute("SELECT content FROM profile WHERE id = 1").fetchone()
    return row[0] if row else ""


def set_profile(conn, content: str, summary: str, source: str) -> None:
    """Save the current version to history, then replace it."""
    old = conn.execute("SELECT content FROM profile WHERE id = 1").fetchone()
    if old is not None:
        conn.execute(
            "INSERT INTO profile_history (content, summary, source) VALUES (%s, %s, %s)",
            (old[0], summary, source),
        )
        conn.execute(
            "DELETE FROM profile_history WHERE id NOT IN "
            "(SELECT id FROM profile_history ORDER BY id DESC LIMIT %s)",
            (HISTORY_KEEP,),
        )
    conn.execute(
        "INSERT INTO profile (id, content, updated_at) VALUES (1, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at",
        (content, datetime.now(timezone.utc)),
    )


def undo_last(conn) -> str | None:
    """Restore the newest saved version. Undo is itself a change, so undo twice = redo."""
    row = conn.execute(
        "SELECT id, content, summary FROM profile_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    hist_id, content, summary = row
    conn.execute("DELETE FROM profile_history WHERE id = %s", (hist_id,))
    set_profile(conn, content, f"undo of: {summary}", "undo")
    return summary or "the last change"


def _sections(lines: list[str]) -> list[tuple[str, int, int]]:
    """(heading text, first body line index, end index) for each '## ' section."""
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    out = []
    for n, i in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        out.append((lines[i][3:].strip(), i + 1, end))
    return out


def _bullet(text: str) -> str:
    # One line only: line breaks inside an edit must never create headings or bullets.
    return " ".join(text.split()).removeprefix("- ").strip()


def apply_edits(content: str, edits) -> tuple[str, list[str], list[str]]:
    """Apply line edits; returns (new text, one summary per applied edit, skip reasons).
    An edit that can't be applied exactly is skipped, never guessed."""
    lines = content.split("\n")
    summaries: list[str] = []
    skipped: list[str] = []
    for edit in edits if isinstance(edits, list) else []:
        if not isinstance(edit, dict):
            skipped.append("an edit wasn't an object")
            continue
        section, action = edit.get("section"), edit.get("action")
        old, new = edit.get("old"), edit.get("new")
        if not isinstance(section, str) or action not in ("add", "replace", "remove"):
            skipped.append(f"bad edit {edit!r}")
            continue
        section = " ".join(section.split()).lstrip("#").strip()  # "## People" -> "People"
        if not section:
            skipped.append("an edit has no section")
            continue
        match = [s for s in _sections(lines) if s[0].lower() == section.lower()]
        if action == "add":
            if not isinstance(new, str) or not new.strip():
                skipped.append("add needs 'new'")
                continue
            line = f"- {_bullet(new)}"
            if match:
                _, start, end = match[0]
                if any(lines[i].lower() == line.lower() for i in range(start, end)):
                    skipped.append(f"{_bullet(new)!r} is already in {section!r}")
                    continue
                insert_at = end
                while insert_at > start and not lines[insert_at - 1].strip():
                    insert_at -= 1
                lines.insert(insert_at, line)
            else:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += ["", f"## {section.strip()}", line, ""]
            summaries.append(f"+ {_bullet(new)}")
            continue
        if not match:
            skipped.append(f"section {section!r} not found")
            continue
        if not isinstance(old, str) or not old.strip():
            skipped.append(f"{action} needs 'old'")
            continue
        _, start, end = match[0]
        hits = [i for i in range(start, end) if lines[i].startswith("- ") and old.lower() in lines[i].lower()]
        if not hits:
            skipped.append(f"no line with {old!r} in {section!r}")
            continue
        if len(hits) > 1:
            skipped.append(f"{old!r} matches more than one line in {section!r}")
            continue
        before = _bullet(lines[hits[0]])
        if action == "remove":
            del lines[hits[0]]
            summaries.append(f"− {before}")
        else:
            if not isinstance(new, str) or not new.strip():
                skipped.append("replace needs 'new'")
                continue
            lines[hits[0]] = f"- {_bullet(new)}"
            summaries.append(f"{before} → {_bullet(new)}")
    result = "\n".join(lines)
    if len(result) > PROFILE_MAX_CHARS:
        raise ProfileEditError(f"the profile would be longer than {PROFILE_MAX_CHARS} characters")
    return result, summaries, skipped


NOTE_ITEM_CHARS = 80


def shorten(text: str, limit: int = NOTE_ITEM_CHARS) -> str:
    """One note item, cut to `limit` characters with an ellipsis (the full text is in `profile`)."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_note(summaries: list[str]) -> str:
    if not summaries:
        return ""
    shown = "; ".join(shorten(s) for s in summaries[:_NOTE_MAX])
    more = len(summaries) - _NOTE_MAX
    return f"📝 Profile: {shown}" + (f"; +{more} more" if more > 0 else "")


UPDATE_PROFILE_TOOL = {
    "name": "update_profile",
    "description": (
        "Update the owner's About-me profile (shown to you above). Call it when the owner states "
        "something lasting about themselves - goals, routines, projects, preferences, people, setup - "
        "or asks to change or remove something in their profile. Never for one-off moods or events "
        "('I'm tired today'), never invent anything. Edit only the lines that changed; one call can "
        "carry several edits. Use the section names exactly as they appear in the profile."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {"type": "string", "description": "Section heading, e.g. 'Typical week'"},
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "old": {"type": "string", "description": "replace/remove: words from the existing line"},
                        "new": {"type": "string", "description": "add/replace: the full new line"},
                    },
                    "required": ["section", "action"],
                },
            }
        },
        "required": ["edits"],
    },
}
