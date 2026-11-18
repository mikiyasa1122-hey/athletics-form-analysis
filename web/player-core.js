/*
 * プレイヤーの中核ロジック（DOMに依存しない。node --test で検証する）。
 * - 表示位置は「原本フレームの提示順インデックス」で持つ。<video>.currentTime の概算シークは使わない（FR-PLAY-004）。
 * - 再生倍率はUI状態であり、解析データ・元動画時刻・フレームIDを変えない（FR-TIME-006, AC-010）。
 * - 骨格は表示中の原本フレームIDと完全一致するものだけを返す。近傍フレームへずらさない（FR-PLAY-003）。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PlayerCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const RATES = Object.freeze([1, 0.5, 0.25, 0.1]);

  function deepFreeze(obj) {
    if (obj && typeof obj === "object" && !Object.isFrozen(obj)) {
      Object.freeze(obj);
      for (const v of Object.values(obj)) deepFreeze(v);
    }
    return obj;
  }

  function parseFraction(text) {
    const m = /^(-?\d+)\/(\d+)$/.exec(String(text));
    if (!m || Number(m[2]) === 0) return null;
    return Number(m[1]) / Number(m[2]);
  }

  /** スナップショットを読み込み、変更できないモデルにする。 */
  function loadSnapshot(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.frames) || snapshot.frames.length === 0) {
      throw new Error("スナップショットに原本フレームがない");
    }
    const frames = snapshot.frames.slice().sort((a, b) => a.presentation_index - b.presentation_index);
    const positionById = new Map();
    frames.forEach((f, i) => {
      if (!Number.isInteger(f.source_frame_id)) throw new Error("source_frame_id が整数でない");
      if (positionById.has(f.source_frame_id)) throw new Error("source_frame_id が重複している: " + f.source_frame_id);
      positionById.set(f.source_frame_id, i);
    });
    const poseById = new Map();
    for (const p of snapshot.pose_raw || []) poseById.set(p.source_frame_id, p);
    return deepFreeze({
      snapshot: snapshot,
      frames: frames,
      positionById: positionById,
      poseById: poseById,
      events: (snapshot.events && snapshot.events.effective) || [],
      metrics: snapshot.metrics || [],
    });
  }

  function createState() {
    return Object.freeze({ position: 0, rate: 1, playing: false, playbackElapsedMs: 0, loop: null });
  }

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  function setRate(state, rate) {
    if (!RATES.includes(rate)) throw new Error("対応していない再生倍率: " + rate);
    return Object.freeze(Object.assign({}, state, { rate: rate }));
  }

  function setPlaying(state, playing) {
    return Object.freeze(Object.assign({}, state, { playing: Boolean(playing) }));
  }

  /** 原本フレーム単位で前後に移動する。ループ中はループ範囲内に収める。 */
  function step(model, state, delta) {
    if (!Number.isInteger(delta)) throw new Error("delta は整数");
    const lo = state.loop ? state.loop[0] : 0;
    const hi = state.loop ? state.loop[1] : model.frames.length - 1;
    return Object.freeze(Object.assign({}, state, { position: clamp(state.position + delta, lo, hi) }));
  }

  function seekFrameId(model, state, frameId) {
    const pos = model.positionById.get(frameId);
    if (pos === undefined) throw new Error("原本フレーム " + frameId + " は存在しない");
    return Object.freeze(Object.assign({}, state, { position: pos, loop: null }));
  }

  function overlayFor(model, frameId) {
    return model.poseById.get(frameId) || null;
  }

  /** 画面に出す内容。フレームID・PTS・元動画時刻・骨格は必ず同じ原本フレームを指す。 */
  function view(model, state) {
    const f = model.frames[state.position];
    const overlay = overlayFor(model, f.source_frame_id);
    return {
      position: state.position,
      frameCount: model.frames.length,
      frameId: f.source_frame_id,
      pts: f.pts,
      timeBase: f.time_base,
      sourceTime: f.t,
      sourceTimeExact: f.t_exact,
      flags: f.flags || [],
      overlay: overlay,
      overlayFrameId: overlay ? overlay.source_frame_id : null,
      rate: state.rate,
      playbackElapsedMs: state.playbackElapsedMs,
    };
  }

  function exactTime(frame) {
    const v = parseFraction(frame.t_exact);
    return v === null ? frame.t : v;
  }

  /** 次の原本フレームを表示するまでの待ち時間（ms）。元動画のPTS差 ÷ 再生倍率。末尾なら null。 */
  function nextDelayMs(model, state) {
    const hi = state.loop ? state.loop[1] : model.frames.length - 1;
    if (state.position >= hi) return null;
    const dt = exactTime(model.frames[state.position + 1]) - exactTime(model.frames[state.position]);
    return (dt * 1000) / state.rate;
  }

  /** 再生中の1ティック。ループ中は末尾から先頭へ戻る。 */
  function tick(model, state) {
    const delay = nextDelayMs(model, state);
    if (delay === null) {
      if (state.loop) {
        return Object.freeze(Object.assign({}, state, { position: state.loop[0] }));
      }
      return setPlaying(state, false);
    }
    return Object.freeze(Object.assign({}, state, {
      position: state.position + 1,
      playbackElapsedMs: state.playbackElapsedMs + delay,
    }));
  }

  /** イベント根拠フレームの前後 pad フレームをループ範囲にする（FR-PLAY-006）。 */
  function loopAroundEvent(model, state, event, pad) {
    const ids = event.source_frame_ids || [];
    const positions = ids.map((id) => model.positionById.get(id)).filter((p) => p !== undefined);
    if (positions.length === 0) throw new Error("イベントの根拠フレームが索引にない");
    const lo = clamp(Math.min.apply(null, positions) - pad, 0, model.frames.length - 1);
    const hi = clamp(Math.max.apply(null, positions) + pad, 0, model.frames.length - 1);
    const evidence = model.positionById.get(event.evidence_frame_id);
    return Object.freeze(Object.assign({}, state, {
      loop: [lo, hi],
      position: evidence === undefined ? lo : evidence,
    }));
  }

  function clearLoop(state) {
    return Object.freeze(Object.assign({}, state, { loop: null }));
  }

  function eventsAtFrame(model, frameId) {
    return model.events.filter((e) => (e.source_frame_ids || []).includes(frameId));
  }

  function formatSeconds(t, digits) {
    if (typeof t !== "number" || !Number.isFinite(t)) return "—";
    return t.toFixed(digits === undefined ? 6 : digits) + " s";
  }

  const STATUS_LABEL = Object.freeze({
    valid: "有効",
    estimated: "推定",
    review_required: "要確認",
    unavailable: "判定不可",
  });

  const CAPTURE_LABEL = Object.freeze({
    verified: "撮影時間を確認済み",
    container_only: "動画のPTSを実時間とみなす（撮影メタデータなし）",
    declared: "申告のみ（実時間を確認できない）",
    slowmo_suspected: "スロー書き出しの疑い（実時間を確認できない）",
    unknown: "不明",
  });

  return {
    RATES: RATES,
    loadSnapshot: loadSnapshot,
    createState: createState,
    setRate: setRate,
    setPlaying: setPlaying,
    step: step,
    seekFrameId: seekFrameId,
    overlayFor: overlayFor,
    view: view,
    nextDelayMs: nextDelayMs,
    tick: tick,
    loopAroundEvent: loopAroundEvent,
    clearLoop: clearLoop,
    eventsAtFrame: eventsAtFrame,
    formatSeconds: formatSeconds,
    parseFraction: parseFraction,
    STATUS_LABEL: STATUS_LABEL,
    CAPTURE_LABEL: CAPTURE_LABEL,
  };
});
