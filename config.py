import datetime
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Ordered strongest-to-weakest. Each Gemini model has its own separate
# free-tier daily quota, so falling through this list on exhaustion
# stretches the effective daily capacity far past any single model's limit.
DEFAULT_MODEL_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]


def resolve_api_key() -> str | None:
    load_dotenv()
    return os.getenv("GEMINI_API_KEY")


def resolve_browser_executable_path() -> str | None:
    load_dotenv()
    return os.getenv("BROWSER_EXECUTABLE_PATH") or None


def resolve_exa_api_key() -> str | None:
    load_dotenv()
    return os.getenv("EXA_API_KEY") or None


DEFAULT_TIMEZONE = "Asia/Tbilisi"


def resolve_timezone() -> ZoneInfo:
    load_dotenv()
    name = os.getenv("JARVIS_TIMEZONE") or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown JARVIS_TIMEZONE %r, using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


DEFAULT_TODO_LIST_TIME = datetime.time(9, 0)


def resolve_todo_list_time() -> datetime.time:
    load_dotenv()
    raw = os.getenv("TODO_LIST_TIME")
    if not raw:
        return DEFAULT_TODO_LIST_TIME
    try:
        return datetime.time.fromisoformat(raw.strip())
    except ValueError:
        logger.warning("invalid TODO_LIST_TIME %r, using 09:00", raw)
        return DEFAULT_TODO_LIST_TIME


DEFAULT_NUDGE_TIME = datetime.time(19, 30)


def resolve_nudge_time() -> datetime.time:
    load_dotenv()
    raw = os.getenv("NUDGE_TIME")
    if not raw:
        return DEFAULT_NUDGE_TIME
    try:
        return datetime.time.fromisoformat(raw.strip())
    except ValueError:
        logger.warning("invalid NUDGE_TIME %r, using 19:30", raw)
        return DEFAULT_NUDGE_TIME


DEFAULT_TODO_LIST_LEAD_MINUTES = 30


def resolve_todo_list_lead_minutes() -> int:
    load_dotenv()
    raw = os.getenv("TODO_LIST_LEAD_MINUTES")
    if not raw:
        return DEFAULT_TODO_LIST_LEAD_MINUTES
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "invalid TODO_LIST_LEAD_MINUTES %r, using %d", raw, DEFAULT_TODO_LIST_LEAD_MINUTES,
        )
        return DEFAULT_TODO_LIST_LEAD_MINUTES
    if value < 0:
        logger.warning(
            "invalid TODO_LIST_LEAD_MINUTES %r, using %d", raw, DEFAULT_TODO_LIST_LEAD_MINUTES,
        )
        return DEFAULT_TODO_LIST_LEAD_MINUTES
    return value


def _resolve_positive_int(name: str, default: int) -> int:
    load_dotenv()
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("invalid %s %r, using %d", name, raw, default)
        return default
    return value


def resolve_bypass_hours() -> int:
    return _resolve_positive_int("BYPASS_HOURS", 12)


def resolve_computer_task_max_steps() -> int:
    return _resolve_positive_int("COMPUTER_TASK_MAX_STEPS", 60)


def resolve_computer_task_timeout_minutes() -> int:
    return _resolve_positive_int("COMPUTER_TASK_TIMEOUT_MINUTES", 30)


def resolve_database_url() -> str:
    load_dotenv()
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return url


def resolve_instance_role() -> str:
    load_dotenv()
    return os.getenv("JARVIS_ROLE", "local")


def resolve_models() -> list[str]:
    raw = os.getenv("GEMINI_MODELS")
    if raw is not None:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if not models:
            raise ValueError("GEMINI_MODELS is set but contains no model names")
        return models

    legacy = os.getenv("GEMINI_MODEL")
    if legacy:
        logger.warning(
            "GEMINI_MODEL is deprecated; use GEMINI_MODELS (comma-separated) "
            "instead. Falling back to a single-model chain: %s",
            legacy,
        )
        return [legacy]

    return DEFAULT_MODEL_CHAIN


def resolve_groq_api_key() -> str | None:
    load_dotenv()
    return os.getenv("GROQ_API_KEY", "").strip() or None


def resolve_groq_model() -> str:
    load_dotenv()
    return os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"


def resolve_claude_model() -> str:
    load_dotenv()
    return os.getenv("CLAUDE_MODEL") or "sonnet"


def resolve_claude_oauth_token() -> str | None:
    load_dotenv()
    return os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip() or None


def resolve_claude_cli_path() -> str | None:
    """Path to the native Claude Code executable for the Agent SDK's
    cli_path. On Windows the SDK finds the npm claude.CMD shim and refuses
    batch scripts, so the native claude.exe must be passed explicitly. On
    Linux None lets the SDK find ~/.local/bin/claude itself."""
    load_dotenv()
    explicit = os.getenv("CLAUDE_CLI_PATH", "").strip()
    if explicit:
        return explicit
    if sys.platform != "win32":
        return None
    candidates = []
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
            / "bin" / "claude.exe"
        )
    userprofile = os.getenv("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / ".local" / "bin" / "claude.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def resolve_show_brain() -> bool:
    load_dotenv()
    raw = os.getenv("JARVIS_SHOW_BRAIN", "").strip().lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return True
