from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from brain.reminders import (
    CATEGORIES,
    AmbiguousItemError,
    ItemNotFoundError,
    add_todo,
    agenda_items,
    claim_due_reminders,
    clear_list_dirty,
    clear_ping_needs_render,
    create_reminder,
    display_text,
    drop_ping,
    edit_reminder,
    finish_item,
    get_daily_list_state,
    get_ping_by_message_id,
    get_reminder_info,
    get_reminder_ping_state,
    get_reminder_text,
    is_early_reminder_text,
    mark_ping_needs_render,
    mark_pings_dirty,
    mark_reminder_done,
    pings_needing_render,
    prune_old_pings,
    record_ping,
    restore_reminder,
    save_daily_list_state,
    snooze_reminder,
    toggle_item,
)
from brain.schedule import ScheduleInputError

TZ = ZoneInfo("Asia/Tbilisi")
TODAY = date(2026, 9, 26)


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def _reminder_row(conn, reminder_id):
    return conn.execute(
        "SELECT text, starts_at, rrule, next_fire_at, status, last_done_on "
        "FROM reminders WHERE id = %s",
        (reminder_id,),
    ).fetchone()


def _dirty(conn):
    row = conn.execute("SELECT needs_refresh FROM daily_list WHERE id = 1").fetchone()
    return bool(row and row[0])


def test_create_one_time_reminder_stores_it_and_marks_list_dirty(conn):
    created = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)

    assert created["text"] == "Close trades"
    assert created["next_fire_at"] == at(26, 21)
    assert created["rrule"] is None
    row = _reminder_row(conn, created["id"])
    assert row[3] == at(26, 21)
    assert row[4] == "active"
    assert _dirty(conn)


def test_create_recurring_reminder_stores_rule_and_first_fire(conn):
    created = create_reminder(
        conn, "Check market", "2026-09-26T09:00", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        at(26, 10), TZ,
    )

    assert created["next_fire_at"] == at(28, 9)
    assert _reminder_row(conn, created["id"])[2] == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"


def test_create_reminder_treats_blank_repeat_as_one_time(conn):
    created = create_reminder(conn, "Call", "2026-09-26T21:00", "  ", at(26, 10), TZ)
    assert created["rrule"] is None


def test_create_reminder_rejects_empty_text(conn):
    with pytest.raises(ScheduleInputError):
        create_reminder(conn, "  ", "2026-09-26T21:00", None, at(26, 10), TZ)


def test_create_reminder_rejects_text_over_500_characters(conn):
    with pytest.raises(ScheduleInputError):
        create_reminder(conn, "x" * 501, "2026-09-26T21:00", None, at(26, 10), TZ)
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_create_reminder_rejects_a_past_time_without_saving(conn):
    with pytest.raises(ScheduleInputError):
        create_reminder(conn, "Too late", "2026-09-26T08:00", None, at(26, 10), TZ)
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_add_todo_defaults_to_today(conn):
    todo = add_todo(conn, "Gym", None, TODAY)
    assert todo["due_date"] == TODAY
    assert _dirty(conn)


def test_add_todo_with_a_date(conn):
    assert add_todo(conn, "Dentist", "2026-09-28", TODAY)["due_date"] == date(2026, 9, 28)


def test_add_todo_rejects_empty_text(conn):
    with pytest.raises(ScheduleInputError):
        add_todo(conn, "", None, TODAY)


def test_add_todo_rejects_text_over_500_characters(conn):
    with pytest.raises(ScheduleInputError):
        add_todo(conn, "x" * 501, None, TODAY)
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0


def test_get_reminder_text_raises_for_unknown_id(conn):
    with pytest.raises(ItemNotFoundError):
        get_reminder_text(conn, 999)


def test_mark_one_time_reminder_done_sets_status(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]

    assert mark_reminder_done(conn, rid, TODAY) == "Close trades"
    row = _reminder_row(conn, rid)
    assert row[4] == "done"
    assert row[5] == TODAY


def test_mark_recurring_reminder_done_keeps_the_series_active(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    mark_reminder_done(conn, rid, TODAY)

    row = _reminder_row(conn, rid)
    assert row[4] == "active"
    assert row[5] == TODAY


def test_snooze_one_time_reminder_moves_it(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    conn.execute("UPDATE reminders SET next_fire_at = NULL WHERE id = %s", (rid,))

    until = snooze_reminder(conn, rid, 10, at(26, 21, 3))

    assert until == at(26, 21, 13)
    row = _reminder_row(conn, rid)
    assert row[1] == at(26, 21, 13)
    assert row[3] == at(26, 21, 13)
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_snooze_recurring_reminder_creates_a_one_time_copy(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    until = snooze_reminder(conn, rid, 60, at(26, 21))

    assert until == at(26, 22)
    assert _reminder_row(conn, rid)[3] == at(26, 21)  # series untouched
    copy = conn.execute(
        "SELECT text, rrule, next_fire_at FROM reminders WHERE id <> %s", (rid,)
    ).fetchone()
    assert copy == ("Market", None, at(26, 22))


def test_finish_item_done_on_todo(conn):
    tid = add_todo(conn, "Gym", None, TODAY)["id"]

    assert finish_item(conn, f"t{tid}", "done", TODAY, at(26, 12)) == "Gym"
    done_at = conn.execute("SELECT done_at FROM todos WHERE id = %s", (tid,)).fetchone()[0]
    assert done_at == at(26, 12)


def test_finish_item_cancel_on_todo_deletes_it(conn):
    tid = add_todo(conn, "Gym", None, TODAY)["id"]

    finish_item(conn, f"t{tid}", "cancel", TODAY, at(26, 12))

    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0


def test_finish_item_cancel_on_reminder_stops_it_firing(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    assert finish_item(conn, f"r{rid}", "cancel", TODAY, at(26, 12)) == "Market"
    row = _reminder_row(conn, rid)
    assert row[4] == "cancelled"
    assert row[3] is None


def test_finish_item_rejects_unknown_and_malformed_ids(conn):
    for bad in ("t999", "r999", "x1", "", "t"):
        with pytest.raises(ItemNotFoundError):
            finish_item(conn, bad, "done", TODAY, at(26, 12))


def test_finish_item_rejects_an_unknown_action(conn):
    tid = add_todo(conn, "Gym", None, TODAY)["id"]
    with pytest.raises(ScheduleInputError):
        finish_item(conn, f"t{tid}", "explode", TODAY, at(26, 12))


def test_toggle_todo_twice_restores_it(conn):
    tid = add_todo(conn, "Gym", None, TODAY)["id"]

    assert toggle_item(conn, f"t{tid}", TODAY, at(26, 12)) is True
    assert toggle_item(conn, f"t{tid}", TODAY, at(26, 12)) is False
    assert conn.execute("SELECT done_at FROM todos WHERE id = %s", (tid,)).fetchone()[0] is None


def test_toggle_one_time_reminder_twice_restores_it(conn):
    rid = create_reminder(conn, "Call", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]

    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is True
    assert _reminder_row(conn, rid)[4] == "done"
    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is False
    row = _reminder_row(conn, rid)
    assert row[4] == "active"
    assert row[5] is None


def test_toggle_recurring_reminder_only_affects_today(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is True
    assert _reminder_row(conn, rid)[5] == TODAY
    # Yesterday's tick doesn't count as done today.
    conn.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY - timedelta(days=1), rid))
    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is True


def test_agenda_lists_reminders_by_time_then_todos(conn):
    # Same category for every item here: this test is about the ordering
    # *within* a category (reminders by time, then todos), not about
    # category grouping — that's covered separately below.
    add_todo(conn, "Gym", None, TODAY, category="admin")
    create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="admin")
    create_reminder(conn, "Lunch", "2026-09-26T13:00", None, at(26, 10), TZ, category="admin")

    items = agenda_items(conn, TODAY, TZ)

    assert [(i["text"], i["time"]) for i in items] == [
        ("Lunch", "13:00"), ("Close trades", "21:00"), ("Gym", None),
    ]
    assert items[2]["id"].startswith("t")
    assert items[0]["id"].startswith("r")


def test_agenda_carries_over_unfinished_todos_with_since(conn):
    add_todo(conn, "Call the bank", "2026-09-22", TODAY)

    item = agenda_items(conn, TODAY, TZ)[0]

    assert item["since"] == date(2026, 9, 22)
    assert item["done"] is False


def test_agenda_hides_future_todos_and_old_done_todos(conn):
    add_todo(conn, "Dentist", "2026-09-28", TODAY)
    old = add_todo(conn, "Old", "2026-09-20", TODAY)["id"]
    finish_item(conn, f"t{old}", "done", TODAY, at(24, 12))

    assert agenda_items(conn, TODAY, TZ) == []
    assert [i["text"] for i in agenda_items(conn, date(2026, 9, 28), TZ)] == ["Dentist"]


def test_agenda_shows_todos_done_today_as_done(conn):
    tid = add_todo(conn, "Gym", None, TODAY)["id"]
    finish_item(conn, f"t{tid}", "done", TODAY, at(26, 12))

    item = agenda_items(conn, TODAY, TZ)[0]

    assert item["done"] is True
    assert item["since"] is None


def test_agenda_includes_recurring_occurrences_and_done_state(conn):
    rid = create_reminder(
        conn, "Market", "2026-09-21T09:30", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", at(21, 8), TZ
    )["id"]

    assert agenda_items(conn, TODAY, TZ) == []  # Saturday
    monday = agenda_items(conn, date(2026, 9, 28), TZ)
    assert [(i["text"], i["time"], i["done"]) for i in monday] == [("Market", "09:30", False)]
    mark_reminder_done(conn, rid, date(2026, 9, 28))
    assert agenda_items(conn, date(2026, 9, 28), TZ)[0]["done"] is True


def test_agenda_keeps_a_fired_one_time_reminder_on_its_day_only(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    conn.execute("UPDATE reminders SET next_fire_at = NULL WHERE id = %s", (rid,))

    assert [i["text"] for i in agenda_items(conn, TODAY, TZ)] == ["Close trades"]
    assert agenda_items(conn, date(2026, 9, 27), TZ) == []


def test_agenda_excludes_cancelled_reminders(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    finish_item(conn, f"r{rid}", "cancel", TODAY, at(26, 12))

    assert agenda_items(conn, TODAY, TZ) == []


def test_agenda_lists_an_hourly_reminder_once_at_its_first_time(conn):
    create_reminder(conn, "Water", "2026-09-26T10:00", "FREQ=HOURLY", at(26, 9), TZ)

    items = agenda_items(conn, TODAY, TZ)

    assert [(i["text"], i["time"]) for i in items] == [("Water", "10:00")]


def test_claim_due_one_time_reminder_clears_next_fire(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]

    assert claim_due_reminders(conn, at(26, 20, 59), TZ) == []
    claimed = claim_due_reminders(conn, at(26, 21), TZ)

    assert claimed == [
        {"id": rid, "text": "Close trades", "due_at": at(26, 21), "category": "other"}
    ]
    assert _reminder_row(conn, rid)[3] is None
    assert claim_due_reminders(conn, at(26, 21, 1), TZ) == []


def test_claim_due_reminders_orders_ties_by_id(conn):
    rid_first = create_reminder(conn, "B", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    rid_second = create_reminder(conn, "A", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    # Both share next_fire_at. Rewriting the first row's tuple moves it to the
    # end of the heap, so without an explicit id tiebreak a plain scan would
    # return the rows out of id order.
    conn.execute("UPDATE reminders SET next_fire_at = next_fire_at WHERE id = %s", (rid_first,))
    conn.commit()

    ids = [c["id"] for c in claim_due_reminders(conn, at(26, 21), TZ)]

    assert ids == sorted([rid_first, rid_second])


def test_claim_recurring_advances_past_missed_occurrences(conn):
    rid = create_reminder(conn, "Market", "2026-09-21T09:00", "FREQ=DAILY", at(21, 8), TZ)["id"]

    claimed = claim_due_reminders(conn, at(24, 14), TZ)

    assert [c["due_at"] for c in claimed] == [at(21, 9)]  # one ping, not four
    assert _reminder_row(conn, rid)[3] == at(25, 9)


def test_claim_advances_a_monthly_bymonthday_early_reminder_by_a_full_month(conn):
    # Every imported early reminder depends on claim_due_reminders passing
    # lead_days through to next_occurrence — without it, the early row's
    # next fire would land on the MAIN's day (28th) instead of staying one
    # day ahead of it (27th).
    created = create_reminder(
        conn, "Pay rent", "2026-08-28T10:00", "FREQ=MONTHLY;BYMONTHDAY=28", at(26, 10), TZ,
        remind_days_before=1,
    )
    early_id = created["early"]["id"]
    assert created["early"]["next_fire_at"] == datetime(2026, 9, 27, 10, 0, tzinfo=TZ)

    claimed = claim_due_reminders(conn, datetime(2026, 10, 27, 10, 0, tzinfo=TZ), TZ)

    early_claims = [c for c in claimed if c["id"] == early_id]
    # due_at is whatever was already overdue (its own stored next_fire_at);
    # "now" (2026-10-27) is only the reference point for finding what's next.
    assert early_claims == [
        {
            "id": early_id, "text": "Tomorrow: Pay rent",
            "due_at": datetime(2026, 9, 27, 10, 0, tzinfo=TZ), "category": "other",
        }
    ]
    assert _reminder_row(conn, early_id)[3] == datetime(2026, 11, 27, 10, 0, tzinfo=TZ)


def test_claim_advances_a_yearly_early_reminder_to_the_day_before_next_year(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2027-04-10T10:00", "FREQ=YEARLY",
        datetime(2026, 3, 1, 10, 0, tzinfo=TZ), TZ, remind_days_before=1,
    )
    early_id = created["early"]["id"]
    assert created["early"]["next_fire_at"] == datetime(2027, 4, 9, 10, 0, tzinfo=TZ)

    claimed = claim_due_reminders(conn, datetime(2027, 4, 9, 10, 0, tzinfo=TZ), TZ)

    early_claims = [c for c in claimed if c["id"] == early_id]
    assert early_claims == [
        {
            "id": early_id, "text": "Tomorrow: Giorgi's birthday",
            "due_at": datetime(2027, 4, 9, 10, 0, tzinfo=TZ), "category": "other",
        }
    ]
    assert _reminder_row(conn, early_id)[3] == datetime(2028, 4, 9, 10, 0, tzinfo=TZ)


def test_claim_skips_a_reminder_cancelled_after_it_became_due(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    finish_item(conn, f"r{rid}", "cancel", TODAY, at(26, 21, 1))

    assert claim_due_reminders(conn, at(26, 21, 2), TZ) == []


def test_claim_does_not_fire_a_reminder_cancelled_mid_claim(conn, monkeypatch):
    """Verify the UPDATE guard prevents firing a reminder cancelled between SELECT and UPDATE."""
    from brain.schedule import next_occurrence as real_next_occurrence

    rid = create_reminder(
        conn, "Market", "2026-09-21T09:00", "FREQ=DAILY", at(21, 8), TZ
    )["id"]

    original_fire_at = _reminder_row(conn, rid)[3]

    # Monkeypatch next_occurrence to cancel the reminder but restore its fire time
    # so the next_fire_at guard doesn't prevent the UPDATE, isolating the status guard.
    def cancel_then_delegate(rrule, starts_at, now, tz, **kwargs):
        finish_item(conn, f"r{rid}", "cancel", TODAY, at(24, 14, 30))
        # Restore next_fire_at so the next_fire_at guard won't prevent the UPDATE
        conn.execute("UPDATE reminders SET next_fire_at = %s WHERE id = %s", (original_fire_at, rid))
        return real_next_occurrence(rrule, starts_at, now, tz, **kwargs)

    monkeypatch.setattr("brain.reminders.next_occurrence", cancel_then_delegate)

    # Claim should return empty list because status guard prevents update
    claimed = claim_due_reminders(conn, at(24, 14), TZ)

    assert claimed == []

    # Verify the reminder remains cancelled and next_fire_at was NOT updated
    row = _reminder_row(conn, rid)
    assert row[4] == "cancelled"  # status
    assert row[3] == original_fire_at  # next_fire_at should not have been updated


def test_restore_reminder_makes_it_due_again(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    claim_due_reminders(conn, at(26, 21), TZ)

    restore_reminder(conn, rid, at(26, 21))

    assert [c["id"] for c in claim_due_reminders(conn, at(26, 21, 1), TZ)] == [rid]


def test_claim_skips_a_recurring_reminder_already_done_today_but_still_advances(conn):
    # Ticked off on the list at 12:00, well before its 15:00 ping — the tick
    # must not send a ping for something already done today, but the series
    # still has to move on to tomorrow's occurrence.
    rid = create_reminder(
        conn, "Creatine — 2 scoops", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ,
    )["id"]
    toggle_item(conn, f"r{rid}", TODAY, at(26, 12))

    claimed = claim_due_reminders(conn, at(26, 15), TZ)

    assert claimed == []
    assert _reminder_row(conn, rid)[3] == at(27, 15)


def test_claim_still_sends_a_recurring_reminder_done_on_a_different_day(conn):
    rid = create_reminder(
        conn, "Creatine — 2 scoops", "2026-09-25T15:00", "FREQ=DAILY", at(24, 8), TZ,
    )["id"]
    claim_due_reminders(conn, at(25, 15), TZ)  # fires yesterday's occurrence, advances to today
    toggle_item(conn, f"r{rid}", date(2026, 9, 25), at(25, 16))  # ticked done for yesterday only

    claimed = claim_due_reminders(conn, at(26, 15), TZ)

    assert [c["id"] for c in claimed] == [rid]


# --- display_text (double-emoji stripping) ----------------------------------


def test_display_text_strips_the_categorys_own_leading_emoji():
    assert display_text("💊 Creatine — 2 scoops", "supplement") == "Creatine — 2 scoops"


def test_display_text_leaves_text_without_the_emoji_unchanged():
    assert display_text("Creatine — 2 scoops", "supplement") == "Creatine — 2 scoops"


def test_display_text_leaves_a_different_categorys_emoji_alone():
    assert display_text("💊 Creatine — 2 scoops", "trading") == "💊 Creatine — 2 scoops"


def test_display_text_strips_the_emoji_after_a_tomorrow_prefix():
    assert display_text("Tomorrow: 💳 Grok renews", "subscription") == "Tomorrow: Grok renews"


def test_display_text_strips_the_emoji_after_an_in_n_days_prefix():
    assert display_text("In 5 days: 🎂 Mom's birthday", "birthday") == "In 5 days: Mom's birthday"


def test_display_text_handles_unknown_category_falling_back_to_other():
    assert display_text("📌 Something", None) == "Something"
    assert display_text("📌 Something", "bogus") == "Something"


# --- reminder pings (ping <-> list sync) -------------------------------------


def test_get_reminder_info_returns_text_and_category(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="trading",
    )["id"]

    assert get_reminder_info(conn, rid) == {"text": "Close trades", "category": "trading"}


def test_get_reminder_info_raises_for_unknown_id(conn):
    with pytest.raises(ItemNotFoundError):
        get_reminder_info(conn, 999)


def test_record_ping_and_pings_needing_render(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]

    record_ping(conn, rid, 9001, TODAY, at(26, 21))

    assert pings_needing_render(conn) == []  # not dirty until flagged

    mark_pings_dirty(conn, rid, TODAY)

    pending = pings_needing_render(conn)
    assert len(pending) == 1
    assert pending[0]["reminder_id"] == rid
    assert pending[0]["message_id"] == 9001
    assert pending[0]["fired_on"] == TODAY
    assert pending[0]["due_at"] == at(26, 21)


def test_mark_pings_dirty_only_touches_the_matching_day(conn):
    # A recurring reminder: day-scoping applies (a one-time reminder's ping
    # is flagged regardless of day — see the dedicated test for that below).
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 21))
    record_ping(conn, rid, 9002, date(2026, 9, 25), at(25, 21))

    mark_pings_dirty(conn, rid, TODAY)

    pending_message_ids = [p["message_id"] for p in pings_needing_render(conn)]
    assert pending_message_ids == [9001]


def test_clear_ping_needs_render_and_drop_ping(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 21))
    mark_pings_dirty(conn, rid, TODAY)
    ping_id = pings_needing_render(conn)[0]["id"]

    clear_ping_needs_render(conn, ping_id)
    assert pings_needing_render(conn) == []

    mark_pings_dirty(conn, rid, TODAY)
    drop_ping(conn, ping_id)
    assert pings_needing_render(conn) == []
    row = conn.execute("SELECT COUNT(*) FROM reminder_pings").fetchone()[0]
    assert row == 0


def test_get_reminder_ping_state_for_one_time_reminder(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="trading",
    )["id"]

    assert get_reminder_ping_state(conn, rid, TODAY) == {
        "text": "Close trades", "category": "trading", "done": False,
    }

    mark_reminder_done(conn, rid, TODAY)

    assert get_reminder_ping_state(conn, rid, TODAY)["done"] is True


def test_get_reminder_ping_state_for_recurring_reminder_is_per_day(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ,
    )["id"]
    toggle_item(conn, f"r{rid}", TODAY, at(26, 12))

    assert get_reminder_ping_state(conn, rid, TODAY)["done"] is True
    assert get_reminder_ping_state(conn, rid, date(2026, 9, 27))["done"] is False


def test_get_reminder_ping_state_returns_none_for_unknown_reminder(conn):
    assert get_reminder_ping_state(conn, 999, TODAY) is None


def test_mark_reminder_done_flags_that_days_pings_dirty(conn):
    rid = create_reminder(conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 15))

    mark_reminder_done(conn, rid, TODAY)

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_finish_item_done_flags_that_days_pings_dirty(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 21))

    finish_item(conn, f"r{rid}", "done", TODAY, at(26, 21, 3))

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_toggle_item_untick_flags_that_days_pings_dirty(conn):
    rid = create_reminder(conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(26, 12))  # done
    clear_ping_needs_render(conn, pings_needing_render(conn)[0]["id"])

    toggle_item(conn, f"r{rid}", TODAY, at(26, 13))  # un-tick

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


# --- review fixes: multi-occurrence days, display_text FE0F, ping lookups --


def test_claim_does_not_skip_the_rest_of_an_hourly_reminders_day_once_done_today(conn):
    # Regression: an HOURLY reminder ticked done once must not go silent for
    # the rest of the day — "done today" only means something for a
    # reminder with exactly one occurrence that day.
    rid = create_reminder(conn, "Water", "2026-09-26T08:00", "FREQ=HOURLY", at(26, 7), TZ)["id"]
    claim_due_reminders(conn, at(26, 8), TZ)  # fires 08:00, advances to 09:00
    toggle_item(conn, f"r{rid}", TODAY, at(26, 8, 30))  # ticked done on the list

    claimed = claim_due_reminders(conn, at(26, 9), TZ)

    assert [c["id"] for c in claimed] == [rid]


def test_claim_does_not_skip_the_second_occurrence_of_a_byhour_reminder_once_done_today(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-26T09:00",
        "FREQ=DAILY;BYHOUR=9,21;BYMINUTE=0;BYSECOND=0", at(26, 7), TZ,
    )["id"]
    claim_due_reminders(conn, at(26, 9), TZ)  # fires 09:00, advances to 21:00
    toggle_item(conn, f"r{rid}", TODAY, at(26, 9, 30))  # ticked done after the morning dose

    claimed = claim_due_reminders(conn, at(26, 21), TZ)

    assert [c["id"] for c in claimed] == [rid]


def test_claim_still_skips_a_single_occurrence_reminder_done_today(conn):
    # Unaffected control case — the original behavior must survive.
    rid = create_reminder(conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ)["id"]
    toggle_item(conn, f"r{rid}", TODAY, at(26, 12))

    assert claim_due_reminders(conn, at(26, 15), TZ) == []


def test_mark_pings_dirty_skips_a_multi_occurrence_day_leaving_the_earlier_ping_alone(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-26T09:00",
        "FREQ=DAILY;BYHOUR=9,21;BYMINUTE=0;BYSECOND=0", at(26, 7), TZ,
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 9))   # this morning's ping, already sent
    record_ping(conn, rid, 9002, TODAY, at(26, 21))  # tonight's ping, already sent

    mark_pings_dirty(conn, rid, TODAY)

    # Two pings already exist for today (a multi-occurrence day) — the
    # day-level "done" flag doesn't map to either one specifically, so
    # neither gets flagged for re-rendering.
    assert pings_needing_render(conn) == []


def test_mark_pings_dirty_still_flags_a_single_occurrence_day(conn):
    rid = create_reminder(conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 15))

    mark_pings_dirty(conn, rid, TODAY)

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_mark_pings_dirty_flags_all_pings_of_a_one_time_reminder_regardless_of_day(conn):
    # A one-time reminder finished shortly after midnight (so "today" at
    # click time differs from the day it actually fired on) must still
    # re-render its own ping.
    rid = create_reminder(conn, "Close trades", "2026-09-26T23:55", None, at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 23, 55))

    mark_pings_dirty(conn, rid, date(2026, 9, 27))  # "today" at the moment of the click

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_get_ping_by_message_id_returns_the_row(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 21))

    ping = get_ping_by_message_id(conn, 9001)

    assert ping["reminder_id"] == rid
    assert ping["fired_on"] == TODAY
    assert ping["due_at"] == at(26, 21)


def test_get_ping_by_message_id_returns_none_when_not_found(conn):
    assert get_ping_by_message_id(conn, 999999) is None


def test_mark_ping_needs_render_sets_the_flag_by_ping_id(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    record_ping(conn, rid, 9001, TODAY, at(26, 21))
    ping_id = get_ping_by_message_id(conn, 9001)["id"]

    mark_ping_needs_render(conn, ping_id)

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_prune_old_pings_removes_only_rows_older_than_the_cutoff(conn):
    record_ping(conn, 1, 5001, date(2026, 9, 10), at(10, 9))
    record_ping(conn, 1, 5002, date(2026, 9, 21), at(21, 9))

    removed = prune_old_pings(conn, date(2026, 9, 20))

    assert removed == 1
    remaining = [
        r[0] for r in conn.execute("SELECT message_id FROM reminder_pings").fetchall()
    ]
    assert remaining == [5002]


def test_display_text_strips_the_emoji_when_the_text_is_missing_the_variation_selector():
    # CATEGORIES["fitness"] carries a trailing U+FE0F on its emoji; the
    # user's own text often won't.
    assert display_text("🏋 Gym", "fitness") == "Gym"


def test_display_text_strips_the_emoji_when_the_text_has_an_extra_variation_selector():
    # CATEGORIES["supplement"]'s emoji has no U+FE0F; a client-added one on
    # the user's text shouldn't block the match either.
    assert display_text("💊️ Creatine", "supplement") == "Creatine"


def test_display_text_returns_the_original_text_when_stripping_would_leave_nothing():
    assert display_text("🏋️", "fitness") == "🏋️"


# --- categories -------------------------------------------------------------


def test_create_reminder_defaults_category_to_other(conn):
    created = create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ)
    assert created["category"] == "other"
    assert _reminder_category(conn, created["id"]) == "other"


def test_create_reminder_stores_a_known_category(conn):
    created = create_reminder(
        conn, "Mom's birthday", "2026-09-26T10:00", None, at(26, 10), TZ, category="birthday"
    )
    assert created["category"] == "birthday"


def test_create_reminder_normalizes_category_case_whitespace_and_unknowns(conn):
    created = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="  Birthday  "
    )
    assert created["category"] == "birthday"  # strip + lower

    created2 = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="nonsense"
    )
    assert created2["category"] == "other"


def test_add_todo_defaults_category_to_task_and_normalizes_unknown(conn):
    assert add_todo(conn, "Gym", None, TODAY)["category"] == "task"
    assert add_todo(conn, "Gym", None, TODAY, category="fitness")["category"] == "fitness"
    assert add_todo(conn, "Gym", None, TODAY, category="bogus")["category"] == "task"


def _reminder_category(conn, reminder_id):
    return conn.execute(
        "SELECT category FROM reminders WHERE id = %s", (reminder_id,)
    ).fetchone()[0]


def _reminder_full_row(conn, reminder_id):
    return conn.execute(
        "SELECT text, starts_at, rrule, next_fire_at, status, category, parent_id, lead_days "
        "FROM reminders WHERE id = %s",
        (reminder_id,),
    ).fetchone()


# --- early ("day before") reminders -----------------------------------------


def test_create_reminder_with_early_reminder_creates_both_rows(conn):
    now = datetime(2026, 3, 1, 10, 0, tzinfo=TZ)  # before this year's 10 April
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", now, TZ,
        category="birthday", remind_days_before=1,
    )

    assert created["category"] == "birthday"
    early = created["early"]
    assert early is not None
    assert set(early) == {"id", "text", "next_fire_at"}
    assert early["text"] == "Tomorrow: Giorgi's birthday"
    assert early["next_fire_at"] == datetime(2026, 4, 9, 10, 0, tzinfo=TZ)

    main_row = _reminder_full_row(conn, created["id"])
    early_row = _reminder_full_row(conn, early["id"])
    assert main_row[3] == created["next_fire_at"]
    assert main_row[5] == "birthday"
    assert main_row[6] is None  # parent_id
    assert main_row[7] == 0  # lead_days
    assert early_row[1] == main_row[1]  # same starts_at
    assert early_row[2] == main_row[2]  # same rrule
    assert early_row[5] == "birthday"
    assert early_row[6] == created["id"]  # parent_id
    assert early_row[7] == 1  # lead_days


def test_create_reminder_early_text_for_more_than_one_day(conn):
    created = create_reminder(
        conn, "Pay rent", "2026-10-01T10:00", "FREQ=MONTHLY;BYMONTHDAY=1", at(26, 10), TZ,
        remind_days_before=3,
    )
    assert created["early"]["text"] == "In 3 days: Pay rent"


def test_create_reminder_no_early_reminder_when_omitted_or_zero(conn):
    assert create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ
    )["early"] is None
    assert create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, remind_days_before=0
    )["early"] is None
    assert create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, remind_days_before="0"
    )["early"] is None


def test_create_reminder_accepts_int_valued_float_or_string_remind_days_before(conn):
    # "when" is far enough ahead that the early (day-before) time isn't past.
    assert create_reminder(
        conn, "A", "2026-09-29T21:00", None, at(26, 10), TZ, remind_days_before=1.0
    )["early"]["text"] == "Tomorrow: A"
    assert create_reminder(
        conn, "B", "2026-09-29T21:00", None, at(26, 10), TZ, remind_days_before="1"
    )["early"]["text"] == "Tomorrow: B"


@pytest.mark.parametrize("bad", [31, -1, 1.5, "abc", "2.5"])
def test_create_reminder_rejects_invalid_remind_days_before(conn, bad):
    with pytest.raises(ScheduleInputError):
        create_reminder(
            conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ,
            remind_days_before=bad,
        )
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_create_reminder_one_time_early_in_the_past_creates_main_only(conn):
    # Main is 2026-09-26T21:00, tomorrow's reminder minus 1 day would be
    # 2026-09-25T21:00 — already past "now" (26th 10:00) — no error, just no
    # early reminder.
    created = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, remind_days_before=1
    )
    assert created["early"] is None
    assert created["lead_days"] == 1
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_create_reminder_recurring_bymonthday_early_fires_the_day_before(conn):
    created = create_reminder(
        conn, "Pay rent", "2026-08-28T10:00", "FREQ=MONTHLY;BYMONTHDAY=28", at(26, 10), TZ,
        remind_days_before=1,
    )
    assert created["next_fire_at"] == datetime(2026, 9, 28, 10, 0, tzinfo=TZ)
    assert created["early"]["next_fire_at"] == datetime(2026, 9, 27, 10, 0, tzinfo=TZ)


def test_create_reminder_recurring_rule_ending_within_the_lead_creates_main_only(conn):
    # FREQ=DAILY;COUNT=1 fires exactly once, at dtstart (2026-09-27). The
    # main reminder still fires fine, but there's no occurrence left for
    # the early (5 days before) row to attach to — same "no early
    # reminder, not an error" treatment as a one-time reminder whose early
    # time has already passed.
    created = create_reminder(
        conn, "Short course", "2026-09-27T10:00", "FREQ=DAILY;COUNT=1", at(26, 10), TZ,
        remind_days_before=5,
    )
    assert created["next_fire_at"] == datetime(2026, 9, 27, 10, 0, tzinfo=TZ)
    assert created["early"] is None
    assert created["lead_days"] == 5
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_finish_item_cancel_cascades_to_the_early_reminder(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", at(26, 10), TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    finish_item(conn, f"r{main_id}", "cancel", TODAY, at(26, 12))

    main_row = _reminder_full_row(conn, main_id)
    early_row = _reminder_full_row(conn, early_id)
    assert main_row[4] == "cancelled" and main_row[3] is None
    assert early_row[4] == "cancelled" and early_row[3] is None


def test_finish_item_done_does_not_cascade_to_the_early_reminder(conn):
    created = create_reminder(
        conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ, remind_days_before=1
    )
    main_id, early_id = created["id"], created["early"]["id"]

    finish_item(conn, f"r{main_id}", "done", TODAY, at(26, 12))

    early_row = _reminder_full_row(conn, early_id)
    assert early_row[4] == "active"
    assert early_row[3] is not None


def test_finish_item_cancel_on_early_row_only_cancels_that_row(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", at(26, 10), TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    finish_item(conn, f"r{early_id}", "cancel", TODAY, at(26, 12))

    main_row = _reminder_full_row(conn, main_id)
    early_row = _reminder_full_row(conn, early_id)
    assert main_row[4] == "active"
    assert early_row[4] == "cancelled"


def test_agenda_shows_the_early_reminder_the_day_before_a_recurring_occurrence(conn):
    create_reminder(
        conn, "Market", "2026-09-21T09:30", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", at(21, 8), TZ,
        category="trading", remind_days_before=1,
    )

    sunday = agenda_items(conn, date(2026, 9, 27), TZ)
    assert [(i["text"], i["time"], i["category"]) for i in sunday] == [
        ("Tomorrow: Market", "09:30", "trading")
    ]
    # Monday is also the day before Tuesday's occurrence, so both the main
    # reminder (today) and the early one (for tomorrow) legitimately show.
    monday = agenda_items(conn, date(2026, 9, 28), TZ)
    assert [(i["text"], i["time"]) for i in monday] == [
        ("Market", "09:30"), ("Tomorrow: Market", "09:30"),
    ]


def test_agenda_shows_a_one_time_early_reminder_on_the_previous_day(conn):
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=1
    )
    early_id = created["early"]["id"]

    day_before = agenda_items(conn, date(2026, 10, 4), TZ)
    assert [i["id"] for i in day_before] == [f"r{early_id}"]
    assert day_before[0]["text"] == "Tomorrow: Concert"

    on_the_day = agenda_items(conn, date(2026, 10, 5), TZ)
    assert [i["text"] for i in on_the_day] == ["Concert"]


def test_agenda_items_include_category(conn):
    add_todo(conn, "Gym", None, TODAY, category="fitness")
    item = agenda_items(conn, TODAY, TZ)[0]
    assert item["category"] == "fitness"


def test_agenda_groups_by_category_order_then_existing_order_within_category(conn):
    add_todo(conn, "Read", None, TODAY, category="learning")  # rank 5
    create_reminder(
        conn, "Vitamins", "2026-09-26T08:00", None, at(26, 7), TZ, category="supplement"
    )  # rank 2
    create_reminder(
        conn, "Mom's birthday", "2026-09-26T10:00", None, at(26, 7), TZ, category="birthday"
    )  # rank 0

    items = agenda_items(conn, TODAY, TZ)

    assert [i["category"] for i in items] == ["birthday", "supplement", "learning"]


def test_snooze_recurring_early_reminder_creates_a_one_time_copy_with_no_lead_or_parent(conn):
    created = create_reminder(
        conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ,
        category="trading", remind_days_before=1,
    )
    early_id = created["early"]["id"]

    snooze_reminder(conn, early_id, 60, at(25, 21))

    copy = conn.execute(
        "SELECT text, rrule, category, lead_days, parent_id FROM reminders WHERE id <> %s AND id <> %s",
        (created["id"], early_id),
    ).fetchone()
    assert copy == ("Tomorrow: Market", None, "trading", 0, None)


def test_snooze_one_time_early_reminder_clears_lead_days_so_it_shows_on_the_snoozed_day(conn):
    # A one-time early row keeps the SAME starts_at/rrule as its main
    # reminder and relies on lead_days to shift its occurrence back by N
    # days. Once it's snoozed, starts_at IS its real (new) fire time, so
    # lead_days must drop to 0 or occurrences_on would subtract the lead
    # again and show it a day early / miss it on the actual day.
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=1
    )
    early_id = created["early"]["id"]
    assert created["early"]["next_fire_at"] == datetime(2026, 10, 4, 19, 0, tzinfo=TZ)

    until = snooze_reminder(conn, early_id, 60, datetime(2026, 10, 4, 19, 0, tzinfo=TZ))
    assert until == datetime(2026, 10, 4, 20, 0, tzinfo=TZ)

    row = _reminder_full_row(conn, early_id)
    assert row[7] == 0  # lead_days
    assert row[6] == created["id"]  # parent_id untouched, cancel cascade still works

    same_day = agenda_items(conn, date(2026, 10, 4), TZ)
    assert [(i["text"], i["time"]) for i in same_day] == [("Tomorrow: Concert", "20:00")]
    assert agenda_items(conn, date(2026, 10, 3), TZ) == []


# --- untimed ("no time") reminders -------------------------------------


def _reminder_timed(conn, reminder_id):
    return conn.execute(
        "SELECT timed, next_fire_at, starts_at FROM reminders WHERE id = %s", (reminder_id,)
    ).fetchone()


def test_create_untimed_daily_reminder_has_no_next_fire_and_midnight_starts_at(conn):
    created = create_reminder(
        conn, "Stretch", "2026-09-26T15:30", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )

    assert created["timed"] is False
    assert created["next_fire_at"] is None
    timed, next_fire_at, starts_at = _reminder_timed(conn, created["id"])
    assert timed is False
    assert next_fire_at is None
    assert starts_at == datetime(2026, 9, 26, 0, 0, tzinfo=TZ)


def test_create_untimed_one_time_reminder(conn):
    created = create_reminder(
        conn, "Return library book", "2026-09-28", None, at(26, 10), TZ, timed=False,
    )

    assert created["timed"] is False
    assert created["next_fire_at"] is None
    timed, next_fire_at, starts_at = _reminder_timed(conn, created["id"])
    assert timed is False
    assert next_fire_at is None
    assert starts_at == datetime(2026, 9, 28, 0, 0, tzinfo=TZ)


def test_create_untimed_one_time_rejects_a_past_date(conn):
    with pytest.raises(ScheduleInputError):
        create_reminder(conn, "Too late", "2026-09-25", None, at(26, 10), TZ, timed=False)
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_create_untimed_one_time_allows_today(conn):
    created = create_reminder(
        conn, "Today thing", "2026-09-26", None, at(26, 10), TZ, timed=False,
    )
    assert created["next_fire_at"] is None


def test_create_untimed_recurring_requires_an_occurrence_on_or_after_today(conn):
    # FREQ=DAILY;COUNT=1 with DTSTART in the past never occurs today or later.
    with pytest.raises(ScheduleInputError):
        create_reminder(
            conn, "Old course", "2026-09-20", "FREQ=DAILY;COUNT=1", at(26, 10), TZ, timed=False,
        )
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_untimed_reminder_is_never_claimed_due(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T15:30", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    assert claim_due_reminders(conn, at(26, 23, 59), TZ) == []
    assert claim_due_reminders(conn, at(30, 12, 0), TZ) == []
    assert _reminder_timed(conn, rid)[1] is None  # next_fire_at still NULL


def test_agenda_shows_untimed_reminder_with_no_time_and_sorted_after_timed(conn):
    create_reminder(conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, category="admin")
    create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ,
        category="admin", timed=False,
    )
    add_todo(conn, "Gym", None, TODAY, category="admin")

    items = agenda_items(conn, TODAY, TZ)

    assert [(i["text"], i["time"], i["kind"]) for i in items] == [
        ("Close trades", "21:00", "reminder"),
        ("Stretch", None, "reminder"),
        ("Gym", None, "todo"),
    ]


def test_agenda_shows_untimed_recurring_reminder_on_every_occurrence_day(conn):
    create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )

    for day in (TODAY, date(2026, 9, 27), date(2026, 9, 30)):
        items = agenda_items(conn, day, TZ)
        assert [(i["text"], i["time"]) for i in items] == [("Stretch", None)]


def test_toggle_untimed_recurring_reminder_only_affects_today(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is True
    assert _reminder_timed(conn, rid) is not None
    row = conn.execute("SELECT last_done_on FROM reminders WHERE id = %s", (rid,)).fetchone()
    assert row[0] == TODAY
    assert toggle_item(conn, f"r{rid}", TODAY, at(26, 12)) is False


def test_toggle_untimed_one_time_reminder_marks_it_done(conn):
    rid = create_reminder(
        conn, "Return library book", "2026-09-28", None, at(26, 10), TZ, timed=False,
    )["id"]

    assert toggle_item(conn, f"r{rid}", date(2026, 9, 28), at(28, 12)) is True
    status = conn.execute("SELECT status FROM reminders WHERE id = %s", (rid,)).fetchone()[0]
    assert status == "done"


def test_create_untimed_early_reminder_inherits_timed_false(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10", "FREQ=YEARLY",
        datetime(2026, 3, 1, 10, 0, tzinfo=TZ), TZ, timed=False, remind_days_before=1,
    )

    early = created["early"]
    assert early is not None
    early_timed = conn.execute(
        "SELECT timed, next_fire_at FROM reminders WHERE id = %s", (early["id"],)
    ).fetchone()
    assert early_timed == (False, None)


# --- edit_reminder ----------------------------------------------------


def _full_row(conn, reminder_id):
    return conn.execute(
        "SELECT text, starts_at, rrule, next_fire_at, status, category, timed, lead_days "
        "FROM reminders WHERE id = %s",
        (reminder_id,),
    ).fetchone()


def test_edit_reminder_time_only_moves_a_daily_reminder(conn):
    rid = create_reminder(conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    # Editing before 15:00 today should move it to today at 15:00.
    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", time="15:00")

    assert result["next_fire_at"] == at(26, 15)
    row = _full_row(conn, rid)
    assert row[1] == at(26, 15)  # starts_at date unchanged, time updated
    assert row[3] == at(26, 15)


def test_edit_reminder_time_only_after_the_new_time_today_moves_to_tomorrow(conn):
    rid = create_reminder(conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    # now is already past 15:00 today, so the next occurrence is tomorrow.
    result = edit_reminder(conn, at(26, 16), TZ, item_id=f"r{rid}", time="15:00")

    assert result["next_fire_at"] == at(27, 15)


def test_edit_reminder_by_name_matches_a_single_active_main_reminder(conn):
    rid = create_reminder(conn, "Duolingo lesson", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, name="duolingo", time="15:00")

    assert result["id"] == rid


def test_edit_reminder_by_name_no_match_raises_not_found(conn):
    create_reminder(conn, "Duolingo lesson", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)

    with pytest.raises(ItemNotFoundError):
        edit_reminder(conn, at(26, 10), TZ, name="piano", time="15:00")


def test_edit_reminder_by_name_several_matches_raises_ambiguous(conn):
    r1 = create_reminder(conn, "Pay rent", "2026-09-26T19:00", None, at(26, 10), TZ)["id"]
    r2 = create_reminder(conn, "Pay Giorgi back", "2026-09-27T19:00", None, at(26, 10), TZ)["id"]

    with pytest.raises(AmbiguousItemError) as exc_info:
        edit_reminder(conn, at(26, 10), TZ, name="pay", time="15:00")

    matches = dict(exc_info.value.matches)
    assert set(matches) == {r1, r2}


_MARCH_1 = datetime(2026, 3, 1, 10, 0, tzinfo=TZ)


def test_edit_reminder_editing_an_early_row_id_edits_its_parent(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", _MARCH_1, TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    result = edit_reminder(conn, _MARCH_1, TZ, item_id=f"r{early_id}", text="Giorgi's bday")

    assert result["id"] == main_id
    assert _full_row(conn, main_id)[0] == "Giorgi's bday"


def test_edit_reminder_moving_a_yearly_birthdays_date_moves_its_early_row(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", _MARCH_1, TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    result = edit_reminder(
        conn, _MARCH_1, TZ, item_id=f"r{main_id}", when="2026-04-11T10:00",
    )

    assert result["next_fire_at"] == datetime(2026, 4, 11, 10, 0, tzinfo=TZ)
    early_row = _full_row(conn, early_id)
    assert early_row[3] == datetime(2026, 4, 10, 10, 0, tzinfo=TZ)  # a day before the 11th


def test_edit_reminder_text_change_updates_the_linked_early_rows_text(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", _MARCH_1, TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    edit_reminder(conn, _MARCH_1, TZ, item_id=f"r{main_id}", text="Giorgi's bday")

    assert _full_row(conn, early_id)[0] == "Tomorrow: Giorgi's bday"


def test_edit_reminder_repeat_none_makes_it_one_time(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", repeat="none")

    assert result["rrule"] is None
    assert _full_row(conn, rid)[2] is None


def test_edit_reminder_timed_to_untimed_clears_next_fire_at_on_main_and_early(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", _MARCH_1, TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    result = edit_reminder(conn, _MARCH_1, TZ, item_id=f"r{main_id}", no_time=True)

    assert result["timed"] is False
    assert result["next_fire_at"] is None
    main_row = _full_row(conn, main_id)
    early_row = _full_row(conn, early_id)
    assert main_row[6] is False and main_row[3] is None
    assert early_row[6] is False and early_row[3] is None
    # starts_at keeps its date, just at local midnight.
    assert main_row[1] == datetime(2026, 4, 10, 0, 0, tzinfo=TZ)


def test_edit_reminder_untimed_to_timed_requires_a_time(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    with pytest.raises(ScheduleInputError):
        edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", no_time=False)

    assert _full_row(conn, rid)[6] is False  # untouched


def test_edit_reminder_untimed_to_timed_with_a_time_works(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", no_time=False, time="08:00")

    assert result["timed"] is True
    # now (10:00) is already past 08:00 today, so the next occurrence is tomorrow.
    assert result["next_fire_at"] == at(27, 8)


def test_edit_reminder_setting_time_on_an_untimed_reminder_makes_it_timed(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", time="08:00")

    assert result["timed"] is True
    # now (10:00) is already past 08:00 today, so the next occurrence is tomorrow.
    assert result["next_fire_at"] == at(27, 8)


def test_edit_reminder_on_a_cancelled_reminder_fails(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    finish_item(conn, f"r{rid}", "cancel", TODAY, at(26, 12))

    with pytest.raises(ItemNotFoundError):
        edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", time="15:00")


def test_edit_reminder_with_no_fields_fails(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    with pytest.raises(ScheduleInputError):
        edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}")


def test_edit_reminder_an_invalid_field_leaves_the_row_untouched(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    before = _full_row(conn, rid)

    with pytest.raises(ScheduleInputError):
        edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", time="not-a-time")

    assert _full_row(conn, rid) == before


def test_edit_reminder_category_is_normalized_like_create(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", category="trading")

    assert result["category"] == "trading"
    assert _full_row(conn, rid)[5] == "trading"


def test_edit_reminder_moving_main_earlier_that_pushes_its_early_row_into_the_past_still_succeeds(conn):
    # The main reminder still fires fine after the move, but its early
    # (day-before-ish) offset now lands in the past — that must not fail
    # the whole edit, only null out that one early row's next_fire_at.
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=3,
    )
    main_id, early_id = created["id"], created["early"]["id"]
    assert created["early"]["next_fire_at"] == datetime(2026, 10, 2, 19, 0, tzinfo=TZ)

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{main_id}", when="2026-09-27T19:00")

    assert result["next_fire_at"] == at(27, 19)
    assert result["early"] == [{"id": early_id, "text": "In 3 days: Concert", "next_fire_at": None}]
    assert _full_row(conn, early_id)[3] is None


def test_edit_reminder_marks_list_dirty(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    clear_list_dirty(conn)

    edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", time="15:00")

    assert _dirty(conn)


# --- review fixes: date-only `when` on a timed reminder (finding 1) --------


def test_edit_reminder_date_only_when_keeps_the_current_time_of_day(conn):
    rid = create_reminder(conn, "Giorgi's birthday", "2027-04-10T10:00", "FREQ=YEARLY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", when="2027-04-11")

    assert result["next_fire_at"] == datetime(2027, 4, 11, 10, 0, tzinfo=TZ)


def test_edit_reminder_date_only_when_does_not_wrongly_reject_as_already_passed(conn):
    rid = create_reminder(conn, "Close trades", "2026-09-27T21:00", None, at(26, 10), TZ)["id"]

    # "now" is the morning of the 27th — local midnight has passed, but the
    # reminder's real (kept) time of 21:00 has not.
    result = edit_reminder(conn, at(27, 5), TZ, item_id=f"r{rid}", when="2026-09-27")

    assert result["next_fire_at"] == at(27, 21)


def test_edit_reminder_when_and_time_together_applies_time_on_whens_date(conn):
    rid = create_reminder(conn, "Duolingo", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", when="2026-10-01", time="07:30")

    assert result["next_fire_at"] == datetime(2026, 10, 1, 7, 30, tzinfo=TZ)


def test_edit_reminder_date_only_when_keeps_time_for_early_rows_too(conn):
    created = create_reminder(
        conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", _MARCH_1, TZ,
        remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]

    result = edit_reminder(conn, _MARCH_1, TZ, item_id=f"r{main_id}", when="2026-04-11")

    assert result["next_fire_at"] == datetime(2026, 4, 11, 10, 0, tzinfo=TZ)
    assert _full_row(conn, early_id)[3] == datetime(2026, 4, 10, 10, 0, tzinfo=TZ)


# --- review fixes: blank strings mean "not provided" (finding 2) -----------


def test_edit_reminder_blank_repeat_leaves_the_rule_unchanged(conn):
    rid = create_reminder(conn, "Giorgi's birthday", "2026-04-10T10:00", "FREQ=YEARLY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", repeat="  ", category="birthday")

    assert result["rrule"] == "FREQ=YEARLY"


def test_edit_reminder_literal_none_case_insensitive_clears_the_rule(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", repeat=" NoNe ")

    assert result["rrule"] is None


def test_edit_reminder_blank_text_leaves_text_unchanged(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", text="   ", category="trading")

    assert result["text"] == "Market"


def test_edit_reminder_blank_category_leaves_category_unchanged(conn):
    rid = create_reminder(
        conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ, category="trading",
    )["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", category="  ", text="Market check")

    assert result["category"] == "trading"


def test_edit_reminder_blank_when_and_time_are_treated_as_not_provided(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(
        conn, at(26, 10), TZ, item_id=f"r{rid}", when=" ", time=" ", category="trading",
    )

    assert result["next_fire_at"] == at(26, 21)  # untouched


def test_edit_reminder_all_blank_fields_asks_what_to_change(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    with pytest.raises(ScheduleInputError):
        edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", text="   ", when="  ")


def test_create_reminder_blank_remind_days_before_means_no_early_reminder(conn):
    created = create_reminder(
        conn, "Close trades", "2026-09-26T21:00", None, at(26, 10), TZ, remind_days_before="   ",
    )
    assert created["early"] is None
    assert created["lead_days"] == 0


# --- review fixes: name matching (findings 3 and 9) -------------------------


def test_edit_reminder_by_name_excludes_an_expired_one_time_match(conn):
    create_reminder(
        conn, "Creatine — 2 scoops", "2026-09-01T08:00", "FREQ=DAILY", at(1, 10), TZ,
    )
    # A one-time "copy" with the same text that was valid when created but
    # has since fallen into the past by the time we edit.
    create_reminder(conn, "Creatine — 2 scoops", "2026-09-20T08:00", None, at(1, 10), TZ)

    result = edit_reminder(conn, at(26, 10), TZ, name="creatine", time="09:00")

    assert _full_row(conn, result["id"])[2] == "FREQ=DAILY"  # matched the recurring one


def test_edit_reminder_by_name_prefers_an_exact_match_over_a_partial_one(conn):
    exact_id = create_reminder(conn, "Gym", "2026-09-26T18:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    create_reminder(conn, "Gym membership renewal", "2026-09-30T10:00", None, at(26, 10), TZ)

    result = edit_reminder(conn, at(26, 10), TZ, name="gym", time="19:00")

    assert result["id"] == exact_id


def test_edit_reminder_by_name_prefers_a_recurring_match_over_a_one_time_duplicate(conn):
    recurring_id = create_reminder(
        conn, "Creatine — 2 scoops", "2026-09-01T08:00", "FREQ=DAILY", at(1, 10), TZ,
    )["id"]
    # A "snooze copy" style one-time reminder with the exact same text, still in the future.
    create_reminder(conn, "Creatine — 2 scoops", "2026-09-27T08:00", None, at(26, 10), TZ)

    result = edit_reminder(conn, at(26, 10), TZ, name="Creatine — 2 scoops", time="09:00")

    assert result["id"] == recurring_id


def test_edit_reminder_by_name_still_ambiguous_when_two_recurring_matches_tie(conn):
    r1 = create_reminder(
        conn, "Pay rent", "2026-09-26T19:00", "FREQ=MONTHLY;BYMONTHDAY=26", at(26, 10), TZ,
    )["id"]
    r2 = create_reminder(
        conn, "Pay rent", "2026-09-27T19:00", "FREQ=MONTHLY;BYMONTHDAY=27", at(26, 10), TZ,
    )["id"]

    with pytest.raises(AmbiguousItemError) as exc_info:
        edit_reminder(conn, at(26, 10), TZ, name="Pay rent", time="20:00")

    matches = dict(exc_info.value.matches)
    assert set(matches) == {r1, r2}


def test_edit_reminder_by_name_escapes_underscore_wildcard(conn):
    a_id = create_reminder(conn, "Pay Jon_Doe back", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    create_reminder(conn, "Pay JonXDoe back", "2026-09-27T19:00", None, at(26, 10), TZ)

    result = edit_reminder(conn, at(26, 10), TZ, name="Jon_Doe", time="20:00")

    assert result["id"] == a_id


def test_edit_reminder_by_name_escapes_percent_wildcard(conn):
    a_id = create_reminder(conn, "Save 10% tip", "2026-09-26T19:00", "FREQ=DAILY", at(26, 10), TZ)["id"]
    create_reminder(conn, "Save 10XYZ999 tip", "2026-09-27T19:00", None, at(26, 10), TZ)

    result = edit_reminder(conn, at(26, 10), TZ, name="10% tip", time="20:00")

    assert result["id"] == a_id


# --- review fix: no_time=False on an already-timed reminder (finding 4) ----


def test_edit_reminder_no_time_false_on_an_already_timed_reminder_is_a_noop(conn):
    rid = create_reminder(conn, "Market", "2026-09-26T21:00", "FREQ=DAILY", at(26, 10), TZ)["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", no_time=False, category="trading")

    assert result["timed"] is True
    assert result["next_fire_at"] == at(26, 21)  # unchanged time-of-day


# --- review fix: untimed reply date must not be stale (finding 5) ----------


def test_edit_reminder_untimed_next_on_list_ignores_a_stale_starts_at(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-01T00:00", "FREQ=DAILY", at(1, 10), TZ, timed=False,
    )["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", category="fitness")

    assert result["next_on_list"] == datetime(2026, 9, 26, 0, 0, tzinfo=TZ)


def test_create_untimed_reminder_next_on_list_for_a_daily_reminder_starting_weeks_ago(conn):
    created = create_reminder(
        conn, "Stretch", "2026-09-01", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )

    assert created["next_on_list"] == datetime(2026, 9, 26, 0, 0, tzinfo=TZ)


# --- review fix: `when` with a time makes an untimed reminder timed (finding 6) ---


def test_edit_reminder_when_with_a_time_component_on_untimed_makes_it_timed(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    result = edit_reminder(conn, at(26, 10), TZ, item_id=f"r{rid}", when="2026-09-27T08:00")

    assert result["timed"] is True
    assert result["next_fire_at"] == datetime(2026, 9, 27, 8, 0, tzinfo=TZ)


# --- review fix: early-row children edge cases (finding 7) -----------------


def test_edit_reminder_does_not_rewrite_a_snoozed_one_time_early_copy(conn):
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]
    snooze_reminder(conn, early_id, 60, datetime(2026, 10, 4, 19, 0, tzinfo=TZ))
    before = _full_row(conn, early_id)
    assert before[7] == 0  # lead_days dropped to 0 by the snooze

    edit_reminder(conn, at(26, 10), TZ, item_id=f"r{main_id}", when="2026-10-06T19:00")

    # The snoozed copy (lead_days=0) must be left completely untouched.
    assert _full_row(conn, early_id) == before


def test_edit_reminder_revives_a_done_one_time_early_row_when_main_date_changes(conn):
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]
    finish_item(conn, f"r{early_id}", "done", TODAY, at(26, 12))
    assert _full_row(conn, early_id)[4] == "done"

    edit_reminder(conn, at(26, 10), TZ, item_id=f"r{main_id}", when="2026-10-06T19:00")

    early_row = _full_row(conn, early_id)
    assert early_row[4] == "active"
    assert early_row[3] == datetime(2026, 10, 5, 19, 0, tzinfo=TZ)  # a day before the 6th


def test_edit_reminder_does_not_revive_a_done_early_row_when_date_time_unchanged(conn):
    created = create_reminder(
        conn, "Concert", "2026-10-05T19:00", None, at(26, 10), TZ, remind_days_before=1,
    )
    main_id, early_id = created["id"], created["early"]["id"]
    finish_item(conn, f"r{early_id}", "done", TODAY, at(26, 12))

    edit_reminder(conn, at(26, 10), TZ, item_id=f"r{main_id}", category="content")

    assert _full_row(conn, early_id)[4] == "done"  # left alone


# --- review fix: restore_reminder must never revive an untimed row (finding 8) ---


def test_restore_reminder_does_not_restore_an_untimed_reminder(conn):
    rid = create_reminder(
        conn, "Stretch", "2026-09-26T00:00", "FREQ=DAILY", at(26, 10), TZ, timed=False,
    )["id"]

    restore_reminder(conn, rid, at(26, 15))

    assert _reminder_timed(conn, rid)[1] is None


def test_daily_list_state_defaults_and_round_trips(conn):
    assert get_daily_list_state(conn) == {
        "list_date": None, "message_id": None, "needs_refresh": False, "weather_line": None,
    }
    add_todo(conn, "Gym", None, TODAY)  # marks dirty
    assert get_daily_list_state(conn)["needs_refresh"] is True

    save_daily_list_state(conn, TODAY, 12345)
    # save_daily_list_state does not touch needs_refresh
    state = get_daily_list_state(conn)
    assert state["list_date"] == TODAY
    assert state["message_id"] == 12345
    assert state["needs_refresh"] is True


def test_save_daily_list_state_stores_and_updates_the_weather_line(conn):
    save_daily_list_state(conn, TODAY, 12345, "☀️ Lisbon today: 18–25°C · no rain expected")

    assert get_daily_list_state(conn)["weather_line"] == "☀️ Lisbon today: 18–25°C · no rain expected"

    # A same-day repost (e.g. after the pinned message was deleted) can carry
    # the same stored line forward unchanged.
    save_daily_list_state(conn, TODAY, 67890, "☀️ Lisbon today: 18–25°C · no rain expected")

    assert get_daily_list_state(conn)["weather_line"] == "☀️ Lisbon today: 18–25°C · no rain expected"


def test_is_early_reminder_text_matches_the_tomorrow_and_in_n_days_prefixes(conn):
    assert is_early_reminder_text("Tomorrow: Pay rent") is True
    assert is_early_reminder_text("In 3 days: Pay rent") is True
    assert is_early_reminder_text("Pay rent") is False

    clear_list_dirty(conn)
    assert get_daily_list_state(conn)["needs_refresh"] is False


# --- find_reminders ---------------------------------------------------------

def test_describe_repeat_shapes():
    from brain.reminders import describe_repeat
    assert describe_repeat(None) == "once"
    assert describe_repeat("FREQ=YEARLY") == "yearly"
    assert describe_repeat("FREQ=MONTHLY") == "monthly"
    assert describe_repeat("FREQ=DAILY") == "daily"
    assert describe_repeat("FREQ=DAILY;INTERVAL=15") == "every 15 days"
    assert describe_repeat("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "weekdays"
    assert describe_repeat("FREQ=WEEKLY;BYDAY=WE,SA") == "every Wed, Sat"


def test_find_reminders_by_name_returns_next_date_and_early_flag(conn):
    from brain.reminders import find_reminders
    create_reminder(conn, "🎂 Lazares Kenchadze's birthday", "2026-10-26T10:00", "FREQ=YEARLY",
                    at(26, 10), TZ, category="birthday", remind_days_before=1)
    create_reminder(conn, "💳 Grok renews", "2026-09-28T10:00", "FREQ=MONTHLY",
                    at(26, 10), TZ, category="subscription")
    found = find_reminders(conn, at(26, 12), TZ, query="lazare")
    assert len(found) == 1
    item = found[0]
    assert item["next"] == datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
    assert item["repeat"] == "yearly" and item["has_early"] is True
    assert item["category"] == "birthday"


def test_find_reminders_by_category_sorted_soonest_first_and_excludes_early_rows(conn):
    from brain.reminders import find_reminders
    create_reminder(conn, "🎂 B birthday", "2026-12-11T10:00", "FREQ=YEARLY",
                    at(26, 10), TZ, category="birthday", remind_days_before=1)
    create_reminder(conn, "🎂 A birthday", "2026-10-26T10:00", "FREQ=YEARLY",
                    at(26, 10), TZ, category="birthday", remind_days_before=1)
    create_reminder(conn, "💳 Grok renews", "2026-09-28T10:00", "FREQ=MONTHLY",
                    at(26, 10), TZ, category="subscription")
    found = find_reminders(conn, at(26, 12), TZ, category="birthday")
    assert [f["text"] for f in found] == ["🎂 A birthday", "🎂 B birthday"]


def test_find_reminders_skips_cancelled_and_passed_one_time(conn):
    from brain.reminders import find_reminders
    rid = create_reminder(conn, "Old thing", "2026-09-26T21:00", None, at(26, 10), TZ)["id"]
    finish_item(conn, f"r{rid}", "cancel", TODAY, at(26, 12))
    create_reminder(conn, "Done soon", "2026-09-26T13:00", None, at(26, 10), TZ)
    assert find_reminders(conn, at(26, 14), TZ) == []


def test_find_reminders_untimed_daily_shows_today(conn):
    from brain.reminders import find_reminders
    create_reminder(conn, "📚 Duolingo", "2026-09-20", "FREQ=DAILY", at(20, 8), TZ,
                    category="learning", timed=False)
    found = find_reminders(conn, at(26, 15), TZ, query="duolingo")
    assert found[0]["next"].date() == TODAY and found[0]["timed"] is False


def test_find_reminders_text_lists_ids_dates_and_handles_no_match(conn):
    from brain.reminders import find_reminders, find_reminders_text
    create_reminder(conn, "🎂 Lazares Kenchadze's birthday", "2026-10-26T10:00", "FREQ=YEARLY",
                    at(26, 10), TZ, category="birthday", remind_days_before=1)
    text = find_reminders_text(find_reminders(conn, at(26, 12), TZ, query="lazare"), "lazare", None)
    assert "Lazares Kenchadze's birthday — next Mon 26 Oct 2026, 10:00" in text
    assert "yearly · + day-before reminder" in text
    assert "[r" in text
    assert "No active reminders match" in find_reminders_text([], "zzz", None)


def test_create_reminder_tool_tells_the_model_not_to_invent_times():
    from brain.reminders import CREATE_REMINDER_TOOL

    desc = CREATE_REMINDER_TOOL["description"]
    props = CREATE_REMINDER_TOOL["parameters_json_schema"]["properties"]
    assert "never make up a time" in desc.lower()
    assert "once per reminder" in desc.lower()
    assert "never make up a time" in props["no_time"]["description"].lower()
    assert "always set" in props["category"]["description"].lower()


def test_edit_reminder_only_changes_category_when_asked():
    from brain.reminders import EDIT_REMINDER_TOOL

    category = EDIT_REMINDER_TOOL["parameters_json_schema"]["properties"]["category"]
    assert "only when the user asks" in category["description"].lower()
