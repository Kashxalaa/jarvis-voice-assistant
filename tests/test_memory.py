import pytest

from brain.memory import init_db, add_message, get_recent_messages


def test_add_and_get_recent_messages_returns_in_chronological_order(conn):
    add_message(conn, "user", "hello")
    add_message(conn, "assistant", "hi there")

    messages = get_recent_messages(conn)

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]
    assert all(m["timestamp"] for m in messages)


def test_get_recent_messages_truncates_to_limit_keeping_most_recent(conn):
    for i in range(25):
        add_message(conn, "user", f"message {i}")

    messages = get_recent_messages(conn, limit=20)

    assert len(messages) == 20
    assert messages[0]["content"] == "message 5"
    assert messages[-1]["content"] == "message 24"


from brain.memory import upsert_fact, get_all_facts


def test_upsert_fact_inserts_new_key(conn):
    upsert_fact(conn, "main_editor", "VS Code")

    assert get_all_facts(conn) == {"main_editor": "VS Code"}


def test_upsert_fact_overwrites_existing_key(conn):
    upsert_fact(conn, "main_editor", "VS Code")
    upsert_fact(conn, "main_editor", "Neovim")

    assert get_all_facts(conn) == {"main_editor": "Neovim"}


from brain.memory import count_user_messages


def test_count_user_messages_only_counts_user_role(conn):
    add_message(conn, "user", "one")
    add_message(conn, "assistant", "reply")
    add_message(conn, "user", "two")

    assert count_user_messages(conn) == 2


def test_init_db_migration_adds_missing_columns_and_backfills_legacy_rows(conn):
    # Simulates the live production DB as it was before this migration
    # existed: the columns don't exist at all (not just left at their
    # defaults). Drop them, insert legacy rows the way the old schema
    # would have, then run init_db as it runs on every process start and
    # confirm it actually adds the columns back (ADD COLUMN, not a no-op)
    # with the right defaults backfilled onto the pre-existing rows.
    conn.execute(
        "ALTER TABLE reminders DROP COLUMN category, DROP COLUMN parent_id, "
        "DROP COLUMN lead_days"
    )
    conn.execute("ALTER TABLE todos DROP COLUMN category")

    row = conn.execute(
        "INSERT INTO reminders (text, starts_at, next_fire_at) "
        "VALUES ('Legacy', now(), now()) RETURNING id"
    ).fetchone()
    reminder_id = row[0]
    todo_row = conn.execute(
        "INSERT INTO todos (text, due_date) VALUES ('Legacy todo', CURRENT_DATE) RETURNING id"
    ).fetchone()
    todo_id = todo_row[0]

    from tests.conftest import TEST_DATABASE_URL

    migrated_conn = init_db(TEST_DATABASE_URL)  # must not raise on a DB missing the columns
    try:
        reminder = migrated_conn.execute(
            "SELECT category, parent_id, lead_days FROM reminders WHERE id = %s",
            (reminder_id,),
        ).fetchone()
        assert reminder == ("other", None, 0)
        todo_category = migrated_conn.execute(
            "SELECT category FROM todos WHERE id = %s", (todo_id,)
        ).fetchone()[0]
        assert todo_category == "task"
    finally:
        migrated_conn.close()


def test_init_db_migration_adds_timed_column_defaulting_true_on_legacy_rows(conn):
    # Simulates the live production DB before the "timed" column existed:
    # drop it, insert a legacy row the old schema would have produced, then
    # run init_db (as it runs on every process start) and confirm existing
    # rows are backfilled to TRUE — the 98 real reminders already in
    # production must keep pinging exactly as they do today.
    conn.execute("ALTER TABLE reminders DROP COLUMN timed")

    row = conn.execute(
        "INSERT INTO reminders (text, starts_at, next_fire_at) "
        "VALUES ('Legacy', now(), now()) RETURNING id"
    ).fetchone()
    reminder_id = row[0]

    from tests.conftest import TEST_DATABASE_URL

    migrated_conn = init_db(TEST_DATABASE_URL)  # must not raise on a DB missing the column
    try:
        timed = migrated_conn.execute(
            "SELECT timed FROM reminders WHERE id = %s", (reminder_id,)
        ).fetchone()[0]
        assert timed is True
    finally:
        migrated_conn.close()


def test_init_db_creates_reminder_pings_table_idempotently(conn):
    # init_db already ran once via the `conn` fixture; running it again must
    # not raise (CREATE TABLE IF NOT EXISTS), and the table must have the
    # columns the ping<->list sync relies on.
    from tests.conftest import TEST_DATABASE_URL

    migrated_conn = init_db(TEST_DATABASE_URL)
    try:
        row = migrated_conn.execute(
            "INSERT INTO reminder_pings (reminder_id, message_id, fired_on, due_at) "
            "VALUES (1, 123456789, CURRENT_DATE, now()) RETURNING needs_render"
        ).fetchone()
        assert row[0] is False
    finally:
        migrated_conn.close()


def test_init_db_creates_reminder_pings_indexes_idempotently(conn):
    # Two runs must not raise (CREATE INDEX IF NOT EXISTS), and both the
    # lookup index and the partial needs_render index must exist.
    from tests.conftest import TEST_DATABASE_URL

    migrated_conn = init_db(TEST_DATABASE_URL)
    try:
        names = {
            r[0]
            for r in migrated_conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'reminder_pings'"
            ).fetchall()
        }
        assert "idx_reminder_pings_reminder_fired_on" in names
        assert "idx_reminder_pings_needs_render" in names
    finally:
        migrated_conn.close()


def test_init_db_migration_adds_weather_line_column_idempotently(conn):
    # Simulates the live production DB before this migration existed: the
    # column doesn't exist at all. Drop it, insert a legacy row the old
    # schema would have produced, then run init_db twice (as it runs on
    # every process start) and confirm it adds the column back without
    # raising either time, defaulting existing rows to NULL (no weather
    # line for a day that never fetched one).
    conn.execute("ALTER TABLE daily_list DROP COLUMN weather_line")
    conn.execute("INSERT INTO daily_list (id, list_date) VALUES (1, CURRENT_DATE)")

    from tests.conftest import TEST_DATABASE_URL

    migrated_conn = init_db(TEST_DATABASE_URL)  # must not raise on a DB missing the column
    try:
        weather_line = migrated_conn.execute(
            "SELECT weather_line FROM daily_list WHERE id = 1"
        ).fetchone()[0]
        assert weather_line is None

        again_conn = init_db(TEST_DATABASE_URL)  # a second run must not raise either
        try:
            pass
        finally:
            again_conn.close()
    finally:
        migrated_conn.close()


def test_init_db_connection_can_be_used_from_a_different_thread(conn):
    import threading

    results = []

    def worker():
        add_message(conn, "user", "from another thread")
        results.append(count_user_messages(conn))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert results == [1]


from brain.memory import upsert_action, get_action


def test_upsert_action_inserts_new_entry(conn):
    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")

    assert get_action(conn, "vscode") == {
        "name": "vscode", "kind": "app", "value": "C:/apps/code.exe"
    }


def test_upsert_action_overwrites_existing_entry(conn):
    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")
    upsert_action(conn, "vscode", "app", "D:/tools/code.exe")

    assert get_action(conn, "vscode")["value"] == "D:/tools/code.exe"


def test_get_action_returns_none_when_not_found(conn):
    assert get_action(conn, "nonexistent") is None


def test_list_actions_returns_all_actions_sorted_by_name(conn):
    from brain.memory import list_actions

    upsert_action(conn, "vscode", "app", "C:/apps/code.exe")
    upsert_action(conn, "journal_folder", "folder", "E:/journal")

    assert list_actions(conn) == [
        {"name": "journal_folder", "kind": "folder", "value": "E:/journal"},
        {"name": "vscode", "kind": "app", "value": "C:/apps/code.exe"},
    ]


def test_list_actions_returns_empty_list_when_none_exist(conn):
    from brain.memory import list_actions

    assert list_actions(conn) == []


from brain.memory import (
    set_pending_proposal,
    get_pending_proposal,
    clear_pending_proposal,
    decrement_pending_proposal_turns,
)


def test_set_and_get_pending_proposal(conn):
    set_pending_proposal(
        conn, "url", "quickbrew_site", "https://quickbrew.com", "reason",
        "2026-09-20T12:00:00+00:00",
    )

    proposal = get_pending_proposal(conn)
    assert proposal["kind"] == "url"
    assert proposal["suggested_name"] == "quickbrew_site"
    assert proposal["suggested_value"] == "https://quickbrew.com"
    assert proposal["reasoning"] == "reason"
    assert proposal["created_at"] == "2026-09-20T12:00:00+00:00"
    assert proposal["turns_remaining"] == 3


def test_set_pending_proposal_replaces_any_existing_one(conn):
    set_pending_proposal(
        conn, "url", "first", "https://first.com", "r1", "2026-09-20T12:00:00+00:00"
    )
    set_pending_proposal(
        conn, "app", "second", "C:/second.exe", "r2", "2026-09-20T12:05:00+00:00"
    )

    proposal = get_pending_proposal(conn)
    assert proposal["suggested_name"] == "second"


def test_get_pending_proposal_returns_none_when_nothing_pending(conn):
    assert get_pending_proposal(conn) is None


def test_clear_pending_proposal_removes_it(conn):
    set_pending_proposal(
        conn, "url", "quickbrew_site", "https://quickbrew.com", "reason",
        "2026-09-20T12:00:00+00:00",
    )

    clear_pending_proposal(conn)

    assert get_pending_proposal(conn) is None


def test_decrement_pending_proposal_turns_reduces_the_count(conn):
    set_pending_proposal(
        conn, "url", "quickbrew_site", "https://quickbrew.com", "reason",
        "2026-09-20T12:00:00+00:00",
    )

    decrement_pending_proposal_turns(conn)

    assert get_pending_proposal(conn)["turns_remaining"] == 2


def test_touch_local_heartbeat_then_is_fresh_returns_true(conn):
    from brain.memory import is_local_heartbeat_fresh, touch_local_heartbeat

    touch_local_heartbeat(conn)

    assert is_local_heartbeat_fresh(conn) is True


def test_is_local_heartbeat_fresh_returns_false_when_never_touched(conn):
    from brain.memory import is_local_heartbeat_fresh

    assert is_local_heartbeat_fresh(conn) is False


def test_is_local_heartbeat_fresh_returns_false_when_stale(conn):
    from datetime import datetime, timedelta, timezone

    from brain.memory import is_local_heartbeat_fresh

    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn.execute(
        "INSERT INTO local_heartbeat (id, updated_at) VALUES (1, %s)", (old_time,)
    )
    conn.commit()

    assert is_local_heartbeat_fresh(conn, max_age_seconds=30) is False


def test_touch_local_heartbeat_overwrites_the_previous_value(conn):
    from brain.memory import touch_local_heartbeat

    touch_local_heartbeat(conn)
    touch_local_heartbeat(conn)

    row = conn.execute("SELECT COUNT(*) FROM local_heartbeat").fetchone()
    assert row[0] == 1


def test_is_local_heartbeat_fresh_ignores_client_clock_skew(conn, monkeypatch):
    from datetime import datetime

    from brain import memory
    from brain.memory import is_local_heartbeat_fresh, touch_local_heartbeat

    touch_local_heartbeat(conn)

    class _SkewedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 1, 1, tzinfo=tz)

    monkeypatch.setattr(memory, "datetime", _SkewedDatetime)

    assert is_local_heartbeat_fresh(conn) is True


def test_connection_recovers_after_a_failed_statement_without_manual_rollback(conn):
    import psycopg

    with pytest.raises(psycopg.Error):
        conn.execute("SELECT 1/0")

    add_message(conn, "user", "hello")

    assert count_user_messages(conn) == 1


def test_ensure_connected_returns_same_connection_when_still_alive(conn):
    from brain.memory import ensure_connected
    from tests.conftest import TEST_DATABASE_URL

    result = ensure_connected(conn, TEST_DATABASE_URL)

    assert result is conn


def test_ensure_connected_reconnects_when_connection_is_closed():
    from brain.memory import ensure_connected, init_db
    from tests.conftest import TEST_DATABASE_URL

    dead_conn = init_db(TEST_DATABASE_URL)
    dead_conn.close()

    result = ensure_connected(dead_conn, TEST_DATABASE_URL)

    assert result is not dead_conn
    assert not result.closed
    result.execute("SELECT 1")
    result.close()


def test_init_db_does_not_raise_when_vector_extension_is_unavailable(monkeypatch):
    import psycopg

    from brain.memory import init_db

    real_connect = psycopg.connect
    intercepted = []

    def _connect_and_block_vector_extension(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        real_execute = conn.execute

        def _execute(query, *a, **kw):
            if "CREATE EXTENSION" in query and "vector" in query:
                intercepted.append(query)
                raise psycopg.errors.UndefinedFile("simulated: extension not installed")
            return real_execute(query, *a, **kw)

        conn.execute = _execute
        return conn

    monkeypatch.setattr(psycopg, "connect", _connect_and_block_vector_extension)

    from tests.conftest import TEST_DATABASE_URL

    conn = init_db(TEST_DATABASE_URL)

    assert intercepted, "init_db never attempted to enable the vector extension"
    conn.execute("SELECT 1")  # connection must still be usable afterward
    conn.close()


def test_insert_and_search_notes_by_embedding(notes_conn):
    from brain.memory import insert_note, search_notes_by_embedding

    close_embedding = [1.0, 0.0] + [0.0] * 766
    far_embedding = [0.0, 1.0] + [0.0] * 766
    insert_note(
        notes_conn, "Trading setup", "We configured the trading journal automation.",
        "trading,automation", 7, close_embedding, "explicit", "2026-09-21T00:00:00+00:00",
    )
    insert_note(
        notes_conn, "Unrelated", "Completely different topic.",
        "other", 5, far_embedding, "explicit", "2026-09-21T00:00:00+00:00",
    )

    results = search_notes_by_embedding(notes_conn, close_embedding, limit=1)

    assert len(results) == 1
    assert results[0]["title"] == "Trading setup"
    assert results[0]["use_count"] == 0


def test_search_notes_by_embedding_excludes_unrelated_notes_below_similarity_floor(notes_conn):
    from brain.memory import insert_note, search_notes_by_embedding

    close_embedding = [1.0, 0.0] + [0.0] * 766
    orthogonal_embedding = [0.0, 1.0] + [0.0] * 766
    insert_note(
        notes_conn, "Trading setup", "We configured the trading journal automation.",
        "trading,automation", 7, close_embedding, "explicit", "2026-09-21T00:00:00+00:00",
    )
    insert_note(
        notes_conn, "Unrelated", "Completely different topic.",
        "other", 5, orthogonal_embedding, "explicit", "2026-09-21T00:00:00+00:00",
    )

    # limit is large enough that the unrelated note would appear if it
    # weren't filtered by similarity — it must not appear at all.
    results = search_notes_by_embedding(notes_conn, close_embedding, limit=10)

    assert [r["title"] for r in results] == ["Trading setup"]


def test_search_notes_by_embedding_returns_empty_list_when_no_notes_exist(notes_conn):
    from brain.memory import search_notes_by_embedding

    results = search_notes_by_embedding(notes_conn, [0.0] * 768, limit=3)

    assert results == []


def test_bump_note_usage_increments_count_and_sets_last_used(notes_conn):
    from brain.memory import bump_note_usage, insert_note, search_notes_by_embedding

    embedding = [1.0] + [0.0] * 767
    note_id = insert_note(
        notes_conn, "T", "C", "tag", 5, embedding, "explicit", "2026-09-21T00:00:00+00:00",
    )

    bump_note_usage(notes_conn, note_id, "2026-09-21T01:00:00+00:00")

    results = search_notes_by_embedding(notes_conn, embedding, limit=1)
    assert results[0]["use_count"] == 1


def test_get_consolidation_state_defaults_to_zero_when_never_run(notes_conn):
    from brain.memory import get_consolidation_state

    state = get_consolidation_state(notes_conn)

    assert state == {"last_consolidated_message_id": 0}


def test_update_consolidation_state_then_get_returns_it(notes_conn):
    from brain.memory import get_consolidation_state, update_consolidation_state

    update_consolidation_state(notes_conn, 42)

    assert get_consolidation_state(notes_conn) == {"last_consolidated_message_id": 42}


def test_update_consolidation_state_overwrites_previous_value(notes_conn):
    from brain.memory import get_consolidation_state, update_consolidation_state

    update_consolidation_state(notes_conn, 10)
    update_consolidation_state(notes_conn, 20)

    assert get_consolidation_state(notes_conn) == {"last_consolidated_message_id": 20}


def test_get_messages_to_consolidate_excludes_the_live_recent_window(notes_conn):
    from brain.memory import add_message, get_messages_to_consolidate

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")

    results = get_messages_to_consolidate(
        notes_conn, after_id=0, keep_recent=20, batch_size=50
    )

    assert len(results) == 5
    assert [r["content"] for r in results] == [f"message {i}" for i in range(5)]


def test_get_messages_to_consolidate_respects_after_id_and_batch_size(notes_conn):
    from brain.memory import add_message, get_messages_to_consolidate

    for i in range(25):
        add_message(notes_conn, "user", f"message {i}")

    results = get_messages_to_consolidate(
        notes_conn, after_id=2, keep_recent=20, batch_size=2
    )

    assert len(results) == 2
    assert [r["content"] for r in results] == ["message 2", "message 3"]


def test_get_messages_to_consolidate_returns_empty_when_not_enough_messages(notes_conn):
    from brain.memory import add_message, get_messages_to_consolidate

    for i in range(5):
        add_message(notes_conn, "user", f"message {i}")

    results = get_messages_to_consolidate(
        notes_conn, after_id=0, keep_recent=20, batch_size=50
    )

    assert results == []
