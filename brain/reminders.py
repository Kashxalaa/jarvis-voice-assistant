import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from brain.schedule import (
    ScheduleInputError,
    first_fire_at,
    next_occurrence,
    occurrences_on,
    parse_day,
    parse_when,
)

logger = logging.getLogger(__name__)


CATEGORIES = {
    "birthday": "🎂 Birthdays & Anniversaries",
    "subscription": "💳 Subscriptions",
    "supplement": "💊 Supplements",
    "fitness": "🏋️ Fitness",
    "trading": "📈 Trading",
    "learning": "📚 Learning",
    "content": "🎬 Content",
    "admin": "🗂️ Admin",
    "task": "📝 Tasks",
    "other": "📌 Other",
}
_CATEGORY_ORDER = {key: index for index, key in enumerate(CATEGORIES)}

# Ping embed colors (option A redesign) — per category, plus a fixed grey for
# a done ping regardless of category.
PING_COLORS = {
    "birthday": 0xEB459E,
    "subscription": 0x5865F2,
    "supplement": 0x23A55A,
    "fitness": 0xE67E22,
    "trading": 0xF0B232,
    "learning": 0x9B59B6,
    "content": 0xED4245,
    "admin": 0x95A5A6,
    "task": 0x99AAB5,
    "other": 0x99AAB5,
}
DONE_PING_COLOR = 0x4E5058

_EARLY_PREFIX_RE = re.compile(r"^(Tomorrow: |In \d+ days: )")
_VARIATION_SELECTOR = "️"


def _category_emoji(category: str | None) -> str:
    header = CATEGORIES.get(category, CATEGORIES["other"])
    return header.split(" ", 1)[0]


def _strip_leading_emoji(rest: str, emoji: str) -> str:
    """Remove a leading `emoji` from `rest`, comparing with U+FE0F (the emoji
    variation selector) removed from both sides — so "🏋 Gym" still strips
    fitness's "🏋️ Fitness" emoji even though the text is missing the
    variation selector the category header carries, and vice versa."""
    plain_emoji = emoji.replace(_VARIATION_SELECTOR, "")
    if not plain_emoji:
        return rest
    plain_rest = rest.replace(_VARIATION_SELECTOR, "")
    if not plain_rest.startswith(plain_emoji):
        return rest
    consumed = 0
    matched = 0
    while consumed < len(rest) and matched < len(plain_emoji):
        if rest[consumed] != _VARIATION_SELECTOR:
            matched += 1
        consumed += 1
    # A variation selector right after the matched emoji belongs to it too.
    while consumed < len(rest) and rest[consumed] == _VARIATION_SELECTOR:
        consumed += 1
    return rest[consumed:].lstrip()


def is_early_reminder_text(text: str) -> bool:
    """True if `text` carries the "Tomorrow: " / "In N days: " prefix
    `_early_text` adds to an early ("day before") reminder's own text — used
    to tell an early ping apart from its main occurrence's ping without a
    separate DB round-trip (e.g. to skip a fitness ping's weather fetch on
    early rows, which only makes sense for the main occurrence)."""
    return _EARLY_PREFIX_RE.match(text) is not None


def display_text(text: str, category: str | None) -> str:
    """The item's text as shown on the list/pings: strip a leading emoji that
    just repeats the category's own emoji (already shown by the header/embed
    color), whether or not it follows a "Tomorrow: " / "In N days: " prefix.
    If stripping it would leave nothing behind, the original text is kept."""
    prefix_match = _EARLY_PREFIX_RE.match(text)
    prefix = prefix_match[0] if prefix_match else ""
    rest = text[len(prefix):]
    stripped = _strip_leading_emoji(rest, _category_emoji(category))
    if not stripped:
        return text
    return prefix + stripped


_MIN_LEAD_DAYS = 1
_MAX_LEAD_DAYS = 30
_LEAD_DAYS_ERROR = "I can remind you between 1 and 30 days early."


def _normalize_category(category: str | None, default: str) -> str:
    key = (category or "").strip().lower()
    return key if key in CATEGORIES else default


def _category_rank(category: str) -> int:
    return _CATEGORY_ORDER.get(category, _CATEGORY_ORDER["other"])


def _normalize_lead_days(remind_days_before) -> int:
    """None/0 (int, float or numeric string) -> 0 (no early reminder).
    Otherwise must be an int-valued 1..30, else ScheduleInputError."""
    if remind_days_before is None:
        return 0
    if isinstance(remind_days_before, str) and not remind_days_before.strip():
        return 0  # blank string means "not provided", same as None
    if isinstance(remind_days_before, bool):
        raise ScheduleInputError(_LEAD_DAYS_ERROR)
    if isinstance(remind_days_before, int):
        days = remind_days_before
    elif isinstance(remind_days_before, float):
        if not remind_days_before.is_integer():
            raise ScheduleInputError(_LEAD_DAYS_ERROR)
        days = int(remind_days_before)
    elif isinstance(remind_days_before, str):
        try:
            value = float(remind_days_before.strip())
        except ValueError:
            raise ScheduleInputError(_LEAD_DAYS_ERROR)
        if not value.is_integer():
            raise ScheduleInputError(_LEAD_DAYS_ERROR)
        days = int(value)
    else:
        raise ScheduleInputError(_LEAD_DAYS_ERROR)
    if days == 0:
        return 0
    if not (_MIN_LEAD_DAYS <= days <= _MAX_LEAD_DAYS):
        raise ScheduleInputError(_LEAD_DAYS_ERROR)
    return days


def _early_text(text: str, lead_days: int) -> str:
    return f"Tomorrow: {text}" if lead_days == 1 else f"In {lead_days} days: {text}"


class ItemNotFoundError(LookupError):
    pass


class AmbiguousItemError(LookupError):
    """A name matched more than one active main reminder.

    `matches` is a list of (id, text) pairs for the caller to disambiguate.
    """

    def __init__(self, matches):
        self.matches = matches
        super().__init__(matches)


_MIDNIGHT = time()


def mark_list_dirty(conn) -> None:
    conn.execute(
        "INSERT INTO daily_list (id, needs_refresh) VALUES (1, TRUE) "
        "ON CONFLICT (id) DO UPDATE SET needs_refresh = TRUE"
    )


MAX_TEXT_LENGTH = 500


def _require_text(text: str | None, question: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ScheduleInputError(question)
    if len(text) > MAX_TEXT_LENGTH:
        raise ScheduleInputError("That's too long — keep it under 500 characters.")
    return text


def create_reminder(
    conn, text, when, repeat, now: datetime, tz: ZoneInfo,
    category: str | None = None, remind_days_before=None, timed: bool = True,
) -> dict:
    text = _require_text(text, "What should I remind you about?")
    rrule = (repeat or "").strip() or None
    category = _normalize_category(category, "other")
    lead_days = _normalize_lead_days(remind_days_before)

    if timed:
        starts_at = parse_when(when or "", tz)
        # Compute both fire times before touching the database — never
        # insert the main reminder and then fail (or vice versa), leaving a
        # half pair.
        fire_at = first_fire_at(rrule, starts_at, now, tz)
        early_fire_at = None
        if lead_days:
            try:
                early_fire_at = first_fire_at(rrule, starts_at, now, tz, lead_days=lead_days)
            except ScheduleInputError:
                # The main rule/time still fires fine (already validated
                # above), but its early offset doesn't: a one-time reminder
                # whose early time has already passed, or a recurring rule
                # whose last occurrence (COUNT/UNTIL) falls within the lead
                # window. Either way, just skip the early reminder rather
                # than failing the whole request.
                early_fire_at = None
        create_early = lead_days > 0 and early_fire_at is not None
        next_on_list = None
    else:
        # An untimed reminder never pings: next_fire_at stays NULL forever.
        # starts_at is local midnight of the given day (the time part, if
        # any, is ignored); the rrule still expands normally from there.
        day = parse_when(when or "", tz).date()
        starts_at = datetime.combine(day, time(), tzinfo=tz)
        today = now.astimezone(tz).date()
        if rrule is None:
            if day < today:
                raise ScheduleInputError("That day has already passed.")
            next_on_list = starts_at
        else:
            # Reuse first_fire_at to validate the rule has an occurrence on
            # or after today, using today's local midnight as the
            # reference point rather than the real "now" — an untimed
            # reminder for today must not be rejected just because the
            # wall clock is already past midnight. Its result is also the
            # first day this reminder actually shows up on, which can be
            # well after starts_at's own (possibly long-past) date.
            day_start = datetime.combine(today, time(), tzinfo=tz)
            next_on_list = first_fire_at(rrule, starts_at, day_start, tz)
        fire_at = None
        early_fire_at = None
        # Untimed early reminders never fire either, so — unlike the timed
        # case — there's no "already passed" reason to skip creating one.
        create_early = lead_days > 0

    row = conn.execute(
        "INSERT INTO reminders (text, starts_at, rrule, next_fire_at, category, timed) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (text, starts_at, rrule, fire_at, category, timed),
    ).fetchone()
    main_id = row[0]

    early = None
    if create_early:
        early_text = _early_text(text, lead_days)
        early_row = conn.execute(
            "INSERT INTO reminders "
            "(text, starts_at, rrule, next_fire_at, category, lead_days, parent_id, timed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (early_text, starts_at, rrule, early_fire_at, category, lead_days, main_id, timed),
        ).fetchone()
        early = {"id": early_row[0], "text": early_text, "next_fire_at": early_fire_at}

    mark_list_dirty(conn)
    return {
        "id": main_id, "text": text, "next_fire_at": fire_at, "rrule": rrule,
        "category": category, "early": early, "lead_days": lead_days,
        "timed": timed, "starts_at": starts_at, "next_on_list": next_on_list,
    }


def _parse_time_of_day(value: str) -> time:
    try:
        return datetime.strptime((value or "").strip(), "%H:%M").time()
    except ValueError:
        raise ScheduleInputError(f"I couldn't understand the time {value!r}.")


def _blank_to_none(value: str | None) -> str | None:
    """Treat "" / whitespace-only as "not provided", same as None."""
    if value is None:
        return None
    return value if value.strip() else None


def _escape_like(value: str) -> str:
    """Escape LIKE/ILIKE metacharacters so a literal % or _ in the user's
    text is matched literally instead of acting as a wildcard."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolve_edit_target(conn, item_id: str | None, name: str | None, today_start: datetime) -> int:
    if item_id:
        kind, number = _parse_item_id(item_id)
        if kind != "reminder":
            raise ItemNotFoundError(item_id)
        row = conn.execute(
            "SELECT parent_id FROM reminders WHERE id = %s", (number,)
        ).fetchone()
        if row is None:
            raise ItemNotFoundError(item_id)
        target_id = row[0] if row[0] is not None else number
    elif name:
        pattern = f"%{_escape_like(name.strip())}%"
        matches = conn.execute(
            "SELECT id, text, rrule FROM reminders WHERE status = 'active' "
            "AND parent_id IS NULL AND (rrule IS NOT NULL OR starts_at >= %s) "
            "AND text ILIKE %s ESCAPE '\\' ORDER BY id",
            (today_start, pattern),
        ).fetchall()
        if not matches:
            raise ItemNotFoundError(name)
        target_id = _pick_best_match(matches, name)
    else:
        raise ItemNotFoundError("")

    status_row = conn.execute(
        "SELECT status FROM reminders WHERE id = %s", (target_id,)
    ).fetchone()
    if status_row is None or status_row[0] != "active":
        raise ItemNotFoundError(str(target_id))
    return target_id


def _pick_best_match(matches: list[tuple[int, str, str | None]], name: str) -> int:
    """Narrow several ILIKE matches down to one: prefer an exact
    (case-insensitive) text match, then a recurring row over a one-time
    one (e.g. a snooze copy sharing the same text as its recurring
    original). Raises AmbiguousItemError only if a single best candidate
    still can't be picked out."""
    lname = name.strip().lower()
    exact = [m for m in matches if m[1].strip().lower() == lname]
    pool = exact if exact else matches
    if len(pool) == 1:
        return pool[0][0]
    recurring = [m for m in pool if m[2] is not None]
    if recurring:
        pool = recurring
    if len(pool) == 1:
        return pool[0][0]
    raise AmbiguousItemError([(mid, mtext) for mid, mtext, _rrule in pool])


def edit_reminder(
    conn, now: datetime, tz: ZoneInfo, item_id: str | None = None, name: str | None = None,
    text: str | None = None, when: str | None = None, time: str | None = None,
    repeat: str | None = None, no_time: bool | None = None, category: str | None = None,
) -> dict:
    item_id = _blank_to_none(item_id)
    name = _blank_to_none(name)
    text = _blank_to_none(text)
    when = _blank_to_none(when)
    time = _blank_to_none(time)
    repeat = _blank_to_none(repeat)
    category = _blank_to_none(category)

    if all(v is None for v in (text, when, time, repeat, no_time, category)):
        raise ScheduleInputError("What should I change?")

    today = now.astimezone(tz).date()
    today_start = datetime.combine(today, _MIDNIGHT, tzinfo=tz)

    main_id = _resolve_edit_target(conn, item_id, name, today_start)
    row = conn.execute(
        "SELECT text, starts_at, rrule, category, timed "
        "FROM reminders WHERE id = %s",
        (main_id,),
    ).fetchone()
    cur_text, cur_starts_at, cur_rrule, cur_category, cur_timed = row
    cur_starts_at = cur_starts_at.astimezone(tz)

    # --- timed / untimed ----------------------------------------------
    when_has_time = when is not None and len(when.strip()) > 10  # "YYYY-MM-DD" is 10 chars
    if no_time is True:
        target_timed = False
    elif no_time is False:
        target_timed = True
        # Only a genuine untimed->timed transition needs a new time — if
        # it's already timed, no_time=False (redundantly) confirming that
        # is a no-op, not a demand for a time nothing asked to change.
        if not cur_timed and time is None and not when_has_time:
            raise ScheduleInputError("What time should it be at?")
    elif time is not None or when_has_time:
        target_timed = True
    else:
        target_timed = cur_timed

    # --- text -----------------------------------------------------------
    new_text = (
        _require_text(text, "What should I remind you about?") if text is not None else cur_text
    )

    # --- category ---------------------------------------------------------
    new_category = _normalize_category(category, "other") if category is not None else cur_category

    # --- repeat -----------------------------------------------------------
    if repeat is None:
        new_rrule = cur_rrule
    else:
        new_rrule = None if repeat.strip().lower() == "none" else repeat.strip()

    # --- starts_at ----------------------------------------------------
    if when is not None:
        parsed_when = parse_when(when, tz)
        day = parsed_when.date()
        if not target_timed:
            new_starts_at = datetime.combine(day, _MIDNIGHT, tzinfo=tz)
        elif time is not None:
            # Both given: `time` wins for the time of day, `when` for the date.
            new_starts_at = datetime.combine(day, _parse_time_of_day(time), tzinfo=tz)
        elif when_has_time:
            new_starts_at = datetime.combine(day, parsed_when.time(), tzinfo=tz)
        else:
            # Date-only `when` on a reminder that stays timed: keep its
            # current time of day instead of silently dropping it to
            # midnight.
            new_starts_at = datetime.combine(day, cur_starts_at.time(), tzinfo=tz)
    elif time is not None and no_time is not True:
        new_starts_at = datetime.combine(cur_starts_at.date(), _parse_time_of_day(time), tzinfo=tz)
    elif not target_timed:
        new_starts_at = datetime.combine(cur_starts_at.date(), _MIDNIGHT, tzinfo=tz)
    else:
        new_starts_at = cur_starts_at

    date_or_time_changed = new_starts_at != cur_starts_at or new_rrule != cur_rrule

    # --- linked early rows -----------------------------------------------
    # A snoozed one-time early copy has lead_days reset to 0 and is no
    # longer a "day before"-style clone — it must be left alone, not
    # rewritten into "In 0 days: ...".
    children = conn.execute(
        "SELECT id, lead_days, status FROM reminders WHERE parent_id = %s "
        "AND status <> 'cancelled' AND lead_days > 0",
        (main_id,),
    ).fetchall()

    # --- next_fire_at (validate everything before writing anything) -----
    if target_timed:
        new_fire = first_fire_at(new_rrule, new_starts_at, now, tz)
        next_on_list = None
    else:
        if new_rrule is None:
            if new_starts_at.date() < today:
                raise ScheduleInputError("That day has already passed.")
            next_on_list = new_starts_at
        else:
            day_start = today_start
            next_on_list = first_fire_at(new_rrule, new_starts_at, day_start, tz)
        new_fire = None

    child_updates = []
    for child_id, lead_days, child_status in children:
        child_fire = None
        if target_timed:
            try:
                child_fire = first_fire_at(new_rrule, new_starts_at, now, tz, lead_days=lead_days)
            except ScheduleInputError:
                # An early row whose time is now in the past doesn't fail
                # the edit — it just stops firing, same treatment as
                # create_reminder's own early-reminder computation.
                child_fire = None
        # If the main reminder's date/time actually moved, a one-time
        # early row already ticked off as "done" for the old date must
        # wake back up so the moved reminder still gets its early ping.
        new_status = "active" if (date_or_time_changed and child_status == "done") else child_status
        child_updates.append({
            "id": child_id, "text": _early_text(new_text, lead_days), "next_fire_at": child_fire,
            "status": new_status,
        })

    # --- write everything in one all-or-nothing transaction --------------
    with conn.transaction():
        conn.execute(
            "UPDATE reminders SET text = %s, starts_at = %s, rrule = %s, next_fire_at = %s, "
            "category = %s, timed = %s WHERE id = %s",
            (new_text, new_starts_at, new_rrule, new_fire, new_category, target_timed, main_id),
        )
        for child in child_updates:
            conn.execute(
                "UPDATE reminders SET text = %s, starts_at = %s, rrule = %s, next_fire_at = %s, "
                "category = %s, timed = %s, status = %s WHERE id = %s",
                (
                    child["text"], new_starts_at, new_rrule, child["next_fire_at"],
                    new_category, target_timed, child["status"], child["id"],
                ),
            )
        mark_list_dirty(conn)

    return {
        "id": main_id, "text": new_text, "next_fire_at": new_fire, "rrule": new_rrule,
        "timed": target_timed, "category": new_category, "starts_at": new_starts_at,
        "next_on_list": next_on_list,
        "early": [
            {"id": c["id"], "text": c["text"], "next_fire_at": c["next_fire_at"]}
            for c in child_updates
        ],
    }


def add_todo(conn, text, due_date: str | None, today: date, category: str | None = None) -> dict:
    text = _require_text(text, "What should I add to your list?")
    day = parse_day(due_date, today)
    category = _normalize_category(category, "task")
    row = conn.execute(
        "INSERT INTO todos (text, due_date, category) VALUES (%s, %s, %s) RETURNING id",
        (text, day, category),
    ).fetchone()
    mark_list_dirty(conn)
    return {"id": row[0], "text": text, "due_date": day, "category": category}


def get_reminder_text(conn, reminder_id: int) -> str:
    row = conn.execute("SELECT text FROM reminders WHERE id = %s", (reminder_id,)).fetchone()
    if row is None:
        raise ItemNotFoundError(f"r{reminder_id}")
    return row[0]


def mark_reminder_done(conn, reminder_id: int, today: date) -> str:
    # A one-time reminder is finished for good; a recurring one is only
    # ticked off for today, so the series keeps firing.
    row = conn.execute(
        "UPDATE reminders SET "
        "status = CASE WHEN rrule IS NULL THEN 'done' ELSE status END, "
        "last_done_on = %s "
        "WHERE id = %s AND status <> 'cancelled' RETURNING text",
        (today, reminder_id),
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(f"r{reminder_id}")
    mark_list_dirty(conn)
    mark_pings_dirty(conn, reminder_id, today)
    return row[0]


def snooze_reminder(conn, reminder_id: int, minutes: int, now: datetime) -> datetime:
    row = conn.execute(
        "SELECT text, rrule, category FROM reminders WHERE id = %s AND status <> 'cancelled'",
        (reminder_id,),
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(f"r{reminder_id}")
    text, rrule, category = row
    until = now + timedelta(minutes=minutes)
    if rrule is None:
        # starts_at now IS the real (new) fire time, so lead_days must drop
        # to 0 — otherwise occurrences_on would subtract the lead again and
        # show this on the wrong day. parent_id is left alone: a snoozed
        # early reminder is still cancelled if its main reminder is.
        conn.execute(
            "UPDATE reminders SET starts_at = %s, next_fire_at = %s, status = 'active', "
            "last_done_on = NULL, lead_days = 0 WHERE id = %s",
            (until, until, reminder_id),
        )
    else:
        # A one-time copy: category carries over, lead_days = 0 and
        # parent_id = NULL by column default — it's its own standalone
        # reminder from here, cancelling it never cascades anywhere.
        conn.execute(
            "INSERT INTO reminders (text, starts_at, next_fire_at, category) "
            "VALUES (%s, %s, %s, %s)",
            (text, until, until, category),
        )
    mark_list_dirty(conn)
    return until


def _parse_item_id(item_id: str) -> tuple[str, int]:
    item_id = (item_id or "").strip().lower()
    if len(item_id) >= 2 and item_id[0] in ("t", "r") and item_id[1:].isdigit():
        return ("todo" if item_id[0] == "t" else "reminder"), int(item_id[1:])
    raise ItemNotFoundError(item_id)


def finish_item(conn, item_id: str, action: str, today: date, now: datetime) -> str:
    kind, number = _parse_item_id(item_id)
    if action not in ("done", "cancel"):
        raise ScheduleInputError(f"I don't know how to {action!r} an item.")
    if kind == "todo" and action == "done":
        row = conn.execute(
            "UPDATE todos SET done_at = COALESCE(done_at, %s) WHERE id = %s RETURNING text",
            (now, number),
        ).fetchone()
    elif kind == "todo":
        row = conn.execute(
            "DELETE FROM todos WHERE id = %s RETURNING text", (number,)
        ).fetchone()
    elif action == "done":
        return mark_reminder_done(conn, number, today)
    else:
        row = conn.execute(
            "UPDATE reminders SET status = 'cancelled', next_fire_at = NULL "
            "WHERE id = %s AND status <> 'cancelled' RETURNING text",
            (number,),
        ).fetchone()
        if row is not None:
            # Cascade: cancelling a reminder also cancels its linked early
            # ("day before") reminder. "Done" never reaches this branch, so
            # it never cascades. Cancelling an early row itself has no
            # children of its own, so this is a no-op for it.
            conn.execute(
                "UPDATE reminders SET status = 'cancelled', next_fire_at = NULL "
                "WHERE parent_id = %s AND status <> 'cancelled'",
                (number,),
            )
    if row is None:
        raise ItemNotFoundError(item_id)
    mark_list_dirty(conn)
    return row[0]


def toggle_item(conn, item_id: str, today: date, now: datetime) -> bool:
    kind, number = _parse_item_id(item_id)
    if kind == "todo":
        row = conn.execute(
            "UPDATE todos SET done_at = CASE WHEN done_at IS NULL THEN %s ELSE NULL END "
            "WHERE id = %s RETURNING done_at IS NOT NULL",
            (now, number),
        ).fetchone()
        if row is None:
            raise ItemNotFoundError(item_id)
        mark_list_dirty(conn)
        return row[0]

    row = conn.execute(
        "SELECT rrule, status, last_done_on FROM reminders "
        "WHERE id = %s AND status <> 'cancelled'",
        (number,),
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(item_id)
    rrule, status, last_done_on = row
    done_today = status == "done" if rrule is None else last_done_on == today
    if not done_today:
        mark_reminder_done(conn, number, today)
        return True
    conn.execute(
        "UPDATE reminders SET "
        "status = CASE WHEN rrule IS NULL THEN 'active' ELSE status END, "
        "last_done_on = NULL WHERE id = %s",
        (number,),
    )
    mark_list_dirty(conn)
    mark_pings_dirty(conn, number, today)
    return False


_REPEAT_WORDS = {
    "YEARLY": "yearly", "MONTHLY": "monthly", "WEEKLY": "weekly",
    "DAILY": "daily", "HOURLY": "hourly",
}
_DAY_NAMES = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}


def describe_repeat(rrule: str | None) -> str:
    if not rrule:
        return "once"
    parts = dict(
        part.split("=", 1)
        for part in rrule.upper().replace("RRULE:", "").split(";")
        if "=" in part
    )
    freq = parts.get("FREQ", "")
    interval = parts.get("INTERVAL", "1")
    if freq == "WEEKLY" and parts.get("BYDAY"):
        days = [_DAY_NAMES.get(d[-2:], d) for d in parts["BYDAY"].split(",")]
        if days == ["Mon", "Tue", "Wed", "Thu", "Fri"]:
            return "weekdays"
        return "every " + ", ".join(days)
    if interval != "1" and freq == "DAILY":
        return f"every {interval} days"
    return _REPEAT_WORDS.get(freq, "repeating")


def find_reminders(
    conn, now: datetime, tz: ZoneInfo, query: str | None = None, category: str | None = None,
) -> list[dict]:
    """Active main reminders matching a name and/or category, soonest first."""
    sql = (
        "SELECT r.id, r.text, r.category, r.starts_at, r.rrule, r.timed, "
        "EXISTS (SELECT 1 FROM reminders e WHERE e.parent_id = r.id AND e.status = 'active') "
        "FROM reminders r WHERE r.status = 'active' AND r.parent_id IS NULL"
    )
    params: list = []
    query = (query or "").strip()
    if query:
        sql += " AND r.text ILIKE %s ESCAPE '\\'"
        params.append(f"%{_escape_like(query)}%")
    category = (category or "").strip().lower()
    if category in CATEGORIES:
        sql += " AND r.category = %s"
        params.append(category)
    today_start = datetime.combine(now.astimezone(tz).date(), time(), tzinfo=tz)
    found = []
    for rid, text, cat, starts_at, rrule, timed, has_early in conn.execute(sql, params).fetchall():
        # Untimed items count from the start of today so today's shows as today.
        after = now if timed else today_start - timedelta(microseconds=1)
        try:
            upcoming = next_occurrence(rrule, starts_at, after, tz)
        except ScheduleInputError:
            continue
        if upcoming is None:
            continue
        found.append({
            "id": f"r{rid}", "text": text, "category": cat, "next": upcoming.astimezone(tz),
            "timed": timed, "repeat": describe_repeat(rrule), "has_early": has_early,
        })
    found.sort(key=lambda item: item["next"])
    return found


def find_reminders_text(items: list[dict], query: str | None, category: str | None) -> str:
    if not items:
        what = " ".join(p for p in (category, f'"{query}"' if query else None) if p) or "anything"
        return f"No active reminders match {what}."
    lines = [f"{len(items)} matching reminder(s), soonest first:"]
    for item in items:
        when = item["next"]
        day = f"{when:%a} {when.day} {when:%b %Y}"
        stamp = f"{day}, {when:%H:%M}" if item["timed"] else f"{day} (no set time)"
        extras = [item["repeat"]]
        if item["has_early"]:
            extras.append("+ day-before reminder")
        header = CATEGORIES.get(item["category"], CATEGORIES["other"])
        lines.append(
            f"- [{item['id']}] {display_text(item['text'], item['category'])} — next {stamp}"
            f" · {' · '.join(extras)} · {header}"
        )
    return "\n".join(lines)


def agenda_items(conn, day: date, tz: ZoneInfo) -> list[dict]:
    day_start = datetime.combine(day, time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    # A one-time early row's occurrence is starts_at - lead_days, which can
    # land up to 30 days before starts_at, i.e. its starts_at can be up to
    # 30 days *after* this day's window. Widen the prefilter and let
    # occurrences_on() (which knows each row's own lead_days) decide.
    prefetch_end = day_end + timedelta(days=_MAX_LEAD_DAYS)

    reminder_items = []
    rows = conn.execute(
        "SELECT id, text, starts_at, rrule, status, last_done_on, lead_days, category, timed "
        "FROM reminders "
        "WHERE status <> 'cancelled' "
        "AND (rrule IS NOT NULL OR (starts_at >= %s AND starts_at < %s)) "
        "ORDER BY id",
        (day_start, prefetch_end),
    ).fetchall()
    for rid, text, starts_at, rrule, status, last_done_on, lead_days, category, timed in rows:
        try:
            occurrences = occurrences_on(rrule, starts_at, day, tz, lead_days=lead_days)
        except ScheduleInputError as exc:
            logger.warning("skipping reminder %s with a bad rule: %s", rid, exc)
            continue
        if not occurrences:
            continue
        done = status == "done" if rrule is None else last_done_on == day
        reminder_items.append({
            "id": f"r{rid}", "kind": "reminder", "text": text,
            "time": occurrences[0].strftime("%H:%M") if timed else None, "done": done,
            "since": None, "category": category, "_sort": occurrences[0],
            "_kind_rank": 0 if timed else 1,
        })
    reminder_items.sort(key=lambda item: item["_sort"])
    for item in reminder_items:
        del item["_sort"]

    todo_rows = conn.execute(
        "SELECT id, text, due_date, done_at, category FROM todos "
        "WHERE (done_at IS NULL AND due_date <= %s) "
        "OR (done_at IS NOT NULL AND (done_at AT TIME ZONE %s)::date = %s) "
        "ORDER BY due_date, id",
        (day, tz.key, day),
    ).fetchall()
    todo_items = [
        {
            "id": f"t{tid}", "kind": "todo", "text": text, "time": None,
            "done": done_at is not None,
            "since": due_date if done_at is None and due_date < day else None,
            "category": category, "_kind_rank": 2,
        }
        for tid, text, due_date, done_at, category in todo_rows
    ]
    items = reminder_items + todo_items
    # Group by category (CATEGORIES order), then within a category: timed
    # reminders, then untimed reminders, then todos. Python's sort is
    # stable, so each group keeps the order it already arrived in
    # (reminders by occurrence time, todos by due date/id).
    items.sort(key=lambda item: (_category_rank(item["category"]), item["_kind_rank"]))
    for item in items:
        del item["_kind_rank"]
    return items


def claim_due_reminders(conn, now: datetime, tz: ZoneInfo) -> list[dict]:
    rows = conn.execute(
        "SELECT id, text, starts_at, rrule, next_fire_at, lead_days, category, last_done_on "
        "FROM reminders "
        "WHERE status = 'active' AND next_fire_at <= %s ORDER BY next_fire_at, id",
        (now,),
    ).fetchall()
    claimed = []
    for rid, text, starts_at, rrule, due_at, lead_days, category, last_done_on in rows:
        try:
            upcoming = (
                next_occurrence(rrule, starts_at, now, tz, lead_days=lead_days)
                if rrule else None
            )
        except ScheduleInputError as exc:
            logger.warning("reminder %s has a bad rule, firing once more: %s", rid, exc)
            upcoming = None
        # Guarded so a reminder cancelled (or claimed elsewhere) since the
        # SELECT is left alone.
        updated = conn.execute(
            "UPDATE reminders SET next_fire_at = %s "
            "WHERE id = %s AND status = 'active' AND next_fire_at = %s",
            (upcoming, rid, due_at),
        ).rowcount
        if updated != 1:
            continue
        # Already ticked off for the day this occurrence falls on (e.g. from
        # the list, before the ping was due) — the series still advances
        # above, but no ping goes out for something already done. This only
        # makes sense for a reminder with exactly one occurrence that day:
        # for a multi-per-day series (FREQ=HOURLY, BYHOUR=9,21, ...) "done
        # today" can't mean "done for every occurrence today", so the rest
        # of the day must keep pinging normally.
        due_date = due_at.astimezone(tz).date()
        if last_done_on == due_date:
            try:
                occurrences_that_day = occurrences_on(rrule, starts_at, due_date, tz, lead_days=lead_days)
            except ScheduleInputError as exc:
                logger.warning("reminder %s has a bad rule, sending anyway: %s", rid, exc)
                occurrences_that_day = []
            if len(occurrences_that_day) == 1:
                continue
        claimed.append({"id": rid, "text": text, "due_at": due_at, "category": category})
    return claimed


def restore_reminder(conn, reminder_id: int, due_at: datetime) -> None:
    conn.execute(
        "UPDATE reminders SET next_fire_at = %s WHERE id = %s AND status = 'active' AND timed",
        (due_at, reminder_id),
    )


def get_daily_list_state(conn) -> dict:
    row = conn.execute(
        "SELECT list_date, message_id, needs_refresh, weather_line FROM daily_list WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"list_date": None, "message_id": None, "needs_refresh": False, "weather_line": None}
    return {"list_date": row[0], "message_id": row[1], "needs_refresh": row[2], "weather_line": row[3]}


def save_daily_list_state(
    conn, list_date: date, message_id: int | None, weather_line: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO daily_list (id, list_date, message_id, weather_line) "
        "VALUES (1, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
        "list_date = EXCLUDED.list_date, message_id = EXCLUDED.message_id, "
        "weather_line = EXCLUDED.weather_line",
        (list_date, message_id, weather_line),
    )


def clear_list_dirty(conn) -> None:
    conn.execute("UPDATE daily_list SET needs_refresh = FALSE WHERE id = 1")


def get_reminder_info(conn, reminder_id: int) -> dict:
    row = conn.execute(
        "SELECT text, category FROM reminders WHERE id = %s", (reminder_id,)
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(f"r{reminder_id}")
    return {"text": row[0], "category": row[1]}


def get_reminder_ping_state(conn, reminder_id: int, day: date) -> dict | None:
    """Whether `reminder_id` is done for `day` — a one-time reminder is done
    once and for all, a recurring one only for the day matching last_done_on.
    Returns None if the reminder no longer exists."""
    row = conn.execute(
        "SELECT text, category, rrule, status, last_done_on FROM reminders WHERE id = %s",
        (reminder_id,),
    ).fetchone()
    if row is None:
        return None
    text, category, rrule, status, last_done_on = row
    done = status == "done" if rrule is None else last_done_on == day
    return {"text": text, "category": category, "done": done}


def record_ping(
    conn, reminder_id: int, message_id: int, fired_on: date, due_at: datetime,
    ping_number: int = 1, created_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO reminder_pings (reminder_id, message_id, fired_on, due_at, ping_number, created_at) "
        "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))",
        (reminder_id, message_id, fired_on, due_at, ping_number, created_at),
    )


def pings_due_for_reping(
    conn, now: datetime, interval: timedelta, max_repings: int, tz: ZoneInfo,
) -> list[dict]:
    """The latest, still-live ping of every unresolved occurrence whose last
    ping went out at or before `sent_before` and that has re-pings left.

    Resolved means: reminder cancelled/done (status), a recurring one ticked
    off for the ping's own day, or a one-time one snoozed/rescheduled
    (next_fire_at set again). Early heads-ups are never re-pinged.

    Only a live chain qualifies: its latest ping went out between one and two
    intervals ago and was fired today (local), so old rows from before this
    feature existed, or from a day nobody answered, never re-ping. A newer
    occurrence of the same reminder having been pinged also ends the older
    chain (HOURLY/BYHOUR series)."""
    rows = conn.execute(
        "SELECT p.id, p.reminder_id, p.message_id, p.fired_on, p.due_at, p.ping_number, "
        "r.text, r.category, r.rrule, r.last_done_on, r.lead_days, r.next_fire_at "
        "FROM reminder_pings p JOIN reminders r ON r.id = p.reminder_id "
        "WHERE NOT p.superseded AND p.ping_number <= %s AND p.created_at <= %s "
        "AND p.created_at > %s AND p.fired_on = %s "
        "AND r.status = 'active' AND r.timed "
        "AND NOT EXISTS (SELECT 1 FROM reminder_pings q WHERE q.reminder_id = p.reminder_id "
        "AND q.due_at = p.due_at AND q.ping_number > p.ping_number) "
        "AND NOT EXISTS (SELECT 1 FROM reminder_pings q WHERE q.reminder_id = p.reminder_id "
        "AND q.due_at > p.due_at) "
        "ORDER BY p.id",
        (max_repings, now - interval, now - 2 * interval, now.astimezone(tz).date()),
    ).fetchall()
    due = []
    for (pid, rid, message_id, fired_on, due_at, number, text, category, rrule,
         last_done_on, lead_days, next_fire_at) in rows:
        if lead_days > 0 or is_early_reminder_text(text):
            continue
        if rrule is not None and last_done_on == fired_on:
            continue
        if rrule is None and next_fire_at is not None:
            continue
        due.append({
            "id": pid, "reminder_id": rid, "message_id": message_id, "fired_on": fired_on,
            "due_at": due_at, "ping_number": number, "text": text, "category": category,
        })
    return due


def reping_still_needed(conn, reminder_id: int, fired_on: date) -> bool:
    """False once the occurrence was resolved (done, cancelled, deleted,
    snoozed/rescheduled) — used to re-check around a re-ping send, where a
    click on another instance can land at any moment."""
    row = conn.execute(
        "SELECT status, rrule, last_done_on, next_fire_at FROM reminders WHERE id = %s",
        (reminder_id,),
    ).fetchone()
    if row is None:
        return False
    status, rrule, last_done_on, next_fire_at = row
    if status != "active":
        return False
    if rrule is not None:
        return last_done_on != fired_on
    return next_fire_at is None


def ping_row_exists(conn, ping_id: int) -> bool:
    return conn.execute("SELECT 1 FROM reminder_pings WHERE id = %s", (ping_id,)).fetchone() is not None


def claim_ping_for_reping(conn, ping_id: int) -> bool:
    """Marks the ping superseded before anything is sent, so a second tick
    can never pick it up too. False if someone else already claimed it."""
    return conn.execute(
        "UPDATE reminder_pings SET superseded = TRUE WHERE id = %s AND NOT superseded",
        (ping_id,),
    ).rowcount == 1


def release_ping_claim(conn, ping_id: int) -> None:
    conn.execute("UPDATE reminder_pings SET superseded = FALSE WHERE id = %s", (ping_id,))


def supersede_chain(conn, reminder_id: int, due_at: datetime) -> None:
    """Stops any further re-pings for the occurrence (reminder_id, due_at)."""
    conn.execute(
        "UPDATE reminder_pings SET superseded = TRUE WHERE reminder_id = %s AND due_at = %s",
        (reminder_id, due_at),
    )


def is_ping_superseded(conn, ping_id: int) -> bool:
    row = conn.execute("SELECT superseded FROM reminder_pings WHERE id = %s", (ping_id,)).fetchone()
    return bool(row and row[0])


def get_nudge_date(conn) -> date | None:
    row = conn.execute("SELECT nudge_date FROM daily_list WHERE id = 1").fetchone()
    return row[0] if row else None


def set_nudge_date(conn, day: date | None) -> None:
    conn.execute(
        "INSERT INTO daily_list (id, nudge_date) VALUES (1, %s) "
        "ON CONFLICT (id) DO UPDATE SET nudge_date = EXCLUDED.nudge_date",
        (day,),
    )


def mark_pings_dirty(conn, reminder_id: int, day: date) -> None:
    """Flag reminder_pings rows for re-rendering after `reminder_id`'s done
    state changed for `day`.

    A one-time reminder only ever has one occurrence — full stop, not "one
    occurrence on `day`" — so if it's finished shortly after midnight (fired
    on one day, marked done on the next), its ping must still be flagged
    regardless of which day it's filed under. A recurring reminder is
    day-scoped, but only when that day had exactly one occurrence: for a
    multi-per-day series (HOURLY, BYHOUR=9,21, ...) the day-level "done"
    flag doesn't correspond to any single ping, so none of that day's
    already-sent pings are touched — only the one actually clicked (handled
    directly by the caller) becomes done.
    """
    row = conn.execute("SELECT rrule FROM reminders WHERE id = %s", (reminder_id,)).fetchone()
    if row is None:
        return
    (rrule,) = row
    if rrule is None:
        conn.execute(
            "UPDATE reminder_pings SET needs_render = TRUE WHERE reminder_id = %s",
            (reminder_id,),
        )
        return
    count = conn.execute(
        "SELECT COUNT(DISTINCT due_at) FROM reminder_pings WHERE reminder_id = %s AND fired_on = %s",
        (reminder_id, day),
    ).fetchone()[0]
    if count > 1:
        return
    conn.execute(
        "UPDATE reminder_pings SET needs_render = TRUE WHERE reminder_id = %s AND fired_on = %s",
        (reminder_id, day),
    )


def mark_ping_needs_render(conn, ping_id: int) -> None:
    """Re-flag a single ping row by its own id — used to restore
    needs_render after an edit attempt failed, without re-running the
    day/multi-occurrence logic in mark_pings_dirty."""
    conn.execute("UPDATE reminder_pings SET needs_render = TRUE WHERE id = %s", (ping_id,))


def pings_needing_render(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, reminder_id, message_id, fired_on, due_at, ping_number "
        "FROM reminder_pings WHERE needs_render = TRUE ORDER BY id"
    ).fetchall()
    return [
        {"id": r[0], "reminder_id": r[1], "message_id": r[2], "fired_on": r[3], "due_at": r[4],
         "ping_number": r[5]}
        for r in rows
    ]


def clear_ping_needs_render(conn, ping_id: int) -> None:
    conn.execute("UPDATE reminder_pings SET needs_render = FALSE WHERE id = %s", (ping_id,))


def drop_ping(conn, ping_id: int) -> None:
    conn.execute("DELETE FROM reminder_pings WHERE id = %s", (ping_id,))


def get_ping_by_message_id(conn, message_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, reminder_id, fired_on, due_at FROM reminder_pings WHERE message_id = %s",
        (message_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "reminder_id": row[1], "fired_on": row[2], "due_at": row[3]}


def prune_old_pings(conn, before_day: date) -> int:
    """Delete tracked ping rows fired before `before_day` — called from the
    tick with a ~7-day-old cutoff so reminder_pings doesn't grow forever."""
    result = conn.execute("DELETE FROM reminder_pings WHERE fired_on < %s", (before_day,))
    return result.rowcount


_CATEGORY_PROPERTY = {
    "type": "string",
    "enum": list(CATEGORIES.keys()),
    "description": (
        "birthday: birthdays/anniversaries; subscription: recurring bills or "
        "subscriptions; supplement: daily supplements/medication; fitness: "
        "workouts/gym; trading: trading/market check-ins; learning: study or "
        "courses; content: videos/reading/watch-later; admin: paperwork/"
        "chores; task: a generic to-do; other: anything else. Always set "
        "it — pick the closest fit from the item's own text (a course is "
        "learning), even in a follow-up message; 'other' only when nothing fits."
    ),
}

CREATE_REMINDER_TOOL = {
    "name": "create_reminder",
    "description": (
        "Set a reminder, once or repeating. Work out the exact local date and "
        "time yourself from the current date and time you were given (e.g. "
        "'in 2 hours', 'Friday at 10', 'tonight at 9'). If the user gives a "
        "date without a time for a dated event (birthdays, bills, renewals), "
        "use 10:00. For tasks, routines, courses or habits the user gives no "
        "time for, set no_time to true (they sit on the daily list without a "
        "ping) and start them today — never make up a time and never borrow "
        "one from another reminder. Call this once per reminder: for 'add 2 "
        "reminders' make two calls, each with its own category."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about"},
            "when": {
                "type": "string",
                "description": (
                    "Local date and time, ISO format without timezone, e.g. "
                    "2026-09-26T21:00. For repeating reminders, the first "
                    "occurrence (its time of day is used for every repeat)."
                ),
            },
            "repeat": {
                "type": "string",
                "description": (
                    "Only for repeating reminders: an RFC 5545 RRULE without "
                    "DTSTART, e.g. FREQ=DAILY, FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR, "
                    "FREQ=MONTHLY;BYMONTHDAY=1. Omit for a one-time reminder."
                ),
            },
            "category": _CATEGORY_PROPERTY,
            "remind_days_before": {
                "type": "integer",
                "description": (
                    "Also remind this many days before each occurrence "
                    "(1-30), e.g. 1 for 'the day before'. Omit for no early "
                    "reminder."
                ),
            },
            "no_time": {
                "type": "boolean",
                "description": (
                    "true for things to do on a day without a specific time "
                    "— they sit on the daily list with no ping. Use it whenever "
                    "the user didn't say a time for a task or routine; never "
                    "make up a time."
                ),
            },
        },
        "required": ["text", "when"],
    },
}

ADD_TODO_TOOL = {
    "name": "add_todo",
    "description": (
        "Add an item to the user's to-do list (the list Jarvis posts every "
        "morning). Use for tasks without a specific time; use "
        "create_reminder when the user wants to be pinged at a time."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The to-do item"},
            "due_date": {
                "type": "string",
                "description": "Local date YYYY-MM-DD it belongs to; omit for today",
            },
            "category": _CATEGORY_PROPERTY,
        },
        "required": ["text"],
    },
}

LIST_AGENDA_TOOL = {
    "name": "list_agenda",
    "description": (
        "Show the user's to-dos and reminders for a day. Use when they ask "
        "what's on their list or schedule, or to find the id of an item "
        "that isn't on today's list."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Local date YYYY-MM-DD; omit for today",
            },
        },
    },
}

FINISH_ITEM_TOOL = {
    "name": "finish_item",
    "description": (
        "Mark a to-do or reminder as done, or cancel it, by its id (like t3 "
        "or r5, from today's list or from list_agenda)."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "The item id, e.g. t3 or r5"},
            "action": {
                "type": "string",
                "enum": ["done", "cancel"],
                "description": "done = completed; cancel = remove / stop reminding",
            },
        },
        "required": ["item_id", "action"],
    },
}

EDIT_REMINDER_TOOL = {
    "name": "edit_reminder",
    "description": (
        "Change an existing reminder's text, date/time, repeat rule, "
        "category, or whether it pings at all. Use item_id (like r5) from "
        "today's list or list_agenda when you have it; otherwise use name "
        "— a distinctive word from the reminder's text — to look it up. "
        "Use time (HH:MM) for 'move X to HH:MM': it keeps the current date "
        "and only changes the time of day."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "The reminder's id, e.g. r5"},
            "name": {
                "type": "string",
                "description": (
                    "A distinctive word from the reminder's text, if you "
                    "don't have its item_id"
                ),
            },
            "text": {"type": "string", "description": "New text for the reminder"},
            "when": {
                "type": "string",
                "description": (
                    "New local date/datetime, ISO format, e.g. "
                    "2026-09-28T21:00 or 2026-09-28. Replaces the date (and, "
                    "for a timed reminder, the time of day)."
                ),
            },
            "time": {
                "type": "string",
                "description": (
                    "New time of day only, HH:MM, keeping the reminder's "
                    "current date — use this for 'move X to HH:MM'."
                ),
            },
            "repeat": {
                "type": "string",
                "description": (
                    "New RFC 5545 RRULE without DTSTART, or the literal "
                    "'none' to make it one-time."
                ),
            },
            "no_time": {
                "type": "boolean",
                "description": (
                    "true to make it an untimed item on the daily list with "
                    "no ping; false to make it timed again (needs time or "
                    "when with a time)."
                ),
            },
            "category": {
                "type": "string",
                "enum": list(CATEGORIES.keys()),
                "description": "Only when the user asks to change the category.",
            },
        },
        "required": [],
    },
}

FIND_REMINDERS_TOOL = {
    "name": "find_reminders",
    "description": (
        "Search ALL of the user's saved reminders — birthdays, anniversaries, "
        "subscriptions, supplements, routines, tasks — by name and/or category. "
        "Returns each match with its next date/time, how it repeats and its id. "
        "Use it for questions like 'when is X's birthday', 'list my birthdays', "
        "'what subscriptions do I have', 'when is my next workout', 'what "
        "reminders do I have'. Reminders are NOT stored in search_memory: "
        "always check here before saying a reminder or birthday doesn't exist."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A word or name from the reminder's text, e.g. 'Lazare', "
                    "'Grok', 'workout'. Omit to list everything (optionally "
                    "filtered by category)."
                ),
            },
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": "Optional category filter, e.g. 'birthday' or 'subscription'.",
            },
        },
    },
}

REMINDER_TOOLS = [
    CREATE_REMINDER_TOOL, ADD_TODO_TOOL, LIST_AGENDA_TOOL, FINISH_ITEM_TOOL, EDIT_REMINDER_TOOL,
    FIND_REMINDERS_TOOL,
]
