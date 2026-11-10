import json
import logging
import os
import webbrowser
from datetime import datetime, timezone

import brain.window_manager as window_manager
from brain.memory import (
    clear_pending_proposal,
    get_action,
    get_pending_proposal,
    set_pending_proposal,
    upsert_action,
)

AUTOMATION_SUPPORTED = window_manager.AUTOMATION_SUPPORTED

logger = logging.getLogger(__name__)

PENDING_PROPOSAL_MAX_TURNS = 3
PENDING_PROPOSAL_MAX_AGE_SECONDS = 24 * 60 * 60


class ActionNotFoundError(Exception):
    pass


class ActionExecutionError(Exception):
    pass


class ActionSavedButNotExecutedError(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


def _open(kind: str, value: str) -> None:
    if kind in ("app", "folder"):
        if not AUTOMATION_SUPPORTED:
            raise OSError("PC automation is not available on this platform")
        os.startfile(value)
    elif kind == "url":
        window_manager.require_http_url(value)
        if not webbrowser.open(value):
            raise OSError(f"failed to open url: {value}")
    else:
        raise ValueError(f"unknown action kind: {kind}")


def run_one_off_action(kind: str, value: str) -> None:
    _open(kind, value)


def _parse_routine_steps(value: str) -> list[dict]:
    try:
        raw_steps = json.loads(value)
    except json.JSONDecodeError:
        raise ActionExecutionError("routine is malformed: not valid JSON")
    if not isinstance(raw_steps, list):
        raise ActionExecutionError("routine is malformed: expected a JSON list of step objects")

    steps = []
    for raw_step in raw_steps:
        if isinstance(raw_step, str):
            # Legacy format from before per-step placement existed: a bare
            # action name with no new_window/monitor/maximized/size.
            raw_step = {"name": raw_step}
        if not isinstance(raw_step, dict) or not isinstance(raw_step.get("name"), str):
            raise ActionExecutionError(
                "routine is malformed: each step must be a string name or an "
                "object with a 'name' string"
            )
        new_window = raw_step.get("new_window", False)
        if not isinstance(new_window, bool):
            raise ActionExecutionError("routine is malformed: 'new_window' must be a boolean")
        monitor = raw_step.get("monitor")
        if monitor is not None and not isinstance(monitor, int):
            raise ActionExecutionError("routine is malformed: 'monitor' must be an integer")
        maximized = raw_step.get("maximized", False)
        if not isinstance(maximized, bool):
            raise ActionExecutionError("routine is malformed: 'maximized' must be a boolean")
        size = raw_step.get("size")
        if size is not None and (
            not isinstance(size, list)
            or len(size) != 2
            or not all(isinstance(v, (int, float)) and v > 0 for v in size)
        ):
            raise ActionExecutionError(
                "routine is malformed: 'size' must be a [width, height] list of positive numbers"
            )
        position = raw_step.get("position")
        if position is not None and position not in window_manager.POSITIONS:
            raise ActionExecutionError(
                "routine is malformed: 'position' must be one of " + ", ".join(window_manager.POSITIONS)
            )
        steps.append(
            {
                "name": raw_step["name"],
                "new_window": new_window,
                "monitor": monitor,
                "maximized": maximized,
                "size": size,
                "position": position,
            }
        )
    return steps


def _execute_steps(conn, label: str, steps: list[dict]) -> str:
    opened = []
    for step in steps:
        step_action = get_action(conn, step["name"])
        if step_action is None:
            logger.warning("%s references missing action %s", label, step["name"])
            continue
        needs_placement = (
            step["monitor"] is not None
            or step["maximized"]
            or step["size"] is not None
            or step["position"] is not None
            or (step_action["kind"] == "url" and step["new_window"])
        )
        try:
            if needs_placement:
                window_manager.open_placed(
                    step_action["kind"],
                    step_action["value"],
                    step["new_window"],
                    step["monitor"],
                    maximized=step["maximized"],
                    size=step["size"],
                    position=step["position"],
                )
            else:
                _open(step_action["kind"], step_action["value"])
            opened.append(step["name"])
        except (OSError, ValueError) as exc:
            logger.warning("failed to open %s (%s): %s", step["name"], step_action["value"], exc)
    if not opened:
        raise ActionExecutionError(f"couldn't open anything in {label}")
    return f"opened {', '.join(opened)}"


def execute_action(conn, name: str) -> str:
    action = get_action(conn, name)
    if action is None:
        raise ActionNotFoundError(name)

    if action["kind"] == "routine":
        try:
            steps = _parse_routine_steps(action["value"])
        except ActionExecutionError:
            logger.warning("routine %s has a malformed step list: %r", name, action["value"])
            raise
        return _execute_steps(conn, f"routine '{name}'", steps)

    try:
        _open(action["kind"], action["value"])
    except (OSError, ValueError) as exc:
        logger.warning("failed to open %s (%s): %s", name, action["value"], exc)
        raise ActionExecutionError(f"couldn't open '{name}': {exc}")
    return f"opened {name}"


def run_ad_hoc_placement(conn, steps_json: str) -> str:
    steps = _parse_routine_steps(steps_json)
    missing = [s["name"] for s in steps if get_action(conn, s["name"]) is None]
    if missing:
        raise ActionNotFoundError(", ".join(missing))
    return _execute_steps(conn, "ad-hoc placement", steps)


def propose_action(
    conn, kind: str, suggested_name: str, suggested_value: str, reasoning: str
) -> None:
    set_pending_proposal(
        conn,
        kind,
        suggested_name,
        suggested_value,
        reasoning,
        datetime.now(timezone.utc).isoformat(),
        turns_remaining=PENDING_PROPOSAL_MAX_TURNS,
    )


def confirm_pending_action(
    conn, confirmed: bool, corrected_value: str | None = None
) -> str | None:
    proposal = get_pending_proposal(conn)
    if proposal is None:
        return None
    if not confirmed:
        clear_pending_proposal(conn)
        return None

    value = corrected_value or proposal["suggested_value"]
    kind = proposal["kind"]
    name = proposal["suggested_name"]

    if kind in ("app", "folder") and not AUTOMATION_SUPPORTED:
        upsert_action(conn, name, kind, value)
        clear_pending_proposal(conn)
        raise ActionSavedButNotExecutedError(name)

    if kind == "routine":
        try:
            steps = _parse_routine_steps(value)
        except ActionExecutionError:
            clear_pending_proposal(conn)
            raise
        missing = [s["name"] for s in steps if get_action(conn, s["name"]) is None]
        if missing:
            clear_pending_proposal(conn)
            raise ActionExecutionError(
                f"routine references unknown action(s): {', '.join(missing)}"
            )
    else:
        try:
            _open(kind, value)
        except (OSError, ValueError) as exc:
            clear_pending_proposal(conn)
            raise ActionExecutionError(f"couldn't open '{name}': {exc}") from exc

    clear_pending_proposal(conn)
    upsert_action(conn, name, kind, value)
    if kind == "routine":
        execute_action(conn, name)
    return name


def is_pending_proposal_expired(proposal: dict, now: datetime) -> bool:
    if proposal["turns_remaining"] <= 0:
        return True
    created_at = datetime.fromisoformat(proposal["created_at"])
    age_seconds = (now - created_at).total_seconds()
    return age_seconds > PENDING_PROPOSAL_MAX_AGE_SECONDS


RUN_KNOWN_ACTION_TOOL = {
    "name": "run_known_action",
    "description": (
        "Run a previously configured named action (an app, folder, url, "
        "or routine) that matches what the user is asking for."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The known action's exact name"}
        },
        "required": ["name"],
    },
}

PROPOSE_NEW_ACTION_TOOL = {
    "name": "propose_new_action",
    "description": (
        "Propose a brand-new action to open when the user's request doesn't "
        "match any known action yet. Does not execute anything by itself — "
        "the user must confirm first. For a multi-step 'routine' (e.g. "
        "'get my PC ready for X'), every step must already be a known "
        "action (see the 'Known actions' list in the system prompt) before "
        "you propose the routine. If one or more steps aren't known yet, "
        "propose just ONE of the missing steps first (kind app/folder/url), "
        "let the user confirm it, then repeat for the next missing step, "
        "and only once every step exists propose the routine itself."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["app", "folder", "url", "routine"]},
            "suggested_name": {"type": "string"},
            "suggested_value": {
                "type": "string",
                "description": (
                    "For app: a file system path. For folder: a file system "
                    "path. For url: a full http:// or https:// URL. For "
                    "routine: a JSON array of step objects, e.g. "
                    "[{\"name\": \"tradingview_site\", \"new_window\": true, "
                    "\"monitor\": 1}, {\"name\": \"trading_journal\", "
                    "\"new_window\": true, \"monitor\": 2, \"maximized\": "
                    "true}]. 'name' must be the EXACT name of an existing "
                    "known action — never a description, a raw URL, or a "
                    "name that isn't already known. 'new_window' (optional, "
                    "default false) opens a url step in its own window "
                    "instead of a tab. 'monitor' (optional) moves that "
                    "step's window onto the given 1-indexed monitor, "
                    "ordered the same way Windows Display Settings numbers "
                    "them (the primary monitor is always 1, regardless of "
                    "its physical position); setting 'monitor' on a url "
                    "step always implies a new window. 'maximized' "
                    "(optional, default false) maximizes the window after "
                    "placement. 'size' (optional) is a [width, height] "
                    "pair — values of 1 or less are a fraction of the "
                    "target monitor (e.g. [0.5, 1.0] for half-width, "
                    "full-height), larger values are exact pixels; 'size' "
                    "only has an effect when 'monitor' is also set. "
                    "'position' (optional) is where on that monitor the "
                    "window goes: left, right, top, bottom, top-left, "
                    "top-right, bottom-left, bottom-right or center — use "
                    "left/right for two windows side by side (half width "
                    "is the default for left/right)."
                ),
            },
            "reasoning": {"type": "string"},
        },
        "required": ["kind", "suggested_name", "suggested_value", "reasoning"],
    },
}

RUN_AD_HOC_PLACEMENT_TOOL = {
    "name": "run_ad_hoc_placement",
    "description": (
        "Immediately open one or more ALREADY-KNOWN actions with custom "
        "window placement (new_window/monitor/maximized/size), with no "
        "confirmation and without saving a new named routine. Use this "
        "whenever every action named in the request is already in the "
        "'Known actions' list and the user just wants different placement "
        "this time (e.g. 'open tradingview on my second monitor at half "
        "size', or 'open tradingview and the journal side by side' when "
        "both are already known). If ANY referenced action is not already "
        "known, do not use this tool — use propose_new_action instead so "
        "the user can confirm the new action first."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "string",
                "description": (
                    "A JSON array of step objects, same shape as a routine "
                    "step: [{\"name\": ..., \"new_window\": bool, "
                    "\"monitor\": int, \"maximized\": bool, \"size\": "
                    "[w, h], \"position\": \"left\"|\"right\"|...}, ...]. "
                    "Every 'name' must already be a known "
                    "action."
                ),
            },
        },
        "required": ["steps"],
    },
}

RUN_ONE_OFF_ACTION_TOOL = {
    "name": "run_one_off_action",
    "description": (
        "Immediately open a URL, app, or folder for a ONE-TIME request — "
        "something the user is very unlikely to ask for again, like "
        "looking something up ('google the best pizza place near me', "
        "'search for X', 'find Y') or a throwaway link mentioned in "
        "passing. Executes right away with no confirmation, and is never "
        "saved as a reusable action. Do NOT use this for anything the "
        "user would plausibly ask for again — a specific app, folder, or "
        "site they use regularly (their trading journal, a frequently- "
        "visited site, a particular program) — use propose_new_action "
        "for those instead, so they get saved for next time."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["app", "folder", "url"]},
            "value": {
                "type": "string",
                "description": (
                    "For app/folder: a file system path. For url: a full "
                    "http:// or https:// URL."
                ),
            },
        },
        "required": ["kind", "value"],
    },
}

CONFIRM_PENDING_ACTION_TOOL = {
    "name": "confirm_pending_action",
    "description": (
        "Record whether the user just confirmed, denied, or corrected the "
        "currently pending proposed action."
    ),
    "parameters_json_schema": {
        "type": "object",
        "properties": {
            "confirmed": {"type": "boolean"},
            "corrected_value": {
                "type": "string",
                "description": "Only set if the user corrected the suggested value",
            },
        },
        "required": ["confirmed"],
    },
}

AUTOMATION_TOOLS = [
    RUN_KNOWN_ACTION_TOOL,
    PROPOSE_NEW_ACTION_TOOL,
    RUN_AD_HOC_PLACEMENT_TOOL,
    RUN_ONE_OFF_ACTION_TOOL,
]
