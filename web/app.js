/* フォーム解析アプリの画面。計算は player-core.js とサーバーに置き、ここは表示と操作だけにする。
 * 解析データやファイル名など外から来た文字列は textContent でのみ挿入する（HTMLとして解釈させない）。 */
(function () {
  "use strict";
  const PC = window.PlayerCore;
  const $ = (id) => document.getElementById(id);

  // ---------- 共通 ----------
  function node(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function banner(msg) { const b = $("banner"); b.textContent = msg || ""; b.hidden = !msg; }
  function button(label, onClick, cls) {
    const b = node("button", label, cls);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  async function api(method, path, body) {
    const opt = { method: method, headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(path, opt);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  let catalog = null;
  async function loadCatalog() {
    if (!catalog) catalog = await api("GET", "/api/disciplines");
    return catalog;
  }
  function disciplineName(id) {
    const d = catalog && catalog.disciplines.find((x) => x.discipline_id === id);
    return d ? d.name : id;
  }
  function fillDisciplines(select, withAll) {
    clear(select);
    if (withAll) { const o = node("option", "すべて"); o.value = ""; select.appendChild(o); }
    const groups = {};
    const catLabel = {
      sprint: "短距離", middle_distance: "中距離", long_distance: "長距離", hurdles: "ハードル", steeplechase: "障害",
      horizontal_jump: "跳躍（幅）", vertical_jump: "跳躍（高さ）", throw: "投擲", race_walk: "競歩", relay: "リレー",
    };
    for (const d of catalog.disciplines) {
      if (!groups[d.category]) {
        groups[d.category] = document.createElement("optgroup");
        groups[d.category].label = catLabel[d.category] || d.category;
        select.appendChild(groups[d.category]);
      }
      const o = node("option", d.name);
      o.value = d.discipline_id;
      groups[d.category].appendChild(o);
    }
  }
  function disciplineNote(id) {
    const d = catalog.disciplines.find((x) => x.discipline_id === id);
    if (!d) return "";
    const st = catalog.status_labels;
    const parts = Object.keys(d.metric_status).map((m) => metricName(m) + ": " + (st[d.metric_status[m]] || d.metric_status[m]));
    const notes = d.notes.length ? " / " + d.notes.join(" / ") : "";
    return "この種目の指標 — " + parts.join("、") + notes;
  }
  const METRIC_NAMES = {
    contact_time: "接地時間", cadence: "ピッチ",
    knee_angle_at_contact: "接地時の膝角度（2D）", trunk_lean_at_contact: "接地時の体幹前傾（2D）",
  };
  function metricName(id) { return METRIC_NAMES[id] || id; }
  const JOB_LABEL = {
    queued: "待機中", probing: "時刻の読み取り", inferring: "姿勢推定", analyzing: "計算",
    completed: "完了", failed: "失敗", cancel_requested: "取り消し中", cancelled: "取り消し",
  };

  // ---------- ルーティング ----------
  let pollTimer = null;
  function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

  async function route() {
    stopPolling();
    stopPlayback();
    banner("");
    const h = location.hash || "#/";
    for (const v of ["view-home", "view-video", "view-compare"]) $(v).hidden = true;
    try {
      await loadCatalog();
      const m = /^#\/video\/([a-z0-9]{12})$/.exec(h);
      if (m) { $("view-video").hidden = false; await openVideo(m[1]); }
      else if (h === "#/compare") { $("view-compare").hidden = false; await openCompare(); }
      else { $("view-home").hidden = false; await openHome(); }
    } catch (err) {
      banner("読み込めませんでした: " + err.message);
    }
  }

  // ---------- 一覧・アップロード ----------
  let homeReady = false;
  async function openHome() {
    if (!homeReady) {
      fillDisciplines($("up-discipline"), false);
      fillDisciplines($("f-discipline"), true);
      $("up-discipline").addEventListener("change", () => { $("up-discipline-note").textContent = disciplineNote($("up-discipline").value); });
      $("up-discipline-note").textContent = disciplineNote($("up-discipline").value);
      $("up-date").value = new Date().toISOString().slice(0, 10);
      $("upload-form").addEventListener("submit", (e) => { e.preventDefault(); upload(); });
      $("filter-form").addEventListener("submit", (e) => { e.preventDefault(); refreshList(); });
      homeReady = true;
    }
    await refreshList();
  }

  async function refreshList() {
    const p = new URLSearchParams();
    if ($("f-q").value) p.set("q", $("f-q").value);
    if ($("f-discipline").value) p.set("discipline", $("f-discipline").value);
    if ($("f-from").value) p.set("from", $("f-from").value);
    if ($("f-to").value) p.set("to", $("f-to").value);
    if ($("f-tag").value) p.set("tag", $("f-tag").value);
    const data = await api("GET", "/api/videos?" + p.toString());
    const body = $("video-list");
    clear(body);
    $("list-empty").hidden = data.videos.length > 0;
    for (const v of data.videos) {
      const tr = document.createElement("tr");
      tr.appendChild(node("td", v.practice_date || v.created_at.slice(0, 10)));
      tr.appendChild(node("td", disciplineName(v.discipline)));
      tr.appendChild(node("td", v.filename));
      tr.appendChild(node("td", v.tags || ""));
      tr.appendChild(node("td", v.latest_analysis ? "あり" : "なし", "status status-" + (v.latest_analysis ? "completed" : "none")));
      const td = document.createElement("td");
      const a = node("a", "開く", "button");
      a.href = "#/video/" + v.id;
      td.appendChild(a);
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }

  function upload() {
    const f = $("up-file").files[0];
    if (!f) return;
    const meta = {
      discipline: $("up-discipline").value, camera_view: $("up-view").value, direction: $("up-direction").value,
      practice_date: $("up-date").value || null, place: $("up-place").value, record: $("up-record").value,
      tags: $("up-tags").value, notes: $("up-notes").value, declared_capture_fps: $("up-fps").value.trim(),
      declared_slowmo: $("up-slowmo").checked,
    };
    // 進み具合を出すため XMLHttpRequest で送る（fetch では送信の進み具合が取れない）
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos");
    xhr.setRequestHeader("X-Athform-Upload", "1");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(f.name));
    xhr.setRequestHeader("X-Athform-Meta", encodeURIComponent(JSON.stringify(meta)));
    $("up-submit").disabled = true;
    $("up-progress").hidden = false;
    $("up-status").textContent = "送信中…";
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) $("up-progress").value = e.loaded / e.total; };
    xhr.onerror = () => { $("up-submit").disabled = false; $("up-status").textContent = "送信できませんでした"; };
    xhr.onload = async () => {
      $("up-submit").disabled = false;
      $("up-progress").hidden = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = {}; }
      if (xhr.status !== 201) { $("up-status").textContent = "追加できませんでした: " + (data.error || xhr.status); return; }
      $("up-status").textContent = "";
      $("upload-form").reset();
      $("up-date").value = new Date().toISOString().slice(0, 10);
      try { await api("POST", "/api/videos/" + data.video.id + "/jobs", { kind: "full", mode: "performance" }); }
      catch (err) { banner("解析を開始できませんでした: " + err.message); }
      location.hash = "#/video/" + data.video.id;
    };
    xhr.send(f);
  }

  // ---------- 解析画面 ----------
  let current = null;      // { video, job, analyses, corrections }
  let model = null;
  let state = PC.createState();
  let timer = null;
  let selectedEvent = null;
  let picking = false;
  const images = new Map();
  let videoReady = false;

  function setupVideoView() {
    if (videoReady) return;
    videoReady = true;
    fillDisciplines($("m-discipline"), false);
    for (const r of PC.RATES) { const o = node("option", "×" + r); o.value = String(r); $("rate-select").appendChild(o); }
    $("rate-select").addEventListener("change", () => { state = PC.setRate(state, Number($("rate-select").value)); render(); });
    $("btn-play").addEventListener("click", togglePlay);
    $("btn-prev").addEventListener("click", () => stepBy(-1));
    $("btn-next").addEventListener("click", () => stepBy(1));
    $("btn-unloop").addEventListener("click", () => { state = PC.clearLoop(state); render(); });
    $("show-skeleton").addEventListener("change", render);
    $("show-ground").addEventListener("change", render);
    $("scrubber").addEventListener("input", () => {
      if (!model) return;
      stopPlayback();
      state = PC.step(model, PC.clearLoop(state), Number($("scrubber").value) - state.position);
      render();
    });
    document.addEventListener("keydown", (ev) => {
      if ($("view-video").hidden || !model) return;
      const t = ev.target;
      if (t instanceof HTMLInputElement && t.type !== "range" && t.type !== "checkbox") return;
      if (t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement) return;
      if (ev.key === "ArrowRight") { ev.preventDefault(); stepBy(1); }
      else if (ev.key === "ArrowLeft") { ev.preventDefault(); stepBy(-1); }
      else if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
    });
    $("run-performance").addEventListener("click", () => startJob("full", "performance"));
    $("run-balanced").addEventListener("click", () => startJob("full", "balanced"));
    $("rerun").addEventListener("click", () => {
      if (confirm("姿勢推定からやり直します（時間がかかります）。手動訂正は残ります。")) startJob("full", "performance");
    });
    $("job-cancel").addEventListener("click", async () => {
      if (current && current.job) { await api("POST", "/api/jobs/" + current.job.id + "/cancel", {}); pollJob(); }
    });
    $("meta-form").addEventListener("submit", (e) => { e.preventDefault(); saveMeta(); });
    $("delete-video").addEventListener("click", deleteVideo);
    $("c-add").addEventListener("click", () => {
      correct({ action: "add", event_type: $("c-type").value, side: $("c-side").value,
                after_frame_id: PC.view(model, state).frameId });
    });
    $("ground-pick").addEventListener("click", () => {
      picking = !picking;
      $("ground-pick").setAttribute("aria-pressed", String(picking));
      $("stage").classList.toggle("picking", picking);
    });
    $("ground-reset").addEventListener("click", () => saveGround(null));
    $("stage").addEventListener("click", (ev) => {
      if (!picking || !model) return;
      const rect = $("stage").getBoundingClientRect();
      const size = PC.imageSize(model);
      const x = (ev.clientX - rect.left) / rect.width * size.width;
      const y = (ev.clientY - rect.top) / rect.height * size.height;
      picking = false;
      $("ground-pick").setAttribute("aria-pressed", "false");
      $("stage").classList.remove("picking");
      const band = (model.snapshot.detector && model.snapshot.detector.contact_band_px) || 6;
      saveGround({ ground_y: y, ground_x0: x, ground_slope: 0, contact_band_px: band });
    });
  }

  async function openVideo(id) {
    setupVideoView();
    current = await api("GET", "/api/videos/" + id);
    const v = current.video;
    $("v-title").textContent = disciplineName(v.discipline) + " — " + v.filename + (v.practice_date ? "（" + v.practice_date + "）" : "");
    $("version-note").hidden = true;
    $("export-link").href = "/api/videos/" + id + "/export";
    fillMeta(v);
    renderHistory();
    await loadAnalysis();
    showJob();
  }

  function fillMeta(v) {
    $("m-discipline").value = v.discipline;
    $("m-view").value = v.camera_view || "side";
    $("m-direction").value = v.direction || "left_to_right";
    $("m-date").value = v.practice_date || "";
    $("m-place").value = v.place || "";
    $("m-record").value = v.record || "";
    $("m-tags").value = v.tags || "";
    $("m-fps").value = v.declared_capture_fps || "";
    $("m-slowmo").checked = !!v.declared_slowmo;
    $("m-notes").value = v.notes || "";
    const g = v.ground_override ? JSON.parse(v.ground_override) : null;
    $("ground-text").textContent = g
      ? "手動で指定: 高さ " + g.ground_y.toFixed(0) + " px（接地とみなす幅 " + (g.contact_band_px || 6).toFixed(0) + " px）"
      : "足の最下点から自動で推定しています。カメラが動く動画では外れることがあります。";
  }

  let viewingLatest = true;
  async function loadAnalysis(revision) {
    let snap = null;
    try {
      snap = await api("GET", "/api/videos/" + current.video.id + "/analysis" + (revision ? "?revision=" + revision : ""));
    } catch (err) { snap = null; }
    const latest = current.analyses.length ? current.analyses[0].revision : null;
    viewingLatest = !snap || !latest || snap.revision === latest;
    $("no-analysis").hidden = !!snap;
    $("player").hidden = !snap;
    if (!snap) { model = null; renderEmptyPanels(); return; }
    model = PC.loadSnapshot(snap);
    state = PC.setRate(PC.createState(), Number($("rate-select").value) || 1);
    images.clear();
    const size = PC.imageSize(model);
    $("stage").width = size.width;
    $("stage").height = size.height;
    $("scrubber").max = String(model.frames.length - 1);
    $("v-preview").hidden = !snap.preview;
    $("v-preview").textContent = snap.preview ? "プレビュー（正式な計測ではない）" : "";
    renderTitle();
    renderQuality(); renderEvents(); renderMetrics(); renderCorrections(); render();
  }

  function renderTitle() {
    const s = model.snapshot, v = current.video;
    const name = (s.discipline && s.discipline.name) || disciplineName(s.capture_conditions.discipline);
    $("v-title").textContent = name + " — " + v.filename + (v.practice_date ? "（" + v.practice_date + "）" : "") +
      "　版 " + (s.revision || "?");
    const notes = [];
    if (!viewingLatest) notes.push("過去の版を表示しています。訂正は最新の版にだけできます。");
    if (s.capture_conditions.discipline !== v.discipline || (s.capture_conditions.camera_view || "") !== (v.camera_view || "")) {
      notes.push("動画の情報（種目・撮影角度）が、この版を解析したときと違います。最新の情報で計算し直すには「保存して再計算」を押してください。");
    }
    $("version-note").textContent = notes.join(" ");
    $("version-note").hidden = !notes.length;
    for (const id of ["c-add", "c-reason"]) $(id).disabled = !viewingLatest;
  }

  function renderCorrections() {
    const ul = $("corrections-list");
    clear(ul);
    const unapplied = new Map(((model.snapshot.events || {}).unapplied_corrections || []).map((u) => [u.index, u.reason]));
    // 最新の版では保存中の訂正（取り消しボタン付き）、過去の版ではその版を作ったときの訂正を表示する（再レビュー N5）
    const list = viewingLatest ? (current.corrections || []) : ((model.snapshot.events || {}).corrections || []);
    const label = { move: "移動", add: "追加", delete: "削除" };
    list.forEach((c, i) => {
      const li = node("li");
      const what = (label[c.action] || c.action) + "　" + (c.event_id || ((c.side === "left" ? "左" : "右") + (c.event_type === "toe_off" ? "離地" : "接地"))) +
        (c.after_frame_id !== null && c.after_frame_id !== undefined ? " → frame " + c.after_frame_id : "") + "　理由: " + c.reason;
      let state = "適用";
      if (c.revoked_at) state = "取り消し済み（" + (c.revoked_reason || "") + "）";
      else if (unapplied.has(i)) state = "適用できなかった: " + unapplied.get(i);
      li.appendChild(node("span", what + "　［" + state + "］"));
      if (!c.revoked_at && viewingLatest) {
        li.appendChild(document.createTextNode(" "));
        li.appendChild(button("取り消す", () => revokeCorrection(i)));
      }
      ul.appendChild(li);
    });
    if (!list.length) ul.appendChild(node("li", "まだありません"));
  }

  async function revokeCorrection(i) {
    const reason = $("c-reason").value.trim();
    if (!reason) { $("c-status").textContent = "取り消しの理由を「訂正の理由」に入力してください"; $("c-reason").focus(); return; }
    try {
      const snap = await api("POST", "/api/videos/" + current.video.id + "/corrections/" + i + "/revoke", { reason: reason });
      await afterCorrection(snap);
      $("c-status").textContent = "訂正を取り消し、計算し直しました";
    } catch (err) { $("c-status").textContent = "取り消せませんでした: " + err.message; }
  }

  function renderEmptyPanels() {
    for (const id of ["quality-list", "quality-notes", "events-body", "metrics-body", "unsupported-list"]) clear($(id));
    $("feedback-text").textContent = "";
  }

  function renderHistory() {
    const ul = $("history-list");
    clear(ul);
    for (const a of current.analyses) {
      const li = node("li");
      const b = button("版 " + a.revision + "（" + a.created_at.replace("T", " ").slice(0, 16) + "、" + a.mode + "）",
                       () => loadAnalysis(a.revision));
      li.appendChild(b);
      ul.appendChild(li);
    }
    if (!current.analyses.length) ul.appendChild(node("li", "まだありません"));
  }

  // ---------- ジョブ ----------
  async function startJob(kind, mode) {
    try {
      const r = await api("POST", "/api/videos/" + current.video.id + "/jobs", { kind: kind, mode: mode });
      current.job = r.job;
      showJob();
    } catch (err) { banner(err.message); }
  }

  function showJob() {
    const j = current && current.job;
    const active = j && ["queued", "probing", "inferring", "analyzing", "cancel_requested"].includes(j.state);
    $("job-box").hidden = !j || (!active && j.state === "completed");
    if (j) {
      $("job-text").textContent = (JOB_LABEL[j.state] || j.state) + (j.message ? "：" + j.message : "") + (j.error ? "（" + j.error + "）" : "");
      $("job-progress").value = j.progress || 0;
      $("job-cancel").hidden = !active;
    }
    if (j && j.state === "failed") {
      $("no-analysis-text").textContent = "解析できませんでした: " + (j.error || "") + "。撮影条件を確かめて、もう一度試してください。";
    } else if (active) {
      $("no-analysis-text").textContent = "解析中です。この画面を閉じても解析は続きます。";
    } else {
      $("no-analysis-text").textContent = "まだ解析していません。";
    }
    $("run-performance").disabled = !!active;
    $("run-balanced").disabled = !!active;
    if (active) pollJob();
  }

  function pollJob() {
    stopPolling();
    pollTimer = setTimeout(async () => {
      if (!current || !current.job) return;
      try {
        const r = await api("GET", "/api/jobs/" + current.job.id);
        const was = current.job.state;
        current.job = r.job;
        if (r.job.state === "completed" && was !== "completed") {
          await openVideo(current.video.id);
          return;
        }
        showJob();
      } catch (err) { banner(err.message); }
    }, 1000);
  }

  async function saveMeta() {
    const fields = {
      discipline: $("m-discipline").value, camera_view: $("m-view").value, direction: $("m-direction").value,
      practice_date: $("m-date").value || null, place: $("m-place").value, record: $("m-record").value,
      tags: $("m-tags").value, notes: $("m-notes").value, declared_capture_fps: $("m-fps").value.trim(),
      declared_slowmo: $("m-slowmo").checked,
    };
    try {
      await api("PATCH", "/api/videos/" + current.video.id, fields);
      $("m-status").textContent = "保存しました";
      if (current.analyses.length) await startJob("reanalyze", "performance");
      else await openVideo(current.video.id);
    } catch (err) { $("m-status").textContent = "保存できませんでした: " + err.message; }
  }

  async function saveGround(g) {
    try {
      await api("PATCH", "/api/videos/" + current.video.id, { ground_override: g });
      await startJob("reanalyze", "performance");
    } catch (err) { banner(err.message); }
  }

  async function deleteVideo() {
    if (!confirm("この動画と、解析結果・訂正・フレーム画像をすべて削除します。元に戻せません。")) return;
    try {
      await api("DELETE", "/api/videos/" + current.video.id, {});
      location.hash = "#/";
    } catch (err) { banner(err.message); }
  }

  async function correct(body) {
    const reason = $("c-reason").value.trim();
    if (!reason) { $("c-status").textContent = "訂正の理由を入力してください"; $("c-reason").focus(); return; }
    body.reason = reason;
    $("c-status").textContent = "再計算中…";
    if (!viewingLatest) { $("c-status").textContent = "過去の版は訂正できません"; return; }
    try {
      const snap = await api("POST", "/api/videos/" + current.video.id + "/corrections", body);
      await afterCorrection(snap);
      $("c-status").textContent = "訂正を保存し、依存する指標を計算し直しました";
    } catch (err) { $("c-status").textContent = "訂正できませんでした: " + err.message; }
  }

  async function afterCorrection(snap) {
    const keep = PC.view(model, state).frameId;
    current = await api("GET", "/api/videos/" + current.video.id);
    renderHistory();
    viewingLatest = true;
    model = PC.loadSnapshot(snap);
    state = PC.seekFrameId(model, PC.setRate(PC.createState(), state.rate), keep);
    selectedEvent = null;
    renderTitle();
    renderQuality(); renderEvents(); renderMetrics(); renderCorrections(); render();
  }

  // ---------- 描画 ----------
  const BONES = [
    ["left_shoulder", "right_shoulder"], ["left_hip", "right_hip"], ["left_shoulder", "left_hip"], ["right_shoulder", "right_hip"],
    ["left_shoulder", "left_elbow"], ["left_elbow", "left_wrist"], ["right_shoulder", "right_elbow"], ["right_elbow", "right_wrist"],
    ["left_hip", "left_knee"], ["left_knee", "left_ankle"], ["left_ankle", "left_heel"], ["left_ankle", "left_toe"],
    ["right_hip", "right_knee"], ["right_knee", "right_ankle"], ["right_ankle", "right_heel"], ["right_ankle", "right_toe"],
  ];
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function imageFor(frameId) {
    const url = PC.frameImageUrl(model, frameId);
    if (!url) return null;
    let img = images.get(frameId);
    if (!img) {
      img = new Image();
      img.onload = () => { if (model && PC.view(model, state).frameId === frameId) render(); };
      img.src = url;
      images.set(frameId, img);
      if (images.size > 400) images.delete(images.keys().next().value);
    }
    return img;
  }

  /** 骨格の点が1つでもあるか（人物を検出できなかったフレームは、点がすべて空の記録になる）。 */
  function hasSkeleton(v) {
    return !!(v.overlay && v.overlay.keypoints && Object.values(v.overlay.keypoints).some(Boolean));
  }

  function drawStage(v) {
    const cv = $("stage");
    const ctx = cv.getContext("2d");
    const w = cv.width, h = cv.height;
    ctx.fillStyle = cssVar("--stage");
    ctx.fillRect(0, 0, w, h);
    const img = imageFor(v.frameId);
    if (img && img.complete && img.naturalWidth) ctx.drawImage(img, 0, 0, w, h);
    for (const id of PC.prefetchIds(model, state, 8)) imageFor(id);
    const lw = Math.max(2, w / 400);
    if ($("show-ground").checked) {
      const y0 = PC.groundYAt(model, 0), y1 = PC.groundYAt(model, w);
      if (y0 !== null) {
        ctx.strokeStyle = cssVar("--ground"); ctx.lineWidth = lw; ctx.setLineDash([lw * 4, lw * 3]);
        ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(w, y1); ctx.stroke(); ctx.setLineDash([]);
      }
    }
    if ($("show-skeleton").checked && hasSkeleton(v)) {
      const kp = v.overlay.keypoints || {};
      ctx.lineWidth = lw * 1.5;
      for (const [a, b] of BONES) {
        const p = kp[a], q = kp[b];
        if (!p || !q) continue;
        ctx.strokeStyle = a.startsWith("left") && b.startsWith("left") ? cssVar("--joint") : cssVar("--bone");
        ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
      }
      for (const name of Object.keys(kp)) {
        const p = kp[name];
        if (!p) continue;
        ctx.fillStyle = p[2] >= 0.3 ? cssVar("--joint") : cssVar("--review");
        ctx.beginPath(); ctx.arc(p[0], p[1], lw * 2.2, 0, Math.PI * 2); ctx.fill();
      }
    }
    if (!hasSkeleton(v)) {
      ctx.fillStyle = "#fff"; ctx.font = Math.round(w / 40) + "px system-ui, sans-serif";
      ctx.fillText("このフレームでは対象の人物を検出できませんでした", w * 0.03, h * 0.08);
    }
  }

  function render() {
    if (!model) return;
    const v = PC.view(model, state);
    drawStage(v);
    $("ro-frame").textContent = v.frameId + "  (" + (v.position + 1) + " / " + v.frameCount + ")";
    $("ro-pts").textContent = v.pts + " × " + v.timeBase;
    $("ro-time").textContent = PC.formatSeconds(v.sourceTime) + "  [" + v.sourceTimeExact + "]";
    $("ro-elapsed").textContent = (v.playbackElapsedMs / 1000).toFixed(3) + " s（×" + v.rate + "）";
    $("ro-overlay").textContent = !hasSkeleton(v) ? "なし（人物を検出できない）" : String(v.overlayFrameId);
    const evs = PC.eventsAtFrame(model, v.frameId);
    $("ro-events").textContent = evs.length ? evs.map((e) => (e.side === "left" ? "左" : "右") + (e.event_type === "foot_contact" ? "接地" : "離地")).join("、") : "—";
    $("scrubber").value = String(v.position);
    $("btn-play").textContent = state.playing ? "一時停止" : "再生";
    $("btn-unloop").hidden = !state.loop;
    for (const tr of $("events-body").querySelectorAll("tr")) {
      tr.classList.toggle("current", (tr.dataset.frames || "").split(",").includes(String(v.frameId)));
    }
  }

  function renderQuality() {
    clear($("quality-list")); clear($("quality-notes"));
    const s = model.snapshot, q = s.capture_quality || {}, c = s.frame_completeness || {}, iv = q.interval_s || {};
    const fmt = (x) => (typeof x === "number" ? (x * 1000).toFixed(2) + " ms" : "—");
    const rows = [
      ["撮影時間軸", PC.CAPTURE_LABEL[q.capture_time_status] || q.capture_time_status],
      ["公称FPS", q.nominal_fps === null || q.nominal_fps === undefined ? "—" : q.nominal_fps],
      ["撮影FPS", q.capture_fps === null || q.capture_fps === undefined ? "不明" : q.capture_fps],
      ["フレーム間隔", fmt(iv.min) + " / " + fmt(iv.median) + " / " + fmt(iv.max) + "（最小/中央/最大）"],
      ["可変FPS", q.vfr ? "あり" : "なし"],
      ["欠落の疑い", (q.gap_frame_ids || []).length + " 箇所"],
      ["復号失敗", c.decode_check === "packets" ? (c.decode_failed_source_frame_ids || []).length + " 枚" : "確認できない"],
      ["観測の途切れ", ((s.events && s.events.observation_breaks) || []).length + " 箇所（またいだ指標は出さない）"],
      ["解析フレーム", (c.analyzed_frame_count ?? "—") + " / " + (c.decoded_frame_count ?? "—")],
      ["人物を検出できない", (c.unobservable_frame_ids || []).length + " フレーム（取り直し " + (c.reacquired_frame_ids || []).length + " 回）"],
      ["地面の線", groundLabel(s.ground_estimate) + ((s.ground_quality && s.ground_quality.problems && s.ground_quality.problems.length) ? "／問題: " + s.ground_quality.problems.join("、") : "")],
      ["モデル", s.model ? s.model.weights_version : "—"],
      ["モード", s.preview ? "プレビュー（" + (s.preview_reason || "") + "）" : "精度優先・全原本フレーム"],
      ["解析ID", (s.analysis_id || "").slice(0, 12) + (s.revision ? "（版 " + s.revision + "）" : "")],
    ];
    for (const [k, v] of rows) { $("quality-list").appendChild(node("dt", k)); $("quality-list").appendChild(node("dd", v)); }
    for (const n of q.capture_time_notes || []) $("quality-notes").appendChild(node("li", n));
  }

  function groundLabel(g) {
    if (!g) return "—";
    if (g.method === "user_override") return "手動で指定";
    if (g.method === "unavailable") return "推定できない（" + (g.reason || "") + "）";
    return "自動推定（足の最下点 " + (g.foot_points || 0) + " 点から）";
  }

  function statusCell(status) {
    return node("td", PC.STATUS_LABEL[status] || status, "status status-" + status);
  }

  function seek(frameId) {
    stopPlayback();
    state = PC.seekFrameId(model, state, frameId);
    render();
  }

  function renderEvents() {
    const body = $("events-body");
    clear(body);
    const typeLabel = { foot_contact: "接地", toe_off: "離地" };
    for (const e of model.events) {
      const tr = document.createElement("tr");
      tr.dataset.frames = (e.source_frame_ids || []).join(",");
      if (selectedEvent === e.event_id) tr.classList.add("selected");
      tr.appendChild(node("td", (typeLabel[e.event_type] || e.event_type) + (e.source === "manual" ? "（手動）" : "")));
      tr.appendChild(node("td", e.side === "left" ? "左" : "右"));
      const iv = e.time_interval_sec || {};
      tr.appendChild(node("td", PC.formatSeconds(iv.lower, 4) + " 〜 " + PC.formatSeconds(iv.upper, 4), "mono"));
      tr.appendChild(node("td", (e.source_frame_ids || []).join(" → "), "mono"));
      const st = statusCell(e.quality_status === "valid" ? "valid" : "review_required");
      if (e.quality_reasons && e.quality_reasons.length) st.title = e.quality_reasons.join(" / ");
      tr.appendChild(st);
      const td = document.createElement("td");
      td.appendChild(button("移動", () => seek(e.evidence_frame_id)));
      td.appendChild(button("前後をループ", () => {
        stopPlayback();
        state = PC.setRate(PC.loopAroundEvent(model, state, e, 6), Math.min(state.rate, 0.25));
        $("rate-select").value = String(state.rate);
        render();
      }));
      if (viewingLatest) {
        td.appendChild(button("表示中のフレームへ移す", () => correct({ action: "move", event_id: e.event_id,
                                                                          after_frame_id: PC.view(model, state).frameId })));
        td.appendChild(button("削除", () => correct({ action: "delete", event_id: e.event_id }), "danger"));
      }
      tr.appendChild(td);
      tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "BUTTON") { selectedEvent = e.event_id; renderEvents(); } });
      body.appendChild(tr);
    }
    if (!model.events.length) {
      const tr = document.createElement("tr");
      const td = node("td", "接地・離地を検出できませんでした。地面の線の位置や撮影角度を確かめてください。");
      td.colSpan = 6;
      tr.appendChild(td);
      body.appendChild(tr);
    }
  }

  function renderMetrics() {
    const body = $("metrics-body");
    clear(body);
    for (const m of model.metrics) {
      const tr = document.createElement("tr");
      const name = node("td", metricName(m.metric_id));
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
      if (ids.length) {
        const target = ids[ids.length > 1 ? 1 : 0];
        if (model.positionById.has(target)) ev.appendChild(button("frame " + target, () => seek(target)));
      }
      tr.appendChild(ev);
      body.appendChild(tr);
    }
    const ul = $("unsupported-list");
    clear(ul);
    const d = model.snapshot.discipline || {};
    const un = d.unsupported_metrics || {};
    for (const k of Object.keys(un)) ul.appendChild(node("li", k + " — " + un[k]));
    if (!Object.keys(un).length) ul.appendChild(node("li", "なし"));
    $("feedback-text").textContent = (model.snapshot.feedback && model.snapshot.feedback.reason) || "";
  }

  // ---------- 再生 ----------
  function stopPlayback() {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    if (state.playing) state = PC.setPlaying(state, false);
  }
  function schedule() {
    const delay = PC.nextDelayMs(model, state);
    const wait = delay === null ? (state.loop ? 400 : null) : delay;
    if (wait === null) { stopPlayback(); render(); return; }
    timer = setTimeout(() => { state = PC.tick(model, state); render(); if (state.playing) schedule(); }, wait);
  }
  function togglePlay() {
    if (!model) return;
    if (state.playing) { stopPlayback(); render(); return; }
    if (PC.nextDelayMs(model, state) === null && !state.loop) state = PC.step(model, state, -state.position);
    state = PC.setPlaying(state, true);
    render();
    schedule();
  }
  function stepBy(d) { if (!model) return; stopPlayback(); state = PC.step(model, state, d); render(); }

  // ---------- 比較 ----------
  let compareReady = false;
  async function openCompare() {
    const data = await api("GET", "/api/videos");
    const withAnalysis = data.videos.filter((v) => v.latest_analysis);
    for (const [id, blank] of [["cmp-a", false], ["cmp-b", true]]) {
      const sel = $(id);
      clear(sel);
      if (blank) { const o = node("option", "（A と同じ動画）"); o.value = ""; sel.appendChild(o); }
      for (const v of withAnalysis) {
        const o = node("option", (v.practice_date || "") + " " + disciplineName(v.discipline) + " " + v.filename);
        o.value = v.id;
        sel.appendChild(o);
      }
    }
    if (!compareReady) {
      $("compare-form").addEventListener("submit", (e) => { e.preventDefault(); runCompare(); });
      compareReady = true;
    }
    if (!withAnalysis.length) banner("解析済みの動画がまだありません。");
  }

  async function runCompare() {
    const p = new URLSearchParams();
    p.set("a", $("cmp-a").value);
    if ($("cmp-b").value) p.set("b", $("cmp-b").value);
    if ($("cmp-ra").value.trim()) p.set("ra", $("cmp-ra").value.trim());
    if ($("cmp-rb").value.trim()) p.set("rb", $("cmp-rb").value.trim());
    try {
      const r = await api("GET", "/api/compare?" + p.toString());
      $("cmp-result").hidden = false;
      clear($("cmp-blocking")); clear($("cmp-warnings")); clear($("cmp-body"));
      for (const b of r.compatibility.blocking) $("cmp-blocking").appendChild(node("li", "比べられない: " + b));
      for (const w of r.compatibility.warnings) $("cmp-warnings").appendChild(node("li", w));
      const fmt = (s) => (s ? (+s.median.toFixed(3)) + " " + s.unit + "（" + s.n + "件" + (s.excluded ? "、除外 " + s.excluded : "") + "）" : "—");
      for (const row of r.rows) {
        const tr = document.createElement("tr");
        tr.appendChild(node("td", metricName(row.metric_id)));
        tr.appendChild(node("td", fmt(row.a)));
        tr.appendChild(node("td", fmt(row.b)));
        tr.appendChild(node("td", row.diff === null ? "—" : (+row.diff.toFixed(3)).toString(), "mono"));
        tr.appendChild(node("td", row.note));
        $("cmp-body").appendChild(tr);
      }
    } catch (err) { banner("比べられませんでした: " + err.message); }
  }

  window.addEventListener("hashchange", route);
  route();
})();
