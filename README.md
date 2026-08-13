# NIIM Label Print Server

JSON object / array を受け取り、Queue に積み、Web UI で内容確認・補正したうえで `jsreport` でラベル PNG preview を生成する phase 1 実装です。

この段階では **印刷はまだ行いません**。`niimprint` は将来の phase 2 で外部依存として呼び出す前提で、現状は変更していません。

## 現在の構成

- `src/label_print_server/`
  - FastAPI アプリ本体
  - SQLite ベースの Queue / session override 保存
  - transform / template選択 / printer選択パイプライン
  - jsreport preview 連携
  - server-rendered UI
- `tests/test_label_print_server.py`
  - intake / transform / preview のテスト
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

## まだやっていないこと

- `niimprint` を使った実機印刷
- 印刷成功時の dequeue
- プリンタ状態監視や再試行制御

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

- `printers`: プリンタ名、モデル、将来使う接続先
- `templates`: `jsreport` 側で利用可能なテンプレート名一覧
- `debug`: `true` のときだけ UI からの queue intake を有効化
- `summary_key`: hook 未定義時に一覧の代表テキストへ使うキー名
- `user_hooks_path`: `transform(data: dict | list) -> list[dict]` と `summary_text(data: dict) -> str` を置ける Python ファイル
- `selection`: template / printer の初期選択ロジック

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
- `POST /jobs/dequeue-all` - 一覧上の全 job を dequeue
- `POST /jobs/{job_id}/preview-from-list` - 一覧上の個別 job をレンダリング
- `POST /jobs/{job_id}/dequeue` - 一覧上の個別 job を dequeue
- `POST /api/jobs` - API から JSON object / array を投入
- `GET /api/jobs` - Queue 一覧を JSON で取得
- `GET /jobs/{job_id}` - 個別 job の編集 / preview 画面
- `POST /jobs/{job_id}/draft` - セッション内 draft 保存
- `POST /jobs/{job_id}/preview` - 詳細画面から PNG preview 生成

## テスト

```bash
PYTHONPATH=src ./venv/bin/ruff check src/label_print_server tests/test_label_print_server.py
PYTHONPATH=src ./venv/bin/python -m unittest -v tests.test_label_print_server
```

## 次の段階

phase 2 では preview 済みデータを `niimprint` に渡す adapter を追加し、印刷成功時だけ dequeue する想定です。
