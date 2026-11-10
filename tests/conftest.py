import os

import pytest
import requests
from dotenv import load_dotenv

from brain.memory import init_db

load_dotenv()
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/jarvis_test"
)
NOTES_TEST_DATABASE_URL = os.getenv("NOTES_TEST_DATABASE_URL")

_TABLES = [
    "messages", "facts", "actions", "pending_proposal", "local_heartbeat",
    "reminders", "todos", "daily_list", "reminder_pings",
    "computer_state", "pc_tasks", "activity_log", "profile", "profile_history", "lessons", "lesson_changes", "brain_state",
]
_NOTES_TABLES = ["notes", "consolidation_state"]


def _ends_with_test_suffix(database_url: str) -> bool:
    return database_url.rsplit("/", 1)[-1].endswith("_test")


@pytest.fixture
def conn():
    connection = init_db(TEST_DATABASE_URL)
    connection.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _clear_weather_geocode_cache():
    # brain.web_tools._geocode caches successful lookups in-process by
    # location string across the whole test session — without clearing it,
    # a test asserting requests.get is called N times for "Tokyo" (say)
    # would silently pass or fail depending on test order/whatever earlier
    # test happened to warm the cache for the same location string first.
    from brain.web_tools import _geocode_cache

    _geocode_cache.clear()
    yield
    _geocode_cache.clear()


def _blocked_session_request(self, method, url, *args, **kwargs):
    raise AssertionError(
        f"Test attempted a real HTTP request ({method} {url}) instead of mocking "
        "requests.get/requests.post at the module level (e.g. "
        '`patch("brain.web_tools.requests.get", ...)`) — tests must never touch the network.'
    )


@pytest.fixture(autouse=True)
def _block_real_http_requests(monkeypatch):
    # A safety net, not the primary isolation mechanism: every test that
    # exercises brain.web_tools already mocks requests.get/requests.post at
    # the module level, which replaces the name entirely and never reaches
    # this — Session.request only gets hit when something slips through
    # unmocked (e.g. a weather fetch a test forgot to patch), in which case
    # this fails loudly right where the real call would have gone out,
    # instead of silently hitting the live Open-Meteo API from a test.
    monkeypatch.setattr(requests.sessions.Session, "request", _blocked_session_request)


@pytest.fixture
def notes_conn():
    if not NOTES_TEST_DATABASE_URL:
        pytest.skip("NOTES_TEST_DATABASE_URL not set — see .env.example")
    if not _ends_with_test_suffix(NOTES_TEST_DATABASE_URL):
        pytest.fail(
            "NOTES_TEST_DATABASE_URL does not point at a database name "
            "ending in '_test' — refusing to truncate what might be "
            "production data"
        )
    connection = init_db(NOTES_TEST_DATABASE_URL)
    connection.execute(f"TRUNCATE {', '.join(_TABLES + _NOTES_TABLES)} RESTART IDENTITY")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _isolate_claude_scratch_dir(monkeypatch, tmp_path):
    # ClaudeBrain's cwd is ~/.jarvis/claude; never create that in the real
    # home directory from a test run.
    scratch = tmp_path / "claude-scratch"
    scratch.mkdir()
    from brain.brains import claude

    fake = lambda: scratch  # noqa: E731
    fake._real = claude._scratch_dir  # for the test of the real path logic
    monkeypatch.setattr(claude, "_scratch_dir", fake)


@pytest.fixture(autouse=True)
def _reset_router_judge_state():
    # The judge's rate-limit cooldown and warning throttle are module-level;
    # reset them so one test's simulated 429 can't disable the judge in another.
    import brain.router as router

    router._last_warned.clear()
    router._judge_paused_until = 0.0
    yield
    router._last_warned.clear()
    router._judge_paused_until = 0.0
