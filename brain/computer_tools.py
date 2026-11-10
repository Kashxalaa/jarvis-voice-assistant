"""Custom tools for Claude's computer mode (spec A2, "Claude's toolkit").

The functions here are plain, synchronous and individually testable; the SDK
tool objects are built by build_computer_tools(). Every function also refuses
Windows device-namespace paths on its own (defence in depth: the guard decides
first, but a tool must never trust that).
"""
import asyncio
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import threading
from dataclasses import dataclass, field

from claude_agent_sdk import SdkMcpTool, tool

from brain import window_manager
from brain.guard import GuardContext, normalize_path

logger = logging.getLogger(__name__)

INSTALL_TIMEOUT_SECONDS = 15 * 60
INSTALL_OUTPUT_TAIL = 1500
LONG_OUTPUT_CHARS = 1500

_URI_SCHEME = re.compile(r"(?i)^[a-z][a-z0-9+.-]+:")
_PKG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+\-]*(?:\[[A-Za-z0-9._,\-]+\])?(?:==[A-Za-z0-9._+\-]+)?$"
)
_NPM_SCOPED_RE = re.compile(r"^@[a-z0-9][a-z0-9._\-]*/[a-z0-9][a-z0-9._\-]*$")
_PLAIN_DRIVE_DEVICE = re.compile(r"^\\\\\?\\[A-Za-z]:\\")


@dataclass
class ComputerTaskState:
    task_id: str
    tainted: bool = False
    self_tainted: bool = False   # this task read untrusted content itself (not just inherited)
    steps: int = 0
    files_changed: set[str] = field(default_factory=set)
    diffs: list[str] = field(default_factory=list)       # unified diffs of text edits
    outputs: list[str] = field(default_factory=list)      # long tool outputs to attach
    stopped: bool = False                                 # set by the Stop button
    diff_bytes: int = 0
    texts: list[str] = field(default_factory=list)        # Claude's own text so far
    limit_hit: str | None = None                          # "steps" | "time" once a limit stopped the task
    _cancels: list = field(default_factory=list)          # in-flight cross-loop futures (approval cards)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def stop(self) -> None:
        """The Stop button: deny every further step and cancel a pending approval.
        Threads already running (install, copy) finish on their own."""
        self.stopped = True
        with self._lock:
            pending = list(self._cancels)
        for fut in pending:
            fut.cancel()

    def track(self, fut) -> None:
        with self._lock:
            self._cancels.append(fut)

    def untrack(self, fut) -> None:
        with self._lock:
            if fut in self._cancels:
                self._cancels.remove(fut)


# ----------------------------------------------------------------------- paths

def _clean(raw: str) -> str:
    return "".join(c for c in raw if unicodedata.category(c) != "Cf").strip()


def device_path_error(raw: str) -> str | None:
    """Refuse \\\\?\\, \\\\.\\ and \\??\\ paths in any slash style, except a plain \\\\?\\X:\\ without '..'."""
    p = _clean(raw).strip("\"'").strip().replace("/", "\\")
    if not p.startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        return None
    if _PLAIN_DRIVE_DEVICE.match(p) and ".." not in p.split("\\"):
        return None
    return f"{raw}: device-namespace paths are not allowed"


def resolve_tool_path(raw: str, cwd: str | None) -> str:
    """The absolute path a custom tool operates on: a leading ~ is expanded,
    relative paths resolve against cwd, and NO environment variables are expanded."""
    p = _clean(raw)
    if p == "~" or p.startswith(("~/", "~\\")):
        p = os.path.expanduser(p)
    if cwd and not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.abspath(p)


def _checked(raw, cwd: str | None) -> tuple[str | None, str | None]:
    """(absolute path, None) or (None, error line)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, f"{raw!r}: not a valid path"
    err = device_path_error(raw)
    if err:
        return None, err
    return resolve_tool_path(raw, cwd), None


def _placement(args: dict) -> tuple[int | None, bool, tuple[float, float] | None, str | None]:
    """(monitor, maximized, size, error) from a window tool's arguments."""
    monitor, maximized, size = args.get("monitor"), args.get("maximized", False), args.get("size")
    if monitor is not None and (isinstance(monitor, bool) or not isinstance(monitor, int)):
        return None, False, None, "ERROR monitor must be a number (1 is the primary)"
    if not isinstance(maximized, bool):
        return None, False, None, "ERROR maximized must be true or false"
    if size is not None:
        if (not isinstance(size, list) or len(size) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in size)):
            return None, False, None, "ERROR size must be [width, height] (fractions like 0.5 or pixels)"
        size = (float(size[0]), float(size[1]))
    return monitor, maximized, size, None


# ---------------------------------------------------------------------- tools

def _send2trash(path: str) -> None:
    """Imported lazily so a missing package can never stop the bot from starting."""
    from send2trash import send2trash

    send2trash(path)


TRASH_MAX_BYTES = 2 * 1024 ** 3
TRASH_SIZE_BUDGET_S = 5.0
_DRIVE_FIXED = 3
_NO_BIN = "can't use the Recycle Bin there"
_TOO_BIG = "too big for the Recycle Bin"


def _default_os() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def _drive_type(root: str) -> int:
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _same_device(path: str, home: str) -> bool:
    return os.stat(path).st_dev == os.stat(home).st_dev


def _tree_size(path: str, deadline: float) -> int | None:
    """Total bytes under path (symlinks not followed); None if the time budget runs out."""
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None
        cur = stack.pop()
        try:
            if os.path.islink(cur) or not os.path.isdir(cur):
                total += os.lstat(cur).st_size
                continue
            with os.scandir(cur) as it:
                for entry in it:
                    if time.monotonic() > deadline:
                        return None
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
        except OSError:
            continue
    return total


def _trash_refusal(path: str, os_name: str, home: str | None, total: int, deadline: float):
    """(error or None, new running total). Refuse anything the Recycle Bin can't hold,
    because send2trash would otherwise delete it permanently."""
    if os_name == "windows":
        if path.startswith("\\\\"):
            return _NO_BIN + " (network path)", total
        root = os.path.splitdrive(path)[0] + "\\"
        try:
            if _drive_type(root) != _DRIVE_FIXED:
                return _NO_BIN + " (not a fixed drive)", total
        except Exception:  # noqa: BLE001
            return _NO_BIN + " (could not check the drive)", total
    else:
        try:
            if home and not _same_device(path, home):
                return _NO_BIN + " (not on the home mount)", total
        except OSError:
            return _NO_BIN + " (could not check the mount)", total
    size = _tree_size(path, deadline)
    if size is None or total + size > TRASH_MAX_BYTES:
        return _TOO_BIG, total
    return None, total + size


def move_to_trash(paths: list[str], *, cwd: str | None = None, os_name: str | None = None) -> str:
    os_name = os_name or _default_os()
    lines = []
    total = 0
    deadline = time.monotonic() + TRASH_SIZE_BUDGET_S
    for raw in paths or []:
        path, err = _checked(raw, cwd)
        if err:
            lines.append(f"ERROR {err}")
            continue
        if not os.path.lexists(path):
            lines.append(f"ERROR {raw}: not found")
            continue
        err, total = _trash_refusal(path, os_name, cwd, total, deadline)
        if err:
            lines.append(f"ERROR {raw}: {err}; nothing was deleted")
            continue
        try:
            _send2trash(path)
            lines.append(f"Moved to the trash: {path}")
        except Exception as exc:  # noqa: BLE001 - reported per path; never fall back to a permanent delete
            lines.append(f"ERROR {raw}: could not move to the trash ({exc}); nothing was deleted")
    return "\n".join(lines) or "Nothing to do."


def make_folder(path: str, *, cwd: str | None = None) -> str:
    target, err = _checked(path, cwd)
    if err:
        return f"ERROR {err}"
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        return f"ERROR {path}: {exc}"
    return f"Folder ready: {target}"


def _pairs(items, src_key: str) -> list[tuple[object, object]]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append((item.get(src_key), item.get("to")))
        else:
            out.append((None, None))
    return out


def _is_reparse(path: str) -> bool:
    """Junction, symlink or any other reparse point (never followed by copy_paths)."""
    try:
        if os.path.islink(path):
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _copy_file_exclusive(src: str, dst: str) -> None:
    """Copy with exclusive creation: raises FileExistsError instead of overwriting."""
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        shutil.copyfileobj(fin, fout)
    shutil.copystat(src, dst)


def _move_one(src: str, dst: str) -> None:
    """Move without overwriting. Files: an atomic no-replace rename on the same volume
    (os.rename fails on Windows if dst exists; on POSIX os.link fails if it exists),
    else copy with 'xb' then unlink. Folders: re-check right before shutil.move;
    a residual race remains for folders on POSIX (rename onto an empty folder)."""
    if os.path.isfile(src) and not os.path.islink(src):
        try:
            if os.name == "nt":
                os.rename(src, dst)
            else:
                os.link(src, dst)
                os.unlink(src)
            return
        except FileExistsError:
            raise
        except OSError:
            if os.path.lexists(dst):
                raise FileExistsError(dst)
            _copy_file_exclusive(src, dst)  # different volume
            os.unlink(src)
            return
    if os.path.lexists(dst):
        raise FileExistsError(dst)
    shutil.move(src, dst)


def move_paths(moves: list[dict], *, cwd: str | None = None) -> str:
    lines = []
    for raw_src, raw_dst in _pairs(moves, "from"):
        src, err = _checked(raw_src, cwd)
        dst, err2 = _checked(raw_dst, cwd)
        if err or err2:
            lines.append(f"ERROR {err or err2}")
        elif not os.path.lexists(src):
            lines.append(f"ERROR {raw_src}: not found")
        elif os.path.lexists(dst):
            lines.append(f"ERROR {raw_dst}: already exists - not overwritten")
        else:
            try:
                _move_one(src, dst)
                lines.append(f"Moved {src} -> {dst}")
            except FileExistsError:
                lines.append(f"ERROR {raw_dst}: already exists - not overwritten")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"ERROR {raw_src} -> {raw_dst}: {exc}")
    return "\n".join(lines) or "Nothing to do."


def copy_paths(copies: list[dict], *, cwd: str | None = None) -> str:
    lines = []
    for raw_src, raw_dst in _pairs(copies, "from"):
        src, err = _checked(raw_src, cwd)
        dst, err2 = _checked(raw_dst, cwd)
        if err or err2:
            lines.append(f"ERROR {err or err2}")
        elif not os.path.lexists(src):
            lines.append(f"ERROR {raw_src}: not found")
        elif _is_reparse(src):
            lines.append(f"ERROR {raw_src}: is a link/junction - not followed")
        elif os.path.lexists(dst):
            lines.append(f"ERROR {raw_dst}: already exists - not overwritten")
        else:
            try:
                if os.path.isdir(src):
                    skipped: list[str] = []

                    def ignore(folder, names, skipped=skipped):
                        bad = [n for n in names if _is_reparse(os.path.join(folder, n))]
                        skipped.extend(os.path.join(folder, n) for n in bad)
                        return bad

                    if os.path.lexists(dst):
                        raise FileExistsError(dst)
                    shutil.copytree(src, dst, symlinks=True, ignore=ignore)
                    line = f"Copied {src} -> {dst}"
                    if skipped:
                        line += f" (skipped {len(skipped)} link/junction(s), not followed: " + ", ".join(skipped[:5]) + ")"
                    lines.append(line)
                else:
                    _copy_file_exclusive(src, dst)
                    lines.append(f"Copied {src} -> {dst}")
            except FileExistsError:
                lines.append(f"ERROR {raw_dst}: already exists - not overwritten")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"ERROR {raw_src} -> {raw_dst}: {exc}")
    return "\n".join(lines) or "Nothing to do."


_BAD_PKG_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".zip")


def _package_ok(manager: str, package) -> bool:
    if not isinstance(package, str) or not package:
        return False
    if package.lower().endswith(_BAD_PKG_SUFFIXES):
        return False
    if manager == "npm" and _NPM_SCOPED_RE.fullmatch(package):
        return True
    if "/" in package or "\\" in package:
        return False
    return bool(_PKG_RE.fullmatch(package) and (manager == "pip" or "[" not in package))


def _pip_python(os_name: str) -> list[str] | None:
    """The owner's Python for pip - never Jarvis's own interpreter (sys.executable)."""
    own = os.path.normcase(os.path.realpath(sys.executable))
    if os_name == "windows":
        py = shutil.which("py")
        if py:
            return [py, "-3"]
        found = shutil.which("python")
    else:
        found = shutil.which("python3")
    if found and os.path.normcase(os.path.realpath(found)) != own:
        return [found]
    return None


def _install_argv(action: str, manager: str, package: str, os_name: str) -> list[str] | None:
    install = action == "install"
    if manager == "winget" and os_name == "windows":
        argv = ["winget", "install" if install else "uninstall", "--id", package, "-e",
                "--accept-source-agreements"]
        return argv + (["--accept-package-agreements"] if install else [])
    if manager == "pip":
        base = _pip_python(os_name)
        if base is None:
            return None
        if install:
            return base + ["-m", "pip", "install", "--user", package]
        return base + ["-m", "pip", "uninstall", "-y", package]
    if manager == "npm":
        return ["npm", "install" if install else "uninstall", "-g", package]
    if manager == "flatpak" and os_name == "linux":
        return ["flatpak", "install" if install else "uninstall", "--user", "-y", package]
    return None


def install_software(action: str, manager: str, package: str, os_name: str) -> str:
    if action not in ("install", "uninstall"):
        return "ERROR: action must be install or uninstall"
    if not _package_ok(manager, package):
        return "ERROR: the package must be a plain package id (no URLs, paths, archives or flags)"
    argv = _install_argv(action, manager, package, os_name)
    if argv is None:
        if manager == "pip":
            return "ERROR: no separate Python was found for pip (Jarvis's own Python is never used)"
        return f"ERROR: {manager} is not available on this machine"
    if manager != "pip":
        argv[0] = shutil.which(argv[0]) or argv[0]
    try:
        with tempfile.TemporaryDirectory() as empty_dir:  # a fresh empty cwd: no stray requirements/configs
            proc = subprocess.run(  # fixed argv, never a shell
                argv, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL, shell=False, cwd=empty_dir,
            )
    except subprocess.TimeoutExpired:
        return f"ERROR: {action} of {package} timed out after 15 minutes"
    except OSError as exc:
        return f"ERROR: could not run {manager}: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return f"{manager} {action} {package}: exit code {proc.returncode}\n{output[-INSTALL_OUTPUT_TAIL:]}"


def open_path(path: str, os_name: str, *, ctx: GuardContext, cwd: str | None = None) -> str:
    """Open the NORMALIZED path (what the guard classified), never the raw string."""
    if not isinstance(path, str) or not path.strip():
        return "ERROR: not a valid path"
    if _URI_SCHEME.match(_clean(path).strip("\"'").strip()):
        return "ERROR: web addresses and app links cannot be opened with open_path"
    err = device_path_error(path)
    if err:
        return f"ERROR {err}"
    target = normalize_path(path, ctx)
    if not os.path.exists(target):
        return f"ERROR {path}: not found"
    try:
        if os_name == "windows":
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {path}: {exc}"
    return f"Opened {target}"


_EDITOR_BAD_CHARS = set('&|<>^%!"\n\r')
_BATCH_EXT = (".cmd", ".bat")


def _editor_path_error(raw: str) -> str | None:
    if any(c in _EDITOR_BAD_CHARS for c in raw):
        return f"{raw!r}: this path contains characters that are not allowed for opening in an editor"
    return None


def _vscode_exe(os_name: str) -> str | None:
    """VS Code's real executable. On Windows `code` resolves to bin\\code.cmd - a batch file,
    which cmd.exe would parse (so `&` in a folder name would run commands) - so use Code.exe."""
    found = shutil.which("code")
    if not found:
        return None
    if os_name == "windows":
        if found.lower().endswith(_BATCH_EXT):
            exe = os.path.join(os.path.dirname(os.path.dirname(found)), "Code.exe")
            return exe if os.path.isfile(exe) else None
        return found
    return found


def open_in_editor(path: str | None, os_name: str = "windows", *, cwd: str | None = None,
                   claude_path: str | None = None) -> str:
    raw = path if isinstance(path, str) and path.strip() else "."
    err = _editor_path_error(raw)
    if err:
        return f"ERROR {err}"
    target, err = _checked(raw, cwd)
    if err:
        return f"ERROR {err}"
    err = _editor_path_error(target)
    if err:
        return f"ERROR {err}"
    if not os.path.exists(target):
        return f"ERROR {raw}: not found"
    try:
        code = _vscode_exe(os_name)
        if code and not code.lower().endswith(_BATCH_EXT):
            subprocess.Popen([code, "--", target], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened {target} in VS Code"
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        if os_name == "windows":
            exe = claude_path
            if not exe or exe.lower().endswith(_BATCH_EXT) or not os.path.isfile(exe):
                return "ERROR: neither VS Code nor the Claude Code program could be found"
            subprocess.Popen([exe], cwd=folder,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", "claude"], cwd=folder)
        return f"VS Code is not installed; opened a terminal with Claude Code in {folder}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR could not open an editor: {exc}"


# ------------------------------------------------------------- SDK tool objects

def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


_STR = {"type": "string"}
_PAIR = {
    "type": "object",
    "properties": {"from": _STR, "to": _STR},
    "required": ["from", "to"],
}


def build_computer_tools(
    state: ComputerTaskState, send_file_cb, queue_task_cb, *,
    ctx: GuardContext, cwd: str | None = None, machine: str = "pc", claude_path: str | None = None,
) -> list[SdkMcpTool]:
    """SDK tool objects. The server only gets queue_pc_task."""
    cwd = cwd or ctx.cwd or ctx.home

    @tool("queue_pc_task", "Queue a task for the owner's PC to run when it is next on.",
          {"type": "object", "properties": {"text": _STR}, "required": ["text"]})
    async def _queue(args: dict) -> dict:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _text("ERROR: text is required")
        return _text(await queue_task_cb(text.strip()))

    if machine == "server":
        return [_queue]
    # queue_pc_task is for the server only: on the PC the task can simply be done.

    @tool("move_to_trash", "Delete files or folders by moving them to the Recycle Bin/Trash "
          "(the only way to delete).",
          {"type": "object", "properties": {"paths": {"type": "array", "items": _STR}},
           "required": ["paths"]})
    async def _trash(args: dict) -> dict:
        paths = args.get("paths")
        out = await asyncio.to_thread(
            move_to_trash, paths if isinstance(paths, list) else [], cwd=cwd, os_name=ctx.os_name
        )
        for line in out.splitlines():
            if line.startswith("Moved to the trash: "):
                state.files_changed.add(line[len("Moved to the trash: "):])
        return _text(out)

    @tool("make_folder", "Create a folder (and any missing parents).",
          {"type": "object", "properties": {"path": _STR}, "required": ["path"]})
    async def _mkdir(args: dict) -> dict:
        out = await asyncio.to_thread(make_folder, args.get("path"), cwd=cwd)
        return _text(out)

    @tool("move_paths", "Move or rename files/folders. 'to' must be the full new path and must not "
          "exist (nothing is ever overwritten).",
          {"type": "object", "properties": {"moves": {"type": "array", "items": _PAIR}},
           "required": ["moves"]})
    async def _move(args: dict) -> dict:
        moves = args.get("moves")
        out = await asyncio.to_thread(move_paths, moves if isinstance(moves, list) else [], cwd=cwd)
        _note_changed(state, out, "Moved ")
        return _text(out)

    @tool("copy_paths", "Copy files/folders. 'to' must be the full new path and must not exist.",
          {"type": "object", "properties": {"copies": {"type": "array", "items": _PAIR}},
           "required": ["copies"]})
    async def _copy(args: dict) -> dict:
        copies = args.get("copies")
        out = await asyncio.to_thread(copy_paths, copies if isinstance(copies, list) else [], cwd=cwd)
        _note_changed(state, out, "Copied ")
        return _text(out)

    @tool("install_software", "Install or uninstall software with a package manager "
          "(winget on Windows, pip, npm, flatpak on Linux).",
          {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["install", "uninstall"]},
              "manager": {"type": "string", "enum": ["winget", "pip", "npm", "flatpak"]},
              "package": _STR}, "required": ["action", "manager", "package"]})
    async def _install(args: dict) -> dict:
        out = await asyncio.to_thread(
            install_software, args.get("action"), args.get("manager"), args.get("package"), ctx.os_name
        )
        return _text(out)

    @tool("open_path", "Open a file or folder with its default app (like double-clicking it).",
          {"type": "object", "properties": {"path": _STR}, "required": ["path"]})
    async def _open(args: dict) -> dict:
        out = await asyncio.to_thread(open_path, args.get("path"), ctx.os_name, ctx=ctx, cwd=cwd)
        return _text(out)

    @tool("send_file", "Send a file to the owner in Discord.",
          {"type": "object", "properties": {"path": _STR, "destination": _STR}, "required": ["path"]})
    async def _send(args: dict) -> dict:
        raw = args.get("path")
        target, err = _checked(raw, cwd)
        if err:
            return _text(f"ERROR {err}")
        real = os.path.realpath(target)  # exactly what the guard checked as the real path
        if not os.path.isfile(real):
            return _text(f"ERROR {raw}: not a file")
        return _text(await send_file_cb(real))

    @tool("open_in_editor", "Open a folder or file in VS Code (or a terminal with Claude Code).",
          {"type": "object", "properties": {"path": _STR}})
    async def _editor(args: dict) -> dict:
        out = await asyncio.to_thread(
            open_in_editor, args.get("path"), ctx.os_name, cwd=cwd, claude_path=claude_path
        )
        return _text(out)

    _place_props = {
        "monitor": {"type": "integer", "description": "1 is the primary monitor, then 2, 3..."},
        "maximized": {"type": "boolean"},
        "size": {"type": "array", "items": {"type": "number"},
                 "description": "[width, height]: fractions of the monitor (0.5 = half) or pixels"},
        "position": {"type": "string", "enum": list(window_manager.POSITIONS),
                     "description": "Where on the monitor: left/right half, top/bottom half, a quarter "
                                    "(top-left...), or center. Use it for side-by-side windows."},
    }

    @tool("open_on_screen", "Open web pages (as tabs in ONE new browser window) or a folder/file, and "
          "put that window on a given monitor, optionally maximized or sized. Give urls or path.",
          {"type": "object", "properties": {
              "urls": {"type": "array", "items": _STR}, "path": _STR, **_place_props}})
    async def _on_screen(args: dict) -> dict:
        monitor, maximized, size, err = _placement(args)
        position = args.get("position")
        if err or (position is not None and position not in window_manager.POSITIONS):
            return _text(err or f"ERROR position must be one of: {', '.join(window_manager.POSITIONS)}")
        urls, raw = args.get("urls"), args.get("path")
        if urls is not None and raw is None:
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
                return _text("ERROR urls must be a list of web addresses")
            return _text(await asyncio.to_thread(
                window_manager.open_urls_on_screen, urls, monitor, maximized, size, position))
        if raw is not None and urls is None:
            target, perr = _checked(raw, cwd)
            if perr:
                return _text(f"ERROR {perr}")
            return _text(await asyncio.to_thread(
                window_manager.open_path_on_screen, target, monitor, maximized, size, position))
        return _text("ERROR give either urls or path")

    @tool("move_window", "Move an already-open window (found by part of its title) to a monitor, "
          "optionally maximized or sized. Use list_windows to see titles.",
          {"type": "object", "properties": {"title": _STR, **_place_props}, "required": ["title"]})
    async def _move_window(args: dict) -> dict:
        monitor, maximized, size, err = _placement(args)
        position = args.get("position")
        if err or (position is not None and position not in window_manager.POSITIONS):
            return _text(err or f"ERROR position must be one of: {', '.join(window_manager.POSITIONS)}")
        return _text(await asyncio.to_thread(
            window_manager.move_window, args.get("title"), monitor, maximized, size, position))

    @tool("list_windows", "List the open windows and which monitor each is on.",
          {"type": "object", "properties": {}})
    async def _list_windows(args: dict) -> dict:
        return _text(await asyncio.to_thread(window_manager.list_windows))

    return [_trash, _mkdir, _move, _copy, _install, _open, _send, _editor,
            _on_screen, _move_window, _list_windows]


def _note_changed(state: ComputerTaskState, output: str, prefix: str) -> None:
    for line in output.splitlines():
        if line.startswith(prefix) and " -> " in line:
            state.files_changed.add(line[len(prefix):].split(" -> ", 1)[1])


# ------------------------------------------------------------- owner's folders

# Windows "known folder" ids. Desktop/Documents/Pictures are often moved into
# OneDrive and named in the system language, so Claude must be told where they
# really are instead of guessing ~/Desktop.
_KNOWN_FOLDER_IDS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}
_folders_cache: dict[str, dict[str, str]] = {}


def _windows_known_folder(guid: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    folder_id = _GUID()
    if ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid), ctypes.byref(folder_id)) != 0:
        return None
    path = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id), 0, None, ctypes.byref(path)
    ) != 0:
        return None
    try:
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def known_folders(os_name: str) -> dict[str, str]:
    """The owner's Desktop/Documents/Downloads/... as the OS really has them. Cached."""
    cached = _folders_cache.get(os_name)
    if cached is not None:
        return cached
    found: dict[str, str] = {}
    for name, guid in _KNOWN_FOLDER_IDS.items():
        try:
            if os_name == "windows" and sys.platform == "win32":
                path = _windows_known_folder(guid)
            else:
                path = os.path.join(os.path.expanduser("~"), name)
        except Exception as exc:  # noqa: BLE001 — a missing folder just isn't listed
            logger.warning("couldn't resolve the %s folder: %s", name, exc)
            path = None
        if path and os.path.isdir(path):
            found[name] = path
    _folders_cache[os_name] = found
    return found


def owner_folders_line(os_name: str) -> str:
    folders = known_folders(os_name)
    if not folders:
        return ""
    listed = "; ".join(f"{name} = {path}" for name, path in folders.items())
    return (
        f"\n\nThe owner's folders: {listed}. Use these exact paths when the owner "
        "mentions them (e.g. 'my Desktop') — don't guess paths like ~/Desktop."
    )
