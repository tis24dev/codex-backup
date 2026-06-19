# codex-backup

Hourly backup of the Codex CLI credentials (`~/.codex/auth.json`): saves, verifies, and rotates them automatically via a cron job.

## Install

Quick install (single command):

```bash
git clone https://github.com/tis24dev/codex-backup.git && cd codex-backup && python3 install.py
```

Or step by step:

```bash
git clone https://github.com/tis24dev/codex-backup.git
cd codex-backup
python3 install.py
```

The installer:

- creates a `codex-backup` symlink in `/usr/local/bin` (fallback `~/.local/bin`) pointing to `backup_auth.py`, so you can run `codex-backup` from anywhere;
- registers an hourly cron job that runs the backup automatically.
