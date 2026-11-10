"""Left/right/centre placement on a monitor, and placed windows coming to the front."""
import pytest

import brain.window_manager as wm

pytestmark = pytest.mark.skipif(not wm.AUTOMATION_SUPPORTED, reason="Windows-only window placement")

MONITORS = [
    {"left": 0, "top": 0, "right": 2560, "bottom": 1400},
    {"left": 2560, "top": 0, "right": 4480, "bottom": 1160},   # 1920 x 1160
]


@pytest.fixture
def moves(monkeypatch):
    calls = []
    monkeypatch.setattr(wm, "list_monitors", lambda: MONITORS)
    monkeypatch.setattr(wm.win32gui, "MoveWindow",
                        lambda hwnd, x, y, w, h, repaint: calls.append((x, y, w, h)))
    return calls


@pytest.mark.parametrize("position,expected", [
    ("left", (2560, 0, 960, 1160)),
    ("right", (3520, 0, 960, 1160)),
    ("top", (2560, 0, 1920, 580)),
    ("bottom", (2560, 580, 1920, 580)),
    ("top-right", (3520, 0, 960, 580)),
    ("bottom-left", (2560, 580, 960, 580)),
])
def test_position_alone_sets_half_size_and_offset(moves, position, expected):
    assert wm._place_window(1, 2, position=position) is True
    assert moves == [expected]


def test_right_with_an_explicit_size_is_pushed_to_the_right_edge(moves):
    wm._place_window(1, 2, size=(0.25, 1.0), position="right")
    assert moves == [(4480 - 480, 0, 480, 1160)]


def test_center_centres_the_window(moves):
    wm._place_window(1, 1, size=(0.5, 0.5), position="center")
    assert moves == [(640, 350, 1280, 700)]


def test_no_position_keeps_the_old_top_left_behaviour(moves):
    wm._place_window(1, 2, size=(0.5, 1.0))
    assert moves == [(2560, 0, 960, 1160)]


def test_unknown_position_is_rejected(moves):
    assert wm._place_window(1, 2, position="diagonal") is False and moves == []


def test_adjusting_a_window_brings_it_to_the_front(monkeypatch, moves):
    raised = []
    monkeypatch.setattr(wm.win32gui, "ShowWindow", lambda h, cmd: None)
    monkeypatch.setattr(wm, "_bring_to_front", lambda h: raised.append(h))
    wm._adjust_window(7, 2, False, None, position="right")
    assert moves == [(3520, 0, 960, 1160)] and raised == [7]


def test_routine_style_adjustment_also_brings_it_to_the_front(monkeypatch, moves):
    raised = []
    monkeypatch.setattr(wm, "_bring_to_front", lambda h: raised.append(h))
    wm._apply_window_adjustments(8, 1, False, None, position="left")
    assert raised == [8]


def test_bring_to_front_raises_without_needing_focus_rights(monkeypatch):
    calls = []
    monkeypatch.setattr(wm.win32gui, "SetWindowPos", lambda h, after, *a: calls.append(after))
    monkeypatch.setattr(wm.win32gui, "SetForegroundWindow",
                        lambda h: (_ for _ in ()).throw(wm.pywintypes.error(0, "x", "denied")))
    wm._bring_to_front(9)   # a refused foreground request must not raise
    assert calls == [wm.win32con.HWND_TOPMOST, wm.win32con.HWND_NOTOPMOST]


def test_move_window_passes_the_position(monkeypatch, moves):
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: {5: "Trading journal - Brave"})
    monkeypatch.setattr(wm.win32gui, "ShowWindow", lambda h, cmd: None)
    monkeypatch.setattr(wm, "_bring_to_front", lambda h: None)
    out = wm.move_window("journal", 2, False, None, position="right")
    assert moves == [(3520, 0, 960, 1160)] and "right" in out


# ---- routines and ad-hoc placement carry the position --------------------------

def test_routine_step_position_is_parsed_and_validated():
    from brain.automation import ActionExecutionError, _parse_routine_steps

    steps = _parse_routine_steps('[{"name": "journal", "monitor": 2, "position": "right"}]')
    assert steps[0]["position"] == "right"
    assert _parse_routine_steps('["journal"]')[0]["position"] is None
    with pytest.raises(ActionExecutionError):
        _parse_routine_steps('[{"name": "journal", "monitor": 2, "position": "diagonal"}]')


def test_routine_execution_passes_the_position(monkeypatch):
    from brain import automation

    calls = []
    monkeypatch.setattr(automation, "get_action", lambda conn, name: {"kind": "url", "value": "https://x.y"})
    monkeypatch.setattr(automation.window_manager, "open_placed",
                        lambda kind, value, new_window, monitor, maximized=False, size=None, position=None:
                        calls.append((monitor, position)))
    automation._execute_steps(None, "r", automation._parse_routine_steps(
        '[{"name": "a", "monitor": 2, "position": "left"}, {"name": "b", "monitor": 2, "position": "right"}]'))
    assert calls == [(2, "left"), (2, "right")]


def test_open_placed_passes_the_position_to_the_window(monkeypatch):
    seen = []
    monkeypatch.setattr(wm, "resolve_browser_executable", lambda: r"C:\brave.exe")
    monkeypatch.setattr(wm, "_snapshot_browser_windows", lambda: set())
    monkeypatch.setattr(wm.subprocess, "Popen", lambda argv, **kw: None)
    monkeypatch.setattr(wm, "_find_new_browser_window", lambda before, **kw: 4)
    monkeypatch.setattr(wm, "_apply_window_adjustments",
                        lambda hwnd, monitor, maximized, size, position=None: seen.append(position))
    wm.open_placed("url", "https://x.y", True, 2, position="right")
    assert seen == ["right"]


def test_claude_window_tools_accept_a_position(monkeypatch, tmp_path):
    import asyncio

    from brain import computer_tools as ct
    from brain.computer_tools import ComputerTaskState
    from brain.guard import GuardContext

    calls = []
    monkeypatch.setattr(ct.window_manager, "move_window",
                        lambda t, m, mx, s, position=None: calls.append(position) or "ok")
    ctx = GuardContext("pc", "windows", str(tmp_path), r"E:\Programing\VoiceAssistant")
    tools = {t.name: t for t in ct.build_computer_tools(
        ComputerTaskState("t"), None, None, ctx=ctx, cwd=str(tmp_path))}
    asyncio.run(tools["move_window"].handler({"title": "journal", "monitor": 2, "position": "right"}))
    bad = asyncio.run(tools["move_window"].handler({"title": "journal", "monitor": 2, "position": "up"}))
    assert calls == ["right"] and bad["content"][0]["text"].startswith("ERROR")
