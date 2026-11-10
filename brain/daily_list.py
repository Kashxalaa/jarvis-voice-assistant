import asyncio
import concurrent.futures
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from brain.memory import get_all_facts
from brain.reminders import (
    CATEGORIES,
    DONE_PING_COLOR,
    PING_COLORS,
    ItemNotFoundError,
    agenda_items,
    claim_due_reminders,
    claim_ping_for_reping,
    get_nudge_date,
    is_ping_superseded,
    ping_row_exists,
    pings_due_for_reping,
    reping_still_needed,
    release_ping_claim,
    set_nudge_date,
    supersede_chain,
    clear_list_dirty,
    clear_ping_needs_render,
    display_text,
    drop_ping,
    get_daily_list_state,
    get_ping_by_message_id,
    get_reminder_info,
    get_reminder_ping_state,
    is_early_reminder_text,
    mark_list_dirty,
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
from brain.schedule import is_late
from brain.web_tools import forecast_day_line, forecast_hour_line

PING_PRUNE_AFTER_DAYS = 7
# An unanswered ping is re-sent this often, at most MAX_REPINGS times (so an
# occurrence pings 1 + MAX_REPINGS times in total).
REPING_INTERVAL = timedelta(minutes=60)
MAX_REPINGS = 3

logger = logging.getLogger(__name__)

MAX_BUTTONS = 25
MAX_CONTENT = 1900
_REMINDER_BUTTON = re.compile(r"rem:(done|s10|s60):(\d+)")
_LIST_BUTTON = re.compile(r"list:([tr]\d+)")
_SNOOZE_MINUTES = {"s10": 10, "s60": 60}
# Weather is a nice-to-have on the list/pings — never worth blocking or
# meaningfully delaying either for. Capped hard at ~6s regardless of how
# slow (or hung) the weather service is being.
_WEATHER_FETCH_TIMEOUT_SECONDS = 6
# A dedicated, small pool for weather calls only — kept separate from
# asyncio's default to_thread executor (shared with every DB call this
# module makes) so a hung weather fetch can never starve those out of a
# worker thread. wait_for's cancellation above doesn't actually kill a
# blocking call already running on a thread; isolating it here just bounds
# the damage to at most 2 stuck threads, in a pool nothing else uses.
_WEATHER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="weather-fetch",
)
# How long after the list is first posted a still-null weather line may be
# retried on a later same-day refresh (e.g. the 09:00 fetch failed, but a
# 09:30 refresh — triggered by something else going dirty — gets another
# shot). No extra state is stored for this: it's inferred from list_time.
_WEATHER_RETRY_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class ButtonSpec:
    custom_id: str
    label: str
    done: bool


@dataclass(frozen=True)
class ListRender:
    title: str
    body: str
    footer: str
    buttons: list[ButtonSpec]


@dataclass(frozen=True)
class PingCard:
    title: str
    description: str
    color: int
    buttons: bool  # whether the Done/Snooze view belongs on this card


@dataclass(frozen=True)
class ButtonOutcome:
    kind: str  # "reminder" or "list"
    card: PingCard | None = None
    list_render: ListRender | None = None
    list_message_id: int | None = None
    # Done on a ping: delete the message to save space (card is the fallback).
    delete_message: bool = False


def _since_label(since: date, day: date) -> str:
    if (day - since).days < 7:
        return f"since {since:%a}"
    return f"since {since.day} {since:%b}"


def _line_for_item(number: int, item: dict, day: date, with_id: bool = False) -> str:
    text = display_text(item["text"], item.get("category"))
    if item["since"]:
        text = f"{text} ↪ {_since_label(item['since'], day)}"
    if item["done"]:
        line = f"✅  ~~{text}~~"
    elif item["time"]:
        line = f"**{number}**  `{item['time']}`  {text}"
    else:
        line = f"**{number}**  {text}"
    return f"[{item['id']}] {line}" if with_id else line


_NO_CATEGORY = object()  # sentinel: distinct from any real category, including None


def _iter_body_lines(items: list[dict], day: date, with_id: bool = False):
    """Yields (line, is_item, button_or_None) — a header/blank line has
    is_item False and no button; an item line (done or not) has is_item True
    and its ButtonSpec, numbered continuously across category groups."""
    groups = []
    current_category = _NO_CATEGORY
    for it in items:
        category = it.get("category")
        if category != current_category or not groups:
            groups.append((category, []))
            current_category = category
        groups[-1][1].append(it)

    number = 0
    for group_index, (category, group_items) in enumerate(groups):
        if group_index > 0:
            yield "", False, None
        header = CATEGORIES.get(category, CATEGORIES["other"])
        yield f"**{header}**", False, None
        for it in group_items:
            number += 1
            line = _line_for_item(number, it, day, with_id=with_id)
            label = f"✓{number}" if it["done"] else str(number)
            button = ButtonSpec(f"list:{it['id']}", label, it["done"])
            yield line, True, button


def _truncate_body(
    lines: list[str], is_item_flags: list[bool], total_items: int, max_content: int = MAX_CONTENT,
) -> tuple[str, int]:
    kept: list[str] = []
    items_kept = 0
    for line, is_item in zip(lines, is_item_flags):
        remaining = total_items - items_kept
        footer_line = f"… and {remaining} more — ask me for the full list."
        if len("\n".join(kept + [line, footer_line])) > max_content:
            # Never leave a category header (or the blank line before it)
            # dangling as the last visible content with none of its items
            # shown — drop it.
            while kept and (kept[-1] == "" or (kept[-1].startswith("**") and kept[-1].endswith("**"))):
                kept.pop()
            kept.append(footer_line)
            break
        kept.append(line)
        if is_item:
            items_kept += 1
    return "\n".join(kept), items_kept


def render_list(items: list[dict], day: date, weather: str | None = None) -> ListRender:
    title = f"📋 {day:%A}, {day.day} {day:%B}"
    if not items:
        return ListRender(title, "Nothing on the list — all clear. 🎉", "", [])

    entries = list(_iter_body_lines(items, day))
    lines = [entry[0] for entry in entries]
    is_item_flags = [entry[1] for entry in entries]
    all_buttons = [entry[2] for entry in entries if entry[2] is not None][:MAX_BUTTONS]
    total_items = sum(is_item_flags)

    # The weather line (plus its blank-line separator) eats into the same
    # 1900-char budget as the item lines, so it must be subtracted from the
    # budget up front rather than appended after truncation already ran
    # against the full MAX_CONTENT.
    weather_prefix = f"{weather}\n\n" if weather else ""
    items_budget = MAX_CONTENT - len(weather_prefix)

    items_body = "\n".join(lines)
    buttons = all_buttons
    if len(items_body) > items_budget:
        items_body, items_kept = _truncate_body(lines, is_item_flags, total_items, items_budget)
        buttons = all_buttons[:items_kept]

    body = weather_prefix + items_body

    done_count = sum(1 for it in items if it["done"])
    footer = f"{done_count} of {total_items} done"
    return ListRender(title, body, footer, buttons)


def agenda_text(items: list[dict], day: date, with_ids: bool = False) -> str:
    if not items:
        return f"Nothing scheduled for {day:%A} {day.day} {day:%b}."
    if not with_ids:
        render = render_list(items, day)
        return f"{render.title}\n\n{render.body}"

    # A separate, simpler renderer for Gemini's eyes only — no buttons, no
    # length truncation, never used for the pinned Discord list — but it
    # reuses _line_for_item's own formatting via with_id=True rather than a
    # second hand-duplicated copy of it. Ids let Gemini act on items from
    # other days (edit_reminder, finish_item) without a fuzzy name lookup.
    title = f"📋 {day:%A}, {day.day} {day:%B}"
    lines = [entry[0] for entry in _iter_body_lines(items, day, with_id=True)]
    return f"{title}\n\n" + "\n".join(lines)


def today_items_prompt(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["Today's list (use these ids with finish_item):"]
    for item in items:
        text = display_text(item["text"], item.get("category"))
        text = f"{item['time']} {text}" if item["time"] else text
        lines.append(f"- [{item['id']}] {text}{' (done)' if item['done'] else ''}")
    return "\n".join(lines)


# Discord embed titles are capped at 256 chars; leave room for the "~~" the
# done card wraps the title in (4 chars) plus the "…" clip marker.
_TITLE_LIMIT = 250


def _card_title_and_overflow(text: str, wrap_strikethrough: bool) -> tuple[str, str | None]:
    """Returns (title, overflow) — overflow is the untouched `text` when it
    had to be clipped to fit Discord's embed title limit, so the caller can
    put the full text in the description instead; None when nothing needed
    clipping."""
    if len(text) <= _TITLE_LIMIT:
        title = f"~~{text}~~" if wrap_strikethrough else text
        return title, None
    clipped = text[: _TITLE_LIMIT - 1] + "…"
    title = f"~~{clipped}~~" if wrap_strikethrough else clipped
    return title, text


def active_ping_card(
    text: str, category: str | None, time_label: str, late_for: str | None = None,
    weather: str | None = None,
) -> PingCard:
    title, overflow = _card_title_and_overflow(display_text(text, category), wrap_strikethrough=False)
    description = f"`{time_label}` · {CATEGORIES.get(category, CATEGORIES['other'])}"
    if late_for:
        description += f" · late — was due {late_for}"
    if overflow:
        description = f"{overflow}\n{description}"
    if weather:
        description += f"\n{weather}"
    color = PING_COLORS.get(category, PING_COLORS["other"])
    return PingCard(title, description, color, True)


def reping_card(
    text: str, category: str | None, time_label: str, ping_number: int, ping_total: int,
) -> PingCard:
    title, overflow = _card_title_and_overflow(
        f"Still to do: {display_text(text, category)}", wrap_strikethrough=False,
    )
    description = (
        f"`{time_label}` · {CATEGORIES.get(category, CATEGORIES['other'])}"
        f" · reminder {ping_number} of {ping_total}"
    )
    if overflow:
        description = f"{overflow}\n{description}"
    color = PING_COLORS.get(category, PING_COLORS["other"])
    return PingCard(title, description, color, True)




NUDGE_TITLE = "🔔 Still on your list today"


def nudge_lines(items: list[dict], day: date) -> list[str]:
    """Bullet lines for today's undone untimed reminders and to-dos."""
    lines = []
    for it in items:
        if it["done"] or it["time"] is not None:
            continue
        text = display_text(it["text"], it.get("category"))
        if it["since"]:
            text = f"{text} ↪ {_since_label(it['since'], day)}"
        lines.append(f"• {text}")
    return lines


def done_ping_card(text: str, category: str | None, done_at_label: str) -> PingCard:
    title, overflow = _card_title_and_overflow(display_text(text, category), wrap_strikethrough=True)
    description = f"✅ Done at {done_at_label} · {CATEGORIES.get(category, CATEGORIES['other'])}"
    if overflow:
        description = f"{overflow}\n{description}"
    return PingCard(title, description, DONE_PING_COLOR, False)


def snoozed_ping_card(text: str, category: str | None, until_label: str) -> PingCard:
    title, overflow = _card_title_and_overflow(display_text(text, category), wrap_strikethrough=False)
    description = f"💤 Snoozed until {until_label} · {CATEGORIES.get(category, CATEGORIES['other'])}"
    if overflow:
        description = f"{overflow}\n{description}"
    color = PING_COLORS.get(category, PING_COLORS["other"])
    return PingCard(title, description, color, False)


def apply_button(
    conn, custom_id: str, now: datetime, tz: ZoneInfo, message_id: int | None = None,
) -> ButtonOutcome:
    today = now.astimezone(tz).date()
    match = _REMINDER_BUTTON.fullmatch(custom_id)
    if match:
        action, reminder_id = match[1], int(match[2])
        info = get_reminder_info(conn, reminder_id)
        # The clicked ping's own recorded row, if any — an old, pre-deploy
        # ping (or a click routed without a message id) has none, so fall
        # back to today. Using the ping's own fired_on (not "today") matters
        # for a stale ping: clicking Done on YESTERDAY's ping must mark
        # yesterday done, not today — otherwise today's own occurrence would
        # be wrongly skipped as "already done today".
        ping_row = get_ping_by_message_id(conn, message_id) if message_id is not None else None
        fired_on = ping_row["fired_on"] if ping_row is not None else today
        if action == "done":
            mark_reminder_done(conn, reminder_id, fired_on)
            if ping_row is not None:
                # The caller deletes the clicked message — forget its row so the
                # sync tick never tries to redraw a message that no longer exists.
                drop_ping(conn, ping_row["id"])
            card = done_ping_card(info["text"], info["category"], f"{now.astimezone(tz):%H:%M}")
            return ButtonOutcome("reminder", card=card, delete_message=True)
        else:
            until = snooze_reminder(conn, reminder_id, _SNOOZE_MINUTES[action], now)
            if ping_row is not None:
                # A snoozed card must never get its buttons back via the
                # sync tick — drop its row entirely. The snooze copy's own
                # later ping gets a fresh row when it fires.
                drop_ping(conn, ping_row["id"])
                # ...and the rest of its chain must stop re-pinging too, even
                # if the snooze came from a stale, already-superseded ping.
                supersede_chain(conn, reminder_id, ping_row["due_at"])
            card = snoozed_ping_card(info["text"], info["category"], f"{until.astimezone(tz):%H:%M}")
        return ButtonOutcome("reminder", card=card)

    match = _LIST_BUTTON.fullmatch(custom_id)
    if match:
        toggle_item(conn, match[1], today, now)
        state = get_daily_list_state(conn)
        # Reuse today's already-stored weather line — a button tap must
        # never drop it from the re-rendered list until the next tick
        # happens to refresh it. Only valid when the stored state is
        # actually for today: a stale row (list_date still yesterday's,
        # e.g. before the day's first tick has run) must not leak
        # yesterday's line onto today's render.
        weather = state["weather_line"] if state["list_date"] == today else None
        render = render_list(agenda_items(conn, today, tz), today, weather)
        # The stored message id is whatever is currently pinned — even if it
        # was posted for a stale list_date — so a click on it edits that
        # message in place with today's freshly rendered list. Only a click
        # on some other, older message (handled by the caller comparing
        # interaction.message.id) falls back to the ephemeral notice.
        return ButtonOutcome("list", list_render=render, list_message_id=state["message_id"])

    raise ItemNotFoundError(custom_id)


class ListMessageMissing(Exception):
    """The stored daily-list message no longer exists on Discord."""


class PingMessageMissing(Exception):
    """A tracked ping message no longer exists on Discord."""


async def _get_location(conn) -> str | None:
    try:
        facts = await asyncio.to_thread(get_all_facts, conn)
    except Exception as exc:
        logger.warning("fetching location fact failed: %s", exc)
        return None
    return facts.get("location")


async def _run_weather_call(call):
    """Runs the zero-arg `call` on the dedicated weather thread pool with a
    hard timeout cap. Never raises — any exception (including the timeout)
    is the caller's problem to swallow, since weather must never block or
    break the list/ping it's attached to."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_WEATHER_EXECUTOR, call), _WEATHER_FETCH_TIMEOUT_SECONDS,
    )


async def _fetch_weather_line(conn, fetch) -> str | None:
    """Runs `fetch(location)` on the dedicated weather pool with a hard cap,
    returning None on any failure, timeout, or missing home-location fact."""
    location = await _get_location(conn)
    if not location:
        return None
    try:
        return await _run_weather_call(lambda: fetch(location))
    except Exception as exc:
        logger.warning("fetching weather failed: %s", exc)
        return None


async def _fetch_hour_weather(conn, tz: ZoneInfo, due_at: datetime, cache: dict) -> str | None:
    """Fetches the fitness-ping forecast for `due_at`'s local hour, memoized
    in `cache` per (location, local hour) — `run_scheduler_tick` passes a
    fresh dict each tick, so several fitness reminders due the same hour
    share one Open-Meteo request instead of firing one each."""
    location = await _get_location(conn)
    if not location:
        return None
    local = due_at.astimezone(tz)
    key = (location, local.date(), local.hour)
    if key in cache:
        return cache[key]
    try:
        result = await _run_weather_call(lambda: forecast_hour_line(location, due_at))
    except Exception as exc:
        logger.warning("fetching weather failed: %s", exc)
        result = None
    cache[key] = result
    return result


def _within_weather_retry_window(local_now: datetime, list_time: time) -> bool:
    anchor = local_now.date()
    list_posted_at = datetime.combine(anchor, list_time, tzinfo=local_now.tzinfo)
    return local_now < list_posted_at + _WEATHER_RETRY_WINDOW


def effective_list_time(items: list[dict], list_time: time, lead_minutes: int) -> time:
    """The actual local time the daily list posts on a day with `items`.

    TODO_LIST_TIME (`list_time`) is the latest the list ever posts — but if
    any timed item's earliest occurrence is less than `lead_minutes` after
    that, the list posts `lead_minutes` before that occurrence instead (so
    it's up before the first ping), clamped at local midnight. Untimed
    items and to-dos never move the post time earlier; a done timed item
    still counts, since it was still the day's first occurrence.
    """
    timed = [item["time"] for item in items if item.get("time")]
    if not timed:
        return list_time
    first = min(datetime.strptime(t, "%H:%M").time() for t in timed)
    lead_adjusted_minutes = first.hour * 60 + first.minute - lead_minutes
    if lead_adjusted_minutes < 0:
        lead_adjusted = time(0, 0)
    else:
        lead_adjusted = time(lead_adjusted_minutes // 60, lead_adjusted_minutes % 60)
    return min(list_time, lead_adjusted)


async def run_scheduler_tick(
    conn, sender, now: datetime, tz: ZoneInfo, list_time: time, lead_minutes: int = 30,
    nudge_time: time = time(19, 30),
) -> None:
    claimed = await asyncio.to_thread(claim_due_reminders, conn, now, tz)
    # Fresh per tick — memoizes forecast_hour_line across several fitness
    # reminders claimed in the same tick that fall in the same local hour.
    hour_weather_cache: dict = {}
    for reminder in claimed:
        due_at = reminder["due_at"]
        time_label = due_at.astimezone(tz).strftime("%H:%M")
        late_for = time_label if is_late(due_at, now) else None
        weather = None
        # Checking the text prefix rather than lead_days > 0: a snoozed copy
        # of an early reminder keeps its "Tomorrow: "/"In N days: " text
        # (copied verbatim by snooze_reminder) but has lead_days reset to 0,
        # so a lead_days-based check would wrongly treat it as the main
        # occurrence and fetch weather for it.
        if reminder["category"] == "fitness" and not is_early_reminder_text(reminder["text"]):
            weather = await _fetch_hour_weather(conn, tz, due_at, hour_weather_cache)
        try:
            message_id = await sender.send_reminder(
                reminder["id"], reminder["text"], reminder["category"], time_label, late_for,
                weather,
            )
        except Exception as exc:
            logger.warning("sending reminder %s failed, will retry: %s", reminder["id"], exc)
            try:
                await asyncio.to_thread(restore_reminder, conn, reminder["id"], due_at)
            except Exception as restore_exc:
                logger.warning("restoring reminder %s failed: %s", reminder["id"], restore_exc)
            continue
        try:
            fired_on = due_at.astimezone(tz).date()
            await asyncio.to_thread(
                record_ping, conn, reminder["id"], message_id, fired_on, due_at, 1, now,
            )
        except Exception as exc:
            logger.warning("failed to record ping for reminder %s: %s", reminder["id"], exc)
    try:
        await _send_repings(conn, sender, now, tz)
    except Exception as exc:
        logger.warning("re-pinging failed: %s", exc)
    try:
        await _sync_pings(conn, sender, now, tz)
    except Exception as exc:
        # A failure syncing pings must not stop today's list from posting
        # or refreshing.
        logger.warning("syncing pings failed: %s", exc)
    try:
        cutoff = now.astimezone(tz).date() - timedelta(days=PING_PRUNE_AFTER_DAYS)
        await asyncio.to_thread(prune_old_pings, conn, cutoff)
    except Exception as exc:
        logger.warning("pruning old pings failed: %s", exc)
    await _sync_daily_list(conn, sender, now, tz, list_time, lead_minutes)
    try:
        await _send_nudge(conn, sender, now, tz, nudge_time)
    except Exception as exc:
        logger.warning("evening nudge failed: %s", exc)


async def _send_repings(conn, sender, now: datetime, tz: ZoneInfo) -> None:
    due = await asyncio.to_thread(pings_due_for_reping, conn, now, REPING_INTERVAL, MAX_REPINGS, tz)
    for ping in due:
        # Claim (mark superseded) BEFORE sending so a second tick can't
        # send the same re-ping; un-claim if the send fails so it retries.
        if not await asyncio.to_thread(claim_ping_for_reping, conn, ping["id"]):
            continue
        # A click (Done/Snooze/list tick) may have landed since the query.
        if not await asyncio.to_thread(reping_still_needed, conn, ping["reminder_id"], ping["fired_on"]):
            await asyncio.to_thread(release_ping_claim, conn, ping["id"])
            continue
        time_label = ping["due_at"].astimezone(tz).strftime("%H:%M")
        number = ping["ping_number"] + 1
        # NOTE: a crash between the claim above and the send, or record_ping
        # failing after a successful send, silently ends this chain. That is
        # intentional: it fails safe (never a double send), at worst one
        # missed re-ping.
        try:
            message_id = await sender.send_reminder(
                ping["reminder_id"], ping["text"], ping["category"], time_label, None, None,
                ping_number=number, ping_total=MAX_REPINGS + 1,
            )
        except Exception as exc:
            logger.warning("re-ping of reminder %s failed, will retry: %s", ping["reminder_id"], exc)
            try:
                await asyncio.to_thread(release_ping_claim, conn, ping["id"])
            except Exception as release_exc:
                logger.warning("releasing re-ping claim %s failed: %s", ping["id"], release_exc)
            continue
        try:
            await asyncio.to_thread(
                record_ping, conn, ping["reminder_id"], message_id, ping["fired_on"],
                ping["due_at"], number, now,
            )
        except Exception as exc:
            logger.warning("failed to record re-ping for reminder %s: %s", ping["reminder_id"], exc)
            continue
        # Re-check after the send: a click during it must leave exactly one
        # message in the channel, showing the right state.
        try:
            snoozed = not await asyncio.to_thread(ping_row_exists, conn, ping["id"])
            needed = await asyncio.to_thread(
                reping_still_needed, conn, ping["reminder_id"], ping["fired_on"],
            )
            if snoozed:
                # The clicked (old) message already shows "Snoozed"; the new
                # one would be a live duplicate, so remove it.
                await asyncio.to_thread(supersede_chain, conn, ping["reminder_id"], ping["due_at"])
                await sender.delete_ping(message_id)
                continue
            if not needed:
                state = await asyncio.to_thread(
                    get_reminder_ping_state, conn, ping["reminder_id"], ping["fired_on"],
                )
                if state is not None and state["done"]:
                    # Done mid-send: the new row renders as the done card on
                    # the next sync; the old message goes as usual.
                    await asyncio.to_thread(mark_pings_dirty, conn, ping["reminder_id"], ping["fired_on"])
                    await sender.delete_ping(ping["message_id"])
                else:
                    # Cancelled (or otherwise resolved): nothing left to show.
                    await asyncio.to_thread(supersede_chain, conn, ping["reminder_id"], ping["due_at"])
                    await sender.delete_ping(message_id)
                    await sender.delete_ping(ping["message_id"])
                continue
        except Exception as exc:
            logger.warning("post-send re-check for ping %s failed: %s", ping["id"], exc)
        # Only the latest ping of a chain stays in the channel; the one it
        # replaces is deleted so re-pings don't pile up in the chat.
        await sender.delete_ping(ping["message_id"])


async def _send_nudge(conn, sender, now: datetime, tz: ZoneInfo, nudge_time: time) -> None:
    local_now = now.astimezone(tz)
    today = local_now.date()
    if local_now.time() < nudge_time:
        return
    previous = await asyncio.to_thread(get_nudge_date, conn)
    if previous == today:
        return
    # Claim the day first so a slow send can't be doubled by the next tick.
    await asyncio.to_thread(set_nudge_date, conn, today)
    try:
        items = await asyncio.to_thread(agenda_items, conn, today, tz)
        lines = nudge_lines(items, today)
        if lines:
            await sender.send_nudge(lines)
    except Exception:
        await asyncio.to_thread(set_nudge_date, conn, previous)
        raise


async def _sync_pings(conn, sender, now: datetime, tz: ZoneInfo) -> None:
    pending = await asyncio.to_thread(pings_needing_render, conn)
    for ping in pending:
        # Clear the flag BEFORE reading state and editing, not after: a
        # concurrent toggle landing while we're mid-edit re-flags the row via
        # its own fresh UPDATE, and since we never clear again afterward,
        # that re-flag survives instead of being clobbered by us.
        await asyncio.to_thread(clear_ping_needs_render, conn, ping["id"])
        try:
            state = await asyncio.to_thread(
                get_reminder_ping_state, conn, ping["reminder_id"], ping["fired_on"]
            )
            if state is None:
                await asyncio.to_thread(drop_ping, conn, ping["id"])
                continue
            if await asyncio.to_thread(is_ping_superseded, conn, ping["id"]):
                # A replaced ping's message was deleted when it was re-pinged.
                continue
            if state["done"]:
                # Done (ticked in the pinned list): delete the ping to save space.
                await sender.delete_ping(ping["message_id"])
                await asyncio.to_thread(drop_ping, conn, ping["id"])
                continue
            else:
                # No late_for here: this restores the card after an un-tick,
                # possibly long after the original due time, and lateness
                # computed against the tick's own "now" would show "late"
                # for almost any restore.
                time_label = ping["due_at"].astimezone(tz).strftime("%H:%M")
                if ping["ping_number"] > 1:
                    card = reping_card(
                        state["text"], state["category"], time_label,
                        ping["ping_number"], MAX_REPINGS + 1,
                    )
                else:
                    card = active_ping_card(state["text"], state["category"], time_label)
            await sender.edit_ping(ping["message_id"], ping["reminder_id"], card)
        except PingMessageMissing:
            await asyncio.to_thread(drop_ping, conn, ping["id"])
        except Exception as exc:
            logger.warning("failed to sync ping %s: %s", ping["id"], exc)
            await asyncio.to_thread(mark_ping_needs_render, conn, ping["id"])


async def _sync_daily_list(conn, sender, now, tz, list_time, lead_minutes: int = 30) -> None:
    local_now = now.astimezone(tz)
    today = local_now.date()
    state = await asyncio.to_thread(get_daily_list_state, conn)
    new_day = state["list_date"] != today

    items = None
    if new_day and local_now.time() < list_time:
        # TODO_LIST_TIME is only the cap — a day whose first timed item
        # fires soon enough can post earlier than that. Only fetch here
        # (an extra DB query most ticks never pay) when the wall clock
        # hasn't reached the cap yet, so it's actually worth checking
        # whether the lead-adjusted time has already arrived.
        items = await asyncio.to_thread(agenda_items, conn, today, tz)
        if local_now.time() < effective_list_time(items, list_time, lead_minutes):
            return
    if not new_day and not state["needs_refresh"]:
        return

    # Clear first so a change made while we render isn't lost; re-mark on
    # failure so the next tick tries again.
    await asyncio.to_thread(clear_list_dirty, conn)
    try:
        if items is None:
            items = await asyncio.to_thread(agenda_items, conn, today, tz)

        if new_day and items:
            # Only a new day's first post needs a fresh fetch — and only
            # when there's actually something to post; an empty day never
            # posts at all, so there's no point fetching for it.
            weather = await _fetch_weather_line(
                conn, lambda location: forecast_day_line(location, today, tz)
            )
        elif new_day:
            weather = None
        elif state["weather_line"] is None and _within_weather_retry_window(
            local_now, effective_list_time(items, list_time, lead_minutes)
        ):
            # The new-day fetch failed earlier — since this refresh is
            # happening anyway (something else went dirty), and we're still
            # within an hour of the (possibly lead-adjusted) list time, give
            # it one more shot rather than leaving the list without weather
            # for the rest of the day.
            weather = await _fetch_weather_line(
                conn, lambda location: forecast_day_line(location, today, tz)
            )
        else:
            # A same-day refresh (or a repost after the message was deleted)
            # reuses the line already stored for today — never refetched.
            weather = state["weather_line"]

        render = render_list(items, today, weather)

        if not new_day and weather != state["weather_line"]:
            # A same-day retry (see above) just fetched a value that must be
            # persisted here — the edit_list path just below returns right
            # after editing and never otherwise touches daily_list, so a
            # retried line would be re-fetched on every dirty refresh within
            # the window instead of once.
            await asyncio.to_thread(save_daily_list_state, conn, today, state["message_id"], weather)

        if not new_day and state["message_id"] is not None:
            try:
                await sender.edit_list(state["message_id"], render)
                return
            except ListMessageMissing:
                logger.warning("daily list message was deleted — posting a fresh one")

        if not items:
            if new_day and state["message_id"] is not None:
                await sender.unpin(state["message_id"])
            await asyncio.to_thread(save_daily_list_state, conn, today, None, weather)
            return

        message_id = await sender.post_list(render)
        if state["message_id"] is not None and state["message_id"] != message_id:
            await sender.unpin(state["message_id"])
        await asyncio.to_thread(save_daily_list_state, conn, today, message_id, weather)
    except Exception:
        await asyncio.to_thread(mark_list_dirty, conn)
        raise
