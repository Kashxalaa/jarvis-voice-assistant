"""One-time migration: copy assistant_memory.db (SQLite) into the
Postgres database named by DATABASE_URL. Run once, after Task 1-3 are
deployed and DATABASE_URL is configured, before switching main.py /
discord_bot.py over to using it day-to-day.

Usage: python scripts/migrate_to_postgres.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.memory import init_db
from config import resolve_database_url

SQLITE_PATH = str(Path(__file__).resolve().parent.parent / "assistant_memory.db")


def migrate() -> None:
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    pg_conn = init_db(resolve_database_url())

    existing_message_count = pg_conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if existing_message_count:
        print(
            f"Skipped messages: {existing_message_count} already present in the "
            "target database (this script isn't safe to re-run for messages, "
            "which would duplicate history)."
        )
    else:
        try:
            messages = sqlite_conn.execute(
                "SELECT role, content, timestamp FROM messages ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            messages = []
        for role, content, timestamp in messages:
            pg_conn.execute(
                "INSERT INTO messages (role, content, timestamp) VALUES (%s, %s, %s)",
                (role, content, timestamp),
            )
        print(f"Migrated {len(messages)} messages.")

    try:
        facts = sqlite_conn.execute("SELECT key, value FROM facts").fetchall()
    except sqlite3.OperationalError:
        facts = []
    for key, value in facts:
        pg_conn.execute(
            "INSERT INTO facts (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
    print(f"Migrated {len(facts)} facts.")

    try:
        actions = sqlite_conn.execute("SELECT name, kind, value FROM actions").fetchall()
    except sqlite3.OperationalError:
        actions = []
    for name, kind, value in actions:
        pg_conn.execute(
            "INSERT INTO actions (name, kind, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind, value = EXCLUDED.value",
            (name, kind, value),
        )
    print(f"Migrated {len(actions)} actions.")

    pg_conn.commit()
    pg_conn.close()
    sqlite_conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
