import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from brain.daily_list import (
    ButtonOutcome,
    ButtonSpec,
    ListRender,
    PingCard,
    active_ping_card,
    agenda_text,
    apply_button,
    done_ping_card,
    effective_list_time,
    render_list,
    snoozed_ping_card,
    today_items_prompt,
)
from brain.reminders import (
    ItemNotFoundError,
    add_todo,
    clear_ping_needs_render,
    create_reminder,
    get_ping_by_message_id,
    get_reminder_ping_state,
    pings_needing_render,
    record_ping,
    save_daily_list_state,
    toggle_item,
)

TZ = ZoneInfo("Asia/Tbilisi")
TODAY = date(2026, 9, 27)  # a Sunday


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _stub_weather_fetches(monkeypatch):
    # Every tick test in this file that sets a "location" fact but isn't
    # itself testing weather behavior (e.g. a plain non-fitness ping test)
    # would otherwise fall through to the real forecast_day_line/
    # forecast_hour_line and hit the live Open-Meteo API — a real bug a
    # reviewer caught. Default both to a safe "no weather" stub here; a
    # test that specifically exercises weather re-patches the same names
    # with its own fake, which simply overrides this default for its
    # duration.
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: None)
    monkeypatch.setattr("brain.daily_list.forecast_hour_line", lambda location, at: None)


def item(id, text, time=None, done=False, since=None, category=None):
    kind = "todo" if id.startswith("t") else "reminder"
    return {
        "id": id, "kind": kind, "text": text, "time": time, "done": done,
        "since": since, "category": category,
    }


# --- effective_list_time -------------------------------------------------


def test_effective_list_time_moves_earlier_for_a_first_reminder_at_six():
    items = [item("r1", "Gym", time="06:00")]
    assert effective_list_time(items, time(9, 0), 30) == time(5, 30)


def test_effective_list_time_moves_earlier_for_a_first_reminder_at_nine():
    items = [item("r1", "Curcuma", time="09:00")]
    assert effective_list_time(items, time(9, 0), 30) == time(8, 30)


def test_effective_list_time_caps_at_list_time_when_first_reminder_is_later():
    items = [item("r1", "Afternoon check-in", time="15:00")]
    assert effective_list_time(items, time(9, 0), 30) == time(9, 0)


def test_effective_list_time_falls_back_to_list_time_with_no_timed_items():
    items = [item("t1", "Buy milk", time=None)]
    assert effective_list_time(items, time(9, 0), 30) == time(9, 0)


def test_effective_list_time_with_no_items_at_all():
    assert effective_list_time([], time(9, 0), 30) == time(9, 0)


def test_effective_list_time_clamps_at_local_midnight():
    items = [item("r1", "Very early", time="00:15")]
    assert effective_list_time(items, time(9, 0), 30) == time(0, 0)


def test_effective_list_time_counts_a_done_timed_item():
    items = [item("r1", "Gym", time="06:00", done=True)]
    assert effective_list_time(items, time(9, 0), 30) == time(5, 30)


def test_effective_list_time_with_zero_lead_uses_the_first_time_itself():
    items = [item("r1", "Gym", time="06:00")]
    assert effective_list_time(items, time(9, 0), 0) == time(6, 0)


def test_effective_list_time_uses_the_earliest_of_several_timed_items():
    items = [item("r1", "Later", time="09:00"), item("r2", "Earlier", time="06:00")]
    assert effective_list_time(items, time(9, 0), 30) == time(5, 30)


def test_effective_list_time_ignores_untimed_items_and_todos():
    items = [
        item("t1", "Buy milk", time=None),
        item("r1", "Untimed reminder", time=None),
        item("r2", "Gym", time="06:00"),
    ]
    assert effective_list_time(items, time(9, 0), 30) == time(5, 30)


# --- render_list --------------------------------------------------------


def test_render_list_title_is_the_full_weekday_day_and_month():
    render = render_list([], TODAY)
    assert render.title == "📋 Sunday, 27 September"


def test_render_list_empty():
    render = render_list([], TODAY)
    assert render.body == "Nothing on the list — all clear. 🎉"
    assert render.footer == ""
    assert render.buttons == []


def test_render_list_single_category_still_shows_its_header():
    items = [
        item("t1", "Vitamins", category="supplement"),
        item("t2", "Fish oil", category="supplement"),
    ]

    render = render_list(items, TODAY)

    assert render.body.splitlines() == [
        "**💊 Supplements**",
        "**1**  Vitamins",
        "**2**  Fish oil",
    ]


def test_render_list_groups_by_category_with_blank_line_between_groups_and_continuous_numbering():
    items = [
        item("r1", "Mom's birthday", time="10:00", category="birthday"),
        item("t1", "Vitamins", category="supplement"),
        item("t2", "Fish oil", category="supplement"),
    ]

    render = render_list(items, TODAY)

    assert render.body.splitlines() == [
        "**🎂 Birthdays & Anniversaries**",
        "**1**  `10:00`  Mom's birthday",
        "",
        "**💊 Supplements**",
        "**2**  Vitamins",
        "**3**  Fish oil",
    ]


def test_render_list_open_timed_line_format():
    render = render_list([item("r5", "Creatine — 2 scoops", time="15:00", category="supplement")], TODAY)
    assert "**1**  `15:00`  Creatine — 2 scoops" in render.body.splitlines()


def test_render_list_open_untimed_todo_line_format():
    render = render_list([item("t7", "Make and schedule video", category="content")], TODAY)
    assert "**1**  Make and schedule video" in render.body.splitlines()


def test_render_list_open_todo_keeps_since_suffix():
    render = render_list(
        [item("t4", "Call the bank", since=date(2026, 9, 23), category="task")], TODAY
    )
    assert "↪ since Wed" in render.body


def test_render_list_uses_a_date_for_items_carried_a_week_or_more():
    render = render_list(
        [item("t1", "Old", since=date(2026, 9, 18), category="task")], TODAY
    )
    assert "↪ since 18 Sep" in render.body


def test_render_list_done_line_format_has_no_number_and_no_time():
    render = render_list(
        [item("t3", "Curcuma — 1 pill", done=True, category="supplement")], TODAY
    )
    assert render.body.splitlines() == ["**💊 Supplements**", "✅  ~~Curcuma — 1 pill~~"]


def test_render_list_strips_the_categorys_own_leading_emoji_from_item_text():
    render = render_list(
        [item("t1", "💊 Creatine — 2 scoops", category="supplement")], TODAY
    )
    assert "💊 Creatine" not in render.body
    assert "**1**  Creatine — 2 scoops" in render.body.splitlines()


def test_render_list_strips_the_emoji_after_a_tomorrow_prefix():
    render = render_list(
        [item("r1", "Tomorrow: 💳 Grok renews — $30.00 (Visa)", done=True, category="subscription")],
        TODAY,
    )
    assert "✅  ~~Tomorrow: Grok renews — $30.00 (Visa)~~" in render.body.splitlines()


def test_render_list_shows_the_weather_line_first_then_a_blank_line_then_the_groups():
    items = [item("t1", "Vitamins", category="supplement")]

    render = render_list(items, TODAY, weather="☀️ Lisbon today: 18–25°C · no rain expected")

    assert render.body.splitlines()[:3] == [
        "☀️ Lisbon today: 18–25°C · no rain expected",
        "",
        "**💊 Supplements**",
    ]


def test_render_list_omits_the_weather_line_when_none():
    items = [item("t1", "Vitamins", category="supplement")]

    render = render_list(items, TODAY, weather=None)

    assert render.body.splitlines()[0] == "**💊 Supplements**"


def test_render_list_empty_day_ignores_a_weather_line():
    render = render_list([], TODAY, weather="☀️ Lisbon today: 18–25°C · no rain expected")

    assert render.body == "Nothing on the list — all clear. 🎉"


def test_render_list_truncation_budget_accounts_for_the_weather_line():
    weather = "☀️ Lisbon today: 18–25°C · no rain expected"
    items = [item(f"t{n}", f"Task number {n} " + "x" * 60, category="task") for n in range(1, 41)]

    render = render_list(items, TODAY, weather=weather)

    assert render.body.startswith(weather + "\n\n")
    assert len(render.body) <= 1900


def test_render_list_footer_counts_done_out_of_all():
    items = [
        item("t1", "A", done=True, category="task"),
        item("t2", "B", done=False, category="task"),
        item("t3", "C", done=False, category="task"),
    ]
    render = render_list(items, TODAY)
    assert render.footer == "1 of 3 done"


def test_render_list_buttons_labels_and_styles():
    items = [
        item("t1", "Gym", done=True, category="task"),
        item("t2", "Bank", done=False, category="task"),
    ]
    render = render_list(items, TODAY)
    assert render.buttons == [
        ButtonSpec("list:t1", "✓1", True),
        ButtonSpec("list:t2", "2", False),
    ]


def test_render_list_buttons_number_matches_position_even_for_done_lines():
    items = [
        item("t1", "A", done=True, category="task"),
        item("t2", "B", done=False, category="task"),
        item("t3", "C", done=True, category="task"),
    ]
    render = render_list(items, TODAY)
    assert [b.label for b in render.buttons] == ["✓1", "2", "✓3"]


def test_render_list_caps_buttons_at_25_and_content_length():
    items = [item(f"t{n}", f"Task number {n} " + "x" * 60, category="task") for n in range(1, 41)]

    render = render_list(items, TODAY)

    assert len(render.body) <= 1900
    assert "more" in render.body.splitlines()[-1]
    assert len(render.buttons) <= 25

    visible_item_lines = [
        line for line in render.body.splitlines()
        if line.startswith("**") and not line.endswith("**")
    ]
    assert len(render.buttons) == len(visible_item_lines)

    footer_match = re.search(r"… and (\d+) more", render.body.splitlines()[-1])
    assert footer_match
    assert int(footer_match.group(1)) == 40 - len(visible_item_lines)
    # The footer field still counts every item, truncated or not.
    assert render.footer == "0 of 40 done"


def test_render_list_no_truncation_with_30_short_items():
    items = [item(f"t{n}", f"Task {n}", category="task") for n in range(1, 31)]

    render = render_list(items, TODAY)

    assert len(render.buttons) == 25
    assert len(render.body) <= 1900
    assert "more" not in render.body.splitlines()[-1]
    item_lines = [
        line for line in render.body.splitlines()
        if line.startswith("**") and not line.endswith("**")
    ]
    assert len(item_lines) == 30


def test_render_list_truncation_never_leaves_an_orphaned_header_or_blank_line_before_the_footer():
    items = [item(f"t{n}", f"Task {n}", category="trading") for n in range(1, 21)]
    items.append(item("t99", "x" * 2000, category="admin"))

    render = render_list(items, TODAY)

    lines = render.body.splitlines()
    assert lines[-1].startswith("… and")
    assert lines[-2] != ""
    assert not lines[-2].startswith("**") or not lines[-2].endswith("**")
    assert "Admin" not in render.body


def test_render_list_truncation_keeps_buttons_matching_visible_items_across_categories():
    items = []
    for n in range(1, 21):
        items.append(item(f"r{n}", f"Reminder number {n} " + "x" * 60, category="trading"))
    for n in range(1, 21):
        items.append(item(f"t{n}", f"Task number {n} " + "x" * 60, category="task"))

    render = render_list(items, TODAY)

    assert len(render.body) <= 1900
    assert "more" in render.body.splitlines()[-1]
    header_lines = [
        line for line in render.body.splitlines() if line.startswith("**") and line.endswith("**")
    ]
    assert len(header_lines) >= 1

    visible_item_lines = [
        line for line in render.body.splitlines()
        if line.startswith("**") and not line.endswith("**")
    ]
    assert len(render.buttons) == len(visible_item_lines)


# --- agenda_text / today_items_prompt -----------------------------------


def test_agenda_text_mentions_empty_days():
    assert "nothing" in agenda_text([], TODAY).lower()


def test_agenda_text_without_ids_matches_render_list_title_and_body():
    items = [item("r5", "Close trades", time="21:00", category="trading"), item("t3", "Gym", category="task")]
    render = render_list(items, TODAY)
    assert agenda_text(items, TODAY) == f"{render.title}\n\n{render.body}"


def test_agenda_text_with_ids_includes_each_items_id():
    items = [
        item("r5", "Close trades", time="21:00", category="trading"),
        item("t3", "Gym", done=True, category="fitness"),
    ]

    text = agenda_text(items, TODAY, with_ids=True)

    assert "[r5]" in text
    assert "[t3]" in text
    assert "Close trades" in text and "Gym" in text


def test_agenda_text_with_ids_matches_render_list_once_the_id_is_removed():
    items = [
        item("r5", "Close trades", time="21:00", category="trading"),
        item("t3", "Gym", done=True, category="fitness"),
        item("t4", "Call the bank", since=date(2026, 9, 22), category="task"),
    ]

    plain_body_lines = render_list(items, TODAY).body.splitlines()
    id_lines = agenda_text(items, TODAY, with_ids=True).splitlines()[2:]

    item_plain_lines = [l for l in plain_body_lines if not (l.startswith("**") and l.endswith("**")) and l != ""]
    item_id_lines = [l for l in id_lines if not (l.startswith("**") and l.endswith("**")) and l != ""]
    assert len(item_plain_lines) == len(item_id_lines)
    for plain, with_id, it in zip(item_plain_lines, item_id_lines, items):
        assert with_id.replace(f"[{it['id']}] ", "") == plain


def test_agenda_text_with_ids_on_empty_day_is_unchanged():
    assert agenda_text([], TODAY, with_ids=True) == agenda_text([], TODAY)


def test_today_items_prompt():
    assert today_items_prompt([]) == ""
    prompt = today_items_prompt([
        item("r5", "Close trades", time="21:00", category="trading"),
        item("t3", "Gym", done=True, category="fitness"),
    ])
    assert "finish_item" in prompt
    assert "- [r5] 21:00 Close trades" in prompt
    assert "- [t3] Gym (done)" in prompt


def test_today_items_prompt_uses_display_text():
    prompt = today_items_prompt([item("t1", "💊 Vitamins", category="supplement")])
    assert "- [t1] Vitamins" in prompt


# --- apply_button --------------------------------------------------------


def test_apply_button_done_on_a_reminder(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]

    outcome = apply_button(conn, f"rem:done:{rid}", at(27, 21, 3), TZ)

    assert outcome.kind == "reminder"
    assert outcome.card == PingCard(
        "~~Close trades~~", "✅ Done at 21:03 · 📈 Trading", 0x4E5058, False,
    )
    status = conn.execute("SELECT status FROM reminders WHERE id = %s", (rid,)).fetchone()[0]
    assert status == "done"


def test_apply_button_snooze_on_a_reminder(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]

    outcome = apply_button(conn, f"rem:s10:{rid}", at(27, 21, 3), TZ)

    assert outcome.card.title == "Close trades"
    assert outcome.card.description == "💤 Snoozed until 21:13 · 📈 Trading"
    assert outcome.card.buttons is False


def test_apply_button_toggles_a_list_item_and_rerenders(conn):
    tid = add_todo(conn, "Gym", None, TODAY, category="fitness")["id"]
    save_daily_list_state(conn, TODAY, 777)

    outcome = apply_button(conn, f"list:t{tid}", at(27, 12), TZ)

    assert outcome.kind == "list"
    assert "✅  ~~Gym~~" in outcome.list_render.body
    assert outcome.list_render.buttons[0].done is True
    assert outcome.list_message_id == 777


def test_apply_button_on_the_pinned_list_from_yesterday_returns_its_id(conn):
    tid = add_todo(conn, "Gym", None, TODAY, category="fitness")["id"]
    save_daily_list_state(conn, date(2026, 9, 26), 555)  # yesterday's pinned list

    outcome = apply_button(conn, f"list:t{tid}", at(27, 12), TZ)

    assert outcome.kind == "list"
    assert "✅  ~~Gym~~" in outcome.list_render.body
    # The click was on the message that's still pinned, even though it was
    # posted for yesterday's list — edit it in place with today's list.
    assert outcome.list_message_id == 555
    done_at = conn.execute("SELECT done_at FROM todos WHERE id = %s", (tid,)).fetchone()[0]
    assert done_at is not None


def test_apply_button_list_branch_rerenders_with_todays_stored_weather_line(conn):
    tid = add_todo(conn, "Gym", None, TODAY, category="fitness")["id"]
    save_daily_list_state(conn, TODAY, 777, "☀️ Lisbon today: 18–25°C · no rain expected")

    outcome = apply_button(conn, f"list:t{tid}", at(27, 12), TZ)

    assert outcome.list_render.body.startswith(
        "☀️ Lisbon today: 18–25°C · no rain expected\n\n"
    )


def test_apply_button_list_branch_omits_weather_stored_for_a_different_day(conn):
    tid = add_todo(conn, "Gym", None, TODAY, category="fitness")["id"]
    # A stale row: weather was stored for yesterday's list_date, which must
    # never leak onto today's re-render.
    save_daily_list_state(conn, date(2026, 9, 26), 555, "☀️ yesterday's weather line")

    outcome = apply_button(conn, f"list:t{tid}", at(27, 12), TZ)

    assert not outcome.list_render.body.startswith("☀️")


def test_apply_button_unknown_item_raises(conn):
    with pytest.raises(ItemNotFoundError):
        apply_button(conn, "rem:done:999", at(27, 12), TZ)
    with pytest.raises(ItemNotFoundError):
        apply_button(conn, "list:t999", at(27, 12), TZ)
    with pytest.raises(ItemNotFoundError):
        apply_button(conn, "bogus", at(27, 12), TZ)


# --- review fixes: title clipping, message-id-aware apply_button ---------


def test_active_ping_card_clips_a_long_title_and_keeps_the_full_text_in_the_description():
    long_text = "x" * 500
    card = active_ping_card(long_text, "other", "15:00", None)

    assert len(card.title) <= 256
    assert card.title.endswith("…")
    assert card.description.startswith(long_text)
    assert card.description.endswith("`15:00` · 📌 Other")


def test_active_ping_card_leaves_a_short_title_alone():
    card = active_ping_card("Creatine — 2 scoops", "supplement", "15:00", None)
    assert card.title == "Creatine — 2 scoops"
    assert card.description == "`15:00` · 💊 Supplements"


def test_active_ping_card_shows_the_weather_as_the_last_description_line():
    card = active_ping_card(
        "Gym", "fitness", "06:00", None, weather="🌦️ 14°C (feels 12°C) · rain 40% · wind 18 km/h",
    )
    assert card.description == (
        "`06:00` · 🏋️ Fitness\n🌦️ 14°C (feels 12°C) · rain 40% · wind 18 km/h"
    )


def test_active_ping_card_omits_the_weather_line_when_none():
    card = active_ping_card("Gym", "fitness", "06:00", None)
    assert "\n" not in card.description


def test_done_ping_card_clips_a_long_title_leaving_room_for_the_strikethrough():
    long_text = "x" * 500
    card = done_ping_card(long_text, "other", "15:06")

    assert len(card.title) <= 256
    assert card.title.startswith("~~") and card.title.endswith("…~~")
    assert card.description.startswith(long_text)


def test_snoozed_ping_card_clips_a_long_title():
    long_text = "x" * 500
    card = snoozed_ping_card(long_text, "other", "15:16")

    assert len(card.title) <= 256
    assert card.title.endswith("…")
    assert card.description.startswith(long_text)


def test_apply_button_reminder_done_marks_that_days_pings_dirty(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))

    apply_button(conn, f"rem:done:{rid}", at(27, 15, 6), TZ)

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]


def test_apply_button_done_on_yesterdays_ping_marks_yesterday_not_today(conn):
    # Regression: clicking Done on a stale (yesterday's) ping must not mark
    # TODAY done for a recurring reminder — that would wrongly skip today's
    # occurrence (see claim_due_reminders' done-today skip).
    rid = create_reminder(
        conn, "Creatine", "2026-09-26T15:00", "FREQ=DAILY", at(26, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 8001, date(2026, 9, 26), at(26, 15))  # yesterday's ping

    apply_button(conn, f"rem:done:{rid}", at(27, 9, 0), TZ, message_id=8001)

    row = conn.execute("SELECT last_done_on FROM reminders WHERE id = %s", (rid,)).fetchone()
    assert row[0] == date(2026, 9, 26)


def test_apply_button_done_falls_back_to_today_when_the_message_has_no_ping_row(conn):
    # Old, pre-deploy pings (or a chat "done") have no reminder_pings row —
    # falls back to today, matching the pre-fix behavior.
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]

    apply_button(conn, f"rem:done:{rid}", at(27, 15, 6), TZ, message_id=424242)

    row = conn.execute("SELECT last_done_on FROM reminders WHERE id = %s", (rid,)).fetchone()
    assert row[0] == TODAY


def test_apply_button_done_clears_needs_render_for_the_clicked_message_immediately(conn):
    # The clicked ping's own message is about to be edited directly by the
    # caller — it shouldn't also sit flagged for the next tick to re-render
    # (which would just change "Done at HH:MM" to the tick's own minute).
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))

    apply_button(conn, f"rem:done:{rid}", at(27, 15, 6), TZ, message_id=9001)

    assert pings_needing_render(conn) == []


def test_apply_button_snooze_drops_the_clicked_pings_row(conn):
    # A snoozed card must never get its buttons back via the ping-sync tick
    # — dropping its row takes it out of that mechanism entirely; the
    # snooze copy's later ping gets its own new row when it fires.
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))

    apply_button(conn, f"rem:s10:{rid}", at(27, 15, 1), TZ, message_id=9001)

    assert get_ping_by_message_id(conn, 9001) is None


def test_apply_button_snooze_then_untick_leaves_the_dropped_ping_alone(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    apply_button(conn, f"rem:s10:{rid}", at(27, 15, 1), TZ, message_id=9001)

    apply_button(conn, f"list:r{rid}", at(27, 16), TZ)  # tick done
    apply_button(conn, f"list:r{rid}", at(27, 17), TZ)  # un-tick

    assert pings_needing_render(conn) == []


def test_apply_button_list_toggle_restores_pings_after_an_untick(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    apply_button(conn, f"list:r{rid}", at(27, 12), TZ)  # tick done before it fires
    from brain.reminders import clear_ping_needs_render
    clear_ping_needs_render(conn, pings_needing_render(conn)[0]["id"])

    apply_button(conn, f"list:r{rid}", at(27, 13), TZ)  # un-tick

    assert [p["message_id"] for p in pings_needing_render(conn)] == [9001]
    assert get_reminder_ping_state(conn, rid, TODAY)["done"] is False


import asyncio
from datetime import time

from brain.daily_list import ListMessageMissing, PingMessageMissing, run_scheduler_tick
from brain.reminders import get_daily_list_state

NINE = time(9, 0)


class FakeSender:
    def __init__(self, fail_send=False, missing_edit=False, missing_ping=False, broken_ping=False):
        self.fail_send = fail_send
        self.missing_edit = missing_edit
        self.missing_ping = missing_ping
        self.broken_ping = broken_ping
        self.reminders = []
        self.repings = []
        self.nudges = []
        self.weather_lines = []
        self.posted = []
        self.edited = []
        self.unpinned = []
        self.pings_edited = []
        self.deleted = []
        self._next_id = 1000

    async def send_reminder(
        self, reminder_id, text, category, time_label, late_for, weather=None,
        ping_number=1, ping_total=None,
    ):
        if self.fail_send:
            raise RuntimeError("discord down")
        self._next_id += 1
        if ping_number > 1:
            self.repings.append((reminder_id, text, category, time_label, ping_number, ping_total))
            self.weather_lines.append(weather)
            return self._next_id
        self.reminders.append((reminder_id, text, category, time_label, late_for))
        self.weather_lines.append(weather)
        return self._next_id

    async def edit_ping(self, message_id, reminder_id, card):
        if self.missing_ping:
            raise PingMessageMissing(message_id)
        if self.broken_ping:
            raise RuntimeError("discord hiccup")
        self.pings_edited.append((message_id, reminder_id, card))

    async def delete_ping(self, message_id):
        self.deleted.append(message_id)

    async def post_list(self, render):
        self._next_id += 1
        self.posted.append((self._next_id, render))
        return self._next_id

    async def edit_list(self, message_id, render):
        if self.missing_edit:
            raise ListMessageMissing(message_id)
        self.edited.append((message_id, render))

    async def unpin(self, message_id):
        self.unpinned.append(message_id)

    async def send_nudge(self, lines):
        if self.fail_send:
            raise RuntimeError("discord down")
        self._next_id += 1
        self.nudges.append(lines)
        return self._next_id


def tick(conn, sender, now):
    asyncio.run(run_scheduler_tick(conn, sender, now, TZ, NINE))


def test_tick_fires_a_due_reminder_exactly_once(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]
    sender = FakeSender()

    tick(conn, sender, at(27, 21, 0))
    tick(conn, sender, at(27, 21, 1))

    assert sender.reminders == [(rid, "Close trades", "trading", "21:00", None)]


def test_tick_records_a_ping_after_sending(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]
    sender = FakeSender()

    tick(conn, sender, at(27, 21, 0))

    row = conn.execute(
        "SELECT reminder_id, message_id, fired_on FROM reminder_pings"
    ).fetchone()
    assert row[0] == rid
    assert row[1] is not None
    assert row[2] == TODAY


def test_tick_restores_a_reminder_when_sending_fails(conn):
    rid = create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]

    tick(conn, FakeSender(fail_send=True), at(27, 21))
    sender = FakeSender()
    tick(conn, sender, at(27, 21, 1))

    assert sender.reminders == [(rid, "Close trades", "trading", "21:00", None)]


def test_tick_marks_late_reminders(conn):
    create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )
    sender = FakeSender()

    tick(conn, sender, at(27, 21, 30))

    assert sender.reminders[0][4] == "21:00"


def test_tick_skips_a_recurring_reminder_already_ticked_off_today(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    toggle_item(conn, f"r{rid}", TODAY, at(27, 12))  # ticked done on the list at noon
    sender = FakeSender()

    tick(conn, sender, at(27, 15))

    assert sender.reminders == []


def test_tick_deletes_a_ping_ticked_done_in_the_list(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))  # ticked done from the list, marks ping dirty
    sender = FakeSender()

    tick(conn, sender, at(27, 16, 30))

    assert sender.deleted == [9001] and sender.pings_edited == []
    assert pings_needing_render(conn) == []


def test_tick_renders_a_dirty_ping_as_active_again_after_an_untick(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 17))  # un-tick
    sender = FakeSender()

    tick(conn, sender, at(27, 17, 30))

    assert len(sender.pings_edited) == 1
    _message_id, _reminder_id, card = sender.pings_edited[0]
    assert card.title == "Creatine"
    assert card.buttons is True
    # Regression: the restore must not compute lateness against the tick's
    # own time (over 2 hours after the 15:00 due time here) — it would
    # always show "late" for a restore that happens any real time after.
    assert "late" not in card.description


def test_tick_drops_a_ping_row_when_the_message_was_deleted(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))
    sender = FakeSender(missing_ping=True)

    tick(conn, sender, at(27, 16, 30))

    assert conn.execute("SELECT COUNT(*) FROM reminder_pings").fetchone()[0] == 0


def test_tick_keeps_a_ping_dirty_when_editing_it_fails_unexpectedly(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16, 5))  # un-tick: the card is redrawn active
    sender = FakeSender(broken_ping=True)

    tick(conn, sender, at(27, 16, 30))  # must not raise — logged and left dirty

    assert len(pings_needing_render(conn)) == 1


def test_sync_pings_survives_a_concurrent_toggle_during_the_edit(conn):
    # Lost-update regression: needs_render must be cleared BEFORE the edit,
    # not after — otherwise a toggle that lands while we're mid-edit gets
    # clobbered by our own after-the-fact clear.
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))
    toggle_item(conn, f"r{rid}", TODAY, at(27, 16))  # un-tick: marks the ping dirty

    class RacySender(FakeSender):
        async def edit_ping(self, message_id, reminder_id, card):
            # Simulate a concurrent tick landing while this edit is
            # still in flight.
            toggle_item(conn, f"r{reminder_id}", TODAY, at(27, 16, 1))
            await super().edit_ping(message_id, reminder_id, card)

    tick(conn, RacySender(), at(27, 16, 30))

    assert 9001 in [p["message_id"] for p in pings_needing_render(conn)]


def test_tick_prunes_ping_rows_older_than_7_days(conn):
    record_ping(conn, 1, 5001, date(2026, 9, 10), at(10, 9))  # 17 days before TODAY
    record_ping(conn, 1, 5002, date(2026, 9, 21), at(21, 9))  # 6 days before TODAY
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))

    remaining = [
        r[0] for r in conn.execute("SELECT message_id FROM reminder_pings").fetchall()
    ]
    assert remaining == [5002]


def test_tick_still_syncs_the_list_when_syncing_pings_fails(conn, monkeypatch):
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr(
        "brain.daily_list.pings_needing_render",
        lambda conn: (_ for _ in ()).throw(RuntimeError("pings sync exploded")),
    )
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))  # must not raise

    assert len(sender.posted) == 1


def test_tick_does_nothing_with_the_list_before_list_time(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()

    tick(conn, sender, at(27, 8, 59))

    assert sender.posted == []


def test_tick_posts_the_list_once_after_list_time(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))
    tick(conn, sender, at(27, 9, 1))

    assert len(sender.posted) == 1
    assert "Gym" in sender.posted[0][1].body
    state = get_daily_list_state(conn)
    assert state["list_date"] == TODAY
    assert state["message_id"] == sender.posted[0][0]


def test_tick_posts_once_when_started_after_list_time(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()

    tick(conn, sender, at(27, 14, 0))

    assert len(sender.posted) == 1


def test_tick_records_an_empty_day_without_posting(conn):
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))

    assert sender.posted == []
    assert get_daily_list_state(conn)["list_date"] == TODAY


def test_tick_unpins_yesterdays_list_on_a_new_day(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    save_daily_list_state(conn, date(2026, 9, 26), 555)
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))

    assert sender.unpinned == [555]
    assert len(sender.posted) == 1


def test_tick_refreshes_the_list_when_dirty(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()
    tick(conn, sender, at(27, 9, 0))
    message_id = sender.posted[0][0]

    add_todo(conn, "Call the bank", None, TODAY, category="task")
    tick(conn, sender, at(27, 9, 1))
    tick(conn, sender, at(27, 9, 2))

    assert len(sender.edited) == 1
    assert sender.edited[0][0] == message_id
    assert "Call the bank" in sender.edited[0][1].body


def test_tick_posts_the_list_when_the_first_item_arrives_after_an_empty_morning(conn):
    sender = FakeSender()
    tick(conn, sender, at(27, 9, 0))

    add_todo(conn, "Gym", None, TODAY, category="task")
    tick(conn, sender, at(27, 12, 0))

    assert len(sender.posted) == 1


def test_tick_reposts_when_the_list_message_was_deleted(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    tick(conn, FakeSender(), at(27, 9, 0))
    add_todo(conn, "More", None, TODAY, category="task")
    sender = FakeSender(missing_edit=True)

    tick(conn, sender, at(27, 9, 1))

    assert len(sender.posted) == 1
    assert get_daily_list_state(conn)["message_id"] == sender.posted[0][0]


def test_tick_keeps_the_list_dirty_when_editing_fails_unexpectedly(conn):
    add_todo(conn, "Gym", None, TODAY, category="task")
    tick(conn, FakeSender(), at(27, 9, 0))
    add_todo(conn, "More", None, TODAY, category="task")

    class BrokenEditSender(FakeSender):
        async def edit_list(self, message_id, render):
            raise RuntimeError("discord hiccup")

    with pytest.raises(RuntimeError):
        tick(conn, BrokenEditSender(), at(27, 9, 1))

    assert get_daily_list_state(conn)["needs_refresh"] is True


def test_tick_leaves_the_list_alone_before_list_time_even_if_dirty(conn):
    save_daily_list_state(conn, date(2026, 9, 26), 555)
    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()

    tick(conn, sender, at(27, 7, 0))

    assert sender.posted == [] and sender.edited == [] and sender.unpinned == []


def test_tick_does_not_post_before_the_lead_adjusted_time_of_an_early_reminder(conn):
    create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )
    sender = FakeSender()

    tick(conn, sender, at(27, 5, 29))

    assert sender.posted == []


def test_tick_posts_at_the_lead_adjusted_time_of_an_early_reminder(conn):
    create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )
    sender = FakeSender()

    tick(conn, sender, at(27, 5, 30))

    assert len(sender.posted) == 1
    assert "Gym" in sender.posted[0][1].body


def test_tick_caps_the_post_time_at_list_time_for_a_later_first_reminder(conn):
    create_reminder(
        conn, "Afternoon check-in", "2026-09-27T15:00", None, at(27, 5), TZ, category="admin",
    )
    sender = FakeSender()

    tick(conn, sender, at(27, 8, 59))
    assert sender.posted == []

    tick(conn, sender, at(27, 9, 0))
    assert len(sender.posted) == 1


def test_tick_unpins_yesterdays_list_at_the_new_lead_adjusted_effective_time(conn):
    create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )
    save_daily_list_state(conn, date(2026, 9, 26), 555)
    sender = FakeSender()

    tick(conn, sender, at(27, 5, 29))
    assert sender.unpinned == []

    tick(conn, sender, at(27, 5, 30))
    assert sender.unpinned == [555]


def test_tick_respects_a_custom_lead_minutes_argument(conn):
    create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )
    sender = FakeSender()

    asyncio.run(run_scheduler_tick(conn, sender, at(27, 4, 59), TZ, NINE, 60))
    assert sender.posted == []

    asyncio.run(run_scheduler_tick(conn, sender, at(27, 5, 0), TZ, NINE, 60))
    assert len(sender.posted) == 1


def test_tick_keeps_a_change_made_while_posting(conn):
    from brain.reminders import mark_list_dirty

    add_todo(conn, "Gym", None, TODAY, category="task")
    sender = FakeSender()

    class DirtyingSender(FakeSender):
        async def post_list(self, render):
            # Simulate a concurrent chat edit during the post
            mark_list_dirty(conn)
            return await super().post_list(render)

    tick(conn, DirtyingSender(), at(27, 9, 0))

    assert get_daily_list_state(conn)["needs_refresh"] is True


def test_tick_keeps_sending_other_reminders_when_a_restore_fails(conn, monkeypatch):
    from brain.daily_list import restore_reminder

    rid1 = create_reminder(
        conn, "First", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]
    rid2 = create_reminder(
        conn, "Second", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )["id"]

    class SelectiveFailSender(FakeSender):
        async def send_reminder(self, reminder_id, text, category, time_label, late_for, weather=None):
            if reminder_id == rid1:
                raise RuntimeError("send failed")
            return await super().send_reminder(
                reminder_id, text, category, time_label, late_for, weather
            )

    monkeypatch.setattr(
        "brain.daily_list.restore_reminder",
        lambda conn, id, due_at: (_ for _ in ()).throw(RuntimeError("restore also failed")),
    )

    sender = SelectiveFailSender()
    tick(conn, sender, at(27, 21))

    assert len(sender.reminders) == 1
    assert sender.reminders[0][0] == rid2
    assert sender.reminders[0][1] == "Second"


# --- weather: morning list ------------------------------------------------


_DAY_WEATHER = "☀️ Lisbon today: 18–25°C · no rain expected"


def test_tick_new_day_post_includes_the_weather_line_and_stores_it(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: _DAY_WEATHER)
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))

    assert sender.posted[0][1].body.startswith(_DAY_WEATHER + "\n\n")
    assert get_daily_list_state(conn)["weather_line"] == _DAY_WEATHER


def test_tick_refresh_reuses_the_stored_weather_line_without_refetching(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: _DAY_WEATHER)
    sender = FakeSender()
    tick(conn, sender, at(27, 9, 0))

    def _boom(location, day, tz):
        raise AssertionError("forecast_day_line must not be refetched on a same-day refresh")

    monkeypatch.setattr("brain.daily_list.forecast_day_line", _boom)
    add_todo(conn, "Call the bank", None, TODAY, category="task")
    tick(conn, sender, at(27, 9, 1))
    tick(conn, sender, at(27, 9, 2))

    assert sender.edited[0][1].body.startswith(_DAY_WEATHER + "\n\n")


def test_tick_new_day_post_has_no_weather_line_when_location_is_unset(conn, monkeypatch):
    add_todo(conn, "Gym", None, TODAY, category="task")

    def _boom(location, day, tz):
        raise AssertionError("forecast_day_line must not be called without a location fact")

    monkeypatch.setattr("brain.daily_list.forecast_day_line", _boom)
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))

    assert not sender.posted[0][1].body.startswith("☀️")
    assert get_daily_list_state(conn)["weather_line"] is None


def test_tick_new_day_post_has_no_weather_line_when_the_fetch_fails(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: None)
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))  # must not raise / must not block the post

    assert len(sender.posted) == 1
    assert "Gym" in sender.posted[0][1].body
    assert get_daily_list_state(conn)["weather_line"] is None


def test_tick_empty_day_never_fetches_weather(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")

    def _boom(location, day, tz):
        raise AssertionError("forecast_day_line must not be called for an empty day (no post anyway)")

    monkeypatch.setattr("brain.daily_list.forecast_day_line", _boom)
    sender = FakeSender()

    tick(conn, sender, at(27, 9, 0))  # must not raise

    assert sender.posted == []


def test_tick_retries_a_null_weather_line_on_a_refresh_within_an_hour_of_the_list_time(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: None)
    sender = FakeSender()
    tick(conn, sender, at(27, 9, 0))  # 09:00 fetch fails, list posted without weather
    assert get_daily_list_state(conn)["weather_line"] is None

    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: _DAY_WEATHER)
    add_todo(conn, "Call the bank", None, TODAY, category="task")  # marks the list dirty
    tick(conn, sender, at(27, 9, 30))  # 09:30 — still within list_time (09:00) + 1h

    assert sender.edited[-1][1].body.startswith(_DAY_WEATHER + "\n\n")
    assert get_daily_list_state(conn)["weather_line"] == _DAY_WEATHER


def test_tick_does_not_retry_a_null_weather_line_after_the_retry_window(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list.forecast_day_line", lambda location, day, tz: None)
    sender = FakeSender()
    tick(conn, sender, at(27, 9, 0))  # 09:00 fetch fails, list posted without weather

    def _boom(location, day, tz):
        raise AssertionError("forecast_day_line must not be retried outside the retry window")

    monkeypatch.setattr("brain.daily_list.forecast_day_line", _boom)
    add_todo(conn, "Call the bank", None, TODAY, category="task")  # marks the list dirty
    tick(conn, sender, at(27, 10, 30))  # more than list_time (09:00) + 1h later

    assert not sender.edited[-1][1].body.startswith("☀️")
    assert get_daily_list_state(conn)["weather_line"] is None


def test_tick_new_day_weather_fetch_exceeding_the_cap_does_not_delay_the_post(conn, monkeypatch):
    import time

    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    add_todo(conn, "Gym", None, TODAY, category="task")
    monkeypatch.setattr("brain.daily_list._WEATHER_FETCH_TIMEOUT_SECONDS", 0.05)

    def _slow(location, day, tz):
        time.sleep(1)
        return _DAY_WEATHER

    monkeypatch.setattr("brain.daily_list.forecast_day_line", _slow)

    # Measure up to the moment post_list is actually called, not the whole
    # tick() call: asyncio.run()'s own cleanup waits for the (uncancellable,
    # blocking) 1s sleep's background thread to finish before it returns —
    # that's a test-harness artifact of using asyncio.run per tick, not a
    # delay the production event loop (which stays alive across ticks)
    # would ever impose on the post itself.
    started = time.monotonic()
    elapsed_at_post = {}

    class TimingSender(FakeSender):
        async def post_list(self, render):
            elapsed_at_post["value"] = time.monotonic() - started
            return await super().post_list(render)

    sender = TimingSender()
    tick(conn, sender, at(27, 9, 0))

    assert elapsed_at_post["value"] < 0.9  # capped at ~0.05s, nowhere near the 1s sleep
    assert len(sender.posted) == 1
    assert "Gym" in sender.posted[0][1].body
    assert not sender.posted[0][1].body.startswith(_DAY_WEATHER)


# --- weather: fitness pings ------------------------------------------------


_HOUR_WEATHER = "🌦️ 14°C (feels 12°C) · rain 40% · wind 18 km/h"


def test_tick_fitness_ping_includes_the_weather_line(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    rid = create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )["id"]
    monkeypatch.setattr("brain.daily_list.forecast_hour_line", lambda location, at: _HOUR_WEATHER)
    sender = FakeSender()

    tick(conn, sender, at(27, 6, 0))

    assert sender.reminders == [(rid, "Gym", "fitness", "06:00", None)]
    assert sender.weather_lines == [_HOUR_WEATHER]


def test_tick_non_fitness_ping_never_fetches_weather(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    create_reminder(
        conn, "Close trades", "2026-09-27T21:00", None, at(27, 10), TZ, category="trading",
    )

    def _boom(location, at):
        raise AssertionError("forecast_hour_line must not be called for a non-fitness ping")

    monkeypatch.setattr("brain.daily_list.forecast_hour_line", _boom)
    sender = FakeSender()

    tick(conn, sender, at(27, 21, 0))

    assert len(sender.reminders) == 1
    assert sender.weather_lines == [None]


def test_tick_fitness_ping_fetch_failure_still_sends_the_ping(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    rid = create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )["id"]
    monkeypatch.setattr("brain.daily_list.forecast_hour_line", lambda location, at: None)
    sender = FakeSender()

    tick(conn, sender, at(27, 6, 0))  # must not raise / must not skip the ping

    assert sender.reminders == [(rid, "Gym", "fitness", "06:00", None)]
    assert sender.weather_lines == [None]


def test_tick_fitness_early_reminder_ping_has_no_weather(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    created = create_reminder(
        conn, "Gym class", "2026-09-28T06:00", None, at(27, 5), TZ, category="fitness",
        remind_days_before=1,
    )
    early_id = created["early"]["id"]
    assert created["early"]["text"] == "Tomorrow: Gym class"

    def _boom(location, at):
        raise AssertionError("forecast_hour_line must not be called for an early ping")

    monkeypatch.setattr("brain.daily_list.forecast_hour_line", _boom)
    sender = FakeSender()

    tick(conn, sender, at(27, 6, 0))  # fires the early ("day before") ping only

    assert sender.reminders == [(early_id, "Tomorrow: Gym class", "fitness", "06:00", None)]
    assert sender.weather_lines == [None]


def test_tick_memoizes_the_hour_weather_fetch_for_same_hour_fitness_reminders(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    rid1 = create_reminder(
        conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )["id"]
    rid2 = create_reminder(
        conn, "Stretch", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness",
    )["id"]
    calls = []

    def _counting(location, at_):
        calls.append((location, at_))
        return _HOUR_WEATHER

    monkeypatch.setattr("brain.daily_list.forecast_hour_line", _counting)
    sender = FakeSender()

    tick(conn, sender, at(27, 6, 0))

    assert {r[0] for r in sender.reminders} == {rid1, rid2}
    assert len(calls) == 1  # one fetch, memoized across both same-hour reminders
    assert sender.weather_lines == [_HOUR_WEATHER, _HOUR_WEATHER]


def test_tick_does_not_memoize_across_different_hours(conn, monkeypatch):
    from brain.memory import upsert_fact

    upsert_fact(conn, "location", "Lisbon, Portugal")
    create_reminder(conn, "Gym", "2026-09-27T06:00", None, at(27, 5), TZ, category="fitness")
    create_reminder(conn, "Yoga", "2026-09-27T07:00", None, at(27, 5), TZ, category="fitness")
    calls = []

    def _counting(location, at_):
        calls.append(at_)
        return _HOUR_WEATHER

    monkeypatch.setattr("brain.daily_list.forecast_hour_line", _counting)
    sender = FakeSender()

    tick(conn, sender, at(27, 6, 0))
    tick(conn, sender, at(27, 7, 0))

    assert len(calls) == 2


def test_weather_fetches_run_on_a_dedicated_small_thread_pool():
    import concurrent.futures

    from brain.daily_list import _WEATHER_EXECUTOR

    assert isinstance(_WEATHER_EXECUTOR, concurrent.futures.ThreadPoolExecutor)
    assert _WEATHER_EXECUTOR._max_workers == 2


# ---------------------------------------------------------------- re-pings

def _trading(conn, rrule=None):
    return create_reminder(
        conn, "Trading and journal", "2026-09-27T17:30", rrule, at(27, 10), TZ,
        category="trading",
    )["id"]


def _ping_rows(conn, rid):
    return conn.execute(
        "SELECT message_id, ping_number, superseded FROM reminder_pings "
        "WHERE reminder_id = %s ORDER BY ping_number", (rid,),
    ).fetchall()


def test_no_reping_before_sixty_minutes(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 29))
    assert sender.repings == []
    assert len(_ping_rows(conn, rid)) == 1


def test_reping_after_sixty_minutes_with_still_to_do_numbering(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == [(rid, "Trading and journal", "trading", "17:30", 2, 4)]
    assert sender.weather_lines[-1] is None
    rows = _ping_rows(conn, rid)
    assert [(r[1], r[2]) for r in rows] == [(1, True), (2, False)]


def test_previous_ping_is_deleted_on_reping(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    first_message = _ping_rows(conn, rid)[0][0]
    tick(conn, sender, at(27, 18, 30))
    assert sender.deleted == [first_message]
    assert not [e for e in sender.pings_edited if e[0] == first_message]


def test_deleted_pings_are_not_redrawn_when_the_reminder_is_done(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    first_message = _ping_rows(conn, rid)[0][0]
    tick(conn, sender, at(27, 18, 30))
    apply_button(conn, f"list:r{rid}", at(27, 18, 40), TZ)
    tick(conn, sender, at(27, 18, 41))
    assert not [e for e in sender.pings_edited if e[0] == first_message]
    # The latest ping is deleted once the reminder is done.
    assert len(sender.deleted) == 2


def test_stops_after_three_repings(conn):
    rid = _trading(conn)
    sender = FakeSender()
    for hour, minute in [(17, 30), (18, 30), (19, 30), (20, 30), (21, 30), (22, 30)]:
        tick(conn, sender, at(27, hour, minute))
    assert [r[4] for r in sender.repings] == [2, 3, 4]
    assert len(_ping_rows(conn, rid)) == 4


def test_no_reping_when_recurring_reminder_done_today(conn):
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    conn.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY, rid))
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == []


def test_no_reping_when_one_time_reminder_done(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    conn.execute("UPDATE reminders SET status = 'done' WHERE id = %s", (rid,))
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == []


def test_no_reping_when_cancelled(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = %s", (rid,))
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == []


def test_no_reping_when_snoozed(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    message_id = _ping_rows(conn, rid)[0][0]
    apply_button(conn, f"rem:s10:{rid}", at(27, 17, 35), TZ, message_id=message_id)
    tick(conn, sender, at(27, 18, 31))
    assert sender.repings == []


def test_snooze_on_a_superseded_ping_stops_the_chain(conn):
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    first_message = _ping_rows(conn, rid)[0][0]
    tick(conn, sender, at(27, 18, 30))
    apply_button(conn, f"rem:s60:{rid}", at(27, 18, 40), TZ, message_id=first_message)
    tick(conn, sender, at(27, 19, 31))
    assert [r[4] for r in sender.repings if r[3] == "17:30"] == [2]


def test_no_reping_for_an_early_heads_up(conn):
    create_reminder(
        conn, "Rent", "2026-09-28T17:30", None, at(27, 10), TZ,
        category="subscription", remind_days_before=1,
    )
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == []


def test_send_failure_leaves_no_row_and_retries_next_tick(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    sender.fail_send = True
    tick(conn, sender, at(27, 18, 30))
    rows = _ping_rows(conn, rid)
    assert [(r[1], r[2]) for r in rows] == [(1, False)]
    sender.fail_send = False
    tick(conn, sender, at(27, 18, 31))
    assert [r[4] for r in sender.repings] == [2]


def test_done_on_the_latest_reping_ends_the_chain(conn):
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    latest = _ping_rows(conn, rid)[-1][0]
    apply_button(conn, f"rem:done:{rid}", at(27, 18, 40), TZ, message_id=latest)
    tick(conn, sender, at(27, 19, 45))
    assert [r[4] for r in sender.repings] == [2]


def test_two_ticks_in_the_same_minute_do_not_double_send(conn):
    _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    tick(conn, sender, at(27, 18, 30))
    assert [r[4] for r in sender.repings] == [2]


def test_list_untick_does_not_resurrect_buttons_on_superseded_rows(conn):
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    apply_button(conn, f"list:r{rid}", at(27, 18, 40), TZ)  # tick done
    apply_button(conn, f"list:r{rid}", at(27, 18, 41), TZ)  # un-tick
    sender.pings_edited.clear()
    tick(conn, sender, at(27, 18, 42))
    first_message = _ping_rows(conn, rid)[0][0]
    assert all(e[2].buttons is False for e in sender.pings_edited if e[0] == first_message)


# ------------------------------------------------------------------ nudge

def _nudge_tick(conn, sender, now):
    asyncio.run(run_scheduler_tick(conn, sender, now, TZ, NINE, 30, time(19, 30)))


def _untimed(conn, text="Duolingo"):
    return create_reminder(
        conn, text, "2026-09-27", "FREQ=DAILY", at(27, 8), TZ, category="learning",
        timed=False,
    )["id"]


def test_no_nudge_before_the_nudge_time(conn):
    _untimed(conn)
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 19, 29))
    assert sender.nudges == []


def test_nudge_lists_only_undone_untimed_items_and_todos(conn):
    _untimed(conn, "Duolingo")
    done = _untimed(conn, "Make and schedule video")
    conn.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY, done))
    create_reminder(conn, "Timed thing", "2026-09-27T21:00", None, at(27, 8), TZ)
    add_todo(conn, "Carried todo", "2026-09-22", TODAY)
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 19, 30))
    assert sender.nudges == [["• Duolingo", "• Carried todo ↪ since Tue"]]


def test_nudge_sent_once_per_day_and_survives_restart(conn):
    _untimed(conn)
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 19, 30))
    _nudge_tick(conn, sender, at(27, 19, 31))
    _nudge_tick(conn, sender, at(27, 19, 32))
    assert len(sender.nudges) == 1


def test_late_start_after_nudge_time_sends_once(conn):
    _untimed(conn)
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 22, 0))
    _nudge_tick(conn, sender, at(27, 22, 1))
    assert len(sender.nudges) == 1


def test_no_nudge_when_everything_is_done_and_day_is_handled(conn):
    rid = _untimed(conn)
    conn.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY, rid))
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 19, 30))
    conn.execute("UPDATE reminders SET last_done_on = NULL WHERE id = %s", (rid,))
    _nudge_tick(conn, sender, at(27, 19, 31))
    assert sender.nudges == []


def test_nudge_works_again_the_next_day(conn):
    _untimed(conn)
    sender = FakeSender()
    _nudge_tick(conn, sender, at(27, 19, 30))
    _nudge_tick(conn, sender, at(28, 19, 30))
    assert len(sender.nudges) == 2


def test_failed_nudge_send_is_retried(conn):
    _untimed(conn)
    sender = FakeSender(fail_send=True)
    _nudge_tick(conn, sender, at(27, 19, 30))
    sender.fail_send = False
    _nudge_tick(conn, sender, at(27, 19, 31))
    assert len(sender.nudges) == 1


def test_init_db_twice_is_idempotent_for_the_new_columns(conn):
    from brain.memory import init_db
    from tests.conftest import TEST_DATABASE_URL
    init_db(TEST_DATABASE_URL).close()
    cols = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name IN ('reminder_pings', 'daily_list')").fetchall()}
    assert {"ping_number", "superseded", "nudge_date"} <= cols


# ------------------------------------------------ re-ping review fixes

def _manual_ping(conn, rid, message_id, due_at, created_at, ping_number=1):
    record_ping(
        conn, rid, message_id, due_at.astimezone(TZ).date(), due_at, ping_number, created_at,
    )


def test_old_preexisting_rows_never_reping_after_a_deploy(conn):
    # One-time reminder fired 3 days ago (left 'active', next_fire_at NULL),
    # and one whose ping went out 5 hours ago today: neither is a live chain.
    old = create_reminder(conn, "Old one", "2026-09-24T10:00", None, at(24, 8), TZ)["id"]
    stale = create_reminder(conn, "Stale today", "2026-09-27T10:00", None, at(27, 8), TZ)["id"]
    conn.execute("UPDATE reminders SET next_fire_at = NULL")
    _manual_ping(conn, old, 1, at(24, 10), at(24, 10))
    _manual_ping(conn, stale, 2, at(27, 10), at(27, 10))
    sender = FakeSender()
    tick(conn, sender, at(27, 15, 0))
    assert sender.repings == []


def test_a_row_from_sixty_one_minutes_ago_does_reping(conn):
    rid = create_reminder(conn, "Fresh", "2026-09-27T10:00", None, at(27, 8), TZ)["id"]
    conn.execute("UPDATE reminders SET next_fire_at = NULL")
    _manual_ping(conn, rid, 1, at(27, 10), at(27, 10))
    sender = FakeSender()
    tick(conn, sender, at(27, 11, 1))
    assert [r[4] for r in sender.repings] == [2]


def test_older_occurrence_chain_does_not_reping_once_a_newer_one_was_pinged(conn):
    rid = create_reminder(
        conn, "Hourly thing", "2026-09-27T10:00", "FREQ=DAILY;BYHOUR=10,11;BYMINUTE=0",
        at(27, 8), TZ,
    )["id"]
    conn.execute("UPDATE reminders SET next_fire_at = NULL")
    _manual_ping(conn, rid, 1, at(27, 10), at(27, 10))
    _manual_ping(conn, rid, 2, at(27, 11), at(27, 11))
    sender = FakeSender()
    tick(conn, sender, at(27, 11, 1))
    assert sender.repings == []


def test_done_between_claim_and_send_skips_the_reping(conn, monkeypatch):
    import brain.daily_list as dl
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    real_claim = dl.claim_ping_for_reping

    def claim_then_click(c, ping_id):
        result = real_claim(c, ping_id)
        c.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY, rid))
        return result

    monkeypatch.setattr(dl, "claim_ping_for_reping", claim_then_click)
    tick(conn, sender, at(27, 18, 30))
    assert sender.repings == []
    assert len(_ping_rows(conn, rid)) == 1


def test_done_during_the_send_deletes_the_new_ping_and_the_old(conn):
    rid = _trading(conn, "FREQ=DAILY")

    class ClickingSender(FakeSender):
        async def send_reminder(self, *args, **kwargs):
            result = await super().send_reminder(*args, **kwargs)
            if kwargs.get("ping_number", 1) > 1:
                conn.execute("UPDATE reminders SET last_done_on = %s WHERE id = %s", (TODAY, rid))
            return result

    sender = ClickingSender()
    tick(conn, sender, at(27, 17, 30))
    first_message = _ping_rows(conn, rid)[0][0]
    tick(conn, sender, at(27, 18, 30))
    assert sender.deleted[0] == first_message
    # The same tick's sync deleted the new ping too, since it's already done.
    assert len(sender.deleted) == 2 and [r for r in _ping_rows(conn, rid) if not r[2]] == []


def test_snooze_during_the_send_supersedes_the_new_ping(conn):
    rid = _trading(conn, "FREQ=DAILY")

    class SnoozingSender(FakeSender):
        async def send_reminder(self, *args, **kwargs):
            result = await super().send_reminder(*args, **kwargs)
            if kwargs.get("ping_number", 1) > 1:
                first = conn.execute(
                    "SELECT message_id FROM reminder_pings WHERE reminder_id = %s "
                    "AND ping_number = 1", (rid,),
                ).fetchone()[0]
                apply_button(conn, f"rem:s10:{rid}", at(27, 18, 30), TZ, message_id=first)
            return result

    sender = SnoozingSender()
    tick(conn, sender, at(27, 17, 30))
    first_message = _ping_rows(conn, rid)[0][0]
    tick(conn, sender, at(27, 18, 30))
    rows = _ping_rows(conn, rid)
    assert [(r[1], r[2]) for r in rows] == [(2, True)]
    new_message = rows[0][0]
    assert first_message not in sender.deleted
    assert sender.deleted == [new_message]


def test_untick_restores_the_reping_card_on_the_latest_row(conn):
    rid = _trading(conn, "FREQ=DAILY")
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    tick(conn, sender, at(27, 18, 30))
    latest = _ping_rows(conn, rid)[-1][0]
    apply_button(conn, f"list:r{rid}", at(27, 18, 40), TZ)
    apply_button(conn, f"list:r{rid}", at(27, 18, 41), TZ)
    sender.pings_edited.clear()
    tick(conn, sender, at(27, 18, 42))
    card = [e[2] for e in sender.pings_edited if e[0] == latest][-1]
    assert card.title == "Still to do: Trading and journal"
    assert card.description == "`17:30` · 📈 Trading · reminder 2 of 4"
    assert card.buttons is True


def test_done_button_asks_to_delete_the_ping_and_forgets_its_row(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))

    outcome = apply_button(conn, f"rem:done:{rid}", at(27, 15, 6), TZ, message_id=9001)

    assert outcome.delete_message is True
    assert conn.execute("SELECT count(*) FROM reminder_pings WHERE message_id = 9001").fetchone()[0] == 0


def test_snooze_does_not_delete_the_ping(conn):
    rid = create_reminder(
        conn, "Creatine", "2026-09-27T15:00", "FREQ=DAILY", at(27, 8), TZ, category="supplement",
    )["id"]
    record_ping(conn, rid, 9001, TODAY, at(27, 15))
    assert apply_button(conn, f"rem:s60:{rid}", at(27, 15, 6), TZ, message_id=9001).delete_message is False


def test_ticking_it_in_the_list_deletes_the_ping_message(conn):
    rid = _trading(conn)
    sender = FakeSender()
    tick(conn, sender, at(27, 17, 30))
    message = _ping_rows(conn, rid)[0][0]
    apply_button(conn, f"list:r{rid}", at(27, 17, 40), TZ)
    tick(conn, sender, at(27, 17, 41))
    assert message in sender.deleted
    assert not [e for e in sender.pings_edited if e[0] == message]
    assert _ping_rows(conn, rid) == []
