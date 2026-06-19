#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import base64
import contextlib
import os
import json
import argparse
import tempfile
import re
import select
import subprocess
import sys
import termios
import inspect
import traceback
import time
import tty
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional, Set


DEFAULT_SOURCE_AUTH = Path.home() / ".codex" / "auth.json"
SOURCE_AUTH_ENV = "CODEX_AUTH_PATH"
CODEX_BIN_ENV = "CODEX_BACKUP_CODEX_BIN"
CODEX_VERIFY_ENV = "CODEX_BACKUP_VERIFY_CODEX"
CODEX_VERIFY_TIMEOUT_ENV = "CODEX_BACKUP_VERIFY_TIMEOUT"
CODEX_RESTORE_CAPACITY_ENV = "CODEX_BACKUP_RESTORE_CAPACITY"
CODEX_RESTORE_CAPACITY_TIMEOUT_ENV = "CODEX_BACKUP_RESTORE_CAPACITY_TIMEOUT"
CODEX_VERIFY_DEFAULT = "status"
CODEX_RESTORE_CAPACITY_DEFAULT = "live"
CODEX_VERIFY_LIVE_EXPECTED = "CODEX_AUTH_OK"
CODEX_RESTORE_CAPACITY_WINDOW_MINUTES = 5 * 60
CODEX_RESTORE_CAPACITY_WINDOW_SECONDS = CODEX_RESTORE_CAPACITY_WINDOW_MINUTES * 60
_DEBUG_LEVEL = 0
_RUN_ID = uuid.uuid4().hex[:8]
_RUN_STARTED_AT = time.perf_counter()
BACKUP_BASE = Path(__file__).resolve().parent / "data_backup"
LEGACY_BACKUP_BASES = (Path("/backup/data_backup"),)
FILE_NAME = "auth.json"
META_NAME = "meta.json"


ANSI_STYLES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}


def _ansi_enabled(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    term = os.environ.get("TERM", "")
    return (
        hasattr(stream, "isatty")
        and stream.isatty()
        and "NO_COLOR" not in os.environ
        and term.lower() not in ("", "dumb")
    )


def _style(text: str, *styles: str, stream: Any = None) -> str:
    if not styles or not _ansi_enabled(stream):
        return text
    prefix = "".join(ANSI_STYLES[name] for name in styles if name in ANSI_STYLES)
    if not prefix:
        return text
    return f"{prefix}{text}{ANSI_STYLES['reset']}"


def _visible_len(text: str) -> int:
    return len(re.sub(r"\x1b\[[0-9;]*m", "", text))


def _pad_visible(text: str, width: int) -> str:
    return text + (" " * max(0, width - _visible_len(text)))


def _terminal_width() -> int:
    return max(64, min(112, shutil.get_terminal_size((88, 24)).columns))


def _display_path(path: Path | str) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return value


def _truncate_middle(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return "." * width
    left = max(1, (width - 3) // 2)
    right = max(1, width - 3 - left)
    return f"{value[:left]}...{value[-right:]}"


def _print_panel(title: str, lines: List[str], stream: Any = None) -> None:
    stream = stream or sys.stdout
    clean_lines = [line.rstrip() for line in lines]
    content_width = max([_visible_len(title), *( _visible_len(line) for line in clean_lines), 42])
    inner_width = min(_terminal_width() - 4, content_width)
    border = _style("+" + "-" * (inner_width + 2) + "+", "cyan", stream=stream)
    print(border, file=stream)
    print(
        f"| {_pad_visible(_style(title, 'bold', stream=stream), inner_width)} |",
        file=stream,
    )
    if clean_lines:
        print(_style("| " + "-" * inner_width + " |", "cyan", stream=stream), file=stream)
        for line in clean_lines:
            print(f"| {_pad_visible(_truncate_middle(line, inner_width), inner_width)} |", file=stream)
    print(border, file=stream)


def _status_color(label: str) -> str:
    return {
        "OK": "green",
        "INFO": "cyan",
        "WARN": "yellow",
        "ERR": "red",
    }.get(label, "blue")


def _print_status(
    label: str,
    message: str,
    detail: Optional[str] = None,
    stream: Any = None,
) -> None:
    stream = stream or sys.stdout
    badge = _style(f"[{label}]", _status_color(label), "bold", stream=stream)
    suffix = f": {detail}" if detail else ""
    print(f"{badge} {message}{suffix}", file=stream)


def _print_duplicate_backup(exc: DuplicateBackupError) -> None:
    _print_status("INFO", "No backup created", "the analyzed data matches an existing backup.")
    if exc.backup_dir is not None:
        print(f"       Already saved in: {_display_path(exc.backup_dir)}")


def _format_iso_display(value: Any, *, date_only: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        return "n/a"
    parsed = _parse_iso_datetime_utc(value)
    if parsed is not None:
        if date_only:
            return parsed.date().isoformat()
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    candidate = value.strip().replace("T", " ")
    if date_only:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", candidate)
        if match:
            return match.group(1)
    return candidate


def _root_label(item: Dict[str, Any]) -> str:
    root = Path(str(item.get("root", "")))
    if not str(root):
        return "n/a"
    return f"{root.parent.name}/{root.name}" if root.parent else root.name


def _print_app_header() -> None:
    _print_panel(
        "Codex Backup Tool",
        [
            "Auth snapshot backup and restore",
            f"Source: {_display_path(_get_source_auth_path())}",
            f"Vault:  {_display_path(BACKUP_BASE)}",
        ],
    )


def _print_main_menu() -> None:
    print()
    print(_style("Actions", "bold"))
    print(f"  {_style('[1]', 'cyan', 'bold')} Backup current auth.json")
    print(f"  {_style('[2]', 'cyan', 'bold')} Restore a saved backup")
    print(f"  {_style('[q]', 'dim')} Quit")
    print()


def _print_backup_table(backups: List[Dict[str, Any]]) -> None:
    width = _terminal_width()
    idx_width = max(1, len(str(len(backups))))
    show_source = width >= 104
    source_width = 20 if show_source else 0
    fixed_width = 2 + idx_width + 2 + 10 + 2 + 10 + 2
    if show_source:
        fixed_width += source_width + 2
    name_width = max(24, width - fixed_width)

    headers = [
        "#".rjust(idx_width),
        "Created".ljust(10),
        "Expires".ljust(10),
        "Source".ljust(source_width) if show_source else "",
        "Snapshot".ljust(name_width),
    ]
    header_line = "  ".join(part for part in headers if part)
    separator = "  ".join(
        part
        for part in (
            "-" * idx_width,
            "-" * 10,
            "-" * 10,
            "-" * source_width if show_source else "",
            "-" * name_width,
        )
        if part
    )
    print(_style("  " + header_line, "bold"))
    print(_style("  " + separator, "cyan"))

    for idx, item in enumerate(backups, start=1):
        created = _format_iso_display(item.get("analyzed_at"), date_only=True)
        expiry = _format_iso_display(item.get("expiry"), date_only=True)
        name = _truncate_middle(str(item.get("name", "n/a")), name_width)
        fields = [
            str(idx).rjust(idx_width),
            created.ljust(10),
            expiry.ljust(10),
        ]
        if show_source:
            fields.append(_truncate_middle(_root_label(item), source_width).ljust(source_width))
        fields.append(name.ljust(name_width))
        print("  " + "  ".join(fields))


class DuplicateBackupError(RuntimeError):
    """The current backup matches an existing backup."""

    def __init__(self, backup_dir: Optional[Path] = None) -> None:
        self.backup_dir = backup_dir
        message = "No backup created: the analyzed data matches an existing backup."
        if backup_dir is not None:
            message = f"{message} Already saved in: {backup_dir}"
        super().__init__(message)


class CodexAuthVerificationError(RuntimeError):
    """Auth verification through the codex binary failed."""


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
JWT_KEY_HINT_MARKERS = (
    "token",
    "jwt",
    "id_token",
    "idtoken",
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "bearer",
    "authorization",
    "auth",
    "credentials",
)
BACKUP_EXPIRY_PREFERRED_TOKEN_TYPES = ("access_token",)


def _debug_enabled() -> bool:
    return _DEBUG_LEVEL > 0


def _debug(message: str, **kwargs: object) -> None:
    if not _debug_enabled():
        return
    if not kwargs:
        details = ""
    else:
        details = ", ".join(f"{key}={value!r}" for key, value in kwargs.items())
    elapsed_ms = (time.perf_counter() - _RUN_STARTED_AT) * 1000
    caller = inspect.currentframe()
    function = "?"
    if caller is not None and caller.f_back is not None:
        function = caller.f_back.f_code.co_name
    if _DEBUG_LEVEL >= 2 and function != "<module>":
        details = f"func={function!r}, elapsed_ms={elapsed_ms:.1f}" + (", " + details if details else "")
    elif details:
        details = f"elapsed_ms={elapsed_ms:.1f}, " + details
    else:
        details = f"elapsed_ms={elapsed_ms:.1f}"

    prefix = datetime.now(tz=timezone.utc).isoformat()
    if details:
        print(f"[DEBUG {_RUN_ID} {prefix}] {message} | {details}", file=sys.stderr)
    else:
        print(f"[DEBUG {_RUN_ID} {prefix}] {message}", file=sys.stderr)


def _debug_exception(message: str, exc: BaseException) -> None:
    if _debug_enabled():
        _debug(message, exc_type=type(exc).__name__, error=str(exc))
        if _DEBUG_LEVEL >= 3:
            _debug(message + " traceback", traceback=traceback.format_exc().strip())


@contextlib.contextmanager
def _debug_phase(name: str, **kwargs: object):
    if not _debug_enabled():
        yield
        return

    start = time.perf_counter()
    _debug("START phase", phase=name, **kwargs)
    try:
        yield
        duration_ms = (time.perf_counter() - start) * 1000
        _debug("END phase", phase=name, elapsed_ms=f"{duration_ms:.2f}ms", status="OK")
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        if _DEBUG_LEVEL >= 2:
            _debug(
                "END phase",
                phase=name,
                elapsed_ms=f"{duration_ms:.2f}ms",
                status="ERROR",
                error_type=type(exc).__name__,
            )
        _debug_exception("Exception in phase", exc)
        raise


def _debug_json_dump_preview(payload: Any, max_len: int = 140) -> str:
    if not _debug_enabled():
        return ""
    rendered = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    if len(rendered) <= max_len:
        return rendered
    return rendered[:max_len] + "..."


def _configure_debug_from_args() -> None:
    global _DEBUG_LEVEL
    parser = argparse.ArgumentParser(
        add_help=True,
        description="JSON auth backup/restore tool",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable debug output (use -vv for stronger debug, -vvv to include tracebacks)",
    )
    args, _ = parser.parse_known_args(sys.argv[1:])
    _DEBUG_LEVEL = args.verbose
    _debug(
        "Debug configuration",
        arg_level=_DEBUG_LEVEL,
        source="argv",
        argv=sys.argv[1:],
    )


def _consume_pending_escape_sequence() -> bool:
    """Consume leftover ESC sequences, usually generated by arrow keys."""
    fd = sys.stdin.fileno()
    while True:
        readable, _, _ = select.select([sys.stdin], [], [], 0.10)
        if not readable:
            return False
        chunk = os.read(fd, 1)
        if not chunk:
            return False
        if chunk.isalpha() or chunk in (b"~",):
            return True


def _read_menu_choice(prompt: str) -> str:
    """Read a menu choice while ignoring arrow keys and handling Ctrl+C cleanly."""
    if not sys.stdin.isatty():
        return input(prompt).strip().lower()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buffer = ""
    pending_escape = False
    print(prompt, end="", flush=True)
    try:
        tty.setcbreak(fd)
        while True:
            char = sys.stdin.read(1)
            if char == "":
                raise EOFError
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x1b":
                pending_escape = not _consume_pending_escape_sequence()
                continue
            if pending_escape:
                if char in ("[", "O"):
                    _consume_pending_escape_sequence()
                    pending_escape = False
                    continue
                if char in ("A", "B", "C", "D", "H", "F", "~"):
                    pending_escape = False
                    continue
                pending_escape = False
            if not buffer and char in ("[", "O"):
                pending_escape = True
                continue
            if not buffer and char in ("A", "B", "C", "D", "H", "F"):
                continue
            if char in ("\r", "\n"):
                print()
                return buffer.strip().lower()
            if char in ("\x7f", "\b"):
                if buffer:
                    buffer = buffer[:-1]
                    print("\b \b", end="", flush=True)
                continue
            if char.isprintable():
                buffer += char
                print(char, end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _iter_backup_roots() -> tuple[Path, ...]:
    _debug("Computing backup roots", primary=str(BACKUP_BASE), legacy=[str(path) for path in LEGACY_BACKUP_BASES])
    return (BACKUP_BASE, *LEGACY_BACKUP_BASES)


def _get_source_auth_path() -> Path:
    source = Path(os.path.expanduser(os.environ.get(SOURCE_AUTH_ENV, str(DEFAULT_SOURCE_AUTH)))).resolve()
    _debug("Resolved auth source path", source=str(source), env=SOURCE_AUTH_ENV, fallback=str(DEFAULT_SOURCE_AUTH))
    return source


def _get_default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(os.path.expanduser(configured)).resolve()
    return (Path.home() / ".codex").resolve()


def _auth_path_matches_codex_home(auth_path: Path) -> bool:
    try:
        return auth_path.resolve() == (_get_default_codex_home() / FILE_NAME).resolve()
    except OSError:
        return False


def _codex_verify_mode() -> str:
    raw_mode = os.environ.get(CODEX_VERIFY_ENV, CODEX_VERIFY_DEFAULT).strip().lower()
    if raw_mode in ("", "0", "false", "no", "off", "disabled", "disable", "skip"):
        return "off"
    if raw_mode in ("1", "true", "yes", "on", "status", "local", "quick"):
        return "status"
    if raw_mode in ("live", "exec", "full", "remote"):
        return "live"
    raise ValueError(
        f"{CODEX_VERIFY_ENV} is invalid: {raw_mode!r}. Use off, status, or live."
    )


def _codex_verify_timeout() -> float:
    raw_timeout = os.environ.get(CODEX_VERIFY_TIMEOUT_ENV, "75").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(f"{CODEX_VERIFY_TIMEOUT_ENV} is invalid: {raw_timeout!r}") from exc
    if timeout <= 0:
        raise ValueError(f"{CODEX_VERIFY_TIMEOUT_ENV} must be > 0")
    return timeout


def _codex_restore_capacity_mode() -> str:
    raw_mode = os.environ.get(
        CODEX_RESTORE_CAPACITY_ENV,
        CODEX_RESTORE_CAPACITY_DEFAULT,
    ).strip().lower()
    if raw_mode in ("", "0", "false", "no", "off", "disabled", "disable", "skip"):
        return "off"
    if raw_mode in ("1", "true", "yes", "on", "status", "local", "quick"):
        return "status"
    if raw_mode in ("live", "exec", "full", "remote"):
        return "live"
    raise ValueError(
        f"{CODEX_RESTORE_CAPACITY_ENV} is invalid: {raw_mode!r}. Use off, status, or live."
    )


def _codex_restore_capacity_timeout() -> float:
    raw_timeout = os.environ.get(CODEX_RESTORE_CAPACITY_TIMEOUT_ENV, "45").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(f"{CODEX_RESTORE_CAPACITY_TIMEOUT_ENV} is invalid: {raw_timeout!r}") from exc
    if timeout <= 0:
        raise ValueError(f"{CODEX_RESTORE_CAPACITY_TIMEOUT_ENV} must be > 0")
    return timeout


def _codex_output_preview(stdout: str, stderr: str, max_len: int = 800) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if not combined:
        return "no output"
    if len(combined) > max_len:
        return combined[:max_len] + "..."
    return combined


def _resolve_codex_bin() -> str:
    configured = os.environ.get(CODEX_BIN_ENV)
    if configured:
        return configured

    found = shutil.which("codex")
    if found:
        return found

    user_local = Path.home() / ".local" / "bin" / "codex"
    if user_local.is_file():
        return str(user_local)

    return "codex"


@contextlib.contextmanager
def _temporary_codex_home_for_auth(auth_path: Path):
    temp_parent = Path(tempfile.gettempdir()) / "codex-backup-verify"
    try:
        temp_parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temp_parent.chmod(0o700)
    except OSError as exc:
        raise CodexAuthVerificationError(
            f"cannot create temporary CODEX_HOME parent {temp_parent}: {exc}"
        ) from exc

    temp_dir = Path(tempfile.mkdtemp(prefix=".codex-verify-", dir=temp_parent))
    try:
        temp_dir.chmod(0o700)
        temp_auth = temp_dir / FILE_NAME
        shutil.copy2(auth_path, temp_auth)
        os.chmod(temp_auth, 0o600)
        _debug("Temporary CODEX_HOME created for auth verification", codex_home=str(temp_dir))
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        _debug("Temporary CODEX_HOME removed", codex_home=str(temp_dir))


@contextlib.contextmanager
def _codex_home_for_auth_file(auth_path: Path):
    if _auth_path_matches_codex_home(auth_path):
        yield auth_path.parent.resolve()
        return

    with _temporary_codex_home_for_auth(auth_path) as codex_home:
        yield codex_home


def _run_codex_command(
    args: List[str],
    codex_home: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    codex_bin = _resolve_codex_bin()
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    command = [codex_bin, *args]
    _debug("Running codex command", command=command, codex_home=str(codex_home), timeout=timeout)
    try:
        return subprocess.run(
            command,
            cwd="/tmp" if Path("/tmp").is_dir() else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CodexAuthVerificationError(
            f"codex binary not found: set {CODEX_BIN_ENV} or fix PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CodexAuthVerificationError(
            f"codex verification timed out after {timeout:g}s."
        ) from exc


def _verify_codex_login_status(codex_home: Path, timeout: float) -> Dict[str, Any]:
    result = _run_codex_command(["login", "status"], codex_home, timeout)
    output = _codex_output_preview(result.stdout, result.stderr)
    if result.returncode != 0 or "Logged in" not in output:
        raise CodexAuthVerificationError(
            f"codex login status is not valid (exit {result.returncode}): {output}"
        )
    return {
        "mode": "status",
        "ok": True,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        "detail": output.splitlines()[0],
    }


def _codex_live_jsonl_ok(stdout: str) -> bool:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        if str(item.get("text", "")).strip() == CODEX_VERIFY_LIVE_EXPECTED:
            return True
    return False


def _verify_codex_live(codex_home: Path, timeout: float) -> Dict[str, Any]:
    result = _run_codex_command(
        [
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--json",
            f"Reply exactly {CODEX_VERIFY_LIVE_EXPECTED}",
        ],
        codex_home,
        timeout,
    )
    if result.returncode != 0 or not _codex_live_jsonl_ok(result.stdout):
        raise CodexAuthVerificationError(
            f"codex exec live check is not valid (exit {result.returncode}): "
            f"{_codex_output_preview(result.stdout, result.stderr)}"
        )
    return {
        "mode": "live",
        "ok": True,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        "detail": "codex exec completed an authenticated request",
    }


def _verify_codex_auth_for_file(auth_path: Path, context: str) -> Optional[Dict[str, Any]]:
    mode = _codex_verify_mode()
    if mode == "off":
        _debug("Codex verification disabled", context=context)
        return None

    timeout = _codex_verify_timeout()
    with _codex_home_for_auth_file(auth_path) as codex_home:
        _debug(
            "Codex auth verification started",
            context=context,
            mode=mode,
            auth_path=str(auth_path),
            codex_home=str(codex_home),
        )
        if mode == "status":
            result = _verify_codex_login_status(codex_home, timeout)
        else:
            result = _verify_codex_live(codex_home, timeout)

    result["context"] = context
    result["auth_path"] = str(auth_path)
    _debug("Codex auth verification completed", context=context, mode=result["mode"])
    return result


def _percent_from_number(value: Any, *, fraction_allowed: bool = False) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        candidate = value.strip().rstrip("%")
        try:
            numeric = float(candidate)
        except ValueError:
            return None
    else:
        return None

    if fraction_allowed and 0 <= numeric <= 1:
        numeric *= 100
    if not (0 <= numeric <= 100):
        return None
    return int(round(numeric))


def _number_from_json(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip().rstrip("%")
        if not candidate:
            return None
        try:
            return float(candidate)
        except ValueError:
            return None
    return None


def _bounded_percent(value: float) -> int:
    return int(round(max(0.0, min(100.0, value))))


def _json_value_for_keys(record: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key, value in record.items():
        normalized = str(key).lower().replace("-", "_")
        if normalized in keys:
            return value
    return None


def _json_percent_for_keys(record: Dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    value = _json_value_for_keys(record, keys)
    if value is None:
        return None
    return _percent_from_number(value, fraction_allowed=True)


def _json_number_for_keys(record: Dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    value = _json_value_for_keys(record, keys)
    if value is None:
        return None
    return _number_from_json(value)


def _codex_window_text_is_5h(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.strip().lower()
    if not lowered:
        return False
    return bool(
        re.search(r"(^|[^0-9])5\s*(h|hr|hrs|hour|hours)\b", lowered)
        or re.search(r"(^|[^0-9])300\s*(m|min|mins|minute|minutes)\b", lowered)
    )


def _codex_limit_record_is_5h(record: Dict[str, Any]) -> bool:
    minutes = _json_number_for_keys(record, ("window_minutes", "window_mins", "minutes"))
    if minutes is not None and round(minutes) == CODEX_RESTORE_CAPACITY_WINDOW_MINUTES:
        return True

    seconds = _json_number_for_keys(record, ("window_seconds", "window_secs", "seconds"))
    if seconds is not None and round(seconds) == CODEX_RESTORE_CAPACITY_WINDOW_SECONDS:
        return True

    for key in ("window", "window_label", "duration", "name", "label", "limit_name"):
        value = _json_value_for_keys(record, (key,))
        if _codex_window_text_is_5h(value):
            return True
    return False


def _codex_reset_at_from_record(record: Dict[str, Any]) -> Optional[str]:
    value = _json_value_for_keys(
        record,
        (
            "resets_at",
            "reset_at",
            "reset_time",
            "window_reset_at",
            "retry_at",
        ),
    )
    if value is None:
        return None

    numeric = _number_from_json(value)
    if numeric is not None:
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    parsed = _parse_iso_datetime_utc(value)
    if parsed is not None:
        return parsed.isoformat()
    return str(value).strip() or None


def _codex_capacity_from_limit_record(
    record: Dict[str, Any],
    source: str,
) -> Optional[Dict[str, Any]]:
    remaining_percent = _json_percent_for_keys(
        record,
        (
            "available_percent",
            "remaining_percent",
            "left_percent",
            "free_percent",
            "percent_available",
            "percent_remaining",
        ),
    )
    used_percent = _json_percent_for_keys(
        record,
        (
            "used_percent",
            "usage_percent",
            "consumed_percent",
            "spent_percent",
            "percent_used",
        ),
    )

    if remaining_percent is None and used_percent is not None:
        remaining_percent = _bounded_percent(100 - used_percent)

    if remaining_percent is None:
        limit = _json_number_for_keys(record, ("limit", "total", "capacity", "max"))
        remaining = _json_number_for_keys(record, ("available", "remaining", "left", "free"))
        used = _json_number_for_keys(record, ("used", "usage", "consumed", "spent"))
        if limit is not None and limit > 0 and remaining is not None:
            remaining_percent = _bounded_percent((remaining / limit) * 100)
        elif limit is not None and limit > 0 and used is not None:
            remaining_percent = _bounded_percent(100 - ((used / limit) * 100))

    if remaining_percent is None:
        return None

    details: Dict[str, Any] = {
        "available_percent": remaining_percent,
        "window_minutes": CODEX_RESTORE_CAPACITY_WINDOW_MINUTES,
        "source": source,
    }
    if used_percent is not None:
        details["used_percent"] = used_percent
    else:
        details["used_percent"] = _bounded_percent(100 - remaining_percent)

    reset_at = _codex_reset_at_from_record(record)
    if reset_at is not None:
        details["reset_at"] = reset_at
    return details


def _parse_codex_5h_capacity_from_json(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        rate_limits = value.get("rate_limits")
        if isinstance(rate_limits, dict):
            primary = rate_limits.get("primary")
            if isinstance(primary, dict) and _codex_limit_record_is_5h(primary):
                parsed = _codex_capacity_from_limit_record(primary, "rate_limits.primary")
                if parsed is not None:
                    return parsed

            for key, child in rate_limits.items():
                if key == "primary":
                    continue
                if isinstance(child, dict) and _codex_limit_record_is_5h(child):
                    parsed = _codex_capacity_from_limit_record(child, f"rate_limits.{key}")
                    if parsed is not None:
                        return parsed

        if _codex_limit_record_is_5h(value):
            parsed = _codex_capacity_from_limit_record(value, "json.5h")
            if parsed is not None:
                return parsed

        for child in value.values():
            parsed = _parse_codex_5h_capacity_from_json(child)
            if parsed is not None:
                return parsed
        return None

    if isinstance(value, list):
        for child in value:
            parsed = _parse_codex_5h_capacity_from_json(child)
            if parsed is not None:
                return parsed
    return None


def _parse_codex_5h_capacity_from_text(text: str) -> Optional[Dict[str, Any]]:
    for line in text.splitlines():
        if not _codex_window_text_is_5h(line):
            continue
        percent = _parse_codex_capacity_percent_from_text(line)
        if percent is not None:
            return {
                "available_percent": percent,
                "window_minutes": CODEX_RESTORE_CAPACITY_WINDOW_MINUTES,
                "source": "text.5h",
            }
    return None


def _parse_codex_5h_capacity(output: str) -> Optional[Dict[str, Any]]:
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or not candidate.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        parsed = _parse_codex_5h_capacity_from_json(payload)
        if parsed is not None:
            return parsed

    return _parse_codex_5h_capacity_from_text(output)


def _iter_jsonl_tail_lines(path: Path, max_bytes: int = 2 * 1024 * 1024):
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return

    lines = data.decode("utf-8", errors="ignore").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    for line in reversed(lines):
        yield line


def _latest_codex_session_5h_capacity(codex_home: Path) -> Optional[Dict[str, Any]]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return None

    try:
        session_files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for session_file in session_files[:40]:
        for line in _iter_jsonl_tail_lines(session_file):
            candidate = line.strip()
            if not candidate or "rate_limits" not in candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            parsed = _parse_codex_5h_capacity_from_json(payload)
            if parsed is None:
                continue
            parsed = dict(parsed)
            parsed["source"] = f"session.{parsed.get('source', 'rate_limits')}"
            parsed["session_file"] = str(session_file)
            checked_at = _parse_iso_datetime_utc(payload.get("timestamp"))
            if checked_at is not None:
                parsed["checked_at"] = checked_at.isoformat()
            return parsed
    return None


def _codex_capacity_is_recent(
    capacity: Dict[str, Any],
    max_age_seconds: int = 15 * 60,
) -> bool:
    checked_at = _parse_iso_datetime_utc(capacity.get("checked_at"))
    if checked_at is None:
        return False
    age_seconds = (datetime.now(tz=timezone.utc) - checked_at).total_seconds()
    return -60 <= age_seconds <= max_age_seconds


def _parse_codex_capacity_percent_from_text(text: str) -> Optional[int]:
    if not text.strip():
        return None

    value_pattern = r"(\d{1,3}(?:\.\d+)?)"
    remaining_words = r"(available|remaining|left|free|capacity|disponibile|rimast[aoe]|residu[ao])"
    used_words = r"(used|consumed|spent|usage|utilizzat[aoe]|usat[aoe]|consumat[aoe])"

    remaining_patterns = (
        (rf"{remaining_words}[^\n%]{{0,48}}?{value_pattern}\s*%", 2),
        (rf"{value_pattern}\s*%[^\n]{{0,32}}?{remaining_words}", 1),
    )
    for pattern, value_group in remaining_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _percent_from_number(match.group(value_group))

    used_patterns = (
        (rf"{used_words}[^\n%]{{0,48}}?{value_pattern}\s*%", 2),
        (rf"{value_pattern}\s*%[^\n]{{0,32}}?{used_words}", 1),
    )
    for pattern, value_group in used_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            used = _percent_from_number(match.group(value_group))
            if used is not None:
                return max(0, min(100, 100 - used))

    return None


def _parse_codex_capacity_percent_from_json(value: Any) -> Optional[int]:
    remaining_markers = ("available", "remaining", "left", "free", "capacity")
    used_markers = ("used", "consumed", "spent", "usage")

    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in remaining_markers):
                percent = _percent_from_number(child, fraction_allowed=True)
                if percent is not None:
                    return percent
            if any(marker in key_lower for marker in used_markers):
                used = _percent_from_number(child, fraction_allowed=True)
                if used is not None:
                    return max(0, min(100, 100 - used))

        for child in value.values():
            percent = _parse_codex_capacity_percent_from_json(child)
            if percent is not None:
                return percent
        return None

    if isinstance(value, list):
        for child in value:
            percent = _parse_codex_capacity_percent_from_json(child)
            if percent is not None:
                return percent
        return None

    if isinstance(value, str):
        return _parse_codex_capacity_percent_from_text(value)
    return None


def _parse_codex_capacity_percent(output: str) -> Optional[int]:
    capacity = _parse_codex_5h_capacity(output)
    if capacity is not None:
        percent = capacity.get("available_percent")
        if isinstance(percent, int):
            return percent

    percent = _parse_codex_capacity_percent_from_text(output)
    if percent is not None:
        return percent

    for line in output.splitlines():
        candidate = line.strip()
        if not candidate or not candidate.startswith(("{", "[")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        percent = _parse_codex_capacity_percent_from_json(payload)
        if percent is not None:
            return percent
    return None


def _codex_output_indicates_capacity_exhausted(output: str) -> bool:
    lowered = output.lower()
    exhausted_phrases = (
        "usage limit reached",
        "rate limit reached",
        "quota exceeded",
        "insufficient_quota",
        "capacity exhausted",
        "no capacity remaining",
    )
    return any(phrase in lowered for phrase in exhausted_phrases)


def _codex_output_indicates_network_error(output: str) -> bool:
    lowered = output.lower()
    network_phrases = (
        "operation not permitted",
        "error sending request",
        "failed to connect",
        "http/request failed",
        "stream disconnected before completion",
    )
    return any(phrase in lowered for phrase in network_phrases)


def _codex_output_error_message(output: str) -> Optional[str]:
    messages: List[str] = []
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "error":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
        turn_error = payload.get("error")
        if isinstance(turn_error, dict):
            message = turn_error.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())

    if messages:
        return messages[-1]

    for line in output.splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("{"):
            return cleaned
    return None


def _format_codex_capacity_label(result: Dict[str, Any]) -> str:
    percent = result.get("available_percent")
    if isinstance(percent, int):
        return f"{percent}%"
    if result.get("ok"):
        return "unknown"
    return str(result.get("label") or "fail")


def _codex_command_output(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)


def _read_auth_payload(auth_path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _auth_identity_values(auth_path: Path) -> Dict[str, str]:
    payload = _read_auth_payload(auth_path)
    if payload is None:
        return {}

    tokens = payload.get("tokens")
    token_account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    values: Dict[str, str] = {}
    for key, value in (
        ("account_id", payload.get("account_id") or token_account_id),
        ("user_id", payload.get("user_id")),
        ("email", payload.get("email")),
    ):
        if isinstance(value, str) and value.strip():
            values[key] = value.strip().lower() if key == "email" else value.strip()
    return values


def _auth_identity_matches(first_auth_path: Path, second_auth_path: Path) -> bool:
    first = _auth_identity_values(first_auth_path)
    second = _auth_identity_values(second_auth_path)
    for key in ("account_id", "user_id", "email"):
        if first.get(key) and first.get(key) == second.get(key):
            return True
    return False


def _default_codex_session_5h_capacity_for_auth(auth_path: Path) -> Optional[Dict[str, Any]]:
    codex_home = _get_default_codex_home()
    current_auth = codex_home / FILE_NAME
    if not current_auth.is_file() or not _auth_identity_matches(auth_path, current_auth):
        return None

    capacity = _latest_codex_session_5h_capacity(codex_home)
    if capacity is None:
        return None
    capacity = dict(capacity)
    capacity["source"] = f"default_{capacity.get('source', 'session')}"
    return capacity


def _check_codex_capacity_for_auth(auth_path: Path) -> Dict[str, Any]:
    mode = _codex_restore_capacity_mode()
    if mode == "off":
        return {"mode": mode, "ok": None, "available_percent": None, "label": "off"}

    default_session_capacity = _default_codex_session_5h_capacity_for_auth(auth_path)
    if mode == "live" and default_session_capacity is not None and _codex_capacity_is_recent(default_session_capacity):
        percent = default_session_capacity.get("available_percent")
        if isinstance(percent, int):
            extra = dict(default_session_capacity)
            extra.pop("available_percent", None)
            return {
                "mode": mode,
                "ok": True,
                "available_percent": percent,
                "label": "OK",
                "detail": "latest local Codex 5h token_count",
                **extra,
            }

    timeout = _codex_restore_capacity_timeout()
    session_capacity = None
    try:
        with _codex_home_for_auth_file(auth_path) as codex_home:
            if mode == "live":
                result = _run_codex_command(
                    [
                        "--ask-for-approval",
                        "never",
                        "exec",
                        "--ephemeral",
                        "--skip-git-repo-check",
                        "--ignore-rules",
                        "--ignore-user-config",
                        "--sandbox",
                        "read-only",
                        "--json",
                        f"Reply exactly {CODEX_VERIFY_LIVE_EXPECTED}",
                    ],
                    codex_home,
                    timeout,
                )
                session_capacity = _latest_codex_session_5h_capacity(codex_home)
            else:
                result = _run_codex_command(["login", "status"], codex_home, timeout)
    except (CodexAuthVerificationError, OSError, shutil.Error) as exc:
        if default_session_capacity is not None:
            percent = default_session_capacity.get("available_percent")
            if isinstance(percent, int):
                extra = dict(default_session_capacity)
                extra.pop("available_percent", None)
                return {
                    "mode": mode,
                    "ok": False,
                    "available_percent": percent,
                    "label": "fallback",
                    "detail": "latest local Codex 5h token_count after live check failed",
                    "error": str(exc),
                    **extra,
                }
        label = "timeout" if "timed out" in str(exc).lower() else "fail"
        return {
            "mode": mode,
            "ok": False,
            "available_percent": None,
            "label": label,
            "error": str(exc),
        }

    full_output = _codex_command_output(result.stdout, result.stderr)
    output = _codex_output_preview(result.stdout, result.stderr, max_len=4000)
    error_message = _codex_output_error_message(full_output)
    capacity = _parse_codex_5h_capacity(full_output)
    if capacity is None:
        capacity = session_capacity
    if capacity is None:
        capacity = default_session_capacity
    percent = None
    extra: Dict[str, Any] = {}
    if capacity is not None:
        percent = capacity.get("available_percent")
        extra.update(capacity)
    else:
        percent = _parse_codex_capacity_percent(full_output)
        if percent is not None:
            extra["source"] = "legacy"

    if percent is None and _codex_output_indicates_capacity_exhausted(full_output):
        percent = 0
        extra.update(
            {
                "available_percent": percent,
                "window_minutes": CODEX_RESTORE_CAPACITY_WINDOW_MINUTES,
                "source": "exhausted",
            }
        )

    if mode == "live":
        ok = result.returncode == 0 and _codex_live_jsonl_ok(result.stdout)
    else:
        ok = result.returncode == 0 and "logged in" in output.lower()

    if ok:
        label = "OK" if isinstance(percent, int) else "unknown"
    elif _codex_output_indicates_network_error(full_output):
        label = "network"
    else:
        label = "fail"
    extra.pop("available_percent", None)
    result_detail = {
        "mode": mode,
        "ok": ok,
        "available_percent": percent,
        "label": label,
        "detail": output.splitlines()[0] if output.splitlines() else "",
        **extra,
    }
    if error_message and not ok:
        result_detail["error"] = error_message
    return result_detail


def _codex_capacity_cache_key(auth_path: Path) -> str:
    payload = _read_auth_payload(auth_path)
    if payload is None:
        return f"file:{_sha256_file(auth_path)}"

    tokens = payload.get("tokens")
    token_account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    account_id = payload.get("account_id") or token_account_id
    user_id = payload.get("user_id")
    email = payload.get("email")
    if account_id:
        identity = f"account:{account_id}"
    elif user_id:
        identity = f"user:{user_id}"
    elif email:
        identity = f"email:{str(email).strip().lower()}"
    else:
        identity = ""
    if not identity:
        return f"file:{_sha256_file(auth_path)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"identity:{digest}"


def _annotate_backups_with_codex_capacity(backups: List[Dict[str, Any]]) -> None:
    mode = _codex_restore_capacity_mode()
    if mode == "off":
        for item in backups:
            item["capacity"] = "off"
        return

    mode_label = "live 5h rate-limit request" if mode == "live" else "login status"
    _print_status("INFO", "Checking Codex 5h remaining", f"{len(backups)} saved tokens via {mode_label}")
    capacity_items: Dict[str, List[Dict[str, Any]]] = {}
    capacity_auth_paths: Dict[str, Path] = {}
    for item in backups:
        backup_dir = item.get("dir")
        auth_path = Path(backup_dir) / FILE_NAME if backup_dir is not None else Path(FILE_NAME)
        if not auth_path.is_file():
            item["capacity"] = "missing"
            continue
        cache_key = _codex_capacity_cache_key(auth_path)
        capacity_items.setdefault(cache_key, []).append(item)
        capacity_auth_paths.setdefault(cache_key, auth_path)

    def apply_result(cache_key: str, result: Dict[str, Any]) -> None:
        for item in capacity_items[cache_key]:
            item["capacity"] = _format_codex_capacity_label(result)
            item["capacity_detail"] = dict(result)

    def check_capacity(cache_key: str) -> Dict[str, Any]:
        try:
            return _check_codex_capacity_for_auth(capacity_auth_paths[cache_key])
        except Exception as exc:  # pragma: no cover - last-resort UI guard
            _debug(
                "Codex restore capacity check failed unexpectedly",
                cache_key=cache_key,
                error=str(exc),
            )
            return {
                "mode": mode,
                "ok": False,
                "available_percent": None,
                "label": "fail",
                "error": str(exc),
            }

    if not capacity_items:
        return

    worker_count = min(3, len(capacity_items))
    if worker_count == 1:
        cache_key = next(iter(capacity_items))
        apply_result(cache_key, check_capacity(cache_key))
        return

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_keys = {
            executor.submit(check_capacity, cache_key): cache_key
            for cache_key in capacity_items
        }
        for future in as_completed(future_keys):
            cache_key = future_keys[future]
            apply_result(cache_key, future.result())


EXPIRY_KEY_MARKERS = (
    "exp",
    "expires_at",
    "expires",
    "expiry",
    "expiry_at",
    "expired",
    "valid_until",
    "valid_to",
    "validfrom",
    "valid_through",
)

ACCOUNT_KEY_MARKERS = (
    "email",
    "account",
    "username",
    "user",
)

ID_KEY_MARKERS = (
    "sub",
    "id",
    "user_id",
    "account_id",
)

PROVIDER_KEY_MARKERS = (
    "provider",
    "issuer",
    "iss",
    "aud",
    "audience",
)


def _normalize_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _safe_filename_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value.strip())
    cleaned = cleaned.strip("._-")
    if not cleaned:
        return fallback
    return cleaned[:80]


def _sha256_file(path: Path) -> str:
    _debug("Computing SHA256 hash", path=str(path))
    digest = hashlib.sha256()
    chunks = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
            chunks += 1
    hex_digest = digest.hexdigest()
    _debug("SHA256 hash computed", path=str(path), chunks=chunks, sha256=hex_digest)
    return hex_digest


def _looks_like_email(value: str) -> bool:
    return bool(EMAIL_RE.fullmatch(value))


# Accepted Unix timestamp range for the `exp` claim.
# Outside these bounds `datetime.fromtimestamp(..., tz=utc)` raises
# ValueError/OverflowError (year outside 1..9999): such an `exp` is not a
# plausible timestamp and must be discarded here, the single place that feeds
# all timestamps later passed to fromtimestamp downstream.
JWT_EXP_MIN = 0
JWT_EXP_MAX = 253402300799  # 9999-12-31T23:59:59Z


def _coerce_valid_exp(value: int, key_hint: str, path_hint: str) -> Optional[int]:
    if not (JWT_EXP_MIN <= value <= JWT_EXP_MAX):
        _debug(
            "JWT exp out of range, discarded",
            key_hint=key_hint,
            path_hint=path_hint,
            exp=value,
            min=JWT_EXP_MIN,
            max=JWT_EXP_MAX,
        )
        return None
    _debug("JWT exp extracted", key_hint=key_hint, path_hint=path_hint, exp=value)
    return value


def _extract_jwt_expiry(
    token: str,
    key_hint: str = "",
    path_hint: str = "",
) -> Optional[int]:
    """Return the `exp` claim if the value looks like a valid JWT."""
    if not JWT_TOKEN_RE.fullmatch(token):
        _debug("JWT is not valid base64url by regex", token=token[:32], token_len=len(token))
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    payload = parts[1]
    _debug("Parsing JWT", token_preview=token[:28], payload_preview=payload[:32])
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}")
        claim = json.loads(decoded.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        _debug("JWT parsing failed", token_preview=token[:28], payload_preview=payload[:32])
        return None

    if not isinstance(claim, dict):
        _debug("JWT claim is not a dict", token_preview=token[:28], claim_type=type(claim).__name__)
        return None

    normalized_hint = f"{key_hint} {path_hint}".lower()
    has_token_hint = any(marker in normalized_hint for marker in JWT_KEY_HINT_MARKERS)
    has_jwt_context = any(
        hint in claim
        for hint in ("iss", "aud", "audience", "sub", "uid", "email", "scope", "iat", "nbf")
    )
    if not has_token_hint and not has_jwt_context:
        _debug("JWT discarded: no token context", key_hint=key_hint, path_hint=path_hint)
        return None

    exp = claim.get("exp")
    if isinstance(exp, bool):
        _debug("JWT exp is boolean", exp=exp)
        return None
    if isinstance(exp, int):
        return _coerce_valid_exp(exp, key_hint, path_hint)
    if isinstance(exp, str) and exp.isdigit():
        try:
            value = int(exp)
        except ValueError:
            _debug("JWT exp is not convertible", key_hint=key_hint, path_hint=path_hint, exp=exp)
            return None
        return _coerce_valid_exp(value, key_hint, path_hint)
    return None


def _classify_jwt(token_type_hint: str, path: str) -> str:
    lowered = token_type_hint.lower()
    if "id_token" in lowered or "idtoken" in lowered:
        return "id_token"
    if "access_token" in lowered or "accesstoken" in lowered:
        return "access_token"
    if "refresh_token" in lowered or "refreshtoken" in lowered:
        return "refresh_token"
    if "token_type" in lowered:
        return "token_type"
    if "jwt" in path.lower() or path.endswith(".tokens"):
        return "jwt"
    return "other"


def _days_until_expiry(exp: int, now: int) -> int:
    delta = exp - now
    if delta >= 0:
        return (delta + 86399) // 86400
    return delta // 86400


def _parse_iso_datetime_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None

    try:
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expiry_entry_timestamp(entry: Dict[str, Any]) -> Optional[int]:
    raw_exp = entry.get("exp")
    if not isinstance(raw_exp, bool) and raw_exp is not None:
        try:
            return int(raw_exp)
        except (TypeError, ValueError):
            pass

    parsed = _parse_iso_datetime_utc(entry.get("expires_at_utc"))
    if parsed is None:
        return None
    return int(parsed.timestamp())


def _expiry_reference_timestamp(analyzed: Dict[str, Any]) -> int:
    analyzed_at = _parse_iso_datetime_utc(analyzed.get("analyzed_at"))
    if analyzed_at is not None:
        return int(analyzed_at.timestamp())
    return int(datetime.now(tz=timezone.utc).timestamp())


def _select_expiry_candidate(
    candidates: List[tuple[int, Dict[str, Any]]],
    reference_ts: int,
) -> Optional[tuple[int, Dict[str, Any]]]:
    if not candidates:
        return None

    future = [(exp, entry) for exp, entry in candidates if exp >= reference_ts]
    if future:
        return min(future, key=lambda item: item[0])
    return max(candidates, key=lambda item: item[0])


def _select_jwt_expiry_candidate(
    candidates: List[tuple[int, Dict[str, Any]]],
    reference_ts: int,
) -> Optional[tuple[int, Dict[str, Any]]]:
    if not candidates:
        return None

    preferred = [
        (exp, entry)
        for exp, entry in candidates
        if str(entry.get("token_type", "")).lower() in BACKUP_EXPIRY_PREFERRED_TOKEN_TYPES
    ]
    future_preferred = [(exp, entry) for exp, entry in preferred if exp >= reference_ts]
    if future_preferred:
        return min(future_preferred, key=lambda item: item[0])

    future = [(exp, entry) for exp, entry in candidates if exp >= reference_ts]
    if future:
        return min(future, key=lambda item: item[0])

    if preferred:
        return max(preferred, key=lambda item: item[0])
    return max(candidates, key=lambda item: item[0])


def _build_backup_expiry_metadata(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "analyze_preferred",
        "path": entry.get("path"),
        "token_type": entry.get("token_type"),
        "exp": entry.get("exp"),
        "expires_at_utc": entry.get("expires_at_utc"),
    }


def _get_snapshot_backup_expiry(analyzed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    snapshot = analyzed.get("auth_snapshot")
    if not isinstance(snapshot, dict):
        return None
    backup_expiry = snapshot.get("backup_expiry")
    if (
        isinstance(backup_expiry, dict)
        and isinstance(backup_expiry.get("expires_at_utc"), str)
        and backup_expiry.get("expires_at_utc", "").strip()
    ):
        return backup_expiry
    return None


def _build_auth_snapshot(
    analyzed: Dict[str, Any],
    codex_verification: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    extracted = analyzed.get("extracted", {})
    if not isinstance(extracted, dict):
        extracted = {}

    preferred_expiry_entry = _extract_preferred_expiry_entry(analyzed)
    backup_expiry = (
        _build_backup_expiry_metadata(preferred_expiry_entry)
        if isinstance(preferred_expiry_entry, dict) and preferred_expiry_entry.get("expires_at_utc")
        else None
    )

    return {
        "schema_version": 1,
        "created_at": created_at or datetime.now(tz=timezone.utc).isoformat(),
        "source": analyzed.get("source"),
        "source_size": analyzed.get("source_size"),
        "source_mtime": analyzed.get("source_mtime"),
        "source_sha256": analyzed.get("sha256"),
        "analyzed_at": analyzed.get("analyzed_at"),
        "backup_expiry": backup_expiry,
        "jwt_expired": extracted.get("jwt_expired"),
        "jwt_expired_by_type": extracted.get("jwt_expired_by_type", {}),
        "jwt_token_types": extracted.get("jwt_token_types", []),
        "codex_valid": (
            bool(codex_verification.get("ok"))
            if isinstance(codex_verification, dict)
            else None
        ),
        "codex_auth_verification": codex_verification,
    }


def _sync_auth_snapshot(
    analyzed: Dict[str, Any],
    codex_verification: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    previous_snapshot = analyzed.get("auth_snapshot")
    created_at = None
    if isinstance(previous_snapshot, dict) and isinstance(previous_snapshot.get("created_at"), str):
        created_at = previous_snapshot["created_at"]
    elif isinstance(analyzed.get("analyzed_at"), str):
        created_at = analyzed["analyzed_at"]

    snapshot = _build_auth_snapshot(analyzed, codex_verification, created_at)
    analyzed["auth_snapshot"] = snapshot
    return snapshot


def _extract_preferred_expiry_entry(analyzed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the token/entry to use as the main expiry.

    Rules:
      - prefer a non-expired access_token because it represents the operational
        validity reused by Codex
      - then use the first non-expired token with the exp closest to the analysis time
      - if all tokens are expired, still use the one with the most recent exp
      - fall back to jwt_summary or jwt_expiry_utc using the same rule
      - use the saved backup_expiry only when there are no JWT data to recompute
    """
    extracted = analyzed.get("extracted", {})
    if not isinstance(extracted, dict):
        return None

    reference_ts = _expiry_reference_timestamp(analyzed)
    jwt_tokens = extracted.get("jwt_tokens", [])

    if isinstance(jwt_tokens, list) and jwt_tokens:
        candidates: List[tuple[int, Dict[str, Any]]] = []
        for token in jwt_tokens:
            if not isinstance(token, dict):
                continue
            exp = _expiry_entry_timestamp(token)
            if exp is None:
                continue
            candidates.append((exp, token))

        selected = _select_jwt_expiry_candidate(candidates, reference_ts)
        if selected is not None:
            exp, token = selected
            _debug(
                "Backup expiry selected (preferred token)",
                token_type=token.get("token_type"),
                token_path=token.get("path"),
                exp=exp,
                reference_ts=reference_ts,
            )
            return token

    jwt_summary = extracted.get("jwt_summary", {})
    if isinstance(jwt_summary, dict):
        summary_candidates: List[tuple[int, Dict[str, Any]]] = []
        for key in (
            "first_to_expire",
            "earliest_expiring",
            "oldest_expiry_token",
            "last_to_expire",
            "latest_expiring",
            "most_recent_expiry_token",
        ):
            entry = jwt_summary.get(key)
            if not isinstance(entry, dict) or not isinstance(entry.get("expires_at_utc"), str):
                continue
            exp = _expiry_entry_timestamp(entry)
            if exp is None:
                continue
            summary_candidates.append((exp, entry))

        selected = _select_expiry_candidate(summary_candidates, reference_ts)
        if selected is not None:
            exp, entry = selected
            _debug(
                "Backup expiry selected (summary fallback)",
                value=entry.get("expires_at_utc"),
                exp=exp,
                reference_ts=reference_ts,
            )
            return entry

    jwt_expiry_utc = extracted.get("jwt_expiry_utc")
    if isinstance(jwt_expiry_utc, list) and jwt_expiry_utc:
        list_candidates: List[tuple[int, Dict[str, Any]]] = []
        for candidate in jwt_expiry_utc:
            if not isinstance(candidate, str):
                continue
            entry = {"expires_at_utc": candidate}
            exp = _expiry_entry_timestamp(entry)
            if exp is None:
                continue
            list_candidates.append((exp, entry))

        selected = _select_expiry_candidate(list_candidates, reference_ts)
        if selected is not None:
            exp, entry = selected
            _debug(
                "Backup expiry selected (list fallback)",
                value=entry.get("expires_at_utc"),
                exp=exp,
                reference_ts=reference_ts,
            )
            return entry

    stored_backup_expiry = extracted.get("backup_expiry")
    if (
        isinstance(stored_backup_expiry, dict)
        and isinstance(stored_backup_expiry.get("expires_at_utc"), str)
        and stored_backup_expiry.get("expires_at_utc", "").strip()
    ):
        _debug(
            "Backup expiry selected (saved metadata)",
            token_type=stored_backup_expiry.get("token_type"),
            token_path=stored_backup_expiry.get("path"),
            exp=stored_backup_expiry.get("exp"),
        )
        return stored_backup_expiry

    return None


def _refresh_backup_expiry_metadata(meta: Dict[str, Any]) -> bool:
    extracted = meta.get("extracted")
    if not isinstance(extracted, dict):
        return False

    entry = _extract_preferred_expiry_entry(meta)
    if not isinstance(entry, dict) or not entry.get("expires_at_utc"):
        return False

    refreshed = _build_backup_expiry_metadata(entry)
    changed = extracted.get("backup_expiry") != refreshed

    extracted["backup_expiry"] = refreshed
    previous_snapshot = meta.get("auth_snapshot")
    codex_verification = None
    if isinstance(previous_snapshot, dict):
        saved_verification = previous_snapshot.get("codex_auth_verification")
        if isinstance(saved_verification, dict):
            codex_verification = saved_verification
    if codex_verification is None and isinstance(meta.get("codex_auth_verification"), dict):
        codex_verification = meta["codex_auth_verification"]

    snapshot = _sync_auth_snapshot(meta, codex_verification)
    return changed or previous_snapshot != snapshot


def _backup_first_expiry_label(analyzed: Dict[str, Any]) -> str:
    entry = _get_snapshot_backup_expiry(analyzed)
    if not isinstance(entry, dict):
        entry = _extract_preferred_expiry_entry(analyzed)
    if not isinstance(entry, dict):
        return "no_expiry"

    raw_expiry = entry.get("expires_at_utc")
    if not isinstance(raw_expiry, str):
        return "no_expiry"

    candidate = raw_expiry.strip()
    if not candidate:
        return "no_expiry"

    parsed = _parse_iso_datetime_utc(candidate)
    if parsed is not None:
        return parsed.date().isoformat()

    match = re.match(r"^(\d{4}-\d{2}-\d{2})", candidate)
    if match:
        return match.group(1)
    return "no_expiry"


def _format_backup_timestamp(ts: Optional[datetime] = None) -> str:
    when = ts or datetime.now(tz=timezone.utc)
    return when.strftime("%Y-%m-%d")


def _build_backup_folder_name(
    analyzed: Dict[str, Any],
    created_at: Optional[datetime] = None,
    index: int = 0,
) -> str:
    ts = _format_backup_timestamp(created_at)
    expiry = _backup_first_expiry_label(analyzed)
    return f"backup{index:02d}-{ts}_exp-{expiry}"


def _resolve_unique_backup_dir_name(
    root: Path,
    analyzed: Dict[str, Any],
    created_at: Optional[datetime] = None,
    exclude: Optional[Path] = None,
) -> Path:
    suffix = 0
    while True:
        candidate = root / _build_backup_folder_name(analyzed, created_at, suffix)
        if not candidate.exists() or candidate == exclude:
            return candidate
        suffix += 1


def _parse_backup_creation_from_meta(meta: Dict[str, Any], backup_dir: Path) -> Optional[datetime]:
    if not isinstance(meta, dict):
        return None
    analyzed_at = meta.get("analyzed_at")
    if isinstance(analyzed_at, str):
        parsed = _parse_iso_datetime_utc(analyzed_at)
        if parsed is not None:
            return parsed
        _debug("analyzed_at is not parseable", analyzed_at=analyzed_at)

    try:
        mtime = backup_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        return None


def _write_metadata_file(meta_file: Path, meta: Dict[str, Any]) -> None:
    temp_path = meta_file.with_name(f".{meta_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, meta_file)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _migrate_backup_names(backup_root: Path) -> tuple[int, int]:
    """Rename legacy backups into the backupNN-date_exp format.

    Return (total_scanned, migrated).
    """
    if not backup_root.exists():
        _debug("Backup root missing for name migration", root=str(backup_root))
        return 0, 0

    scanned = 0
    migrated = 0
    meta_missing = 0
    meta_corrupt = 0
    meta_missing_names: list[str] = []
    meta_corrupt_names: list[str] = []
    legacy_pattern = re.compile(r"^data_backup-[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}_.+")
    already_new_pattern = re.compile(r"^backup\d{2}-\d{4}-\d{2}-\d{2}_exp-")
    new_with_index_pattern = re.compile(r"^backup(?P<index>\d{2})-(?P<date>\d{4}-\d{2}-\d{2})_exp-(?P<expiry>.+)$")

    for entry in backup_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".codex-verify-"):
            _debug("Migration skipped (temporary CODEX_HOME)", entry=str(entry))
            continue
        scanned += 1
        meta_file = entry / META_NAME
        if not meta_file.is_file():
            _debug("Migration skipped (missing metadata)", entry=str(entry))
            meta_missing += 1
            meta_missing_names.append(entry.name)
            continue

        if already_new_pattern.match(entry.name):
            try:
                with meta_file.open("r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                _debug("Migration skipped (unreadable metadata)", entry=str(entry))
                meta_corrupt += 1
                meta_corrupt_names.append(entry.name)
                continue

            match = new_with_index_pattern.match(entry.name)
            if not match:
                _debug("Migration skipped (invalid new pattern)", entry=str(entry))
                continue

            if _refresh_backup_expiry_metadata(meta):
                _write_metadata_file(meta_file, meta)
                _debug("backup_expiry metadata updated", entry=str(entry))

            created_at = _parse_backup_creation_from_meta(meta, entry)
            index = int(match.group("index"))
            target = backup_root / _build_backup_folder_name(meta, created_at, index)
            if target == entry:
                _debug("Migration skipped (name already correct)", entry=str(entry))
                continue
            if target.exists():
                _debug(
                    "Migration name collision (new format)",
                    source=str(entry),
                    target=str(target),
                )
                target = _resolve_unique_backup_dir_name(backup_root, meta, created_at)

            try:
                entry.rename(target)
                migrated += 1
                _debug("Migrated backup directory", source=str(entry), target=str(target))
            except OSError as exc:
                _debug(
                    "Error while migrating backup directory",
                    source=str(entry),
                    target=str(target),
                    error=str(exc),
                )
            continue

        if not legacy_pattern.match(entry.name):
            _debug("Migration skipped (not a legacy name)", entry=str(entry))
            continue

        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            _debug("Migration skipped (unreadable metadata)", entry=str(entry))
            meta_corrupt += 1
            meta_corrupt_names.append(entry.name)
            continue

        if _refresh_backup_expiry_metadata(meta):
            _write_metadata_file(meta_file, meta)
            _debug("backup_expiry metadata updated", entry=str(entry))

        created_at = _parse_backup_creation_from_meta(meta, entry)
        target = _resolve_unique_backup_dir_name(backup_root, meta, created_at, exclude=entry)
        if target == entry:
            _debug("Migration skipped (already consistent)", entry=str(entry))
            continue

        if target.exists():
            _debug(
                "Migration name collision",
                source=str(entry),
                target=str(target),
            )

        try:
            entry.rename(target)
            migrated += 1
            _debug("Migrated backup directory", source=str(entry), target=str(target))
        except OSError as exc:
            _debug(
                "Error while migrating backup directory",
                source=str(entry),
                target=str(target),
                error=str(exc),
            )

    if meta_missing:
        missing_preview = ", ".join(meta_missing_names[:3]) + (", ..." if len(meta_missing_names) > 3 else "")
        print(
            f"WARNING: {backup_root}: {meta_missing} directories excluded from migration due to missing metadata "
            f"({missing_preview})",
            file=sys.stderr,
        )

    if meta_corrupt:
        corrupt_preview = ", ".join(meta_corrupt_names[:3]) + (", ..." if len(meta_corrupt_names) > 3 else "")
        print(
            f"WARNING: {backup_root}: {meta_corrupt} directories excluded from migration due to unreadable/corrupt metadata "
            f"({corrupt_preview})",
            file=sys.stderr,
        )

    _debug(
        "Backup name migration completed",
        root=str(backup_root),
        scanned=scanned,
        migrated=migrated,
        meta_missing=meta_missing,
        meta_corrupt=meta_corrupt,
    )
    return scanned, migrated


def _walk_json(
    value: Any,
    emails: Set[str],
    expiries: Set[str],
    jwt_expiry_timestamps: Set[int],
    jwt_tokens: List[Dict[str, str]],
    account_ids: Set[str],
    account_names: Set[str],
    providers: Set[str],
    top_level_keys: List[str],
    path_hint: str = "",
    depth: int = 0,
    ) -> None:
    _debug(
        "Walking JSON",
        path_hint=path_hint,
        depth=depth,
        node_type=type(value).__name__,
    )
    if isinstance(value, dict):
        for key, child in value.items():
            key_lower = _normalize_string(key).lower()
            if depth == 0:
                top_level_keys.append(_normalize_string(key))

            normalized = _normalize_string(child)
            child_path = f"{path_hint}.{key}" if path_hint else key

            if "email" in key_lower and normalized and _looks_like_email(normalized):
                emails.add(normalized)

            if any(marker in key_lower for marker in EXPIRY_KEY_MARKERS):
                if normalized:
                    expiries.add(normalized)

            if isinstance(child, str):
                jwt_expiry = _extract_jwt_expiry(normalized, key_lower, child_path)
                if jwt_expiry is not None:
                    token_type = _classify_jwt(key_lower, child_path)
                    jwt_expiry_timestamps.add(jwt_expiry)
                    jwt_tokens.append(
                        {
                            "token_type": token_type,
                            "path": child_path,
                            "exp": str(jwt_expiry),
                            "expires_at_utc": datetime.fromtimestamp(
                                jwt_expiry,
                                tz=timezone.utc,
                            ).isoformat(),
                        }
                    )
                    _debug(
                        "JWT extracted from JSON",
                        path=child_path,
                        token_type=token_type,
                        exp=jwt_expiry,
                    )

            if any(marker in key_lower for marker in ID_KEY_MARKERS):
                if normalized:
                    account_ids.add(normalized)

            if any(marker in key_lower for marker in ACCOUNT_KEY_MARKERS):
                if normalized:
                    account_names.add(normalized)

            if any(marker in key_lower for marker in PROVIDER_KEY_MARKERS):
                if normalized:
                    providers.add(normalized)

            _walk_json(
                child,
                emails,
                expiries,
                jwt_expiry_timestamps,
                jwt_tokens,
                account_ids,
                account_names,
                providers,
                top_level_keys,
                child_path,
                depth + 1,
            )

    elif isinstance(value, list):
        _debug("Walking JSON list", path_hint=path_hint, depth=depth, size=len(value))
        list_path = f"{path_hint}[]"
        for item in value:
            _walk_json(
                item,
                emails,
                expiries,
                jwt_expiry_timestamps,
                jwt_tokens,
                account_ids,
                account_names,
                providers,
                top_level_keys,
                list_path,
                depth + 1,
            )


def _build_signature(meta: Dict[str, Any]) -> str:
    _debug("Building metadata signature", meta_keys=list(meta.keys()))
    extracted = meta.get("extracted", meta)
    signature = {
        "emails": sorted(extracted.get("emails", [])),
        "expiries": sorted(extracted.get("expiries", [])),
        "account_ids": sorted(extracted.get("account_ids", [])),
        "account_names": sorted(extracted.get("account_names", [])),
        "providers": sorted(extracted.get("providers", [])),
        "jwt_expiry_unix": sorted(extracted.get("jwt_expiry_unix", [])),
        "jwt_token_types": sorted(extracted.get("jwt_token_types", [])),
        "top_level_keys": sorted(extracted.get("top_level_keys", [])),
    }
    source_sha = meta.get("sha256")
    source_size = meta.get("source_size")
    if source_sha is not None and source_size is not None:
        signature["signature_version"] = 2
        signature["source_sha256"] = source_sha
        signature["source_size"] = source_size
    else:
        signature["signature_version"] = 1
    signature_dump = json.dumps(signature, sort_keys=True, separators=(",", ":"))
    _debug(
        "Signature built",
        signature_version=signature.get("signature_version"),
        fields=list(signature.keys()),
        signature_preview=signature_dump[:120],
    )
    return signature_dump


def _read_existing_signatures(backup_root: Path) -> Dict[str, Path]:
    _debug("Reading signatures from backup root", root=str(backup_root))
    signatures: Dict[str, Path] = {}
    if not backup_root.exists():
        _debug("Backup root does not exist", root=str(backup_root))
        return signatures

    for entry in backup_root.iterdir():
        if not entry.is_dir():
            continue
        meta_file = entry / META_NAME
        if not meta_file.is_file():
            continue
        try:
            with meta_file.open("r", encoding="utf-8") as f:
                meta = json.load(f)
            signature = _build_signature(meta)
            signatures.setdefault(signature, entry)
        except (OSError, json.JSONDecodeError):
            _debug("Metadata file is not readable", path=str(meta_file))
            continue

    _debug("Signatures read", root=str(backup_root), total=len(signatures))
    return signatures


def _read_signatures_from_known_roots() -> Dict[str, Path]:
    signatures: Dict[str, Path] = {}
    roots = _iter_backup_roots()
    for root in roots:
        _debug("Checking root for dedupe", root=str(root))
        for signature, backup_dir in _read_existing_signatures(root).items():
            signatures.setdefault(signature, backup_dir)
    _debug("Total signatures collected", total=len(signatures))
    return signatures


def _load_backup_metadata(backup_dir: Path) -> Dict[str, Any]:
    meta_file = backup_dir / META_NAME
    _debug("Loading backup metadata", backup_dir=str(backup_dir))
    with meta_file.open("r", encoding="utf-8") as f:
        return json.load(f)


def _get_backup_expiry_from_meta(meta: Dict[str, Any]) -> str:
    """Extract the readable expiry date from already loaded metadata."""
    try:
        entry = _extract_preferred_expiry_entry(meta)
        if isinstance(entry, dict):
            expires_at = entry.get("expires_at_utc")
            if isinstance(expires_at, str) and expires_at:
                return expires_at

        extracted = meta.get("extracted", {})
        jwt_expiry_utc = extracted.get("jwt_expiry_utc", [])
        if jwt_expiry_utc:
            return ", ".join(jwt_expiry_utc)
    except (TypeError, ValueError):
        return "n/a"

    return "n/a"


def _get_backup_expiry_line(backup_dir: Path) -> str:
    try:
        meta = _load_backup_metadata(backup_dir)
    except (OSError, json.JSONDecodeError):
        return "n/a"
    return _get_backup_expiry_from_meta(meta)


def _list_backups() -> List[Dict[str, Any]]:
    backups: List[Dict[str, Any]] = []
    with _debug_phase("list_backups"):
        _debug("Listing available backups")
        for root in _iter_backup_roots():
            _debug("Scanning backup root", root=str(root))
            if not root.exists():
                _debug("Root does not exist, skipping", root=str(root))
                continue
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                backup_file = entry / FILE_NAME
                if not backup_file.is_file():
                    _debug("Entry without auth.json, skipping", entry=str(entry))
                    continue
                meta = {}
                try:
                    meta = _load_backup_metadata(entry)
                except (OSError, json.JSONDecodeError):
                    _debug("Invalid metadata JSON", entry=str(entry))
                    meta = {}

                analyzed_at = meta.get("analyzed_at", "")
                analyzed_at_dt = _parse_backup_creation_from_meta(meta, entry)
                analyzed_sort_key = (
                    analyzed_at_dt.timestamp()
                    if isinstance(analyzed_at_dt, datetime)
                    else 0.0
                )
                expiry = _get_backup_expiry_from_meta(meta)
                backups.append(
                    {
                        "dir": entry,
                        "root": str(root),
                        "name": entry.name,
                        "analyzed_at": analyzed_at,
                        "analyzed_at_sort_key": analyzed_sort_key,
                        "expiry": expiry,
                    }
                )

    def _analyzed_key(item: Dict[str, Any]) -> float:
        try:
            return float(item["analyzed_at_sort_key"])
        except (TypeError, ValueError, KeyError):
            return 0.0

    backups.sort(key=_analyzed_key, reverse=True)
    _debug("Backups sorted", count=len(backups))
    return backups


def _restore_backup_entry(backup_dir: Path, destination: Path) -> Path:
    _debug("Restoring from backup", backup_dir=str(backup_dir), destination=str(destination))
    source_file = backup_dir / FILE_NAME
    if not source_file.is_file():
        raise FileNotFoundError(f"Backup file missing: {source_file}")

    _verify_codex_auth_for_file(source_file, "restore")

    if not destination.parent.exists():
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _debug("Destination parent created", parent=str(destination.parent))

    os.umask(0o077)
    expected_hash = _sha256_file(source_file)
    temp_path: Optional[Path] = None

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.restore-",
            suffix=".tmp",
            dir=destination.parent,
        )
        os.close(fd)
        temp_path = Path(temp_name)
        _debug(
            "Temporary restore file created",
            temp_file=str(temp_path),
            source=str(source_file),
            destination=str(destination),
        )

        shutil.copy2(source_file, temp_path)
        restored_hash = _sha256_file(temp_path)
        _debug(
            "Temporary restore file hash",
            temp_file=str(temp_path),
            expected=expected_hash,
            actual=restored_hash,
        )
        if restored_hash != expected_hash:
            raise RuntimeError(
                f"Restore integrity check failed: hash {restored_hash} != {expected_hash}"
            )

        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
        temp_path = None
        os.chmod(destination, 0o600)
        _debug("Atomic restore completed", source=str(source_file), destination=str(destination))
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
                _debug("Temporary restore file removed", temp_file=str(temp_path))
            except FileNotFoundError:
                pass

    return destination


def _copy_file_with_hash_verification(
    source_file: Path,
    destination_file: Path,
    expected_hash: str,
) -> None:
    _debug("Copying file with hash verification", source=str(source_file), destination=str(destination_file))
    shutil.copy2(source_file, destination_file)
    destination_hash = _sha256_file(destination_file)
    _debug("Destination file hash", destination=str(destination_file), expected=expected_hash, actual=destination_hash)
    if destination_hash != expected_hash:
        raise RuntimeError(
            f"Integrity check failed for {destination_file.name}: "
            f"hash {destination_hash} != {expected_hash}"
        )
    os.chmod(destination_file, 0o600)
    _debug("Hash verification OK", destination=str(destination_file))


def choose_backup_and_restore() -> Optional[Path]:
    with _debug_phase("choose_backup_and_restore"):
        _debug("Restore selection started")
        backups = _list_backups()
        if not backups:
            _debug("No backups available for restore")
            _print_status("WARN", "No backups available for restore.")
            return None

        _print_panel(
            "Restore Backup",
            [
                f"Destination: {_display_path(_get_source_auth_path())}",
                f"Available snapshots: {len(backups)}",
            ],
        )
        print()
        _print_backup_table(backups)
        print()

        _debug("Backups available for restore", options=len(backups))
        while True:
            raw_choice = input(_style("Restore number (q=quit)", "bold") + ": ").strip().lower()
            _debug("User input", raw_choice=raw_choice)
            if raw_choice in ("q", "quit", "esci", "exit"):
                return None
            if not raw_choice.isdigit():
                _print_status("WARN", "Enter a valid number or 'q' to quit.")
                continue

            selection = int(raw_choice) - 1
            if selection < 0 or selection >= len(backups):
                _print_status("WARN", "Number out of range.")
                continue

            chosen = backups[selection]["dir"]
            _debug("Backup selected", chosen=str(chosen), choice=raw_choice)
            restored = _restore_backup_entry(chosen, _get_source_auth_path())
            return restored


def analyze_auth_file(path: Path) -> Dict[str, Any]:
    with _debug_phase("analyze_auth_file", path=str(path), mtime=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()):
        _debug("Auth file analysis started", path=str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        stat = path.stat()

        emails: Set[str] = set()
        expiries: Set[str] = set()
        jwt_expiry_timestamps: Set[int] = set()
        jwt_tokens: List[Dict[str, str]] = []
        account_ids: Set[str] = set()
        account_names: Set[str] = set()
        providers: Set[str] = set()
        top_level_keys: List[str] = []

        _walk_json(
            data,
            emails,
            expiries,
            jwt_expiry_timestamps,
            jwt_tokens,
            account_ids,
            account_names,
            providers,
            top_level_keys,
        )
        _debug(
            "JSON analysis completed",
            email_count=len(emails),
            account_count=len(account_ids),
            provider_count=len(providers),
            jwt_tokens_count=len(jwt_tokens),
            expiry_count=len(jwt_expiry_timestamps),
            top_level_keys_count=len(top_level_keys),
            jwt_token_types_preview=_debug_json_dump_preview(sorted({token["token_type"] for token in jwt_tokens})),
            providers_preview=_debug_json_dump_preview(sorted(providers)),
        )

        jwt_expiry_utc = [
            datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat()
            for expiry in sorted(jwt_expiry_timestamps)
        ]

        jwt_tokens_sorted = sorted(jwt_tokens, key=lambda token: int(token.get("exp", 0)))
        jwt_token_types = sorted({token["token_type"] for token in jwt_tokens_sorted})
        jwt_expired_by_type: Dict[str, bool] = {}
        jwt_expiry_by_type: Dict[str, List[Dict[str, str]]] = {}
        now = int(datetime.now(tz=timezone.utc).timestamp())

        for token in jwt_tokens_sorted:
            token_type = token["token_type"]
            days_until = _days_until_expiry(int(token["exp"]), now)
            token["days_until_expiry"] = days_until
            payload = {
                "path": token["path"],
                "exp": token["exp"],
                "expires_at_utc": token["expires_at_utc"],
                "days_until_expiry": days_until,
            }
            jwt_expiry_by_type.setdefault(token_type, []).append(payload)

        for token_type, entries in jwt_expiry_by_type.items():
            jwt_expired_by_type[token_type] = any(
                int(item["exp"]) <= now for item in entries
            )

        any_jwt_expired = any(int(token["exp"]) <= now for token in jwt_tokens_sorted)

        jwt_summary = {
            "jwt_count": len(jwt_tokens_sorted),
            "most_recent_expiry": None,
            "oldest_expiry": None,
            "earliest_expiring": None,
            "latest_expiring": None,
        }

        if jwt_expiry_timestamps:
            earliest_ts = min(jwt_expiry_timestamps)
            latest_ts = max(jwt_expiry_timestamps)
            # "oldest" and "latest" in absolute creation context here mean older/newer
            jwt_summary["most_recent_expiry"] = str(latest_ts)
            jwt_summary["oldest_expiry"] = str(earliest_ts)
            jwt_summary["earliest_expiring"] = {
                "exp": str(earliest_ts),
                "expires_at_utc": datetime.fromtimestamp(earliest_ts, tz=timezone.utc).isoformat(),
            }
            jwt_summary["latest_expiring"] = {
                "exp": str(latest_ts),
                "expires_at_utc": datetime.fromtimestamp(latest_ts, tz=timezone.utc).isoformat(),
            }

        if jwt_tokens_sorted:
            oldest_token = jwt_tokens_sorted[0]
            newest_token = jwt_tokens_sorted[-1]
            jwt_summary["oldest_expiry_token"] = oldest_token
            jwt_summary["most_recent_expiry_token"] = newest_token
            jwt_summary["first_to_expire"] = oldest_token
            jwt_summary["last_to_expire"] = newest_token

        extracted = {
            "emails": sorted(emails),
            "expiries": sorted(expiries),
            "account_ids": sorted(account_ids),
            "account_names": sorted(account_names),
            "providers": sorted(providers),
            "jwt_summary": jwt_summary,
            "jwt_expiry_unix": sorted(jwt_expiry_timestamps),
            "jwt_expiry_utc": sorted(jwt_expiry_utc),
            "jwt_tokens": jwt_tokens_sorted,
            "jwt_token_types": jwt_token_types,
            "jwt_expiry_by_type": jwt_expiry_by_type,
            "jwt_expired": any_jwt_expired,
            "jwt_expired_by_type": jwt_expired_by_type,
            "backup_expiry": None,
            "top_level_keys": sorted(set(top_level_keys)),
        }

        preferred_expiry_entry = _extract_preferred_expiry_entry({"extracted": extracted})
        if isinstance(preferred_expiry_entry, dict) and preferred_expiry_entry.get("expires_at_utc"):
            extracted["backup_expiry"] = _build_backup_expiry_metadata(preferred_expiry_entry)
            _debug(
                "Backup expiry selected",
                source=preferred_expiry_entry.get("source"),
                token_type=preferred_expiry_entry.get("token_type"),
                path=preferred_expiry_entry.get("path"),
                exp=preferred_expiry_entry.get("exp"),
                expires_at_utc=preferred_expiry_entry.get("expires_at_utc"),
            )

        _debug(
            "Metadata extracted",
            emails_preview=_debug_json_dump_preview(extracted["emails"]),
            accounts_preview=_debug_json_dump_preview(extracted["account_ids"]),
            providers_preview=_debug_json_dump_preview(extracted["providers"]),
            tokens_preview=_debug_json_dump_preview(extracted["jwt_token_types"]),
        )

        sha256_value = _sha256_file(path)
        _debug("Hash computed during analysis", sha256=sha256_value)

        return {
            "source": str(path),
            "source_size": stat.st_size,
            "source_mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "sha256": sha256_value,
            "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
            "extracted": extracted,
        }


def make_backup() -> Path:
    with _debug_phase("make_backup"):
        source_auth = _get_source_auth_path()
        if not source_auth.is_file():
            raise FileNotFoundError(f"Source file not found: {source_auth}")
        _debug("Backup source is valid", source=str(source_auth))

        os.umask(0o077)
        BACKUP_BASE.mkdir(parents=True, mode=0o700, exist_ok=True)
        _debug("Prepared backup base", backup_base=str(BACKUP_BASE))

        codex_verification = _verify_codex_auth_for_file(source_auth, "backup")
        analyzed = analyze_auth_file(source_auth)
        _sync_auth_snapshot(analyzed, codex_verification)
        analyzed_source_hash = analyzed.get("sha256")
        if not isinstance(analyzed_source_hash, str):
            raise RuntimeError("Unable to compute the source file hash.")

        # Protect against race conditions: if the file changes between analysis
        # and copy, the final content verification will detect it.
        live_hash = _sha256_file(source_auth)
        if live_hash != analyzed_source_hash:
            _debug("Hash mismatch between analysis and live file", analyzed=analyzed_source_hash, live=live_hash)
            raise RuntimeError(
                "The source file changed during analysis: repeat the backup."
            )
        _debug("Race-condition check passed", analyzed_hash=analyzed_source_hash)

        with _debug_phase("build_signature"):
            current_signature = _build_signature(analyzed)
            _debug("Current signature computed", signature_preview=current_signature[:140])

        with _debug_phase("dedupe_check"):
            existing_signatures = _read_signatures_from_known_roots()
            _debug("Signatures collected", total=len(existing_signatures))
            if current_signature in existing_signatures:
                existing_backup_dir = existing_signatures[current_signature]
                _debug(
                    "Duplicate backup detected",
                    signature_preview=current_signature[:120],
                    existing_backup_dir=str(existing_backup_dir),
                )
                raise DuplicateBackupError(existing_backup_dir)

        backup_created_at = datetime.now(tz=timezone.utc)
        target_dir = _resolve_unique_backup_dir_name(BACKUP_BASE, analyzed, backup_created_at)
        tmp_prefix = _safe_filename_component(f".{target_dir.name}_tmp_", "tmp")
        temp_dir = Path(tempfile.mkdtemp(prefix=tmp_prefix, dir=BACKUP_BASE))
        _debug("Temporary directory created", temp_dir=str(temp_dir), target_dir=str(target_dir))
        target_file = temp_dir / FILE_NAME
        meta_file = temp_dir / META_NAME

        committed = False
        try:
            temp_dir.chmod(0o700)
            _copy_file_with_hash_verification(source_auth, target_file, live_hash)
            with meta_file.open("w", encoding="utf-8") as f:
                json.dump(analyzed, f, indent=2, sort_keys=True)
            os.chmod(meta_file, 0o600)
            _debug("Metadata write completed", meta_file=str(meta_file), meta_size=meta_file.stat().st_size)
            os.replace(temp_dir, target_dir)
            committed = True
            _debug("Backup written successfully", target_dir=str(target_dir), temp_dir=str(temp_dir))
            return target_dir
        finally:
            if not committed and temp_dir.exists():
                _debug("Cleaning up incomplete temporary backup", temp_dir=str(temp_dir))
                shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    _configure_debug_from_args()
    _debug(
        "Main started",
        executable=sys.executable,
        args=sys.argv,
        tty=sys.stdin.isatty(),
        debug_enabled=_debug_enabled(),
    )

    for root in _iter_backup_roots():
        _debug("Backup name migration started", root=str(root))
        try:
            _migrate_backup_names(root)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _debug("Backup name migration failed", root=str(root), error=str(exc))

    try:
        if not sys.stdin.isatty():
            _debug("Non-interactive path -> automatic backup")
            target = make_backup()
            _print_status("OK", "Backup created", _display_path(target))
            return 0

        _debug("Interactive menu started")
        _print_app_header()
        _print_main_menu()

        while True:
            choice = _read_menu_choice(_style("Select an option", "bold") + ": ")
            _debug("User choice", choice=choice)
            if choice in ("1", "b", "backup"):
                _debug("Branch BACKUP")
                target = make_backup()
                _print_status("OK", "Backup created", _display_path(target))
                return 0
            if choice in ("2", "r", "restore"):
                _debug("Branch RESTORE")
                restored = choose_backup_and_restore()
                if restored is not None:
                    _print_status("OK", "Restored", _display_path(restored))
                else:
                    _print_status("INFO", "Restore canceled.")
                return 0
            if choice in ("q", "quit", "esci", "exit"):
                _debug("User exit")
                return 0
            _print_status("WARN", "Invalid choice", "enter '1' for BACKUP, '2' for RESTORE, or 'q' to quit")
    except KeyboardInterrupt:
        print()
        _debug("User interrupted with Ctrl+C")
        return 0
    except DuplicateBackupError as exc:
        _debug("Duplicate backup caught", error=str(exc))
        _print_duplicate_backup(exc)
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, EOFError, shutil.Error) as exc:
        _debug("Main error", error=str(exc))
        _print_status("ERR", "Error during operation", str(exc), stream=sys.stderr)
        return 1


if __name__ == "__main__": 
    raise SystemExit(main())
