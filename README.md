<div align="center">

# codex-backup

### Back up your Codex CLI login automatically, every hour.

**Saves, verifies, and rotates `~/.codex/auth.json` so you never lose access.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.8+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-FCC624.svg?logo=linux&logoColor=black)](https://www.kernel.org/)
[![Codex CLI](https://img.shields.io/badge/for-Codex%20CLI-412991.svg?logo=openai&logoColor=white)](https://github.com/openai/codex)

</div>

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
