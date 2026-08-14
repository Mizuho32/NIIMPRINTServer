# NIIM Label Print Server

JSON object / array を受け取り、Queue に積み、Web UI で内容確認・補正したうえで `jsreport` でラベル PNG preview を生成し、`niimprint` 経由で実機印刷するサーバーです。

`niimprint` は外部依存として import して呼び出しており、パッケージ自体は変更していません。`src/label_print_server/printer_client.py` はプリンタごとに接続を1本だけ張って使い回し（実機テストで、印刷のたびに接続し直すと `ECONNRESET`/`EBUSY` が頻発し遅くもなることが分かったため）、失敗時だけ接続を破棄して待ってから再接続します。詳細・経緯は `mds/ConnectNiimPrint.md` 参照。

## 現在の構成

- `src/label_print_server/`
  - FastAPI アプリ本体
  - SQLite ベースの Queue / session override 保存
  - transform / template選択 / printer選択パイプライン
  - jsreport preview 連携
  - `niimprint` 印刷アダプタ（プリンタごとに接続を使い回す持続接続 + 失敗時のみ再接続・リトライ）
  - server-rendered UI
- `tests/test_label_print_server.py`
  - intake / transform / preview / print のテスト
- `tests/test_printer_client.py`
  - 印刷アダプタのリトライ挙動のテスト（`niimprint` を fake module に差し替えて実行するので実機・実パッケージ不要）
- `bin/start_label_print_server.sh`
  - `jsreport` と label print server をまとめて起動
- `requirements-label-print-server.txt`
  - label print server 側の追加依存

## できること

1. JSON object / array を HTTP/API から投入
2. transform で object|list を list に正規化して draft を生成
3. template / printer の初期選択を適用
4. 一覧画面で代表テキスト、template、printer を確認
5. 一覧画面から個別レンダリング / 一括レンダリング / dequeue
6. 一覧画面でレンダリング結果をまとめて確認
7. Job 詳細画面で draft JSON を個別調整して preview を再生成
8. Queue を保持したまま再確認
9. 一覧画面・詳細画面から Print 実行。`niimprint` 経由で実機に送信し、成功時のみ Queue から dequeue（失敗時は Queue に残り、エラー内容を表示）
10. 印刷失敗時は設定した回数・待機時間で再接続 + リトライしてから失敗扱いにする
11. プリンタごとの接続を使い回す（連続印刷のたびに繋ぎ直さない）ので、複数枚の連続印刷が速い
12. 印刷枚数（quantity）を一覧・詳細画面で指定可能。他のフィールドと同様ブラウザセッションに保存され、入力するとその場で(自動保存、`input` イベント経由)保存されるので `Print all queued jobs` にも反映される。同じラベルを指定枚数分連続で印刷し、途中の1枚が失敗すると残り枚数は印刷せず job は Queue に残る

## まだやっていないこと

- プリンタの生存監視（heartbeat によるオンライン確認など）
- 印刷キューのバックグラウンド処理（現状は操作者が Print ボタンを押した時だけ同期的に印刷する）

## セットアップ

```bash
./venv/bin/pip install -r requirements-label-print-server.txt
cd jsreport && npm install
```

## 起動

```bash
./bin/start_label_print_server.sh
```

- label print server: `http://127.0.0.1:8000`
- jsreport: `http://127.0.0.1:5488`

## 設定

設定ファイルは `src/label_print_server/config.example.json` です。

主な項目:

- `printers`: プリンタ名、モデル、接続先。各エントリの項目:
  - `name` / `model`: 表示名と niimprint 側のモデル名（`b1` / `b18` / `b21` / `d11` / `d110` など）
  - `address`: `connection` が `bluetooth` なら MAC アドレス、`usb` ならシリアルポート（省略時は自動検出）
  - `connection`: `bluetooth`（既定）または `usb`
  - `density`: 印刷濃度（既定 `5`）
- `templates`: `jsreport` 側で利用可能なテンプレート名一覧
- `debug`: `true` のときだけ UI からの queue intake を有効化
- `summary_key`: hook 未定義時に一覧の代表テキストへ使うキー名
- `user_hooks_path`: `transform(data: dict | list) -> list[dict]` と `summary_text(data: dict) -> str` を置ける Python ファイル
- `selection`: template / printer の初期選択ロジック
- `retry`: 印刷失敗時の再試行設定
  - `max_attempts`: 最大試行回数（既定 `3`）
  - `delay_seconds`: 接続を破棄してから次に再接続するまでの最低待機秒数（既定 `1.0`）。プリンタごとの持続接続が失敗して再接続が必要になったときにだけ効く（接続を使い回せている間は待たない）

`transform` は未定義なら `object -> [object]`, `array[object] -> array[object]` として扱い、`summary_text` は未定義なら `summary_key` → `name` 系の順で使います。
```
└ transform の適用タイミングは intake時 です。POST /api/jobs と debug mode の POST /jobs/intake
で受け取った入力に対してすぐ transform(data)
-> dict を適用し、その結果を内部の draft_json として保存しています。元入力は payload_json
に残るので、入力原本と加工後データの両方を保持する構成です.
```

`selection` の selector は引き続き Python callable を `module:function` 形式で指定します。

## 主なエンドポイント

- `GET /jobs` - Queue 一覧
- `POST /jobs/intake` - debug mode 時だけ UI から JSON object / array を投入
- `POST /jobs/render-all` - 一覧上の全 job を一括レンダリング
- `POST /jobs/print-all` - 一覧上の全 job を一括印刷。job ごとに成功時のみ dequeue、失敗した job だけ Queue に残る
- `POST /jobs/dequeue-all` - 一覧上の全 job を dequeue
- `POST /jobs/{job_id}/preview-from-list` - 一覧上の個別 job をレンダリング
- `POST /jobs/{job_id}/dequeue` - 一覧上の個別 job を dequeue
- `POST /jobs/{job_id}/quantity` - 印刷枚数をセッションに保存（一覧・詳細の Qty 欄から `input` イベントで自動呼び出し）
- `POST /jobs/{job_id}/print-from-list` - 一覧上の個別 job を印刷（保存済みの `quantity` 枚数分）。成功時のみ dequeue、失敗時は一覧にエラー表示
- `POST /api/jobs` - API から JSON object / array を投入
- `GET /api/jobs` - Queue 一覧を JSON で取得
- `GET /jobs/{job_id}` - 個別 job の編集 / preview 画面
- `POST /jobs/{job_id}/draft` - セッション内 draft 保存
- `POST /jobs/{job_id}/preview` - 詳細画面から PNG preview 生成
- `POST /jobs/{job_id}/print` - 詳細画面から印刷（保存済みの `quantity` 枚数分）。preview 済みの画像をそのまま送信し、成功時のみ dequeue

## テスト

```bash
PYTHONPATH=src ./venv/bin/ruff check src/label_print_server tests/
PYTHONPATH=src ./venv/bin/python -m unittest -v tests.test_label_print_server tests.test_printer_client
```

`tests/test_printer_client.py` は `niimprint` を fake module に差し替えてリトライ挙動だけを検証するので、`niimprint` 自体がインストール/import 可能である必要はありません。実機に印刷する場合のみ、起動時に `PYTHONPATH` へ `niimprint/` を含める必要があります（`bin/start_label_print_server.sh` は対応済み）。

## 次の段階

印刷まで一通り実装できたので、残っているのはプリンタの生存監視（heartbeat）と、Web UI からの同期的な Print 操作に依存しないバックグラウンド印刷キューです。
