#!/usr/bin/env python3
from __future__ import annotations

import os
import argparse
import shlex
import subprocess
import sys
from pathlib import Path
import re


DEBUG = os.environ.get("CODEX_BACKUP_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

ANSI_STYLES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}


def _ansi_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    term = os.environ.get("TERM", "")
    return (
        hasattr(stream, "isatty")
        and stream.isatty()
        and "NO_COLOR" not in os.environ
        and term.lower() not in ("", "dumb")
    )


def _style(text: str, *styles: str, stream=None) -> str:
    if not styles or not _ansi_enabled(stream):
        return text
    prefix = "".join(ANSI_STYLES[name] for name in styles if name in ANSI_STYLES)
    if not prefix:
        return text
    return f"{prefix}{text}{ANSI_STYLES['reset']}"


def _display_path(path: Path | str) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home):]
    return value


def _print_panel(title: str, lines: list[str]) -> None:
    width = max([len(title), *(len(line) for line in lines), 42])
    border = _style("+" + "-" * (width + 2) + "+", "cyan")
    print(border)
    print(f"| {_style(title, 'bold').ljust(width)} |")
    if lines:
        print(_style("| " + "-" * width + " |", "cyan"))
        for line in lines:
            print(f"| {line.ljust(width)} |")
    print(border)


def _status_color(label: str) -> str:
    return {
        "OK": "green",
        "INFO": "cyan",
        "WARN": "yellow",
        "ERR": "red",
    }.get(label, "cyan")


def _print_status(label: str, message: str, detail: str = "", stream=None) -> None:
    stream = stream or sys.stdout
    badge = _style(f"[{label}]", _status_color(label), "bold", stream=stream)
    suffix = f": {detail}" if detail else ""
    print(f"{badge} {message}{suffix}", file=stream)


def _configure_debug(argv: list[str]) -> None:
    global DEBUG
    parser = argparse.ArgumentParser(
        add_help=True,
        description="Install codex-backup in cron and create the symlink.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug output during installation.",
    )
    args, _ = parser.parse_known_args(argv)
    DEBUG = DEBUG or args.verbose


def _debug(message: str, **kwargs: object) -> None:
    if not DEBUG:
        return
    from datetime import datetime, timezone
    details = ", ".join(f"{key}={value!r}" for key, value in kwargs.items())
    prefix = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if details:
        print(f"[DEBUG {prefix}] {message} | {details}", file=sys.stderr)
    else:
        print(f"[DEBUG {prefix}] {message}", file=sys.stderr)


def _load_crontab() -> list[str]:
    _debug("Checking crontab prerequisites", tool="subprocess.run", command="crontab -l")
    _debug("Loading current crontab")
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("The 'crontab' command is not available on this system.") from None

    stdout = (result.stdout or "").strip()
    _debug("Crontab result", returncode=result.returncode, stdout_preview=stdout[:120], stderr_preview=(result.stderr or "")[:120])
    if result.returncode == 0:
        return stdout.splitlines()

    # No crontab for the current user -> return an empty list.
    if result.returncode == 1 and not stdout and "no crontab" in (result.stderr or "").lower():
        return []

    raise RuntimeError(f"Error reading crontab: {result.stderr.strip() or result.stdout.strip()}")


def _write_crontab(lines: list[str]) -> None:
    _debug("Writing crontab", entries=len(lines))
    data = "\n".join(lines)
    if data:
        data += "\n"

    result = subprocess.run(
        ["crontab", "-"],
            input=data,
            text=True,
            capture_output=True,
            check=False,
        )
    _debug(
        "Crontab write result",
        returncode=result.returncode,
        stderr_preview=(result.stderr or "")[:120],
    )
    if result.returncode != 0:
        raise RuntimeError(f"Error writing crontab: {result.stderr.strip() or result.stdout.strip()}")


def _build_cron_entry(script_dir: Path) -> tuple[str, str]:
    wrapper = script_dir / "run-backup.sh"
    log_file = script_dir / "codex-backup.log"
    tag = "# codex-backup hourly auth backup"
    command = (
        f"0 * * * * cd {shlex.quote(str(script_dir))} && "
        f"{shlex.quote(str(wrapper))} >> {shlex.quote(str(log_file))} 2>&1"
    )
    _debug("Cron command built", tag=tag, wrapper=str(wrapper), command=command)
    return tag, command


def _candidate_symlink_locations(preferred_name: str = "codex-backup") -> list[Path]:
    _debug("Computing symlink locations", preferred_name=preferred_name)
    locations: list[Path] = []
    preferred = Path("/usr/local/bin") / preferred_name
    fallback_home = Path.home() / ".local" / "bin" / preferred_name
    locations.append(preferred)
    if fallback_home.parent != preferred.parent:
        locations.append(fallback_home)
    return locations


def _write_executable_symlink(target: Path, link_path: Path) -> Path:
    _debug("Checking/creating symlink", target=str(target), link_path=str(link_path))
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink():
            current = link_path.resolve()
            if current == target.resolve():
                _debug("Symlink already correct", link_path=str(link_path), current=str(current))
                return link_path
            link_path.unlink()
        else:
            raise RuntimeError(
                f"ERROR: a file/non-symlink already exists at {link_path}. Remove it and run the installation again."
            )

    try:
        link_path.symlink_to(target)
    except OSError as exc:
        raise RuntimeError(f"ERROR: unable to create symlink {link_path}: {exc}") from exc

    _debug("Symlink written", link=str(link_path), target=str(target))
    return link_path


def _ensure_symlink(target: Path, preferred_name: str = "codex-backup") -> Path:
    _debug("Starting symlink setup", target=str(target), preferred_name=preferred_name)
    if not target.is_file():
        raise RuntimeError(f"ERROR: invalid symlink target: {target}")

    for link_path in _candidate_symlink_locations(preferred_name):
        parent = link_path.parent
        try:
            _debug("Checking parent directory", parent=str(parent), exists=parent.exists())
            if not parent.exists():
                if str(parent).startswith(str(Path.home())):
                    _debug("Creating symlink parent directory", parent=str(parent))
                    parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                else:
                    continue

            if not os.access(parent, os.X_OK | os.W_OK):
                _debug("Parent is not writable, skipping", parent=str(parent))
                continue

            return _write_executable_symlink(target, link_path)
        except RuntimeError:
            raise
        except OSError:
            continue

    raise RuntimeError(
        "ERROR: no writable symlink location found (tried /usr/local/bin and ~/.local/bin)."
    )


def _is_managed_backup_entry(line: str, wrapper: str) -> bool:
    # Match cron lines that run a `run-backup.sh` command for this backup stack,
    # also when the full path is omitted or slightly different.
    _debug("Checking managed cron entry", line=line.strip())
    return bool(re.search(r"(^|\s)/?([\w./-]*/)?run-backup\.sh(\s|$)", line)) and "codex-backup" in line.lower()


def _filter_existing_entries(
    lines: list[str],
    tag: str,
    command: str,
    wrapper: str,
) -> tuple[list[str], bool, bool]:
    """Remove all entries managed by this tool from the crontab.

    Return ``(cleaned, removed_other, had_canonical)``:
      - ``cleaned``: lines to keep, with no managed entry and without the blank
        line that immediately preceded a removed block (prevents blank-line
        accumulation across reinstalls);
      - ``removed_other``: ``True`` if at least one managed entry was removed
        other than the single canonical block (legacy entries, commands without
        tags, mismatched tags, duplicated canonical blocks);
      - ``had_canonical``: ``True`` if the crontab already contained the
        canonical block, meaning ``tag`` followed exactly by the desired
        ``command``.

    When ``had_canonical`` is true and ``removed_other`` is false, the crontab
    is already configured correctly and the caller can avoid rewriting it.
    """
    cleaned: list[str] = []
    removed_other = False
    had_canonical = False
    wrapper_path = wrapper.strip()
    _debug("Filtering crontab entries", wrapper=wrapper_path, command=command, tag=tag, total_lines=len(lines))

    def _drop_preceding_blank() -> None:
        # Remove the blank line immediately before the managed block being
        # removed, so blank lines do not accumulate over time.
        if cleaned and cleaned[-1].strip() == "":
            cleaned.pop()

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]

        if line == tag:
            next_line = lines[index + 1] if index + 1 < total else None
            _drop_preceding_blank()
            if next_line is not None and next_line.strip() == command:
                if had_canonical:
                    removed_other = True
                    _debug("Removed duplicated canonical block", line=line)
                else:
                    had_canonical = True
                    _debug("Detected existing canonical block", line=line)
                index += 2
            else:
                # Tag without the expected command immediately after it:
                # corrupt/legacy managed block, remove it (tag + next line).
                removed_other = True
                _debug("Removed non-canonical managed tag", line=line)
                index += 2 if next_line is not None else 1
            continue

        if line.strip() == command:
            removed_other = True
            _drop_preceding_blank()
            _debug("Removed cron command without tag", command=line)
            index += 1
            continue

        if _is_managed_backup_entry(line, wrapper_path):
            removed_other = True
            _drop_preceding_blank()
            _debug("Removed managed cron entry", line=line)
            index += 1
            continue

        _debug("Kept cron line", line=line)
        cleaned.append(line)
        index += 1

    return cleaned, removed_other, had_canonical


def main(argv: list[str] | None = None) -> int:
    _configure_debug(sys.argv[1:] if argv is None else argv)
    _debug("Install main started", script=Path(__file__).resolve())
    tool_dir = Path(__file__).resolve().parent
    _print_panel(
        "Codex Backup Installer",
        [
            f"Directory: {_display_path(tool_dir)}",
            "Schedule: hourly cron backup",
        ],
    )

    auth_tool = tool_dir / "backup_auth.py"
    wrapper = tool_dir / "run-backup.sh"
    _debug("Checking tool files", auth_tool=str(auth_tool), wrapper=str(wrapper))

    if not auth_tool.is_file():
        _debug("auth_tool missing", auth_tool=str(auth_tool))
        _print_status("ERR", "backup_auth.py not found", _display_path(tool_dir), stream=sys.stderr)
        return 1

    if not wrapper.is_file():
        _debug("wrapper missing", wrapper=str(wrapper))
        _print_status("ERR", "run-backup.sh not found", _display_path(tool_dir), stream=sys.stderr)
        return 1

    if not os.access(wrapper, os.X_OK):
        _debug("Wrapper is not executable", wrapper=str(wrapper))
        _print_status("ERR", "run-backup.sh is not executable", _display_path(wrapper), stream=sys.stderr)
        return 1

    try:
        symlink_path = _ensure_symlink(auth_tool, "codex-backup")
        _debug("Symlink installation completed", symlink=str(symlink_path))
        _print_status(
            "OK",
            "Symlink ready",
            f"{_display_path(symlink_path)} -> {_display_path(auth_tool)}",
        )
    except RuntimeError as exc:
        _debug("Symlink setup error", error=str(exc))
        _print_status("ERR", str(exc), stream=sys.stderr)
        return 1

    try:
        tag, command = _build_cron_entry(tool_dir)
        _debug("Cron command built", tag=tag, command=command)
        current_lines = _load_crontab()
        _debug("Current crontab lines", count=len(current_lines))
        cleaned_lines, removed_other, had_canonical = _filter_existing_entries(
            current_lines, tag, command, str(wrapper)
        )

        if had_canonical and not removed_other:
            _debug("Cron already configured correctly, no changes")
            _print_status("INFO", "Cron is already configured for this tool.")
            return 0

        # Remove any trailing blank lines before appending the managed block:
        # one separator, no accumulation across reinstalls.
        while cleaned_lines and cleaned_lines[-1].strip() == "":
            cleaned_lines.pop()
        if cleaned_lines:
            cleaned_lines.append("")
        cleaned_lines.append(tag)
        cleaned_lines.append(command)
        _debug("Final crontab ready", lines=len(cleaned_lines))

        _write_crontab(cleaned_lines)

        if removed_other or had_canonical:
            _debug("Cron updated: existing entries replaced")
            _print_status("OK", "Cron updated", "previous entry replaced")
        else:
            _debug("Cron added without replacements")
            _print_status("OK", "Cron added", "hourly execution configured")

        print()
        _print_panel("Inserted Cron Entry", [command])
        _debug("Installation completed successfully")
    except RuntimeError as exc:
        _debug("Error during crontab/symlink installation", error=str(exc))
        _print_status("ERR", "Error", str(exc), stream=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
