"""open_on_screen / move_window / list_windows: the window tools Claude's computer mode uses."""
import pytest

import brain.window_manager as wm

pytestmark = pytest.mark.skipif(not wm.AUTOMATION_SUPPORTED, reason="Windows-only window placement")

MONITORS = [
    {"left": 0, "top": 0, "right": 2560, "bottom": 1400},
    {"left": 2560, "top": 0, "right": 4480, "bottom": 1160},
]


@pytest.fixture
def placed(monkeypatch):
    """Record placement calls instead of moving real windows."""
    calls = []
    monkeypatch.setattr(wm, "list_monitors", lambda: MONITORS)
    monkeypatch.setattr(
        wm, "_adjust_window",
        lambda hwnd, monitor, maximized, size, position=None: calls.append((hwnd, monitor, maximized, size)) or True,
    )
    return calls


def test_urls_open_as_tabs_in_one_new_browser_window_on_the_monitor(monkeypatch, placed):
    launched = []
    monkeypatch.setattr(wm, "resolve_browser_executable", lambda: r"C:\brave.exe")
    monkeypatch.setattr(wm, "_snapshot_browser_windows", lambda: {1})
    monkeypatch.setattr(wm.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(wm, "_find_new_browser_window", lambda before, **kw: 77)

    out = wm.open_urls_on_screen(["https://claude.ai", "https://gemini.google.com"], 1, True, None)

    assert launched == [[r"C:\brave.exe", "--new-window", "https://claude.ai", "https://gemini.google.com"]]
    assert placed == [(77, 1, True, None)]
    assert "monitor 1" in out and "2 tabs" in out


def test_urls_must_be_http(monkeypatch, placed):
    out = wm.open_urls_on_screen(["file:///C:/x", "https://a.b"], 1, False, None)
    assert out.startswith("ERROR") and placed == []


def test_unknown_monitor_is_an_error_that_says_how_many_there_are(monkeypatch, placed):
    out = wm.open_urls_on_screen(["https://a.b"], 3, False, None)
    assert out.startswith("ERROR") and "2 monitors" in out


def test_without_a_browser_urls_still_open_but_say_placement_was_skipped(monkeypatch, placed):
    opened = []
    monkeypatch.setattr(wm, "resolve_browser_executable", lambda: None)
    monkeypatch.setattr(wm.webbrowser, "open", lambda url: opened.append(url) or True)
    out = wm.open_urls_on_screen(["https://a.b", "https://c.d"], 2, False, None)
    assert opened == ["https://a.b", "https://c.d"] and "couldn't place" in out


def test_folder_opens_in_explorer_on_the_monitor(monkeypatch, placed, tmp_path):
    launched = []
    monkeypatch.setattr(wm, "_snapshot_windows_by_class", lambda cls: {1})
    monkeypatch.setattr(wm.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(wm, "_find_new_window_by_class", lambda before, cls, **kw: 55)

    out = wm.open_path_on_screen(str(tmp_path), 2, False, None)

    assert launched == [["explorer", str(tmp_path)]]
    assert placed == [(55, 2, False, None)]
    assert "monitor 2" in out


def test_file_opens_with_its_app_and_the_new_window_is_placed(monkeypatch, placed, tmp_path):
    doc = tmp_path / "Captions.rtf"
    doc.write_text("x")
    started = []
    snapshots = iter([{1: "Discord"}, {1: "Discord", 9: "Captions.rtf - WordPad"}])
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: next(snapshots))
    monkeypatch.setattr(wm.os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr(wm.time, "sleep", lambda s: None)

    out = wm.open_path_on_screen(str(doc), 1, True, None)

    assert started == [str(doc)]
    assert placed == [(9, 1, True, None)]
    assert "monitor 1" in out


def test_file_whose_window_never_appears_is_opened_but_not_placed(monkeypatch, placed, tmp_path):
    doc = tmp_path / "a.rtf"
    doc.write_text("x")
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: {1: "Discord"})
    monkeypatch.setattr(wm.os, "startfile", lambda p: None, raising=False)
    monkeypatch.setattr(wm.time, "sleep", lambda s: None)
    clock = iter(range(0, 100))
    monkeypatch.setattr(wm.time, "monotonic", lambda: next(clock))

    out = wm.open_path_on_screen(str(doc), 1, False, None)

    assert placed == [] and "couldn't place" in out


def test_missing_path_is_an_error(placed, tmp_path):
    assert wm.open_path_on_screen(str(tmp_path / "nope"), 1, False, None).startswith("ERROR")


def test_move_window_moves_the_one_matching_window(monkeypatch, placed):
    monkeypatch.setattr(wm, "_visible_titled_windows",
                        lambda: {1: "Discord", 2: "coffee - File Explorer", 3: "Brave"})
    out = wm.move_window("coffee", 2, False, None)
    assert placed == [(2, 2, False, None)]
    assert "coffee - File Explorer" in out and "monitor 2" in out


def test_move_window_with_several_matches_lists_them_and_moves_nothing(monkeypatch, placed):
    monkeypatch.setattr(wm, "_visible_titled_windows",
                        lambda: {1: "Claude - Brave", 2: "Gemini - Brave"})
    out = wm.move_window("brave", 1, False, None)
    assert out.startswith("ERROR") and "Claude - Brave" in out and "Gemini - Brave" in out
    assert placed == []


def test_move_window_prefers_an_exact_title_match(monkeypatch, placed):
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: {1: "Notes", 2: "Notes - old"})
    wm.move_window("notes", 1, False, None)
    assert placed == [(1, 1, False, None)]


def test_move_window_with_no_match_is_an_error(monkeypatch, placed):
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: {1: "Discord"})
    assert wm.move_window("excel", 1, False, None).startswith("ERROR")


def test_list_windows_says_which_monitor_each_window_is_on(monkeypatch):
    monkeypatch.setattr(wm, "list_monitors", lambda: MONITORS)
    monkeypatch.setattr(wm, "_visible_titled_windows", lambda: {1: "Discord", 2: "Brave"})
    rects = {1: (100, 100, 900, 900), 2: (3000, 10, 4000, 1000)}
    monkeypatch.setattr(wm.win32gui, "GetWindowRect", lambda h: rects[h])
    out = wm.list_windows()
    assert "Discord (monitor 1)" in out and "Brave (monitor 2)" in out
    assert "2 monitors" in out


def test_adjust_window_restores_before_placing(monkeypatch):
    shown = []
    monkeypatch.setattr(wm.win32gui, "ShowWindow", lambda h, cmd: shown.append(cmd))
    monkeypatch.setattr(wm, "_place_window", lambda h, m, size=None, position=None: True)
    monkeypatch.setattr(wm, "_bring_to_front", lambda h: None)
    assert wm._adjust_window(5, 1, True, None) is True
    assert shown == [wm.win32con.SW_RESTORE, wm.win32con.SW_MAXIMIZE]
