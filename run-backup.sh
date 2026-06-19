#!/usr/bin/env bash
set -euo pipefail

is_true() {
  case "${1,,}" in
    0|false|no|off|disable|disabled) return 1 ;;
    *) return 0 ;;
  esac
}

log() {
  if ! is_true "${CODEX_BACKUP_DEBUG:-1}"; then
    return
  fi
  # shellcheck disable=SC2155
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s [run-backup] %s\n' "${ts}" "$*" >&2
}

log "Starting run-backup.sh"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
SCRIPT="${SCRIPT_DIR}/backup_auth.py"

log "Directory tool: ${SCRIPT_DIR}"
log "Python script: ${SCRIPT}"
if [ -w "${SCRIPT_DIR}" ]; then
  LOG_FILE="${SCRIPT_DIR}/codex-backup.log"
else
  LOG_DIR="/tmp/codex-backup"
  mkdir -p "${LOG_DIR}" 2>/dev/null || LOG_DIR="/tmp"
  LOG_FILE="${LOG_DIR}/codex-backup.log"
fi
log "Log file: ${LOG_FILE}"

if [[ ! -f "$SCRIPT" ]]; then
  log "Error: missing backup_auth.py file"
  echo "backup_auth.py not found in ${SCRIPT_DIR}" >&2
  exit 1
fi

umask 077
log "Creating data_backup directory: ${SCRIPT_DIR}/data_backup"
mkdir -p "${SCRIPT_DIR}/data_backup"

if "${PYTHON_BIN}" "${SCRIPT}" >>"${LOG_FILE}" 2>&1; then
  log "Python execution completed successfully"
else
  status=$?
  log "Python execution failed with code ${status}"
  exit "${status}"
fi
