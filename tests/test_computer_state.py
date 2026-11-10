from datetime import datetime, timedelta, timezone

from brain import computer_state as cs

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_bypass_is_off_by_default(conn):
    assert cs.get_bypass_until(conn, NOW) is None
    assert cs.bypass_warned(conn) is False


def test_set_bypass_round_trips(conn):
    until = NOW + timedelta(hours=12)
    cs.set_bypass(conn, until)
    assert cs.get_bypass_until(conn, NOW) == until


def test_bypass_expired_reads_as_none(conn):
    cs.set_bypass(conn, NOW - timedelta(seconds=1))
    assert cs.get_bypass_until(conn, NOW) is None
    cs.set_bypass(conn, NOW)
    assert cs.get_bypass_until(conn, NOW) is None


def test_clear_bypass(conn):
    cs.set_bypass(conn, NOW + timedelta(hours=1))
    cs.clear_bypass(conn)
    assert cs.get_bypass_until(conn, NOW) is None


def test_clear_bypass_without_row_is_fine(conn):
    cs.clear_bypass(conn)
    assert cs.get_bypass_until(conn, NOW) is None


def test_warned_flag_and_reset_by_set_bypass(conn):
    cs.set_bypass(conn, NOW + timedelta(hours=1))
    cs.mark_bypass_warned(conn)
    assert cs.bypass_warned(conn) is True
    cs.set_bypass(conn, NOW + timedelta(hours=12))
    assert cs.bypass_warned(conn) is False


def test_clear_bypass_resets_warned(conn):
    cs.set_bypass(conn, NOW + timedelta(hours=1))
    cs.mark_bypass_warned(conn)
    cs.clear_bypass(conn)
    assert cs.bypass_warned(conn) is False


def test_queue_and_list_in_order(conn):
    a = cs.queue_pc_task(conn, "first", NOW)
    b = cs.queue_pc_task(conn, "second", NOW + timedelta(minutes=1))
    tasks = cs.list_pc_tasks(conn)
    assert [t["id"] for t in tasks] == [a, b]
    assert tasks[0]["text"] == "first"
    assert tasks[0]["requested_at"] == NOW


def test_next_pc_task_claims_oldest_and_marks_running(conn):
    a = cs.queue_pc_task(conn, "first", NOW)
    cs.queue_pc_task(conn, "second", NOW + timedelta(minutes=1))
    task = cs.next_pc_task(conn, NOW + timedelta(hours=1))
    assert task["id"] == a and task["text"] == "first"
    assert [t["text"] for t in cs.list_pc_tasks(conn)] == ["second"]
    row = conn.execute("SELECT status, started_at FROM pc_tasks WHERE id = %s", (a,)).fetchone()
    assert row[0] == "running" and row[1] is not None


def test_next_pc_task_none_when_empty(conn):
    assert cs.next_pc_task(conn, NOW) is None


def test_next_pc_task_skips_expired(conn):
    old = cs.queue_pc_task(conn, "old", NOW - timedelta(days=8))
    fresh = cs.queue_pc_task(conn, "fresh", NOW - timedelta(days=1))
    task = cs.next_pc_task(conn, NOW)
    assert task["id"] == fresh
    status = conn.execute("SELECT status FROM pc_tasks WHERE id = %s", (old,)).fetchone()[0]
    assert status == "queued"


def test_drop_expired_returns_and_marks(conn):
    old = cs.queue_pc_task(conn, "old", NOW - timedelta(days=8))
    cs.queue_pc_task(conn, "fresh", NOW - timedelta(days=6))
    dropped = cs.drop_expired_pc_tasks(conn, NOW)
    assert [t["id"] for t in dropped] == [old]
    assert dropped[0]["text"] == "old"
    assert [t["text"] for t in cs.list_pc_tasks(conn)] == ["fresh"]
    status = conn.execute("SELECT status FROM pc_tasks WHERE id = %s", (old,)).fetchone()[0]
    assert status == "expired"
    assert cs.drop_expired_pc_tasks(conn, NOW) == []


def test_cancel_pc_task(conn):
    a = cs.queue_pc_task(conn, "x", NOW)
    assert cs.cancel_pc_task(conn, a) is True
    assert cs.list_pc_tasks(conn) == []
    assert cs.cancel_pc_task(conn, a) is False
    assert cs.cancel_pc_task(conn, 9999) is False


def test_cancel_does_not_touch_running_task(conn):
    a = cs.queue_pc_task(conn, "x", NOW)
    cs.next_pc_task(conn, NOW)
    assert cs.cancel_pc_task(conn, a) is False


def test_finish_pc_task(conn):
    a = cs.queue_pc_task(conn, "x", NOW)
    cs.next_pc_task(conn, NOW)
    cs.finish_pc_task(conn, a, "done", "all good")
    row = conn.execute(
        "SELECT status, note, finished_at FROM pc_tasks WHERE id = %s", (a,)
    ).fetchone()
    assert row[0] == "done" and row[1] == "all good" and row[2] is not None


def test_finish_pc_task_never_overwrites_a_finished_task(conn):
    a = cs.queue_pc_task(conn, "x", NOW)
    cs.cancel_pc_task(conn, a)
    cs.finish_pc_task(conn, a, "done", "late")
    status = conn.execute("SELECT status FROM pc_tasks WHERE id = %s", (a,)).fetchone()[0]
    assert status == "cancelled"


def test_log_and_recent_activity_newest_first(conn):
    cs.log_activity(conn, "pc", "t1", "Bash", "ls", "RUN", "ok", None)
    cs.log_activity(conn, "pc", "t1", "Write", "a.txt", "ASK_UNLESS_BYPASS", "ok", "owner")
    rows = cs.recent_activity(conn)
    assert [r["tool"] for r in rows] == ["Write", "Bash"]
    assert rows[0]["approver"] == "owner" and rows[1]["approver"] is None
    assert rows[0]["verdict"] == "ASK_UNLESS_BYPASS"
    assert set(rows[0]) >= {"id", "at", "machine", "task_id", "tool", "detail", "verdict", "outcome", "approver"}
    assert len(cs.recent_activity(conn, limit=1)) == 1


def test_prune_activity(conn):
    cs.log_activity(conn, "pc", "t", "Bash", "old", "RUN", "ok", None)
    cs.log_activity(conn, "pc", "t", "Bash", "new", "RUN", "ok", None)
    conn.execute("UPDATE activity_log SET at = %s WHERE detail = 'old'", (NOW - timedelta(days=91),))
    assert cs.prune_activity(conn, NOW) == 1
    assert [r["detail"] for r in cs.recent_activity(conn)] == ["new"]


def test_fail_stale_pc_tasks_marks_running_failed_with_note_and_leaves_queued(conn):
    stale = cs.queue_pc_task(conn, "was running", NOW - timedelta(hours=2))
    queued = cs.queue_pc_task(conn, "still waiting", NOW - timedelta(hours=1))
    assert cs.next_pc_task(conn, NOW)["id"] == stale

    assert cs.fail_stale_pc_tasks(conn, NOW) == [stale]

    row = conn.execute(
        "SELECT status, note, finished_at FROM pc_tasks WHERE id = %s", (stale,)
    ).fetchone()
    assert row[0] == "failed" and row[1] == "interrupted (restart)" and row[2] == NOW
    assert [t["id"] for t in cs.list_pc_tasks(conn)] == [queued]
    assert cs.fail_stale_pc_tasks(conn, NOW) == []


def test_end_expired_bypass_reports_the_expiry_exactly_once(conn):
    assert cs.end_expired_bypass(conn, NOW) is False  # no row at all
    cs.set_bypass(conn, NOW + timedelta(minutes=5))
    assert cs.end_expired_bypass(conn, NOW) is False  # still running
    assert cs.end_expired_bypass(conn, NOW + timedelta(minutes=5)) is True
    assert cs.end_expired_bypass(conn, NOW + timedelta(minutes=6)) is False
    assert cs.get_bypass_until(conn, NOW) is None
    assert cs.bypass_warned(conn) is False


def test_end_expired_bypass_ignores_a_bypass_ended_by_safe(conn):
    cs.set_bypass(conn, NOW + timedelta(minutes=5))
    cs.clear_bypass(conn)
    assert cs.end_expired_bypass(conn, NOW + timedelta(hours=1)) is False


def test_claim_bypass_warning_succeeds_once_per_window(conn):
    assert cs.claim_bypass_warning(conn, NOW) is False  # no bypass at all
    cs.set_bypass(conn, NOW + timedelta(minutes=10))
    assert cs.claim_bypass_warning(conn, NOW) is True
    assert cs.claim_bypass_warning(conn, NOW) is False
    cs.set_bypass(conn, NOW + timedelta(minutes=10))  # a new window re-arms it
    assert cs.claim_bypass_warning(conn, NOW) is True
    assert cs.claim_bypass_warning(conn, NOW + timedelta(minutes=11)) is False  # expired
