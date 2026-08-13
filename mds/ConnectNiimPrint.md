# niimprintを接続

## 概要

niimprint/をimportして接続したい。
基本は、コマンドの
```
python niimprint --model (model name) -a (addr) -c bluetooth --image ${IMG}
```
相当のことをやって成功した順にdequeすればいいと思うんだけど、
なんか連続で印刷したときに前回印刷の後処理待ちかなんかでエラー吐くことがあるらしい。

niimprint/ 80bf9 でやった、endの前にstatus pollしてwaitみたいなのが開始時にも要るのかな?

## 要件

- niimprintを接続して、印刷できるように
- 印刷前waitみたいなの、必要そうなら実装。よくわからなかったらスキップして、後で対処


## 調査結果

### 1. 現状の `niimprint` 実装

- `niimprint/niimprint/printer.py` の `PrinterClientFixed`（80bf9ec で追加、`__main__.py` も既にこちらを使用）が現行の実質デフォルト実装。
- 印刷後 (`end_page_print()` の後) に `_wait_for_print_complete()` で `get_print_status()` をポーリングし、`page >= total_pages` かつ `progress1 >= 100` になるまで待ってから `while not self.end_print(): time.sleep(0.1)` で `end_print()` を返り値が truthy になるまでリトライしている。つまり「印刷後の後処理待ち」自体は既に実装済み。
- 一方 `print_image()` 冒頭では `START_PRINT` 送信直後に `try: self._recv() except: pass` で応答を捨てるだけの実装になっており、開始側には「完了待ち」に相当する処理は元々無い。

### 2. コミュニティ資料での裏取り ([NIIMBOT Community Wiki](https://printers.niim.blue/interfacing/print-tasks/), [niimbluelib](https://github.com/MultiMote/niimbluelib))

- Wiki には「Bluetooth接続の場合、`PrintStart` 直後の最初の応答パケットと `PrintEnd` 直後の最初の応答パケットが drop されることがある」という既知のクセが明記されている。`PrinterClientFixed.print_image()` 冒頭の wake-upパケット送信 + 応答を捨てる `try/except` は、まさにこの `PrintStart` 側の drop を吸収するためのワークアラウンドと見られる。
- 一方 `PrintEnd` 側の drop に対する同種のワークアラウンドは入っていない（後述）。
- リファレンス実装とされる `niimbluelib` の print task 実装を見ても、「次のジョブを開始する前に明示的な readiness/idle チェック（status poll）を行う」処理は存在しない。前ジョブの完了待ち（status polling）がきちんと終わっていれば、そのまま次のジョブの初期化コマンドを送る設計になっている。
  → 結論として、「開始時に専用の status poll を新設する」必要性は薄そう。コミュニティ実装でもやっていない。

### 3. 本命と思われる原因: `_transceive()` の `None` 未処理

- `PrinterClient._transceive()` は最大6回（約0.6秒）リトライしても該当する応答パケットが受信できない場合、`None` を返す実装になっている。
- しかし `start_print` / `end_print` / `start_page_print` / `end_page_print` / `set_label_density` / `set_label_type` などの呼び出し側は、すべて `bool(packet.data[0])` と無条件にアクセスしており、`packet` が `None` の場合は `AttributeError` が飛ぶ。`False` を返してリトライに回る、という設計には元々なっていない。
- `PrinterClientFixed.print_image()` 終端の `while not self.end_print(): time.sleep(0.1)` は「`end_print()` が正常に `False` を返すケース」の再試行しか想定しておらず、応答 drop で `None`（＝`AttributeError`）になるケースはこのループに入る前に例外で落ちる。
- Wiki が明言している「`PrintEnd` 後の最初のパケットが drop されることがある」は、まさにこの `end_print()` の1回目呼び出しで起こり得る事象であり、「前回印刷の後処理待ちらしきところでエラーを吐く」という現象の実体はここである可能性が高い。

### 4. 結論・方針

- **開始前の status poll は今のところ不要**と判断。80bf9ec で入れた「終了時に完了を待ってから `end_print()` する」設計方針自体は妥当で、コミュニティ実装とも整合している。
- 実際にエラーを引き起こしているのはおそらく `niimprint` 側 `_transceive()` の `None` 未処理（`AttributeError`）であり、応答 drop という既知のBluetooth特有の現象に起因する。ただし `niimprint/` は変更しない方針（`README.md` / plan.md）のため、`niimprint` 自体は直さず、label_print_server 側の print アダプタ（phase 2）で吸収する:
  - 印刷呼び出しは `PrinterClientFixed` を使う。
  - `niimprint` 呼び出し全体を `try/except Exception`（`AttributeError` を含む）で囲み、失敗時は一定回数だけ再接続 + リトライしてから job を failed として扱う、というリトライ層をアダプタ側に持たせる。
  - `BluetoothTransport`/`SerialTransport` に `close()` は無いため、接続はジョブ間で使い回しつつ、エラー時は単純にオブジェクトを破棄して新規に接続し直す方針でよさそう。
  - CLI (`python -m niimprint`) は1プリントごとにプロセスを起動して都度新規接続する設計だが、サーバーとして import して使う場合は接続を使い回す（＝連続印刷になる）ため、上記のリトライ層は特に重要になる。

## 5. 実機テストでの追加知見・方針転換 (2026-08-13)

phase 2 実装（毎回新規接続 + リトライ層、`NiimprintPrintClient`）を実機（B1）で使ったところ、以下の症状が出た。

### 症状

- **ほぼ毎回、1回目の接続試行 (attempt 1/3) で `ECONNRESET` (`[Errno 104] Connection reset by peer`)** になり、2回目で成功する。
- **3枚連続で印刷すると、3回目の印刷で3回のリトライすべてが `EBUSY` (`[Errno 16] Device or resource busy`) になり、印刷が失敗する。**
- 結果として、印刷のたびに「接続→(高確率で失敗)→delay→再接続」を繰り返すため、体感速度がかなり遅い。

### 原因の切り分け

2つの症状は原因が別と考えられる。

1. **`ECONNRESET` が毎回起きる**: 直前の印刷の切断直後に次の接続を張ろうとして、OS/プリンタ側がまだ切断処理中のところに弾かれていると見られる。現在のリトライ実装は**リトライ間**にしか `delay_seconds` を入れておらず、1回目の接続前には待ちが無い。1回目が構造的にほぼ確実に失敗し、2回目（delay 後）で通っているのはこれと辻褄が合う。
2. **3枚目で `EBUSY` が3回とも起きる**: OSレベルで「チャネルが使用中」というエラーであり、単なるタイミングの問題というより**前のソケットが正しく閉じられていない**疑いが強い。`niimprint` の `BluetoothTransport` に `close()` が無いため、リトライのたびに新しいソケットを作りながら、失敗した古いソケットは明示的に閉じずに GC 任せにしている。しかも `NiimprintPrintClient` は失敗した例外を `last_error` に保持し続ける実装で、traceback 経由で古い `transport`/socket への参照が想定より長く生き続け、GC による解放（≒ fd/RFCOMM チャネルの解放）が遅れている可能性がある。

### 検討した方針

| 方針 | 内容 | 評価 |
| --- | --- | --- |
| A. 都度接続 + 明示close + settle delay | 毎回新規接続する今の設計のまま、ソケットを確実に close し、接続前に直近切断からの経過時間で最低待ち時間を確保する | 変更範囲は小さいが、印刷のたびに「切断 → 待つ → 再接続」を繰り返す構造は変わらず、**体感速度の遅さは解決しない** |
| **B. プリンタごとに接続を使い回す (persistent connection)** | Queue/プロセス全体でプリンタごとに1本の接続を保持し、複数印刷で使い回す。エラー時だけ再接続 | 接続の頻度そのものを減らせるため、速度・EBUSY双方に効く。実装コストはAより大きい |
| C. heartbeat/get_print_status で前ジョブ完了を確認してから次を張る | - | 前回調査の通り、busy/idle 判定に使える保証が薄く、根本原因（接続の解放漏れ）には効かない可能性が高いため見送り |
| D. リトライ回数/delay を config で増やすだけ | - | 応急処置。3回とも失敗するレベルの busy には効かない可能性が高く、体感速度もさらに悪化する |

→ **現状すでに体感速度が遅く、Queue から複数枚まとめて印刷する運用を考えると尚更「毎回接続し直す」設計自体がボトルネックになる**ため、**方針B（持続接続化）を採用**する。ただし、B は「毎回接続する頻度」を減らすだけで、実際に（再）接続が発生する場面（初回接続・エラー後の再接続）では A で挙げた「明示 close」「settle delay」は依然として必要なので、B の中に取り込む。

### 方針B: 持続接続化の設計

- **プリンタ名ごとに1本の接続を保持するプール**を `NiimprintPrintClient`（`printer_client.py`）内に持たせる。`{printer_name: {"transport": ..., "client": PrinterClientFixed, "last_used_at": ...}}` のようなイメージ。
- **`print_label()` の流れ**:
  1. 対象プリンタの接続が無ければ新規接続して保持。あればそのまま使い回す。
  2. `print_image()` が例外なく成功すれば、接続はそのまま保持して return（次回も使い回す）。
  3. 例外が起きたら、その接続は**壊れているとみなして破棄**（明示 close）し、settle delay を挟んでから**新規接続を1回だけ**やり直す。それでも失敗したら、現行同様に `retry.max_attempts` 回まで「新規接続してやり直す」を繰り返し、尽きたら `RuntimeError` にまとめて上げる（＝Aの「明示close + settle delay」はここに内包される）。
- **並行アクセスの直列化**: FastAPI の同期 `def` ルートはスレッドプールで実行されうるため、同じプリンタへの接続を複数リクエストが同時に触らないよう、プリンタ名ごとに `threading.Lock` を持たせて印刷処理全体を直列化する。
- **`close()` の扱い**: `niimprint` の transport には `close()` が無いため、`niimprint` 自体は変更せず、`printer_client.py` 側で `transport._sock.close()`（Bluetooth）/ `transport._serial.close()`（Serial）相当を呼ぶ薄いラッパーを用意する。
- **プロセス終了時のクリーンアップ**: FastAPI の shutdown イベント（lifespan）で、保持している全接続を明示的に close する。
- **既存インターフェースへの影響**: `PrintClient.print_label(printer, image_bytes)` という外向きのシグネチャ自体は変えずに済む見込み。`app.py` 側（`_print_and_dequeue` など）の変更は基本的に不要で、`NiimprintPrintClient` の内部実装だけを持続接続対応に置き換える想定。
- **設定**: 接続の最大アイドル時間（あまり長く使い回すと相手が切ってくる可能性があるので、一定時間未使用なら次回接続し直す）等をどこまで config 化するかは実装時に検討。まずは `retry` セクション配下 or 新設の `connection` セクションに置く案。

まだ実装はしていない。次のステップはこの設計での実装。

### 実装結果 (2026-08-13)

上記の設計通り `NiimprintPrintClient`（`src/label_print_server/printer_client.py`）をプリンタ名ごとの持続接続方式に置き換えた。

- 接続はプリンタ名をキーに保持し、成功した印刷では再利用。失敗した印刷でだけ `_discard_connection()` で明示的に close（`transport._sock` / `transport._serial` を直接 close。`niimprint` 自体は変更していないので private 属性越しに触っている）。
- 再接続前の待ち時間は新しい config セクションを増やさず、既存の `retry.delay_seconds` を「直近の切断からの経過時間」ベースの settle delay として流用（初回接続や、直近の切断から十分時間が経っている場合は待たない）。
- 同一プリンタへの並行アクセスは printer 名ごとの `threading.Lock` で直列化。
- サーバー終了時 (`create_app` の FastAPI `lifespan`) に保持している全接続を close。
- 外向きの `PrintClient.print_label(printer, image_bytes)` のシグネチャは変更なし。`app.py` 側の変更は shutdown 時の cleanup 呼び出しのみ。
- `tests/test_printer_client.py` に、接続の使い回し・失敗時の close+再接続・`close()` の一括クローズ・settle delay の有無をそれぞれ検証するテストを追加。
- 接続の最大アイドル時間の管理（一定時間使っていない持続接続を能動的に閉じる）は今回は見送り。実運用で必要になったら追加する。

