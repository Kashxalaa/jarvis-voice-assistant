from datetime import datetime, timedelta

PC_TASK_MAX_AGE = timedelta(days=7)


def set_bypass(conn, until: datetime) -> None:
    # A fresh bypass window always re-arms the 15-minute expiry warning.
    conn.execute(
        "INSERT INTO computer_state (id, bypass_until, bypass_warned) VALUES (1, %s, FALSE) "
        "ON CONFLICT (id) DO UPDATE SET bypass_until = EXCLUDED.bypass_until, "
        "bypass_warned = FALSE",
        (until,),
    )


def clear_bypass(conn) -> None:
    conn.execute(
        "UPDATE computer_state SET bypass_until = NULL, bypass_warned = FALSE WHERE id = 1"
    )


def get_bypass_until(conn, now: datetime) -> datetime | None:
    row = conn.execute("SELECT bypass_until FROM computer_state WHERE id = 1").fetchone()
    if row is None or row[0] is None or row[0] <= now:
        return None
    return row[0]


def mark_bypass_warned(conn) -> None:
    conn.execute(
        "INSERT INTO computer_state (id, bypass_warned) VALUES (1, TRUE) "
        "ON CONFLICT (id) DO UPDATE SET bypass_warned = TRUE"
    )


def bypass_warned(conn) -> bool:
    row = conn.execute("SELECT bypass_warned FROM computer_state WHERE id = 1").fetchone()
    return bool(row and row[0])


def queue_pc_task(conn, text: str, now: datetime) -> int:
    row = conn.execute(
        "INSERT INTO pc_tasks (text, requested_at) VALUES (%s, %s) RETURNING id",
        (text, now),
    ).fetchone()
    return row[0]


def _task_dict(row) -> dict:
    return {"id": row[0], "text": row[1], "requested_at": row[2]}


def list_pc_tasks(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, text, requested_at FROM pc_tasks WHERE status = 'queued' "
        "ORDER BY requested_at, id"
    ).fetchall()
    return [_task_dict(r) for r in rows]


def cancel_pc_task(conn, task_id: int) -> bool:
    row = conn.execute(
        "UPDATE pc_tasks SET status = 'cancelled' WHERE id = %s AND status = 'queued' "
        "RETURNING id",
        (task_id,),
    ).fetchone()
    return row is not None


def next_pc_task(conn, now: datetime) -> dict | None:
    # Claims the oldest queued task that is still fresh. Stale ones are left
    # for drop_expired_pc_tasks, which the caller runs first to report them.
    row = conn.execute(
        """
        UPDATE pc_tasks SET status = 'running', started_at = %s
        WHERE id = (
            SELECT id FROM pc_tasks
            WHERE status = 'queued' AND requested_at >= %s
            ORDER BY requested_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, text, requested_at
        """,
        (now, now - PC_TASK_MAX_AGE),
    ).fetchone()
    return _task_dict(row) if row else None


def drop_expired_pc_tasks(conn, now: datetime) -> list[dict]:
    rows = conn.execute(
        "UPDATE pc_tasks SET status = 'expired', finished_at = %s "
        "WHERE status = 'queued' AND requested_at < %s "
        "RETURNING id, text, requested_at",
        (now, now - PC_TASK_MAX_AGE),
    ).fetchall()
    return sorted((_task_dict(r) for r in rows), key=lambda t: (t["requested_at"], t["id"]))


def finish_pc_task(conn, task_id: int, status: str, note: str = "") -> None:
    # status: done | failed | cancelled | partial (a step/time limit stopped it)
    conn.execute(
        "UPDATE pc_tasks SET status = %s, note = %s, finished_at = now() "
        "WHERE id = %s AND status = 'running'",
        (status, note, task_id),
    )


def log_activity(
    conn, machine: str, task_id: str, tool: str, detail: str,
    verdict: str, outcome: str, approver: str | None,
) -> None:
    conn.execute(
        "INSERT INTO activity_log (machine, task_id, tool, detail, verdict, outcome, approver) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (machine, task_id, tool, detail, verdict, outcome, approver),
    )


def recent_activity(conn, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        "SELECT id, at, machine, task_id, tool, detail, verdict, outcome, approver "
        "FROM activity_log ORDER BY at DESC, id DESC LIMIT %s",
        (limit,),
    ).fetchall()
    keys = ("id", "at", "machine", "task_id", "tool", "detail", "verdict", "outcome", "approver")
    return [dict(zip(keys, r)) for r in rows]


def prune_activity(conn, now: datetime, days: int = 90) -> int:
    return conn.execute(
        "DELETE FROM activity_log WHERE at < %s", (now - timedelta(days=days),)
    ).rowcount


def fail_stale_pc_tasks(conn, now: datetime) -> list[int]:
    # A task still 'running' when the local instance starts belongs to a run
    # that died with the previous process. Call before picking up queued ones.
    rows = conn.execute(
        "UPDATE pc_tasks SET status = 'failed', note = 'interrupted (restart)', "
        "finished_at = %s WHERE status = 'running' RETURNING id",
        (now,),
    ).fetchall()
    return sorted(r[0] for r in rows)


def claim_bypass_warning(conn, now: datetime) -> bool:
    # Atomic warn-once: True for exactly one caller per bypass window.
    row = conn.execute(
        "UPDATE computer_state SET bypass_warned = TRUE "
        "WHERE id = 1 AND NOT bypass_warned AND bypass_until IS NOT NULL "
        "AND bypass_until > %s RETURNING id",
        (now,),
    ).fetchone()
    return row is not None


def end_expired_bypass(conn, now: datetime) -> bool:
    # True exactly once per expired bypass window (the row is cleared), so the
    # caller posts "Bypass ended" a single time. /safe clears first -> False.
    row = conn.execute(
        "UPDATE computer_state SET bypass_until = NULL, bypass_warned = FALSE "
        "WHERE id = 1 AND bypass_until IS NOT NULL AND bypass_until <= %s RETURNING id",
        (now,),
    ).fetchone()
    return row is not None
