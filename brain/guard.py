"""Deterministic safety guard for Claude's computer access (spec A2).

Every tool call Claude wants to make on the owner's PC or the cloud server is classified here as
RUN / ASK_UNLESS_BYPASS / ALWAYS_ASK / REFUSE.  The module is pure: no I/O, no DB, no Discord/SDK
imports.  It fails closed: anything it cannot confidently parse or classify is REFUSE, and an
unknown tool is REFUSE.  False asks are acceptable; a false RUN for anything in the spec's REFUSE or
ALWAYS_ASK tables is a bug.

Letters (A..I) in comments below refer to the rows of the blocklist table in the
computer-access design (A disks, B boot, C system folders, D registry, E download-and-run,
F security, G accounts/permissions, I Jarvis itself).

Shell tools: Claude Code exposes "Bash" (Git Bash on Windows, bash on Linux) and, where the
PowerShell tool is enabled, "PowerShell".  Both are handled; on Windows "Bash" is parsed with the
same rules (POSIX quoting, Windows paths accepted).
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
from dataclasses import dataclass
from enum import IntEnum


class Verdict(IntEnum):  # ordered by strictness
    RUN = 0
    ASK_UNLESS_BYPASS = 1
    ALWAYS_ASK = 2
    REFUSE = 3


@dataclass(frozen=True)
class GuardContext:
    machine: str  # "pc" or "server"
    os_name: str  # "windows" or "linux"
    home: str  # owner's home dir
    jarvis_dir: str  # Jarvis project dir on this machine
    bypass: bool = False  # bypass currently active (PC only; ignored on server)
    tainted: bool = False  # web/downloaded content read earlier in this task
    protected_packages: frozenset = frozenset()  # lowercase pip names from Jarvis's requirements.txt (rule I)
    cwd: str | None = None  # working directory of the tool calls (None -> home)
    # Jarvis's runtime: its Python (prefix, site-packages, sys.path dirs) and the Claude CLI folders.
    # Treated exactly like Jarvis's own folder (rule I): never written, moved into/out of, or trashed.
    protected_dirs: tuple = ()


@dataclass(frozen=True)
class GuardDecision:
    verdict: Verdict
    reason: str  # user-facing
    needs_approval: bool  # final answer after bypass/taint: True -> ask the owner
    summary: str  # one-line description of the action for the approval card


RUN = Verdict.RUN
AB = Verdict.ASK_UNLESS_BYPASS
ASK = Verdict.ALWAYS_ASK
REFUSE = Verdict.REFUSE

# ------------------------------------------------------------------ user-facing reasons
R_DEVICE = "device paths (\\\\?\\, \\\\.\\, \\??\\) aren't allowed"
R_DISK = "formatting or repartitioning disks is blocked"  # A
R_BOOT = "changing the boot configuration is blocked"  # B
R_CRITICAL = "deleting or overwriting a whole drive, your user folder or system folders is blocked"  # C
R_SYSTEM = "changing system folders is blocked"  # C
R_REGISTRY = "registry edits are blocked"  # D
R_DOWNLOAD_RUN = "downloading and running code is blocked"  # E
R_UNPARSEABLE = "this command is obfuscated or too complex to check safely, so it is blocked"  # spec: fail closed
R_SECURITY = "turning off security features is blocked"  # F
R_ACCOUNTS = "changing accounts, passwords, permissions or using sudo/admin rights is blocked"  # G
R_JARVIS = "Jarvis cannot change its own project, .env, service, scheduled task or database"  # I
R_DELETE = "raw delete commands are blocked - use move_to_trash"
R_SECRET = "this touches a secret or credential file, so it needs your approval"
R_SEND = "this sends data off the machine, so it needs your approval"
R_SERVER = "every command on the server needs your approval"
R_MOVE = "moving or renaming files needs your approval"
R_INSTALL = "installing or uninstalling software needs your approval"
R_OVERWRITE = "this may overwrite an existing file"
R_TAINT = "web or downloaded content was read in this task, so this needs your approval"
R_UNKNOWN_CWD = "I can't tell which folder this touches, so it needs your approval"

_MAX_DEPTH = 6
_UNK = "@unknown-dir@"


class _Unparseable(ValueError):
    pass


# ------------------------------------------------------------------ paths

_ENV_PATTERNS = [
    re.compile(r"%([A-Za-z_][A-Za-z_0-9()]*)%"),
    re.compile(r"\$\{env:([A-Za-z_0-9()]+)\}", re.I),
    re.compile(r"\$env:([A-Za-z_][A-Za-z_0-9]*)", re.I),
    re.compile(r"\$\{([A-Za-z_][A-Za-z_0-9]*)\}"),
    re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)"),
]


def _last_segment(path: str) -> str:
    return re.split(r"[\\/]+", path.rstrip("\\/"))[-1]


def _env_table(ctx: GuardContext) -> dict[str, str]:
    home = ctx.home
    user = _last_segment(home)
    if ctx.os_name == "windows":
        drive = home[:2] if re.match(r"^[A-Za-z]:", home) else "C:"
        return {
            "userprofile": home, "home": home, "homepath": home[2:] if home[1:2] == ":" else home,
            "homedrive": drive, "systemroot": drive + "\\Windows", "windir": drive + "\\Windows",
            "systemdrive": drive, "programfiles": drive + "\\Program Files",
            "programfiles(x86)": drive + "\\Program Files (x86)", "programw6432": drive + "\\Program Files",
            "programdata": drive + "\\ProgramData", "allusersprofile": drive + "\\ProgramData",
            "appdata": home + "\\AppData\\Roaming", "localappdata": home + "\\AppData\\Local",
            "temp": home + "\\AppData\\Local\\Temp", "tmp": home + "\\AppData\\Local\\Temp",
            "public": drive + "\\Users\\Public", "onedrive": home + "\\OneDrive", "username": user,
        }
    return {"home": home, "user": user, "logname": user}


def _expand(path: str, ctx: GuardContext) -> str:
    table = _env_table(ctx)

    def sub(match: re.Match) -> str:
        return table.get(match.group(1).lower(), match.group(0))

    for pat in _ENV_PATTERNS:
        path = pat.sub(sub, path)
    return path


def _has_unresolved(path: str) -> bool:
    return bool(re.search(r"%[^%\s]+%|\$[A-Za-z_{(:]|@SUBST@|`", path))


def _collapse_windows(p: str) -> str:
    """p is lowercase with forward slashes and an absolute prefix (x:/ or //)."""
    if p.startswith("//"):
        prefix, rest = "//", re.sub(r"/+", "/", p[2:])
    else:
        prefix, rest = p[:2], re.sub(r"/+", "/", p[2:])
    parts = [seg if seg in (".", "..") else (seg.rstrip(". ") or seg) for seg in rest.split("/")]
    norm = posixpath.normpath("/" + "/".join(parts))
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    if prefix == "//":
        return "//" + norm.lstrip("/")
    return prefix + ("/" if norm == "/" else norm)


def _strip_cf(text: str) -> str:
    """Drop invisible Unicode format characters (BOM, zero-width space/joiner, bidi marks ...)."""
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


_DEVICE_PREFIX = "//@device@/"  # normalized form of every NT/Win32 namespace path except X:\ and UNC\ ones
_DEVDOTS_PREFIX = "//@devdots@/"  # the same with a ".." component somewhere: refused for every tool
_NS_PREFIX = re.compile(r"^(//[?.]|/{1,2}\?\?)/")  # \?\  \.\  \??\  in any slash style (after "\" -> "/")


class _DeviceDots(Exception):
    "A namespace-prefixed path contains '..': Windows would resolve it somewhere we cannot predict."


def _is_device(p: str) -> bool:
    return p.startswith("//@dev")


def _is_devdots(p: str) -> bool:
    return p.startswith(_DEVDOTS_PREFIX)


def _normalize(path: str, ctx: GuardContext, cwd: str | None = None) -> str:
    p = _strip_cf(path).strip()
    while len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        p = p[1:-1].strip()
    p = _expand(p, ctx)
    if p == "~" or p.startswith("~/") or p.startswith("~\\"):
        p = ctx.home + p[1:]
    if ctx.os_name == "windows":
        p = p.replace("\\", "/").lower()
        m = _NS_PREFIX.match(p)
        if m:
            rest = p[m.end():]
            dotdot = ".." in rest.split("/")
            if m.group(1) == "//?" and not dotdot and re.match(r"[a-z]:(/|$)", rest):
                p = rest  # exactly \?\X:\...
            elif m.group(1) == "//?" and not dotdot and re.match(r"unc/", rest):
                p = "//" + rest[4:]  # \?\UNC\host\share
            else:  # a device namespace (GLOBALROOT, Volume{guid}, \??\, \.\): do not try to resolve it
                return (_DEVDOTS_PREFIX if dotdot else _DEVICE_PREFIX) + re.sub(r"/+", "/", rest.strip("/"))
        m = re.match(r"^/(?:mnt/)?([a-z])(?:/(.*))?$", p)
        if m:  # Git Bash (/c/Users) and WSL (/mnt/c/Users) spellings
            p = m.group(1) + ":/" + (m.group(2) or "")
        home = ctx.home.replace("\\", "/").lower()
        if re.match(r"^[a-z]:$", p):
            p += "/"
        elif re.match(r"^[a-z]:[^/]", p):
            p = p[:2] + "/" + p[2:]
        if not (re.match(r"^[a-z]:/", p) or p.startswith("//")):
            if p.startswith("/"):
                p = home[:2] + p
            else:
                base = cwd if cwd is not None else _cwd_of(ctx)
                p = base + "/" + p
        return _collapse_windows(p)
    if not p.startswith("/"):
        p = (cwd if cwd is not None else _cwd_of(ctx)) + "/" + p
    return posixpath.normpath("/" + p.lstrip("/"))


def normalize_path(path: str, ctx: GuardContext) -> str:
    """Absolute path, lowercase on windows, forward slashes, env vars and ~ expanded."""
    return _normalize(path, ctx)


def _cwd_of(ctx: GuardContext) -> str:
    """Where relative paths resolve: ctx.cwd when given, else the home folder."""
    root = "c:/" if ctx.os_name == "windows" else "/"
    if ctx.cwd:
        return _normalize(ctx.cwd, ctx, cwd=root)
    return _normalize(ctx.home, ctx, cwd=root)


def _home(ctx: GuardContext) -> str:
    return _normalize(ctx.home, ctx, cwd="c:/" if ctx.os_name == "windows" else "/")


def _jarvis(ctx: GuardContext) -> str:
    return _normalize(ctx.jarvis_dir, ctx, cwd="c:/" if ctx.os_name == "windows" else "/")


def _under(child: str, parent: str) -> bool:
    child, parent = child.lower(), parent.lower()
    parent = parent.rstrip("/")
    if parent == "":
        return child.startswith("/")
    return child == parent or child.startswith(parent + "/")


_WIN_SYSTEM = re.compile(  # C (system folders), B (boot/EFI)
    r"^[a-z]:/(windows|program files( \(x86\))?|programdata|system volume information|\$recycle\.bin|"
    r"recovery|perflogs|boot|efi)(/|$)|^[a-z]:/(pagefile|hiberfil|swapfile)\.sys$|^[a-z]:/(bootmgr|bootnxt)$"
)
_WIN_BOOT = re.compile(r"^[a-z]:/(boot|efi)(/|$)|^[a-z]:/(bootmgr|bootnxt)$")
_LNX_SYSTEM_PREFIXES = (  # C
    "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/var", "/opt", "/root",
    "/dev", "/proc", "/sys", "/srv", "/run", "/snap", "/lost+found", "/efi",
)
_LNX_SYSTEM_EXCEPTIONS = ("/var/tmp", "/run/user")


def _boot_path(p: str, ctx: GuardContext) -> bool:
    if ctx.os_name == "windows":
        return bool(_WIN_BOOT.match(p))
    return _under(p, "/boot") or _under(p, "/efi")


def _system_under(p: str, ctx: GuardContext) -> bool:
    if ctx.os_name == "windows":
        return bool(_WIN_SYSTEM.match(p))
    if any(_under(p, e) for e in _LNX_SYSTEM_EXCEPTIONS):
        return False
    return any(_under(p, s) for s in _LNX_SYSTEM_PREFIXES)


def _protected_dirs(ctx: GuardContext) -> list[str]:
    root = "c:/" if ctx.os_name == "windows" else "/"
    home = _home(ctx)
    out = []
    for d in ctx.protected_dirs:
        if not isinstance(d, str) or not d.strip():
            continue
        n = _normalize(d, ctx, cwd=root)
        if not _under(home, n):  # never let a sys.path entry that contains the whole home lock it
            out.append(n)
    return out


def _jarvis_under(p: str, ctx: GuardContext) -> bool:
    """Inside Jarvis's folder or its runtime (rule I)."""
    return _under(p, _jarvis(ctx)) or any(_under(p, d) for d in _protected_dirs(ctx))


def _contains_protected(p: str, ctx: GuardContext) -> bool:
    """p is Jarvis's folder/runtime itself or a parent of it."""
    return _under(_jarvis(ctx), p) or any(_under(d, p) for d in _protected_dirs(ctx))


def _critical(p: str, ctx: GuardContext, include_system: bool = True) -> bool:
    """Whole drives, the user folder (and its parents), system folders: never delete/move/overwrite."""
    home = _home(ctx)
    if _under(home, p):  # the home dir itself or any parent of it (incl. drive root)
        return True
    if ctx.os_name == "windows":
        if re.match(r"^[a-z]:/?$", p) or re.match(r"^[a-z]:/users(/[^/]+)?$", p):
            return True
    else:
        if p in ("/", "/home", "/mnt", "/media"):
            return True
        if re.match(r"^/(home|mnt)/[^/]+$|^/media(/[^/]+){1,2}$", p):
            return True
    return include_system and _system_under(p, ctx)


def _write_block(p: str, ctx: GuardContext) -> str | None:
    if _is_device(p):
        return R_DEVICE
    if _boot_path(p, ctx):
        return R_BOOT
    if _system_under(p, ctx):
        return R_SYSTEM
    if _jarvis_under(p, ctx):
        return R_JARVIS
    return None


def _delete_block(p: str, ctx: GuardContext) -> str | None:
    if _is_device(p):
        return R_DEVICE
    if _boot_path(p, ctx):
        return R_BOOT
    if _critical(p, ctx):
        return R_CRITICAL
    if _jarvis_under(p, ctx) or _contains_protected(p, ctx):
        return R_JARVIS
    return None


# Secret files: always ask (spec "ALWAYS_ASK / Reading secret files").  Matched on lowercase
# normalized paths, so they are case-insensitive on Windows and effectively on Linux too.
_SECRET_BASENAMES = [
    r"\.key$", r"\.pem$", r"^id_", r"^\.env($|\.)", r"\.env$", r"^\.envrc$",  # keys, .env / .env.*
    r"^wallet\.dat$", r"\.kdbx?$", r"\.(pfx|p12|ppk|jks|keystore)$",  # wallets, password managers, certs
    r"^\.netrc$", r"^_netrc$", r"^\.git-credentials$", r"^\.npmrc$", r"^\.pypirc$", r"^\.pgpass$",
    r"^authorized_keys$", r"^(shadow|gshadow|sudoers|master\.passwd)$",
    r"^(login data( for account)?|cookies(-journal)?|web data|logins\.json|key[34]\.db|cookies\.sqlite|local state)$",
    r"^(credentials|secrets?|client_secrets?)\.(json|ya?ml|toml|ini|txt)$", r"\.credentials\.json$",
    r"^pgpass\.conf$", r"^hosts\.yml$",
]
_SECRET_PATHS = [
    r"/\.ssh/", r"/\.gnupg/", r"/\.aws/", r"/\.kube/", r"/\.azure/", r"/\.config/gcloud/", r"/\.password-store/",
    r"/\.docker/config\.json/",
    # browser profiles
    r"/user data/", r"/google/chrome/", r"/microsoft/edge/", r"/bravesoftware/", r"/chromium/",
    r"/google-chrome/", r"/microsoft-edge/", r"/mozilla/firefox/", r"/\.mozilla/", r"/opera software/",
    r"/vivaldi/", r"/\.config/(opera|vivaldi|brave)", r"/snap/(firefox|chromium)/",
    # password managers
    r"/bitwarden/", r"/1password/", r"/lastpass/", r"/keepass", r"/dashlane/", r"/nordpass/", r"/enpass/",
    r"/proton ?pass/",
    # crypto wallets / extension storage
    r"/electrum/", r"/\.electrum/", r"/exodus/", r"/metamask", r"/nkbihfbeogaeaoehlefnkodbefgpgknn/",
    r"/\.bitcoin/", r"/\.ethereum/", r"/ledger live/", r"/monero", r"/wasabi", r"/local extension settings/",
    # Windows credential stores
    r"/microsoft/credentials/", r"/microsoft/protect/", r"/microsoft/vault/", r"/system32/config/",
    # Linux system secrets
    r"^/etc/(shadow|gshadow|sudoers|ssh/ssh_host)", r"^/proc/.*/(environ|cmdline|mem)/",
    # tokens of other tools (I6)
    r"/\.config/gh/", r"/github cli/", r"/discord(?:canary|ptb)?/local storage/", r"/(?:code|code - insiders|cursor|vscodium|windsurf)/user/globalstorage/",
    r"/telegram desktop/tdata/", r"/\.claude/\.credentials\.json/",
]
_SECRET_BASE_RES = [re.compile(r) for r in _SECRET_BASENAMES]
_SECRET_PATH_RES = [re.compile(r) for r in _SECRET_PATHS]

# Persistence spots: writing there means code runs later without another look.
_PERSIST_RES = [re.compile(r) for r in [
    r"/start menu/programs/startup/", r"/\.bashrc/", r"/\.bash_profile/", r"/\.zshrc/", r"/\.profile/",
    r"/\.config/autostart/", r"/\.config/systemd/", r"/\.gitconfig/", r"/\.git/config/", r"/\.git/hooks/",
    r"/documents/(windows)?powershell/", r"/\.claude/settings[^/]*\.json/", r"/\.ssh/",
    # more places where a write makes code or config run later (I7)
    r"/\.config/git/", r"/site-packages/[^/]*\.pth/", r"/(usercustomize|sitecustomize)\.py/",
    r"/\.bash_login/", r"/\.bash_logout/", r"/\.zprofile/", r"/\.zshenv/", r"/\.config/fish/", r"/\.xprofile/",
    r"/\.xinitrc/", r"/\.config/environment\.d/", r"/\.pam_environment/", r"/\.vscode/(tasks|settings)\.json/",
    r"/\.npmrc/", r"/pip\.(ini|conf)/", r"/\.local/share/systemd/user/",
]]


def _is_secret(p: str, windows: bool | None = None) -> bool:
    low = p.lower()
    if re.search(r"^[a-z]:/|^//", low) and re.search(r"/[^/]*~\d[^/]*(/|$)", low):
        return True  # 8.3 short names (ENV~1, SSH~1) can hide a secret: treat as possibly secret
    base = low.rstrip("/").rsplit("/", 1)[-1]
    bases = {base, base.replace("*", ""), base.replace("*", "x").replace("?", "x")}
    if any(rx.search(b) for rx in _SECRET_BASE_RES for b in bases):
        return True
    padded = low.rstrip("/") + "/"
    return any(rx.search(padded) for rx in _SECRET_PATH_RES)


def _is_persist(p: str, ctx: GuardContext | None = None) -> bool:
    """Auto-start / config locations where a write can make code run later (ALWAYS_ASK)."""
    padded = p.lower().rstrip("/") + "/"
    if any(rx.search(padded) for rx in _PERSIST_RES):
        return True
    return ctx is not None and _under(p, _home(ctx) + "/.claude")


def _is_unc(p: str) -> bool:
    """Network (UNC) or device-namespace path: reading it can leak credentials or reach other volumes."""
    return p.startswith("//")


# ------------------------------------------------------------------ shell scanning

@dataclass
class _Seg:
    text: str
    pipe_in: bool


_SEP_CHARS = ";&|\n\r(){}"
_DASHES = re.compile("[\u2010-\u2015\u2212]")  # PowerShell and Git Bash treat these like "-" in options
_SUBST = "$(@SUBST@)"


def _match_paren(s: str, i: int, posix_escape: bool) -> int:
    depth = 0
    q = None
    j = i
    while j < len(s):
        ch = s[j]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and posix_escape:
                j += 1
        else:
            if ch in "'\"":
                q = ch
            elif ch == "\\" and posix_escape:
                j += 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    raise _Unparseable("unbalanced parentheses")


def _scan(cmd: str, os_name: str, out: list[_Seg], depth: int = 0, cmd_mode: bool = False) -> None:
    """Split a command into sub-commands (incl. $(...), `...` bodies) appending to out."""
    if depth > _MAX_DEPTH:
        raise _Unparseable("nested too deeply")
    cmd = _DASHES.sub("-", _strip_cf(cmd))
    win = os_name == "windows"
    if win:
        cmd = cmd.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    esc = "^" if cmd_mode else ("`^" if win else "\\")
    posix_escape = not win
    n = len(cmd)
    i = 0
    cur: list[str] = []
    q = None
    seg_pipe = False
    run = ""

    def flush() -> None:
        text = "".join(cur).strip()
        cur.clear()
        if text:
            out.append(_Seg(text, seg_pipe))

    while i < n:
        c = cmd[i]
        nxt = cmd[i + 1] if i + 1 < n else ""
        if q is None:
            if c in esc:
                cur.append(c + nxt)
                i += 2
                run = ""
                continue
            if c == "'" and not cmd_mode:
                q = "'"
                cur.append(c)
                i += 1
                run = ""
                continue
            if c == '"':
                q = '"'
                cur.append(c)
                i += 1
                run = ""
                continue
            if (c == "$" or c in "<>") and nxt == "(":
                j = _match_paren(cmd, i + 1, posix_escape)
                _scan(cmd[i + 2:j], os_name, out, depth + 1, cmd_mode)
                cur.append(_SUBST)
                i = j + 1
                run = ""
                continue
            if c == "`" and not win:
                j = cmd.find("`", i + 1)
                if j < 0:
                    raise _Unparseable("unbalanced backtick")
                _scan(cmd[i + 1:j], os_name, out, depth + 1, cmd_mode)
                cur.append(_SUBST)
                i = j + 1
                run = ""
                continue
            if c == "$" and nxt == "{":
                j = cmd.find("}", i)
                if j < 0:
                    raise _Unparseable("unbalanced brace")
                cur.append(cmd[i:j + 1])
                i = j + 1
                run = ""
                continue
            if c == "&" and ((i > 0 and cmd[i - 1] in "<>") or nxt == ">"):
                cur.append(c)
                i += 1
                continue
            if c in _SEP_CHARS:
                if "".join(cur).strip() == "" and run:
                    run += c
                else:
                    flush()
                    run = c
                cur.clear()
                seg_pipe = run.replace("\n", "").replace("\r", "") in ("|", "|&")
                i += 1
                continue
            cur.append(c)
            if not c.isspace():
                run = ""
            i += 1
        elif q == "'":
            cur.append(c)
            if c == "'":
                q = None
            i += 1
        else:  # inside double quotes
            if c in esc and nxt:
                cur.append(c + nxt)
                i += 2
                continue
            if c == "$" and nxt == "(":
                j = _match_paren(cmd, i + 1, posix_escape)
                _scan(cmd[i + 2:j], os_name, out, depth + 1, cmd_mode)
                cur.append(_SUBST)
                i = j + 1
                continue
            if c == "`" and not win:
                j = cmd.find("`", i + 1)
                if j < 0:
                    raise _Unparseable("unbalanced backtick")
                _scan(cmd[i + 1:j], os_name, out, depth + 1, cmd_mode)
                cur.append(_SUBST)
                i = j + 1
                continue
            cur.append(c)
            if c == '"':
                q = None
            i += 1
    if q is not None:
        raise _Unparseable("unterminated quote")
    flush()


class _Op(str):
    """A redirection operator token."""


def _tokenize(seg: str, os_name: str, cmd_mode: bool = False) -> list[str]:
    seg = _DASHES.sub("-", _strip_cf(seg))
    win = os_name == "windows"
    esc = "^" if cmd_mode else ("`^" if win else "\\")
    tokens: list[str] = []
    cur: list[str] = []
    has = False
    q = None
    i = 0
    n = len(seg)

    def push() -> None:
        nonlocal has
        if has:
            tokens.append("".join(cur))
        cur.clear()
        has = False

    while i < n:
        c = seg[i]
        nxt = seg[i + 1] if i + 1 < n else ""
        if q is None:
            if c in esc:
                if nxt in ("\n", "\r") and not win:
                    i += 2
                    continue
                if nxt:
                    cur.append(nxt)
                    has = True
                i += 2
                continue
            if c == "'" and not cmd_mode:
                q = "'"
                has = True
            elif c == '"':
                q = '"'
                has = True
            elif c.isspace():
                push()
            elif c in "<>":
                word = "".join(cur)
                if has and re.fullmatch(r"\d+|[*&]", word):
                    cur.clear()
                    has = False
                else:
                    push()
                op = c
                if c == ">" and nxt in (">", "|"):
                    op += nxt
                    i += 1
                tokens.append(_Op(op))
            else:
                cur.append(c)
                has = True
            i += 1
        elif q == "'":
            if c == "'":
                q = None
            else:
                cur.append(c)
            i += 1
        else:
            if c in esc and nxt and (nxt in '"\\$`' or win):
                cur.append(nxt)
                i += 2
                continue
            if c == '"':
                q = None
            else:
                cur.append(c)
            i += 1
    if q is not None:
        raise _Unparseable("unterminated quote")
    push()
    return tokens


def _requote(tok: str, cmd_mode: bool = False) -> str:
    if tok and not re.search(r"[\s;&|(){}<>\"'$`^]", tok):
        return tok
    if cmd_mode or "'" in tok:
        return '"' + tok.replace('"', '\\"') + '"'
    return "'" + tok + "'"


def split_shell(command: str, os_name: str) -> list[str]:
    """Every sub-command, including the bodies of nested -c/-Command/cmd /c wrappers and $(...)."""
    out: list[_Seg] = []
    _scan(command, os_name, out)
    result: list[str] = []
    _collect(out, os_name, result, 0)
    return result


def _collect(segs: list[_Seg], os_name: str, result: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise _Unparseable("nested too deeply")
    for seg in segs:
        result.append(seg.text)
        body = _nested_body(seg.text, os_name)
        if body is not None:
            inner: list[_Seg] = []
            _scan(body[0], os_name, inner, depth + 1, body[1])
            _collect(inner, os_name, result, depth + 1)


_POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"}
_PS_SHELLS = {"powershell", "pwsh", "powershell_ise"}
_PS_VALUE_OPTS = re.compile(r"^-(ex\w*|ep|w(i\w*)?|wo\w*|wd|in\w*|ou\w*|ve\w*|ps\w*|conf\w*|se\w*)$", re.I)
_PS_COMMAND_OPT = re.compile(r"^-c(o(m(m(a(n(d)?)?)?)?)?)?$", re.I)
_PS_ENCODED_OPT = re.compile(r"^-e(c|n\w*)?$", re.I)
_PS_FILE_OPT = re.compile(r"^-f(i(l(e)?)?)?$", re.I)


def _basename_lower(word: str) -> str:
    base = re.split(r"[\\/]", word)[-1].lower()
    for ext in (".exe", ".com"):
        if base.endswith(ext) and len(base) > len(ext):
            return base[: -len(ext)]
    return base


def _nested_body(seg_text: str, os_name: str) -> tuple[str, bool] | None:
    """(body, cmd_mode) when the segment is a nested shell invocation, else None."""
    try:
        toks = [t for t in _tokenize(seg_text, os_name) if not isinstance(t, _Op)]
    except _Unparseable:
        return None
    toks = _strip_prefix(toks)
    if not toks:
        return None
    return _wrapper_body(_basename_lower(toks[0]), toks[1:], os_name)


def _explode_args(args: list[str], os_name: str) -> list[str]:
    out: list[str] = []
    for a in args:
        if a[:1] in ("-", "/") and re.search(r"\s", a):
            try:
                out.extend(t for t in _tokenize(a, os_name) if not isinstance(t, _Op))
                continue
            except _Unparseable:
                pass
        out.append(a)
    return out


def _wrapper_body(name: str, args: list[str], os_name: str = "windows") -> tuple[str, bool] | None:
    args = _explode_args(args, os_name)
    if name in _POSIX_SHELLS:
        for idx, a in enumerate(args):
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a):
                return (args[idx + 1], False) if idx + 1 < len(args) else ("", False)
        return None
    if name in _PS_SHELLS:
        i = 0
        while i < len(args):
            a = args[i]
            if _PS_COMMAND_OPT.match(a):
                rest = args[i + 1:]
                return (rest[0] if len(rest) == 1 else " ".join(_requote(t) for t in rest), False)
            if _PS_ENCODED_OPT.match(a) or _PS_FILE_OPT.match(a):
                return None
            if a.startswith("-"):
                i += 2 if _PS_VALUE_OPTS.match(a) else 1
                continue
            rest = args[i:]
            return (rest[0] if len(rest) == 1 else " ".join(_requote(t) for t in rest), False)
        return None
    if name == "cmd":
        for idx, a in enumerate(args):
            m = re.fullmatch(r"(?i)/[ckr](?:\s+(.*))?", a, re.S)
            if m:
                head = [m.group(1)] if m.group(1) else []
                rest = head + list(args[idx + 1:])
                if len(rest) == 1:
                    return rest[0], True
                return " ".join(_requote(t, True) for t in rest), True
        return None
    return None


def _strip_prefix(words: list[str]) -> list[str]:
    i = 0
    while i < len(words):
        w = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", w) and "$(" not in w:
            i += 1
            continue
        if w.lower() in ("then", "else", "elif", "do", "while", "until", "!", "time"):
            i += 1
            continue
        break
    return words[i:]


# ------------------------------------------------------------------ rule tables

# A - disks/partitions
_DISK_CMDS = {
    "format", "diskpart", "mke2fs", "mkswap", "mkntfs", "mkdosfs", "mkexfatfs", "fdisk", "sfdisk", "cfdisk",
    "gdisk", "sgdisk", "cgdisk", "parted", "gparted", "wipefs", "blkdiscard", "format-volume", "clear-disk",
    "initialize-disk", "new-partition", "remove-partition", "resize-partition", "set-partition", "set-disk",
    "lvremove", "vgremove", "pvremove", "lvcreate", "vgcreate", "pvcreate", "lvextend", "lvreduce", "lvresize",
    "cryptsetup", "mdadm", "zpool", "hdparm", "badblocks", "clear-volume", "remove-volume",
}
# B - boot configuration
_BOOT_CMDS = {
    "bcdedit", "bcdboot", "bootrec", "bootsect", "grub-install", "grub2-install", "update-grub", "update-grub2",
    "grub-mkconfig", "grub2-mkconfig", "grub-set-default", "grub-reboot", "efibootmgr", "os-prober",
    "update-initramfs", "mkinitcpio", "dracut", "mountvol", "reagentc", "sbctl", "mokutil", "kernel-install",
}
# raw deletes - use move_to_trash
_DELETE_CMDS = {
    "rm", "rmdir", "del", "erase", "rd", "ri", "remove-item", "unlink", "shred", "srm", "sdelete", "sdelete64",
    "wipe", "clear-recyclebin", "cipher",
}
_MOVE_CMDS = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "rni", "git-mv"}  # ask
_COPY_CMDS = {
    "cp", "copy", "xcopy", "robocopy", "copy-item", "cpi", "install", "rsync", "ln", "mklink", "scp-local",
}
_TRUNC_CMDS = {
    "set-content", "out-file", "clear-content", "clc", "export-csv", "export-clixml", "truncate", "tee", "tee-object",
}
_APPEND_CMDS = {"add-content", "ac", "touch", "mkdir", "md", "new-item", "ni", "sed", "sc-content"}
_PERM_CMDS = {"chmod", "chown", "chgrp", "setfacl", "chattr", "attrib"}  # G on system paths
_READ_CMDS = {
    "ls", "dir", "gci", "get-childitem", "cat", "type", "gc", "get-content", "more", "less", "head", "tail", "bat",
    "strings", "xxd", "hexdump", "od", "grep", "egrep", "fgrep", "rg", "ag", "findstr", "select-string", "sls",
    "wc", "du", "df", "stat", "file", "tree", "pwd", "echo", "write-output", "write-host", "where", "which",
    "get-command", "gcm", "get-item", "gi", "get-itemproperty", "gp", "get-itempropertyvalue", "gpv", "test-path",
    "resolve-path", "get-process", "gps", "ps", "top", "htop", "whoami", "hostname", "date", "get-date", "sort",
    "sort-object", "uniq", "cut", "awk", "gawk", "diff", "fc", "comp", "md5sum", "sha1sum", "sha256sum",
    "get-filehash", "printf", "true", "false", "cd", "chdir", "sl", "set-location", "pushd", "popd",
    "push-location", "pop-location", "select-object", "where-object", "measure-object", "format-table", "ft", "fl",
    "format-list", "out-string", "out-null", "get-service", "get-volume", "get-disk", "get-psdrive", "get-partition",
    "systeminfo", "ipconfig", "ping", "tasklist", "uname", "free", "uptime", "lsblk", "lscpu", "printenv",
    "id", "groups", "basename", "dirname", "realpath", "readlink", "nslookup", "tracert", "traceroute", "netstat",
    "journalctl", "dmesg", "lsof", "history", "tr", "xargs-echo", "jq", "yq", "sleep", "start-sleep", "clear", "cls",
    "get-computerinfo",
    "get-scheduledtask", "get-winevent", "get-eventlog", "get-hotfix", "get-netipaddress", "get-random",
    "convertto-json", "convertfrom-json", "convertto-csv", "import-csv", "get-member", "get-alias", "get-help",
    "get-module", "get-host", "get-location", "get-wmiobject", "get-ciminstance", "code", "explorer", "notepad",
    "xdg-open", "open", "set-variable", "new-variable", "export-alias",
}
# wrappers whose remaining words are another command
_WRAPPERS = {
    "if", "for", "foreach", "while", "until", "else", "elif", "do", "then", "time", "nohup", "command", "builtin",
    "exec", "setsid", "nice", "ionice", "timeout", "stdbuf", "watch", "caffeinate", "unbuffer", "flock", "chroot",
    "strace", "ltrace", "xargs", "env", "wsl", "busybox", "call", "start", "parallel", "taskset", "numactl",
    "proxychains", "torify", "foreach-object", "%", "where-object", "?", "invoke-command", "icm", "start-job",
    "sajb", "start-threadjob", "measure-command", "trace-command", "&", ".", "source", "nohup.exe",
}
# sudo-like privilege escalation
_SUDO_CMDS = {"sudo", "doas", "pkexec", "gsudo", "runas", "su", "sudoedit", "run0"}  # G
_CMD_NAMES_EXEC_DIRECT = {"iex", "invoke-expression", "eval"}  # E
_INTERPRETERS_STRONG = {"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "pwsh", "powershell", "cmd", "iex",
                        "invoke-expression", "sudo", "source", "."}
_INTERPRETERS_WEAK = {"python", "python3", "py", "perl", "ruby", "node", "nodejs", "php", "lua", "deno", "bun"}
_DOWNLOADERS = {
    "curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod", "start-bitstransfer", "bitsadmin",
    "aria2c", "axel", "fetch",
}
_EXEC_EXT = (".exe", ".msi", ".ps1", ".sh", ".bat", ".cmd", ".com", ".vbs", ".scr", ".jar", ".run", ".appimage")
_SCRIPT_RUNNERS = _POSIX_SHELLS | _PS_SHELLS | _INTERPRETERS_WEAK | {"cscript", "wscript", "java"}
_LAUNCHERS = {"start", "start-process", "saps", "invoke-item", "ii", "msiexec", "rundll32", "regsvr32", "mshta",
              "explorer-run"}

# D - registry
_REGISTRY_TOKEN = re.compile(
    r"(?<![\w])(hk(?:lm|cu|cr|u|cc):|hkey_(?:local_machine|current_user|classes_root|users|current_config)|"
    r"(?:microsoft\.powershell\.core\\)?registry::)", re.I)
_REG_READ_VERBS = {"query", "export", "compare"}
_REG_CMDS = {"reg", "regedit", "regedt32", "regini", "regsvr32", "mshta"}
_PS_REGISTRY_READERS = _READ_CMDS | {"select-string", "get-itemproperty"}

# F - security
_SECURITY_CMDS = {
    "set-mppreference", "add-mppreference", "remove-mppreference", "set-netfirewallprofile",
    "disable-netfirewallrule", "new-netfirewallrule", "remove-netfirewallrule", "set-netfirewallrule",
    "set-executionpolicy", "aa-teardown", "aa-disable", "aa-complain", "aa-audit", "setenforce",
    "disable-windowsoptionalfeature", "set-ruleoption", "disable-computerrestore",
}
_SECURITY_SERVICES = re.compile(
    r"windefend|mpssvc|wdnissvc|wdboot|wdfilter|securityhealth|wscsvc|sense\b|firewalld|\bufw\b|apparmor|auditd|"
    r"selinux|defender", re.I)

# G - accounts, passwords, permissions
_ACCOUNT_CMDS = {
    "takeown", "passwd", "chpasswd", "useradd", "usermod", "userdel", "adduser", "deluser", "groupadd", "groupmod",
    "groupdel", "addgroup", "delgroup", "visudo", "vipw", "vigr", "chage", "gpasswd", "newusers", "new-localuser",
    "set-localuser", "remove-localuser", "enable-localuser", "disable-localuser", "rename-localuser",
    "add-localgroupmember", "remove-localgroupmember", "new-localgroup", "set-localgroup", "remove-localgroup",
    "set-adaccountpassword", "dsadd", "mimikatz", "ntdsutil", "net1", "lusrmgr",
}
_NET_ACCOUNT_SUBS = {"user", "users", "localgroup", "accounts", "group", "admin"}

# I - Jarvis itself
_JARVIS_RE = re.compile(r"jarvis", re.I)
_PG_RE = re.compile(r"postgres|postmaster|pgsql|pg_ctl", re.I)
_PG_CMDS = {"pg_ctl", "pg_ctlcluster", "pg_dropcluster", "dropdb", "dropuser", "initdb", "pg_upgradecluster",
            "pg_createcluster"}
_SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "invoke-sqlcmd", "mongosh", "redis-cli"}
_SQL_DESTRUCTIVE = re.compile(r"\b(drop|truncate|delete|alter)\b", re.I)
_KILL_CMDS = {"kill", "pkill", "killall", "taskkill", "stop-process", "spps"}
_SERVICE_MUTATING = {"stop", "disable", "mask", "kill", "edit", "revert", "set-property", "delete", "config",
                     "pause", "failure", "remove", "unmask", "link", "disable-service", "create"}
_SERVICE_RESTARTISH = {"start", "restart", "reload", "try-restart", "reload-or-restart", "enable", "continue"}
_SC_VERBS = {"query", "queryex", "qc", "start", "stop", "config", "create", "delete", "failure", "description",
             "pause", "continue", "interrogate", "control", "sdset", "sidtype", "privs", "triggerinfo",
             "qfailure", "qdescription", "qprivs", "qsidtype", "qtriggerinfo", "getdisplayname", "getkeyname",
             "enumdepend", "sdshow", "lock", "qc"}
_KNOWN_TOOLS = (
    _DOWNLOADERS | _READ_CMDS | _DISK_CMDS | _DELETE_CMDS | _MOVE_CMDS | _COPY_CMDS | _PERM_CMDS | _SQL_CLIENTS |
    _KILL_CMDS | _BOOT_CMDS | _ACCOUNT_CMDS | _SECURITY_CMDS |
    {"git", "reg", "netsh", "sc", "net", "schtasks", "find", "sed", "tar", "zip", "unzip", "certutil", "robocopy",
     "xcopy", "ufw", "icacls", "takeown", "attrib"}
)
_TASK_DESTRUCTIVE = {"unregister-scheduledtask", "disable-scheduledtask", "stop-scheduledtask",
                     "set-scheduledtask"}
_TASK_CREATE = {"register-scheduledtask", "new-service", "systemd-run", "at", "batch", "launchctl",
                "register-objectevent", "register-wmievent", "set-wmiinstance"}

# sending data off the machine (ALWAYS_ASK)
# installs (ASK_UNLESS_BYPASS)
# raw (whole-command) patterns that are refused wherever they appear
_RAW_REFUSE = [
    (re.compile(r"(?i)invoke-expression"), R_UNPARSEABLE),  # E
    (re.compile(r"(?i)\[\s*scriptblock\s*\]\s*::\s*create"), R_UNPARSEABLE),
    (re.compile(r"(?i)frombase64string"), R_UNPARSEABLE),
    (re.compile(r"(?i)\$executioncontext"), R_UNPARSEABLE),
    (re.compile(r"(?i)\.\s*delete\s*\(|\]\s*::\s*delete\b"), R_DELETE),
    (re.compile(r"(?i)wscript\.shell|shell\.application|scripting\.filesystemobject"), R_UNPARSEABLE),
    (re.compile(r"(?i)process\s*\]\s*::\s*start"), R_UNPARSEABLE),
    (re.compile(r"(?i)win32_process|win32_service|invoke-wmimethod|invoke-cimmethod"), R_UNPARSEABLE),
    (re.compile(r"(?i)lsass"), R_ACCOUNTS),
    (re.compile(r"(?i)\\\\\.\\"), R_DISK),  # \\.\PhysicalDrive0, \\.\C:  raw devices (A)
    (re.compile(r"(?i)(?<!&)&(?!&)\s*[$(]"), R_UNPARSEABLE),  # PowerShell call operator on a computed name
    (re.compile(r"(?i)(?:^|[\s;|&(])(?:\.)\s+[$(]"), R_UNPARSEABLE),
    (re.compile(r"(?i)(?:%|foreach-object|foreach)\s+(?:-membername\s+)?(?:delete|remove)\b"), R_DELETE),
    (re.compile(r"(?i)(\$\(|`|<\()[^)`]*base64\s+(-d|--decode|-D)\b"), R_UNPARSEABLE),  # base64 decode -> run
    (re.compile(r"(?i)(?:>>?|of=|tee(?:\s+-a)?\s+)\s*/dev/(?!null\b|zero\b|stdout\b|stderr\b|stdin\b|tty\b|fd/|"
                r"urandom\b|random\b)"), R_DISK),  # A: writing to a device
    (re.compile(r"(?i)(?:^|[\s;|&(])(?:bash|sh|zsh|dash|ksh|source|eval|\.)\s[^\n;|&]*<<"), R_UNPARSEABLE),
    (re.compile(r"(?i)(?:^|[\s;|&(])(?:source|\.)\s+/dev/(?:stdin|fd/)"), R_UNPARSEABLE),
    (re.compile(r"(?i)\bnew-psdrive\b.*registry"), R_REGISTRY),
    (re.compile(r"(?i)microsoft\.win32\.registry"), R_REGISTRY),
]
_RAW_DOWNLOADER = re.compile(r"(?i)downloadstring|downloadfile|downloaddata|new-object\s+net\.webclient|"
                             r"system\.net\.webclient|system\.net\.http\.httpclient|start-bitstransfer")
# ------------------------------------------------------------------ evaluation state

class _Acc:
    """Collects blocklist findings.  The shell evaluator is blocklist-only: only REFUSE findings are kept;
    everything else about a shell command is decided by the allow-list (see _shell_decision)."""

    def __init__(self) -> None:
        self.items: list[tuple[Verdict, str]] = []

    def add(self, verdict: Verdict, reason: str) -> None:
        if verdict == REFUSE:
            self.items.append((verdict, reason))

    def result(self) -> tuple[Verdict, str]:
        if not self.items:
            return RUN, "safe to run"
        best = max(v for v, _ in self.items)
        for v, r in self.items:
            if v == best:
                return v, r
        return RUN, "safe to run"


class _State:
    def __init__(self, ctx: GuardContext, tool: str) -> None:
        self.ctx = ctx
        self.posix = tool == "Bash" or ctx.os_name != "windows"
        unknown_base = "c:/" if ctx.os_name == "windows" else "/"
        self.cwd: str | None = _cwd_of(ctx)
        self.unknown_cwd = unknown_base + _UNK
        self.mentions_jarvis = False
        self.downloader = False
        self.weak_pipe = False
        self.exec_like = False

    def norm(self, raw: str) -> str:
        cwd = self.cwd if self.cwd is not None else self.unknown_cwd
        p = _normalize(raw, self.ctx, cwd=cwd)
        if _jarvis_under(p, self.ctx):
            self.mentions_jarvis = True
        return p


# ------------------------------------------------------------------ argument parsing

def _is_option(tok: str, st: _State) -> bool:
    if tok.startswith("-") and len(tok) > 1:
        return True
    if not st.posix or (st.ctx.os_name == "windows" and not st.posix):
        if re.fullmatch(r"/[A-Za-z?]{1,2}([:=].*)?", tok) or re.fullmatch(r"/[A-Za-z]+:.*", tok):
            return True
    return False


class _Info:
    def __init__(self) -> None:
        self.pos: list[str] = []
        self.opts: list[str] = []
        self.cands: list[str] = []


def _parse_args(args: list[str], st: _State) -> _Info:
    info = _Info()
    for a in args:
        if not a or a == "--":
            continue
        if _is_option(a, st):
            info.opts.append(a.lower())
            m = re.match(r"^(-{1,2}[\w-]+|/[\w-]+)[=:](.+)$", a)
            if m:
                info.cands.append(m.group(2))
            continue
        info.pos.append(a)
        if re.match(r"^\w+=", a):
            info.cands.append(a.split("=", 1)[1])
        else:
            info.cands.append(a)
        if "," in a and not a.startswith("-"):
            info.cands.extend(p for p in a.split(",") if p)
    return info


def _pathlike(tok: str) -> bool:
    return bool(re.search(r"[\\/]", tok) or re.match(r"^[A-Za-z]:", tok) or tok.startswith("~")
                or tok.startswith("%") or re.match(r"^\$(env:|\{)", tok, re.I))


def _ps_param(args: list[str], prefix_min: str, full: str) -> str | None:
    """Value of a PowerShell named parameter, allowing unambiguous prefixes (-Dest, -Destination)."""
    for i, a in enumerate(args):
        if a.startswith("-"):
            key = re.split(r"[=:]", a[1:], maxsplit=1)[0].lower()
            if len(key) >= len(prefix_min) and full.startswith(key) and key.startswith(prefix_min):
                if re.search(r"[=:]", a[1:]) and len(re.split(r"[=:]", a, maxsplit=1)) > 1 and re.split(r"[=:]", a, maxsplit=1)[1]:
                    return re.split(r"[=:]", a, maxsplit=1)[1]
                return args[i + 1] if i + 1 < len(args) else None
    return None


# ------------------------------------------------------------------ command evaluation

class _Evaluator:
    def __init__(self, ctx: GuardContext, tool: str) -> None:
        self.ctx = ctx
        self.st = _State(ctx, tool)
        self.acc = _Acc()

    # ---- path helpers
    def path(self, raw: str) -> str:
        return self.st.norm(raw)

    def check_secret(self, cands: list[str]) -> None:
        for c in cands:
            if not c or c.startswith("-"):
                continue
            try:
                p = self.path(c)
            except Exception:
                continue
            _ = p  # normalizing records whether the command mentions Jarvis's folder

    def write_target(self, raw: str, *, truncate: bool = False) -> None:
        if not raw:
            return
        p = self.path(raw)
        if _UNK in p:
            self.acc.add(ASK, R_UNKNOWN_CWD)
            return
        blk = _write_block(p, self.ctx)
        if blk:
            self.acc.add(REFUSE, blk)
            return
        if _is_secret(p):
            self.acc.add(ASK, R_SECRET)
        if _is_unc(p):
            self.acc.add(ASK, R_SEND)
        if _is_persist(p):
            self.acc.add(ASK, "changing startup or shell-profile files needs your approval")
        if truncate:
            self.acc.add(AB, R_OVERWRITE)

    def delete_target(self, raw: str) -> None:
        if not raw:
            return
        p = self.path(raw)
        if _UNK in p:
            self.acc.add(ASK, R_UNKNOWN_CWD)
            return
        blk = _delete_block(p, self.ctx)
        if blk:
            self.acc.add(REFUSE, blk)

    # ---- redirects
    def handle_redirs(self, redirs: list[tuple[str, str]]) -> None:
        for op, target in redirs:
            if target.startswith("&"):
                continue
            low = target.lower()
            if low in ("/dev/null", "$null", "nul", "nul:", "/dev/stdout", "/dev/stderr", "/dev/tty", "con"):
                continue
            if op == "<":
                self.check_secret([target])
                continue
            if target.startswith("/dev/") or target.startswith("\\\\.\\"):
                self.acc.add(REFUSE, R_DISK)
                continue
            self.write_target(target, truncate=(op != ">>"))
            self.check_secret([target])

    # ---- core
    def eval_segment(self, text: str, pipe_in: bool, depth: int, cmd_mode: bool = False) -> None:
        if not self.st.posix:
            m = re.match(r"^\s*\$[\w:{}.\[\]]+\s*[-+*/%]?=\s*(.+)$", text, re.S)
            if m:  # "$x = format D:" runs the right-hand side
                self.eval_nested(m.group(1), depth, cmd_mode)
                return
        tokens = _tokenize(text, self.ctx.os_name, cmd_mode)
        words: list[str] = []
        redirs: list[tuple[str, str]] = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if isinstance(t, _Op):
                target = tokens[i + 1] if i + 1 < len(tokens) and not isinstance(tokens[i + 1], _Op) else ""
                redirs.append((str(t), target))
                i += 2
                continue
            words.append(t)
            i += 1
        self.handle_redirs(redirs)
        self.eval_words(words, pipe_in, depth, top=True)

    def eval_nested(self, body: str, depth: int, cmd_mode: bool) -> None:
        if depth > _MAX_DEPTH:
            raise _Unparseable("nested too deeply")
        segs: list[_Seg] = []
        _scan(body, self.ctx.os_name, segs, depth + 1, cmd_mode)
        for seg in segs:
            self.eval_segment(seg.text, seg.pipe_in, depth + 1, cmd_mode)

    def eval_words(self, words: list[str], pipe_in: bool, depth: int, top: bool) -> None:
        if depth > _MAX_DEPTH:
            raise _Unparseable("nested too deeply")
        acc, st, ctx = self.acc, self.st, self.ctx
        words = _strip_prefix(words)
        if not words:
            return
        raw_name = words[0]
        base = re.split(r"[\\/]", raw_name)[-1] if not raw_name.startswith("$") else raw_name
        low = base.lower()
        for ext in (".exe", ".com"):
            if low.endswith(ext) and len(low) > len(ext):
                low = low[: -len(ext)]
        for ext in (".cmd", ".bat", ".ps1"):
            if low.endswith(ext) and low[: -len(ext)] in _KNOWN_TOOLS:
                low = low[: -len(ext)]
        args = words[1:]
        if _pathlike(raw_name) or st.posix is False:
            self.check_secret([raw_name])

        # --- command word we cannot identify
        if low not in ("%", "?") and top:
            if not st.posix and low.startswith("$"):
                return  # PowerShell expression statement ($x = 1, $_.Name ...)
            if re.search(r"[*?]|\$|`|@SUBST@|%[^%]+%", low) or (st.posix and "," in low and not low.startswith("-")):
                acc.add(REFUSE, R_UNPARSEABLE)
                return
        if low.startswith("[") and low != "[":
            return  # .NET expression; dangerous members are caught by the raw patterns

        self.record_exec(low, raw_name, args, pipe_in)

        if pipe_in and low in _INTERPRETERS_STRONG:
            acc.add(REFUSE, R_DOWNLOAD_RUN)
            return
        if pipe_in and low in _INTERPRETERS_WEAK:
            st.weak_pipe = True
        if low in _CMD_NAMES_EXEC_DIRECT:
            acc.add(REFUSE, R_UNPARSEABLE)
            return
        if low in _SUDO_CMDS:
            self.sudo(low, args)
            return

        info = _parse_args(args, st)
        self.check_secret(info.cands)
        if low in _DOWNLOADERS or (low == "certutil" and any(o in ("-urlcache", "-verifyctl") for o in info.opts)):
            st.downloader = True

        # cwd tracking
        if low in ("cd", "chdir", "sl", "set-location", "pushd", "push-location"):
            if info.pos and not info.pos[0].startswith("-"):
                p = st.norm(info.pos[0]) if not _has_unresolved(_expand(info.pos[0], ctx)) else None
                st.cwd = p if p and _UNK not in p else None
            elif low in ("cd", "chdir") and st.posix:
                st.cwd = _home(ctx)
            if "-" in info.pos:
                st.cwd = None
            return
        if low in ("popd", "pop-location"):
            st.cwd = None
            return

        if low in _WRAPPERS:
            self.wrapper(low, words, args, info, pipe_in, depth)
            return
        if low in _POSIX_SHELLS or low in _PS_SHELLS or low == "cmd":
            self.nested_shell(low, args, info, depth)
            return
        if low in ("start-process", "saps"):
            self.start_process(args, info, depth)
            return

        if self.specific(low, args, info, words, depth):
            return
        self.generic(low, raw_name, args, info)

    # ---- exec / download bookkeeping
    def record_exec(self, low: str, raw_name: str, args: list[str], pipe_in: bool) -> None:
        st, acc, ctx = self.st, self.acc, self.ctx
        targets: list[str] = []
        pathlike_exec = raw_name.startswith(("./", ".\\", "~/", "~\\", "/tmp/", "/var/tmp/", "/dev/shm/")) or \
            re.search(r"(?i)[\\/](downloads|temp|tmp)[\\/]", raw_name)
        known_tool = low in _KNOWN_TOOLS and not re.search(r"[\\/]", raw_name)
        if (raw_name.lower().endswith(_EXEC_EXT) or low.endswith(_EXEC_EXT)) and not known_tool:
            targets.append(raw_name)
        elif pathlike_exec and low not in _WRAPPERS:
            targets.append(raw_name)
        if low in _SCRIPT_RUNNERS or low in _LAUNCHERS or low in ("source", "."):
            inline = {"-c", "-command", "-enc", "-encodedcommand"}
            pos = [a for a in args if not a.startswith("-") and not (a.startswith("/") and len(a) <= 3)]
            if pos and not (set(a.lower() for a in args if a.startswith("-")) & inline and low not in _PS_SHELLS):
                targets.extend(pos[:1] if low in _SCRIPT_RUNNERS else pos)
            elif low in _LAUNCHERS:
                targets.extend(pos)
        if not targets:
            return
        st.exec_like = True
        if ctx.tainted and any(t.lower().rstrip("\"'").endswith(_EXEC_EXT) for t in targets):
            acc.add(REFUSE, R_DOWNLOAD_RUN)

    # ---- privilege escalation
    def sudo(self, low: str, args: list[str]) -> None:
        acc, ctx = self.acc, self.ctx
        if ctx.machine == "server" and low == "sudo":
            rest = list(args)
            while rest and rest[0] in ("-n", "--non-interactive"):
                rest = rest[1:]
            if self.sudo_allowed(rest):
                acc.add(ASK, R_SERVER)
                return
            acc.add(REFUSE, "sudo is only allowed for a short list of read/restart commands on the server")
            return
        acc.add(REFUSE, R_ACCOUNTS)

    @staticmethod
    def sudo_allowed(rest: list[str]) -> bool:
        if not rest:
            return False
        name, tail = rest[0], rest[1:]
        if name == "systemctl":
            return len(tail) >= 2 and tail[0] in ("status", "restart") and tail[1] in ("jarvis", "jarvis.service") \
                and all(t in ("--no-pager", "-l", "-n") for t in tail[2:])
        if name == "journalctl":
            i, saw_unit = 0, False
            while i < len(tail):
                t = tail[i]
                if t == "-u" and i + 1 < len(tail) and tail[i + 1] in ("jarvis", "jarvis.service"):
                    saw_unit = True
                    i += 2
                elif t == "-n" and i + 1 < len(tail) and re.fullmatch(r"\d+", tail[i + 1]):
                    i += 2
                elif t in ("--no-pager", "-e", "-r") or re.fullmatch(r"-n\d+", t):
                    i += 1
                elif t == "--since" and i + 1 < len(tail) and re.fullmatch(r"[\w :.+-]+", tail[i + 1]):
                    i += 2
                else:
                    return False
            return saw_unit
        if name in ("df", "free"):
            return all(re.fullmatch(r"-[a-zA-Z]+|--human-readable", t) for t in tail)
        if name == "apt":
            return tail == ["list", "--upgradable"]
        return False

    # ---- wrappers
    def wrapper(self, low: str, words: list[str], args: list[str], info: _Info, pipe_in: bool, depth: int) -> None:
        acc = self.acc
        if low in (".", "source", "&") and args and (args[0].startswith("$") or args[0].startswith("(")):
            acc.add(REFUSE, R_UNPARSEABLE)
            return
        if low == "wsl" and any(o in ("--unregister", "--import", "--export") for o in info.opts):
            acc.add(REFUSE, R_DISK if "--unregister" in info.opts else R_JARVIS)
            return
        if low in ("source", "."):
            if args:
                p = self.path(args[0])
                if _is_secret(p):
                    acc.add(ASK, R_SECRET)
            return
        for i in range(1, len(words)):
            sub = words[i:]
            if sub[0].startswith("-") or not sub[0]:
                continue
            self.eval_words(sub, pipe_in and i == 1, 0 + 1, top=False)

    def nested_shell(self, low: str, args: list[str], info: _Info, depth: int) -> None:
        acc = self.acc
        args = _explode_args(args, self.ctx.os_name)
        if any("@SUBST@" in a for a in args):
            acc.add(REFUSE, R_UNPARSEABLE)
            return
        if low in _PS_SHELLS:
            for a in args:
                if _PS_ENCODED_OPT.match(a):
                    acc.add(REFUSE, R_UNPARSEABLE)
                    return
        body = _wrapper_body(low, args, self.ctx.os_name)
        if body is not None:
            text, cmd_mode = body
            if not text.strip():
                acc.add(REFUSE, R_UNPARSEABLE)
                return
            self.eval_nested(text, depth, cmd_mode)
            return
        # running a script file or an interactive shell: only path bookkeeping (Jarvis mentions)
        self.check_secret(info.cands)

    def start_process(self, args: list[str], info: _Info, depth: int) -> None:
        acc = self.acc
        inner: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            low = a.lower()
            if low.startswith("-"):
                key = re.split(r"[=:]", low[1:], maxsplit=1)[0]
                val = re.split(r"[=:]", a, maxsplit=1)[1] if re.search(r"[=:]", a) else None
                if len(key) >= 2 and ("argumentlist".startswith(key) or "filepath".startswith(key)) or key == "args":
                    value = val if val else (args[i + 1] if i + 1 < len(args) else "")
                    if value:
                        if "," in value and " " not in value:
                            value = value.replace(",", " ")
                        inner.append(value)
                    i += 1 if val else 2
                    continue
                if "verb".startswith(key) and len(key) >= 2:
                    v = (val or (args[i + 1] if i + 1 < len(args) else "")).lower()
                    if "runas" in v:
                        acc.add(REFUSE, R_ACCOUNTS)
                        return
                    i += 1 if val else 2
                    continue
                if any(k.startswith(key) and len(key) >= 3 for k in
                       ("workingdirectory", "windowstyle", "redirectstandardoutput", "redirectstandarderror",
                        "redirectstandardinput", "credential", "environment")):
                    i += 1 if val else 2
                    continue
                i += 1
                continue
            inner.append(a)
            i += 1
        if not inner:
            return
        text = " ".join(_requote(t) for t in inner)
        self.eval_nested(text, depth, False)

    # ---- blocklist checks per command family; return True when fully handled
    def specific(self, low: str, args: list[str], info: _Info, words: list[str], depth: int) -> bool:
        acc, st, ctx = self.acc, self.st, self.ctx
        opts = info.opts
        pos = info.pos
        pos_l = [p.lower() for p in pos]

        # D - registry tokens anywhere outside read-only cmdlets
        if low not in _PS_REGISTRY_READERS and any(_REGISTRY_TOKEN.search(w) for w in words[1:]):
            acc.add(REFUSE, R_REGISTRY)
            return True
        if low in _REG_CMDS:
            if low == "reg" and pos_l and pos_l[0] in _REG_READ_VERBS:
                return True
            acc.add(REFUSE, R_REGISTRY)
            return True
        if any(w.lower().endswith(".reg") for w in words) and low not in _READ_CMDS:
            acc.add(REFUSE, R_REGISTRY)
            return True

        # A - disks
        if low in _DISK_CMDS or low.startswith("mkfs"):
            acc.add(REFUSE, R_DISK)
            return True
        if low == "convert" and any(re.fullmatch(r"[a-zA-Z]:", p) for p in pos) and any(o.startswith("/fs") for o in opts):
            acc.add(REFUSE, R_DISK)
            return True
        if low == "nvme" and "format" in pos_l:
            acc.add(REFUSE, R_DISK)
            return True
        if low == "dd":
            if any(re.match(r"(?i)of=(/dev/|\\\\\.\\)", c) for c in args):
                acc.add(REFUSE, R_DISK)
            return True
        # B - boot
        if low in _BOOT_CMDS:
            acc.add(REFUSE, R_BOOT)
            return True
        # raw deletes
        if low in _DELETE_CMDS:
            acc.add(REFUSE, R_DELETE)
            return True
        # F - security
        if low in _SECURITY_CMDS:
            acc.add(REFUSE, R_SECURITY)
            return True
        # G - accounts
        if low in _ACCOUNT_CMDS:
            acc.add(REFUSE, R_ACCOUNTS)
            return True
        # I - postgres
        if low in _PG_CMDS:
            acc.add(REFUSE, R_JARVIS)
            return True
        if low in ("vssadmin", "wbadmin", "wevtutil", "clear-eventlog", "fsutil"):
            if set(pos_l) & {"delete", "cl", "clear-log", "resize", "deletejournal", "usn"} or low == "clear-eventlog":
                acc.add(REFUSE, R_SYSTEM)
            return True
        if low == "wmic":
            if set(pos_l) & {"call", "delete", "create", "set"}:
                acc.add(REFUSE, R_UNPARSEABLE)
            return True
        if low == "certutil" and any(o in ("-decode", "-decodehex", "-encode") for o in opts):
            acc.add(REFUSE, R_UNPARSEABLE)
            return True

        # I - never stop Jarvis or its database
        if low in _KILL_CMDS:
            if any(_JARVIS_RE.search(a) or _PG_RE.search(a) for a in args):
                acc.add(REFUSE, R_JARVIS)
            return True

        if low in ("net", "net1") and pos_l and pos_l[0] in _NET_ACCOUNT_SUBS:
            acc.add(REFUSE, R_ACCOUNTS)
            return True
        # F, I - services and scheduled tasks
        if low in ("systemctl", "service", "sc", "net", "net1", "stop-service", "start-service", "restart-service",
                   "set-service", "suspend-service", "resume-service", "schtasks") or low in _TASK_DESTRUCTIVE \
                or low in _TASK_CREATE:
            if self.services(low, args, info):
                return True

        # F - firewalls
        if low == "ufw":
            if set(pos_l) & {"disable", "reset"}:
                acc.add(REFUSE, R_SECURITY)
            return True
        if low in ("iptables", "ip6tables", "nft", "nftables", "firewall-cmd"):
            if any(o in ("-f", "-x", "-p", "-z", "--flush", "--delete-chain", "--policy", "--panic-off")
                   for o in opts) or "flush" in pos_l:
                acc.add(REFUSE, R_SECURITY)
            return True
        if low == "netsh":
            if re.search(r"advfirewall|firewall", " ".join(pos_l)) and "show" not in pos_l:
                acc.add(REFUSE, R_SECURITY)
            return True

        # G - permissions on system paths
        if low in ("icacls", "cacls", "xcacls", "set-acl") or low in _PERM_CMDS:
            for c in info.cands:
                p = self.path(c)
                if _critical(p, ctx) or _jarvis_under(p, ctx) or _boot_path(p, ctx):
                    acc.add(REFUSE, R_JARVIS if _jarvis_under(p, ctx) else R_ACCOUNTS)
            return True

        # I - destructive SQL
        if low in _SQL_CLIENTS:
            if _SQL_DESTRUCTIVE.search(" ".join(args)):
                acc.add(REFUSE, R_JARVIS)
            return True

        if low == "rsync":
            if any(o.startswith("--delete") or o == "--remove-source-files" for o in opts):
                acc.add(REFUSE, R_DELETE)
            return False
        if low in ("tar", "bsdtar", "unzip", "zip", "7z", "7za", "7zr", "expand-archive", "compress-archive",
                   "gzip", "gunzip", "bzip2", "xz", "unrar", "rar"):
            self.archive(low, args, info)
            return True

        if low == "find":
            if "-delete" in opts:
                acc.add(REFUSE, R_DELETE)
                return True
            for j, w in enumerate(words):
                if w in ("-exec", "-execdir", "-ok", "-okdir"):
                    sub: list[str] = []
                    for t in words[j + 1:]:
                        if t in (";", "\\;", "+"):
                            break
                        sub.append(t)
                    if sub:
                        self.eval_words(sub, False, depth + 1, top=False)
            return False

        if low == "robocopy" and any(o in ("/mir", "/purge") for o in opts):
            acc.add(REFUSE, R_DELETE)
            return True
        return False

    def archive(self, low: str, args: list[str], info: _Info) -> None:
        """Archive tools write where they extract or create: check those destinations."""
        acc, st = self.acc, self.st
        targets: list[str] = []
        al = [a.lower() for a in args]
        if (low in ("tar", "bsdtar") and "--remove-files" in al) or \
                (low in ("7z", "7za", "7zr") and any(a.startswith("-sdel") for a in al)) or \
                (low == "zip" and any(a in ("-m", "--move") or re.fullmatch(r"-[a-z]*m[a-z]*", a) for a in al)) or \
                (low in ("rar", "unrar") and any(a.startswith("-df") for a in al)):
            acc.add(REFUSE, R_DELETE)
        if low in ("tar", "bsdtar"):
            flags = "".join(x.lstrip("-") for x in args if re.fullmatch(r"-[A-Za-z]+", x))
            if args and re.fullmatch(r"[A-Za-z]+", args[0]):
                flags += args[0]  # old style: tar czf out.tgz dir
            creating = bool(set("cru") & set(flags)) or "--create" in args or "--append" in args
            extracting = "x" in flags or "--extract" in args or "--get" in args
            chdir: list[str] = []
            skip: set[int] = set()
            file_arg = None
            for i, x in enumerate(args):
                if x == "-C" and i + 1 < len(args):
                    chdir.append(args[i + 1])
                    skip.add(i + 1)
                elif x.startswith("--directory="):
                    chdir.append(x.split("=", 1)[1])
                elif x == "--directory" and i + 1 < len(args):
                    chdir.append(args[i + 1])
                    skip.add(i + 1)
                elif x.startswith("-C") and len(x) > 2:
                    chdir.append(x[2:])
                elif x == "-f" and i + 1 < len(args):
                    file_arg = args[i + 1]
                    skip.add(i + 1)
                elif x.startswith("--file="):
                    file_arg = x.split("=", 1)[1]
            pos_nc = [x for i, x in enumerate(args) if i not in skip and not x.startswith("-")]
            if file_arg is None and "f" in flags and pos_nc:
                file_arg = pos_nc[1] if (args and re.fullmatch(r"[A-Za-z]+", args[0]) and len(pos_nc) > 1 and pos_nc[0] == args[0]) else pos_nc[0]
            if creating and file_arg:
                targets.append(file_arg)
            if extracting:
                targets.extend(chdir or [st.cwd if st.cwd is not None else st.unknown_cwd])
        elif low in ("unzip", "7z", "7za", "7zr", "unrar", "rar"):
            dirs = []
            for i, a in enumerate(args):
                if a == "-d" and i + 1 < len(args):
                    dirs.append(args[i + 1])
                elif a.startswith("-o") and len(a) > 2:
                    dirs.append(a[2:])
                elif a.startswith("-d") and len(a) > 2 and low == "unzip":
                    dirs.append(a[2:])
            if low in ("7z", "7za", "7zr", "rar") and info.pos[:1] in (["a"], ["u"], ["rn"], ["d"]) and len(info.pos) > 1:
                dirs.append(info.pos[1])
            if not dirs and (low == "unzip" or info.pos[:1] in (["x"], ["e"])):
                dirs.append(st.cwd if st.cwd is not None else st.unknown_cwd)
            targets += dirs
        elif low == "zip":
            if info.pos:
                targets.append(info.pos[0])
        elif low in ("expand-archive", "compress-archive"):
            d = _ps_param(args, "de", "destinationpath")
            if d:
                targets.append(d)
            elif low == "compress-archive" and len(info.pos) > 1:
                targets.append(info.pos[1])
            elif low == "expand-archive":
                targets.append(st.cwd if st.cwd is not None else st.unknown_cwd)
        else:  # gzip/bzip2/xz/gunzip: replace the file in place
            targets.extend(info.pos)
        for t in targets:
            self.write_target(t)
        if any(o in ("-f", "--force") for o in info.opts) and low in ("gzip", "gunzip", "bzip2", "xz"):
            acc.add(AB, R_OVERWRITE)

    def services(self, low: str, args: list[str], info: _Info) -> bool:
        acc, ctx = self.acc, self.ctx
        pos_l = [p.lower() for p in info.pos]
        opts = info.opts
        verb, targets = "", []
        if low == "systemctl":
            verbs = [p for p in pos_l]
            if not verbs:
                return True
            verb, targets = verbs[0], verbs[1:]
        elif low == "service":
            if not pos_l:
                return True
            targets, verb = [pos_l[0]], (pos_l[1] if len(pos_l) > 1 else "status")
        elif low == "sc":
            if not pos_l or pos_l[0] not in _SC_VERBS:
                return False
            verb, targets = pos_l[0], pos_l[1:2]
        elif low in ("net", "net1"):
            if not pos_l or pos_l[0] not in ("stop", "start", "pause", "continue"):
                if pos_l and pos_l[0] == "use" and len(pos_l) > 1:
                    if any(p.startswith("\\\\") for p in info.pos):
                        acc.add(ASK, R_SEND)
                    else:
                        acc.add(AB, "mapping network drives needs your approval")
                if pos_l and pos_l[0] == "share" and len(pos_l) > 1:
                    acc.add(ASK, R_SEND)
                return True
            verb, targets = pos_l[0], pos_l[1:2]
        elif low in ("stop-service", "start-service", "restart-service", "set-service", "suspend-service",
                     "resume-service"):
            verb = {"stop-service": "stop", "start-service": "start", "restart-service": "restart",
                    "set-service": "config", "suspend-service": "pause", "resume-service": "continue"}[low]
            targets = pos_l[:1] or [o.split(":", 1)[-1] for o in opts]
        elif low == "schtasks":
            al = [a.lower() for a in args]
            name_blob = " ".join(args).lower()
            verb_m = next((a for a in al if a in ("/delete", "/change", "/end", "/create", "/run", "/query")), "")
            if verb_m in ("/delete", "/change", "/end"):
                if _JARVIS_RE.search(name_blob) or "*" in name_blob or "/tn" not in al:
                    acc.add(REFUSE, R_JARVIS)
                else:
                    acc.add(ASK, "changing scheduled tasks needs your approval")
            elif verb_m == "/create":
                if _JARVIS_RE.search(name_blob):
                    acc.add(REFUSE, R_JARVIS)
                else:
                    acc.add(ASK, "creating scheduled tasks needs your approval")
            elif verb_m == "/run":
                acc.add(AB, "running a scheduled task needs your approval")
            return True
        elif low in _TASK_DESTRUCTIVE or low in _TASK_CREATE:
            blob = " ".join(args)
            if low in _TASK_DESTRUCTIVE and (_JARVIS_RE.search(blob) or "*" in blob or not args):
                acc.add(REFUSE, R_JARVIS)
            elif low in _TASK_CREATE and _JARVIS_RE.search(blob):
                acc.add(REFUSE, R_JARVIS)
            else:
                acc.add(ASK, "scheduling or changing background jobs needs your approval")
            return True
        if not verb:
            return True
        target_blob = " ".join(targets)
        if verb in _SERVICE_MUTATING or verb in _SERVICE_RESTARTISH:
            if _JARVIS_RE.search(target_blob) or _PG_RE.search(target_blob):
                if verb in _SERVICE_RESTARTISH:
                    acc.add(ASK, "restarting Jarvis or its database needs your approval")
                else:
                    acc.add(REFUSE, R_JARVIS)
            elif _SECURITY_SERVICES.search(target_blob) and verb in ("stop", "disable", "mask", "delete", "config",
                                                                     "pause", "kill", "remove"):
                acc.add(REFUSE, R_SECURITY)
            else:
                acc.add(AB, "changing services needs your approval")
        return True

    # ---- write/move targets: REFUSE on protected locations
    def generic(self, low: str, raw_name: str, args: list[str], info: _Info) -> None:
        st = self.st
        opts, pos = info.opts, info.pos
        if low in _MOVE_CMDS:
            for c in pos:
                self.delete_target(c)
                self.write_target(c)
        elif low in _COPY_CMDS:
            dest: list[str] = []
            if not st.posix or low in ("copy-item", "cpi"):
                d = _ps_param(args, "de", "destination")
                if d:
                    dest = [d]
            if not dest:
                if set(opts) & {"-t", "--target-directory"}:
                    dest = list(pos)
                else:
                    dest = pos[1:] if len(pos) > 1 else []
            for c in dest:
                self.write_target(c)
        elif low == "sed":
            if any(o.startswith("-i") or o == "--in-place" or re.fullmatch(r"-[a-z]*i[a-z]*", o) for o in opts):
                for c in pos[1:]:
                    self.write_target(c)
        elif low in _TRUNC_CMDS or low in _APPEND_CMDS:
            for c in pos:
                self.write_target(c)


def _summary_command(command: str) -> str:
    one = " ".join(command.split())
    return "run: " + (one if len(one) <= 200 else one[:197] + "...")


class _Collect:
    """Verdict collector for non-shell tools (strictest wins, first reason of that strength)."""

    def __init__(self) -> None:
        self.items: list[tuple[Verdict, str]] = []

    def add(self, verdict: Verdict, reason: str) -> None:
        self.items.append((verdict, reason))

    def result(self, default: str = "safe to run") -> tuple[Verdict, str]:
        if not self.items:
            return RUN, default
        best = max(v for v, _ in self.items)
        return best, next(r for v, r in self.items if v == best)


# ------------------------------------------------------------------ shell, step 1: blocklist

R_UNC = "network (UNC) paths need your approval because reading them can leak your Windows credentials"
R_DEVICE_READ = "device paths (\\\\?\\, \\\\.\\, \\??\\) need your approval because they can reach other volumes"


def _remote_reason(p: str) -> str:
    return R_DEVICE_READ if _is_device(p) else R_UNC
R_NOT_SAFE = "this command is not on the safe list, so it needs your approval"
R_TAINT_GIT = "web or downloaded content was read in this task, so git commands need your approval"

# Raw-text scan, independent of parsing: distinctive blocked names as whole words.
_RAW_WORD_RULES = [
    (re.compile(r"(?<![\w-])(?:diskpart|mkfs|fdisk|parted|wipefs|format-volume|clear-disk|initialize-disk|"
                r"remove-partition)(?![\w-])", re.I), R_DISK),  # A
    (re.compile(r"(?<![\w-])format\s+[a-z]:", re.I), R_DISK),  # A
    (re.compile(r"(?<![\w-])(?:bcdedit|bcdboot|grub-install|update-grub|efibootmgr)(?![\w-])", re.I), R_BOOT),  # B
    (re.compile(r"(?<![\w-])(?:set-mppreference|vssadmin)(?![\w-])", re.I), R_SECURITY),  # F
    (re.compile(r"(?<![\w-])takeown(?![\w-])", re.I), R_ACCOUNTS),  # G
    (re.compile(r"(?<![\w])-enc(?:odedcommand)?(?![\w])", re.I), R_UNPARSEABLE),
    (re.compile(r"(?<![\w-])(?:invoke-expression|iex|set-alias|new-alias|sal|nal)(?![\w-])", re.I), R_UNPARSEABLE),
]
_JARVIS_TASK_VERBS = re.compile(
    r"(?<![\w-])(?:stop|delete|disable|change|end|unregister|kill|remove|deregister)(?![\w-])|/(?:delete|change|end)\b",
    re.I)


_NS_DOTDOT_RAW = re.compile(r"(?://[?.]|/{1,2}\?\?)/(?:[^\s'\";|&<>]*/)?\.\.(?=/|[\s'\";|&<>,]|$)")
_NS_ANY = re.compile(r"(?://[?.]|/{1,2}\?\?)/")
_MGMT_WORDS = re.compile(
    r"schtasks|scheduledtask|systemctl|sc\.exe|(?<![\w-])sc\s|stop-service|set-service|(?<![\w-])service(?![\w-])|"
    r"taskkill|stop-process|(?<![\w-])kill|pkill", re.I)


_CLI_PKG = re.compile(r"@anthropic-ai[/\\]claude-code", re.I)
# npm also accepts short forms and common typos (r, in, ins, up, isntall, udpate).
_PKG_VERBS = re.compile(
    r"(?<![\w-])(?:install|uninstall|remove|rm|r|update|upgrade|add|i|in|ins|inst|insta|instal|"
    r"isnt|isnta|isntal|isntall|un|unlink|link|up|udpate)(?![\w-])", re.I)


def _shell_blocklist(tool: str, command: str, ctx: GuardContext) -> tuple[str | None, bool]:
    """(refuse_reason or None, mentions_jarvis_folder)."""
    ev = _Evaluator(ctx, tool)
    acc = ev.acc
    for rx, reason in _RAW_WORD_RULES:
        if rx.search(command):
            acc.add(REFUSE, reason)
    if _JARVIS_RE.search(command) and _JARVIS_TASK_VERBS.search(command) and _MGMT_WORDS.search(command):
        acc.add(REFUSE, R_JARVIS)
    if _CLI_PKG.search(command) and _PKG_VERBS.search(command):
        acc.add(REFUSE, R_JARVIS)
    for rx, reason in _RAW_REFUSE:
        if rx.search(command):
            acc.add(REFUSE, reason)
    if _RAW_DOWNLOADER.search(command):
        ev.st.downloader = True
    segs: list[_Seg] = []
    try:
        _scan(command, ctx.os_name, segs)
        for seg in segs:
            ev.eval_segment(seg.text, seg.pipe_in, 0)
    except _Unparseable:
        acc.add(REFUSE, R_UNPARSEABLE)
    if ev.st.downloader and (ev.st.exec_like or ev.st.weak_pipe):
        acc.add(REFUSE, R_DOWNLOAD_RUN)
    if ctx.os_name == "windows" and _NS_DOTDOT_RAW.search(_strip_cf(command).replace("\\", "/")):
        acc.add(REFUSE, R_DEVICE + " (a '..' inside a device path)")
    if ctx.os_name == "windows" and not acc.items:
        try:
            for part in split_shell(command, ctx.os_name):
                for tok in _tokenize(part, ctx.os_name):
                    for piece in str(tok).split(","):
                        value = re.sub(r"(?i)^-[a-z]+:", "", piece)
                        if _NS_ANY.search(_strip_cf(value).replace("\\", "/")) and \
                                _is_devdots(_normalize(value, ctx, cwd="c:/")):
                            acc.add(REFUSE, R_DEVICE + " (a '..' inside a device path)")
        except _Unparseable:
            pass
    if acc.items:
        return acc.items[0][1], True
    raw = command.lower().replace("\\", "/")
    jarvis = _jarvis(ctx).lower()
    return None, ev.st.mentions_jarvis or jarvis in raw


# ------------------------------------------------------------------ shell, step 3: the allow-list

_NUM = re.compile(r"^[+-]?\d+$")
_FS_PREFIX = re.compile(r"(?i)^(?:microsoft\.powershell\.core\\)?filesystem::")
_PROVIDER = re.compile(r"(?i)^(?:-[a-z]+:)?[a-z]{2,}:")  # env:, variable:, HKCU:, cert: ... (not a C: drive)
_VERSION_PROGRAMS = {  # `<program> --version` may run
    "git", "python", "python3", "py", "node", "npm", "npx", "pip", "pip3", "winget", "code", "claude", "gh", "psql",
    "pg_dump", "java", "dotnet", "go", "cargo", "rustc", "uv", "docker",
}


def _provider_token(tok: str) -> bool:
    """PowerShell provider paths (env:, variable:, HKCU:\\..., cert:) are not files; never allow-listed."""
    for part in tok.split(","):
        value = re.sub(r"(?i)^-[a-z]+:", "", part)
        if _PROVIDER.match(part) and not _FS_PREFIX.match(value):
            return True
        if "::" in _FS_PREFIX.sub("", value):  # Microsoft.PowerShell.Core\Environment::X, x\Registry::HKCU\...
            return True
    return False
_RISKY_CHARS = re.compile(r"[$`(){}<>@%!^]")


def _risky_syntax(command: str) -> bool:
    """Risky syntax anywhere (even inside quotes): variables, substitutions, groups, redirection, `&`, `@`, `%`,
    `!`, `^`, control characters."""
    if _RISKY_CHARS.search(command):
        return True
    if "&" in command.replace("&&", ""):
        return True
    return any(ord(c) < 32 and c not in "\n\t\r" for c in command)


def _wild(tok: str) -> bool:
    return bool(re.search(r"[*?]|\[[^\]]*\]", tok))


def _is_opt_tok(tok: str) -> bool:
    return tok.startswith("-") and len(tok) > 1


_GIT_FORBIDDEN = ("--git-dir", "--work-tree", "--output", "--ext-diff", "--textconv", "--exec-path", "--no-index",
                  "--contents", "--upload-pack", "--open-files-in-pager")


class _Allow:
    """Step 3: every part must be a read-only allow-listed command with allowed arguments."""

    def __init__(self, tool: str, ctx: GuardContext) -> None:
        self.ctx = ctx
        self.posix = tool == "Bash"
        self.cwd = _cwd_of(ctx)
        self.secret = False
        self.unc = _is_unc(self.cwd)  # a shell started in a network/device folder reads from it
        self.git = False

    # ---- helpers
    def touch(self, tok: str) -> None:
        if not tok:
            return
        try:
            parts = [tok]
            if not self.posix:
                parts += [x for x in tok.split(",") if x and x != tok]  # PowerShell path lists: a.txt,.env
            cands: list[str] = []
            for t in parts:
                cands.append(t)
                pieces = t.split(":")
                if len(pieces) > 1 and len(pieces[0]) > 1:
                    cands += [x for x in pieces[1:] if x]  # git rev:path
            for c in cands:
                c = _FS_PREFIX.sub("", c)
                p = _normalize(c, self.ctx, cwd=self.cwd)
                if _is_secret(p):
                    self.secret = True
                if _is_unc(p):
                    self.unc = True  # reading a UNC path leaks credentials to the remote host
        except Exception:  # noqa: BLE001
            self.secret = True

    def run(self, command: str) -> bool:
        if _risky_syntax(command):
            return False
        segs: list[_Seg] = []
        try:
            _scan(command, self.ctx.os_name, segs)
            table = self._posix_table() if self.posix else self._win_table()
            prev = ""
            seen = False
            for seg in segs:
                toks = _tokenize(seg.text, self.ctx.os_name)
                if any(isinstance(t, _Op) for t in toks) or "-" in toks:
                    return False
                if not toks:
                    continue
                seen = True
                name_raw = toks[0]
                if re.search(r"[\\/]", name_raw) or not name_raw:
                    return False
                name = name_raw.lower()
                if name.endswith(".exe") and name != "where.exe":
                    name = name[:-4]
                if not self.posix and any(_provider_token(t) for t in toks):
                    return False
                if len(toks) == 2 and toks[1] == "--version":
                    if name not in _VERSION_PROGRAMS:
                        return False
                    prev = name
                    continue
                fn = table.get(name)
                if fn is None or not fn(toks[1:], seg.pipe_in, prev):
                    return False
                prev = name
            return seen
        except (_Unparseable, ValueError):
            return False

    # ---- generic validators
    def _simple(self, opt_re: str, *, need_input: bool = False, max_pos: int | None = None, wild: bool = False):
        rx = re.compile(opt_re)

        def check(args: list[str], pipe_in: bool, prev: str) -> bool:
            pos = []
            for t in args:
                if _is_opt_tok(t):
                    if not rx.fullmatch(t):
                        return False
                else:
                    pos.append(t)
            files = [t for t in pos if not _NUM.fullmatch(t)]
            if max_pos is not None and len(pos) > max_pos:
                return False
            for t in files:
                if _wild(t) and not wild:
                    return False
                self.touch(t)
            return not (need_input and not files and not pipe_in)

        return check

    def _listing(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        for t in args:
            if not _is_opt_tok(t):
                self.touch(t)
        return True

    def _no_args(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        return not args

    def _projection(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        """Select-Object / Format-* with a `*` property would print every property (e.g. secrets): ask."""
        if any("*" in a or "commandline" in a.lower() for a in args):
            return False
        return self._listing(args, pipe_in, prev)

    def _cd(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        if len(args) > 1 or any(_is_opt_tok(a) or a == "-" for a in args):
            return False
        if args:
            self.touch(args[0])
            self.cwd = _normalize(args[0], self.ctx, cwd=self.cwd)
        else:
            self.cwd = _home(self.ctx)
        return True

    def _echo(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        return True

    def _find(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        value_tests = {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-type", "-maxdepth", "-mindepth",
                       "-size", "-mtime", "-atime", "-ctime", "-mmin", "-amin", "-cmin", "-newer", "-user", "-group",
                       "-perm"}
        flags = {"-print", "-print0", "-prune", "-not", "-and", "-or", "-o", "-a", "-empty", "-xdev", "-mount",
                 "-depth", "-true", "-false"}
        i = 0
        while i < len(args) and not _is_opt_tok(args[i]):
            self.touch(args[i])
            i += 1
        while i < len(args):
            t = args[i]
            if t in value_tests and i + 1 < len(args):
                if t == "-newer":
                    self.touch(args[i + 1])
                i += 2
            elif t in flags:
                i += 1
            else:
                return False
        return True

    def _grep(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        short = re.compile(r"^-[ivnclLwxhHEFoqsaP]+$|^-[ABCm]\d*$")
        long_ = re.compile(r"^--(?:color|colour)(?:=\w+)?$|^--(?:ignore-case|line-number|count|invert-match|"
                           r"word-regexp|files-with-matches|files-without-match|only-matching|no-filename|"
                           r"with-filename|fixed-strings|extended-regexp|quiet|no-messages|text)$|"
                           r"^--(?:max-count|context|after-context|before-context)=\d+$")
        pos: list[str] = []
        skip_num = False
        for t in args:
            if _is_opt_tok(t):
                if not (short.fullmatch(t) or long_.fullmatch(t)):
                    return False
                skip_num = bool(re.fullmatch(r"-[ABCm]", t))
            elif skip_num and _NUM.fullmatch(t):
                skip_num = False
            else:
                skip_num = False
                pos.append(t)
        if not pos:
            return False
        files = pos[1:]  # first positional is the pattern
        for f in files:
            if _wild(f):
                return False
            self.touch(f)
        return bool(files) or pipe_in

    def _git(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        i = 0
        if args[:1] == ["-C"]:
            if len(args) < 3 or _is_opt_tok(args[1]):
                return False
            self.touch(args[1])
            i = 2
        if i >= len(args):
            return False
        sub, rest = args[i], args[i + 1:]
        for t in rest:
            name = t.lower().split("=", 1)[0]
            if name.startswith("--") and any(f.startswith(name) for f in _GIT_FORBIDDEN):
                return False  # also catches unambiguous abbreviations such as --out / --ext
            if t.startswith("-O"):
                return False
        if sub in ("status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe"):
            for t in rest:
                if not _is_opt_tok(t):
                    self.touch(t)
        elif sub == "remote":
            if rest not in ([], ["-v"]):
                return False
        elif sub == "branch":
            if any(t not in ("-a", "-v", "-vv", "--list", "-r") for t in rest):
                return False
        elif sub == "tag":
            if any(t not in ("-l", "--list") for t in rest):
                return False
        elif sub == "stash":
            if rest != ["list"]:
                return False
        else:
            return False
        self.git = True
        return True

    # ---- Linux / Git Bash table
    def _posix_table(self):
        s = self._simple
        return {
            "ls": self._listing, "pwd": self._no_args, "cd": self._cd, "echo": self._echo,
            "wc": s(r"^-[lwcmL]+$", need_input=True), "file": s(r"^-[bLi]+$", need_input=True),
            "stat": s(r"^-L$", need_input=True),
            "du": s(r"^-[ahscxLHkmb]+$|^-d\d*$|^--max-depth=\d+$", wild=True),
            "df": s(r"^-[hHTikPal]+$", wild=True), "free": s(r"^-[bkmgthw]+$|^--(?:human|mega|giga)$", max_pos=0),
            "uptime": s(r"^-[ps]$|^--pretty$", max_pos=0), "ps": self._ps,
            "whoami": self._no_args, "hostname": s(r"^-[fisdIaA]+$", max_pos=0),
            "uname": s(r"^-[asnrvmpio]+$|^--all$", max_pos=0), "id": s(r"^-[ugGnrz]+$", max_pos=1),
            "date": s(r"^-u$|^--utc$", max_pos=0), "which": s(r"^-a$"), "lsblk": s(r"^-[A-Za-z]+$", wild=True),
            "nproc": s(r"^--all$", max_pos=0), "basename": s(r"^-[asz]+$"), "dirname": s(r"^-z$"),
            "realpath": s(r"^-[emsLP]+$|^--relative-to=\S+$"),
            "sort": s(r"^-[rnufbdghMVs]+$|^-k\d+(?:,\d+)?[nr]*$|^-t.$", need_input=True),
            "uniq": s(r"^-[cdui]+$", max_pos=1),
            "cut": s(r"^-[dfcbs]\S*$|^--(?:delimiter|fields|characters|bytes)=\S+$", need_input=True),
            "find": self._find,
            "head": s(r"^-[qv]+$|^-\d+$|^-n\d*$|^-c\d*$|^--lines=\d+$|^--bytes=\d+$", need_input=True),
            "tail": s(r"^-[qv]+$|^-\d+$|^-n\d*$|^-c\d*$|^--lines=\d+$|^--bytes=\d+$", need_input=True),
            "cat": s(r"^-[nbsAETv]+$", need_input=True), "grep": self._grep, "git": self._git,
        }

    def _ps(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        for t in args:
            if _is_opt_tok(t):
                if not re.fullmatch(r"-[A-Za-z]+", t):
                    return False
            elif re.fullmatch(r"[A-Za-z]*e[A-Za-z]*", t):
                return False  # BSD "e" modifier prints process environments
            elif not re.fullmatch(r"[\w,.=:-]+", t):
                return False
        return True

    # ---- PowerShell table
    def _ps_reader(self, allowed: list[str], value_opts: set[str], path_opts: set[str], *, select: bool = False):
        def canon(tok: str) -> str | None:
            name = tok[1:].split(":", 1)[0].lower()
            if not name:
                return None
            return next((a for a in allowed if a.startswith(name)), None)

        def check(args: list[str], pipe_in: bool, prev: str) -> bool:
            files: list[str] = []
            pos: list[str] = []
            has_pattern_opt = False
            i = 0
            while i < len(args):
                t = args[i]
                if _is_opt_tok(t) and not _NUM.fullmatch(t):
                    c = canon(t)
                    if c is None:
                        return False
                    attached = t.split(":", 1)[1] if ":" in t else None
                    if c == "pattern":
                        has_pattern_opt = True
                    if c in value_opts or c in path_opts:
                        if attached:
                            val = attached
                        elif i + 1 < len(args):
                            val = args[i + 1]
                            i += 1
                        else:
                            return False
                        if c in path_opts:
                            files.append(val)
                else:
                    pos.append(t)
                i += 1
            if select:
                pos = pos if has_pattern_opt else pos[1:]
            files += [p for p in pos if not _NUM.fullmatch(p)]
            piped_text = select and pipe_in and prev in ("get-content", "gc", "cat", "type")
            if not files and not piped_text:
                return False
            for f in files:
                if _wild(f):
                    return False
                self.touch(f)
            return True

        return check

    def _findstr(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        pos: list[str] = []
        has_c = False
        for t in args:
            if t.startswith("/") and len(t) > 1 and re.fullmatch(r"/[A-Za-z](?::.*)?", t):
                tl = t.lower()
                if tl.startswith("/c:"):
                    has_c = True
                elif not re.fullmatch(r"/[ivnlbexmr]+", tl):
                    return False
            elif _is_opt_tok(t):
                return False
            else:
                pos.append(t)
        files = pos if has_c else pos[1:]
        if not has_c and not pos:
            return False
        for f in files:
            if _wild(f):
                return False
            self.touch(f)
        return bool(files) or (pipe_in and prev in ("get-content", "gc", "cat", "type"))

    def _ipconfig(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        return args in ([], ["/all"])

    def _where_exe(self, args: list[str], pipe_in: bool, prev: str) -> bool:
        return all(re.fullmatch(r"[\w.+-]+", a) for a in args)

    def _win_table(self):
        listing = self._listing
        reader = self._ps_reader(["path", "literalpath", "totalcount", "tail", "head", "first", "encoding", "raw",
                                  "readcount"], {"totalcount", "tail", "head", "first", "encoding", "readcount"},
                                 {"path", "literalpath"})
        sel = self._ps_reader(["pattern", "path", "literalpath", "casesensitive", "simplematch", "context", "list",
                               "notmatch", "quiet", "allmatches", "encoding"],
                              {"pattern", "context", "encoding"}, {"path", "literalpath"}, select=True)
        table = {n: listing for n in (
            "get-childitem", "gci", "ls", "dir", "get-item", "gi", "test-path", "resolve-path", "get-location",
            "pwd", "gl", "get-process", "ps", "gps", "get-service", "gsv", "get-psdrive", "gdr", "get-volume",
            "get-date", "get-computerinfo", "get-netipaddress", "tree", "get-filehash",
            "sort-object", "sort", "measure-object", "measure", "out-string", "get-member", "gm",
            "where-object")}
        table.update({n: self._projection for n in ("select-object", "select", "format-table", "ft",
                                                    "format-list", "fl")})
        table.update({"whoami": self._no_args, "hostname": self._no_args, "systeminfo": self._no_args,
                      "ipconfig": self._ipconfig, "where.exe": self._where_exe, "git": self._git,
                      "get-content": reader, "gc": reader, "cat": reader, "type": reader,
                      "select-string": sel, "sls": sel, "findstr": self._findstr})
        return table


# ------------------------------------------------------------------ shell decision

def _shell_decision(tool: str, command: str, ctx: GuardContext) -> tuple[Verdict, str]:
    """1 blocklist (incl. non-allow-listed commands that mention Jarvis's folder) -> REFUSE; 2 server -> ALWAYS_ASK;
    3 allow-listed read-only command -> RUN; 4 else ALWAYS_ASK.  A shell command never yields ASK_UNLESS_BYPASS."""
    reason, mentions_jarvis = _shell_blocklist(tool, command, ctx)
    if reason:
        return REFUSE, reason
    chk = _Allow(tool, ctx)
    allowed = chk.run(command)
    if not allowed and mentions_jarvis:
        return REFUSE, R_JARVIS  # rule I: only reads of Jarvis's folder are allowed
    if ctx.machine == "server":
        return ASK, R_SERVER
    if allowed:
        if chk.secret:
            return ASK, R_SECRET
        if chk.unc:
            return ASK, R_UNC
        if chk.git and ctx.tainted:
            return ASK, R_TAINT_GIT
        return RUN, "read-only command"
    return ASK, R_NOT_SAFE


# ------------------------------------------------------------------ tools

JARVIS_TOOL_NAMES = frozenset({  # A1 tool list (cross-checked against brain.tools.build_tool_list in the tests)
    "add_todo", "confirm_pending_action", "create_note", "create_reminder", "edit_reminder", "find_reminders",
    "finish_item", "get_github_repo", "get_weather", "get_youtube_info", "learn_lesson", "list_agenda", "propose_new_action",
    "read_rss_feed", "read_webpage", "run_ad_hoc_placement", "run_known_action", "run_one_off_action",
    "search_memory", "update_profile", "web_search",
})
_SHELL_TOOLS = {"Bash", "PowerShell"}
_TAINT_GATED = {"create_note", "create_reminder", "edit_reminder", "learn_lesson", "update_profile"}


def _needs_approval(verdict: Verdict, ctx: GuardContext) -> bool:
    if verdict == REFUSE:
        return False
    if verdict == ASK:
        return True
    if verdict == AB:
        return not (ctx.bypass and ctx.machine == "pc" and not ctx.tainted)
    return False


def _decision(verdict: Verdict, reason: str, summary: str, ctx: GuardContext) -> GuardDecision:
    return GuardDecision(verdict, reason, _needs_approval(verdict, ctx), summary)


def _refuse(reason: str, summary: str = "blocked call") -> GuardDecision:
    return GuardDecision(REFUSE, reason, False, summary)


def _str_arg(args: dict, key: str) -> str | None:
    v = args.get(key)
    return v if isinstance(v, str) and v.strip() else None


def _forms(raw: str, real: object, ctx: GuardContext) -> list[str]:
    """The given path and (when the hook supplied it) the real path after symlinks/junctions."""
    out = [_normalize(raw, ctx)]
    if isinstance(real, str) and real.strip():
        out.append(_normalize(real, ctx))
    if any(_is_devdots(p) for p in out):
        raise _DeviceDots()
    return out


_VAR_SYNTAX = re.compile(r"%[^%\s]+%|\$env:|\$[A-Za-z_{]", re.I)
R_VARS = "this path uses variables (%VAR% / $VAR) that the file tools do not expand, so it needs your approval"


def _has_var_syntax(raw: str) -> bool:
    return bool(_VAR_SYNTAX.search(raw))


def _real_of(args: dict) -> object:
    return args.get("_real_path")


def _real_in(args: dict, raw: str) -> object:
    m = args.get("_real_paths")
    return m.get(raw) if isinstance(m, dict) else None


# Grep: which roots ask
_SECRET_HOME_DIRS = (".ssh", ".gnupg", ".aws", ".kube", ".azure", ".config", ".mozilla", ".password-store",
                     ".bitcoin", ".ethereum", ".electrum", "AppData")
_SAFE_GREP_EXT = {"py", "pyi", "md", "rst", "js", "ts", "tsx", "jsx", "css", "html", "htm", "java", "c", "h", "cpp",
                  "hpp", "cs", "go", "rs", "rb", "php", "kt", "swift", "lua"}
_SAFE_GREP_TYPES = {"py", "python", "js", "ts", "md", "markdown", "html", "css", "java", "c", "cpp", "go", "rust",
                    "rs", "ruby", "php", "kotlin", "swift", "lua", "rst"}
_GLOB_SAFE = re.compile(r"^(?:\*\*/)?\*\.(?:([a-z0-9]+)|\{([a-z0-9,]+)\})$", re.I)


def _safe_filter(args: dict) -> bool:
    glob = args.get("glob")
    if isinstance(glob, str) and glob.strip():
        m = _GLOB_SAFE.match(glob.strip().replace("\\", "/"))
        if m:
            exts = [m.group(1)] if m.group(1) else m.group(2).split(",")
            if exts and all(e.lower() in _SAFE_GREP_EXT for e in exts):
                return True
    typ = args.get("type")
    return isinstance(typ, str) and typ.strip().lower() in _SAFE_GREP_TYPES


def _grep_root_asks(p: str, ctx: GuardContext) -> bool:
    home, jar = _home(ctx), _jarvis(ctx)
    if _under(home, p) or _under(p, jar) or _under(jar, p):
        return True
    return any(_under(home + "/" + d.lower() if ctx.os_name == "windows" else home + "/" + d, p)
               for d in _SECRET_HOME_DIRS)


def _file_tool(tool: str, args: dict, ctx: GuardContext) -> GuardDecision:
    acc = _Collect()
    if tool in ("Glob", "Grep"):
        base_raw = args.get("path")
        if base_raw is not None and not isinstance(base_raw, str):
            return _refuse("the tool arguments could not be understood")
        pattern_key = "pattern" if tool == "Glob" else "glob"
        pattern = args.get(pattern_key)
        if pattern is not None and not isinstance(pattern, str):
            return _refuse("the tool arguments could not be understood")
        if (tool == "Grep" and not isinstance(args.get("pattern"), str)) or (tool == "Glob" and not pattern):
            return _refuse("the tool arguments could not be understood")
        bases = _forms(base_raw, _real_of(args), ctx) if base_raw else [_home(ctx)]
        for base in bases:
            if _is_unc(base):
                acc.add(ASK, _remote_reason(base))
            checks = [base]
            if pattern:
                pat = pattern.replace("\\", "/")
                try:
                    pat_norm = _normalize(pattern, ctx)
                except Exception:  # noqa: BLE001
                    pat_norm = None
                if pat_norm is not None:
                    if _is_devdots(pat_norm):
                        raise _DeviceDots()
                    if _is_unc(pat_norm):
                        acc.add(ASK, _remote_reason(pat_norm))
                    checks.append(pat_norm)
                checks.append((base.rstrip("/") + "/" + pat).lower())
            if any(_is_secret(c) for c in checks):
                acc.add(ASK, R_SECRET)
            if tool == "Grep" and not _safe_filter(args):
                acc.add(ASK, "a search without a source/document file filter could read secret files, so it needs "
                             "your approval")
            elif tool == "Grep" and _grep_root_asks(base, ctx) and not _safe_filter(args):
                acc.add(ASK, "searching this folder could read secret files, so it needs your approval")
        summary = f"{tool.lower()} {pattern or ''} in {base_raw or 'home'}".strip()
        v, r = acc.result()
        return _decision(v, r, summary, ctx)
    raw = _str_arg(args, "file_path")
    if raw is None:
        return _refuse("the file path is missing or unreadable")
    forms = _forms(raw, _real_of(args), ctx)
    if tool == "Read":
        if any(_is_secret(p) for p in forms):
            acc.add(ASK, R_SECRET)
        for p in forms:
            if _is_unc(p):
                acc.add(ASK, _remote_reason(p))
        v, r = acc.result()
        return _decision(v, r, f"read {raw}", ctx)
    # Write / Edit
    summary = f"{'write' if tool == 'Write' else 'edit'} {raw}"
    if _has_var_syntax(raw):
        acc.add(ASK, R_VARS)
    for p in forms:
        if _is_device(p):
            return _refuse(R_DEVICE, summary)
        if _boot_path(p, ctx):
            return _refuse(R_BOOT, summary)
        if _jarvis_under(p, ctx):
            return _refuse(R_JARVIS, summary)
        if _critical(p, ctx, include_system=False):
            return _refuse(R_CRITICAL, summary)
        if _system_under(p, ctx):
            acc.add(ASK, "changing system folders needs your approval")
        if _is_secret(p):
            acc.add(ASK, R_SECRET)
        if _is_unc(p):
            acc.add(ASK, R_SEND)
        if _is_persist(p, ctx):
            acc.add(ASK, R_PERSIST)
    if tool == "Write" and args.get("_exists", True) is not False:
        acc.add(AB, R_OVERWRITE)
    text = args.get("content") if tool == "Write" else args.get("new_string")
    if isinstance(text, str) and text:
        _scan_script_content(raw, text, ctx, acc)
    if ctx.machine == "server":
        acc.add(ASK, R_SERVER)
    v, r = acc.result()
    return _decision(v, r, summary, ctx)


_SHELL_SCRIPT_EXT = (".ps1", ".psm1", ".sh", ".bash", ".bat", ".cmd", ".zsh")
_CODE_SCRIPT_EXT = (".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".vbs", ".rb", ".pl", ".php", ".lua")
_DESTRUCTIVE_CODE = re.compile(
    r"rmtree|os\.remove|os\.unlink|os\.rmdir|shutil\.move|\.unlink\(|rmsync|rmdirsync|rimraf|remove-item|"
    r"rm\s+-[a-z]*r|del\s+/[sq]|format\s+[a-z]:|diskpart|mkfs|dd\s+if=|deletefile|deletefolder", re.I)
R_PERSIST = "writing to auto-start or config locations needs your approval"


def _scan_script_content(raw_path: str, text: str, ctx: GuardContext, acc: _Collect) -> None:
    """A script written now can be run later without another look: flag ones containing blocked actions."""
    low = raw_path.lower()
    if low.endswith(_SHELL_SCRIPT_EXT):
        tool = "PowerShell" if low.endswith((".ps1", ".psm1", ".bat", ".cmd")) and ctx.os_name == "windows" else "Bash"
        try:
            reason, _ = _shell_blocklist(tool, text[:20000], ctx)
        except Exception:  # noqa: BLE001
            reason = "unverifiable"
        if reason:
            acc.add(ASK, "this script contains commands that are blocked when run directly, so it needs your approval")
    elif low.endswith(_CODE_SCRIPT_EXT) and _DESTRUCTIVE_CODE.search(text):
        acc.add(AB, "this script deletes or formats things, so it needs your approval")


# ---- custom tools

_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*(?:\[[A-Za-z0-9._,\-]+\])?(?:==[A-Za-z0-9._+\-]+)?$")
_NPM_SCOPED_RE = re.compile(r"^@[a-z0-9][a-z0-9._\-]*/[a-z0-9][a-z0-9._\-]*$")
# Rule I: winget ids Jarvis (and the owner's way of fixing it) depend on; uninstalling them is refused.
_PROTECTED_WINGET_PREFIXES = (
    "python.python",  # the Python Jarvis runs on
    "postgresql.",  # Jarvis's database
    "git.git",  # version control
    "openjs.nodejs",  # Node (Claude Code runtime)
    "anthropic.",  # Claude / Claude Code
    "microsoft.visualstudiocode",  # VS Code: how the owner fixes Jarvis
)
_OPEN_SAFE_EXT = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf", "odt", "ods", "odp", "csv", "png", "jpg",
    "jpeg", "gif", "bmp", "webp", "mp3", "wav", "flac", "ogg", "m4a", "mp4", "mkv", "avi", "mov", "webm",
}


def _paths_arg(args: dict) -> list[str] | None:
    paths = args.get("paths")
    if paths is None and isinstance(args.get("path"), str):
        paths = [args["path"]]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        return None
    return paths


def _pairs(args: dict, key: str) -> list[tuple[str, str]] | None:
    items = args.get(key)
    if not isinstance(items, list) or not items:
        return None
    out = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("from"), str) or not isinstance(it.get("to"), str) \
                or not it["from"].strip() or not it["to"].strip():
            return None
        out.append((it["from"], it["to"]))
    return out


def _dest_checks(forms: list[str], ctx: GuardContext, acc: _Collect) -> str | None:
    """Checks for a destination (write target). Returns a REFUSE reason or None."""
    for p in forms:
        if _is_device(p):
            return R_DEVICE
        if _boot_path(p, ctx):
            return R_BOOT
        if _jarvis_under(p, ctx):
            return R_JARVIS
        if _system_under(p, ctx):
            return R_SYSTEM
        if _is_secret(p):
            acc.add(ASK, R_SECRET)
        if _is_unc(p):
            acc.add(ASK, R_SEND)
        if _is_persist(p, ctx):
            acc.add(ASK, R_PERSIST)
    return None


def _open_path_decision(args: dict, ctx: GuardContext) -> GuardDecision:
    raw = _str_arg(args, "path")
    if raw is None:
        return _refuse("open_path needs a path", "open path")
    summary = f"open {raw}"
    acc = _Collect()
    if ctx.tainted:
        acc.add(ASK, R_TAINT)
    if re.match(r"(?i)^[a-z][a-z0-9+.-]+:", _strip_cf(raw).strip().strip("\"'").strip()):
        acc.add(ASK, "opening web addresses or app links (URI schemes) needs your approval")
    for p in _forms(raw, _real_of(args), ctx):
        if _is_unc(p):
            acc.add(ASK, R_UNC)
        if _is_secret(p):
            acc.add(ASK, R_SECRET)
        if re.search(r"/(downloads|temp|tmp)/", p.lower().rstrip("/") + "/"):
            acc.add(ASK, "files in Downloads or Temp can be unsafe to open, so this needs your approval")
        base = p.rstrip("/").rsplit("/", 1)[-1]
        if "." in base.lstrip("."):
            ext = base.rsplit(".", 1)[-1].lower()
            if ext not in _OPEN_SAFE_EXT:
                acc.add(ASK, "opening programs or scripts needs your approval")
    v, r = acc.result("only opens a document or folder")
    return _decision(v, r, summary, ctx)


def _install_decision(args: dict, ctx: GuardContext) -> GuardDecision:
    action, manager, package = args.get("action"), args.get("manager"), args.get("package")
    summary = f"{action} {package} with {manager}"
    if action not in ("install", "uninstall") or manager not in ("winget", "pip", "npm", "flatpak") \
            or not isinstance(package, str):
        return _refuse("install_software needs action install/uninstall, a known manager and a package id", summary)
    if (manager == "winget" and ctx.os_name != "windows") or (manager == "flatpak" and ctx.os_name != "linux"):
        return _refuse(f"{manager} isn't available on this machine", summary)
    plain = _PKG_RE.fullmatch(package) and (manager == "pip" or "[" not in package)
    if not (plain or (manager == "npm" and _NPM_SCOPED_RE.fullmatch(package))):
        return _refuse("the package must be a plain package id - URLs, paths and flags are not allowed", summary)
    if manager == "npm" and re.sub(r"@[^/@]*$", "", package.lower()) == "@anthropic-ai/claude-code":
        return _refuse("Jarvis's Claude Code CLI can't be changed", summary)
    if manager == "winget" and action == "uninstall" and package.lower().startswith(_PROTECTED_WINGET_PREFIXES):
        return _refuse("Jarvis and its tools depend on this program, so it can't be uninstalled", summary)
    if manager == "pip":
        name = re.sub(r"[-_.]+", "-", re.sub(r"\[.*?\]", "", package.split("==", 1)[0])).lower()
        protected = {re.sub(r"[-_.]+", "-", str(x)).lower() for x in ctx.protected_packages}
        if name in protected:
            return _refuse("Jarvis depends on this Python package, so it can't be changed", summary)
        # A pip package can shadow one Jarvis imports: always ask, bypass never covers it.
        return _decision(ASK, R_INSTALL, summary, ctx)
    return _decision(AB, R_INSTALL, summary, ctx)


_PC_ONLY_TOOLS = {"move_to_trash", "make_folder", "move_paths", "copy_paths", "install_software", "open_path",
                  "send_file", "open_in_editor", "open_on_screen", "move_window", "list_windows"}


def _open_on_screen_decision(args: dict, ctx: GuardContext) -> GuardDecision:
    urls, path = args.get("urls"), args.get("path")
    if (urls is None) == (path is None):
        return _refuse("open_on_screen needs either urls or a path", "open on screen")
    if path is not None:
        return _open_path_decision({k: v for k, v in args.items() if k != "urls"}, ctx)
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
        return _refuse("open_on_screen needs a list of web addresses", "open on screen")
    summary = "open " + ", ".join(urls)
    acc = _Collect()
    for url in urls:
        clean = _strip_cf(url).strip()
        m = re.match(r"(?i)^https?://([^/?#]*)([^?#]*)(.*)$", clean)
        if not m or not m.group(1):
            return _refuse("only http(s) web addresses can be opened", summary)
        if "@" in m.group(1) or m.group(3):
            # A query string, fragment or user:pass@ can carry data off the PC.
            acc.add(ASK, "this web address can carry data off the PC, so it needs your approval")
    if ctx.tainted:
        acc.add(ASK, R_TAINT)
    v, r = acc.result("only opens web pages")
    return _decision(v, r, summary, ctx)


def _jarvis_tool(name: str, args: dict, ctx: GuardContext) -> GuardDecision:
    if ctx.machine == "server" and name in _PC_ONLY_TOOLS:
        return _refuse("this tool is not available on the server", name.replace("_", " "))
    if name == "move_to_trash":
        paths = _paths_arg(args)
        summary = "move to trash: " + ", ".join(paths) if paths else "move to trash"
        if paths is None:
            return _refuse("move_to_trash needs a list of paths", summary)
        acc = _Collect()
        for raw in paths:
            if _has_var_syntax(raw):
                acc.add(ASK, R_VARS)
            for p in _forms(raw, _real_in(args, raw) or (_real_of(args) if len(paths) == 1 else None), ctx):
                blk = _delete_block(p, ctx)
                if blk:
                    return _refuse(blk, summary)
                if _is_unc(p):  # \\localhost\C$\... can reach protected folders
                    acc.add(ASK, _remote_reason(p))
                if _is_secret(p):
                    acc.add(ASK, R_SECRET)
        acc.add(AB, "deleting (to the Recycle Bin/Trash) needs your approval")
        v, r = acc.result()
        return _decision(v, r, summary, ctx)
    if name == "make_folder":
        raw = _str_arg(args, "path")
        if raw is None:
            return _refuse("make_folder needs a path", "make folder")
        acc = _Collect()
        summary = f"make folder {raw}"
        if _has_var_syntax(raw):
            acc.add(ASK, R_VARS)
        for p in _forms(raw, _real_of(args), ctx):
            if _is_device(p):
                return _refuse(R_DEVICE, summary)
            if _boot_path(p, ctx):
                return _refuse(R_BOOT, summary)
            if _jarvis_under(p, ctx):
                return _refuse(R_JARVIS, summary)
            if _system_under(p, ctx):
                acc.add(ASK, "changing system folders needs your approval")
            if _is_secret(p):
                acc.add(ASK, R_SECRET)
            if _is_persist(p, ctx):
                acc.add(ASK, R_PERSIST)
        v, r = acc.result("creating a folder is safe")
        return _decision(v, r, summary, ctx)
    if name in ("move_paths", "copy_paths"):
        moving = name == "move_paths"
        pairs = _pairs(args, "moves" if moving else "copies")
        if pairs is None:
            return _refuse(f"{name} needs a list of {{from, to}} pairs", name.replace("_", " "))
        summary = ("move " if moving else "copy ") + "; ".join(f"{a} -> {b}" for a, b in pairs)
        acc = _Collect()
        for src, dst in pairs:
            if _has_var_syntax(dst) or (moving and _has_var_syntax(src)):
                acc.add(ASK, R_VARS)
            for p in _forms(src, _real_in(args, src), ctx):
                if moving:
                    blk = _delete_block(p, ctx)
                    if blk:
                        return _refuse(blk, summary)
                if _is_secret(p):
                    acc.add(ASK, R_SECRET)
                if _is_unc(p):
                    acc.add(ASK, R_UNC)
            blk = _dest_checks(_forms(dst, _real_in(args, dst), ctx), ctx, acc)
            if blk:
                return _refuse(blk, summary)
            ex = args.get("_exists")
            if not moving and (not isinstance(ex, dict) or ex.get(dst, True) is not False):
                acc.add(AB, R_OVERWRITE)  # unknown counts as existing
        if moving:
            acc.add(AB, R_MOVE)
        v, r = acc.result("copying is safe: nothing is overwritten or deleted")
        return _decision(v, r, summary, ctx)
    if name == "install_software":
        return _install_decision(args, ctx)
    if name == "open_path":
        return _open_path_decision(args, ctx)
    if name == "open_on_screen":
        return _open_on_screen_decision(args, ctx)
    if name in ("move_window", "list_windows"):
        return _decision(RUN, "only moves or lists windows", name.replace("_", " "), ctx)
    if name == "send_file":
        path = args.get("path")
        dest = args.get("destination", "discord")
        if not isinstance(path, str) or not path.strip() or not isinstance(dest, str):
            return _refuse("send_file needs a file path", "send file")
        return _decision(ASK, R_SEND, f"send {path} to {dest or 'discord'}", ctx)
    if name == "open_in_editor":
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            return _refuse("open_in_editor needs a path", "open in editor")
        return _decision(RUN, "only opens a window", f"open {path or 'editor'}", ctx)
    if name == "queue_pc_task":
        return _decision(RUN, "only queues a task", "queue a PC task", ctx)
    if name in JARVIS_TOOL_NAMES:
        if name in _TAINT_GATED and ctx.tainted:
            return _decision(AB, R_TAINT, f"{name.replace('_', ' ')} after reading web content", ctx)
        return _decision(RUN, "Jarvis's own tool", name.replace("_", " "), ctx)
    return _refuse(f"{name} is not a known Jarvis tool")


def _evaluate(tool: str, args: dict, ctx: GuardContext) -> GuardDecision:
    if not isinstance(tool, str) or not isinstance(args, dict):
        return _refuse("the tool call could not be understood")
    if ctx.machine not in ("pc", "server") or ctx.os_name not in ("windows", "linux"):
        return _refuse("unknown machine or operating system")
    if ctx.cwd and _is_devdots(_normalize(ctx.cwd, ctx, cwd="c:/" if ctx.os_name == "windows" else "/")):
        raise _DeviceDots()
    if tool in _SHELL_TOOLS:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _refuse("the command is missing or empty", "empty command")
        v, r = _shell_decision(tool, command, ctx)
        return _decision(v, r, _summary_command(command), ctx)
    if tool in ("Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"):
        return _file_tool("Edit" if tool == "MultiEdit" else tool, args, ctx)
    if tool.startswith("mcp__jarvis__") and len(tool) > len("mcp__jarvis__"):
        return _jarvis_tool(tool[len("mcp__jarvis__"):], args, ctx)
    return _refuse(f"{tool or 'this tool'} is not allowed")


def evaluate(tool: str, args: dict, ctx: GuardContext) -> GuardDecision:
    """Classify one tool call.  Any internal error fails closed (REFUSE)."""
    try:
        return _evaluate(tool, args, ctx)
    except _DeviceDots:
        return _refuse(R_DEVICE + " (a '..' inside a device path)")
    except Exception:  # noqa: BLE001 - the guard must never raise; fail closed
        return _refuse("the safety check could not verify this, so it is blocked")
