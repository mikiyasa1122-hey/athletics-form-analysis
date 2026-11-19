---
id: P-0003
type: project
status: validated
scope: project:athletics-form-analysis
tags: [project, local-app, server, security, rtmpose, storage, versioning]
stack: [python, javascript]
importance: high
evidence: [athletics-form-analysis/runs/20261118-2032-functional-app#V2, athletics-form-analysis/runs/20261118-2032-functional-app#V18, athletics-form-analysis/runs/20261118-2032-functional-app#V20, athletics-form-analysis/runs/20261118-2032-functional-app#V23]
applied_in: [20261118-2032-functional-app]
created: 2026-11-19
updated: 2026-11-19
---
# ローカルアプリ（athform.server）の規約

## 適用条件
アプリの API・保存・解析ジョブ・画面を変えるとき。

## 学んだ行動
- 起動は `.venv/Scripts/python -m athform.server`（127.0.0.1:8766）。データは `~/.athform/data`（プロジェクトの外。F-0017）。`--data` / `ATHFORM_DATA` で変えられる。
- numpy・opencv・rtmlib を使うのは `athform/realpose.py` だけ。他は標準ライブラリのみ（テストはモデルなしで動く。姿勢と ffprobe は偽物に差し替える）。
- 書き込み系 API は、Origin の確認を最初に行い、JSON（application/json）か `X-Athform-Upload: 1` を必須にする。静的ファイルは許可リスト。CORS は許可しない（S-0001）。
- 解析は毎回新しい版（`analyses/r<N>.json`、排他作成）。`analysis_id` は内容のハッシュで、版とは別（F-0015）。
- 手動訂正は自動検出を残した別レイヤー。保存済みの訂正は再解析で当てられなければ「未適用」として記録する（失敗にしない）。取り消しは記録を残す。
- 指標の状態の上限は、次の順に重ねて決める。
  1. 品質ゲート
  2. 種目×指標の公開状態（disciplines.py）
  3. プレビュー（軽量モデル）
  4. 地面の線の質（自動推定なら estimated、問題があれば review_required。手動指定のときだけ valid）
- 追跡は範囲を広げず、取り直しには `reacquired` の印を付けて観測を区切る。
- 画面は textContent だけを使う。`[hidden]{display:none!important}` を消さない（F-0014）。

## 根拠・証拠
V2（他サイトからの書き込みの拒否）、V18（訂正と地面の変更の両立）、V20（版の不変性）、V23（自動推定の地面で valid が出ない）。

## 例外・注意点
公開の大会映像（カメラが動く・人物が小さい）では、地面の問題が出て valid は出ない。正確な値には、固定カメラで側面から撮った動画が要る。精度（誤差何ms）は未検証。

## 元の実行記録
athletics-form-analysis/runs/20261118-2032-functional-app
