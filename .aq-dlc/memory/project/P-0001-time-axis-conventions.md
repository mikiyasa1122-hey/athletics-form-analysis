---
id: P-0001
type: project
status: validated
scope: project:athletics-form-analysis
tags: [project, video, time-axis, pts, vfr, measurement, python, stdlib, player]
stack: [python, javascript]
importance: high
evidence: [athletics-form-analysis/runs/20261118-1916-vertical-slice-core#V1, athletics-form-analysis/runs/20261118-1916-vertical-slice-core#V8, athletics-form-analysis/runs/20261118-1916-vertical-slice-core#V19]
applied_in: [20261118-1916-vertical-slice-core]
created: 2026-11-18
updated: 2026-11-18
---
# 時間軸コアの技術選択と規約（REQ-ATH-001）

## 適用条件
このプロジェクトで計測値・イベント・プレイヤーを追加・変更するとき。

## 学んだ行動
- 時刻は整数PTS × `Fraction(time_base)`。公称FPSで時間を計算しない。浮動小数はJSON出力時だけ。
- イベントは「旧状態の最後の観測フレーム」と「新状態の最初の観測フレーム」で挟む区間（lower, upper）。時間指標の範囲は区間の組み合わせで出し、表示桁は区間幅で決める（`format_with_resolution`）。
- 品質ゲートは指標ごと。時間指標 = 撮影時間軸の確度 + 範囲幅/値。角度 = 視点 + 点の可視性。FPSの一律閾値は使わない。
- `valid` の時間指標は撮影FPSメタデータとPTSが一致したとき（verified）だけ。メタデータなしは最高 `estimated`。申告FPSは証明に使わない。
- `analyze()` は再生倍率を受け取らない。入力の索引を変更しない。結果は正準JSONのSHA-256を `analysis_id` にする。
- プレイヤーは原本フレームの提示順インデックスで位置を持ち、骨格は同じ source_frame_id のものだけ表示する。
- Pythonは標準ライブラリのみ（unittest）。JSテストは `node --test` にファイルを明示して渡す（ディレクトリ指定はNode 24で失敗する）。JSテストは毎回 `python -m athform demo` で生成したデモを使う。
- 検証は合成データ（`athform/synth.py`、真値既知）で、区間が真値を含むかを直接確かめる。

## 根拠・証拠
V1（VFR）、V8（区間が真値を含む: 240/120/60/30fps・VFR）、V19（再生倍率不変）が gate で pass。

## 例外・注意点
品質ゲートの閾値（接地時間25%、ピッチ10%、短距離ピッチ下限2.0歩/秒）は暫定。実測ベンチマーク（DEC-016）で置き換える。姿勢推定モデル・実動画（ffprobe）での検証はまだ。

## 元の実行記録
athletics-form-analysis/runs/20261118-1916-vertical-slice-core
