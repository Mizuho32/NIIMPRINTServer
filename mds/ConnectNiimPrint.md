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
