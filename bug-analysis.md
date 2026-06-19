# Bug Analysis - Codex backup tool (2026-06-14)

Context: codebase in `/opt/codex-backup` (`backup_auth.py`, `install.py`, `run-backup.sh`).

## Executive summary
- Overall status: no demonstrable critical crash in the main execution paths.
- Some robustness areas can be improved, such as cron handling in unusual environments, stricter idempotency, and tighter cleanup.

## Analysis 1 - Compilation and Syntax (parallel)

1. `backup_auth.py` compiles correctly with Python.
2. `install.py` compiles correctly with Python.
3. `run-backup.sh` passes `bash -n`.

Note: including `run-backup.sh` in the `py_compile` command produces a false positive (`SyntaxError`) because it is not Python.

### Priority
- P4 (informational): no bug to fix.

## Analysis 2 - Critical Error Handling in the Backup/Restore Flow

### Finding A (P2)
- **File:** `backup_auth.py` (e.g. `make_backup`, `_debug_phase`).
- **Risk:** using `raise SystemExit` to signal a duplicate backup (`lines around 1055`) interrupts the flow as a "control error", even though it is caught and reported as a successful exit (`main` lines 1131-1134).
- **Impact:** acceptable behavior, but the exception semantics are not very readable/reusable for scripts.
- **Improvement:** define a custom `BackupDuplicatedError` exception and handle it explicitly.

### Finding B (P2)
- **File:** `backup_auth.py` (`except Exception` in `choose`/`make_backup`/`main`).
- **Risk:** broad catches mask error sources; debug helps, but final user diagnostics remain uniform.
- **Improvement:** replace with targeted exceptions where possible (`OSError`, `ValueError`, `json.JSONDecodeError`).

## Analysis 3 - I/O, Filesystem, and Backup Naming

### Finding C (P1)
- **File:** `backup_auth.py`, function `_migrate_backup_names` + `_resolve_unique_backup_dir_name`.
- **Risk:** if legacy directories are unreadable or have corrupt metadata, they are skipped (`debug skip`) and not reported to `stderr`.
- **Impact:** possible visibility loss: "unknown" backups remain without restore visibility.
- **Improvement:** report a warning summary at the end of migration with counts/items not migrated.

### Finding D (P3)
- **File:** `backup_auth.py`, restore sorting in `_list_backups`.
- **Risk:** sorting by directory mtime may not reflect the `analyzed_at` saved in metadata.
- **Impact:** weak UX: the displayed order could be confusing.
- **Improvement:** sort by `analyzed_at` from metadata, or provide an option for it.

## Analysis 4 - Installation, Symlink, and Crontab

### Finding E (P0/P1)
- **File:** `install.py`, `main` + `_write_crontab` + `_filter_existing_entries`.
- **Risk:** in read-only environments (for example `/var/spool/cron`), installation fails while inserting the crontab with `mkstemp: Read-only file system`.
- **Impact:** incomplete installation and crontab not registered.
- **Current status:** behavior is reproducible here and visible in the logs.
- **Improvement:** clearly distinguish "symlink ok / cron failed" and allow partial installation; suggest `--skip-cron` or an alternate `crontab` command if available.

## Analysis 5 - UX/CLI Consistency and Diagnostics

### Finding F (P2)
- **File:** `install.py`, `_is_managed_backup_entry`.
- **Risk:** cron-entry cleanup based on the combined pattern (`run-backup.sh` + `codex-backup`) may leave managed entries in a different format, such as a direct Python call to `backup_auth`.
- **Impact:** risk of duplicated schedules over time.
- **Improvement:** consider a stronger comment/tag ID (stable hash/tool path) and possibly remove only entries with a known prefix/tag.

## Verification Performed
- `PYTHONPYCACHEPREFIX=/tmp python3 -m py_compile backup_auth.py install.py`
- `bash -n run-backup.sh`
- `rg` for risky exceptions/branches in `*.py`
- Non-interactive tool execution: expected behavior (backup not created if duplicated).
- Install execution: symlink resolved and cron entry creation attempted.

## Recommended Immediate Action
1. Implement the P1 improvement on `install.py` immediately to make installation more transparent even in read-only environments.
2. Improve messaging/exception paths (`SystemExit`/targeted exceptions) in the backup flow.
3. Evaluate restore sorting by `analyzed_at` for chronological use cases.
