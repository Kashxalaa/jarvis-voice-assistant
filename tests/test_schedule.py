from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from brain.schedule import (
    ScheduleInputError,
    first_fire_at,
    is_late,
    next_occurrence,
    occurrences_on,
    parse_day,
    parse_when,
)

TZ = ZoneInfo("Asia/Tbilisi")
WEEKDAYS = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def test_parse_when_treats_naive_time_as_local():
    assert parse_when("2026-09-26T21:00", TZ) == at(26, 21)


def test_parse_when_converts_an_explicit_offset_to_local():
    # 17:00Z is 21:00 in Tbilisi — must not be read as 17:00 local.
    parsed = parse_when("2026-09-26T17:00:00Z", TZ)
    assert parsed == at(26, 21)
    assert parsed.utcoffset() == timedelta(hours=4)


def test_parse_when_rejects_garbage():
    with pytest.raises(ScheduleInputError):
        parse_when("tomorrow-ish", TZ)


def test_parse_day_defaults_to_today_and_parses_iso():
    assert parse_day(None, date(2026, 9, 26)) == date(2026, 9, 26)
    assert parse_day("", date(2026, 9, 26)) == date(2026, 9, 26)
    assert parse_day("2026-09-28", date(2026, 9, 26)) == date(2026, 9, 28)


def test_parse_day_rejects_garbage():
    with pytest.raises(ScheduleInputError):
        parse_day("friday", date(2026, 9, 26))


def test_first_fire_at_one_time_returns_the_time_itself():
    assert first_fire_at(None, at(26, 21), now=at(26, 10), tz=TZ) == at(26, 21)


def test_first_fire_at_one_time_allows_a_one_minute_grace():
    assert first_fire_at(None, at(26, 10), now=at(26, 10, 1), tz=TZ) == at(26, 10)


def test_first_fire_at_one_time_rejects_the_past():
    with pytest.raises(ScheduleInputError):
        first_fire_at(None, at(26, 9), now=at(26, 10), tz=TZ)


def test_first_fire_at_recurring_skips_to_next_matching_day():
    # Saturday 10:00, "weekdays at 09:00" starting today → Monday 09:00.
    assert first_fire_at(WEEKDAYS, at(26, 9), now=at(26, 10), tz=TZ) == at(28, 9)


def test_first_fire_at_recurring_includes_a_time_later_today():
    assert first_fire_at("FREQ=DAILY", at(26, 21), now=at(26, 10), tz=TZ) == at(26, 21)


def test_first_fire_at_rejects_minutely_rules():
    with pytest.raises(ScheduleInputError):
        first_fire_at("FREQ=MINUTELY", at(26, 21), now=at(26, 10), tz=TZ)


def test_first_fire_at_rejects_an_unparseable_rule():
    with pytest.raises(ScheduleInputError):
        first_fire_at("every tuesday", at(26, 21), now=at(26, 10), tz=TZ)


def test_first_fire_at_accepts_a_bare_date_only_until():
    # UNTIL=20261001 with no time means local end-of-day on 2026-10-01.
    assert first_fire_at(
        "FREQ=DAILY;UNTIL=20261001", at(26, 21), now=at(26, 10), tz=TZ
    ) == at(26, 21)


def test_first_fire_at_accepts_a_bare_datetime_until():
    assert first_fire_at(
        "FREQ=DAILY;UNTIL=20261001T000000", at(26, 21), now=at(26, 10), tz=TZ
    ) == at(26, 21)


def test_occurrences_on_a_bare_date_until_includes_its_last_day_but_not_the_next():
    rrule = "FREQ=DAILY;UNTIL=20261001"
    assert occurrences_on(rrule, at(26, 21), date(2026, 10, 1), TZ) == [
        datetime(2026, 10, 1, 21, 0, tzinfo=TZ)
    ]
    assert occurrences_on(rrule, at(26, 21), date(2026, 10, 2), TZ) == []


def test_first_fire_at_rejects_a_rule_with_dtstart():
    with pytest.raises(ScheduleInputError):
        first_fire_at(
            "DTSTART:20260926T210000\nRRULE:FREQ=DAILY",
            at(26, 21), now=at(26, 10), tz=TZ,
        )


def test_first_fire_at_rejects_a_rule_that_never_fires_again():
    with pytest.raises(ScheduleInputError):
        first_fire_at("FREQ=DAILY;COUNT=1", at(21, 9), now=at(26, 10), tz=TZ)


def test_next_occurrence_collapses_missed_occurrences():
    # Daily 09:00 since Monday; we're at Thursday 14:00 → Friday 09:00.
    assert next_occurrence("FREQ=DAILY", at(21, 9), after=at(24, 14), tz=TZ) == at(25, 9)


def test_next_occurrence_works_when_starts_at_is_in_utc():
    starts_utc = at(21, 9).astimezone(timezone.utc)
    assert next_occurrence("FREQ=DAILY", starts_utc, after=at(24, 14), tz=TZ) == at(25, 9)


def test_next_occurrence_is_none_for_a_one_time_reminder_in_the_past():
    assert next_occurrence(None, at(26, 9), after=at(26, 10), tz=TZ) is None


def test_occurrences_on_matching_and_non_matching_days():
    assert occurrences_on("FREQ=DAILY", at(21, 9), date(2026, 9, 25), TZ) == [at(25, 9)]
    assert occurrences_on(WEEKDAYS, at(21, 9), date(2026, 9, 26), TZ) == []


def test_occurrences_on_one_time():
    assert occurrences_on(None, at(26, 21), date(2026, 9, 26), TZ) == [at(26, 21)]
    assert occurrences_on(None, at(26, 21), date(2026, 9, 27), TZ) == []


def test_is_late_uses_a_five_minute_threshold():
    assert is_late(at(26, 21), now=at(26, 21, 5)) is False
    assert is_late(at(26, 21), now=at(26, 21, 6)) is True


# --- lead_days ("day before") ---------------------------------------------


def test_first_fire_at_one_time_with_lead_days_shifts_back():
    assert first_fire_at(
        None, at(26, 21), now=at(20, 10), tz=TZ, lead_days=1
    ) == at(25, 21)


def test_first_fire_at_one_time_lead_still_rejects_the_past():
    # The early time itself (main minus lead) has already passed.
    with pytest.raises(ScheduleInputError):
        first_fire_at(None, at(26, 21), now=at(26, 10), tz=TZ, lead_days=1)


def test_first_fire_at_recurring_with_lead_days_uses_the_day_before_the_main_occurrence():
    # Saturday 10:00: the next weekday occurrence is Monday 09:00, so the
    # early (1 day before) reminder's first fire is Sunday 09:00.
    assert first_fire_at(
        WEEKDAYS, at(26, 9), now=at(26, 10), tz=TZ, lead_days=1
    ) == at(27, 9)


def test_next_occurrence_with_lead_days_recurring():
    assert next_occurrence(
        "FREQ=DAILY", at(21, 9), after=at(24, 14), tz=TZ, lead_days=1
    ) == at(25, 9)


def test_next_occurrence_with_lead_days_one_time():
    assert next_occurrence(
        None, at(26, 21), after=at(24, 10), tz=TZ, lead_days=1
    ) == at(25, 21)
    assert next_occurrence(
        None, at(26, 21), after=at(25, 21), tz=TZ, lead_days=1
    ) is None


def test_occurrences_on_bymonthday_lead_fires_the_day_before_not_on_it():
    rrule = "FREQ=MONTHLY;BYMONTHDAY=28"
    starts = datetime(2026, 1, 28, 10, 0, tzinfo=TZ)

    assert occurrences_on(rrule, starts, date(2026, 2, 27), TZ, lead_days=1) == [
        datetime(2026, 2, 27, 10, 0, tzinfo=TZ)
    ]
    assert occurrences_on(rrule, starts, date(2026, 2, 28), TZ, lead_days=1) == []


def test_occurrences_on_weekly_byday_lead():
    starts = datetime(2026, 9, 21, 9, 30, tzinfo=TZ)  # Monday
    assert occurrences_on(WEEKDAYS, starts, date(2026, 9, 27), TZ, lead_days=1) == [
        datetime(2026, 9, 27, 9, 30, tzinfo=TZ)
    ]


def test_occurrences_on_daily_interval_lead():
    starts = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)  # Monday
    rrule = "FREQ=DAILY;INTERVAL=2"  # Mon, Wed, Fri, ...
    assert occurrences_on(rrule, starts, date(2026, 9, 22), TZ, lead_days=1) == [
        datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
    ]


def test_occurrences_on_yearly_lead_crosses_a_month_boundary():
    rrule = "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1"
    starts = datetime(2020, 3, 1, 10, 0, tzinfo=TZ)

    # 2027 is not a leap year: 1 March's day-before is 28 Feb.
    assert occurrences_on(rrule, starts, date(2027, 2, 28), TZ, lead_days=1) == [
        datetime(2027, 2, 28, 10, 0, tzinfo=TZ)
    ]
    # 2028 is a leap year: 1 March's day-before is 29 Feb.
    assert occurrences_on(rrule, starts, date(2028, 2, 29), TZ, lead_days=1) == [
        datetime(2028, 2, 29, 10, 0, tzinfo=TZ)
    ]


def test_occurrences_on_yearly_lead_crosses_a_year_boundary():
    rrule = "FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1"
    starts = datetime(2020, 1, 1, 10, 0, tzinfo=TZ)

    assert occurrences_on(rrule, starts, date(2026, 12, 31), TZ, lead_days=1) == [
        datetime(2026, 12, 31, 10, 0, tzinfo=TZ)
    ]


def test_occurrences_on_one_time_lead_shows_on_the_previous_day():
    starts = at(26, 21)
    assert occurrences_on(None, starts, date(2026, 9, 25), TZ, lead_days=1) == [
        at(25, 21)
    ]
    assert occurrences_on(None, starts, date(2026, 9, 26), TZ, lead_days=1) == []
