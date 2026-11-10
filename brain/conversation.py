import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import psycopg

from brain.automation import is_pending_proposal_expired
from brain.brains import FAIL_CONFIG, FAIL_LIMIT, FAIL_UNAVAILABLE, BrainResult, TurnContext
from brain.brains.claude import ClaudeBrain
from brain.brains.gemini import GeminiBrain
from brain.brains.groq import GroqBrain
from brain.computer_state import (
    cancel_pc_task,
    clear_bypass,
    list_pc_tasks,
    recent_activity,
    set_bypass,
)
from brain.daily_list import today_items_prompt
from brain.gemini_client import GeminiConfigError, GeminiUnavailableError
from brain.memory import (
    add_message,
    get_last_brain,
    record_last_brain,
    clear_pending_proposal,
    count_user_messages,
    decrement_pending_proposal_turns,
    get_pending_proposal,
    get_recent_messages,
    list_actions,
)
from brain.lessons import (
    LessonError, add_lesson, forget_lesson, format_lesson_note, list_lessons, lessons_prompt,
    undo_last_lesson_change,
)
from brain.profile import ProfileEditError, apply_edits, format_note, get_profile, set_profile, undo_last
from brain.reminders import agenda_items
from brain.router import RouteDecision, route
from brain.tools import ToolExecutor, build_tool_list
from config import resolve_bypass_hours, resolve_timezone

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are JARVIS, a helpful, concise personal assistant. "
    "Use the owner's profile naturally when relevant.\n\n"
    "Be proactive: answer directly from your own knowledge and reasoning "
    "whenever you can — general knowledge, maths, date and time arithmetic, "
    "explanations, advice. Work things out yourself instead of asking the "
    "user for information you already have (like today's date, given "
    "below) or can reasonably infer. Only ask a clarifying question when "
    "the request is genuinely ambiguous.\n\n"
    "Reminders, birthdays, anniversaries, subscriptions and routines live in "
    "the reminder system: look them up with find_reminders (never "
    "search_memory), and never say one doesn't exist without checking "
    "find_reminders first.\n\n"
    "Use search_memory only when the user refers to something from your "
    "shared past that isn't in the conversation shown — not for questions "
    "you can answer yourself. Use the web tools when the answer depends on "
    "current or specific online information."
)

FACT_EXTRACTION_INTERVAL = 5
HISTORY_LIMIT = 20
FACT_EXTRACTION_WINDOW = 10

UNAVAILABLE_MESSAGE = "I'm having trouble reaching Gemini right now, try again in a moment."
MEMORY_UNAVAILABLE_MESSAGE = "I can't reach my memory right now, try again in a moment."
BUSY_MESSAGE = "All my brains are busy right now — try again in a minute."

_BRAIN_DISPLAY_NAMES = {"gemini": "Gemini", "sonnet": "Claude", "groq": "Groq"}


@dataclass
class Brains:
    gemini: GeminiBrain
    claude: ClaudeBrain | None
    groq: GroqBrain | None
    groq_api_key: str | None
    groq_model: str
    show_tag: bool = True
    # Computer access: a factory building a fresh ClaudeBrain (own ComputerConfig
    # and ComputerTaskState) for each turn, and which machine it works on
    # ("pc" | "server"). Both None -> Claude behaves exactly as in A1.
    computer_claude_factory: Callable[[], ClaudeBrain] | None = None
    computer_machine: str | None = None


_REMEMBER_TRIGGER_PHRASES = (
    "remember that",
    "remember this",
    "remember i",
    "remember my",
    "please remember",
    "don't forget",
    "do not forget",
    "keep in mind",
)


def _is_explicit_remember_request(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _REMEMBER_TRIGGER_PHRASES)


_NO_AUTOMATION_NOTE = (
    "You cannot control the user's PC right now because it is off — if "
    "asked to open, run, or do anything on their PC, say so plainly "
    "instead of claiming to have done it."
)


_COMMANDS = ("/bypass", "/safe", "/queue", "/cancel", "/log", "/profile", "/lessons")
_LOG_DEFAULT = 15
_LOG_MAX = 100
_LOG_INLINE_LIMIT = 1500
_PROFILE_INLINE_LIMIT = 1800
# The quote run_turn puts in front of a reply to one of Jarvis's messages. Greedy, so
# a quote that itself contains '"] ' is removed whole.
_REPLY_QUOTE = re.compile(r'^\[Replying to: ".*"\] ', re.S)
ACTIVITY_FILENAME = "activity.txt"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_time_line() -> str:
    tz = resolve_timezone()
    now = _utc_now().astimezone(tz)
    return (
        f"Current date and time for the user: {now:%A}, {now.day} "
        f"{now:%B %Y}, {now:%H:%M} ({tz.key})."
    )


CLAUDE_CONTINUITY = timedelta(minutes=10)


def _claude_answered_recently(conn) -> bool:
    """True when Claude answered the last message less than 10 minutes ago."""
    try:
        last = get_last_brain(conn)
    except psycopg.Error as exc:
        logger.warning("couldn't read which brain answered last: %s", exc)
        return False
    return last is not None and last[0] == "sonnet" and _utc_now() - last[1] < CLAUDE_CONTINUITY


def _lessons_section(conn) -> str:
    try:
        return lessons_prompt(list_lessons(conn))
    except psycopg.Error as exc:
        logger.warning("couldn't load the lessons: %s", exc)
        return ""


def _today_list_section(conn) -> str:
    tz = resolve_timezone()
    try:
        items = agenda_items(conn, _utc_now().astimezone(tz).date(), tz)
    except psycopg.Error as exc:
        logger.warning("couldn't load today's list for the prompt: %s", exc)
        return ""
    return today_items_prompt(items)


def _system_prompt_with_known_actions(conn, automation_supported: bool) -> str:
    if not automation_supported:
        return f"{SYSTEM_PROMPT}\n\n{_NO_AUTOMATION_NOTE}"
    actions = list_actions(conn)
    if not actions:
        return SYSTEM_PROMPT
    lines = "\n".join(f"- {a['name']} ({a['kind']}): {a['value']}" for a in actions)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Known actions you can run with run_known_action (use the exact name):\n"
        f"{lines}"
    )


def _humanize_tool_name(name: str) -> str:
    return name.removeprefix("mcp__jarvis__").replace("_", " ")


def _chain_for(tier: str, has_image: bool, brains: Brains) -> list:
    # Claude can't see images, so an image always goes Gemini-first —
    # regardless of tier — and Claude only sees it by falling back (with
    # the "can't see it" note added in ClaudeBrain). Groq can't see it
    # either, so it's dropped from an image chain entirely.
    if has_image:
        candidates = [brains.gemini, brains.claude]
    elif tier != "hard":
        candidates = [brains.gemini, brains.claude, brains.groq]
    else:
        candidates = [brains.claude, brains.gemini]
    return [b for b in candidates if b is not None]


def _walk_chain(chain: list, ctx: TurnContext) -> tuple[BrainResult | None, list[BrainResult]]:
    """Try each brain in order. Returns the result to answer with (either a
    clean success or a failure that already ran a side-effect tool, which must not be
    retried) plus every failure seen before it. Returns (None, failures)
    when every brain in the chain failed without running a side-effect tool."""
    failures: list[BrainResult] = []
    for brain in chain:
        result = brain.respond(ctx)
        if result.failure is None:
            return result, failures
        logger.warning(
            "brain %s failed (%s): %s", result.brain, result.failure, result.failure_detail
        )
        if result.side_effect_tools_ran:
            return result, failures
        failures.append(result)
    return None, failures


def _fallback_reason(failures: list[BrainResult]) -> str | None:
    if not failures:
        return None
    if len(failures) == 1:
        failure = failures[0]
        display = _BRAIN_DISPLAY_NAMES.get(failure.brain, failure.brain)
        if failure.failure == FAIL_LIMIT:
            if failure.failure_detail:
                return f"{display} limit, resets {failure.failure_detail}"
            return f"{display} limit"
        return f"{display} unavailable"
    names = " and ".join(
        _BRAIN_DISPLAY_NAMES.get(failure.brain, failure.brain) for failure in failures
    )
    return f"{names} unavailable"


def _tag(brain: str, failures: list[BrainResult]) -> str:
    reason = _fallback_reason(failures)
    if reason:
        return f"— {brain} ({reason})"
    return f"— {brain}"


def _format_queue(conn, tz) -> str:
    tasks = list_pc_tasks(conn)
    if not tasks:
        return "No queued PC tasks."
    lines = []
    for task in tasks:
        when = task["requested_at"].astimezone(tz).strftime("%d %b %H:%M")
        text = " ".join(task["text"].split())
        if len(text) > 120:
            text = text[:119] + "…"
        lines.append(f"#{task['id']} ({when}): {text}")
    return "Queued PC tasks:\n" + "\n".join(lines)


def _format_activity(conn, limit: int, tz) -> str:
    lines = []
    for e in recent_activity(conn, limit):
        who = f" by {e['approver']}" if e["approver"] else ""
        lines.append(
            f"{e['at'].astimezone(tz):%d %b %H:%M} [{e['machine']}] {e['tool']} "
            f"{e['verdict']}/{e['outcome']}{who}: {' '.join((e['detail'] or '').split())}"
        )
    return "\n".join(lines)


def _bare_command_fits(name: str, args: list[str]) -> bool:
    # Without a "/" or "!" prefix a word is only a command when the whole message
    # is exactly that command, so "log my workout" stays normal chat.
    if name in ("/bypass", "/safe", "/queue"):
        return not args
    if name == "/cancel":
        return len(args) == 1 and args[0].lstrip("#").isdecimal()
    if name == "/log":
        return not args or (len(args) == 1 and args[0].isdecimal())
    if name == "/profile":
        return not args or (len(args) == 1 and args[0].lower() == "undo")
    if name == "/lessons":
        return (
            not args
            or (len(args) == 1 and args[0].lower() == "undo")
            or (len(args) == 2 and args[0].lower() == "forget" and args[1].isdecimal())
        )
    return False


def _parse_command(text: str) -> tuple[str, list[str]] | None:
    """(command, args) for "/bypass", "!bypass" or a bare "bypass"; None otherwise.
    Discord treats a leading "/" as its own slash-command picker, so the slash is
    optional."""
    parts = text.strip().split()
    if not parts:
        return None
    first, args = parts[0].lower(), parts[1:]
    prefixed = first[:1] in ("/", "!")
    name = "/" + (first[1:] if prefixed else first)
    if name not in _COMMANDS:
        return None
    if not prefixed and not _bare_command_fits(name, args):
        return None
    return name, args


def is_command_word(text: str) -> bool:
    return _parse_command(text) is not None


def handle_command(conn, text: str) -> tuple[str, list[tuple[str, bytes]]] | None:
    """Chat commands for computer access. No AI call. Returns (reply, attachments)
    or None when the text isn't a command. Only owner-typed text may reach this."""
    parsed = _parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    tz = resolve_timezone()
    try:
        if name == "/bypass" and not args:
            until = _utc_now() + timedelta(hours=resolve_bypass_hours())
            set_bypass(conn, until)
            return (
                f"⚠️ Bypass on until {until.astimezone(tz):%H:%M} (local). "
                "Type safe to end it early.", [],
            )
        if name == "/safe" and not args:
            clear_bypass(conn)
            return "🛡️ Bypass off — I'll ask again before risky steps.", []
        if name == "/queue" and not args:
            return _format_queue(conn, tz), []
        if name == "/cancel":
            if len(args) != 1 or not args[0].lstrip("#").isdecimal():
                return "Usage: cancel <number> (see queue).", []
            task_id = int(args[0].lstrip("#"))
            if cancel_pc_task(conn, task_id):
                return f"Cancelled queued task #{task_id}.", []
            return f"No queued task #{task_id}.", []
        if name == "/log" and len(args) <= 1:
            limit = _LOG_DEFAULT
            if args:
                if not args[0].isdecimal() or int(args[0]) < 1:
                    return "Usage: log [number of entries].", []
                limit = min(int(args[0]), _LOG_MAX)
            body = _format_activity(conn, limit, tz)
            if not body:
                return "No activity logged yet.", []
            if len(body) <= _LOG_INLINE_LIMIT:
                return body, []
            return (
                f"The last {limit} activity entries are in the attached {ACTIVITY_FILENAME}.",
                [(ACTIVITY_FILENAME, body.encode("utf-8"))],
            )
        if name == "/profile":
            if args and not (len(args) == 1 and args[0].lower() == "undo"):
                return "Usage: profile or profile undo.", []
            if args:
                summary = undo_last(conn)
                return (f"↩️ Undid: {summary}" if summary else "Nothing to undo."), []
            doc = get_profile(conn)
            if not doc:
                return "No profile yet.", []
            if len(doc) <= _PROFILE_INLINE_LIMIT:
                return doc, []
            return "Your profile is attached.", [("profile.md", doc.encode("utf-8"))]
        if name == "/lessons":
            low = [a.lower() for a in args]
            if not args:
                texts = list_lessons(conn)
                if not texts:
                    return "No lessons yet.", []
                body = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))
                if len(body) <= _PROFILE_INLINE_LIMIT:
                    return body, []
                return "Your lessons are attached.", [("lessons.md", body.encode("utf-8"))]
            if low == ["undo"]:
                summary = undo_last_lesson_change(conn)
                return (f"↩️ Undid: {summary}" if summary else "Nothing to undo."), []
            if len(args) == 2 and low[0] == "forget" and args[1].isdecimal():
                number = int(args[1])
                try:
                    return f"🗑️ Forgot lesson {number}: {forget_lesson(conn, number)}", []
                except LessonError:
                    return f"There's no lesson {number}.", []
            return "Usage: lessons, lessons undo, or lessons forget N.", []
    except psycopg.Error as exc:
        logger.warning("chat command %s failed: %s", name, exc)
        return "I can't reach my storage right now, try again in a moment.", []
    return None


def _maybe_decrement_pending(conn, result: BrainResult, has_pending: bool) -> None:
    if not result.tools_ran and has_pending:
        decrement_pending_proposal_turns(conn)


def run_turn(
    conn,
    client,
    user_message: str,
    image_bytes: bytes | None = None,
    image_mime_type: str | None = None,
    replied_to: str | None = None,
    brains: Brains | None = None,
    *,
    attachments_out: list | None = None,
    allow_commands: bool = True,
    tier_override: str | None = None,
    outcome_out: dict | None = None,
    queued_task: bool = False,
) -> str:
    # attachments_out: side channel for files (name, bytes) the Discord layer
    # sends after the reply; the return value stays a plain str for every caller.
    # allow_commands=False for text that did not come from the owner typing in
    # chat (queued PC tasks): a queued "/bypass" must never switch bypass on.
    # tier_override skips the judge (queued PC tasks always go to Claude);
    # "claude_only" also drops the fallback chain to just [claude].
    # outcome_out receives {"ok": bool, "brain": name|None}: ok only when a brain
    # produced a clean answer (False for BUSY, a failed brain, or memory errors).
    # queued_task: the text was written by the cloud model, so history stores it
    # labelled "[queued task] ..." and fact extraction is skipped for the turn.
    if outcome_out is not None:
        outcome_out.update(ok=False, brain=None, tools=[])
    base_message = user_message
    if not base_message.strip() and image_bytes:
        base_message = "[sent an image]"
    stored_message = base_message
    if replied_to:
        stored_message = f'[Replying to: "{replied_to}"] {stored_message}'
    if queued_task:
        stored_message = f"[queued task] {stored_message}"

    try:
        history = get_recent_messages(conn, limit=HISTORY_LIMIT)
        add_message(conn, "user", stored_message)
    except psycopg.Error as exc:
        logger.warning("memory read/write failed before generating a reply: %s", exc)
        return MEMORY_UNAVAILABLE_MESSAGE
    try:
        profile = get_profile(conn)
    except psycopg.Error as exc:
        logger.warning("couldn't load the profile: %s", exc)
        profile = ""

    if allow_commands:
        handled = handle_command(conn, user_message)
        if handled is not None:
            command_reply, command_files = handled
            if attachments_out is not None:
                attachments_out.extend(command_files)
            try:
                add_message(conn, "assistant", command_reply)
            except psycopg.Error as exc:
                logger.warning("memory write failed after a chat command: %s", exc)
            return command_reply

    pending = get_pending_proposal(conn)
    has_pending = pending is not None and not is_pending_proposal_expired(
        pending, datetime.now(timezone.utc)
    )
    if pending is not None and not has_pending:
        clear_pending_proposal(conn)
        pending = None

    from brain import automation as _automation

    tool_list = build_tool_list(conn, has_pending)
    system_prompt = _system_prompt_with_known_actions(conn, _automation.AUTOMATION_SUPPORTED)
    lessons_section = _lessons_section(conn)
    if lessons_section:
        system_prompt = f"{system_prompt}\n\n{lessons_section}"
    system_prompt = f"{system_prompt}\n\n{_current_time_line()}"
    if brains is not None and brains.claude is not None and brains.computer_claude_factory:
        # A fresh brain per turn: its ComputerConfig/ComputerTaskState belong to this task.
        # On the server, Claude's own prompt tells it about queue_pc_task; the shared
        # prompt must not, because Gemini/Groq have no such tool.
        brains = replace(brains, claude=brains.computer_claude_factory())
    today_section = _today_list_section(conn)
    if today_section:
        system_prompt = f"{system_prompt}\n\n{today_section}"

    executor = ToolExecutor(conn, client, pending if has_pending else None)

    if brains is None:
        ctx = TurnContext(
            conn=conn,
            system_prompt=system_prompt,
            profile=profile,
            history=history,
            message=stored_message,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            tools=tool_list,
            executor=executor,
        )
        result = GeminiBrain(client).respond(ctx)

        if result.failure == FAIL_CONFIG:
            return (
                f"Gemini configuration problem: {result.failure_detail}. "
                "Check your GEMINI_API_KEY and model name."
            )
        if result.failure == FAIL_UNAVAILABLE:
            logger.warning(
                "Gemini unavailable while generating a reply: %s", result.failure_detail
            )
            return UNAVAILABLE_MESSAGE

        _maybe_decrement_pending(conn, result, has_pending)
        reply = result.text
        tag = None
        turn_tools = list(result.tools_ran)
        handled_as_note = result.handled_as_note
    else:
        has_image = image_bytes is not None
        if tier_override is not None:
            decision = RouteDecision(
                "hard" if tier_override == "claude_only" else tier_override,
                "override", base_message,
            )
        else:
            decision = route(
                base_message, has_image, history, has_pending, brains.groq_api_key,
                brains.groq_model, is_reply=bool(replied_to),
                continues_claude=_claude_answered_recently(conn),
            )

        brain_message = decision.cleaned_message
        if replied_to:
            brain_message = f'[Replying to: "{replied_to}"] {brain_message}'

        ctx = TurnContext(
            conn=conn,
            system_prompt=system_prompt,
            profile=profile,
            history=history,
            message=brain_message,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            tools=tool_list,
            executor=executor,
        )

        if decision.tier == "small_talk":
            answering = BrainResult(decision.small_talk_reply, "groq")
            failures: list[BrainResult] = []
        else:
            if tier_override == "claude_only":
                chain = [brains.claude] if brains.claude is not None else []
            else:
                chain = _chain_for(decision.tier, has_image, brains)
            answering, failures = _walk_chain(chain, ctx)
            if answering is None:
                return BUSY_MESSAGE

        if answering.failure is not None:
            # A tool already ran on this (failed) brain — the action isn't
            # repeatable, so no further fallback; tell the user what
            # completed instead of silently retrying on another brain.
            tool_names = ", ".join(_humanize_tool_name(t) for t in answering.side_effect_tools_ran)
            reply = (
                f"Done: {tool_names} — but I lost my train of thought partway. "
                "Ask me again for the rest."
            )
            tag = _tag(answering.brain, [])
            if outcome_out is not None:
                outcome_out["tools"] = list(answering.tools_ran)
        else:
            _maybe_decrement_pending(conn, answering, has_pending)
            reply = answering.text
            tag = _tag(answering.brain, failures)
            try:
                record_last_brain(conn, answering.brain, _utc_now())
            except psycopg.Error as exc:
                logger.warning("couldn't record which brain answered: %s", exc)
            if outcome_out is not None:
                outcome_out.update(
                    ok=True, brain=answering.brain, tools=list(answering.tools_ran)
                )
            if attachments_out is not None:
                attachments_out.extend(answering.attachments)

        if not brains.show_tag:
            tag = None
        turn_tools = list(answering.tools_ran)
        handled_as_note = answering.handled_as_note

    notes = list(executor.profile_notes)
    lesson_notes = list(executor.lesson_notes)
    try:
        # The memory review is the safety net for lasting things the brain didn't
        # save itself. Skipped when this turn already changed the profile, learned a
        # lesson or saved a note (a redundant Gemini call risking a duplicate).
        should_review = (
            not handled_as_note and not queued_task
            and "update_profile" not in turn_tools and "learn_lesson" not in turn_tools
            and (
                count_user_messages(conn) % FACT_EXTRACTION_INTERVAL == 0
                or _is_explicit_remember_request(user_message)
            )
        )
        if should_review:
            reviewed_profile, reviewed_lessons = _run_memory_review(conn, client)
            notes += reviewed_profile
            lesson_notes += reviewed_lessons
    except psycopg.Error as exc:
        logger.warning("memory review failed: %s", exc)
    for line in (format_note(notes), format_lesson_note(lesson_notes)):
        if line:
            reply = f"{reply}\n{line}"

    try:
        add_message(conn, "assistant", reply)
    except psycopg.Error as exc:
        logger.warning("memory write failed after generating a reply: %s", exc)

    return f"{reply}\n{tag}" if tag else reply


def _run_memory_review(conn, client) -> tuple[list[str], list[str]]:
    """Background safety net for the profile and the lessons. Returns (profile summaries,
    lessons learned) for the notes."""
    # Only the owner's own words: Jarvis's replies can summarise web pages and
    # queued-task text is written by the cloud model, so neither may shape memory.
    # A reply quotes Jarvis's message ([Replying to: "…"]); that quote is not the owner's words.
    recent = [
        {**m, "content": _REPLY_QUOTE.sub("", m["content"], count=1)}
        for m in get_recent_messages(conn, limit=FACT_EXTRACTION_WINDOW)
        if m["role"] == "user" and not m["content"].startswith("[queued task]")
    ]
    if not recent:
        return [], []
    try:
        review = client.review_memory(get_profile(conn), list_lessons(conn), recent)
    except (GeminiUnavailableError, GeminiConfigError) as exc:
        logger.warning("memory review skipped: %s", exc)
        return [], []
    if not review:
        return [], []
    summaries: list[str] = []
    if review.get("profile"):
        try:
            new, summaries, skipped = apply_edits(get_profile(conn), review["profile"])
        except ProfileEditError as exc:
            logger.warning("profile review edits rejected: %s", exc)
            summaries, skipped = [], []
        if skipped:
            logger.info("profile review edits skipped: %s", skipped)
        if summaries:
            set_profile(conn, new, "; ".join(summaries), "review")
    learned: list[str] = []
    for rule in review.get("lessons") or []:
        try:
            added = add_lesson(conn, rule, "review")
        except LessonError as exc:
            logger.warning("review lesson not saved: %s", exc)
            break
        if added:
            learned.append(added)
    return summaries, learned
