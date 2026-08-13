#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSREPORT_PID=""

cleanup() {
  if [[ -n "${JSREPORT_PID}" ]] && kill -0 "${JSREPORT_PID}" 2>/dev/null; then
    kill "${JSREPORT_PID}"
    wait "${JSREPORT_PID}" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

(
  cd "${ROOT_DIR}/jsreport"
  node server.js
) &
JSREPORT_PID="$!"

exec env PYTHONPATH="${ROOT_DIR}/src:${ROOT_DIR}/niimprint" "${ROOT_DIR}/venv/bin/python" -m label_print_server \
  --config "${ROOT_DIR}/src/label_print_server/config.example.json" \
  --database "${ROOT_DIR}/label_print_server.sqlite3" \
  --host 0.0.0.0 \
  "$@"
