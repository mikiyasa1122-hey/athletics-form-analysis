# athletics-form-analysis

陸上競技向けAIフォーム解析プラットフォーム（要件定義 REQ-ATH-001 v0.2.0）の**時間軸の中核**です。
要件書 §19「最初に作る縦切り」のうち、計測の正しさを決める部分を先に作りました。

- 元動画のPTS / time_base から原本フレーム索引を作る（固定FPSで換算しない。VFR・重複PTS・欠落・順序逆転を検出）
- 撮影時間軸の確度（`verified` / `container_only` / `declared` / `slowmo_suspected` / `unknown`）を判定し、スロー書き出しでは秒単位の指標を出さない
- 接地・離地を「観測フレームで挟んだ時刻区間」として持ち、接地時間・ピッチは区間から求めた範囲つきで出す。表示桁は区間幅に合わせる
- 指標ごとの品質ゲート（角度は視点・点の可視性、時間指標は時間軸・不確実性）。判定不可には必ず理由を付ける
- 全原本フレーム解析と完全性の記録。間引きプレビューは正式な値（`valid`）にしない
- 手動訂正は自動検出を残した別レイヤーで、依存指標を再計算する
- 表示用プロキシと原本の対応（複製・欠落・補間フレームの検出）
- 原本フレーム単位のプレイヤー（0.1/0.25/0.5/1.0倍、コマ送り、イベント前後ループ）。再生倍率は解析値を変えない

## まだ作っていないもの

認証・共有・API/DB・アップロード、**実際の姿勢推定モデル**（MMPose等はライセンス確認 DEC-002/NFR-SEC-010 の後。現在は推定済みJSONを読む `FixturePoseAdapter` のみ）、動画からの実フレーム画像の表示、動画内/動画間比較、改善提案、短距離以外の種目。
同梱のデモは**合成データ**で、実在の選手データではありません。品質ゲートの閾値（接地時間25%、ピッチ10%、スロー判定の下限ピッチ）は暫定値で、実測ベンチマーク（DEC-016）で決め直す前提です。

## 使い方

必要なもの: Python 3.11以上（標準ライブラリのみ）、Node.js 18以上（テストのみ）、実動画を扱うときは FFmpeg（ffprobe）。

```bash
python -m athform demo --out web/demo
```

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory web
```

ブラウザで http://127.0.0.1:8765 を開きます。←/→で1フレーム送り、Spaceで再生・停止。

実動画の場合（姿勢推定結果のJSONは別途用意）:

```bash
python -m athform probe input.mp4 --out probe.json
```

```bash
python -m athform analyze --probe probe.json --pose pose.json --video input.mp4 --ground-y 900 --view side --direction left_to_right --out web/snapshot.json
```

`--capture-fps`（申告の撮影FPS）や `--slowmo`（スロー書き出しの申告）を渡せます。申告は精度の証明には使われません。

## テスト

```bash
python -m unittest discover -s tests -t .
```

```bash
node --test web/tests/player-core.test.js web/tests/static.test.js
```
