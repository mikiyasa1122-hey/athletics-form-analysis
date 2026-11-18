/* 画面の描画とイベント処理。計算は player-core.js に置く。
 * 解析データ由来の文字列は textContent でのみ挿入する（HTMLとして解釈させない）。 */
(function () {
  "use strict";
  const PC = window.PlayerCore;
  const $ = (id) => document.getElementById(id);

  const el = {
    demo: $("demo-select"), file: $("file-input"), error: $("load-error"),
    stage: $("stage"), rate: $("rate-select"), play: $("btn-play"), prev: $("btn-prev"), next: $("btn-next"),
    scrub: $("scrubber"), unloop: $("btn-unloop"),
    frame: $("ro-frame"), pts: $("ro-pts"), time: $("ro-time"), elapsed: $("ro-elapsed"),
    overlay: $("ro-overlay"), events: $("ro-events"),
    qList: $("quality-list"), qNotes: $("quality-notes"),
    eventsBody: $("events-body"), metricsBody: $("metrics-body"),
  };

  let model = null;
  let state = PC.createState();
  let timer = null;

  function node(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }

  function clear(n) {
    while (n.firstChild) n.removeChild(n.firstChild);
  }

  function showError(msg) {
    el.error.textContent = msg;
    el.error.hidden = !msg;
  }

  // ---------- 描画 ----------
  const BONES = [
    ["left_shoulder", "right_shoulder"], ["left_hip", "right_hip"],
    ["left_shoulder", "left_hip"], ["right_shoulder", "right_hip"],
    ["left_hip", "left_knee"], ["left_knee", "left_ankle"], ["left_ankle", "left_heel"], ["left_ankle", "left_toe"],
    ["right_hip", "right_knee"], ["right_knee", "right_ankle"], ["right_ankle", "right_heel"], ["right_ankle", "right_toe"],
  ];

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function drawStage(v) {
    const ctx = el.stage.getContext("2d");
    const w = el.stage.width, h = el.stage.height;
    ctx.fillStyle = cssVar("--stage");
    ctx.fillRect(0, 0, w, h);
    const groundY = model.snapshot.detector && model.snapshot.detector.ground_y;
    if (typeof groundY === "number") {
      ctx.strokeStyle = cssVar("--ground");
      ctx.setLineDash([10, 8]);
      ctx.beginPath(); ctx.moveTo(0, groundY); ctx.lineTo(w, groundY); ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.font = "28px system-ui, sans-serif";
    ctx.fillStyle = cssVar("--muted");
    if (!v.overlay) {
      ctx.fillText("このフレームの骨格はありません（推定失敗または未解析）", 40, 60);
      return;
    }
    const kp = v.overlay.keypoints || {};
    const pt = (name) => kp[name] || null;
    ctx.lineWidth = 6;
    for (const [a, b] of BONES) {
      const p = pt(a), q = pt(b);
      if (!p || !q) continue;
      ctx.strokeStyle = a.startsWith("left") && b.startsWith("left") ? cssVar("--joint") : cssVar("--bone");
      ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
    }
    for (const name of Object.keys(kp)) {
      const p = kp[name];
      if (!p) continue;
      ctx.fillStyle = p[2] >= 0.5 ? cssVar("--joint") : cssVar("--review");
      ctx.beginPath(); ctx.arc(p[0], p[1], 7, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = cssVar("--muted");
    ctx.fillText("frame " + v.frameId + "（骨格 frame " + v.overlayFrameId + "）", 40, 60);
    ctx.fillText("左脚 = 青 / 右脚 = 白。合成データのため映像はありません", 40, 100);
  }

  function render() {
    if (!model) return;
    const v = PC.view(model, state);
    drawStage(v);
    el.frame.textContent = v.frameId + "  (" + (v.position + 1) + " / " + v.frameCount + ")";
    el.pts.textContent = v.pts + " × " + v.timeBase;
    el.time.textContent = PC.formatSeconds(v.sourceTime) + "  [" + v.sourceTimeExact + "]";
    el.elapsed.textContent = (v.playbackElapsedMs / 1000).toFixed(3) + " s（×" + v.rate + "）";
    el.overlay.textContent = v.overlayFrameId === null ? "なし" : String(v.overlayFrameId);
    const evs = PC.eventsAtFrame(model, v.frameId);
    el.events.textContent = evs.length ? evs.map((e) => e.side + " " + e.event_type).join(", ") : "—";
    el.scrub.value = String(v.position);
    el.play.textContent = state.playing ? "一時停止" : "再生";
    el.unloop.hidden = !state.loop;
    for (const tr of el.eventsBody.querySelectorAll("tr")) {
      tr.classList.toggle("current", (tr.dataset.frames || "").split(",").includes(String(v.frameId)));
    }
  }

  function renderQuality() {
    clear(el.qList); clear(el.qNotes);
    const q = model.snapshot.capture_quality || {};
    const c = model.snapshot.frame_completeness || {};
    const iv = q.interval_s || {};
    const fmt = (x) => (typeof x === "number" ? (x * 1000).toFixed(2) + " ms" : "—");
    const rows = [
      ["撮影時間軸", PC.CAPTURE_LABEL[q.capture_time_status] || q.capture_time_status],
      ["公称FPS（コンテナ）", q.nominal_fps === null || q.nominal_fps === undefined ? "—" : q.nominal_fps],
      ["撮影FPS", q.capture_fps === null || q.capture_fps === undefined ? "不明" : q.capture_fps],
      ["フレーム間隔（実測）", fmt(iv.min) + " / " + fmt(iv.median) + " / " + fmt(iv.max) + "（最小/中央/最大）"],
      ["可変FPS", q.vfr ? "あり" : "なし"],
      ["欠落の疑い", (q.gap_frame_ids || []).length + " 箇所"],
      ["重複PTS", (q.duplicate_pts_frame_ids || []).length + " 枚"],
      ["復号失敗", c.decode_check === "packets" ? (c.decode_failed_source_frame_ids || []).length + " 枚" : "確認できない（パケット情報なし）"],
      ["観測の途切れ", ((model.snapshot.events && model.snapshot.events.observation_breaks) || []).length + " 箇所（またいだ指標は出さない）"],
      ["解析方式", model.snapshot.preview ? "プレビュー（間引きあり）" : "全原本フレーム（精度優先）"],
      ["解析フレーム", (c.analyzed_frame_count ?? "—") + " / " + (c.decoded_frame_count ?? "—") + "（推定失敗 " + (c.pose_failed_frame_ids || []).length + "）"],
      ["モデル", model.snapshot.model ? model.snapshot.model.model_id + " " + model.snapshot.model.weights_version : "—"],
      ["解析ID", (model.snapshot.analysis_id || "").slice(0, 12)],
    ];
    for (const [k, v] of rows) {
      el.qList.appendChild(node("dt", k));
      el.qList.appendChild(node("dd", v));
    }
    for (const n of q.capture_time_notes || []) el.qNotes.appendChild(node("li", n));
  }

  function statusCell(status) {
    return node("td", PC.STATUS_LABEL[status] || status, "status status-" + status);
  }

  function jumpButton(label, frameId) {
    const b = node("button", label);
    b.type = "button";
    b.addEventListener("click", () => { stop(); state = PC.seekFrameId(model, state, frameId); render(); });
    return b;
  }

  function renderEvents() {
    clear(el.eventsBody);
    const typeLabel = { foot_contact: "接地", toe_off: "離地" };
    for (const e of model.events) {
      const tr = document.createElement("tr");
      tr.dataset.frames = (e.source_frame_ids || []).join(",");
      tr.appendChild(node("td", (typeLabel[e.event_type] || e.event_type) + (e.source === "manual" ? "（手動訂正）" : "")));
      tr.appendChild(node("td", e.side === "left" ? "左" : "右"));
      const iv = e.time_interval_sec || {};
      tr.appendChild(node("td", PC.formatSeconds(iv.lower, 4) + " 〜 " + PC.formatSeconds(iv.upper, 4), "mono"));
      tr.appendChild(node("td", (e.source_frame_ids || []).join(" → "), "mono"));
      const st = statusCell(e.quality_status === "valid" ? "valid" : "review_required");
      if (e.quality_reasons && e.quality_reasons.length) st.title = e.quality_reasons.join(" / ");
      tr.appendChild(st);
      const actions = document.createElement("td");
      actions.appendChild(jumpButton("移動", e.evidence_frame_id));
      const loop = node("button", "前後をループ");
      loop.type = "button";
      loop.addEventListener("click", () => {
        stop();
        state = PC.setRate(PC.loopAroundEvent(model, state, e, 6), Math.min(state.rate, 0.25));
        el.rate.value = String(state.rate);
        render();
      });
      actions.appendChild(loop);
      tr.appendChild(actions);
      el.eventsBody.appendChild(tr);
    }
  }

  function renderMetrics() {
    clear(el.metricsBody);
    const names = {
      contact_time: "接地時間", cadence: "ピッチ",
      knee_angle_at_contact: "接地時の膝角度（2D）", trunk_lean_at_contact: "接地時の体幹前傾（2D）",
    };
    for (const m of model.metrics) {
      const tr = document.createElement("tr");
      const name = node("td", names[m.metric_id] || m.metric_id);
      name.title = m.definition + "（" + m.coordinate_system + "）";
      tr.appendChild(name);
      tr.appendChild(node("td", m.side === "left" ? "左" : m.side === "right" ? "右" : "—"));
      tr.appendChild(node("td", m.display, "mono"));
      tr.appendChild(statusCell(m.status));
      const why = document.createElement("td");
      const ul = node("ul", null, "reasons");
      for (const r of m.reasons || []) ul.appendChild(node("li", r));
      for (const r of m.limitations || []) ul.appendChild(node("li", r, "limit"));
      if (m.apparent_video_value !== null && m.apparent_video_value !== undefined) {
        ul.appendChild(node("li", "動画上の見かけの値: " + m.apparent_video_value + "（撮影実時間ではない）", "limit"));
      }
      why.appendChild(ul);
      tr.appendChild(why);
      const ev = document.createElement("td");
      const ids = (m.evidence_refs && m.evidence_refs.frame_ids) || [];
      if (ids.length) ev.appendChild(jumpButton("frame " + ids[ids.length > 1 ? 1 : 0], ids[ids.length > 1 ? 1 : 0]));
      tr.appendChild(ev);
      el.metricsBody.appendChild(tr);
    }
  }

  // ---------- 再生 ----------
  function stop() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    state = PC.setPlaying(state, false);
  }

  function schedule() {
    const delay = PC.nextDelayMs(model, state);
    const wait = delay === null ? (state.loop ? 400 : null) : delay;
    if (wait === null) { stop(); render(); return; }
    timer = setTimeout(() => {
      state = PC.tick(model, state);
      render();
      if (state.playing) schedule();
    }, wait);
  }

  function togglePlay() {
    if (!model) return;
    if (state.playing) { stop(); render(); return; }
    if (PC.nextDelayMs(model, state) === null && !state.loop) state = PC.step(model, state, -state.position);
    state = PC.setPlaying(state, true);
    render();
    schedule();
  }

  function stepBy(d) {
    if (!model) return;
    stop();
    state = PC.step(model, state, d);
    render();
  }

  // ---------- 読み込み ----------
  function load(snapshot) {
    stop();
    try {
      model = PC.loadSnapshot(snapshot);
    } catch (err) {
      showError("読み込めませんでした: " + err.message);
      return;
    }
    showError("");
    state = PC.setRate(PC.createState(), Number(el.rate.value) || 1);
    el.scrub.max = String(model.frames.length - 1);
    renderQuality(); renderEvents(); renderMetrics(); render();
  }

  async function loadDemo(file) {
    try {
      const res = await fetch("demo/" + encodeURIComponent(file));
      if (!res.ok) throw new Error("HTTP " + res.status);
      load(await res.json());
    } catch (err) {
      showError("デモを読み込めませんでした（" + err.message + "）。web フォルダをHTTPで配信して開いてください。");
    }
  }

  async function init() {
    for (const r of PC.RATES) {
      const o = node("option", "×" + r);
      o.value = String(r);
      el.rate.appendChild(o);
    }
    el.rate.addEventListener("change", () => {
      state = PC.setRate(state, Number(el.rate.value));
      render();
    });
    el.play.addEventListener("click", togglePlay);
    el.prev.addEventListener("click", () => stepBy(-1));
    el.next.addEventListener("click", () => stepBy(1));
    el.unloop.addEventListener("click", () => { state = PC.clearLoop(state); render(); });
    el.scrub.addEventListener("input", () => {
      if (!model) return;
      stop();
      state = PC.step(model, PC.clearLoop(state), Number(el.scrub.value) - state.position);
      render();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.target instanceof HTMLInputElement && ev.target.type !== "range") return;
      if (ev.target instanceof HTMLSelectElement) return;
      if (ev.key === "ArrowRight") { ev.preventDefault(); stepBy(1); }
      else if (ev.key === "ArrowLeft") { ev.preventDefault(); stepBy(-1); }
      else if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
    });
    el.file.addEventListener("change", async () => {
      const f = el.file.files && el.file.files[0];
      if (!f) return;
      try {
        load(JSON.parse(await f.text()));
      } catch (err) {
        showError("JSONとして読めませんでした: " + err.message);
      }
    });
    el.demo.addEventListener("change", () => loadDemo(el.demo.value));
    try {
      const res = await fetch("demo/index.json");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const catalog = await res.json();
      for (const item of catalog) {
        const o = node("option", item.label);
        o.value = item.file;
        el.demo.appendChild(o);
      }
      if (catalog.length) loadDemo(catalog[0].file);
    } catch (err) {
      showError("デモ一覧を読み込めませんでした。右上から解析結果JSONを開いてください。");
    }
  }

  init();
})();
