from unittest.mock import Mock

import pytest

import brain.window_manager as window_manager


@pytest.fixture(autouse=True)
def _never_raise_real_windows(monkeypatch):
    # These tests use made-up window handles; never let them reach a real window.
    if window_manager.AUTOMATION_SUPPORTED:
        monkeypatch.setattr(window_manager, "_bring_to_front", lambda hwnd: None)


def _fake_monitor_info(left, top, right, bottom, device):
    return {
        "Monitor": (left, top, right, bottom),
        "Work": (left, top, right, bottom),
        "Device": device,
    }


def _fake_display_device(name, active=True):
    dev = Mock()
    dev.DeviceName = name
    dev.StateFlags = 1 if active else 0
    return dev


def _fake_enum_display_devices(devices):
    def fn(_none, i):
        if i >= len(devices):
            raise IndexError("no more display devices")
        return devices[i]

    return fn


def test_list_monitors_returns_empty_list_when_none_found(monkeypatch):
    monkeypatch.setattr(window_manager.win32api, "EnumDisplayMonitors", lambda: [])
    monkeypatch.setattr(
        window_manager.win32api, "EnumDisplayDevices", _fake_enum_display_devices([])
    )

    assert window_manager.list_monitors() == []


def test_list_monitors_orders_by_windows_display_index_not_physical_position(monkeypatch):
    # Mirrors a real 3-monitor setup where the primary display (DISPLAY1)
    # sits physically in the middle, DISPLAY2 is to its right, and the
    # third active display (DISPLAY5 — DISPLAY3/4 exist but are inactive)
    # is to its left. "monitor 1/2/3" must follow Windows' own active
    # device order, not left-to-right physical position.
    monkeypatch.setattr(
        window_manager.win32api,
        "EnumDisplayMonitors",
        lambda: [
            ("hmon-right", None, None),
            ("hmon-left", None, None),
            ("hmon-middle", None, None),
        ],
    )
    info_by_handle = {
        "hmon-middle": _fake_monitor_info(0, 0, 2560, 1440, r"\\.\DISPLAY1"),
        "hmon-right": _fake_monitor_info(2560, 0, 4480, 1200, r"\\.\DISPLAY2"),
        "hmon-left": _fake_monitor_info(-2400, 1045, -1029, 1662, r"\\.\DISPLAY5"),
    }
    monkeypatch.setattr(
        window_manager.win32api, "GetMonitorInfo", lambda h: info_by_handle[h]
    )
    monkeypatch.setattr(
        window_manager.win32api,
        "EnumDisplayDevices",
        _fake_enum_display_devices(
            [
                _fake_display_device(r"\\.\DISPLAY1", active=True),
                _fake_display_device(r"\\.\DISPLAY2", active=True),
                _fake_display_device(r"\\.\DISPLAY3", active=False),
                _fake_display_device(r"\\.\DISPLAY4", active=False),
                _fake_display_device(r"\\.\DISPLAY5", active=True),
            ]
        ),
    )

    monitors = window_manager.list_monitors()

    assert [m["left"] for m in monitors] == [0, 2560, -2400]


def test_list_monitors_uses_the_work_area_not_the_full_monitor_rect(monkeypatch):
    monkeypatch.setattr(
        window_manager.win32api, "EnumDisplayMonitors", lambda: [("hmon", None, None)]
    )
    info = {"Monitor": (0, 0, 1920, 1080), "Work": (0, 0, 1920, 1040), "Device": r"\\.\DISPLAY1"}
    monkeypatch.setattr(window_manager.win32api, "GetMonitorInfo", lambda h: info)
    monkeypatch.setattr(
        window_manager.win32api,
        "EnumDisplayDevices",
        _fake_enum_display_devices([_fake_display_device(r"\\.\DISPLAY1", active=True)]),
    )

    monitors = window_manager.list_monitors()

    assert monitors == [{"left": 0, "top": 0, "right": 1920, "bottom": 1040}]


def test_resolve_browser_executable_prefers_the_env_override_when_the_file_exists(
    monkeypatch,
):
    monkeypatch.setattr(
        window_manager, "resolve_browser_executable_path", lambda: "C:/Brave/brave.exe"
    )
    monkeypatch.setattr(window_manager.os.path, "isfile", lambda p: p == "C:/Brave/brave.exe")

    assert window_manager.resolve_browser_executable() == "C:/Brave/brave.exe"


def test_resolve_browser_executable_ignores_env_override_when_file_missing(monkeypatch):
    monkeypatch.setattr(
        window_manager, "resolve_browser_executable_path", lambda: "C:/Brave/brave.exe"
    )
    monkeypatch.setattr(window_manager.os.path, "isfile", lambda p: False)

    assert window_manager.resolve_browser_executable() is None


def test_resolve_browser_executable_falls_back_to_a_common_brave_path(monkeypatch):
    monkeypatch.setattr(window_manager, "resolve_browser_executable_path", lambda: None)
    common_path = window_manager._COMMON_BRAVE_PATHS[0]
    monkeypatch.setattr(window_manager.os.path, "isfile", lambda p: p == common_path)

    assert window_manager.resolve_browser_executable() == common_path


def test_resolve_browser_executable_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(window_manager, "resolve_browser_executable_path", lambda: None)
    monkeypatch.setattr(window_manager.os.path, "isfile", lambda p: False)

    assert window_manager.resolve_browser_executable() is None


def _windows_enum(handles_and_pids, visible=None, titled=None):
    """Builds a fake win32gui.EnumWindows(callback, extra) that calls
    callback(hwnd, extra) for each hwnd in handles_and_pids."""
    visible = visible or set(handles_and_pids)
    titled = titled or set(handles_and_pids)

    def fake_enum_windows(callback, extra):
        for hwnd in handles_and_pids:
            callback(hwnd, extra)

    return fake_enum_windows, visible, titled


def test_find_window_by_pid_returns_the_matching_visible_window(monkeypatch):
    fake_enum, visible, titled = _windows_enum([101, 102])
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: h in visible)
    monkeypatch.setattr(window_manager.win32gui, "GetWindowText", lambda h: "Title" if h in titled else "")
    monkeypatch.setattr(
        window_manager.win32process,
        "GetWindowThreadProcessId",
        lambda h: (0, 555 if h == 102 else 999),
    )

    hwnd = window_manager._find_window_by_pid(555, timeout_seconds=0.05, poll_interval=0.01)

    assert hwnd == 102


def test_find_window_by_pid_returns_none_when_never_found(monkeypatch):
    fake_enum, visible, titled = _windows_enum([101])
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: h in visible)
    monkeypatch.setattr(window_manager.win32gui, "GetWindowText", lambda h: "Title")
    monkeypatch.setattr(window_manager.win32process, "GetWindowThreadProcessId", lambda h: (0, 999))

    hwnd = window_manager._find_window_by_pid(555, timeout_seconds=0.05, poll_interval=0.01)

    assert hwnd is None


def test_find_window_by_pid_ignores_invisible_windows(monkeypatch):
    fake_enum, _, titled = _windows_enum([101], visible=set())
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: False)
    monkeypatch.setattr(window_manager.win32gui, "GetWindowText", lambda h: "Title")
    monkeypatch.setattr(window_manager.win32process, "GetWindowThreadProcessId", lambda h: (0, 555))

    hwnd = window_manager._find_window_by_pid(555, timeout_seconds=0.05, poll_interval=0.01)

    assert hwnd is None


def test_snapshot_browser_windows_returns_visible_chrome_widget_windows(monkeypatch):
    fake_enum, visible, _ = _windows_enum([201, 202], visible={201})
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: h in visible)
    monkeypatch.setattr(window_manager.win32gui, "GetClassName", lambda h: "Chrome_WidgetWin_1")

    assert window_manager._snapshot_browser_windows() == {201}


def test_find_new_browser_window_returns_a_window_not_in_the_before_set(monkeypatch):
    fake_enum, visible, _ = _windows_enum([201, 202])
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(window_manager.win32gui, "GetClassName", lambda h: "Chrome_WidgetWin_1")

    hwnd = window_manager._find_new_browser_window(
        before={201}, timeout_seconds=0.05, poll_interval=0.01
    )

    assert hwnd == 202


def test_find_new_browser_window_returns_none_when_nothing_new_appears(monkeypatch):
    fake_enum, _, _ = _windows_enum([201])
    monkeypatch.setattr(window_manager.win32gui, "EnumWindows", fake_enum)
    monkeypatch.setattr(window_manager.win32gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(window_manager.win32gui, "GetClassName", lambda h: "Chrome_WidgetWin_1")

    hwnd = window_manager._find_new_browser_window(
        before={201}, timeout_seconds=0.05, poll_interval=0.01
    )

    assert hwnd is None


def test_place_window_moves_window_to_the_requested_monitor(monkeypatch):
    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [
            {"left": 0, "top": 0, "right": 1920, "bottom": 1080},
            {"left": 1920, "top": 0, "right": 3840, "bottom": 1080},
        ],
    )
    moves = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "MoveWindow",
        lambda hwnd, x, y, w, h, repaint: moves.append((hwnd, x, y, w, h)),
    )

    result = window_manager._place_window(999, monitor_index=2)

    assert result is True
    assert moves == [(999, 1920, 0, 1920, 1080)]


def test_place_window_returns_false_and_logs_when_monitor_does_not_exist(monkeypatch, caplog):
    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [{"left": 0, "top": 0, "right": 1920, "bottom": 1080}],
    )
    moves = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "MoveWindow",
        lambda hwnd, x, y, w, h, repaint: moves.append((hwnd, x, y, w, h)),
    )

    with caplog.at_level("WARNING"):
        result = window_manager._place_window(999, monitor_index=2)

    assert result is False
    assert moves == []
    assert "monitor" in caplog.text.lower()


def test_require_http_url_accepts_http_and_https():
    window_manager.require_http_url("http://example.com")
    window_manager.require_http_url("https://example.com")


def test_require_http_url_rejects_other_schemes():
    with pytest.raises(ValueError):
        window_manager.require_http_url("C:/some/local/script.bat")


def test_open_placed_app_launches_without_shell_and_skips_placement_when_no_monitor(
    monkeypatch,
):
    popen_calls = []

    class _FakeProc:
        pid = 4242

    def fake_popen(argv):
        popen_calls.append(argv)
        return _FakeProc()

    monkeypatch.setattr(window_manager.subprocess, "Popen", fake_popen)

    window_manager.open_placed("app", "C:/apps/code.exe", new_window=False, monitor=None)

    assert popen_calls == [["C:/apps/code.exe"]]


def test_open_placed_folder_launches_via_explorer_argv(monkeypatch):
    popen_calls = []

    class _FakeProc:
        pid = 4242

    def fake_popen(argv):
        popen_calls.append(argv)
        return _FakeProc()

    monkeypatch.setattr(window_manager.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(window_manager, "_snapshot_windows_by_class", lambda class_name: set())
    monkeypatch.setattr(
        window_manager, "_find_new_window_by_class", lambda before, class_name, **kw: 999
    )

    window_manager.open_placed("folder", "E:/journal", new_window=False, monitor=None)

    assert popen_calls == [["explorer", "E:/journal"]]


def test_open_placed_app_with_monitor_places_the_discovered_window(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: 999)
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index)) or True,
    )

    window_manager.open_placed("app", "C:/apps/code.exe", new_window=False, monitor=2)

    assert placed == [(999, 2)]


def test_open_placed_app_with_monitor_skips_placement_when_window_not_found(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: None)
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index)) or True,
    )

    window_manager.open_placed("app", "C:/apps/code.exe", new_window=False, monitor=2)

    assert placed == []


def test_open_placed_url_without_new_window_uses_webbrowser_open(monkeypatch):
    opened = []
    monkeypatch.setattr(window_manager.webbrowser, "open", lambda url: opened.append(url) or True)

    window_manager.open_placed("url", "https://example.com", new_window=False, monitor=None)

    assert opened == ["https://example.com"]


def test_open_placed_url_without_new_window_raises_when_webbrowser_open_fails(monkeypatch):
    monkeypatch.setattr(window_manager.webbrowser, "open", lambda url: False)

    with pytest.raises(OSError):
        window_manager.open_placed("url", "https://example.com", new_window=False, monitor=None)


def test_open_placed_url_with_new_window_launches_the_resolved_browser(monkeypatch):
    monkeypatch.setattr(window_manager, "resolve_browser_executable", lambda: "C:/Brave/brave.exe")
    monkeypatch.setattr(window_manager, "_snapshot_browser_windows", lambda: set())
    monkeypatch.setattr(window_manager, "_find_new_browser_window", lambda before, **kw: 999)
    popen_calls = []
    monkeypatch.setattr(
        window_manager.subprocess, "Popen", lambda argv: popen_calls.append(argv)
    )

    window_manager.open_placed("url", "https://example.com", new_window=True, monitor=None)

    assert popen_calls == [["C:/Brave/brave.exe", "--new-window", "https://example.com"]]


def test_open_placed_url_with_monitor_forces_new_window_and_places_it(monkeypatch):
    monkeypatch.setattr(window_manager, "resolve_browser_executable", lambda: "C:/Brave/brave.exe")
    monkeypatch.setattr(window_manager, "_snapshot_browser_windows", lambda: {1})
    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: None)
    monkeypatch.setattr(window_manager, "_find_new_browser_window", lambda before, **kw: 2)
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index)) or True,
    )

    # new_window left False on purpose — a monitor request must force it.
    window_manager.open_placed("url", "https://example.com", new_window=False, monitor=1)

    assert placed == [(2, 1)]


def test_open_placed_url_falls_back_to_webbrowser_when_no_browser_resolved(monkeypatch):
    monkeypatch.setattr(window_manager, "resolve_browser_executable", lambda: None)
    opened = []
    monkeypatch.setattr(window_manager.webbrowser, "open", lambda url: opened.append(url) or True)

    window_manager.open_placed("url", "https://example.com", new_window=True, monitor=None)

    assert opened == ["https://example.com"]


def test_open_placed_url_validates_scheme_before_launching(monkeypatch):
    popen_calls = []
    monkeypatch.setattr(
        window_manager.subprocess, "Popen", lambda argv: popen_calls.append(argv)
    )

    with pytest.raises(ValueError):
        window_manager.open_placed("url", "C:/evil.bat", new_window=True, monitor=None)

    assert popen_calls == []


class _FakeWindll:
    def __init__(self, shcore=None, user32=None):
        self.shcore = shcore
        self.user32 = user32


def test_make_dpi_aware_uses_per_monitor_awareness_when_available(monkeypatch):
    shcore_calls = []
    user32_calls = []

    class _Shcore:
        def SetProcessDpiAwareness(self, value):
            shcore_calls.append(value)

    class _User32:
        def SetProcessDPIAware(self):
            user32_calls.append(True)

    monkeypatch.setattr(
        window_manager.ctypes, "windll", _FakeWindll(shcore=_Shcore(), user32=_User32())
    )

    window_manager._make_dpi_aware()

    assert shcore_calls == [2]
    assert user32_calls == []


def test_make_dpi_aware_falls_back_to_user32_when_shcore_unavailable(monkeypatch):
    user32_calls = []

    class _User32:
        def SetProcessDPIAware(self):
            user32_calls.append(True)

    monkeypatch.setattr(window_manager.ctypes, "windll", _FakeWindll(shcore=None, user32=_User32()))

    window_manager._make_dpi_aware()

    assert user32_calls == [True]


def test_make_dpi_aware_swallows_errors_when_nothing_works(monkeypatch):
    monkeypatch.setattr(window_manager.ctypes, "windll", _FakeWindll(shcore=None, user32=None))

    window_manager._make_dpi_aware()  # must not raise


def test_place_window_fills_the_monitor_when_no_size_given(monkeypatch):
    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [{"left": 0, "top": 0, "right": 1920, "bottom": 1080}],
    )
    moves = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "MoveWindow",
        lambda hwnd, x, y, w, h, repaint: moves.append((hwnd, x, y, w, h)),
    )

    window_manager._place_window(999, monitor_index=1)

    assert moves == [(999, 0, 0, 1920, 1080)]


def test_place_window_resizes_by_fraction_of_the_monitor(monkeypatch):
    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [{"left": 0, "top": 0, "right": 1920, "bottom": 1080}],
    )
    moves = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "MoveWindow",
        lambda hwnd, x, y, w, h, repaint: moves.append((hwnd, x, y, w, h)),
    )

    window_manager._place_window(999, monitor_index=1, size=(0.5, 1.0))

    assert moves == [(999, 0, 0, 960, 1080)]


def test_place_window_resizes_by_exact_pixels(monkeypatch):
    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [{"left": 0, "top": 0, "right": 1920, "bottom": 1080}],
    )
    moves = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "MoveWindow",
        lambda hwnd, x, y, w, h, repaint: moves.append((hwnd, x, y, w, h)),
    )

    window_manager._place_window(999, monitor_index=1, size=(800, 600))

    assert moves == [(999, 0, 0, 800, 600)]


def test_open_placed_app_maximizes_without_a_monitor(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: 999)
    maximize_calls = []
    monkeypatch.setattr(
        window_manager.win32gui,
        "ShowWindow",
        lambda hwnd, flag: maximize_calls.append((hwnd, flag)),
    )

    window_manager.open_placed(
        "app", "C:/apps/code.exe", new_window=False, monitor=None, maximized=True
    )

    assert maximize_calls == [(999, window_manager.win32con.SW_MAXIMIZE)]


def test_open_placed_app_places_then_maximizes_when_both_requested(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: 999)
    calls = []
    monkeypatch.setattr(
        window_manager, "_place_window", lambda hwnd, monitor_index, size=None, position=None: calls.append(
            ("place", hwnd, monitor_index, size)
        ) or True,
    )
    monkeypatch.setattr(
        window_manager.win32gui,
        "ShowWindow",
        lambda hwnd, flag: calls.append(("maximize", hwnd, flag)),
    )

    window_manager.open_placed(
        "app", "C:/apps/code.exe", new_window=False, monitor=2, maximized=True
    )

    # Placement must happen before maximizing, so the maximize takes
    # effect on the monitor the window was just moved to.
    assert calls == [
        ("place", 999, 2, None),
        ("maximize", 999, window_manager.win32con.SW_MAXIMIZE),
    ]


def test_open_placed_app_with_size_and_monitor_passes_size_through(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: 999)
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index, size)) or True,
    )

    window_manager.open_placed(
        "app", "C:/apps/code.exe", new_window=False, monitor=1, size=(0.5, 0.5)
    )

    assert placed == [(999, 1, (0.5, 0.5))]


def test_open_placed_app_skips_size_and_logs_when_no_monitor_given(monkeypatch, caplog):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    monkeypatch.setattr(window_manager, "_find_window_by_pid", lambda pid, **kw: 999)
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index, size)) or True,
    )

    with caplog.at_level("WARNING"):
        window_manager.open_placed(
            "app", "C:/apps/code.exe", new_window=False, monitor=None, size=(0.5, 0.5)
        )

    assert placed == []
    assert "size" in caplog.text.lower()


def test_open_placed_url_waits_for_its_own_window_even_without_placement(monkeypatch):
    # Without this wait, a later step's "before" snapshot could still miss
    # this step's window, letting a later step's discovery steal it.
    monkeypatch.setattr(window_manager, "resolve_browser_executable", lambda: "C:/Brave/brave.exe")
    monkeypatch.setattr(window_manager, "_snapshot_browser_windows", lambda: set())
    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: None)
    find_calls = []
    monkeypatch.setattr(
        window_manager,
        "_find_new_browser_window",
        lambda before, **kw: find_calls.append(before) or 999,
    )

    window_manager.open_placed("url", "https://example.com", new_window=True, monitor=None)

    assert find_calls == [set()]


def test_open_placed_folder_with_monitor_uses_class_based_window_discovery(monkeypatch):
    class _FakeProc:
        pid = 4242

    monkeypatch.setattr(window_manager.subprocess, "Popen", lambda argv: _FakeProc())
    snapshot_calls = []
    monkeypatch.setattr(
        window_manager,
        "_snapshot_windows_by_class",
        lambda class_name: snapshot_calls.append(class_name) or set(),
    )
    find_calls = []
    monkeypatch.setattr(
        window_manager,
        "_find_new_window_by_class",
        lambda before, class_name, **kw: find_calls.append((before, class_name)) or 999,
    )
    placed = []
    monkeypatch.setattr(
        window_manager,
        "_place_window",
        lambda hwnd, monitor_index, size=None, position=None: placed.append((hwnd, monitor_index)) or True,
    )

    window_manager.open_placed("folder", "E:/journal", new_window=False, monitor=1)

    assert snapshot_calls == ["CabinetWClass"]
    assert find_calls == [(set(), "CabinetWClass")]
    assert placed == [(999, 1)]


def test_open_placed_folder_without_monitor_still_launches_via_explorer(monkeypatch):
    popen_calls = []
    monkeypatch.setattr(
        window_manager.subprocess, "Popen", lambda argv: popen_calls.append(argv) or Mock(pid=1)
    )
    monkeypatch.setattr(window_manager, "_snapshot_windows_by_class", lambda class_name: set())
    monkeypatch.setattr(
        window_manager, "_find_new_window_by_class", lambda before, class_name, **kw: 999
    )

    window_manager.open_placed("folder", "E:/journal", new_window=False, monitor=None)

    assert popen_calls == [["explorer", "E:/journal"]]


def test_open_placed_app_falls_back_to_startfile_when_popen_cannot_launch(monkeypatch, caplog):
    def failing_popen(argv):
        raise OSError("not a valid Win32 application")

    monkeypatch.setattr(window_manager.subprocess, "Popen", failing_popen)
    startfile_calls = []
    monkeypatch.setattr(window_manager.os, "startfile", lambda value: startfile_calls.append(value))

    with caplog.at_level("WARNING"):
        window_manager.open_placed(
            "app", "C:/Users/me/Desktop/App.lnk", new_window=False, monitor=1
        )

    assert startfile_calls == ["C:/Users/me/Desktop/App.lnk"]


def test_open_placed_rejects_unknown_kind():
    with pytest.raises(ValueError):
        window_manager.open_placed("bogus", "some-value", new_window=False, monitor=1)


def test_place_window_returns_false_and_logs_when_move_window_raises(monkeypatch, caplog):
    import pywintypes

    monkeypatch.setattr(
        window_manager,
        "list_monitors",
        lambda: [{"left": 0, "top": 0, "right": 1920, "bottom": 1080}],
    )

    def failing_move_window(hwnd, x, y, w, h, repaint):
        raise pywintypes.error(1400, "MoveWindow", "Invalid window handle.")

    monkeypatch.setattr(window_manager.win32gui, "MoveWindow", failing_move_window)

    with caplog.at_level("WARNING"):
        result = window_manager._place_window(999, monitor_index=1)

    assert result is False


def test_apply_window_adjustments_swallows_show_window_errors_when_maximizing(monkeypatch):
    import pywintypes

    def failing_show_window(hwnd, flag):
        raise pywintypes.error(1400, "ShowWindow", "Invalid window handle.")

    monkeypatch.setattr(window_manager.win32gui, "ShowWindow", failing_show_window)

    # Must not raise.
    window_manager._apply_window_adjustments(999, monitor=None, maximized=True, size=None)


def test_open_placed_raises_oserror_when_automation_not_supported(monkeypatch):
    monkeypatch.setattr(window_manager, "AUTOMATION_SUPPORTED", False)

    with pytest.raises(OSError, match="not available on this platform"):
        window_manager.open_placed("app", "C:/apps/code.exe", new_window=False, monitor=1)


def test_open_placed_raises_before_launching_anything_when_not_supported(monkeypatch):
    monkeypatch.setattr(window_manager, "AUTOMATION_SUPPORTED", False)
    popen_calls = []
    monkeypatch.setattr(
        window_manager.subprocess, "Popen", lambda argv: popen_calls.append(argv)
    )

    with pytest.raises(OSError):
        window_manager.open_placed("url", "https://example.com", new_window=True, monitor=1)

    assert popen_calls == []
