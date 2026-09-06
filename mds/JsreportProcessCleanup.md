# jsreport / Chrome プロセスが終了後も残る問題の調査・修正メモ

## 症状

`bin/start_label_print_server.sh` でサーバーを起動し、その後 Ctrl-C で止めると、
jsreport (`node server.js`) が puppeteer 経由で起動した Chrome 関連プロセス
（zygote / gpu-process / renderer / utility / `chrome_crashpad_handler` など）
が終了せずに残ってしまう。またこのとき、スクリプト自体は exit code 130
（`128 + SIGINT`）で終了する。

exit code 130 自体は Ctrl-C（SIGINT）で終了したことを示す慣習的な値で、
これは問題ない（後述の修正後もこの値のまま扱っている）。問題は Chrome の
プロセスが残ることの方。

## 調査

### 1. 最初に見つけた原因（`exec` が trap を消す）と、それだけでは直らなかったこと

`bin/start_label_print_server.sh` は元々こうなっていた。

```bash
trap cleanup EXIT INT TERM
( cd jsreport && node server.js ) &
JSREPORT_PID="$!"
exec env PYTHONPATH=... python -m label_print_server ...
```

`exec` は bash プロセス自体を python に置き換える。つまり trap を持っていた
bash が消滅し、以後スクリプトの PID は python そのものになる。この状態で
Ctrl-C すると SIGINT は python に直接届いて python は終了するが、trap を
実行する bash がもう存在しないため `cleanup()`（= jsreport への kill）は
一切実行されず、jsreport とその Chrome プロセスツリーが孤児化する。

これを直すため、`exec` をやめて python もバックグラウンドジョブにして
`wait` する形に変更した。ここまでは実機というより最小再現（`sleep` を
使ったダミースクリプト）で確認できた話。

ところが、実際に jsreport を起動してレンダリングまでさせてから Ctrl-C
相当のテスト（後述）をすると、**まだ Chrome プロセスが残るケースがあった**。
つまり `exec` の除去だけでは不十分だった。

### 2. 本当の原因: cleanup() 自身が二重にシグナルを送ってしまうレース

ポイントは、`( cmd ) &` で作ったバックグラウンドジョブは、シェルの
ジョブコントロールが無効な非対話スクリプト内では**スクリプト自身と同じ
プロセスグループ**に入ること。つまり実際にターミナルで Ctrl-C を押すと、
SIGINT は「スクリプト（bash）」「jsreport (node)」「python」の全員に
**同時に直接**届く。jsreport 自身も `server.js` 内で
`process.on('SIGINT', shutdown)` / `process.on('SIGTERM', shutdown)` を
登録していて、`shutdown()` は `await jsreport.close()`
（puppeteer の `browser.close()` を含む）を実行してから `process.exit()`
する、という自前の正常終了処理を持っている。

これを、実際にプロセスグループ全体に `kill -INT -<pgid>` を送って検証
すると（Ctrl-C の代替として正確に再現できる）、**スクリプト側が何も
しなくても jsreport は自分自身の SIGINT ハンドラだけで Chrome ツリーを
含めてきれいに終了する**ことが確認できた。

問題は、その状態でスクリプト側の `cleanup()`（trap 経由で呼ばれる）が
**追加でもう一度** `kill "${JSREPORT_PID}"` を送ってしまうこと。jsreport
はまだ1回目の SIGINT による `jsreport.close()` の途中（非同期、体感
50〜100ms 程度)なので、そこにもう一度同じシグナルが届くと `shutdown()`
がもう一度呼ばれ、`jsreport.close()` が二重に走る。ログ上は
`Closing jsreport instance` が **2回** 出ることでこれが確認できる
（1回目 = jsreport 自身の SIGINT ハンドラ、2回目 = cleanup() の
明示的な kill）。この二重呼び出しのレースにより、ログには
`jsreport instance has been closed` と出て正常終了したように見えるのに、
puppeteer の `browser.close()` が実際には Chrome プロセスツリーを
待ちきれず、孤児として残ることがある。

さらに、`trap cleanup EXIT INT TERM` のように同じ関数を3つ全部に
バインドしていたのも良くなかった。bash は INT/TERM のトラップを
「`wait` を割り込んで即座に」実行するが、その後スクリプトの実行は
続行され（`set -e` の影響で `wait` の非ゼロ終了ステータスによりすぐ
スクリプトが終わるにしても）、最終的に EXIT トラップも発火する。つまり
`cleanup()` が **シグナル起因で1回、EXIT 起因でもう1回、計2回**呼ばれて
しまい、これ自体も上記の二重シグナル問題を悪化させる一因になっていた。

## 修正

1. `exec` をやめて python もバックグラウンドジョブにし `wait` する
   （trap を生かしたまま保つため）。
2. `cleanup()` は INT/TERM に直接バインドせず、INT/TERM は
   `exit 130` / `exit 143` を実行するだけにして、cleanup 自体は EXIT
   トラップからしか呼ばれないようにする（二重呼び出しの防止）。
3. `cleanup()` の中では、各子プロセスに対していきなり `kill` するのでは
   なく、**まず短い猶予時間（`GRACE_PERIOD_SECONDS`）だけ様子を見て**、
   すでにプロセスグループへの直接シグナルで終了処理中ならそれを待つ。
   猶予時間内に終了しなければ（＝スクリプト自身の PID にだけシグナルが
   来た場合など、子プロセスが直接シグナルを受け取っていないケース）、
   そこで初めて明示的に `kill` する。
4. 2つの子プロセスに対する `wait_or_kill` はシーケンシャルではなく
   **並列**に実行する。直接シグナルを受け取っていないケースで両方
   猶予時間いっぱい待つと、直列だと最大で猶予時間の2倍かかってしまう
   ため。

```bash
#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSREPORT_PID=""
SERVER_PID=""

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
  wait_or_kill "${SERVER_PID}" &
  wait_or_kill "${JSREPORT_PID}" &
  wait
}

trap cleanup EXIT
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

wait "${SERVER_PID}"
```

差分は `bin/start_label_print_server.sh` の1ファイルのみ。niimprint /
jsreport 側のコードには手を入れていない（jsreport 自身の SIGTERM/SIGINT
ハンドラは元々正しく実装されている）。

## 検証

`POST /api/report`（`recipe: chrome-pdf`）でレンダリングを1回走らせ、
Chrome 関連プロセスを約15〜20個発生させたあと、以下の2パターンで確認した。

1. **プロセスグループ全体への SIGINT**（実際の Ctrl-C を正確に再現する方法。
   非対話スクリプト内のバックグラウンドジョブはジョブコントロールが
   無効なため親と同じプロセスグループに入る。したがって
   `kill -INT -<script の pgid>` で同じことが起きる）:
   - 修正前: `Closing jsreport instance` が2回ログに出て、Chrome の
     プロセスツリーが孤児として残ることがあった。
   - 修正後: `Closing jsreport instance` は1回だけ、Chrome
     プロセスツリー（zygote / gpu-process / renderer / utility /
     broker / crashpad_handler 含む）が1つも残らないことを確認。
     exit code は 130（数秒以内に完了）。
2. **スクリプト自身の PID にだけ SIGTERM**（`kill <script pid>` や
   systemd の `KillMode=process` を想定。node/python は直接シグナルを
   受け取らない、最も待たされるケース）:
   - 猶予時間（3秒、並列）ののち確実に両方 kill され、Chrome
     プロセスツリーも含めて残らないことを確認。exit code は 143。

## コミット

- `3db6599 Fix orphaned jsreport/Chrome processes after stopping the server`
  （`exec` 除去。これだけでは不十分だったことが後で判明）
- 上記の二重シグナルレース対策（`exit` 経由での trap 一本化 + 並列
  grace period）は追ってコミット予定。
