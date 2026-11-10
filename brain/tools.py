import logging
from dataclasses import dataclass

import psycopg

from brain.automation import (
    AUTOMATION_TOOLS,
    CONFIRM_PENDING_ACTION_TOOL,
    ActionExecutionError,
    ActionNotFoundError,
    ActionSavedButNotExecutedError,
    confirm_pending_action,
    execute_action,
    propose_action,
    run_ad_hoc_placement,
    run_one_off_action,
)
from brain.daily_list import agenda_text
from brain.notes import CREATE_NOTE_TOOL, SEARCH_MEMORY_TOOL, NoteCreationError, create_note, search_notes
from brain.lessons import LEARN_LESSON_TOOL, LessonError, add_lesson, replace_lesson
from brain.profile import UPDATE_PROFILE_TOOL, ProfileEditError, apply_edits, get_profile, set_profile
from brain.reminders import (
    REMINDER_TOOLS,
    AmbiguousItemError,
    ItemNotFoundError,
    add_todo,
    agenda_items,
    create_reminder,
    edit_reminder,
    find_reminders,
    find_reminders_text,
    finish_item,
)
from brain.schedule import ScheduleInputError, parse_day
from brain.web_tools import (
    GET_GITHUB_REPO_TOOL,
    GET_WEATHER_TOOL,
    GET_YOUTUBE_INFO_TOOL,
    READ_RSS_FEED_TOOL,
    READ_WEBPAGE_TOOL,
    WEB_SEARCH_TOOL,
    get_github_repo,
    get_weather,
    get_youtube_info,
    read_rss_feed,
    read_webpage,
    web_search,
)
from config import resolve_exa_api_key, resolve_timezone

logger = logging.getLogger(__name__)

REMINDER_STORAGE_FAILED = "I couldn't save that right now — try again in a moment."
_UNKNOWN_TOOL_MESSAGE = "I don't know how to do that yet."
_GENERIC_TOOL_ERROR = "I ran into a problem doing that — try again in a moment."

_REMINDER_TOOL_NAMES = frozenset(
    {"create_reminder", "add_todo", "list_agenda", "finish_item", "edit_reminder",
     "find_reminders"}
)


@dataclass
class ToolOutcome:
    text: str                     # action/fixed: the reply text; information: the raw result
    kind: str                     # "action" | "fixed" | "information"
    fallback: str | None = None   # information only: reply if the phrasing step fails
    handled_as_note: bool = False  # create_note succeeded


def _format_day(day) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def _fold(model_text: str | None, result: str) -> str:
    return f"{model_text}\n\n{result}" if model_text else result


def build_tool_list(conn, has_pending: bool) -> list[dict]:
    from brain import automation as _automation

    tools = list(AUTOMATION_TOOLS) if _automation.AUTOMATION_SUPPORTED else []
    tools = tools + [
        SEARCH_MEMORY_TOOL, CREATE_NOTE_TOOL, UPDATE_PROFILE_TOOL, LEARN_LESSON_TOOL,
        READ_WEBPAGE_TOOL, GET_YOUTUBE_INFO_TOOL, GET_GITHUB_REPO_TOOL,
        READ_RSS_FEED_TOOL, GET_WEATHER_TOOL,
    ]
    if resolve_exa_api_key():
        tools = tools + [WEB_SEARCH_TOOL]
    tools = tools + REMINDER_TOOLS
    if has_pending:
        tools = tools + [CONFIRM_PENDING_ACTION_TOOL]
    return tools


class ToolExecutor:
    def __init__(self, conn, gemini_client, pending: dict | None):
        self.conn = conn
        self.client = gemini_client
        self.pending = pending
        self.profile_notes: list[str] = []  # summaries of profile changes made this turn
        self.lesson_notes: list[str] = []   # lessons learned this turn

    def execute(self, name: str, args: dict, model_text: str | None = None) -> ToolOutcome:
        args = args or {}
        try:
            return self._execute(name, args, model_text)
        except Exception:
            logger.exception("tool %s raised unexpectedly", name)
            return ToolOutcome(_GENERIC_TOOL_ERROR, "fixed")

    def _execute(self, name: str, args: dict, model_text: str | None) -> ToolOutcome:
        if self.pending is not None and name == "confirm_pending_action":
            return self._confirm_pending_action(args)

        if name == "run_known_action":
            return self._run_known_action(args)

        if name == "run_ad_hoc_placement":
            return self._run_ad_hoc_placement(args)

        if name == "run_one_off_action":
            return self._run_one_off_action(args)

        if name == "search_memory":
            return self._search_memory(args, model_text)

        if name == "create_note":
            return self._create_note(args)

        if name == "update_profile":
            return self._update_profile(args)

        if name == "learn_lesson":
            return self._learn_lesson(args)

        if name == "read_webpage":
            return self._info_tool(args, "url", "I wasn't sure which page to read — can you share the link?", lambda url: read_webpage(url), model_text)

        if name == "get_youtube_info":
            return self._info_tool(args, "url", "I wasn't sure which video you meant — can you share the link?", lambda url: get_youtube_info(url), model_text)

        if name == "get_github_repo":
            return self._info_tool(args, "owner_repo", "I wasn't sure which repo you meant — can you give me owner/repo?", lambda owner_repo: get_github_repo(owner_repo), model_text)

        if name == "read_rss_feed":
            return self._info_tool(args, "url", "I wasn't sure which feed you meant — can you share the link?", lambda url: read_rss_feed(url), model_text)

        if name == "get_weather":
            result = get_weather(self.conn, args.get("location"))
            return ToolOutcome(result, "information", fallback=_fold(model_text, result))

        if name == "web_search":
            return self._info_tool(args, "query", "I wasn't sure what to search for — can you rephrase?", lambda query: web_search(query), model_text)

        if name in _REMINDER_TOOL_NAMES:
            return self._reminder_tool(name, args)

        if name == "propose_new_action":
            return self._propose_new_action(args)

        return ToolOutcome(_UNKNOWN_TOOL_MESSAGE, "fixed")

    # --- automation -----------------------------------------------------

    def _confirm_pending_action(self, args: dict) -> ToolOutcome:
        pending = self.pending
        confirmed = args.get("confirmed", False)
        corrected_value = args.get("corrected_value")

        if not confirmed and corrected_value:
            propose_action(
                self.conn, pending["kind"], pending["suggested_name"], corrected_value,
                pending.get("reasoning", ""),
            )
            return ToolOutcome(
                f"Got it — did you mean {corrected_value}? Should I go ahead?", "action"
            )

        try:
            executed_name = confirm_pending_action(self.conn, confirmed, corrected_value)
        except ActionSavedButNotExecutedError as exc:
            return ToolOutcome(
                f"Saved '{exc.name}' — your PC is off right now, so I'll open it "
                "once it's back on.",
                "fixed",
            )
        except ActionExecutionError as exc:
            return ToolOutcome(
                f"I tried to save and open that but ran into a problem: {exc}", "fixed"
            )

        if executed_name:
            return ToolOutcome(f"Done — saved and opened '{executed_name}'.", "action")
        return ToolOutcome("Okay, never mind.", "action")

    def _run_known_action(self, args: dict) -> ToolOutcome:
        action_name = args.get("name", "")
        if not action_name:
            return ToolOutcome(
                "I wasn't sure exactly what to open — can you be more specific?", "fixed"
            )
        try:
            summary = execute_action(self.conn, action_name)
            return ToolOutcome(f"Done — {summary}.", "action")
        except ActionNotFoundError:
            return ToolOutcome(
                f"I don't have an action called '{action_name}' set up yet.", "fixed"
            )
        except ActionExecutionError as exc:
            return ToolOutcome(f"I tried to do that but ran into a problem: {exc}", "fixed")

    def _run_ad_hoc_placement(self, args: dict) -> ToolOutcome:
        steps_json = args.get("steps", "")
        if not steps_json:
            return ToolOutcome(
                "I wasn't sure exactly what to open — can you be more specific?", "fixed"
            )
        try:
            summary = run_ad_hoc_placement(self.conn, steps_json)
            return ToolOutcome(f"Done — {summary}.", "action")
        except ActionNotFoundError as exc:
            return ToolOutcome(f"I don't have an action called '{exc}' set up yet.", "fixed")
        except ActionExecutionError as exc:
            return ToolOutcome(f"I tried to do that but ran into a problem: {exc}", "fixed")

    def _run_one_off_action(self, args: dict) -> ToolOutcome:
        kind = args.get("kind")
        value = args.get("value")
        if not (kind and value):
            return ToolOutcome(
                "I wasn't sure exactly what to open — can you be more specific?", "fixed"
            )
        try:
            run_one_off_action(kind, value)
            return ToolOutcome("Done.", "action")
        except (OSError, ValueError) as exc:
            return ToolOutcome(f"I tried to open that but ran into a problem: {exc}", "fixed")

    def _propose_new_action(self, args: dict) -> ToolOutcome:
        kind = args.get("kind")
        suggested_name = args.get("suggested_name")
        suggested_value = args.get("suggested_value")
        if not (kind and suggested_name and suggested_value):
            logger.warning("propose_new_action missing required fields: %r", args)
            return ToolOutcome(
                "I wasn't sure exactly what you meant — can you tell me again?", "fixed"
            )
        propose_action(self.conn, kind, suggested_name, suggested_value, args.get("reasoning", ""))
        return ToolOutcome(
            f"I think you mean the {kind} '{suggested_name}': "
            f"{suggested_value}. Should I go ahead?",
            "action",
        )

    # --- memory -----------------------------------------------------------

    def _search_memory(self, args: dict, model_text: str | None) -> ToolOutcome:
        query = args.get("query", "")
        if not query:
            return ToolOutcome("I wasn't sure what to look for — can you rephrase?", "fixed")
        results = search_notes(self.conn, self.client, query)
        if not results:
            return ToolOutcome(
                "(no saved notes matched)", "information",
                fallback=model_text or "I don't have anything relevant saved about that.",
            )
        lines = "\n".join(f"- {r['title']}: {r['content']}" for r in results)
        fallback = _fold(model_text, lines) if model_text else f"Here's what I remember:\n{lines}"
        return ToolOutcome(lines, "information", fallback=fallback)

    def _update_profile(self, args: dict) -> ToolOutcome:
        try:
            current = get_profile(self.conn)
            new, summaries, skipped = apply_edits(current, args.get("edits"))
        except ProfileEditError as exc:
            return ToolOutcome(f"I couldn't update your profile: {exc}.", "fixed")
        if not summaries:
            reason = "; ".join(skipped) or "no changes given"
            return ToolOutcome(f"I couldn't update your profile ({reason}).", "fixed")
        set_profile(self.conn, new, "; ".join(summaries), "tool")
        self.profile_notes.extend(summaries)
        if skipped:
            logger.info("profile edits skipped: %s", skipped)
        return ToolOutcome("Got it, sir - noted.", "action")

    def _learn_lesson(self, args: dict) -> ToolOutcome:
        text, replaces = args.get("text"), args.get("replaces")
        try:
            if replaces is not None:
                old, new = replace_lesson(self.conn, replaces, text)
                # A rewrite must be visible: show which lesson changed and what it said.
                self.lesson_notes.append(f"{replaces} changed: {new} (was: {old})")
            else:
                added = add_lesson(self.conn, text, "tool")
                if added:
                    self.lesson_notes.append(added)
        except LessonError as exc:
            return ToolOutcome(f"I couldn't save the lesson: {exc}.", "fixed")
        return ToolOutcome("Understood, sir - I'll remember that.", "action")

    def _create_note(self, args: dict) -> ToolOutcome:
        title = args.get("title")
        content = args.get("content")
        if not (title and content):
            return ToolOutcome(
                "I wasn't sure exactly what to remember — can you tell me again?", "fixed"
            )
        tags = args.get("tags", "")
        importance = args.get("importance", 5)
        if not isinstance(importance, int) or not (1 <= importance <= 10):
            importance = 5
        try:
            create_note(self.conn, self.client, title, content, tags, importance, source="explicit")
        except NoteCreationError:
            return ToolOutcome("I couldn't save that right now — try again in a moment.", "fixed")
        return ToolOutcome(
            f'Noted — I\'ll remember "{title}."', "action", handled_as_note=True
        )

    # --- web/info -----------------------------------------------------------

    def _info_tool(self, args, arg_name, missing_message, call, model_text):
        value = args.get(arg_name, "")
        if not value:
            return ToolOutcome(missing_message, "fixed")
        result = call(value)
        return ToolOutcome(result, "information", fallback=_fold(model_text, result))

    # --- reminders -----------------------------------------------------------

    def _reminder_tool(self, name: str, args: dict) -> ToolOutcome:
        from brain import conversation as _conversation

        tz = resolve_timezone()
        now = _conversation._utc_now()
        today = now.astimezone(tz).date()
        try:
            if name == "create_reminder":
                return self._create_reminder(args, now, tz)
            if name == "add_todo":
                return self._add_todo(args, today)
            if name == "list_agenda":
                day = parse_day(args.get("date"), today)
                text = agenda_text(agenda_items(self.conn, day, tz), day, with_ids=True)
                return ToolOutcome(text, "information", fallback=text)
            if name == "edit_reminder":
                return self._edit_reminder(args, now, tz)
            if name == "find_reminders":
                query, category = args.get("query"), args.get("category")
                text = find_reminders_text(
                    find_reminders(self.conn, now, tz, query=query, category=category),
                    query, category,
                )
                return ToolOutcome(text, "information", fallback=text)
            action = args.get("action", "")
            finished = finish_item(self.conn, args.get("item_id", ""), action, today, now)
            if action == "done":
                return ToolOutcome(f"✅ Done: {finished}", "fixed")
            return ToolOutcome(f"🗑️ Cancelled: {finished}", "fixed")
        except AmbiguousItemError as exc:
            matches = ", ".join(f"r{mid} {mtext}" for mid, mtext in exc.matches[:5])
            return ToolOutcome(f"Which one do you mean? {matches}", "fixed")
        except ScheduleInputError as exc:
            return ToolOutcome(f"{exc} Could you tell me again?", "fixed")
        except ItemNotFoundError:
            return ToolOutcome(
                "I couldn't find that item on your list — ask me what's on it?", "fixed"
            )
        except psycopg.Error as exc:
            logger.warning("reminder tool %s failed: %s", name, exc)
            return ToolOutcome(REMINDER_STORAGE_FAILED, "fixed")

    def _create_reminder(self, args: dict, now, tz) -> ToolOutcome:
        created = create_reminder(
            self.conn, args.get("text"), args.get("when"), args.get("repeat"), now, tz,
            category=args.get("category"),
            remind_days_before=args.get("remind_days_before"),
            timed=not args.get("no_time", False),
        )
        repeats = " (repeats)" if created["rrule"] else ""
        if not created["timed"]:
            day = created["next_on_list"].astimezone(tz)
            return ToolOutcome(
                f"🗓️ On your list from {_format_day(day)}{repeats}, no ping: "
                f"{created['text']}",
                "fixed",
            )
        when = created["next_fire_at"].astimezone(tz)
        reply = (
            f"⏰ Reminder set — {_format_day(when)}, {when:%H:%M}: "
            f"{created['text']}{repeats}"
        )
        early = created.get("early")
        if early:
            early_when = early["next_fire_at"].astimezone(tz)
            reply += f" (+ early reminder {_format_day(early_when)}, {early_when:%H:%M})"
        elif created.get("lead_days"):
            reply += " (too late for an early reminder)"
        return ToolOutcome(reply, "fixed")

    def _add_todo(self, args: dict, today) -> ToolOutcome:
        todo = add_todo(
            self.conn, args.get("text"), args.get("due_date"), today,
            category=args.get("category"),
        )
        if todo["due_date"] == today:
            return ToolOutcome(f"📝 Added to today's list: {todo['text']}", "fixed")
        return ToolOutcome(
            f"📝 Added to your list for {_format_day(todo['due_date'])}: {todo['text']}", "fixed"
        )

    def _edit_reminder(self, args: dict, now, tz) -> ToolOutcome:
        edited = edit_reminder(
            self.conn, now, tz,
            item_id=args.get("item_id"), name=args.get("name"), text=args.get("text"),
            when=args.get("when"), time=args.get("time"), repeat=args.get("repeat"),
            no_time=args.get("no_time"), category=args.get("category"),
        )
        repeats = " (repeats)" if edited["rrule"] else ""
        if not edited["timed"]:
            day = edited["next_on_list"].astimezone(tz)
            return ToolOutcome(
                f"✏️ Updated — {edited['text']}: on your list from "
                f"{_format_day(day)}{repeats}, no ping",
                "fixed",
            )
        when = edited["next_fire_at"].astimezone(tz)
        return ToolOutcome(
            f"✏️ Updated — {edited['text']}: next {_format_day(when)}, "
            f"{when:%H:%M}{repeats}",
            "fixed",
        )
