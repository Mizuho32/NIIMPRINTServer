# NIIMBOTラベルプリントサーバー

## 概要
niimprint, jsreportを使った、ラベルプリントサーバーをつくりたい。

## 要件

input: json object array
output: label print
レンダリングはjsreport、印刷はniimprint。
Web UIを備えるが、シンプルなもので済むはずなので、標準のテンプレートで簡潔。
niimprintを使う都合上、pythonが良いか?
jsonオブジェクトのユーザー定義加工を容易に。(name: xxx→ "name is {{ name }}" みたいな)

### 設定項目
- モデル名, bluetooth addrのペア
- テンプレート名リスト
- ユーザー定義加工処理
- モデル、テンプレート選択のユーザー定義アルゴリズム

### 動作

1. サーバー起動。jsreprotも起動しておく。終了はこの逆
2. httpでinput待ち受け。Queに貯める
3. Web UIアクセスで、一覧表示(まだレンダリングしない)。ユーザー定義加工、モデル・テンプレート選択アルゴリズム適用後、最終的にユーザーが手動で変更できるように。手動変更はブラウザセッションに保存。
4. レンダリングボタン押下でレンダリングイメージを表示。
5. 印刷ボタン押下で印刷しつつDeQue

## 調査
- niimprintとの接続のとこはやるだけなので、Que/UIのあたりを優先で。
- 上記でいけそうか?
- 実現する最適な方法
  - ユーザー定義挙動周りの推奨実装
  - 要件を満たす最適なUI設計

## 2026-08-13 追加要件
- Deque all, deque one (行にボタン追加)も
- Queue intakeはデバッグモードの時のみ、かつjson POST APIも実装して。
- transformsとsummary_keyの定義はjsonだとやっぱダルいので、(適当な)指定の場所にユーザーが.pyを配置したら、それを使うようにして(dict→dictとdict→strな関数を定義)。デフォルト挙動はsummary_keyはname, transformsは空で。
