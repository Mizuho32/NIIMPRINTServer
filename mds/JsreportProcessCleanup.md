# jsreport / Chrome プロセスが終了後も残る問題の調査・修正メモ

## 症状

`bin/start_label_print_server.sh` でサーバーを起動し、その後スクリプトを止める
（Ctrl-C / `kill` / systemd stop など）と、jsreport (`node server.js`) や
puppeteer が起動した Chrome 関連プロセス（zygote / gpu-process / renderer /
utility / `chrome_crashpad_handler` など）が終了せずに残ってしまう。

## 原因

`bin/start_label_print_server.sh` は最後にこうなっていた。

```bash
trap cleanup EXIT INT TERM
( cd jsreport && node server.js ) &
JSREPORT_PID="$!"
exec env PYTHONPATH=... python -m label_print_server ...
```

`exec` は bash プロセス自体を python に置き換える。つまり **trap を持っていた
bash が消滅**し、以後スクリプトの PID は python そのものになる。この状態で
スクリプトを止めると、SIGINT/SIGTERM は python に直接届いて python は終了
するが、trap を実行する bash がもう存在しないため `cleanup()`（= jsreport
への kill）は一切実行されない。結果、jsreport とその配下の Chrome プロセス
ツリーが孤児化してそのまま残る。

最小再現で確認済み：`exec` ありのスクリプトに `kill -TERM` を送ると trap 内の
`cleanup` は呼ばれず、バックグラウンドの子プロセスだけが init に
re-parent されて生き残る。

逆に、jsreport (node) プロセスに直接 SIGTERM を送った場合は、jsreport 自身の
`SIGTERM` ハンドラ（`server.js` 内の `shutdown()` → `jsreport.close()`）が
正しく動作し、puppeteer の `browser.close()` 経由で Chrome 側のプロセス
ツリーも含めてきれいに全部終了することも確認済み。つまり問題は jsreport 側
ではなく、**このシェルスクリプトが `exec` で自分の trap を無効化していた
こと**が根本原因。niimprint 側・jsreport 側のコードには手を入れていない。

## 修正

`exec` をやめて、python もバックグラウンドジョブにして `wait` する形に変更。
`wait` 中は bash の trap がすぐに割り込んで実行されるので、シグナルを受けたら
確実に両方の子プロセスを kill できる。

```bash
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
```

差分は `bin/start_label_print_server.sh` の1ファイルのみ。

## 検証

実際に jsreport を起動して `POST /api/report`（`recipe: chrome-pdf`）で
レンダリングを1回走らせ、Chrome 関連プロセスを約15個発生させたあと、修正後の
スクリプトに SIGTERM を送って動作確認した。

- 修正前（`exec` あり）: `kill -TERM <script pid>` を送っても `cleanup` は
  実行されず、jsreport (node) とその子プロセスがそのまま残った。
- 修正後: SIGTERM 送信後、ログに
  `Closing jsreport instance` → `jsreport instance has been closed` →
  uvicorn の `Application shutdown complete` の順で正しく出力され、
  jsreport・python・Chrome プロセスツリー（zygote / gpu-process / renderer /
  utility / broker / crashpad_handler 含む約15プロセス）が一つも残らない
  ことを確認した。

コミット: `3db6599 Fix orphaned jsreport/Chrome processes after stopping the server`
