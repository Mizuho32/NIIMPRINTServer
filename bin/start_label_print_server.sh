#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSREPORT_PID=""
SERVER_PID=""

cleanup() {
  local pid
  for pid in "${SERVER_PID}" "${JSREPORT_PID}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
  for pid in "${SERVER_PID}" "${JSREPORT_PID}"; do
    if [[ -n "${pid}" ]]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done
}

trap cleanup EXIT INT TERM

(
  cd "${ROOT_DIR}/jsreport"
  node server.js
) &
JSREPORT_PID="$!"

env PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}/niimprint" "${ROOT_DIR}/venv/bin/python" -m label_print_server \
  --config "${ROOT_DIR}/src/label_print_server/config.example.json" \
  --database "${ROOT_DIR}/label_print_server.sqlite3" \
  --host 0.0.0.0 \
  "$@" &
SERVER_PID="$!"

# Deliberately not `exec`-ing into the python process here: exec replaces
# bash's own process image, which would discard the trap set up above (there
# would be no shell left to run it). That used to be exactly what happened --
# stopping this script (Ctrl-C, `kill`, systemd stop, ...) sent the signal
# straight to python, python exited, but jsreport (and its whole Chrome
# process tree: zygote/gpu-process/renderer/utility/chrome_crashpad_handler)
# was never told to stop and was left running as an orphan. Keeping bash
# alive and blocking in `wait` instead means a trapped signal interrupts the
# wait immediately and cleanup() reliably stops both children.
wait "${SERVER_PID}"
