#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSREPORT_PID=""
SERVER_PID=""

# Grace period to let a child that may already be shutting down on its own
# finish before we intervene. When this script's whole process group gets a
# signal (a real Ctrl-C, or systemd's default KillMode=control-group), node
# and python receive it directly too -- jsreport installs its own
# SIGINT/SIGTERM handler that calls jsreport.close() (which tears down the
# chrome-pdf extension's puppeteer browser), and uvicorn shuts itself down
# the same way. Sending either of them a *second*, redundant signal while
# that async shutdown is still in flight races with it: jsreport still logs
# "jsreport instance has been closed", but the underlying browser.close()
# can lose the race and leave the whole Chrome process tree (zygote,
# gpu-process, renderer, utility, chrome_crashpad_handler, ...) orphaned.
# Waiting first avoids poking anything that's already on its way out; in
# practice jsreport's own shutdown finishes in well under a second.
GRACE_PERIOD_SECONDS=3

wait_or_kill() {
  local pid="$1" waited_ms=0
  [[ -n "${pid}" ]] || return 0
  while kill -0 "${pid}" 2>/dev/null && (( waited_ms < GRACE_PERIOD_SECONDS * 1000 )); do
    sleep 0.1
    waited_ms=$((waited_ms + 100))
  done
  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
  fi
  wait "${pid}" 2>/dev/null || true
}

cleanup() {
  # Run both in parallel: if neither child was signaled directly (only this
  # script's own PID was), waiting on them one after another would mean up
  # to 2x GRACE_PERIOD_SECONDS of pure dead time before either gets force-
  # killed.
  wait_or_kill "${SERVER_PID}" &
  wait_or_kill "${JSREPORT_PID}" &
  wait
}

trap cleanup EXIT
# Route INT/TERM through `exit` rather than calling cleanup() directly for
# both of them *and* EXIT: bash runs a signal's trap immediately (even
# interrupting the `wait` below), but afterwards the script keeps going and
# then hits the EXIT trap too, so binding cleanup() straight to INT/TERM as
# well as EXIT ran it twice per signal -- itself a second source of the same
# redundant-signal race described above. `exit` here just makes EXIT the one
# and only place cleanup ever runs, with the conventional 128+signal status.
trap 'exit 130' INT
trap 'exit 143' TERM

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

# Not `exec`-ing into the python process here: exec replaces bash's own
# process image, which would discard the traps set up above (there would be
# no shell left to run them). `wait` keeps bash alive and is interrupted by
# a trapped signal immediately, so cleanup() reliably runs on shutdown.
wait "${SERVER_PID}"
