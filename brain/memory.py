import logging
from datetime import datetime, timezone

import psycopg

logger = logging.getLogger(__name__)


def init_db(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS actions (
            name TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_proposal (
            kind TEXT NOT NULL,
            suggested_name TEXT NOT NULL,
            suggested_value TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            created_at TEXT NOT NULL,
            turns_remaining INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_heartbeat (
            id INTEGER PRIMARY KEY,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            text TEXT NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            rrule TEXT,
            next_fire_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'active',
            last_done_on DATE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Added for categories + linked "day before" reminders. ADD COLUMN IF NOT
    # EXISTS with a constant default is a metadata-only change on Postgres 11+
    # (no table rewrite), so this is safe to run against the live production
    # DB, which already has rows.
    conn.execute(
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'other'"
    )
    conn.execute("ALTER TABLE reminders ADD COLUMN IF NOT EXISTS parent_id INTEGER")
    conn.execute(
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS lead_days INTEGER NOT NULL DEFAULT 0"
    )
    # Added for untimed ("no time") reminders. Existing rows all default to
    # TRUE (timed), so nothing about the 98 real production reminders
    # changes behavior.
    conn.execute(
        "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS timed BOOLEAN NOT NULL DEFAULT TRUE"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            text TEXT NOT NULL,
            due_date DATE NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            done_at TIMESTAMPTZ
        )
        """
    )
    conn.execute(
        "ALTER TABLE todos ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'task'"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_list (
            id INTEGER PRIMARY KEY,
            list_date DATE,
            message_id BIGINT,
            needs_refresh BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    # Added for the whole-day weather line on the pinned list: stored once
    # when a new day's list is first posted, then reused on later
    # same-day refreshes instead of being re-fetched. Additive, metadata-only
    # on Postgres 11+ — safe against the live production row.
    conn.execute("ALTER TABLE daily_list ADD COLUMN IF NOT EXISTS weather_line TEXT")
    # Tracks each reminder ping message so ticking an item on the list (or
    # via chat) can find and re-render that day's already-posted ping(s).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_pings (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            reminder_id INTEGER NOT NULL,
            message_id BIGINT NOT NULL,
            fired_on DATE NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            needs_render BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_pings_reminder_fired_on "
        "ON reminder_pings (reminder_id, fired_on)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminder_pings_needs_render "
        "ON reminder_pings (needs_render) WHERE needs_render"
    )
    # Re-pings: each unanswered ping is followed by up to 3 more, one per
    # hour; ping_number counts the chain and superseded marks a ping that
    # has been replaced by the next one. Additive with defaults, so every
    # existing row becomes "ping 1, not superseded".
    conn.execute(
        "ALTER TABLE reminder_pings ADD COLUMN IF NOT EXISTS ping_number INTEGER NOT NULL DEFAULT 1"
    )
    conn.execute(
        "ALTER TABLE reminder_pings ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Last local day the evening "still on your list" nudge was handled.
    conn.execute("ALTER TABLE daily_list ADD COLUMN IF NOT EXISTS nudge_date DATE")
    # Computer access (A2): the /bypass window, tasks queued while the PC is
    # off, and the audit log of what Claude did on a machine.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS computer_state (
            id INTEGER PRIMARY KEY,
            bypass_until TIMESTAMPTZ,
            bypass_warned BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pc_tasks (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            text TEXT NOT NULL,
            requested_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            note TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at TIMESTAMPTZ NOT NULL DEFAULT now(),
            machine TEXT,
            task_id TEXT,
            tool TEXT,
            detail TEXT,
            verdict TEXT,
            outcome TEXT,
            approver TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_at ON activity_log (at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_history (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            content TEXT NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            summary TEXT,
            source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brain_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_brain TEXT NOT NULL,
            at TIMESTAMPTZ NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            source TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_changes (
            id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind TEXT NOT NULL,
            lesson_id INTEGER NOT NULL,
            old_text TEXT,
            at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL,
                importance INTEGER NOT NULL,
                embedding vector(768) NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS consolidation_state (
                id INTEGER PRIMARY KEY,
                last_consolidated_message_id INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except psycopg.Error as exc:
        logger.warning(
            "pgvector unavailable on this connection — notes feature disabled: %s", exc
        )
    conn.commit()
    return conn


def ensure_connected(conn: psycopg.Connection, database_url: str) -> psycopg.Connection:
    if conn.closed:
        return init_db(database_url)
    try:
        conn.execute("SELECT 1")
        return conn
    except psycopg.OperationalError:
        return init_db(database_url)


def add_message(conn: psycopg.Connection, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO messages (role, content, timestamp) VALUES (%s, %s, %s)",
        (role, content, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_recent_messages(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    rows.reverse()
    return [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in rows]


def upsert_fact(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO facts (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )
    conn.commit()


def get_all_facts(conn: psycopg.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM facts").fetchall()
    return dict(rows)


def record_last_brain(conn: psycopg.Connection, brain: str, at) -> None:
    """Which brain answered the latest message, and when (shared by both instances)."""
    conn.execute(
        "INSERT INTO brain_state (id, last_brain, at) VALUES (1, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET last_brain = EXCLUDED.last_brain, at = EXCLUDED.at",
        (brain, at),
    )


def get_last_brain(conn: psycopg.Connection):
    """(brain, at) of the latest answer, or None."""
    row = conn.execute("SELECT last_brain, at FROM brain_state WHERE id = 1").fetchone()
    return (row[0], row[1]) if row else None


def count_user_messages(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'user'"
    ).fetchone()
    return row[0]


def upsert_action(conn: psycopg.Connection, name: str, kind: str, value: str) -> None:
    conn.execute(
        "INSERT INTO actions (name, kind, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, value = EXCLUDED.value",
        (name, kind, value),
    )
    conn.commit()


def list_actions(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("SELECT name, kind, value FROM actions ORDER BY name").fetchall()
    return [{"name": r[0], "kind": r[1], "value": r[2]} for r in rows]


def get_action(conn: psycopg.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT name, kind, value FROM actions WHERE name = %s", (name,)
    ).fetchone()
    if row is None:
        return None
    return {"name": row[0], "kind": row[1], "value": row[2]}


def set_pending_proposal(
    conn: psycopg.Connection,
    kind: str,
    suggested_name: str,
    suggested_value: str,
    reasoning: str,
    created_at: str,
    turns_remaining: int = 3,
) -> None:
    conn.execute("DELETE FROM pending_proposal")
    conn.execute(
        "INSERT INTO pending_proposal "
        "(kind, suggested_name, suggested_value, reasoning, created_at, turns_remaining) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (kind, suggested_name, suggested_value, reasoning, created_at, turns_remaining),
    )
    conn.commit()


def get_pending_proposal(conn: psycopg.Connection) -> dict | None:
    row = conn.execute(
        "SELECT kind, suggested_name, suggested_value, reasoning, created_at, turns_remaining "
        "FROM pending_proposal LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "kind": row[0],
        "suggested_name": row[1],
        "suggested_value": row[2],
        "reasoning": row[3],
        "created_at": row[4],
        "turns_remaining": row[5],
    }


def clear_pending_proposal(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM pending_proposal")
    conn.commit()


def decrement_pending_proposal_turns(conn: psycopg.Connection) -> None:
    conn.execute("UPDATE pending_proposal SET turns_remaining = turns_remaining - 1")
    conn.commit()


def touch_local_heartbeat(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO local_heartbeat (id, updated_at) VALUES (1, now()::text) "
        "ON CONFLICT (id) DO UPDATE SET updated_at = EXCLUDED.updated_at"
    )
    conn.commit()


def is_local_heartbeat_fresh(conn: psycopg.Connection, max_age_seconds: int = 30) -> bool:
    row = conn.execute(
        "SELECT (now() - updated_at::timestamptz) <= (%s * interval '1 second') "
        "FROM local_heartbeat WHERE id = 1",
        (max_age_seconds,),
    ).fetchone()
    if row is None:
        return False
    return bool(row[0])


def insert_note(
    conn: psycopg.Connection,
    title: str,
    content: str,
    tags: str,
    importance: int,
    embedding: list[float],
    source: str,
    created_at: str,
) -> int:
    row = conn.execute(
        "INSERT INTO notes (title, content, tags, importance, embedding, source, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (title, content, tags, importance, embedding, source, created_at),
    ).fetchone()
    conn.commit()
    return row[0]


# Calibrated empirically against real gemini-embedding-001 vectors:
# genuinely related text scored 0.59-0.71 cosine similarity, unrelated
# text scored 0.39-0.54 — 0.55 sits in the gap between them.
NOTES_MIN_SIMILARITY = 0.55


def search_notes_by_embedding(
    conn: psycopg.Connection, embedding: list[float], limit: int = 3
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, content, tags, importance, use_count, "
        "1 - (embedding <=> %s::vector) AS similarity "
        "FROM notes "
        "WHERE 1 - (embedding <=> %s::vector) >= %s "
        "ORDER BY (1 - (embedding <=> %s::vector)) * 0.6 "
        "+ (importance / 10.0) * 0.25 "
        "+ (LEAST(use_count, 10) / 10.0) * 0.15 DESC "
        "LIMIT %s",
        (embedding, embedding, NOTES_MIN_SIMILARITY, embedding, limit),
    ).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "content": r[2],
            "tags": r[3],
            "importance": r[4],
            "use_count": r[5],
            "similarity": r[6],
        }
        for r in rows
    ]


def bump_note_usage(conn: psycopg.Connection, note_id: int, used_at: str) -> None:
    conn.execute(
        "UPDATE notes SET use_count = use_count + 1, last_used_at = %s WHERE id = %s",
        (used_at, note_id),
    )
    conn.commit()


def get_consolidation_state(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        "SELECT last_consolidated_message_id FROM consolidation_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"last_consolidated_message_id": 0}
    return {"last_consolidated_message_id": row[0]}


def update_consolidation_state(conn: psycopg.Connection, last_consolidated_message_id: int) -> None:
    conn.execute(
        "INSERT INTO consolidation_state (id, last_consolidated_message_id) VALUES (1, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "last_consolidated_message_id = EXCLUDED.last_consolidated_message_id",
        (last_consolidated_message_id,),
    )
    conn.commit()


def get_messages_to_consolidate(
    conn: psycopg.Connection, after_id: int, keep_recent: int, batch_size: int
) -> list[dict]:
    boundary_row = conn.execute(
        "SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET %s", (keep_recent - 1,)
    ).fetchone()
    if boundary_row is None:
        return []
    boundary_id = boundary_row[0]
    rows = conn.execute(
        "SELECT id, role, content, timestamp FROM messages "
        "WHERE id > %s AND id < %s ORDER BY id LIMIT %s",
        (after_id, boundary_id, batch_size),
    ).fetchall()
    return [{"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]} for r in rows]
