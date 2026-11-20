/* フォームノートの画面。計算は player-core.js とサーバーに置き、ここは表示と操作だけにする。
 * 解析データやファイル名など外から来た文字列は textContent でのみ挿入する（HTMLとして解釈させない）。 */
(function () {
  "use strict";
  const PC = window.PlayerCore;
  const $ = (id) => document.getElementById(id);

  // ================= 共通 =================
  function node(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function button(label, onClick, cls) {
    const b = node("button", label, cls);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }
  let toastTimer = null;
  function toast(msg, isError) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("error", !!isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3500);
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

  /** ラジオ風のセグメント（[data-value] のボタン群）。 */
  function segValue(id) {
    const b = $(id).querySelector('[aria-checked="true"]');
    return b ? b.dataset.value : null;
  }
  function setSeg(id, value) {
    for (const b of $(id).querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.value === value));
  }
  function wireSeg(id, onChange) {
    $(id).addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b || !b.dataset.value) return;
      setSeg(id, b.dataset.value);
      if (onChange) onChange(b.dataset.value);
    });
  }

  let catalog = null;
  async function loadCatalog() {
    if (!catalog) catalog = await api("GET", "/api/disciplines");
    return catalog;
  }
  function discipline(id) { return catalog && catalog.disciplines.find((x) => x.discipline_id === id); }
  function disciplineName(id) { const d = discipline(id); return d ? d.name : id; }
  const CATEGORY = {
    sprint: "短距離", middle_distance: "中距離", long_distance: "長距離", hurdles: "ハードル", steeplechase: "障害",
    horizontal_jump: "跳躍（幅）", vertical_jump: "跳躍（高さ）", throw: "投擲", race_walk: "競歩", relay: "リレー",
  };
  function fillDisciplines(select, withAll) {
    clear(select);
    if (withAll) { const o = node("option", "すべての種目"); o.value = ""; select.appendChild(o); }
    const groups = {};
    for (const d of catalog.disciplines) {
      if (!groups[d.category]) {
        groups[d.category] = document.createElement("optgroup");
        groups[d.category].label = CATEGORY[d.category] || d.category;
        select.appendChild(groups[d.category]);
      }
      const o = node("option", d.name);
      o.value = d.discipline_id;
      groups[d.category].appendChild(o);
    }
  }
  /** 種目ごとに、何が分かるかを一文で。 */
  function disciplineNote(id) {
    const d = discipline(id);
    if (!d) return "";
    const st = d.metric_status;
    const runs = st.contact_time === "supported" ? "接地時間・ピッチ・膝と体幹の角度を測ります。"
      : st.contact_time === "experimental" ? "接地時間・ピッチ・角度を測ります（この種目では試験的）。"
      : "接地時の角度だけを参考として出します（走りの指標は対象外）。";
    return runs;
  }

  const METRICS = [
    ["contact_time", "接地時間", "s"],
    ["cadence", "ピッチ", "歩/秒"],
    ["knee_angle_at_contact", "接地時の膝の角度", "°"],
    ["trunk_lean_at_contact", "接地時の前傾", "°"],
  ];
  // 状態は利用者の言葉で
  const STATE = { valid: "確定", estimated: "推定", review_required: "要確認", unavailable: "出せない" };
  const RANK = { valid: 0, estimated: 1, review_required: 2, unavailable: 3 };

  function fmtDate(s) {
    if (!s) return "";
    const d = new Date(s.length <= 10 ? s + "T00:00:00" : s);
    if (isNaN(d)) return s;
    return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日";
  }
  function localDate(d) {
    const z = (n) => (n < 10 ? "0" : "") + n;
    return d.getFullYear() + "-" + z(d.getMonth() + 1) + "-" + z(d.getDate());
  }
  function fmtClock(t) {
    if (typeof t !== "number" || !isFinite(t)) return "—";
    const m = Math.floor(t / 60), s = t - m * 60;
    return (m ? m + ":" + (s < 10 ? "0" : "") : "") + s.toFixed(3) + (m ? "" : " s");
  }

  // ================= ルーティング =================
  let pollTimer = null;
  function stopPolling() { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } }

  async function route() {
    stopPolling();
    stopPlayback();
    endPick();
    endPersonPick();
    const h = location.hash || "#/";
    for (const v of ["view-home", "view-video", "view-compare"]) $(v).hidden = true;
    $("nav-home").removeAttribute("aria-current");
    $("nav-compare").removeAttribute("aria-current");
    try {
      await loadCatalog();
      const m = /^#\/video\/([a-z0-9]{12})$/.exec(h);
      if (m) { $("view-video").hidden = false; await openVideo(m[1]); }
      else if (h === "#/compare") { $("view-compare").hidden = false; $("nav-compare").setAttribute("aria-current", "page"); await openCompare(); }
      else { $("view-home").hidden = false; $("nav-home").setAttribute("aria-current", "page"); await openHome(); }
    } catch (err) {
      toast("読み込めませんでした: " + err.message, true);
    }
    window.scrollTo(0, 0);
  }

  // ================= 練習（一覧・追加） =================
  let homeReady = false;
  let pickedFile = null;

  function setupHome() {
    if (homeReady) return;
    homeReady = true;
    fillDisciplines($("up-discipline"), false);
    const pickOne = node("option", "種目を選んでください");
    pickOne.value = "";
    pickOne.disabled = true;
    $("up-discipline").insertBefore(pickOne, $("up-discipline").firstChild);
    $("up-discipline").value = "";
    fillDisciplines($("f-discipline"), true);
    wireSeg("up-direction");
    wireSeg("up-view");
    const showNote = () => { $("up-discipline-note").textContent = disciplineNote($("up-discipline").value); };
    $("up-discipline").addEventListener("change", showNote);
    showNote();

    const drop = $("drop");
    $("drop-empty").addEventListener("click", () => $("up-file").click());
    drop.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && !pickedFile && ev.target === drop) { ev.preventDefault(); $("up-file").click(); }
    });
    $("up-file").addEventListener("change", () => { if ($("up-file").files[0]) pickFile($("up-file").files[0]); });
    $("up-change").addEventListener("click", () => $("up-file").click());
    for (const type of ["dragenter", "dragover"]) {
      drop.addEventListener(type, (ev) => { ev.preventDefault(); drop.classList.add("dragging"); });
    }
    for (const type of ["dragleave", "drop"]) {
      drop.addEventListener(type, (ev) => { ev.preventDefault(); drop.classList.remove("dragging"); });
    }
    drop.addEventListener("drop", (ev) => {
      const f = ev.dataTransfer && ev.dataTransfer.files[0];
      if (f) pickFile(f);
    });
    $("upload-form").addEventListener("submit", (e) => { e.preventDefault(); upload(); });

    let t = null;
    const refilter = () => { clearTimeout(t); t = setTimeout(refreshList, 250); };
    $("filter-form").addEventListener("submit", (e) => { e.preventDefault(); refreshList(); });
    for (const id of ["f-q", "f-tag"]) $(id).addEventListener("input", refilter);
    for (const id of ["f-discipline", "f-from", "f-to"]) $(id).addEventListener("change", refreshList);
  }

  function pickFile(f) {
    pickedFile = f;
    $("drop-empty").hidden = true;
    $("upload-form").hidden = false;
    $("up-name").textContent = f.name + "（" + (f.size / 1048576).toFixed(1) + " MB）";
    if (!$("up-date").value) {
      const d = f.lastModified ? new Date(f.lastModified) : new Date();
      $("up-date").value = localDate(d);
    }
    $("up-discipline").focus();
  }

  function resetUpload() {
    pickedFile = null;
    $("upload-form").reset();
    $("up-discipline").value = "";
    setSeg("up-direction", "left_to_right");
    setSeg("up-view", "side");
    $("upload-form").hidden = true;
    $("drop-empty").hidden = false;
    $("up-progress-wrap").hidden = true;
    $("up-file").value = "";
  }

  async function openHome() {
    setupHome();
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
    const list = $("video-list");
    clear(list);
    $("list-empty").hidden = data.videos.length > 0;
    let anyRunning = false;
    for (const v of data.videos) {
      const a = node("a", null, "card");
      a.href = "#/video/" + v.id;
      const th = node("div", null, "thumb");
      const img = new Image();
      img.alt = "";
      img.loading = "lazy";
      img.onerror = () => img.remove();
      img.src = "/api/videos/" + v.id + "/frames/0.jpg";
      th.appendChild(img);
      const job = v.job;
      const running = job && ["queued", "probing", "inferring", "analyzing", "cancel_requested"].includes(job.state);
      if (running) {
        anyRunning = true;
        th.appendChild(node("span", "解析中 " + Math.round((job.progress || 0) * 100) + "%", "pill run"));
      } else if (job && job.state === "failed" && !v.latest_analysis) {
        th.appendChild(node("span", "解析できませんでした", "pill fail"));
      } else if (!v.latest_analysis) {
        th.appendChild(node("span", "未解析", "pill"));
      }
      a.appendChild(th);
      const body = node("div", null, "card-body");
      body.appendChild(node("span", fmtDate(v.practice_date || v.created_at.slice(0, 10)), "card-date"));
      body.appendChild(node("span", disciplineName(v.discipline) + (v.record ? "　" + v.record : ""), "card-title"));
      body.appendChild(node("span", v.notes || v.tags || v.filename, "card-meta"));
      a.appendChild(body);
      list.appendChild(a);
    }
    stopPolling();
    if (anyRunning && !$("view-home").hidden) {
      pollTimer = setTimeout(() => refreshList().catch(() => { pollTimer = setTimeout(refreshList, 6000); }), 3000);
    }
  }

  function upload() {
    const f = pickedFile;
    if (!f) return;
    const meta = {
      discipline: $("up-discipline").value, camera_view: segValue("up-view"), direction: segValue("up-direction"),
      practice_date: $("up-date").value || null, place: $("up-place").value, record: $("up-record").value,
      tags: $("up-tags").value, notes: $("up-notes").value, declared_capture_fps: $("up-fps").value.trim(),
      declared_slowmo: $("up-slowmo").checked,
    };
    // 送信の進み具合を出すため XMLHttpRequest で送る（fetch では取れない）
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos");
    xhr.setRequestHeader("X-Athform-Upload", "1");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(f.name));
    xhr.setRequestHeader("X-Athform-Meta", encodeURIComponent(JSON.stringify(meta)));
    $("up-submit").disabled = true;
    $("up-progress-wrap").hidden = false;
    $("up-status").textContent = "動画を取り込んでいます…";
    $("up-progress").style.width = "0%";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) $("up-progress").style.width = Math.round(e.loaded / e.total * 100) + "%";
    };
    xhr.onerror = () => { $("up-submit").disabled = false; $("up-status").textContent = "送れませんでした。もう一度試してください"; };
    xhr.onload = async () => {
      $("up-submit").disabled = false;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = {}; }
      if (xhr.status !== 201) { $("up-status").textContent = "取り込めませんでした: " + (data.error || xhr.status); return; }
      try { await api("POST", "/api/videos/" + data.video.id + "/jobs", { kind: "full", mode: "performance" }); }
      catch (err) { toast("解析を始められませんでした: " + err.message, true); }
      resetUpload();
      location.hash = "#/video/" + data.video.id;
    };
    xhr.send(f);
  }

  // ================= 解析画面 =================
  let current = null;      // { video, job, analyses, corrections }
  let model = null;
  let state = PC.createState();
  let timer = null;
  let selectedEvent = null;
  let viewingLatest = true;
  let editing = false;
  const images = new Map();
  let videoReady = false;
  let times = [];          // 位置 → 元動画時刻（タイムライン用）

  function setupVideoView() {
    if (videoReady) return;
    videoReady = true;
    fillDisciplines($("m-discipline"), false);
    wireSeg("m-direction");
    wireSeg("m-view");
    const rateLabel = { 1: "×1", 0.5: "½", 0.25: "¼", 0.1: "⅒" };
    for (const r of PC.RATES) {
      const b = node("button", rateLabel[r] || ("×" + r));
      b.type = "button";
      b.dataset.value = String(r);
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(r === 1));
      b.title = r === 1 ? "そのままの速さ" : r + " 倍の速さ";
      $("rate-seg").appendChild(b);
    }
    wireSeg("rate-seg", (v) => { state = PC.setRate(state, Number(v)); render(); });
    $("btn-play").addEventListener("click", togglePlay);
    $("btn-prev").addEventListener("click", () => stepBy(-1));
    $("btn-next").addEventListener("click", () => stepBy(1));
    $("btn-unloop").addEventListener("click", () => { state = PC.clearLoop(state); render(); });
    for (const id of ["show-skeleton", "show-ground"]) {
      $(id).addEventListener("click", () => { $(id).setAttribute("aria-pressed", String($(id).getAttribute("aria-pressed") !== "true")); render(); });
    }
    document.addEventListener("keydown", (ev) => {
      if ($("view-video").hidden || !model) return;
      const t = ev.target;
      if (t instanceof HTMLInputElement || t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement) return;
      if (ev.key === " " && t instanceof Element && t.closest("button, summary, a")) return;
      if (ev.key === "ArrowRight") { ev.preventDefault(); stepBy(ev.shiftKey ? 10 : 1); }
      else if (ev.key === "ArrowLeft") { ev.preventDefault(); stepBy(ev.shiftKey ? -10 : -1); }
      else if (ev.key === " ") { ev.preventDefault(); togglePlay(); }
      else if (ev.key === "Escape") { endPick(); endPersonPick(); }
    });

    // タイムライン: クリック・ドラッグで移動、印をクリックで選ぶ
    const tl = $("timeline");
    let dragging = false;
    const seekAt = (ev, select) => {
      if (!model) return;
      const rect = tl.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      if (select && editing) {
        const hit = eventAt(x, ev.clientY - rect.top, rect.width);
        if (hit) { selectEvent(hit.event_id, true); return; }
      }
      const t = xToTime(x, rect.width);
      seekPosition(nearestPosition(t));
    };
    tl.addEventListener("pointerdown", (ev) => { dragging = true; tl.setPointerCapture(ev.pointerId); seekAt(ev, true); });
    tl.addEventListener("pointermove", (ev) => { if (dragging) seekAt(ev, false); });
    tl.addEventListener("pointerup", () => { dragging = false; });
    window.addEventListener("resize", () => { if (model) renderTimeline(); });

    $("run-performance").addEventListener("click", () => startJob("full", "performance"));
    $("run-balanced").addEventListener("click", () => startJob("full", "balanced"));
    $("rerun").addEventListener("click", () => {
      closeMenu();
      if (confirm("姿勢推定からやり直します。数分〜十数分かかります。修正した内容は残ります。")) startJob("full", "performance");
    });
    $("job-cancel").addEventListener("click", async () => {
      if (current && current.job) { await api("POST", "/api/jobs/" + current.job.id + "/cancel", {}); pollJob(); }
    });
    $("meta-form").addEventListener("submit", (e) => { e.preventDefault(); saveMeta(); });
    $("delete-video").addEventListener("click", () => { closeMenu(); deleteVideo(); });

    // 修正
    $("edit-toggle").addEventListener("click", () => setEditing(!editing));
    for (const r of ["映像で確認した", "隠れていて誤検出", "地面の線のずれ", "別の人を検出"]) {
      $("reason-chips").appendChild(button(r, () => { $("c-reason").value = r; $("c-reason").focus(); }));
    }
    const add = (type, side) => () => correct({ action: "add", event_type: type, side: side, after_frame_id: PC.view(model, state).frameId });
    $("c-add-contact-left").addEventListener("click", add("foot_contact", "left"));
    $("c-add-contact-right").addEventListener("click", add("foot_contact", "right"));
    $("c-add-off-left").addEventListener("click", add("toe_off", "left"));
    $("c-add-off-right").addEventListener("click", add("toe_off", "right"));

    // 地面の指定（2点）・自動推定に戻す
    $("stage").addEventListener("click", onStageClick);
    $("ground-pick").addEventListener("click", startPick);
    $("person-pick").addEventListener("click", () => (personMode ? endPersonPick() : startPersonPick()));
    $("ground-reset").addEventListener("click", () => {
      if (confirm("自分で決めた地面の線をやめて、自動推定に戻します。計算し直します。")) saveGround(null);
    });
  }

  function setEditing(on) {
    editing = on;
    $("edit-toggle").setAttribute("aria-pressed", String(on));
    $("edit-toggle").textContent = on ? "修正を終える" : "修正する";
    $("edit-panel").hidden = !on;
    if (!on) selectedEvent = null;
    if (model) { renderInspector(); renderTimeline(); }
  }

  function closeMenu() { for (const d of document.querySelectorAll("details.menu")) d.open = false; }

  async function openVideo(id) {
    setupVideoView();
    peopleGen += 1;
    peopleCache.clear();
    current = await api("GET", "/api/videos/" + id);
    const v = current.video;
    $("v-title").textContent = disciplineName(v.discipline) + (v.record ? "　" + v.record : "");
    $("v-sub").textContent = [fmtDate(v.practice_date), v.place, v.filename].filter(Boolean).join(" ・ ");
    $("export-link").href = "/api/videos/" + id + "/export";
    fillMeta(v);
    renderHistory();
    setEditing(false);
    $("version-note").hidden = true;
    await loadAnalysis();
    showJob();
  }

  function fillMeta(v) {
    $("m-discipline").value = v.discipline;
    setSeg("m-view", v.camera_view || "side");
    setSeg("m-direction", v.direction || "left_to_right");
    $("m-date").value = v.practice_date || "";
    $("m-place").value = v.place || "";
    $("m-record").value = v.record || "";
    $("m-tags").value = v.tags || "";
    $("m-fps").value = v.declared_capture_fps || "";
    $("m-slowmo").checked = !!v.declared_slowmo;
    $("m-notes").value = v.notes || "";
  }

  async function loadAnalysis(revision) {
    let snap = null;
    try {
      snap = await api("GET", "/api/videos/" + current.video.id + "/analysis" + (revision ? "?revision=" + revision : ""));
    } catch (err) { snap = null; }
    const latest = current.analyses.length ? current.analyses[0].revision : null;
    viewingLatest = !snap || !latest || snap.revision === latest;
    $("player").hidden = !snap;
    if (!snap) { model = null; return; }
    model = PC.loadSnapshot(snap);
    state = PC.setRate(PC.createState(), Number(segValue("rate-seg")) || 1);
    images.clear();
    times = model.frames.map((f) => f.t);
    const size = PC.imageSize(model);
    $("stage").width = size.width;
    $("stage").height = size.height;
    selectedEvent = null;
    renderAll();
  }

  function renderAll() {
    renderVersionNote();
    renderTrust(); renderSummary(); renderEventsSummary(); renderInspector(); renderCorrections();
    renderQuality(); render();
  }

  function renderVersionNote() {
    const s = model.snapshot, v = current.video;
    const notes = [];
    if (!viewingLatest) notes.push("前の解析（版 " + s.revision + "）を見ています。修正は最新の版にだけできます。");
    if (s.capture_conditions.discipline !== v.discipline || (s.capture_conditions.camera_view || "") !== (v.camera_view || "")) {
      notes.push("動画の情報が、この解析をしたときから変わっています。「動画の情報を変える」から計算し直してください。");
    }
    const j = current.job;
    const latestAt = current.analyses.length ? current.analyses[0].created_at : "";
    const jobIsNewer = j && (!latestAt || (j.updated_at || "") > latestAt);
    if (viewingLatest && jobIsNewer && j.state === "failed") {
      notes.push("最後の計算し直しはうまくいきませんでした" + (j.error ? "（" + j.error + "）" : "") + "。表示しているのはその前の結果です。");
    } else if (viewingLatest && jobIsNewer && j.state === "cancelled") {
      notes.push("最後の計算し直しは取り消されました。表示しているのはその前の結果です。");
    }
    $("version-note").textContent = notes.join(" ");
    $("version-note").hidden = !notes.length;
    $("edit-toggle").disabled = !viewingLatest;
    if (!viewingLatest && editing) setEditing(false);
    $("ground-pick").disabled = !viewingLatest;
    $("person-pick").disabled = !viewingLatest;
    $("ground-reset").hidden = !(viewingLatest && current.video.ground_override);
  }

  function renderHistory() {
    const ul = $("history-list");
    clear(ul);
    for (const a of current.analyses) {
      const li = node("li");
      const label = "版 " + a.revision + "　" + a.created_at.replace("T", " ").slice(0, 16) +
        (a.mode === "balanced" ? "（軽く試した結果）" : a.mode === "legacy" ? "（旧形式）" : "");
      li.appendChild(button(label, () => loadAnalysis(a.revision), "link"));
      ul.appendChild(li);
    }
    if (!current.analyses.length) ul.appendChild(node("li", "まだありません", "muted"));
  }

  // ---------- 確かさ（いちばん上に、やることと一緒に） ----------
  function renderTrust() {
    const box = $("trust");
    clear(box);
    const s = model.snapshot;
    const gq = s.ground_quality || {};
    const q = s.capture_quality || {};
    const usable = model.metrics.filter((m) => m.status !== "unavailable");
    // いちばん弱い状態で決める（一部でも要確認なら「確定」とは言わない）
    const worst = PC.overallStatus(model.metrics);
    const reviewNames = METRICS.map(([id, n]) => {
      const all = usable.filter((m) => m.metric_id === id);
      const rv = all.filter((m) => m.status === "review_required").length;
      if (!rv) return null;
      return rv === all.length ? n : n + "の一部（" + rv + "/" + all.length + " 回）";
    }).filter(Boolean);
    let level, title, text;
    const reasons = [];
    const actions = [];
    if (["slowmo_suspected", "declared", "unknown"].includes(q.capture_time_status)) {
      reasons.push("撮影したときの本当の時間が分からないため、秒で表す値（接地時間・ピッチ）は出していません。");
    }
    for (const p of gq.problems || []) reasons.push(p);
    for (const p of gq.pose_problems || []) reasons.push(p);
    if ((s.events.unapplied_corrections || []).length) reasons.push("当てられなかった修正があります（修正の記録を確かめてください）。");
    if (s.preview) {
      level = "estimated"; title = "軽く試した結果です";
      text = "速いモデルで解析しました。きちんと測るには、精度を優先して解析し直してください。";
      actions.push(button("精度を優先して解析する", () => startJob("full", "performance"), "primary"));
    } else if (!usable.length) {
      level = "unavailable"; title = "この動画では値を出せませんでした";
      text = "下の理由を確かめてください。横から、カメラを固定して全身が映る動画だと測りやすくなります。";
    } else if (worst === "valid") {
      level = "valid"; title = "確定した値です";
      text = "地面の線を指定済みで、すべての値が品質の確認を通っています。";
    } else if (worst === "estimated" && gq.method === "auto" && !(gq.problems || []).length && !reasons.length) {
      level = "estimated"; title = "おおよその値です";
      text = "地面の位置を自動で推定しています。映像で地面を2点クリックして決めると、値が確定します。";
      if (viewingLatest) actions.push(button("地面を指定する", startPick, "primary"));
    } else {
      level = "review_required"; title = "確かめが必要です";
      text = reviewNames.length ? reviewNames.join("・") + " は、確かめが必要です。" : "次の理由で、値はまだ確定できません。";
      const own = mostCommonList(usable.filter((m) => m.status === "review_required").flatMap((m) => m.reasons || []).map(plainReason), 3);
      for (const r of own) if (!reasons.includes(r)) reasons.push(r);
      if (!reasons.length) {
        const lim = mostCommonList(usable.filter((m) => m.status === "estimated").flatMap((m) => m.limitations || []).map(plainReason), 3);
        for (const r of lim) reasons.push(r);
      }
      if (gq.method !== "user_override" && viewingLatest) actions.push(button("地面を指定する", startPick, "quiet"));
    }
    box.className = "trust lv-" + level;
    const head = node("div", null, "trust-head");
    head.appendChild(node("span", null, "dot"));
    head.appendChild(node("span", title, "trust-level"));
    box.appendChild(head);
    box.appendChild(node("p", text));
    const tg = s.target || {};
    box.appendChild(node("p", tg.lost
      ? "選んだ人が、姿勢推定をやり直した後に見つからなかったため、自動の選び方（最初のコマで最も大きい人）に戻しました。選び直してください（" + (tg.reason || "") + "）"
      : tg.frame_id !== undefined
        ? "解析している人: コマ " + tg.frame_id + " で選んだ人（前後を追っています）"
        : "解析している人: 最初のコマで最も大きく写った人（自動）。違うときは選び直せます", tg.lost ? "small" : "muted small"));
    if (reasons.length) {
      const ul = node("ul");
      for (const r of reasons) ul.appendChild(node("li", r));
      box.appendChild(ul);
    }
    if (s.people_available && viewingLatest) {
      actions.push(button(tg.frame_id !== undefined && !tg.lost ? "解析する人を選び直す" : "解析する人を選ぶ", startPersonPick, "quiet"));
    }
    if (actions.length) {
      const row = node("div", null, "row wrap");
      for (const a of actions) row.appendChild(a);
      box.appendChild(row);
    }
  }

  // ---------- 結果（4つの数字） ----------
  let expandedMetric = null;
  function median(xs) { const s = xs.slice().sort((a, b) => a - b); const n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; }
  function mostCommonList(xs, k) {
    const c = new Map();
    for (const x of xs) c.set(x, (c.get(x) || 0) + 1);
    return [...c.entries()].sort((a, b) => b[1] - a[1]).slice(0, k).map(([x]) => x);
  }
  function mostCommon(xs) {
    const c = new Map();
    for (const x of xs) c.set(x, (c.get(x) || 0) + 1);
    let best = null, n = 0;
    for (const [k, v] of c) if (v > n) { best = k; n = v; }
    return best;
  }

  function renderSummary() {
    const box = $("summary");
    clear(box);
    for (const [id, name, unit] of METRICS) {
      const ms = model.metrics.filter((m) => m.metric_id === id);
      const has = (m) => m.value !== null && m.value !== undefined && m.status !== "unavailable";
      // 中央値は「確定・推定」の値だけで出す（比較と同じ）。要確認しかないときだけ、それを要確認として出す
      const usable = ms.filter((m) => has(m) && (m.status === "valid" || m.status === "estimated"));
      const review = ms.filter((m) => has(m) && m.status === "review_required");
      const shown = usable.length ? usable : review;
      const card = node("div", null, "metric");
      card.setAttribute("aria-expanded", String(expandedMetric === id));
      const head = node("button", null, "metric-toggle");
      head.type = "button";
      head.setAttribute("aria-expanded", String(expandedMetric === id));
      head.appendChild(node("span", name, "metric-name"));
      if (shown.length) {
        const vals = shown.map((m) => m.value);
        const med = median(vals);
        const wide = Math.max(...shown.map((m) => (m.interval ? (m.interval.upper - m.interval.lower) / 2 : 0)));
        const digits = unit === "°" ? 0 : unit === "s" ? (wide >= 0.005 ? 2 : 3) : 2;
        const val = node("span", unit === "°" ? String(Math.round(med)) : med.toFixed(digits), "metric-value");
        val.appendChild(node("small", unit));
        head.appendChild(val);
        const ivs = shown.filter((m) => m.interval);
        if (ivs.length) {
          const lo = Math.min(...ivs.map((m) => m.interval.lower)), hi = Math.max(...ivs.map((m) => m.interval.upper));
          head.appendChild(node("span", "幅 " + lo.toFixed(digits) + "〜" + hi.toFixed(digits) + unit + "（" + shown.length + "回分）", "metric-range"));
        } else {
          head.appendChild(node("span", shown.length + "回分の中央値", "metric-range"));
        }
        const st = shown.reduce((a, m) => (RANK[m.status] > RANK[a] ? m.status : a), "valid");
        head.appendChild(node("span", STATE[st], "state state-" + st));
        if (usable.length && review.length) head.appendChild(node("span", "要確認の " + review.length + " 回は除いています", "metric-why"));
        else if (!usable.length) {
          const why = mostCommon(review.flatMap((m) => m.reasons || []));
          if (why) head.appendChild(node("span", plainReason(why), "metric-why"));
        }
      } else {
        head.appendChild(node("span", "—", "metric-value none"));
        head.appendChild(node("span", STATE.unavailable, "state state-unavailable"));
        const why = mostCommon(ms.flatMap((m) => m.reasons || []));
        if (why) head.appendChild(node("span", plainReason(why), "metric-why"));
      }
      head.dataset.metric = id;
      head.addEventListener("click", () => {
        expandedMetric = expandedMetric === id ? null : id;
        renderSummary();
        const again = $("summary").querySelector('[data-metric="' + id + '"]');
        if (again) again.focus();
      });
      card.appendChild(head);
      if (expandedMetric === id) card.appendChild(metricDetail(ms, unit));
      box.appendChild(card);
    }
    $("feedback-text").textContent = "フォームの良し悪しの判定・改善の提案は、専門家が確かめた基準がまだないため出していません。";
  }

  /** 内部向けの理由を、利用者向けに短くする。 */
  function plainReason(r) {
    r = String(r).replace(/^(地面の線|姿勢推定): /, "");   // 先頭の欄の理由と重ならないように、出どころの見出しを外す
    if (/実時間|slowmo|capture_time/.test(r)) return "撮影したときの時間が分からない";
    if (/3回未満/.test(r)) return "続けて見えた接地が少ない";
    if (/対応する離地/.test(r)) return "離地が見えていない";
    if (/途切れ/.test(r)) return "途中で見えなくなっている";
    if (/側面でない/.test(r)) return "横から撮った動画でないと測れない";
    if (/見えない|信頼度不足/.test(r)) return "関節がはっきり見えていない";
    if (/では/.test(r) && /指標/.test(r)) return "この種目では出していない";
    return r.length > 40 ? r.slice(0, 40) + "…" : r;
  }

  function metricDetail(ms, unit) {
    const box = node("div", null, "metric-detail");
    for (const m of ms) {
      const ids = (m.evidence_refs && m.evidence_refs.frame_ids) || [];
      const target = ids[ids.length > 1 ? 1 : 0];
      const b = node("button");
      b.type = "button";
      b.appendChild(node("span", m.side === "left" ? "左" : m.side === "right" ? "右" : "—", m.side === "left" ? "side-l" : m.side === "right" ? "side-r" : ""));
      b.appendChild(node("span", m.value === null || m.value === undefined ? (m.reasons[0] ? plainReason(m.reasons[0]) : "—") : m.display));
      b.appendChild(node("span", STATE[m.status], "state state-" + m.status));
      if (target !== undefined && model.positionById.has(target)) b.addEventListener("click", () => seekFrame(target));
      else b.disabled = true;
      if (m.status !== "valid" && (m.reasons || []).length) b.appendChild(node("span", m.reasons.map(plainReason).join("・"), "why-line"));
      box.appendChild(b);
    }
    return box;
  }

  // ---------- 接地・離地と修正 ----------
  function renderEventsSummary() {
    const ev = model.events;
    const n = (side, type) => ev.filter((e) => e.side === side && e.event_type === type).length;
    const review = ev.filter((e) => e.quality_status !== "valid").length;
    $("events-summary").textContent = ev.length
      ? "左の接地 " + n("left", "foot_contact") + " 回・右の接地 " + n("right", "foot_contact") + " 回" + (review ? "（うち " + review + " 件は要確認）" : "") + "。タイムラインの印で位置を確かめられます。"
      : "接地・離地を見つけられませんでした。地面の線の位置や、撮った向きを確かめてください。";
  }

  function selectEvent(id, seekTo) {
    selectedEvent = id;
    const e = model.events.find((x) => x.event_id === id);
    if (e && seekTo) seekFrame(e.evidence_frame_id);
    renderInspector();
    renderTimeline();
  }

  function renderInspector() {
    const box = $("inspector");
    clear(box);
    if (!editing || !model) return;
    const e = selectedEvent && model.events.find((x) => x.event_id === selectedEvent);
    const nav = node("div", null, "ev-nav");
    const order = model.events.slice().sort((a, b) => a.event_time_sec - b.event_time_sec);
    const idx = e ? order.findIndex((x) => x.event_id === e.event_id) : -1;
    const go = (t, which) => { if (!t) return; selectEvent(t.event_id, true); const b = box.querySelector('[data-nav="' + which + '"]'); if (b) b.focus(); };
    const prevB = button("◀ 前の印", () => go(order[idx > 0 ? idx - 1 : order.length - 1], "prev"), "quiet small-btn");
    const nextB = button("次の印 ▶", () => go(order[idx >= 0 && idx < order.length - 1 ? idx + 1 : 0], "next"), "quiet small-btn");
    prevB.dataset.nav = "prev";
    nextB.dataset.nav = "next";
    nav.appendChild(prevB);
    nav.appendChild(nextB);
    nav.appendChild(node("span", order.length ? (idx >= 0 ? (idx + 1) + " / " + order.length : order.length + " 個の印") : "印がありません", "muted small"));
    box.appendChild(nav);
    if (!e) {
      box.appendChild(node("p", "直したい印を、タイムラインか「前の印・次の印」で選んでください。", "muted small"));
      return;
    }
    const kind = (e.side === "left" ? "左足の" : "右足の") + (e.event_type === "foot_contact" ? "接地" : "離地");
    const iv = e.time_interval_sec || {};
    box.appendChild(node("strong", kind + (e.source === "manual" ? "（修正済み）" : "")));
    box.appendChild(node("span", "コマ " + e.source_frame_ids.join("→") + "　" + fmtClock(iv.lower) + "〜" + fmtClock(iv.upper), "muted small mono"));
    if (e.quality_reasons && e.quality_reasons.length) box.appendChild(node("span", "要確認: " + e.quality_reasons.join("、"), "small"));
    const row = node("div", null, "row wrap");
    row.appendChild(button("いま表示中のコマに移す", () => correct({ action: "move", event_id: e.event_id, after_frame_id: PC.view(model, state).frameId }), "primary"));
    row.appendChild(button("前後をゆっくり見る", () => {
      stopPlayback();
      state = PC.setRate(PC.loopAroundEvent(model, state, e, 6), 0.25);
      setSeg("rate-seg", "0.25");
      togglePlay();
    }, "quiet"));
    row.appendChild(button("この印を消す", () => correct({ action: "delete", event_id: e.event_id }), "quiet danger"));
    box.appendChild(row);
  }

  async function correct(body) {
    if (!viewingLatest) { $("c-status").textContent = "前の版は修正できません"; return; }
    const reason = $("c-reason").value.trim();
    if (!reason) { $("c-status").textContent = "理由を選ぶか入力してください"; $("c-reason").focus(); return; }
    body.reason = reason;
    $("c-status").textContent = "計算し直しています…";
    try {
      const snap = await api("POST", "/api/videos/" + current.video.id + "/corrections", body);
      await afterCorrection(snap);
      $("c-status").textContent = "直しました。関係する値も計算し直しています。";
      toast("修正しました");
    } catch (err) { $("c-status").textContent = "直せませんでした: " + err.message; }
  }

  async function afterCorrection(snap) {
    const keep = PC.view(model, state).frameId;
    current = await api("GET", "/api/videos/" + current.video.id);
    renderHistory();
    viewingLatest = true;
    model = PC.loadSnapshot(snap);
    times = model.frames.map((f) => f.t);
    state = PC.seekFrameId(model, PC.setRate(PC.createState(), state.rate), keep);
    selectedEvent = null;
    renderAll();
  }

  function renderCorrections() {
    const ul = $("corrections-list");
    clear(ul);
    const unapplied = new Map(((model.snapshot.events || {}).unapplied_corrections || []).map((u) => [u.index, u.reason]));
    const list = viewingLatest ? (current.corrections || []) : ((model.snapshot.events || {}).corrections || []);
    const verb = { move: "移動", add: "追加", delete: "削除" };
    list.forEach((c, i) => {
      const li = node("li");
      const what = (verb[c.action] || c.action) + "：" + c.reason + (c.after_frame_id !== null && c.after_frame_id !== undefined ? "（コマ " + c.after_frame_id + "）" : "");
      const label = node("span", what, c.revoked_at ? "off" : "");
      if (c.revoked_at) label.title = "取り消し: " + (c.revoked_reason || "");
      else if (unapplied.has(i)) label.title = "当てられなかった: " + unapplied.get(i);
      li.appendChild(label);
      if (unapplied.has(i) && !c.revoked_at) li.appendChild(node("span", "当てられなかった", "state state-review_required"));
      if (!c.revoked_at && viewingLatest) li.appendChild(button("取り消す", () => revokeCorrection(i), "link"));
      ul.appendChild(li);
    });
    if (!list.length) ul.appendChild(node("li", "まだありません", "muted"));
  }

  async function revokeCorrection(i) {
    const reason = (window.prompt("この修正を取り消す理由（記録に残ります）", "") || "").trim();
    if (!reason) return;
    try {
      const snap = await api("POST", "/api/videos/" + current.video.id + "/corrections/" + i + "/revoke", { reason: reason });
      await afterCorrection(snap);
      toast("修正を取り消しました");
    } catch (err) { toast("取り消せませんでした: " + err.message, true); }
  }

  // ---------- 地面の指定（映像を2点クリック） ----------
  let pick = null;   // null / [] / [{x,y}]
  function startPick() {
    if (!model) return;
    if (!viewingLatest) { toast("前の版を見ているときは、地面を指定できません"); return; }
    endPersonPick();
    pick = [];
    $("stage").classList.add("picking");
    $("pick-hint").textContent = "地面（足が着く線）の上を、左寄りで1回クリックしてください。Esc でやめる";
    $("pick-hint").hidden = false;
    $("stage").scrollIntoView({ block: "center", behavior: "smooth" });
  }
  function endPick() {
    pick = null;
    $("stage").classList.remove("picking");
    $("pick-hint").hidden = true;
  }
  function stagePoint(ev) {
    const rect = $("stage").getBoundingClientRect();
    const size = PC.imageSize(model);
    return { x: (ev.clientX - rect.left) / rect.width * size.width, y: (ev.clientY - rect.top) / rect.height * size.height };
  }

  // ---------- 解析する人を選ぶ（映像の中の人をクリック） ----------
  let personMode = false;
  const peopleCache = new Map();   // 原本フレームID → そのコマの全員
  let peopleGen = 0;               // 選び直すたびに増やす（古い取得結果を捨てる）

  function startPersonPick() {
    if (!model) return;
    if (!viewingLatest) { toast("前の版を見ているときは、人を選べません"); return; }
    if (!model.snapshot.people_available) {
      toast("この動画は写っている人の候補を保存していません。「…」→「姿勢推定からやり直す」の後に選べます", true);
      return;
    }
    endPick();
    stopPlayback();
    personMode = true;
    $("person-pick").setAttribute("aria-pressed", "true");
    $("person-pick").textContent = "選ぶのをやめる";
    $("stage").classList.add("picking");
    $("pick-hint").textContent = "解析したい人をクリックしてください。←→でコマを送って、はっきり写っているコマで選べます（Esc でやめる）";
    $("pick-hint").hidden = false;
    $("stage").scrollIntoView({ block: "center", behavior: "smooth" });
    render();
  }
  function endPersonPick() {
    if (!personMode) return;
    personMode = false;
    $("person-pick").setAttribute("aria-pressed", "false");
    $("person-pick").textContent = "人を選ぶ";
    $("stage").classList.remove("picking");
    $("pick-hint").hidden = true;
    if (model) render();
  }
  function peopleFor(frameId) {
    if (peopleCache.has(frameId)) return peopleCache.get(frameId);
    if (state.playing) return null;   // 再生中は取りに行かない（止めたコマで取る）
    peopleCache.set(frameId, null);   // 取得中
    const gen = peopleGen;
    api("GET", "/api/videos/" + current.video.id + "/people?frame=" + frameId)
      .then((d) => {
        if (gen !== peopleGen) return;
        peopleCache.set(frameId, d);
        if (personMode && model && PC.view(model, state).frameId === frameId) render();
      })
      .catch(() => { if (gen === peopleGen) peopleCache.delete(frameId); });
    return null;
  }
  function personAt(data, p) {
    let best = null, bd = Infinity;
    for (const q of data.people) {
      const [x0, y0, x1, y1] = q.box;
      const padX = (x1 - x0) * 0.15 + 8, padY = (y1 - y0) * 0.1 + 8;
      const inside = p.x >= x0 - padX && p.x <= x1 + padX && p.y >= y0 - padY && p.y <= y1 + padY;
      const d = Math.hypot(p.x - q.center[0], p.y - q.center[1]);
      if (inside && d < bd) { bd = d; best = q; }
    }
    return best;
  }
  async function choosePerson(ev) {
    const v = PC.view(model, state);
    const data = peopleCache.get(v.frameId);
    if (!data) return;
    const who = personAt(data, stagePoint(ev));
    if (!who) { toast("その位置には人が見つかりませんでした。体の上をクリックしてください"); return; }
    if (who.index === data.tracked) { toast("その人はすでに解析しています"); endPersonPick(); return; }
    const hasCorrections = (current.corrections || []).some((c) => !c.revoked_at);
    const hasGround = !!current.video.ground_override;
    if ((hasCorrections || hasGround) && !confirm("解析する人を変えると、" +
      [hasCorrections ? "前の人への接地・離地の修正は取り消し（記録は残ります）" : "", hasGround ? "指定した地面の線は自動推定に戻します（レーンごとに地面の高さが違うため）" : ""]
        .filter(Boolean).join("、") + "。変えますか？")) return;
    endPersonPick();
    toast("選んだ人を追い直しています…");
    try {
      const snap = await api("POST", "/api/videos/" + current.video.id + "/target", { frame_id: v.frameId, person: who.index });
      peopleGen += 1;
      peopleCache.clear();
      await afterCorrection(snap);
      const notes = snap.target_change_notes || [];
      toast("選んだ人で計算し直しました" + (notes.length ? "（" + notes.join("、") + "）" : ""));
    } catch (err) { toast("選び直せませんでした: " + err.message, true); }
  }

  function onStageClick(ev) {
    if (personMode && model) { choosePerson(ev); return; }
    if (!pick || !model) return;
    pick.push(stagePoint(ev));
    if (pick.length === 1) {
      $("pick-hint").textContent = "同じ地面の上を、右寄りでもう1回クリックしてください";
      render();
      return;
    }
    const [a, b] = pick;
    endPick();
    const slope = Math.abs(b.x - a.x) > 20 ? (b.y - a.y) / (b.x - a.x) : 0;
    if (Math.abs(slope) > 1) { toast("2点の傾きが大きすぎます。地面の上を左右でクリックしてください", true); return; }
    const band = (model.snapshot.detector && model.snapshot.detector.contact_band_px) || 6;
    saveGround({ ground_y: (a.y + b.y) / 2, ground_x0: (a.x + b.x) / 2, ground_slope: slope, contact_band_px: Math.min(200, Math.max(1, band)) });
  }
  async function saveGround(g) {
    try {
      await api("PATCH", "/api/videos/" + current.video.id, { ground_override: g });
      toast("地面を決めました。計算し直しています");
      await startJob("reanalyze", "performance");
    } catch (err) { toast(err.message, true); }
  }

  // ---------- ジョブ ----------
  let jobStarted = null;
  async function startJob(kind, mode) {
    try {
      const r = await api("POST", "/api/videos/" + current.video.id + "/jobs", { kind: kind, mode: mode });
      current.job = r.job;
      jobStarted = { at: Date.now(), progress: 0 };
      showJob();
    } catch (err) { toast(err.message, true); }
  }

  const JOB_TITLE = {
    queued: "順番を待っています", probing: "動画の時間を読み取っています", inferring: "体の動きを読み取っています",
    analyzing: "値を計算しています", cancel_requested: "取り消しています",
  };
  function showJob() {
    const j = current && current.job;
    const active = j && ["queued", "probing", "inferring", "analyzing", "cancel_requested"].includes(j.state);
    $("job-box").hidden = !active;
    $("no-analysis").hidden = !!model || !!active;
    if (active) {
      $("job-text").textContent = JOB_TITLE[j.state] || j.state;
      $("job-progress").style.width = Math.round((j.progress || 0) * 100) + "%";
      // 残り時間の目安（姿勢推定の進み方から）
      let eta = j.message || "";
      if (j.state === "inferring" && jobStarted && j.progress > jobStarted.progress + 0.02) {
        const rate = (j.progress - jobStarted.progress) / ((Date.now() - jobStarted.at) / 1000);
        const left = (1 - j.progress) / rate;
        if (isFinite(left) && left > 0) eta = "残り約 " + (left > 90 ? Math.round(left / 60) + " 分" : Math.max(5, Math.round(left / 5) * 5) + " 秒") + "（" + j.message + "）";
      }
      if (!jobStarted) jobStarted = { at: Date.now(), progress: j.progress || 0 };
      $("job-eta").textContent = eta;
      $("job-cancel").hidden = j.state === "cancel_requested";
      pollJob();
    } else {
      jobStarted = null;
    }
    if (!model && !active) {
      $("no-analysis-text").textContent = j && j.state === "failed"
        ? "解析できませんでした。" + (j.error ? "（" + j.error + "）" : "") + " 撮った条件を確かめて、もう一度試してください。"
        : j && j.state === "cancelled" ? "解析を取り消しました。" : "この動画はまだ解析していません。";
    }
    $("run-performance").disabled = !!active;
    $("run-balanced").disabled = !!active;
  }

  function pollJob() {
    stopPolling();
    pollTimer = setTimeout(async () => {
      if (!current || !current.job || $("view-video").hidden) return;
      try {
        const r = await api("GET", "/api/jobs/" + current.job.id);
        const was = current.job.state;
        current.job = r.job;
        if (r.job.state === "completed" && was !== "completed") { await openVideo(current.video.id); toast("解析が終わりました"); return; }
        if (r.job.state === "failed" && was !== "failed") toast("解析できませんでした: " + (r.job.error || ""), true);
        showJob();
        if (model && (r.job.state === "failed" || r.job.state === "cancelled")) renderVersionNote();
      } catch (err) { toast(err.message, true); }
    }, 1500);
  }

  async function saveMeta() {
    const fields = {
      discipline: $("m-discipline").value, camera_view: segValue("m-view"), direction: segValue("m-direction"),
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

  async function deleteVideo() {
    if (!confirm("この動画と、解析の結果・修正・画像をすべて削除します。元に戻せません。")) return;
    try {
      await api("DELETE", "/api/videos/" + current.video.id, {});
      toast("削除しました");
      location.hash = "#/";
    } catch (err) { toast(err.message, true); }
  }

  // ---------- 映像の描画 ----------
  const BONES = [
    ["left_shoulder", "right_shoulder"], ["left_hip", "right_hip"], ["left_shoulder", "left_hip"], ["right_shoulder", "right_hip"],
    ["left_shoulder", "left_elbow"], ["left_elbow", "left_wrist"], ["right_shoulder", "right_elbow"], ["right_elbow", "right_wrist"],
    ["left_hip", "left_knee"], ["left_knee", "left_ankle"], ["left_ankle", "left_heel"], ["left_ankle", "left_toe"],
    ["right_hip", "right_knee"], ["right_knee", "right_ankle"], ["right_ankle", "right_heel"], ["right_ankle", "right_toe"],
  ];
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function hasSkeleton(v) {
    return !!(v.overlay && v.overlay.keypoints && Object.values(v.overlay.keypoints).some(Boolean));
  }
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

  function drawStage(v) {
    const cv = $("stage");
    const ctx = cv.getContext("2d");
    const w = cv.width, h = cv.height;
    ctx.fillStyle = cssVar("--stage");
    ctx.fillRect(0, 0, w, h);
    const img = imageFor(v.frameId);
    if (img && img.complete && img.naturalWidth) ctx.drawImage(img, 0, 0, w, h);
    for (const id of PC.prefetchIds(model, state, 8)) imageFor(id);
    const lw = Math.max(2, w / 420);
    if ($("show-ground").getAttribute("aria-pressed") === "true") {
      const y0 = PC.groundYAt(model, 0), y1 = PC.groundYAt(model, w);
      if (y0 !== null && y0 < h * 2) {
        ctx.strokeStyle = cssVar("--ground"); ctx.lineWidth = lw; ctx.setLineDash([lw * 5, lw * 4]);
        ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(w, y1); ctx.stroke(); ctx.setLineDash([]);
      }
    }
    if ($("show-skeleton").getAttribute("aria-pressed") === "true" && hasSkeleton(v)) {
      const kp = v.overlay.keypoints;
      const colorOf = (a, b) => (a.startsWith("left") && b.startsWith("left")) ? cssVar("--left")
        : (a.startsWith("right") && b.startsWith("right")) ? cssVar("--right") : "#ffffff";
      ctx.lineCap = "round";
      ctx.lineWidth = lw * 1.8;
      for (const [a, b] of BONES) {
        const p = kp[a], q = kp[b];
        if (!p || !q) continue;
        ctx.strokeStyle = colorOf(a, b);
        ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
      }
      for (const name of Object.keys(kp)) {
        const p = kp[name];
        if (!p) continue;
        ctx.fillStyle = p[2] >= 0.3 ? "#ffffff" : "rgba(255,255,255,.35)";
        ctx.beginPath(); ctx.arc(p[0], p[1], lw * 1.6, 0, Math.PI * 2); ctx.fill();
      }
    }
    // このコマの接地・離地
    const evs = PC.eventsAtFrame(model, v.frameId).filter((e) => e.evidence_frame_id === v.frameId);
    if (evs.length) {
      ctx.font = "600 " + Math.round(w / 48) + "px system-ui, sans-serif";
      let y = h * 0.07;
      for (const e of evs) {
        const label = (e.side === "left" ? "左 " : "右 ") + (e.event_type === "foot_contact" ? "接地" : "離地");
        ctx.fillStyle = e.side === "left" ? cssVar("--left") : cssVar("--right");
        const tw = ctx.measureText(label).width;
        ctx.fillRect(w * 0.02, y - w / 48, tw + w / 60, w / 36);
        ctx.fillStyle = "#fff";
        ctx.fillText(label, w * 0.02 + w / 120, y);
        y += w / 30;
      }
    }
    if (!hasSkeleton(v)) {
      ctx.fillStyle = "rgba(255,255,255,.75)"; ctx.font = Math.round(w / 52) + "px system-ui, sans-serif";
      ctx.fillText("このコマでは、追っている人が見えていません", w * 0.02, h - h * 0.04);
    }
    if (personMode) {
      const data = peopleFor(v.frameId);
      if (data) {
        ctx.font = "600 " + Math.round(w / 55) + "px system-ui, sans-serif";
        data.people.forEach((q, i) => {
          const [x0, y0, x1, y1] = q.box;
          const mine = q.index === data.tracked;
          ctx.strokeStyle = mine ? cssVar("--ground") : "rgba(255,255,255,.9)";
          ctx.lineWidth = mine ? lw * 2 : lw;
          ctx.setLineDash(mine ? [] : [lw * 3, lw * 2]);
          const px = (x1 - x0) * 0.12 + 4, py = (y1 - y0) * 0.06 + 4;
          ctx.strokeRect(x0 - px, y0 - py, x1 - x0 + 2 * px, y1 - y0 + 2 * py);
          ctx.setLineDash([]);
          const label = mine ? "いま解析中" : String(i + 1);
          const tw = ctx.measureText(label).width + w / 100;
          ctx.fillStyle = mine ? cssVar("--ground") : "rgba(0,0,0,.7)";
          ctx.fillRect(x0 - px, y0 - py - w / 40, tw, w / 40);
          ctx.fillStyle = mine ? "#000" : "#fff";
          ctx.fillText(label, x0 - px + w / 200, y0 - py - w / 160);
        });
        if (!data.people.length) {
          ctx.fillStyle = "rgba(255,255,255,.85)";
          ctx.fillText("このコマでは人を見つけられませんでした。コマを送ってください", w * 0.02, h * 0.12);
        }
      }
    }
    if (pick && pick.length) {
      ctx.fillStyle = cssVar("--ground");
      for (const p of pick) { ctx.beginPath(); ctx.arc(p.x, p.y, lw * 3, 0, Math.PI * 2); ctx.fill(); }
    }
  }

  function render() {
    if (!model) return;
    const v = PC.view(model, state);
    drawStage(v);
    $("ro-time").textContent = fmtClock(v.sourceTime) + "　" + (v.position + 1) + " / " + v.frameCount + " コマ";
    $("ro-frame").textContent = v.frameId + "（" + (v.position + 1) + " / " + v.frameCount + "）";
    $("ro-pts").textContent = v.pts + " × " + v.timeBase + "　[" + v.sourceTimeExact + "]";
    $("ro-elapsed").textContent = (v.playbackElapsedMs / 1000).toFixed(3) + " s（×" + v.rate + "）";
    $("ro-overlay").textContent = hasSkeleton(v) ? String(v.overlayFrameId) : "なし（人が見えていない）";
    const evs = PC.eventsAtFrame(model, v.frameId);
    $("ro-events").textContent = evs.length ? evs.map((e) => (e.side === "left" ? "左" : "右") + (e.event_type === "foot_contact" ? "接地" : "離地")).join("、") : "—";
    $("btn-play").textContent = state.playing ? "❚❚" : "▶︎";
    $("btn-play").setAttribute("aria-label", state.playing ? "一時停止" : "再生");
    $("btn-unloop").hidden = !state.loop;
    renderTimeline();
  }

  // ---------- タイムライン ----------
  const PAD = 10;
  function span() { return { t0: times[0] || 0, t1: times[times.length - 1] || 1 }; }
  function timeToX(t, width) { const { t0, t1 } = span(); return PAD + (t - t0) / Math.max(1e-9, t1 - t0) * (width - 2 * PAD); }
  function xToTime(x, width) { const { t0, t1 } = span(); return t0 + (x - PAD) / Math.max(1, width - 2 * PAD) * (t1 - t0); }
  function nearestPosition(t) {
    let lo = 0, hi = times.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (times[mid] < t) lo = mid + 1; else hi = mid; }
    if (lo > 0 && Math.abs(times[lo - 1] - t) < Math.abs(times[lo] - t)) lo -= 1;
    return lo;
  }
  function laneY(side, height) { return side === "left" ? height * 0.30 : height * 0.70; }
  function contactBars() {
    const bars = [];
    for (const side of ["left", "right"]) {
      const seq = model.events.filter((e) => e.side === side);
      seq.forEach((e, i) => {
        if (e.event_type !== "foot_contact") return;
        const n = seq[i + 1];
        bars.push({ side: side, start: e, end: n && n.event_type === "toe_off" ? n : null });
      });
    }
    return bars;
  }
  function eventAt(x, y, width) {
    const h = $("timeline").clientHeight;
    let best = null, bd = 9;
    for (const e of model.events) {
      const ex = timeToX(e.event_time_sec, width), ey = laneY(e.side, h);
      const d = Math.max(Math.abs(ex - x), Math.abs(ey - y) / 3);
      if (d < bd) { bd = d; best = e; }
    }
    return best;
  }

  function renderTimeline() {
    const cv = $("timeline");
    if (!model || !cv.clientWidth) return;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (cv.width !== Math.round(W * dpr)) cv.width = Math.round(W * dpr);
    if (cv.height !== Math.round(H * dpr)) cv.height = Math.round(H * dpr);
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // 見えていない区間
    const gapIds = new Set((model.snapshot.frame_completeness || {}).unobservable_frame_ids || []);
    ctx.fillStyle = cssVar("--gap");
    const fw = Math.max(1, (W - 2 * PAD) / Math.max(1, times.length));
    model.frames.forEach((f, i) => { if (gapIds.has(f.source_frame_id)) ctx.fillRect(timeToX(times[i], W) - fw / 2, 4, fw, H - 8); });
    for (const b of (model.snapshot.events.observation_breaks || [])) {
      const x0 = timeToX(b.time_interval_sec.lower, W), x1 = timeToX(b.time_interval_sec.upper, W);
      ctx.fillStyle = cssVar("--gap");
      ctx.globalAlpha = 0.5;
      ctx.fillRect(x0, laneY(b.side, H) - 9, Math.max(1, x1 - x0), 18);
      ctx.globalAlpha = 1;
    }
    // レーンの線
    ctx.strokeStyle = cssVar("--line");
    ctx.lineWidth = 1;
    for (const side of ["left", "right"]) {
      ctx.beginPath(); ctx.moveTo(PAD, laneY(side, H)); ctx.lineTo(W - PAD, laneY(side, H)); ctx.stroke();
    }
    // 接地している区間
    for (const bar of contactBars()) {
      const color = bar.side === "left" ? cssVar("--left") : cssVar("--right");
      const y = laneY(bar.side, H);
      const x0 = timeToX(bar.start.event_time_sec, W);
      const x1 = bar.end ? timeToX(bar.end.event_time_sec, W) : x0 + 3;
      const review = bar.start.quality_status !== "valid" || (bar.end && bar.end.quality_status !== "valid");
      ctx.fillStyle = color;
      ctx.globalAlpha = review ? 0.45 : 1;
      ctx.fillRect(x0, y - 7, Math.max(3, x1 - x0), 14);
      ctx.globalAlpha = 1;
    }
    // 印（修正中は大きく）
    for (const e of model.events) {
      const x = timeToX(e.event_time_sec, W), y = laneY(e.side, H);
      const sel = e.event_id === selectedEvent;
      if (!editing && !sel) continue;
      ctx.fillStyle = sel ? cssVar("--ink") : (e.side === "left" ? cssVar("--left") : cssVar("--right"));
      ctx.beginPath();
      const r = sel ? 6 : 4;
      if (e.event_type === "foot_contact") { ctx.moveTo(x, y - r - 9); ctx.lineTo(x - r, y - 9 - 2 * r); ctx.lineTo(x + r, y - 9 - 2 * r); }
      else { ctx.moveTo(x, y + r + 9); ctx.lineTo(x - r, y + 9 + 2 * r); ctx.lineTo(x + r, y + 9 + 2 * r); }
      ctx.closePath(); ctx.fill();
    }
    // ループ範囲
    if (state.loop) {
      const x0 = timeToX(times[state.loop[0]], W), x1 = timeToX(times[state.loop[1]], W);
      ctx.fillStyle = cssVar("--ink"); ctx.globalAlpha = 0.08; ctx.fillRect(x0, 0, x1 - x0, H); ctx.globalAlpha = 1;
    }
    // 再生位置
    const px = timeToX(times[state.position], W);
    ctx.strokeStyle = cssVar("--ink"); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(px, 2); ctx.lineTo(px, H - 2); ctx.stroke();
    ctx.fillStyle = cssVar("--ink");
    ctx.beginPath(); ctx.arc(px, 4, 4, 0, Math.PI * 2); ctx.fill();
  }

  function renderQuality() {
    clear($("quality-list")); clear($("quality-notes")); clear($("unsupported-list"));
    const s = model.snapshot, q = s.capture_quality || {}, c = s.frame_completeness || {}, iv = q.interval_s || {};
    const ms = (x) => (typeof x === "number" ? (x * 1000).toFixed(1) + " ms" : "—");
    const g = s.ground_estimate || {};
    const rows = [
      ["撮影の時間", PC.CAPTURE_LABEL[q.capture_time_status] || q.capture_time_status],
      ["コマの間隔", ms(iv.median) + (q.vfr ? "（一定でない）" : "") + (q.nominal_fps ? "　約 " + Math.round(q.nominal_fps) + " fps" : "")],
      ["抜けたコマ", (q.gap_frame_ids || []).length + " か所" + (c.decode_check === "packets" ? "・読めなかったコマ " + (c.decode_failed_source_frame_ids || []).length : "")],
      ["人が見えないコマ", (c.unobservable_frame_ids || []).length + " / " + (c.decoded_frame_count ?? "—")],
      ["地面の線", g.method === "user_override" ? "自分で指定" : g.method === "unavailable" ? "推定できない" : "自動推定"],
      ["解析", s.preview ? "軽く試した結果" : "精度優先・全コマ"],
      ["モデル", s.model ? s.model.weights_version : "—"],
      ["版", (s.revision || "—") + "（" + (s.analysis_id || "").slice(0, 10) + "）"],
    ];
    for (const [k, v] of rows) { $("quality-list").appendChild(node("dt", k)); $("quality-list").appendChild(node("dd", v)); }
    for (const n of q.capture_time_notes || []) $("quality-notes").appendChild(node("li", n));
    const un = (s.discipline && s.discipline.unsupported_metrics) || {};
    const names = { stride_length: "ストライド長", running_speed: "走速度", pace_segment: "ペース区間", hurdle_clearance: "ハードル越え",
      lead_trail_leg: "リード脚・抜き脚", barrier_clearance: "障害越え", water_jump: "水濠", takeoff_angle: "跳躍角度",
      takeoff_board: "踏切板", phase_split: "ホップ・ステップ・ジャンプの区切り", bar_clearance: "バーの越え方",
      approach_curve: "助走の曲線", pole_tracking: "ポールの動き", release_angle: "リリース角度", release_speed: "リリース速度",
      technique_type: "投げ方の種類", rotation: "回転", implement_path: "用具の軌跡", rule_compliance: "規則の適否",
      baton_exchange: "バトンの受け渡し" };
    for (const k of Object.keys(un)) $("unsupported-list").appendChild(node("li", (names[k] || k) + " — " + un[k]));
    if (!Object.keys(un).length) $("unsupported-list").appendChild(node("li", "なし"));
  }

  // ---------- 再生 ----------
  function seekFrame(frameId) { stopPlayback(); state = PC.seekFrameId(model, state, frameId); render(); }
  function seekPosition(pos) { stopPlayback(); state = PC.step(model, PC.clearLoop(state), pos - state.position); render(); }
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

  // ================= 比較 =================
  let compareReady = false;
  async function openCompare() {
    const data = await api("GET", "/api/videos");
    const withAnalysis = data.videos.filter((v) => v.latest_analysis);
    for (const [id, blank] of [["cmp-a", false], ["cmp-b", true]]) {
      const sel = $(id);
      clear(sel);
      if (blank) { const o = node("option", "A と同じ動画（前半と後半などを比べる）"); o.value = ""; sel.appendChild(o); }
      for (const v of withAnalysis) {
        const o = node("option", fmtDate(v.practice_date) + "　" + disciplineName(v.discipline) + (v.record ? "　" + v.record : "") + "　" + (v.notes || v.filename));
        o.value = v.id;
        sel.appendChild(o);
      }
    }
    if (!compareReady) {
      $("compare-form").addEventListener("submit", (e) => { e.preventDefault(); runCompare(); });
      compareReady = true;
    }
    $("cmp-result").hidden = true;
    if (!withAnalysis.length) toast("解析が終わった動画がまだありません");
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
      for (const b of r.compatibility.blocking) $("cmp-blocking").appendChild(node("li", "比べられません：" + b));
      for (const w of r.compatibility.warnings) $("cmp-warnings").appendChild(node("li", w));
      const name = Object.fromEntries(METRICS.map(([id, n, u]) => [id, [n, u]]));
      const fmt = (s, u) => (s ? (+s.median.toFixed(u === "°" ? 0 : 3)) + u : "—");
      for (const row of r.rows) {
        const [n, u] = name[row.metric_id] || [row.metric_id, ""];
        const card = node("div", null, "cmp-row");
        card.appendChild(node("strong", n));
        const cnt = (x, tag) => (x ? tag + " ・ " + x.n + "回分" + (x.excluded ? "（要確認など " + x.excluded + " 回を除外）" : "") : tag);
        const a = node("div"); a.appendChild(node("div", fmt(row.a, u), "v")); a.appendChild(node("div", cnt(row.a, "A"), "n")); card.appendChild(a);
        const b = node("div"); b.appendChild(node("div", fmt(row.b, u), "v")); b.appendChild(node("div", cnt(row.b, "B"), "n")); card.appendChild(b);
        const d = node("div"); d.appendChild(node("div", row.diff === null ? "—" : (row.diff > 0 ? "+" : "") + (+row.diff.toFixed(u === "°" ? 1 : 3)) + u, "v")); d.appendChild(node("div", "B − A", "n")); card.appendChild(d);
        card.appendChild(node("div", row.note, "note"));
        $("cmp-body").appendChild(card);
      }
    } catch (err) { toast("比べられませんでした: " + err.message, true); }
  }

  window.addEventListener("hashchange", route);
  route();
})();
