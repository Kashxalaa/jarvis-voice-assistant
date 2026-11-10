import logging
from datetime import datetime, timezone

import psycopg

from brain.gemini_client import GeminiConfigError, GeminiUnavailableError
from brain.memory import (
    bump_note_usage,
    get_consolidation_state,
    get_messages_to_consolidate,
    insert_note,
    search_notes_by_embedding,
    update_consolidation_state,
)

logger = logging.getLogger(__name__)

NOTES_KEEP_RECENT_MESSAGES = 20
CONSOLIDATION_BATCH_SIZE = 50
CONSOLIDATION_INTERVAL_SECONDS = 24 * 60 * 60
MAX_BATCHES_PER_RUN = 10


class NoteCreationError(Exception):
    pass


def create_note(
    conn, client, title: str, content: str, tags: str, importance: int, source: str = "explicit"
) -> int:
    try:
        embedding = client.embed_text(f"{title}\n{content}")
    except (GeminiUnavailableError, GeminiConfigError) as exc:
        logger.warning("create_note embedding failed: %s", exc)
        raise NoteCreationError(str(exc)) from exc
    try:
        return insert_note(
            conn, title, content, tags, importance, embedding, source,
            datetime.now(timezone.utc).isoformat(),
        )
    except psycopg.Error as exc:
        logger.warning("create_note database write failed: %s", exc)
        raise NoteCreationError(str(exc)) from exc


def search_notes(conn, client, query: str, limit: int = 3) -> list[dict]:
    try:
        embedding = client.embed_text(query)
    except (GeminiUnavailableError, GeminiConfigError) as exc:
        logger.warning("search_memory embedding failed: %s", exc)
        return []
    try:
        results = search_notes_by_embedding(conn, embedding, limit=limit)
    except psycopg.Error as exc:
        logger.warning("search_memory database query failed: %s", exc)
        return []
    now = datetime.now(timezone.utc).isoformat()
    for note in results:
        bump_note_usage(conn, note["id"], now)
        note["use_count"] += 1
    return results


def _consolidate_one_batch(conn, client) -> tuple[int, bool]:
    """Returns (notes_created, has_more_to_do)."""
    state = get_consolidation_state(conn)
    messages = get_messages_to_consolidate(
        conn,
        after_id=state["last_consolidated_message_id"],
        keep_recent=NOTES_KEEP_RECENT_MESSAGES,
        batch_size=CONSOLIDATION_BATCH_SIZE,
    )
    if not messages:
        return 0, False

    candidates = client.summarize_for_notes(messages)
    if candidates is None:
        # Summarization itself failed (Gemini unavailable, unusable
        # response) — leave the pointer where it is so this batch gets
        # retried next cycle instead of being silently skipped forever.
        # Don't keep trying further batches this run either.
        return 0, False

    created = 0
    for candidate in candidates:
        try:
            create_note(
                conn, client, candidate["title"], candidate["content"],
                candidate["tags"], candidate["importance"], source="consolidation",
            )
            created += 1
        except NoteCreationError as exc:
            logger.warning("failed to create a note during consolidation: %s", exc)

    if candidates and created != len(candidates):
        # At least one note failed to save — don't advance past this
        # batch; retry it next cycle rather than losing the candidates
        # that didn't make it. Don't attempt further batches either.
        return created, False

    update_consolidation_state(conn, messages[-1]["id"])
    # There may be more eligible messages beyond this one batch — a full
    # batch is a sign there's likely more backlog to drain right now.
    return created, len(messages) == CONSOLIDATION_BATCH_SIZE


def consolidate_old_messages(conn, client) -> int:
    total_created = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        created, has_more = _consolidate_one_batch(conn, client)
        total_created += created
        if not has_more:
            break
    return total_created


SEARCH_MEMORY_TOOL = {
    "name": "search_memory",
    "description": (
        "Search Jarvis's long-term notes for anything relevant to what the "
        "user is asking about right now, when it might relate to something "
        "from earlier in your history together that isn't shown in the "
        "recent conversation above. Uses meaning-based search, not exact "
        "keyword matching — describe what you're looking for in plain "
        "language."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, in plain language",
            },
        },
        "required": ["query"],
    },
}

CREATE_NOTE_TOOL = {
    "name": "create_note",
    "description": (
        "Save a richer, longer-term memory as a note — for anything more "
        "than a single simple fact (a decision, an explanation, context "
        "spanning multiple sentences), especially when the user explicitly "
        "asks to remember something 'as a note' or similar. Do NOT use "
        "this for a single simple fact like a preference or an ID — those "
        "are captured automatically elsewhere; this tool is only for "
        "richer, note-worthy memories."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short title for the note"},
            "content": {
                "type": "string",
                "description": "The note's content, 1-3 sentences",
            },
            "tags": {
                "type": "string",
                "description": "Comma-separated keywords, optional",
            },
            "importance": {
                "type": "integer",
                "description": (
                    "How important this is to remember long-term, 1 "
                    "(trivial) to 10 (critical). Default to 5 if unsure."
                ),
            },
        },
        "required": ["title", "content"],
    },
}
