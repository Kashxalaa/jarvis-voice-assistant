import asyncio
import difflib
import json
import logging
import os
import re
import site
import sys
import time
import uuid
from importlib import metadata as importlib_metadata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from brain.approvals import APPROVAL_TIMEOUT_SECONDS, ApprovalBroker, ApprovalRequest
from brain.brains import FAIL_CONFIG, FAIL_LIMIT, FAIL_UNAVAILABLE, BrainResult, TurnContext
from brain.computer_state import get_bypass_until, log_activity
from brain import computer_tools
from brain.computer_tools import (
    ComputerTaskState,
    build_computer_tools,
    resolve_tool_path,
)
from brain.guard import GuardContext, Verdict, evaluate, normalize_path

logger = logging.getLogger(__name__)

# Claude Code's own built-in tools (file/command access). A2 turns a subset
# of these on with per-action approval; A1 keeps all of them off so Claude
# Sonnet can only act through Jarvis's own tools. A denylist alone is a
# moving target (new built-ins — PowerShell, Agent, Monitor, Skill,
# ToolSearch, Cron*, EnterWorktree, ... — would silently slip through), so
# this is now only the belt-and-braces secondary layer: options.tools=[]
# (see _respond_async) is the authoritative "disable every built-in" switch.
BUILTIN_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch",
    "NotebookEdit", "Task", "TodoWrite",
]

# Environment variables the Claude Code subprocess must never see, so it
# can't leak or act on Jarvis's own secrets even though it inherits the
# parent process's environment by default (ClaudeAgentOptions.env is merged
# *over* the inherited env, not used in place of it).
SCRUBBED_ENV_VARS = [
    "DATABASE_URL", "TEST_DATABASE_URL", "NOTES_TEST_DATABASE_URL",
    "DISCORD_BOT_TOKEN", "GEMINI_API_KEY", "GROQ_API_KEY", "EXA_API_KEY",
]

_MCP_SERVER_NAME = "jarvis"
_TOOL_PREFIX = f"mcp__{_MCP_SERVER_NAME}__"

# Jarvis tools that act on the user's PC, or that can commit to such an
# action on the user's behalf (confirm_pending_action saves AND opens the
# pending action; its corrected_value replaces the value being confirmed;
# propose_new_action seeds a pending action for the next confirm). Refused
# when an information tool (read_webpage, web_search, search_memory,
# get_weather, ...) already ran earlier in the same Claude turn, so content
# Claude just read can't itself trigger or confirm a PC action via prompt
# injection (see _make_sdk_tool).
_PC_ACTION_TOOLS = frozenset({
    "run_one_off_action", "run_known_action", "run_ad_hoc_placement",
    "confirm_pending_action", "propose_new_action",
})
_INFO_THEN_ACTION_REFUSAL = (
    "For safety I don't open or run things in the same answer where I've "
    "read web or stored content. Ask me directly and I'll do it."
)

_EXTRA_SYSTEM_PROMPT = (
    "\n\nYou are answering as Jarvis on Discord. Use the Jarvis tools when "
    "the user asks for an action. Keep answers concise."
)

_IMAGE_UNSEEN_NOTE = "\n\n[The user sent an image you can't see.]"

# Matches the Claude Code CLI's usage-limit messages, e.g.
# "You've hit your Sonnet limit · resets 3:45pm" / "... resets 15:45".
_LIMIT_RE = re.compile(r"hit your .*limit", re.IGNORECASE)
_RESET_RE = re.compile(
    r"resets?\s*(?:at\s+)?([0-9]{1,2}(?::[0-9]{2})?\s?(?:am|pm)?)", re.IGNORECASE
)


def parse_limit(text: str) -> tuple[bool, str]:
    """Detect a Claude usage-limit message and pull out its reset label."""
    if not text or not _LIMIT_RE.search(text):
        return (False, "")
    match = _RESET_RE.search(text)
    return (True, match.group(1).strip() if match else "")


def _format_reset(resets_at: int | None) -> str | None:
    if not resets_at:
        return None
    try:
        return datetime.fromtimestamp(resets_at).astimezone().strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _scratch_dir() -> Path:
    path = Path.home() / ".jarvis" / "claude"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_env(oauth_token: str, computer: bool = False, os_name: str | None = None) -> dict[str, str]:
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
        # Belt and braces against every other way the CLI can pick up
        # credentials or point at a different backend: an API key in the
        # environment, a raw auth token, Bedrock/Vertex, or a custom base
        # URL — only the Pro subscription OAuth token above must work.
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_AUTH_TOKEN": "",
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "CLAUDE_CODE_USE_VERTEX": "0",
        "CLAUDE_CODE_USE_FOUNDRY": "0",
        "ANTHROPIC_CUSTOM_HEADERS": "",
        "ANTHROPIC_UNIX_SOCKET": "",
        # HTTPS_PROXY is deliberately left untouched (inherited as-is) — the
        # user's own network proxy, if any, isn't a Claude auth/backend
        # override the way the vars above are.
    }
    if os.environ.get("ANTHROPIC_BASE_URL"):
        env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    for var in SCRUBBED_ENV_VARS:
        env[var] = ""
    if computer:
        # A repo's own git config can run code (core.fsmonitor, diff.external)
        # even on read-only git commands; blank both for the whole subprocess.
        env.update({
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "diff.external", "GIT_CONFIG_VALUE_1": "",
            # The shell keeps its working directory across commands; make the CLI
            # reset it to the project dir so path checks stay anchored (the hook
            # also reads the cwd the CLI reports and denies a move outside home).
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
        })
        if os_name == "windows":
            # The PowerShell tool (name "PowerShell") is opt-in on Windows.
            env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] = "1"
    return env


async def _deny_non_jarvis_tools_hook(input_data: dict, tool_use_id, context) -> dict:
    """PreToolUse hook: third layer of defense (after allowed_tools/
    disallowed_tools/tools=[]) — refuse anything that isn't one of Jarvis's
    own MCP tools, even if something slips past the other two."""
    tool_name = input_data.get("tool_name", "")
    if tool_name.startswith(_TOOL_PREFIX):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{tool_name} is not available to Jarvis's Claude brain."
            ),
        }
    }


class _TurnState:
    """Mutable, shared across every SDK tool call made during one
    ClaudeBrain.respond() turn — created fresh per call so it never leaks
    between turns or concurrent calls."""

    def __init__(self) -> None:
        self.tools_ran: list[str] = []
        # Subset of tools_ran that were not pure information lookups. Recorded
        # optimistically before the executor runs (a cancelled/timed-out call
        # may already have acted) and removed again once the outcome proves
        # to be information.
        self.side_effect_tools_ran: list[str] = []
        self.handled_as_note: bool = False
        self.info_tool_ran: bool = False


def _build_sdk_tools(ctx: TurnContext, state: _TurnState) -> list[SdkMcpTool]:
    return [_make_sdk_tool(decl, ctx.executor, state) for decl in ctx.tools]


def _make_sdk_tool(decl: dict, executor, state: _TurnState) -> SdkMcpTool:
    name = decl["name"]
    description = decl.get("description", "")
    schema = decl.get("parameters_json_schema") or {"type": "object", "properties": {}}

    @tool(name, description, schema)
    async def _handler(args: dict) -> dict:
        if name in _PC_ACTION_TOOLS and state.info_tool_ran:
            return {"content": [{"type": "text", "text": _INFO_THEN_ACTION_REFUSAL}]}

        # Recorded before the executor runs (not after) so a timeout that
        # cancels mid-execution still reports the action as having been
        # attempted — the caller must not retry/duplicate it on fallback.
        state.tools_ran.append(name)
        state.side_effect_tools_ran.append(name)
        outcome = await asyncio.to_thread(executor.execute, name, args)
        if outcome.kind == "information":
            state.info_tool_ran = True
            state.side_effect_tools_ran.remove(name)
        if outcome.handled_as_note:
            state.handled_as_note = True
        return {"content": [{"type": "text", "text": outcome.text}]}

    return _handler


# --------------------------------------------------------------- computer mode

_COMPUTER_PROMPT = (
    "\n\nFor file work use make_folder, move_paths, copy_paths, move_to_trash, "
    "install_software and open_path rather than shell commands — shell commands "
    "other than simple read-only ones always need the owner's approval. Never "
    "trust the chat history about what exists on the computer (earlier replies "
    "can be wrong): check with your tools before saying something is already "
    "done, and do it again if it isn't there. You CAN put windows on specific "
    "screens: open_on_screen opens web pages (all as tabs in one new browser "
    "window) or a folder/file on a monitor; move_window moves a window that is "
    "already open; list_windows shows windows and monitors. Monitor 1 is the "
    "primary, so 'first screen' means monitor 1 and 'second screen' monitor 2."
)
_SERVER_PROMPT = (
    "\n\nYou are working on Jarvis's cloud server because the owner's PC is off: every "
    "shell command needs the owner's approval. Only for work that must happen ON the PC "
    "(its files, programs, windows or commands) call queue_pc_task, then tell the owner "
    "exactly: \"Your PC is off — I'll do this when it's back on.\" Reminders, the to-do "
    "list, notes, the profile, lessons and web lookups never need the PC — just do them."
)

_WEB_TAINT_TOOLS = frozenset({
    "read_webpage", "web_search", "read_rss_feed", "get_youtube_info", "get_github_repo",
})
_UNTRUSTED_DIR = re.compile(r"/(downloads|temp|tmp)/", re.I)
# Text that mentions an untrusted place (shell commands, search patterns).
_UNTRUSTED_TEXT = re.compile(r"downloads|(?<![A-Za-z0-9_])(?:temp|tmp)(?![A-Za-z0-9_])", re.I)
_OMITTED = "(secret file, contents omitted)"
_WINDOWS_GITBASH_DRIVE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
_PATH_TOOLS_ONE = {"Read", "Write", "Edit", "MultiEdit"}            # args["file_path"]
_CUSTOM_ONE_PATH = {"make_folder", "open_path", "send_file", "open_in_editor", "open_on_screen"}
_SHELL_BY_OS = {"windows": "PowerShell", "linux": "Bash"}
_NO_SIDE_EFFECT = {"Read", "Glob", "Grep"}
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_TEXT_DIFF_LIMIT = 2 * 1024 * 1024
_PROGRESS_INTERVAL_S = 3.0
_A1_MAX_STEPS = 8  # normal chat keeps A1's step budget until a computer tool is used
_FALLBACK_PROTECTED = frozenset({
    "google-genai", "python-dotenv", "discord.py", "pywin32", "psycopg", "pgvector", "requests",
    "yt-dlp", "feedparser", "tzdata", "python-dateutil", "groq", "claude-agent-sdk", "send2trash",
})
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_protected_cache: dict[str, frozenset] = {}


def shell_tool_name(os_name: str) -> str:
    """The shell tool the guard understands for this OS."""
    return _SHELL_BY_OS.get(os_name, "Bash")


def _installed_distributions() -> frozenset:
    """Normalized names of EVERY distribution installed in Jarvis's environment."""
    names = set()
    try:
        for dist in importlib_metadata.distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            if name:
                names.add(re.sub(r"[-_.]+", "-", name).lower())
    except Exception:  # noqa: BLE001
        logger.warning("could not list installed distributions for the guard", exc_info=True)
    return frozenset(names)


def load_protected_packages(jarvis_dir: str) -> frozenset:
    """Lowercase pip names Jarvis depends on: its requirements.txt plus every installed
    distribution (parsed once per folder). Unreadable requirements -> a built-in list."""
    key = os.path.normcase(os.path.abspath(jarvis_dir))
    cached = _protected_cache.get(key)
    if cached is not None:
        return cached
    try:
        names = set()
        with open(os.path.join(jarvis_dir, "requirements.txt"), encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                m = _REQ_NAME.match(line)
                if m:
                    names.add(m.group(1).lower())
        result = frozenset(names) or _FALLBACK_PROTECTED
    except OSError as exc:
        logger.warning("could not read requirements.txt for the guard: %s", exc)
        result = _FALLBACK_PROTECTED
    result = result | _installed_distributions()
    _protected_cache[key] = result
    return result


_dirs_cache: dict = {}


def load_protected_dirs(cli_path: str | None) -> tuple:
    """Jarvis's runtime folders: its Python (prefixes, user site and its parent, every sys.path
    directory) and the Claude CLI (its folder and the npm package root). Computed once."""
    cached = _dirs_cache.get(cli_path)
    if cached is not None:
        return cached
    dirs: list[str] = []

    def add(d):
        if isinstance(d, str) and d.strip() and d not in dirs:
            dirs.append(d)

    add(sys.prefix)
    add(sys.base_prefix)
    try:
        user_site = site.getusersitepackages()
        add(user_site)
        add(os.path.dirname(user_site))
    except Exception:  # noqa: BLE001
        pass
    for entry in sys.path:
        try:
            if entry and os.path.isdir(entry):
                add(os.path.abspath(entry))
        except Exception:  # noqa: BLE001
            pass
    cli_dirs = []
    if cli_path:
        cli_dirs.append(os.path.dirname(os.path.abspath(cli_path)))
    appdata = os.environ.get("APPDATA")
    if appdata:
        cli_dirs.append(os.path.join(appdata, "npm", "node_modules", "@anthropic-ai", "claude-code"))
    for d in cli_dirs:
        add(d)
        parts = os.path.normpath(d).split(os.sep)
        for i in range(len(parts) - 1):
            if parts[i].lower() == "@anthropic-ai" and parts[i + 1].lower() == "claude-code":
                add(os.sep.join(parts[: i + 2]))
    result = tuple(dirs)
    _dirs_cache[cli_path] = result
    return result


@dataclass
class ComputerConfig:
    machine: str                      # "pc" | "server"
    os_name: str                      # "windows" | "linux"
    home: str                         # owner's home; also the agent's cwd
    jarvis_dir: str
    broker: ApprovalBroker
    conn_factory: Callable[[], object]          # DB connection for bypass/log
    progress: Callable[[str], Awaitable[None]] | None = None
    send_file: Callable[[str], Awaitable[str]] | None = None
    max_steps: int = 60
    timeout_s: float = 1800
    # Approvals/progress/send_file must run on the Discord loop, but run_turn
    # runs in a worker thread and ClaudeBrain.respond makes its own loop.
    discord_loop: asyncio.AbstractEventLoop | None = None
    queue_task: Callable[[str], Awaitable[str]] | None = None
    state: ComputerTaskState | None = None      # supplied by the caller so the Stop button can reach it
    approval_timeout_s: float = APPROVAL_TIMEOUT_SECONDS


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _response_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [v for k, v in resp.items() if k in ("stdout", "stderr", "output", "text") and isinstance(v, str)]
        if parts:
            return "\n".join(p for p in parts if p)
        if isinstance(resp.get("content"), (list, str)):
            return _response_text(resp["content"])
    if isinstance(resp, list):
        return "\n".join(
            (b.get("text", "") if isinstance(b, dict) else _response_text(b)) for b in resp
        )
    try:
        return json.dumps(resp, default=str)
    except Exception:  # noqa: BLE001
        return str(resp)


def _read_text(path: str) -> str | None:
    """File text if it is a small text file, else None."""
    try:
        if os.path.getsize(path) > _TEXT_DIFF_LIMIT:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\x00" in data:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class _ComputerSession:
    """Per-task hook logic for computer mode: guard -> approval -> log."""

    def __init__(self, cfg: ComputerConfig, cstate: ComputerTaskState, turn: _TurnState,
                 jarvis_names: set[str], toolkit: list[str], custom_names: list[str],
                 chat_timeout_s: float = 120.0, cli_path: str | None = None):
        self.cfg = cfg
        self.state = cstate
        self.turn = turn
        self.jarvis_names = jarvis_names
        self.allowed_names = set(toolkit) | {f"{_TOOL_PREFIX}{n}" for n in jarvis_names} | {
            f"{_TOOL_PREFIX}{n}" for n in custom_names
        }
        self.protected = load_protected_packages(cfg.jarvis_dir)
        self.protected_dirs = load_protected_dirs(cli_path)
        self.chat_timeout_s = chat_timeout_s
        self.escalated = False       # True once a computer tool is used: then the computer limits apply
        self.t0 = 0.0
        self.deadline = float('inf')
        self._loop = None
        self._conn = None
        self._before: dict[str, str | None] = {}
        self._verdicts: dict[str, Verdict] = {}
        self._paths: dict[str, str] = {}
        self._last_progress = float("-inf")

    # -- limits -----------------------------------------------------------
    def start(self, now: float, loop) -> None:
        """Every turn starts with A1's wall clock; computer limits apply from the first computer tool."""
        self._loop = loop
        self.t0 = now
        self.deadline = now + self.chat_timeout_s

    def escalate(self) -> None:
        if self.escalated:
            return
        self.escalated = True
        if self._loop is not None:
            self.deadline = self.t0 + self.cfg.timeout_s

    def limits(self) -> tuple[int, float]:
        """(steps, seconds) currently in force."""
        if self.escalated:
            return self.cfg.max_steps, self.cfg.timeout_s
        return _A1_MAX_STEPS, self.chat_timeout_s

    # -- plumbing ---------------------------------------------------------
    async def on_discord(self, coro):
        """Await a coroutine on the Discord loop (we run on our own loop in a worker thread)."""
        loop = self.cfg.discord_loop
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        self.state.track(fut)  # the Stop button cancels a pending approval through this
        try:
            return await asyncio.wrap_future(fut)
        finally:
            self.state.untrack(fut)

    def _db(self, fn):
        """Run fn(conn); on a connection error reconnect once."""
        for attempt in (0, 1):
            try:
                if self._conn is None:
                    self._conn = self.cfg.conn_factory()
                return fn(self._conn)
            except Exception:
                self._close_conn()
                if attempt:
                    raise

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        try:
            if conn is not None and hasattr(conn, "close"):
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._close_conn()

    def _log(self, tool: str, detail: str, verdict: str, outcome: str, approver: str | None,
             strict: bool = True) -> None:
        def write(conn):
            log_activity(conn, self.cfg.machine, self.state.task_id, tool.replace("\x00", "")[:200],
                         detail.replace("\x00", "")[:1000], verdict, outcome, approver)
        try:
            self._db(write)
        except Exception:
            if strict:
                raise
            logger.warning("could not write the activity log", exc_info=True)

    # -- path helpers -----------------------------------------------------
    def _cli_join(self, raw: str, cwd: str) -> str:
        """Mirror the CLI's expandPath for built-in tools: ~ and ~/ -> home, Git-Bash
        /c/... -> C:\\... on Windows, relative -> joined to the shell's cwd. No env vars."""
        p = raw.strip()
        home = self.cfg.home
        if p == "~":
            p = home
        elif p.startswith(("~/", "~\\")):
            p = os.path.join(home, p[2:])
        if self.cfg.os_name == "windows":
            m = _WINDOWS_GITBASH_DRIVE.match(p)
            if m:
                p = m.group(1).upper() + ":\\" + (m.group(2) or "").replace("/", "\\")
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        return p

    def _real(self, raw: str, custom: bool, cwd: str | None = None) -> str:
        try:
            p = resolve_tool_path(raw, self.cfg.home) if custom else self._cli_join(raw, cwd or self.cfg.home)
            return os.path.realpath(p)
        except Exception:  # noqa: BLE001
            return raw

    def _exists(self, raw: str, cwd: str) -> bool:
        try:
            p = self._cli_join(raw, cwd)
            return os.path.lexists(p) or os.path.exists(os.path.realpath(p))
        except Exception:  # noqa: BLE001
            return True  # unknown counts as existing

    def _guard_args(self, tool_name: str, tool_input: dict, cwd: str) -> dict:
        # Never trust underscore keys from the model: they are the hook's own channel.
        args = {k: v for k, v in tool_input.items() if not str(k).startswith("_")}
        short = tool_name[len(_TOOL_PREFIX):] if tool_name.startswith(_TOOL_PREFIX) else None

        def one(key: str, custom: bool) -> None:
            raw = args.get(key)
            if isinstance(raw, str) and raw.strip():
                args["_real_path"] = self._real(raw, custom, cwd)

        if tool_name in _PATH_TOOLS_ONE:
            one("file_path", False)
            if tool_name == "Write":
                raw = args.get("file_path")
                args["_exists"] = self._exists(raw, cwd) if isinstance(raw, str) and raw.strip() else True
        elif tool_name in ("Glob", "Grep"):
            one("path", False)
        elif short in _CUSTOM_ONE_PATH:
            one("path", True)
        elif short == "move_to_trash":
            paths = args.get("paths")
            if paths is None:
                one("path", True)
                paths = [args["path"]] if isinstance(args.get("path"), str) else None
            if isinstance(paths, list):
                args["_real_paths"] = {p: self._real(p, True) for p in paths if isinstance(p, str)}
        elif short in ("move_paths", "copy_paths"):
            items = args.get("moves" if short == "move_paths" else "copies")
            real, exists = {}, {}
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                for key in ("from", "to"):
                    raw = item.get(key)
                    if isinstance(raw, str) and raw.strip():
                        real[raw] = self._real(raw, True)
                dst = item.get("to")
                if isinstance(dst, str) and dst.strip():
                    p = resolve_tool_path(dst, self.cfg.home)
                    exists[dst] = os.path.lexists(p) or os.path.exists(os.path.realpath(p))
            args["_real_paths"] = real
            args["_exists"] = exists
        return args

    def _guard_ctx(self, cwd: str, extra_taint: bool = False) -> GuardContext:
        cfg = self.cfg
        bypass = False
        if cfg.machine == "pc":
            now = datetime.now(timezone.utc)
            bypass = self._db(lambda c: get_bypass_until(c, now)) is not None
        return GuardContext(
            machine=cfg.machine, os_name=cfg.os_name, home=cfg.home, jarvis_dir=cfg.jarvis_dir,
            bypass=bypass, tainted=self.state.tainted or extra_taint,
            protected_packages=self.protected, protected_dirs=self.protected_dirs, cwd=cwd,
        )

    @staticmethod
    def _detail(tool_name: str, args: dict) -> str:
        clean = {k: v for k, v in args.items() if not k.startswith("_")}
        if isinstance(clean.get("command"), str):
            return clean["command"]
        if isinstance(clean.get("paths"), list):
            return "\n".join(str(p) for p in clean["paths"])
        for key in ("moves", "copies"):
            if isinstance(clean.get(key), list):
                return "\n".join(
                    f"{i.get('from')} -> {i.get('to')}" if isinstance(i, dict) else str(i) for i in clean[key]
                )
        try:
            return json.dumps(clean, ensure_ascii=False, default=str)[:20000]
        except Exception:  # noqa: BLE001
            return str(clean)[:20000]

    @staticmethod
    def _batch(args: dict) -> int:
        for key in ("paths", "moves", "copies"):
            if isinstance(args.get(key), list) and args[key]:
                return len(args[key])
        return 1

    # -- taint / bookkeeping ---------------------------------------------
    def _under_untrusted(self, raw, real, cwd: str) -> bool:
        """Is this path in Downloads/Temp/tmp? Judged below the owner's home, so a home
        folder (or test dir) that merely contains the word does not count."""
        gctx = GuardContext(self.cfg.machine, self.cfg.os_name, self.cfg.home, self.cfg.jarvis_dir, cwd=cwd)
        forms = []
        for x in (raw, real):
            if isinstance(x, str) and x.strip():
                forms.append(normalize_path(x, gctx))
        home = normalize_path(self.cfg.home, gctx).rstrip("/")
        forms = [("/" + p[len(home):].lstrip("/")) if p.lower().startswith(home.lower() + "/") else p
                 for p in forms]
        return any(_UNTRUSTED_DIR.search(p.rstrip("/") + "/") for p in forms)

    def _touches_untrusted(self, tool_name: str, args: dict, cwd: str) -> bool:
        if tool_name in ("Bash", "PowerShell"):
            cmd = args.get("command")
            return isinstance(cmd, str) and bool(_UNTRUSTED_TEXT.search(cmd))
        if tool_name == "Read":
            return self._under_untrusted(args.get("file_path"), args.get("_real_path"), cwd)
        if tool_name in ("Grep", "Glob"):
            texts = [args.get("pattern"), args.get("glob")] if tool_name == "Grep" else [args.get("pattern")]
            if any(isinstance(t, str) and _UNTRUSTED_TEXT.search(t) for t in texts):
                return True
            return self._under_untrusted(args.get("path") or cwd, args.get("_real_path"), cwd)
        return False

    def _record_allowed(self, tool_name: str, args: dict, tool_use_id, touches: bool,
                        verdict: Verdict) -> None:
        st = self.state
        web = tool_name.startswith(_TOOL_PREFIX) and tool_name[len(_TOOL_PREFIX):] in _WEB_TAINT_TOOLS
        if web or touches:
            st.tainted = True
            st.self_tainted = True
        if st.tainted:
            # The A1 gate: after untrusted content, Jarvis's PC-action tools are refused.
            self.turn.info_tool_ran = True
        # Jarvis tools record themselves in their own handler; the rest are recorded here.
        if not (tool_name.startswith(_TOOL_PREFIX) and tool_name[len(_TOOL_PREFIX):] in self.jarvis_names):
            self.turn.tools_ran.append(tool_name)
            if tool_name not in _NO_SIDE_EFFECT:
                self.turn.side_effect_tools_ran.append(tool_name)
        if tool_name in ("Write", "Edit", "MultiEdit") and tool_use_id:
            raw = args.get("file_path")
            real = args.get("_real_path")
            if isinstance(raw, str) and isinstance(real, str) and verdict < Verdict.ALWAYS_ASK:
                self._before[tool_use_id] = _read_text(real) if os.path.exists(real) else ""

    def _under_home(self, cwd: str) -> bool:
        gctx = GuardContext(self.cfg.machine, self.cfg.os_name, self.cfg.home, self.cfg.jarvis_dir)
        c = normalize_path(cwd, gctx).rstrip("/").lower()
        h = normalize_path(self.cfg.home, gctx).rstrip("/").lower()
        return c == h or c.startswith(h + "/")

    async def _progress(self) -> None:
        cb = self.cfg.progress
        now = time.monotonic()
        if cb is None or now - self._last_progress < _PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        try:
            await asyncio.wait_for(
                self.on_discord(cb(f"⏳ Working… {self.state.steps} steps done")), timeout=10
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - progress is cosmetic
            logger.debug("progress update failed", exc_info=True)

    # -- hooks ------------------------------------------------------------
    async def pre_tool_use(self, input_data: dict, tool_use_id, context) -> dict:
        tool_name = ""
        try:
            tool_name = str(input_data.get("tool_name", ""))
            return await self._pre(
                tool_name, input_data.get("tool_input") or {}, tool_use_id, input_data.get("cwd")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # fail closed: any guard/broker/DB error denies
            logger.warning("computer-mode hook error for %s: %s", tool_name, exc, exc_info=True)
            self._log(tool_name, f"hook error: {type(exc).__name__}", "ERROR", "error", None, strict=False)
            return _deny("The safety check failed, so this was blocked.")

    async def _pre(self, tool_name: str, tool_input: dict, tool_use_id, cwd_in=None) -> dict:
        st, cfg = self.state, self.cfg
        st.steps += 1
        if not isinstance(tool_input, dict):
            tool_input = {}
        if st.stopped:
            self._log(tool_name, "stopped", "REFUSE", "denied", None, strict=False)
            return _deny("Stopped by the owner.")
        if tool_name in self.allowed_names and not (
            tool_name.startswith(_TOOL_PREFIX) and tool_name[len(_TOOL_PREFIX):] in self.jarvis_names
        ):
            self.escalate()  # the first computer tool lifts the chat limits (also covers a long approval wait)
        steps_limit = self.limits()[0]
        if st.steps > steps_limit:
            st.limit_hit = st.limit_hit or "steps"
            self._log(tool_name, "step limit", "REFUSE", "denied", None, strict=False)
            return _deny(f"Step limit of {steps_limit} reached - stop and report what is done.")
        if tool_name not in self.allowed_names:
            self._log(tool_name, "not in this toolkit", "REFUSE", "denied", None, strict=False)
            return _deny(f"{tool_name} is not available.")
        cwd = cfg.home
        if isinstance(cwd_in, str) and cwd_in.strip():
            if not self._under_home(cwd_in):
                self._log(tool_name, "working directory changed", "REFUSE", "denied", None, strict=False)
                return _deny("working directory changed unexpectedly")
            cwd = cwd_in
        await self._progress()

        args = self._guard_args(tool_name, tool_input, cwd)
        touches = self._touches_untrusted(tool_name, args, cwd)
        # re-reads bypass every step; a call that itself touches untrusted places counts as tainted
        decision = evaluate(tool_name, args, self._guard_ctx(cwd, touches))
        if tool_use_id:
            self._verdicts[tool_use_id] = decision.verdict
            if isinstance(args.get("_real_path"), str):
                self._paths[tool_use_id] = args["_real_path"]
        summary = decision.summary or tool_name
        verdict = decision.verdict.name

        if decision.verdict == Verdict.REFUSE:
            self._log(tool_name, summary, verdict, "denied", None)
            return _deny(decision.reason)

        if decision.needs_approval:
            reason = decision.reason or ""
            says = tool_input.get("description")
            if isinstance(says, str) and says.strip():
                reason += f"\nClaude says: {says.strip()[:200]}"
            req = ApprovalRequest(
                machine=cfg.machine, summary=summary, detail=self._detail(tool_name, args),
                reason=reason, batch_size=self._batch(args),
            )
            try:
                result = await self.on_discord(cfg.broker.request(req, cfg.approval_timeout_s))
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if st.stopped and not (task is not None and task.cancelling()):
                    self._log(tool_name, summary, verdict, "denied", None)  # the Stop button cancelled the card
                    return _deny("Stopped by the owner.")
                raise
            if result.allowed is not True:
                outcome = result.outcome if result.outcome in ("denied", "timeout", "error") else "denied"
                self._log(tool_name, summary, verdict, outcome, result.approver)
                if outcome == "timeout":
                    return _deny("No answer from the owner in time, so this was denied.")
                return _deny("The owner denied this action.")
            self._log(tool_name, summary, verdict, "allowed", result.approver)
        else:
            bypassed = decision.verdict == Verdict.ASK_UNLESS_BYPASS
            self._log(tool_name, summary, verdict, "bypass" if bypassed else "auto", None)

        self._record_allowed(tool_name, args, tool_use_id, touches, decision.verdict)
        return {}

    async def post_tool_use(self, input_data: dict, tool_use_id, context) -> dict:
        try:
            self._post(str(input_data.get("tool_name", "")), input_data.get("tool_input") or {},
                       input_data.get("tool_response"), tool_use_id)
        except Exception:  # noqa: BLE001 - result collection must never break a task
            logger.warning("computer-mode result collection failed", exc_info=True)
        return {}

    def _post(self, tool_name: str, tool_input: dict, response, tool_use_id) -> None:
        st = self.state
        verdict = self._verdicts.pop(tool_use_id, None) if tool_use_id else None
        # Never put a secret's contents in an attachment: any step that needed an
        # unconditional approval (or whose verdict is unknown) is reported without them.
        omit = verdict is None or verdict >= Verdict.ALWAYS_ASK
        if tool_name in ("Write", "Edit", "MultiEdit"):
            raw = tool_input.get("file_path")
            if isinstance(raw, str):
                real = self._paths.pop(tool_use_id, None) or self._real(raw, False, self.cfg.home)
                st.files_changed.add(real)
                before = self._before.pop(tool_use_id, None)
                if omit:
                    st.diffs.append(f"=== {raw} ===\n{_OMITTED}\n")
                    return self._collect_output(tool_name, response, omit)
                after = _read_text(real)
                if after is not None and before is not None and st.diff_bytes < _MAX_ATTACHMENT_BYTES:
                    diff = "".join(difflib.unified_diff(
                        before.splitlines(keepends=True), after.splitlines(keepends=True),
                        fromfile=f"a/{raw}", tofile=f"b/{raw}",
                    ))
                    if diff:
                        if not diff.endswith("\n"):
                            diff += "\n"
                        st.diffs.append(diff)
                        st.diff_bytes += len(diff.encode("utf-8", "replace"))
        self._collect_output(tool_name, response, omit)

    def _collect_output(self, tool_name: str, response, omit: bool) -> None:
        text = _response_text(response)
        if len(text) > 1500:
            self.state.outputs.append(f"=== {tool_name} ===\n{_OMITTED if omit else text}")

    def attachments(self) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        st = self.state
        if st.outputs:
            out.append(("output.txt", _cap("\n\n".join(st.outputs))))
        if st.diffs:
            out.append(("changes.diff", _cap("".join(st.diffs))))
        return out


def _cap(text: str) -> bytes:
    data = text.encode("utf-8", "replace")
    if len(data) > _MAX_ATTACHMENT_BYTES:
        data = data[:_MAX_ATTACHMENT_BYTES] + b"\n[truncated at 8 MB]\n"
    return data


class ClaudeBrain:
    name = "sonnet"

    def __init__(
        self,
        oauth_token: str | None,
        model: str = "sonnet",
        timeout: float = 120.0,
        cli_path: str | None = None,
        computer: "ComputerConfig | None" = None,
    ):
        self.computer = computer
        self.oauth_token = oauth_token
        self.model = model
        self.timeout = timeout
        self.cli_path = cli_path
        # Jarvis pins its own CLI; the SDK's startup version probe just adds
        # latency (and a warning) to every turn.
        os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")

    def _failure(self, state: _TurnState, failure: str, detail: str = "") -> BrainResult:
        return BrainResult(
            None, self.name, tools_ran=state.tools_ran,
            side_effect_tools_ran=state.side_effect_tools_ran,
            handled_as_note=state.handled_as_note,
            failure=failure, failure_detail=detail,
        )

    def respond(self, ctx: TurnContext) -> BrainResult:
        if not self.oauth_token:
            return BrainResult(
                None, self.name, failure=FAIL_CONFIG, failure_detail="no Claude token"
            )

        state = _TurnState()
        session = self._new_session(ctx, state)
        try:
            if session is None:
                return asyncio.run(
                    asyncio.wait_for(self._respond_async(ctx, state, session), timeout=self.timeout)
                )
            return asyncio.run(self._guarded(ctx, state, session))
        except Exception as exc:
            if isinstance(exc, TimeoutError) and session is not None:
                session.state.limit_hit = "time"
                return self._limit_result(state, session)
            text = str(exc) or type(exc).__name__
            is_limit, reset = parse_limit(text)
            if is_limit:
                return self._failure(state, FAIL_LIMIT, reset)
            logger.warning("Claude brain failed: %s", text)
            return self._failure(state, FAIL_UNAVAILABLE, text)
        finally:
            if session is not None:
                session.close()

    async def _guarded(self, ctx: TurnContext, state: _TurnState, session: "_ComputerSession") -> BrainResult:
        """Run the turn under a deadline the session can extend (chat 120 s -> computer 30 min)."""
        loop = asyncio.get_running_loop()
        session.start(loop.time(), loop)
        task = asyncio.ensure_future(self._respond_async(ctx, state, session))
        try:
            while True:
                # polled: escalate() can move the deadline while we wait
                done, _ = await asyncio.wait(
                    {task}, timeout=max(0.0, min(0.25, session.deadline - loop.time()))
                )
                if done:
                    return task.result()
                if loop.time() >= session.deadline:
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - the cancelled turn
                        pass
                    raise TimeoutError
        except asyncio.CancelledError:
            task.cancel()
            raise

    def _toolkit(self) -> list[str]:
        cfg = self.computer
        shell = shell_tool_name(cfg.os_name)
        if cfg.machine == "server":
            return [shell, "Read", "Glob", "Grep"]
        return ["Read", "Write", "Edit", "Glob", "Grep", shell]

    def _new_session(self, ctx: TurnContext, state: _TurnState) -> "_ComputerSession | None":
        cfg = self.computer
        if cfg is None:
            return None
        cstate = cfg.state or ComputerTaskState(task_id=uuid.uuid4().hex[:12])
        cfg.state = cstate
        jarvis_names = {t["name"] for t in ctx.tools}
        tools = self._computer_tools(cstate)
        return _ComputerSession(
            cfg, cstate, state, jarvis_names, self._toolkit(), [t.name for t in tools],
            chat_timeout_s=self.timeout, cli_path=self.cli_path,
        )

    def _computer_tools(self, cstate: ComputerTaskState) -> list[SdkMcpTool]:
        cfg = self.computer

        async def send_file_cb(path: str) -> str:
            if cfg.send_file is None:
                return "ERROR: sending files is not available here"
            return await self._on_discord(cfg.send_file(path))

        async def queue_task_cb(text: str) -> str:
            if cfg.queue_task is not None:
                return await self._on_discord(cfg.queue_task(text))
            from brain.computer_state import queue_pc_task

            def run() -> int:
                conn = cfg.conn_factory()
                try:
                    return queue_pc_task(conn, text, datetime.now(timezone.utc))
                finally:
                    if hasattr(conn, "close"):
                        conn.close()

            task_id = await asyncio.to_thread(run)
            return f"Queued task #{task_id} for the PC."

        gctx = GuardContext(cfg.machine, cfg.os_name, cfg.home, cfg.jarvis_dir, cwd=cfg.home)
        return build_computer_tools(
            cstate, send_file_cb, queue_task_cb, ctx=gctx, cwd=cfg.home, machine=cfg.machine,
            claude_path=self.cli_path,
        )

    async def _on_discord(self, coro):
        loop = self.computer.discord_loop
        if loop is None or loop is asyncio.get_running_loop():
            return await coro
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    def _limit_message(self, session: "_ComputerSession") -> str:
        st = session.state
        steps_limit, seconds = session.limits()
        minutes = max(1, round(seconds / 60))
        done = f"{st.steps} steps, {len(st.files_changed)} files changed"
        if st.texts:
            done += f". Last note: {st.texts[-1].strip()[:300]}"
        return (
            f"Stopped at the limit ({steps_limit} steps / {minutes} min). "
            f"Done so far: {done}. Say 'continue' to finish."
        )

    async def _messages(self, prompt, options, session):
        """query() messages; in computer mode the stream is closed as soon as Stop is pressed."""
        agen = query(prompt=prompt, options=options)
        if session is None:
            async for message in agen:
                yield message
            return
        it = agen.__aiter__()
        try:
            while not session.state.stopped:
                nxt = asyncio.ensure_future(it.__anext__())
                while True:
                    done, _ = await asyncio.wait({nxt}, timeout=0.25)
                    if done:
                        break
                    if session.state.stopped:
                        nxt.cancel()
                        try:
                            await nxt
                        except BaseException:  # noqa: BLE001
                            pass
                        return
                try:
                    message = nxt.result()
                except StopAsyncIteration:
                    return
                yield message
        finally:
            close = getattr(agen, "aclose", None)
            if close is not None:
                try:
                    await close()
                except BaseException:  # noqa: BLE001
                    pass

    def _stopped_result(self, state: _TurnState, session: "_ComputerSession") -> BrainResult:
        st = session.state
        text = f"\U0001F6D1 Stopped by you after {st.steps} steps."
        if st.files_changed:
            text += f" Done so far: {len(st.files_changed)} files changed."
        return BrainResult(
            text, self.name, tools_ran=state.tools_ran,
            side_effect_tools_ran=state.side_effect_tools_ran,
            handled_as_note=state.handled_as_note, attachments=session.attachments(),
        )

    def _limit_result(self, state: _TurnState, session: "_ComputerSession") -> BrainResult:
        return BrainResult(
            self._limit_message(session), self.name, tools_ran=state.tools_ran,
            side_effect_tools_ran=state.side_effect_tools_ran,
            handled_as_note=state.handled_as_note, attachments=session.attachments(),
        )

    def _a1_options(self, ctx: TurnContext, server) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=ctx.system_prompt + _EXTRA_SYSTEM_PROMPT,
            mcp_servers={_MCP_SERVER_NAME: server},
            # Layer 1: only Jarvis's own MCP tools are pre-approved.
            allowed_tools=[f"{_TOOL_PREFIX}{t['name']}" for t in ctx.tools],
            # Layer 2: every Claude Code built-in explicitly denied.
            disallowed_tools=BUILTIN_TOOLS,
            # Layer 2b: disable the built-in tool preset outright, so any
            # built-in missing from BUILTIN_TOOLS (a future CLI addition)
            # isn't loaded in the first place.
            tools=[],
            # Don't load the user's own ~/.claude/settings.json, project
            # settings, hooks, plugins or apiKeyHelper — this session runs
            # with only what's configured right here.
            setting_sources=[],
            # Ignore any MCP servers configured outside this call (project
            # .mcp.json, user/global settings, plugins) — only "jarvis".
            strict_mcp_config=True,
            # Layer 3: don't interactively prompt for anything not already
            # pre-approved — deny it. Combined with the PreToolUse hook
            # below as an explicit, testable deny-by-default.
            permission_mode="dontAsk",
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_deny_non_jarvis_tools_hook])]},
            max_turns=8,
            cwd=str(_scratch_dir()),
            env=_build_env(self.oauth_token),
            # Jarvis keeps its own conversation history in Postgres; each
            # message is a fresh, stateless call, and the CLI shouldn't
            # write a resumable transcript for it either.
            extra_args={"no-session-persistence": None},
            cli_path=self.cli_path,
        )

    def _computer_options(
        self, ctx: TurnContext, session: "_ComputerSession", sdk_tools: list[SdkMcpTool]
    ) -> ClaudeAgentOptions:
        cfg = self.computer
        toolkit = self._toolkit()
        server = create_sdk_mcp_server(name=_MCP_SERVER_NAME, tools=sdk_tools)
        mcp_names = [f"{_TOOL_PREFIX}{t.name}" for t in sdk_tools]
        denied = (set(BUILTIN_TOOLS) | {"PowerShell"}) - set(toolkit)
        if cfg.machine == "server":
            prompt_extra = _SERVER_PROMPT
        else:
            prompt_extra = _COMPUTER_PROMPT + computer_tools.owner_folders_line(cfg.os_name)
        return ClaudeAgentOptions(
            model=self.model,
            system_prompt=ctx.system_prompt + _EXTRA_SYSTEM_PROMPT + prompt_extra,
            mcp_servers={_MCP_SERVER_NAME: server},
            # Pre-approved here; the PreToolUse hook below is the real gate
            # (a hook deny always wins) and denies anything the guard refuses
            # or the owner doesn't approve.
            allowed_tools=toolkit + mcp_names,
            disallowed_tools=sorted(denied),
            tools=toolkit,
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            hooks={
                "PreToolUse": [HookMatcher(
                    matcher=None, hooks=[session.pre_tool_use],
                    # must outlast an approval wait (the default is 60 s)
                    timeout=cfg.approval_timeout_s + 60,
                )],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[session.post_tool_use])],
            },
            max_turns=cfg.max_steps,
            cwd=cfg.home,
            env=_build_env(self.oauth_token, computer=True, os_name=cfg.os_name),
            extra_args={"no-session-persistence": None},
            cli_path=self.cli_path,
        )

    async def _respond_async(
        self, ctx: TurnContext, state: _TurnState, session: "_ComputerSession | None" = None
    ) -> BrainResult:
        sdk_tools = _build_sdk_tools(ctx, state)
        if session is not None:
            sdk_tools = sdk_tools + self._computer_tools(session.state)
            options = self._computer_options(ctx, session, sdk_tools)
        else:
            options = self._a1_options(ctx, create_sdk_mcp_server(
                name=_MCP_SERVER_NAME, tools=sdk_tools
            ))
        prompt = self._build_prompt(ctx)

        texts: list[str] = []
        final_result: ResultMessage | None = None
        rejected_reset = None
        rejected = False
        async for message in self._messages(prompt, options, session):
            if isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                if info.status == "rejected":
                    rejected = True
                    rejected_reset = _format_reset(info.resets_at)
            elif isinstance(message, AssistantMessage):
                if message.error in ("rate_limit", "billing_error"):
                    return self._failure(state, FAIL_LIMIT, rejected_reset or "")
                if message.error == "authentication_failed":
                    return self._failure(state, FAIL_CONFIG, "Claude authentication failed")
                new_texts = [
                    block.text for block in message.content if isinstance(block, TextBlock)
                ]
                texts.extend(new_texts)
                if session is not None:
                    session.state.texts.extend(new_texts)
            elif isinstance(message, ResultMessage):
                final_result = message

        if session is not None and session.state.stopped:
            return self._stopped_result(state, session)

        if session is not None:
            if final_result is not None and (
                final_result.subtype == "error_max_turns"
                or final_result.terminal_reason == "max_turns"
            ):
                session.state.limit_hit = session.state.limit_hit or "steps"
            if session.state.limit_hit:
                return self._limit_result(state, session)

        joined = "\n".join(texts) if texts else ""

        if final_result is not None and final_result.is_error:
            detail = final_result.result or joined
            is_limit, reset = parse_limit(detail)
            if is_limit or rejected:
                return self._failure(state, FAIL_LIMIT, reset or rejected_reset or "")
            return self._failure(state, FAIL_UNAVAILABLE, detail)

        answer = (final_result.result if final_result and final_result.result else None) or (
            joined or None
        )
        # Never hand a usage-limit notice back to the user as if it were an answer.
        if answer and parse_limit(answer)[0]:
            return self._failure(state, FAIL_LIMIT, parse_limit(answer)[1])
        if not answer:
            return self._failure(state, FAIL_LIMIT if rejected else FAIL_UNAVAILABLE,
                                 (rejected_reset or "") if rejected else "")

        return BrainResult(
            answer, self.name, tools_ran=state.tools_ran,
            side_effect_tools_ran=state.side_effect_tools_ran,
            handled_as_note=state.handled_as_note,
            attachments=session.attachments() if session is not None else [],
        )

    def _build_prompt(self, ctx: TurnContext) -> str:
        # The system prompt is already sent via system_prompt=, so only the
        # profile, history and the new message go into the prompt text.
        profile_text = (ctx.profile or "").strip() or "(no profile yet)"
        history_text = (
            "\n".join(f"{m['role']}: {m['content']}" for m in ctx.history)
            or "(no prior messages)"
        )
        prompt = (
            f"About the owner (their profile):\n{profile_text}\n\n"
            f"Conversation so far:\n{history_text}\n\n"
            f"user: {ctx.message}\nassistant:"
        )
        if ctx.image_bytes:
            # True multimodal prompts need the streaming (async-iterable)
            # query() form; not implemented in A1 (see task report). The
            # router's image floor keeps images on Gemini unless the judge
            # or an explicit override raises the message to Claude anyway.
            prompt += _IMAGE_UNSEEN_NOTE
        return prompt
