# real-video-ffprobe

- run_id: 20261118-1956-real-video-ffprobe
- 開始: 2026-11-18T19:56
- ブランチ: aq-dlc/real-video-ffprobe（PR #1 のブランチ aq-dlc/vertical-slice-core の上に積む）

## 01 要求と成功条件
前のrunで「実機の ffprobe 出力では未検証」としていた時間軸の処理を、実際の FFmpeg で確かめる。
- R1 実際の ffprobe の出力形式（packets_and_frames、Bフレーム、負のDTS）
- R2 edit list の pre-roll パケット（flags D）を復号失敗に数えない
- R3 実際に壊れたパケットの復号失敗を検出する
- R4 可変FPSの遅い区間を欠落と誤判定しない
- R5 実際のコンテナ時刻で端から端まで解析し、範囲が真値を含む。スロー書き出しでは秒の値を出さない
- R6 FFmpeg を PATH 反映前でも見つける

### 人間への確認（1回）
- FFmpeg のダウンロード（Gyan.FFmpeg.Essentials 9.0.1、GitHub GyanD/codexffmpeg、約106MB、winget、ユーザー単位）→ 「インストールしてよい」
- 検証用の動画 → 「FFmpegで試験動画を作る」（人物の写った動画は使わない）

### 仮定
- 試験動画の中身は FFmpeg のテストパターン。姿勢は、各原本フレームの撮影時刻（実際のPTS ÷ スロー倍率）から合成モデルで作る。
- フィクスチャは ffprobe の実出力から時間軸に関係する項目だけを残す（値は変えない）。`--check` で実機から作り直して一致を確かめる。
- 人物の写った実動画・スマホ固有のメタデータ（iPhone のスローモーション等）・姿勢推定モデルは範囲外。

## 02 Failure First
- 実ツールの出力形式が想定と違う（F-0010 の教訓）→ 実機で作った動画の出力を先に見てから直す
- 先頭カットした動画の pre-roll を復号失敗と誤判定（前回の再レビュー N2）
- 実デコーダが壊れたパケットをどう扱うか分からない
- 可変FPSの扱い（全体の中央値で欠落を判定している）
- winget で入れたツールが gate から見えない（F-0007）
- フィクスチャが実機から離れていく → `--check` を gate に入れる

## 03 設計・実装
実機で見つかったこと:
1. `-show_packets` と `-show_frames` を併用すると、出力は `packets_and_frames` 形式（前のrunで両対応にしてあった）。
2. 先頭カット（`-ss 0.3 -c copy`）では pre-roll パケット9個に flags `D` と負のPTSが付き、フレームは出ない。旧実装は9個の復号失敗・ID 9始まり・時刻のずれになった → D パケットを除外。
3. パケットの中身を壊すと、デコーダはそのフレームを出力から落とす（stderr にエラー）。パケットとの差分で、壊したPTSのIDが正しく復号失敗になった（前のrunの修正 B4 が実機で動作）。
4. 60→30fps の可変FPSで、30fps 区間の間隔が全体の中央値（1/60）の1.5倍を超え、すべて「欠落」・VFR=False になった → 前後4間隔の局所中央値と比べる方式に変更。
5. Bフレームのある H.264 ではパケットが復号順・DTS が負から始まる。PTS順でIDを振る方式で問題なし。
6. winget の FFmpeg は %LOCALAPPDATA%\Microsoft\WinGet\Packages に入り、Python（gate）からも見えた（F-0007 の仮想化は起きない）。ただし実行中のシェルの PATH には反映されない → `athform/tools.py` で既定の場所も探す。

試験動画を作るときの落とし穴（テストデータ側の誤り）:
- `-t` を `-i` の後ろに置くと出力の長さの制限になり、8倍スローが0.15秒分に切り詰められた → ソース（`testsrc2=...:duration=`）に長さを持たせる。
- `setpts` で `0.6/TB` のような小数を使うと浮動小数の丸めで時刻が1tickずれ、1/60・1/20 の不規則な間隔になった → 整数で計算する。

## 04 Quality Gate / レビュー
- gate: compile / js-syntax / test（python 97件）/ test-js（node 13件）/ real-ffprobe（実機で再生成して一致）PASS、V1〜V11 PASS。
- 新しいテストが旧実装で失敗することを確認（pre-roll・VFR・cut_copy の期待値で FAIL）。
- 独立レビュー: （記録待ち）

## 05 修正ループ
（レビュー後に記録）

## 06 Memory
- 保存/統合:
- SKIP（保留した候補）:
- REVIEW の結果:
- Skill改善提案: 前のrunと同じ（cp932 のデコード警告）。

## 結果
（レビュー後に記録）
