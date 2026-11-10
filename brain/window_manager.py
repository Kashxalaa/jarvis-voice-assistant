import logging
import os
import subprocess
import sys
import time
import webbrowser

logger = logging.getLogger(__name__)

AUTOMATION_SUPPORTED = sys.platform == "win32"

if AUTOMATION_SUPPORTED:
    import ctypes

    import pywintypes
    import win32api
    import win32con
    import win32gui
    import win32process

    from config import resolve_browser_executable_path

    _COMMON_BRAVE_PATHS = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]

    _DISPLAY_DEVICE_ACTIVE = 0x00000001

    # Windows' MoveWindow/EnumDisplayMonitors coordinates are DPI-virtualized
    # for a process that hasn't declared its own DPI awareness, which silently
    # scrambles monitor geometry and window placement on any multi-monitor
    # setup with per-monitor DPI scaling. Must run before any monitor/window
    # API call below, so it runs once at import time.
    def _make_dpi_aware() -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass


if AUTOMATION_SUPPORTED:
    _make_dpi_aware()


def resolve_browser_executable() -> str | None:
    override = resolve_browser_executable_path()
    if override:
        return override if os.path.isfile(override) else None
    for path in _COMMON_BRAVE_PATHS:
        if os.path.isfile(path):
            return path
    return None


def _active_display_device_order() -> dict[str, int]:
    order = {}
    i = 0
    while True:
        try:
            dev = win32api.EnumDisplayDevices(None, i)
        except Exception:
            break
        if dev.StateFlags & _DISPLAY_DEVICE_ACTIVE:
            order[dev.DeviceName] = len(order)
        i += 1
    return order


def list_monitors() -> list[dict]:
    device_order = _active_display_device_order()
    monitors = []
    for hmonitor, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(hmonitor)
        left, top, right, bottom = info["Work"]
        monitors.append(
            {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "_order": device_order.get(info["Device"], len(device_order)),
            }
        )
    monitors.sort(key=lambda m: m["_order"])
    for m in monitors:
        del m["_order"]
    return monitors


def _find_window_by_pid(
    pid: int, timeout_seconds: float = 3.0, poll_interval: float = 0.3
) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        found = _first_visible_titled_window_for_pid(pid)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def _first_visible_titled_window_for_pid(pid: int) -> int | None:
    matches = []

    def callback(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid:
            matches.append(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return matches[0] if matches else None


def _snapshot_windows_by_class(class_name: str) -> set[int]:
    matches = set()

    def callback(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == class_name:
            matches.add(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return matches


def _find_new_window_by_class(
    before: set[int],
    class_name: str,
    timeout_seconds: float = 3.0,
    poll_interval: float = 0.3,
) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        new_windows = _snapshot_windows_by_class(class_name) - before
        if new_windows:
            return next(iter(new_windows))
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def _snapshot_browser_windows() -> set[int]:
    return _snapshot_windows_by_class("Chrome_WidgetWin_1")


def _find_new_browser_window(
    before: set[int], timeout_seconds: float = 3.0, poll_interval: float = 0.3
) -> int | None:
    return _find_new_window_by_class(before, "Chrome_WidgetWin_1", timeout_seconds, poll_interval)


# Where on its monitor a window goes. Each entry: default size (fraction of the
# monitor) when no size is given, and the horizontal / vertical alignment.
POSITIONS = {
    "left": ((0.5, 1.0), "left", "top"),
    "right": ((0.5, 1.0), "right", "top"),
    "top": ((1.0, 0.5), "left", "top"),
    "bottom": ((1.0, 0.5), "left", "bottom"),
    "top-left": ((0.5, 0.5), "left", "top"),
    "top-right": ((0.5, 0.5), "right", "top"),
    "bottom-left": ((0.5, 0.5), "left", "bottom"),
    "bottom-right": ((0.5, 0.5), "right", "bottom"),
    "center": ((1.0, 1.0), "center", "center"),
}


def _place_window(
    hwnd: int, monitor_index: int, size: tuple[float, float] | None = None, position: str | None = None,
) -> bool:
    monitors = list_monitors()
    if monitor_index < 1 or monitor_index > len(monitors):
        logger.warning(
            "monitor %s does not exist (found %d monitor(s)); skipping placement",
            monitor_index,
            len(monitors),
        )
        return False
    if position is not None and position not in POSITIONS:
        logger.warning("unknown window position %r; skipping placement", position)
        return False
    target = monitors[monitor_index - 1]
    mon_width = target["right"] - target["left"]
    mon_height = target["bottom"] - target["top"]
    default_size, horizontal, vertical = POSITIONS.get(position, ((1.0, 1.0), "left", "top"))
    width_spec, height_spec = size if size is not None else default_size
    width = int(width_spec * mon_width) if 0 < width_spec <= 1 else int(width_spec)
    height = int(height_spec * mon_height) if 0 < height_spec <= 1 else int(height_spec)
    x = {"left": target["left"], "right": target["right"] - width,
         "center": target["left"] + (mon_width - width) // 2}[horizontal]
    y = {"top": target["top"], "bottom": target["bottom"] - height,
         "center": target["top"] + (mon_height - height) // 2}[vertical]
    try:
        win32gui.MoveWindow(hwnd, x, y, width, height, True)
    except pywintypes.error as exc:
        logger.warning("failed to move window to monitor %s: %s; skipping placement", monitor_index, exc)
        return False
    return True


def require_http_url(value: str) -> None:
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError(f"url action must start with http:// or https://: {value!r}")


def _bring_to_front(hwnd: int) -> None:
    """Raise the window above others. Windows refuses SetForegroundWindow to a
    background process, but a TOPMOST/NOTOPMOST toggle still raises it."""
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
    except pywintypes.error as exc:
        logger.warning("failed to raise window: %s", exc)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except pywintypes.error:
        pass  # focus refused - the window is still on top


def _apply_window_adjustments(
    hwnd: int, monitor: int | None, maximized: bool, size: tuple[float, float] | None,
    position: str | None = None,
) -> None:
    if monitor is not None:
        _place_window(hwnd, monitor, size=size, position=position)
    elif size is not None or position is not None:
        logger.warning("size/position requested without a monitor; skipping it")
    if maximized:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        except pywintypes.error as exc:
            logger.warning("failed to maximize window: %s", exc)
    _bring_to_front(hwnd)


def open_placed(
    kind: str,
    value: str,
    new_window: bool,
    monitor: int | None,
    maximized: bool = False,
    size: tuple[float, float] | None = None,
    position: str | None = None,
) -> None:
    if not AUTOMATION_SUPPORTED:
        raise OSError("PC automation is not available on this platform")
    if kind not in ("app", "folder", "url"):
        raise ValueError(f"unknown action kind: {kind}")
    if kind == "url":
        if monitor is not None or maximized or size is not None or position is not None:
            new_window = True
        _open_url_placed(value, new_window, monitor, maximized, size, position)
    else:
        _open_app_placed(kind, value, monitor, maximized, size, position)


def _open_url_placed(
    value: str,
    new_window: bool,
    monitor: int | None,
    maximized: bool = False,
    size: tuple[float, float] | None = None,
    position: str | None = None,
) -> None:
    require_http_url(value)
    if not new_window:
        if not webbrowser.open(value):
            raise OSError(f"failed to open url: {value}")
        return

    browser_path = resolve_browser_executable()
    if browser_path is None:
        logger.warning(
            "no browser executable resolved; opening %s in the default handler instead", value
        )
        if not webbrowser.open(value):
            raise OSError(f"failed to open url: {value}")
        return

    # Chromium browsers are single-instance: launching with an already-
    # running Brave process just forwards the request and the new window
    # belongs to that existing process, not a fresh PID we could track. We
    # always wait for it here — even when no placement was requested for
    # THIS step — so a later step's "before" snapshot can't miss it and
    # mistakenly attribute this window to itself.
    before = _snapshot_browser_windows()
    subprocess.Popen([browser_path, "--new-window", value])
    hwnd = _find_new_browser_window(before)
    if hwnd is None:
        logger.warning("could not find newly opened browser window for %s", value)
        return
    if monitor is not None or maximized or size is not None or position is not None:
        _apply_window_adjustments(hwnd, monitor, maximized, size, position)


def _open_app_placed(
    kind: str,
    value: str,
    monitor: int | None,
    maximized: bool = False,
    size: tuple[float, float] | None = None,
    position: str | None = None,
) -> None:
    if kind == "folder":
        # explorer.exe is a singleton like a browser: the spawned process
        # just forwards to it and exits, so PID-matching can't work — use
        # the same before/after class-based discovery used for browsers.
        before = _snapshot_windows_by_class("CabinetWClass")
        subprocess.Popen(["explorer", value])
        hwnd = _find_new_window_by_class(before, "CabinetWClass")
        if hwnd is None:
            logger.warning("could not find newly opened folder window for %s", value)
            return
        if monitor is not None or maximized or size is not None or position is not None:
            _apply_window_adjustments(hwnd, monitor, maximized, size, position)
        return

    try:
        proc = subprocess.Popen([value])
    except OSError as exc:
        # A shortcut (.lnk), a document, or anything else that relies on a
        # file association can't be launched directly by CreateProcess —
        # only ShellExecute (os.startfile) understands those. The open
        # still has to succeed even though placement can't: fall back and
        # skip placement rather than losing the open entirely.
        logger.warning(
            "could not launch %s directly (%s); falling back to the default "
            "handler and skipping placement",
            value,
            exc,
        )
        os.startfile(value)
        return

    if monitor is not None or maximized or size is not None or position is not None:
        hwnd = _find_window_by_pid(proc.pid)
        if hwnd is None:
            logger.warning(
                "could not find newly opened window for pid %s; skipping placement", proc.pid
            )
            return
        _apply_window_adjustments(hwnd, monitor, maximized, size, position)


# ------------------------------------------- window tools for Claude's computer mode
# Each returns a short line for Claude ("ERROR ..." on failure) instead of raising.

_FILE_WINDOW_TIMEOUT_S = 8.0


def _visible_titled_windows() -> dict[int, str]:
    found: dict[int, str] = {}

    def callback(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and title not in ("Program Manager",):
                found[hwnd] = title
        return True

    win32gui.EnumWindows(callback, None)
    return found


def _adjust_window(hwnd: int, monitor: int | None, maximized: bool,
                   size: tuple[float, float] | None, position: str | None = None) -> bool:
    """Restore (a maximized/minimized window won't move), place, then maximize."""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except pywintypes.error as exc:
        logger.warning("failed to restore window: %s", exc)
    ok = True
    if monitor is not None:
        ok = _place_window(hwnd, monitor, size=size, position=position)
    if maximized:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        except pywintypes.error as exc:
            logger.warning("failed to maximize window: %s", exc)
            ok = False
    _bring_to_front(hwnd)
    return ok


def _monitor_error(monitor: int | None) -> str | None:
    if monitor is None:
        return None
    count = len(list_monitors())
    if not isinstance(monitor, int) or isinstance(monitor, bool) or not 1 <= monitor <= count:
        return f"ERROR monitor {monitor} doesn't exist — there are {count} monitors (1 is the primary)"
    return None


def _position_error(position: str | None) -> str | None:
    if position is None or position in POSITIONS:
        return None
    return f"ERROR unknown position {position!r} - use one of: {', '.join(POSITIONS)}"


def _where(monitor: int | None, maximized: bool, position: str | None = None) -> str:
    parts = []
    if monitor is not None:
        parts.append(f"on monitor {monitor}" + (f" ({position})" if position else ""))
    if maximized:
        parts.append("maximized")
    return " ".join(parts) or "where Windows put it"


def open_urls_on_screen(urls: list[str], monitor: int | None, maximized: bool,
                        size: tuple[float, float] | None, position: str | None = None) -> str:
    """Open the urls as tabs of ONE new browser window, then place that window."""
    if not urls:
        return "ERROR no urls given"
    for url in urls:
        try:
            require_http_url(url)
        except ValueError:
            return f"ERROR only http(s) web addresses can be opened: {url!r}"
    err = _monitor_error(monitor) or _position_error(position)
    if err:
        return err
    browser = resolve_browser_executable()
    if browser is None:
        for url in urls:
            webbrowser.open(url)
        return (f"Opened {len(urls)} tabs in the default browser, but couldn't place the window "
                "(no Brave/Chrome-style browser found — set BROWSER_EXECUTABLE_PATH).")
    before = _snapshot_browser_windows()
    subprocess.Popen([browser, "--new-window", *urls])
    hwnd = _find_new_browser_window(before, timeout_seconds=5.0)
    if hwnd is None:
        return f"Opened {len(urls)} tabs, but couldn't place the window (it didn't show up in time)."
    if monitor is None and not maximized and position is None:
        return f"Opened {len(urls)} tabs in a new browser window."
    placed = _adjust_window(hwnd, monitor, maximized, size, position)
    where = _where(monitor, maximized, position)
    return f"Opened {len(urls)} tabs in a new browser window {where}." if placed else \
        f"Opened {len(urls)} tabs, but couldn't place the window {where}."


def open_path_on_screen(path: str, monitor: int | None, maximized: bool,
                        size: tuple[float, float] | None, position: str | None = None) -> str:
    """Open a folder (File Explorer) or a file (its default app), then place its window."""
    if not os.path.exists(path):
        return f"ERROR {path}: not found"
    err = _monitor_error(monitor) or _position_error(position)
    if err:
        return err
    name = os.path.basename(path.rstrip("\/")) or path
    if os.path.isdir(path):
        before = _snapshot_windows_by_class("CabinetWClass")
        subprocess.Popen(["explorer", path])
        hwnd = _find_new_window_by_class(before, "CabinetWClass", timeout_seconds=5.0)
    else:
        before_windows = _visible_titled_windows()
        os.startfile(path)
        hwnd = None
        deadline = time.monotonic() + _FILE_WINDOW_TIMEOUT_S
        while hwnd is None:
            new = {h: t for h, t in _visible_titled_windows().items() if h not in before_windows}
            # Prefer the window that shows the file's name (the app may open helpers too).
            hwnd = next((h for h, t in new.items() if name.lower() in t.lower()), None) \
                or next(iter(new), None)
            if hwnd is None:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.3)
    if hwnd is None:
        return f"Opened {name}, but couldn't place its window (it didn't show up in time)."
    if monitor is None and not maximized and position is None:
        return f"Opened {name}."
    where = _where(monitor, maximized, position)
    if _adjust_window(hwnd, monitor, maximized, size, position):
        return f"Opened {name} {where}."
    return f"Opened {name}, but couldn't place its window {where}."


def move_window(title: str, monitor: int | None, maximized: bool,
                size: tuple[float, float] | None, position: str | None = None) -> str:
    """Move an already-open window, found by (part of) its title."""
    if not isinstance(title, str) or not title.strip():
        return "ERROR give part of the window's title"
    err = _monitor_error(monitor) or _position_error(position)
    if err:
        return err
    if monitor is None and not maximized and position is None:
        return "ERROR say which monitor to move it to (or maximized)"
    wanted = title.strip().lower()
    windows = _visible_titled_windows()
    exact = [h for h, t in windows.items() if t.lower() == wanted]
    matches = exact or [h for h, t in windows.items() if wanted in t.lower()]
    if not matches:
        return f"ERROR no open window has {title!r} in its title (use list_windows)"
    if len(matches) > 1:
        names = "; ".join(windows[h] for h in matches[:8])
        return f"ERROR several windows match {title!r}: {names}. Use more of the title."
    hwnd = matches[0]
    where = _where(monitor, maximized, position)
    if _adjust_window(hwnd, monitor, maximized, size, position):
        return f"Moved '{windows[hwnd]}' {where}."
    return f"ERROR couldn't move '{windows[hwnd]}' {where}."


def _monitor_of(hwnd: int, monitors: list[dict]) -> int | None:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except pywintypes.error:
        return None
    cx, cy = (left + right) // 2, (top + bottom) // 2
    for i, m in enumerate(monitors, start=1):
        if m["left"] <= cx < m["right"] and m["top"] <= cy < m["bottom"]:
            return i
    return None


def list_windows() -> str:
    monitors = list_monitors()
    lines = [f"{len(monitors)} monitors (1 is the primary). Open windows:"]
    for hwnd, title in _visible_titled_windows().items():
        mon = _monitor_of(hwnd, monitors)
        lines.append(f"- {title} (monitor {mon})" if mon else f"- {title} (minimized or off-screen)")
    return "\n".join(lines)
