import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

LATE_THRESHOLD = timedelta(minutes=5)
PAST_GRACE = timedelta(minutes=1)
_FORBIDDEN_FREQUENCIES = ("SECONDLY", "MINUTELY")
_BARE_UNTIL = re.compile(r"(UNTIL=)(\d{8})(T\d{6})?(Z?)", re.IGNORECASE)


class ScheduleInputError(ValueError):
    """A requested time or repeat rule can't be used; the message is user-facing."""


def parse_when(when: str, tz: ZoneInfo) -> datetime:
    try:
        # fromisoformat accepts a trailing "Z" since Python 3.11.
        parsed = datetime.fromisoformat(when.strip())
    except (ValueError, AttributeError):
        raise ScheduleInputError(f"I couldn't understand the time {when!r}.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_day(value: str | None, today: date) -> date:
    if not value or not value.strip():
        return today
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ScheduleInputError(f"I couldn't understand the date {value!r}.")


def _localize_bare_until(rrule: str, tz: ZoneInfo) -> str:
    """Rewrite a bare (timezone-less) UNTIL into a UTC moment with a Z suffix.

    Gemini naturally writes UNTIL=20261231 or UNTIL=20261231T235959 without a
    trailing Z, which dateutil rejects once DTSTART is timezone-aware. A
    date-only value means local end of that day.
    """

    def replace(match: re.Match) -> str:
        if match[4]:  # already has a trailing Z — leave it alone
            return match[0]
        year, month, day = int(match[2][:4]), int(match[2][4:6]), int(match[2][6:8])
        if match[3]:
            hour, minute, second = int(match[3][1:3]), int(match[3][3:5]), int(match[3][5:7])
        else:
            hour, minute, second = 23, 59, 59
        local_moment = datetime(year, month, day, hour, minute, second, tzinfo=tz)
        utc_moment = local_moment.astimezone(timezone.utc)
        return f"{match[1]}{utc_moment:%Y%m%dT%H%M%S}Z"

    return _BARE_UNTIL.sub(replace, rrule)


def _rule(rrule: str, starts_at: datetime, tz: ZoneInfo):
    upper = rrule.upper()
    if "DTSTART" in upper:
        raise ScheduleInputError("Please give the repeat rule without a start date.")
    if any(f"FREQ={freq}" in upper for freq in _FORBIDDEN_FREQUENCIES):
        raise ScheduleInputError("Reminders can repeat at most once an hour.")
    rrule = _localize_bare_until(rrule, tz)
    try:
        # Recurrence is evaluated on local wall-clock time, so DTSTART must
        # be in the local zone even if the database handed back UTC.
        return rrulestr(rrule, dtstart=starts_at.astimezone(tz))
    except (ValueError, TypeError) as exc:
        raise ScheduleInputError(f"I couldn't understand the repeat rule {rrule!r}.") from exc


def _evaluate(fn, rrule: str):
    """Run a rule evaluation (.after()/.between()), turning a stray TypeError
    (e.g. from a naive-vs-aware comparison dateutil didn't catch) into a
    friendly ScheduleInputError instead of letting it crash the caller."""
    try:
        return fn()
    except TypeError as exc:
        raise ScheduleInputError(f"I couldn't understand the repeat rule {rrule!r}.") from exc


def first_fire_at(
    rrule: str | None, starts_at: datetime, now: datetime, tz: ZoneInfo,
    lead_days: int = 0,
) -> datetime:
    lead = timedelta(days=lead_days)
    if rrule is None:
        candidate = starts_at.astimezone(tz) - lead
        if candidate < now - PAST_GRACE:
            raise ScheduleInputError("That time has already passed.")
        return candidate
    rule = _rule(rrule, starts_at, tz)
    first = _evaluate(
        lambda: rule.after(now.astimezone(tz) + lead, inc=True), rrule
    )
    if first is None:
        raise ScheduleInputError("That repeat rule never fires again.")
    return first - lead


def next_occurrence(
    rrule: str | None, starts_at: datetime, after: datetime, tz: ZoneInfo,
    lead_days: int = 0,
) -> datetime | None:
    lead = timedelta(days=lead_days)
    if rrule is None:
        candidate = starts_at.astimezone(tz) - lead
        return candidate if candidate > after else None
    rule = _rule(rrule, starts_at, tz)
    main = _evaluate(
        lambda: rule.after(after.astimezone(tz) + lead, inc=False), rrule
    )
    return None if main is None else main - lead


def occurrences_on(
    rrule: str | None, starts_at: datetime, day: date, tz: ZoneInfo,
    lead_days: int = 0,
) -> list[datetime]:
    lead = timedelta(days=lead_days)
    # The early ("day before") row shares its main reminder's starts_at/rrule,
    # so to find its occurrence on `day` we look for the MAIN occurrence on
    # `day + lead_days` and shift it back — never by shifting DTSTART, which
    # would silently break explicit BYMONTHDAY/BYDAY/BYMONTH rules.
    search_day = day + timedelta(days=lead_days)
    start = datetime.combine(search_day, time(), tzinfo=tz)
    end = start + timedelta(days=1)
    if rrule is None:
        local = starts_at.astimezone(tz)
        return [local - lead] if start <= local < end else []
    rule = _rule(rrule, starts_at, tz)
    occurrences = _evaluate(lambda: rule.between(start, end, inc=True), rrule)
    return [o - lead for o in occurrences if o < end]


def is_late(due_at: datetime, now: datetime) -> bool:
    return now - due_at > LATE_THRESHOLD
