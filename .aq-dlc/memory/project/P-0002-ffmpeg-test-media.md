---
id: P-0002
type: project
status: validated
scope: project:athletics-form-analysis
tags: [project, ffmpeg, ffprobe, fixture, test-media, windows, winget]
stack: [python]
importance: medium
evidence: [athletics-form-analysis/runs/20261118-1956-real-video-ffprobe#V3, athletics-form-analysis/runs/20261118-1956-real-video-ffprobe#V4, athletics-form-analysis/runs/20261118-1956-real-video-ffprobe#V6]
applied_in: [20261118-1956-real-video-ffprobe]
created: 2026-11-18
updated: 2026-11-18
---
# FFmpeg の試験動画とフィクスチャの規約

## 適用条件
時間軸の処理を変えるとき、または試験動画の種類を増やすとき。

## 学んだ行動
- 試験動画は `tools/make_test_videos.py` で作る（`.cache/videos/` に出力、コミットしない）。フィクスチャは `tests/fixtures/ffprobe/`。作り方と期待値は manifest.json。変えたら `--check` が gate で通ること。
- FFmpeg は winget の Gyan.FFmpeg.Essentials（9.0.1）。`%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin` に入り、gate の Python からも見える。ただし実行中のシェルの PATH には反映されないので、`athform/tools.py` の `find_tool` を使う。
- ffprobe 9.0 の実出力:
  - `-show_packets -show_frames` を併用すると、出力は `packets_and_frames` 形式になる。
  - Bフレームではパケットが復号順に並び、DTS は負から始まる。
  - 先頭をカットした動画の pre-roll パケットは、flags に `D` が付き PTS が負。フレームは出ない。
  - 壊れたパケットは、フレームとして出てこない（stderr にエラーが出る）。
- 試験動画を作るときの落とし穴:
  - 長さは `testsrc2=...:duration=` でソースに持たせる。`-t` を `-i` の後ろに置くと出力側の制限になり、`setpts=8*PTS` のスローが切り詰められる。
  - `setpts` は整数で計算する（`if(lt(N,36),N,2*N-36)/(60*TB)`）。`0.6/TB` のような小数は丸めで1tickずれる。
  - 撮影FPSメタデータは `-movflags use_metadata_tags -metadata com.android.capture.fps=240.000000` で書ける。

## 根拠・証拠
V3（実機で作り直して一致）、V4（pre-roll）、V6（可変FPS）が gate で pass。

## 例外・注意点
テストパターンの動画であり、スマホの実際の撮影動画（回転、iPhone のスローモーションのメタデータ、音声付き、MOV）は未確認。

## 元の実行記録
athletics-form-analysis/runs/20261118-1956-real-video-ffprobe
