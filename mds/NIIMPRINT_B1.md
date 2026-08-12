# niimprintf B1 印刷途中で切れる問題

`AndBondStyle/niimprint` (niimprint/) で B1 プリンタを使おうとしている。

GitHub Issue #49 にある `PrinterClientFixed` をそのまま使うと、印刷自体は途中まで正常に進むが、**最後まで印刷されず途中で切れる**。

現在使っているコード：

```python
class PrinterClientFixed(PrinterClient):
    def print_image(self, image, density=5):
        # 1) wake-up / protocol-select
        self._send(NiimbotPacket(0x54, b"\x01"))
        time.sleep(0.05)
        try: self._recv()
        except: pass

        # 2) standard config
        self.set_label_density(density)
        self.set_label_type(1)

        # 3) START_PRINT — 7 bytes (was 1)
        self._send(NiimbotPacket(0x01, b"\x00\x01\x00\x00\x00\x00\x00"))
        time.sleep(0.1)
        try: self._recv()
        except: pass

        # 4) standard
        self.start_page_print()

        # 5) SET_DIMENSION — 6 bytes (was 4)
        h, w = image.height, image.width
        self._send(NiimbotPacket(0x13, struct.pack(">HHH", h, w, 1)))
        time.sleep(0.1)
        try: self._recv()
        except: pass

        # 6) raster — standard niimprint format works fine
        for pkt in self._encode_image(image):
            self._send(pkt)

        # 7) standard finalize
        self.end_page_print()
        time.sleep(0.3)
        while not self.end_print():
            time.sleep(0.1)
```

関連 Issue #17 で `@MultiMote` が以下を指摘している：

> it's not enough. You need to poll printer with PrintStatus (0xa3) to check when print is finished before sending PrintEnd. Otherwise, the print will be cut off.

また、

> last parameter in RequestCodeEnum.SET_DIMENSION is copies count (for B1).

とある。

したがって現在のコードで最も怪しいのはここ：

```python
for pkt in self._encode_image(image):
    self._send(pkt)

self.end_page_print()
time.sleep(0.3)
while not self.end_print():
    time.sleep(0.1)
```

現在は、

```text
raster送信
→ end_page_print()
→ 0.3秒待つ
→ end_print()
```

となっている。

B1では画像データを送信し終わった時点ではまだプリンタ内部で印刷処理中の可能性があり、`PrintEnd` を早く送ると印刷タスクが終了してしまい、**印刷が途中で切れる**と考えられる。

Issue #17 の指摘通り、

```text
raster送信
→ end_page_print()
→ PrintStatus (0xa3) をpoll
→ 実際の印刷完了を確認
→ end_print()
```

というフローに変更するのが本命。

`SET_DIMENSION` については現在、

```python
struct.pack(">HHH", h, w, 1)
```

となっており、最後の `1` は B1 の copies count と考えられるため、ここは既に正しそう。

### 次に調べること

`niimprint` のソースから `PrintStatus / 0xa3` の実装・レスポンス形式・完了状態の判定方法を確認する。

例えば：

```bash
grep -R "0xa3\|PrintStatus\|PRINT_STATUS" .
```

または `PrinterClient` 周辺のコードを確認する。

**推測で固定値を決めず、既存の niimprint 実装に合わせて `0xa3` のpollingを追加したい。**

最終的には `PrinterClientFixed.print_image()` の最小限のdiffとして修正したい。

