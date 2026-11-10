import asyncio
import os
import sys
import subprocess
from types import SimpleNamespace

import pytest

from brain import computer_tools as ct
from brain.computer_tools import ComputerTaskState
from brain.guard import GuardContext


def _ctx(home, os_name="windows"):
    return GuardContext("pc", os_name, str(home), str(home / "jarvis"), cwd=str(home))


# ---- device paths ---------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "\\\\?\\GLOBALROOT\\Device\\x", "//?/GLOBALROOT/Device/x", "\\\\.\\PhysicalDrive0", "//./PhysicalDrive0",
    "\\??\\C:\\Windows", "\\\\?\\Volume{abc}\\x", "\\\\?\\C:\\a\\..\\Windows", "//?/C:/a/../b",
    "\\\\?\\UNC\\host\\share", "  \\\\?\\GLOBALROOT\\x", "\u200b\\\\?\\GLOBALROOT\\x", "\"\\\\.\\C:\"",
])
def test_device_paths_are_refused(raw):
    assert ct.device_path_error(raw) is not None


@pytest.mark.parametrize("raw", ["\\\\?\\C:\\Users\\a.txt", "//?/D:/x/y", "C:\\Users\\a", "notes.txt", "~/x"])
def test_plain_paths_are_not_device_paths(raw):
    assert ct.device_path_error(raw) is None


def test_resolve_tool_path_never_expands_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETDIR", "elsewhere")
    got = ct.resolve_tool_path("%SECRETDIR%/a.txt", str(tmp_path))
    assert got == os.path.abspath(os.path.join(str(tmp_path), "%SECRETDIR%/a.txt"))
    got = ct.resolve_tool_path("$SECRETDIR/a.txt", str(tmp_path))
    assert "elsewhere" not in got


def test_resolve_tool_path_expands_tilde_and_cwd(tmp_path):
    assert ct.resolve_tool_path("~", str(tmp_path)) == os.path.abspath(os.path.expanduser("~"))
    assert ct.resolve_tool_path("sub/a.txt", str(tmp_path)) == os.path.abspath(str(tmp_path / "sub" / "a.txt"))


# ---- move_to_trash --------------------------------------------------------

def test_move_to_trash_uses_send2trash_per_path(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    sent = []
    monkeypatch.setattr(ct, "_send2trash", lambda p: sent.append(p))
    out = ct.move_to_trash(["a.txt", "b.txt"], cwd=str(tmp_path))
    assert sent == [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")]
    assert out.count("Moved to the trash") == 2
    assert (tmp_path / "a.txt").exists()  # the fake never deleted anything


def test_move_to_trash_reports_failures_and_never_deletes_permanently(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")

    def fake(p):
        if p.endswith("a.txt"):
            raise OSError("no recycle bin on this drive")

    monkeypatch.setattr(ct, "_send2trash", fake)
    monkeypatch.setattr(os, "remove", lambda p: pytest.fail("permanent delete"))
    out = ct.move_to_trash(["a.txt", "b.txt", "missing.txt"], cwd=str(tmp_path))
    assert "a.txt: could not move to the trash" in out and "nothing was deleted" in out
    assert "Moved to the trash" in out and "missing.txt: not found" in out


def test_move_to_trash_refuses_device_paths(monkeypatch):
    monkeypatch.setattr(ct, "_send2trash", lambda p: pytest.fail("must not be called"))
    out = ct.move_to_trash(["\\\\?\\GLOBALROOT\\Device\\x", "//./C:"])
    assert out.count("device-namespace") == 2


# ---- make_folder / move / copy -------------------------------------------

def test_make_folder_creates_nested(tmp_path):
    out = ct.make_folder("a/b/c", cwd=str(tmp_path))
    assert (tmp_path / "a" / "b" / "c").is_dir() and "Folder ready" in out
    assert ct.make_folder("a/b/c", cwd=str(tmp_path)).startswith("Folder ready")  # idempotent


def test_make_folder_refuses_device_path():
    assert "device-namespace" in ct.make_folder("\\\\?\\GLOBALROOT\\x")


def test_move_paths_moves_and_never_overwrites(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    (tmp_path / "taken.txt").write_text("T")
    out = ct.move_paths([
        {"from": "a.txt", "to": "taken.txt"},
        {"from": "b.txt", "to": "moved.txt"},
        {"from": "ghost.txt", "to": "z.txt"},
    ], cwd=str(tmp_path))
    assert (tmp_path / "taken.txt").read_text() == "T" and (tmp_path / "a.txt").exists()
    assert (tmp_path / "moved.txt").read_text() == "B" and not (tmp_path / "b.txt").exists()
    assert "taken.txt: already exists - not overwritten" in out
    assert "ghost.txt: not found" in out


def test_move_paths_refuses_existing_directory_destination(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "dir").mkdir()
    out = ct.move_paths([{"from": "a.txt", "to": "dir"}], cwd=str(tmp_path))
    assert "already exists" in out and (tmp_path / "a.txt").exists()


def test_move_paths_refuses_device_paths(tmp_path):
    out = ct.move_paths([{"from": "\\\\?\\GLOBALROOT\\x", "to": "y"}, {"from": "a", "to": "//./C:"}],
                        cwd=str(tmp_path))
    assert out.count("device-namespace") == 2


def test_move_paths_malformed_item_is_an_error_line(tmp_path):
    assert "ERROR" in ct.move_paths(["nope", {"from": "a"}], cwd=str(tmp_path))


def test_copy_paths_copies_files_and_folders_without_overwriting(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "in.txt").write_text("I")
    (tmp_path / "keep.txt").write_text("K")
    out = ct.copy_paths([
        {"from": "a.txt", "to": "a2.txt"}, {"from": "d", "to": "d2"}, {"from": "a.txt", "to": "keep.txt"},
    ], cwd=str(tmp_path))
    assert (tmp_path / "a2.txt").read_text() == "A"
    assert (tmp_path / "d2" / "in.txt").read_text() == "I"
    assert (tmp_path / "keep.txt").read_text() == "K"
    assert "keep.txt: already exists - not overwritten" in out


def test_copy_paths_refuses_device_paths(tmp_path):
    assert "device-namespace" in ct.copy_paths([{"from": "a", "to": "\\??\\C:\\x"}], cwd=str(tmp_path))


# ---- install_software -----------------------------------------------------

class _FakeRun:
    def __init__(self, returncode=0, stdout="ok", stderr="", exc=None):
        self.calls = []
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc:
            raise self.exc
        return self.result


@pytest.mark.parametrize("action,manager,package,os_name,expected", [
    ("install", "winget", "VideoLAN.VLC", "windows",
     ["winget", "install", "--id", "VideoLAN.VLC", "-e", "--accept-source-agreements",
      "--accept-package-agreements"]),
    ("uninstall", "winget", "VideoLAN.VLC", "windows",
     ["winget", "uninstall", "--id", "VideoLAN.VLC", "-e", "--accept-source-agreements"]),
    ("install", "npm", "left-pad", "linux", ["npm", "install", "-g", "left-pad"]),
    ("uninstall", "npm", "@scope/pkg", "linux", ["npm", "uninstall", "-g", "@scope/pkg"]),
    ("install", "flatpak", "org.gimp.GIMP", "linux", ["flatpak", "install", "--user", "-y", "org.gimp.GIMP"]),
    ("uninstall", "flatpak", "org.gimp.GIMP", "linux", ["flatpak", "uninstall", "--user", "-y", "org.gimp.GIMP"]),
])
def test_install_software_uses_fixed_argv_without_a_shell(monkeypatch, action, manager, package, os_name, expected):
    fake = _FakeRun()
    monkeypatch.setattr(ct.subprocess, "run", fake)
    monkeypatch.setattr(ct.shutil, "which", lambda name: None)
    out = ct.install_software(action, manager, package, os_name)
    argv, kwargs = fake.calls[0]
    assert argv == expected
    assert kwargs["shell"] is False and kwargs["timeout"] == 15 * 60
    assert "exit code 0" in out


def test_pip_uses_the_py_launcher_not_jarvis_own_python(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ct.subprocess, "run", fake)
    monkeypatch.setattr(ct.shutil, "which", lambda n: "PY.EXE" if n == "py" else None)
    ct.install_software("install", "pip", "requests==2.0", "windows")
    ct.install_software("uninstall", "pip", "requests", "windows")
    assert fake.calls[0][0] == ["PY.EXE", "-3", "-m", "pip", "install", "--user", "requests==2.0"]
    assert fake.calls[1][0] == ["PY.EXE", "-3", "-m", "pip", "uninstall", "-y", "requests"]
    assert all(ct.sys.executable not in argv for argv, _ in fake.calls)


def test_pip_runs_in_a_fresh_empty_temp_dir(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["cwd"] = kw["cwd"]
        seen["listing"] = os.listdir(kw["cwd"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ct.subprocess, "run", fake)
    monkeypatch.setattr(ct.shutil, "which", lambda n: "PY.EXE" if n == "py" else None)
    ct.install_software("install", "pip", "requests", "windows")
    assert seen["listing"] == [] and not os.path.exists(seen["cwd"])  # fresh, then cleaned up


def test_pip_without_a_separate_python_is_refused_when_which_finds_jarvis_own(monkeypatch):
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    monkeypatch.setattr(ct.shutil, "which", lambda n: ct.sys.executable if n == "python" else None)
    assert "Jarvis's own Python is never used" in ct.install_software("install", "pip", "requests", "windows")
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    assert ct.install_software("install", "pip", "requests", "linux").startswith("ERROR")


def test_pip_on_linux_uses_python3(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ct.subprocess, "run", fake)
    monkeypatch.setattr(ct.shutil, "which", lambda n: "/usr/bin/python3" if n == "python3" else None)
    ct.install_software("install", "pip", "requests", "linux")
    assert fake.calls[0][0][:4] == ["/usr/bin/python3", "-m", "pip", "install"]


@pytest.mark.parametrize("package", ["pkg.whl", "pkg-1.0.tar.gz", "PKG.TGZ", "a.zip", "dir/pkg", "dir\\pkg", "@s/x.zip"])
@pytest.mark.parametrize("manager", ["pip", "npm", "winget"])
def test_archive_and_path_package_names_are_rejected(monkeypatch, manager, package):
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert ct.install_software("install", manager, package, "windows").startswith("ERROR")


def test_scoped_npm_packages_are_still_fine(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(ct.subprocess, "run", fake)
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    assert "exit code 0" in ct.install_software("install", "npm", "@scope/pkg", "linux")


@pytest.mark.parametrize("package", [
    "https://x.y/a.whl", "-r req.txt", "--index-url=http://evil", "a b", "a;b", "a&b", "../x", "C:\\x.whl",
    "git+https://x/y", "", "x\ny", "$(id)",
])
def test_install_software_rejects_bad_packages(monkeypatch, package):
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert ct.install_software("install", "pip", package, "windows").startswith("ERROR")


def test_install_software_rejects_bad_action_and_unavailable_manager(monkeypatch):
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert ct.install_software("upgrade", "pip", "x", "windows").startswith("ERROR")
    assert "not available" in ct.install_software("install", "winget", "A.B", "linux")
    assert "not available" in ct.install_software("install", "flatpak", "a.b", "windows")
    assert ct.install_software("install", "apt", "x", "linux").startswith("ERROR")


def test_install_software_returns_exit_code_and_output_tail(monkeypatch):
    monkeypatch.setattr(ct.subprocess, "run", _FakeRun(returncode=1, stdout="x" * 5000, stderr="boom"))
    out = ct.install_software("install", "pip", "requests", "windows")
    assert "exit code 1" in out and out.endswith("boom") and len(out) < 1700


def test_install_software_timeout_and_missing_manager(monkeypatch):
    monkeypatch.setattr(ct.subprocess, "run", _FakeRun(exc=subprocess.TimeoutExpired("x", 900)))
    assert "timed out after 15 minutes" in ct.install_software("install", "pip", "requests", "windows")
    monkeypatch.setattr(ct.subprocess, "run", _FakeRun(exc=FileNotFoundError("nope")))
    assert ct.install_software("install", "npm", "x", "linux").startswith("ERROR")


# ---- open_path ------------------------------------------------------------

def test_open_path_opens_the_normalized_path_not_the_raw_string(monkeypatch, tmp_path):
    (tmp_path / "Docs").mkdir()
    (tmp_path / "Docs" / "Plan.PDF").write_text("x")
    opened = []
    monkeypatch.setattr(ct.os, "startfile", lambda p: opened.append(p), raising=False)
    ctx = _ctx(tmp_path)
    raw = "docs\\..\\DOCS/Plan.PDF"
    out = ct.open_path(raw, "windows", ctx=ctx, cwd=str(tmp_path))
    assert opened == [ct.normalize_path(raw, ctx)] and opened[0] != raw
    assert out.startswith("Opened")


@pytest.mark.parametrize("raw", [
    "https://evil.example/x", "file:///C:/Windows/System32/calc.exe", "ms-msdt:/id x", "'steam://run/1'",
    "\u200bhttp://x.y", "mailto:a@b.c",
])
def test_open_path_refuses_uri_schemes(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(ct.os, "startfile", lambda p: pytest.fail("must not open"), raising=False)
    assert ct.open_path(raw, "windows", ctx=_ctx(tmp_path)).startswith("ERROR")


def test_open_path_refuses_device_paths_and_missing_files(monkeypatch, tmp_path):
    monkeypatch.setattr(ct.os, "startfile", lambda p: pytest.fail("must not open"), raising=False)
    assert "device-namespace" in ct.open_path("\\\\?\\GLOBALROOT\\x", "windows", ctx=_ctx(tmp_path))
    assert "not found" in ct.open_path("nothing.pdf", "windows", ctx=_ctx(tmp_path))


def test_open_path_linux_uses_xdg_open_with_the_normalized_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ct.os.path, "exists", lambda p: True)  # a Linux-style path on a Windows test host
    calls = []
    monkeypatch.setattr(ct.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    ctx = _ctx(tmp_path, "linux")
    ct.open_path("a.pdf", "linux", ctx=ctx, cwd=str(tmp_path))
    assert calls == [["xdg-open", ct.normalize_path("a.pdf", ctx)]]


# ---- open_in_editor -------------------------------------------------------

def _fake_vscode(tmp_path):
    bin_dir = tmp_path / "Microsoft VS Code" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "code.CMD").write_text("@echo off")
    (tmp_path / "Microsoft VS Code" / "Code.exe").write_text("")
    return str(bin_dir / "code.CMD"), str(tmp_path / "Microsoft VS Code" / "Code.exe")


def test_open_in_editor_launches_code_exe_directly_after_double_dash(monkeypatch, tmp_path):
    cmd, exe = _fake_vscode(tmp_path)
    calls = []
    monkeypatch.setattr(ct.shutil, "which", lambda n: cmd)
    monkeypatch.setattr(ct.subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    proj = tmp_path / "proj"
    proj.mkdir()
    out = ct.open_in_editor(str(proj), "windows", cwd=str(tmp_path))
    assert calls == [[exe, "--", str(proj)]] and "VS Code" in out


@pytest.mark.parametrize("name", ["q&calc", "a|b", "a<b", "a>b", "a^b", "%TEMP%", "a!b", "a\"b", "a\nb"])
def test_open_in_editor_refuses_shell_metacharacters(monkeypatch, tmp_path, name):
    monkeypatch.setattr(ct.shutil, "which", lambda n: "CODE")
    monkeypatch.setattr(ct.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    out = ct.open_in_editor(name, "windows", cwd=str(tmp_path))
    assert out.startswith("ERROR") and "not allowed" in out


def test_q_and_calc_folder_cannot_inject_a_command(monkeypatch, tmp_path):
    """make_folder('q&calc') then open_in_editor('q&calc') must never reach a launcher."""
    cmd, _ = _fake_vscode(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n: cmd)
    launched = []
    monkeypatch.setattr(ct.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert ct.make_folder("q&calc", cwd=str(tmp_path)).startswith("Folder ready")
    assert ct.open_in_editor("q&calc", "windows", cwd=str(tmp_path)).startswith("ERROR")
    assert launched == []


def test_no_launch_ever_runs_a_batch_file(monkeypatch, tmp_path):
    cmd, _ = _fake_vscode(tmp_path)
    (tmp_path / "Microsoft VS Code" / "Code.exe").unlink()      # only the .CMD shim exists
    claude = tmp_path / "claude.exe"
    claude.write_text("")
    calls = []
    monkeypatch.setattr(ct.shutil, "which", lambda n: cmd)
    monkeypatch.setattr(ct.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    proj = tmp_path / "proj"
    proj.mkdir()
    ct.open_in_editor(str(proj), "windows", cwd=str(tmp_path), claude_path=str(claude))
    ct.open_in_editor(str(proj), "windows", cwd=str(tmp_path), claude_path=str(tmp_path / "claude.CMD"))
    for argv, _ in calls:
        assert not argv[0].lower().endswith((".cmd", ".bat"))
    assert calls[0][0] == [str(claude)] and calls[0][1]["cwd"] == str(proj)
    assert calls[0][1]["creationflags"] == getattr(ct.subprocess, "CREATE_NEW_CONSOLE", 0x10)
    assert len(calls) == 1  # the .CMD claude was refused


def test_open_in_editor_falls_back_to_a_terminal_with_claude_on_linux(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ct.shutil, "which", lambda n: None)
    monkeypatch.setattr(ct.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    out = ct.open_in_editor(str(tmp_path), "linux", cwd=str(tmp_path))
    assert calls[0][0] == ["x-terminal-emulator", "-e", "claude"] and calls[0][1]["cwd"] == str(tmp_path)
    assert "terminal" in out


def test_open_in_editor_refuses_device_paths_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ct.subprocess, "Popen", lambda *a, **k: pytest.fail("must not run"))
    assert "device-namespace" in ct.open_in_editor("\\\\?\\GLOBALROOT\\x", "windows")
    assert "not found" in ct.open_in_editor("ghost", "windows", cwd=str(tmp_path))


# ---- SDK tool objects -----------------------------------------------------

def _build(tmp_path, machine="pc", send=None, queue=None):
    async def default_send(path):
        return f"sent {path}"

    async def default_queue(text):
        return f"queued {text}"

    state = ComputerTaskState("t1")
    tools = ct.build_computer_tools(state, send or default_send, queue or default_queue,
                                    ctx=_ctx(tmp_path), cwd=str(tmp_path), machine=machine)
    return state, {t.name: t for t in tools}


def test_pc_gets_the_custom_tools_and_no_queue_pc_task(tmp_path):
    _, tools = _build(tmp_path)
    assert set(tools) == {"move_to_trash", "make_folder", "move_paths", "copy_paths", "install_software",
                          "open_path", "send_file", "open_in_editor",
                          "open_on_screen", "move_window", "list_windows"}


def test_open_on_screen_tool_passes_urls_and_placement_to_the_window_code(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ct.window_manager, "open_urls_on_screen",
                        lambda urls, monitor, maximized, size, position=None: calls.append((urls, monitor, maximized, size)) or "ok")
    _, tools = _build(tmp_path)
    asyncio.run(tools["open_on_screen"].handler(
        {"urls": ["https://claude.ai"], "monitor": 1, "maximized": True, "size": [0.5, 1]}))
    assert calls == [(["https://claude.ai"], 1, True, (0.5, 1.0))]


def test_open_on_screen_tool_opens_the_resolved_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ct.window_manager, "open_path_on_screen",
                        lambda path, monitor, maximized, size, position=None: calls.append((path, monitor)) or "ok")
    (tmp_path / "coffee").mkdir()
    _, tools = _build(tmp_path)
    asyncio.run(tools["open_on_screen"].handler({"path": "coffee", "monitor": 2}))
    assert calls == [(str(tmp_path / "coffee"), 2)]


@pytest.mark.parametrize("args", [{"urls": ["https://a.b"], "monitor": "two"},
                                  {"urls": ["https://a.b"], "size": [1]},
                                  {"path": r"\\?\GLOBALROOT\x"},
                                  {}])
def test_open_on_screen_tool_rejects_bad_arguments(monkeypatch, tmp_path, args):
    monkeypatch.setattr(ct.window_manager, "open_urls_on_screen", lambda *a: "should not run")
    monkeypatch.setattr(ct.window_manager, "open_path_on_screen", lambda *a: "should not run")
    _, tools = _build(tmp_path)
    out = asyncio.run(tools["open_on_screen"].handler(args))["content"][0]["text"]
    assert out.startswith("ERROR")


def test_move_window_and_list_windows_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(ct.window_manager, "move_window", lambda t, m, mx, s, position=None: f"moved {t} to {m}")
    monkeypatch.setattr(ct.window_manager, "list_windows", lambda: "2 monitors")
    _, tools = _build(tmp_path)
    moved = asyncio.run(tools["move_window"].handler({"title": "coffee", "monitor": 2}))
    listed = asyncio.run(tools["list_windows"].handler({}))
    assert moved["content"][0]["text"] == "moved coffee to 2"
    assert listed["content"][0]["text"] == "2 monitors"


def test_server_only_gets_queue_pc_task(tmp_path):
    _, tools = _build(tmp_path, machine="server")
    assert set(tools) == {"queue_pc_task"}


def test_make_folder_tool_runs_and_returns_text_content(tmp_path):
    _, tools = _build(tmp_path)
    result = asyncio.run(tools["make_folder"].handler({"path": "x/y"}))
    assert (tmp_path / "x" / "y").is_dir()
    assert result["content"][0]["type"] == "text"


def test_move_paths_tool_records_changed_files(tmp_path):
    state, tools = _build(tmp_path)
    (tmp_path / "a.txt").write_text("A")
    asyncio.run(tools["move_paths"].handler({"moves": [{"from": "a.txt", "to": "b.txt"}]}))
    assert state.files_changed == {str(tmp_path / "b.txt")}


def test_trash_tool_uses_the_fake(monkeypatch, tmp_path):
    state, tools = _build(tmp_path)
    (tmp_path / "a.txt").write_text("A")
    monkeypatch.setattr(ct, "_send2trash", lambda p: None)
    result = asyncio.run(tools["move_to_trash"].handler({"paths": ["a.txt"]}))
    assert "Moved to the trash" in result["content"][0]["text"]
    assert state.files_changed == {str(tmp_path / "a.txt")}


def test_send_file_tool_sends_the_resolved_real_path(tmp_path):
    sent = []

    async def send(path):
        sent.append(path)
        return "ok"

    _, tools = _build(tmp_path, send=send)
    (tmp_path / "a.txt").write_text("A")
    asyncio.run(tools["send_file"].handler({"path": "a.txt"}))
    assert sent == [os.path.realpath(str(tmp_path / "a.txt"))]


def test_send_file_tool_refuses_device_paths_and_non_files(tmp_path):
    async def send(path):
        pytest.fail("must not send")

    _, tools = _build(tmp_path, send=send)
    assert "device-namespace" in asyncio.run(tools["send_file"].handler({"path": "\\\\.\\C:"}))["content"][0]["text"]
    assert "not a file" in asyncio.run(tools["send_file"].handler({"path": "nope"}))["content"][0]["text"]


def test_queue_pc_task_tool_calls_the_callback(tmp_path):
    _, tools = _build(tmp_path, machine="server")
    result = asyncio.run(tools["queue_pc_task"].handler({"text": " tidy downloads "}))
    assert result["content"][0]["text"] == "queued tidy downloads"
    assert "required" in asyncio.run(tools["queue_pc_task"].handler({"text": ""}))["content"][0]["text"]


# ---- fix round 1: Recycle Bin safety --------------------------------------

def _no_trash(monkeypatch):
    sent = []
    monkeypatch.setattr(ct, "_send2trash", lambda p: sent.append(p))
    return sent


def test_trash_refuses_non_fixed_drives(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(ct, "_drive_type", lambda root: 2)  # removable
    out = ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="windows")
    assert "can't use the Recycle Bin there" in out and "nothing was deleted" in out and sent == []


def test_trash_allows_fixed_drives(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    assert "Moved to the trash" in ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="windows")
    assert sent == [str(tmp_path / "a.txt")]


def test_trash_refuses_when_the_drive_cannot_be_checked(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "a.txt").write_text("x")

    def boom(root):
        raise OSError("no")

    monkeypatch.setattr(ct, "_drive_type", boom)
    assert "can't use the Recycle Bin" in ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="windows")
    assert sent == []


def test_trash_refuses_unc_paths(monkeypatch):
    sent = _no_trash(monkeypatch)
    monkeypatch.setattr(ct.os.path, "lexists", lambda p: True)
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    out = ct.move_to_trash(["\\\\host\\share\\x.txt"], os_name="windows")
    assert "can't use the Recycle Bin there" in out and sent == []


def test_trash_refuses_over_2gb_in_total(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "a.bin").write_bytes(b"x" * 6)
    (tmp_path / "b.bin").write_bytes(b"x" * 6)
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    monkeypatch.setattr(ct, "TRASH_MAX_BYTES", 10)
    out = ct.move_to_trash(["a.bin", "b.bin"], cwd=str(tmp_path), os_name="windows")
    assert sent == [str(tmp_path / "a.bin")]                 # the second pushed the total over the cap
    assert "b.bin: too big for the Recycle Bin" in out
    assert ct.TRASH_MAX_BYTES == 10


def test_trash_limit_is_two_gigabytes():
    assert ct.TRASH_MAX_BYTES == 2 * 1024 ** 3


def test_trash_sizes_folders_by_walking(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "d" / "sub").mkdir(parents=True)
    (tmp_path / "d" / "sub" / "f.bin").write_bytes(b"x" * 50)
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    monkeypatch.setattr(ct, "TRASH_MAX_BYTES", 40)
    assert "too big" in ct.move_to_trash(["d"], cwd=str(tmp_path), os_name="windows")
    assert sent == []


def test_trash_refuses_when_the_size_budget_runs_out(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.bin").write_bytes(b"x")
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    monkeypatch.setattr(ct, "TRASH_SIZE_BUDGET_S", -1.0)
    assert "too big for the Recycle Bin" in ct.move_to_trash(["d"], cwd=str(tmp_path), os_name="windows")
    assert sent == []


def test_trash_on_linux_refuses_other_mounts(monkeypatch, tmp_path):
    sent = _no_trash(monkeypatch)
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(ct, "_same_device", lambda p, home: False)
    assert "can't use the Recycle Bin there" in ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="linux")
    assert sent == []
    monkeypatch.setattr(ct, "_same_device", lambda p, home: True)
    assert "Moved to the trash" in ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="linux")


# ---- fix round 1: junctions and the no-overwrite races ----------------------

def test_copy_paths_skips_and_reports_links_inside_a_tree(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "real.txt").write_text("R")
    (tmp_path / "d" / "link").mkdir()
    (tmp_path / "d" / "link" / "secret.txt").write_text("S")
    monkeypatch.setattr(ct, "_is_reparse", lambda p: p.endswith("link"))
    out = ct.copy_paths([{"from": "d", "to": "d2"}], cwd=str(tmp_path))
    assert (tmp_path / "d2" / "real.txt").exists() and not (tmp_path / "d2" / "link").exists()
    assert "skipped 1 link/junction" in out


def test_copy_paths_refuses_a_link_source(monkeypatch, tmp_path):
    (tmp_path / "d").mkdir()
    monkeypatch.setattr(ct, "_is_reparse", lambda p: True)
    out = ct.copy_paths([{"from": "d", "to": "d2"}], cwd=str(tmp_path))
    assert "link/junction - not followed" in out and not (tmp_path / "d2").exists()


def test_is_reparse_detects_junctions_and_reparse_attributes(monkeypatch, tmp_path):
    (tmp_path / "plain").mkdir()
    assert ct._is_reparse(str(tmp_path / "plain")) is False
    monkeypatch.setattr(ct.os.path, "isjunction", lambda p: True, raising=False)
    assert ct._is_reparse(str(tmp_path / "plain")) is True
    monkeypatch.setattr(ct.os.path, "isjunction", lambda p: False, raising=False)

    class St:
        st_file_attributes = 0x400

    monkeypatch.setattr(ct.os, "lstat", lambda p: St())
    assert ct._is_reparse(str(tmp_path / "plain")) is True


def _racy_lexists(monkeypatch, victim):
    real = os.path.lexists
    monkeypatch.setattr(ct.os.path, "lexists", lambda p: False if p == victim else real(p))


def test_copy_never_overwrites_even_when_the_destination_appears_after_the_check(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "taken.txt").write_text("T")
    _racy_lexists(monkeypatch, str(tmp_path / "taken.txt"))
    out = ct.copy_paths([{"from": "a.txt", "to": "taken.txt"}], cwd=str(tmp_path))
    assert (tmp_path / "taken.txt").read_text() == "T" and "already exists - not overwritten" in out


def test_move_never_overwrites_even_when_the_destination_appears_after_the_check(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "taken.txt").write_text("T")
    _racy_lexists(monkeypatch, str(tmp_path / "taken.txt"))
    out = ct.move_paths([{"from": "a.txt", "to": "taken.txt"}], cwd=str(tmp_path))
    assert (tmp_path / "taken.txt").read_text() == "T" and (tmp_path / "a.txt").read_text() == "A"
    assert "already exists - not overwritten" in out


def test_copy_file_exclusive_uses_xb(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    with pytest.raises(FileExistsError):
        ct._copy_file_exclusive(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
    assert (tmp_path / "b.txt").read_text() == "B"


def test_move_across_volumes_copies_exclusively_then_unlinks(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("A")

    def cross(*a, **k):
        raise OSError("cross-device link")

    monkeypatch.setattr(ct.os, "rename", cross)
    monkeypatch.setattr(ct.os, "link", cross)
    ct._move_one(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
    assert (tmp_path / "b.txt").read_text() == "A" and not (tmp_path / "a.txt").exists()


def test_move_across_volumes_does_not_overwrite_a_late_destination(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")

    def cross(*a, **k):
        raise OSError("cross-device link")

    monkeypatch.setattr(ct.os, "rename", cross)
    monkeypatch.setattr(ct.os, "link", cross)
    with pytest.raises(FileExistsError):
        ct._move_one(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
    assert (tmp_path / "b.txt").read_text() == "B" and (tmp_path / "a.txt").exists()


# ---- fix round 1: Stop ------------------------------------------------------

def test_stop_sets_the_flag_and_cancels_tracked_futures():
    from concurrent.futures import Future

    state = ComputerTaskState("t")
    pending, finished = Future(), Future()
    finished.set_result(1)
    state.track(pending)
    state.track(finished)
    state.stop()
    assert state.stopped is True and pending.cancelled()
    state.untrack(pending)
    state.untrack(pending)  # idempotent


def test_send2trash_is_imported_lazily(monkeypatch, tmp_path):
    import sys

    assert not hasattr(ct, "send2trash")  # nothing imported at module load
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setitem(sys.modules, "send2trash", None)  # as if the package were not installed
    monkeypatch.setattr(ct, "_drive_type", lambda root: 3)
    out = ct.move_to_trash(["a.txt"], cwd=str(tmp_path), os_name="windows")
    assert out.startswith("ERROR") and "nothing was deleted" in out and (tmp_path / "a.txt").exists()


def test_owner_folders_line_lists_the_real_known_folder_paths(monkeypatch):
    from brain import computer_tools as ct

    fake = {"Desktop": r"C:\Users\alex\OneDrive\Desktop",
            "Documents": r"C:\Users\alex\OneDrive\Документы",
            "Downloads": r"C:\Users\alex\Downloads"}
    monkeypatch.setattr(ct, "known_folders", lambda os_name: fake)
    line = ct.owner_folders_line("windows")
    assert r"Desktop = C:\Users\alex\OneDrive\Desktop" in line
    assert r"Documents = C:\Users\alex\OneDrive\Документы" in line
    assert "use these exact paths" in line.lower()


def test_owner_folders_line_is_empty_when_nothing_resolves(monkeypatch):
    from brain import computer_tools as ct

    monkeypatch.setattr(ct, "known_folders", lambda os_name: {})
    assert ct.owner_folders_line("windows") == ""


@pytest.mark.skipif(sys.platform != "win32", reason="asks Windows for its known folders")
def test_known_folders_asks_windows_for_the_real_desktop():
    from brain import computer_tools as ct

    folders = ct.known_folders("windows")
    assert os.path.isdir(folders["Desktop"])
    assert set(folders) >= {"Desktop", "Documents", "Downloads"}
