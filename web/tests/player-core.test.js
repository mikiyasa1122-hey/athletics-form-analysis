"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const PC = require("../player-core.js");

const os = require("node:os");
const { execFileSync } = require("node:child_process");

// テストのたびに現在のPythonエンジンでデモ解析を作り直す（古いJSONで通らないように）
const DEMO = fs.mkdtempSync(path.join(os.tmpdir(), "athform-demo-"));
execFileSync(process.env.PYTHON || "python3", ["-m", "athform", "demo", "--out", DEMO],
  { cwd: path.join(__dirname, "..", ".."), stdio: "ignore" });
test.after(() => fs.rmSync(DEMO, { recursive: true, force: true }));
function demo(name) {
  return JSON.parse(fs.readFileSync(path.join(DEMO, name + ".json"), "utf8"));
}
function clone(o) {
  return JSON.parse(JSON.stringify(o));
}

test("全フレームで表示フレームID・PTS・骨格が同じ原本フレームを指す（AC-011, NFR-PLAY-001）", () => {
  for (const name of ["sprint-240fps", "sprint-30fps", "sprint-vfr", "sprint-slowmo-export"]) {
    const snap = demo(name);
    const model = PC.loadSnapshot(snap);
    let s = PC.createState();
    for (let i = 0; i < model.frames.length; i++) {
      const v = PC.view(model, s);
      const src = snap.frames.find((f) => f.presentation_index === i);
      assert.equal(v.frameId, src.source_frame_id);
      assert.equal(v.pts, src.pts);
      assert.equal(v.sourceTime, src.t);
      const hasPose = snap.pose_raw.some((p) => p.source_frame_id === src.source_frame_id);
      // 骨格がある原本フレームでは必ず表示し、そのIDは表示中のフレームと一致する
      assert.equal(v.overlay !== null, hasPose, `frame ${v.frameId}`);
      if (v.overlay) assert.equal(v.overlayFrameId, v.frameId);
      s = PC.step(model, s, 1);
    }
    // 逆方向にも1フレームずつ戻れる
    for (let i = model.frames.length - 1; i >= 0; i--) {
      assert.equal(PC.view(model, s).position, i);
      s = PC.step(model, s, -1);
    }
    assert.equal(PC.view(model, s).position, 0);
  }
});

test("骨格がないフレームでは近傍フレームの骨格を出さない（FR-PLAY-003）", () => {
  const snap = demo("sprint-240fps");
  snap.pose_raw = snap.pose_raw.filter((p) => p.source_frame_id !== 10);
  const model = PC.loadSnapshot(snap);
  const s = PC.seekFrameId(model, PC.createState(), 10);
  const v = PC.view(model, s);
  assert.equal(v.frameId, 10);
  assert.equal(v.overlay, null);
  assert.equal(v.overlayFrameId, null);
});

test("再生倍率を変えてもフレームID・元動画時刻・解析データは変わらない（AC-010）", () => {
  const snap = demo("sprint-240fps");
  const before = clone(snap);
  const model = PC.loadSnapshot(snap);
  let s = PC.seekFrameId(model, PC.createState(), 42);
  const base = PC.view(model, s);
  const delays = {};
  for (const r of PC.RATES) {
    s = PC.setRate(s, r);
    const v = PC.view(model, s);
    assert.equal(v.frameId, base.frameId);
    assert.equal(v.sourceTime, base.sourceTime);
    assert.equal(v.pts, base.pts);
    assert.deepEqual(v.overlay, base.overlay);
    delays[r] = PC.nextDelayMs(model, s);
  }
  assert.deepEqual(snap, before); // 解析データは書き換わっていない
  assert.ok(Object.isFrozen(model.snapshot.metrics[0]));
  // 待ち時間だけが倍率に反比例して変わる
  assert.ok(Math.abs(delays[1] - 1000 / 240) < 1e-9);
  assert.ok(Math.abs(delays[0.25] - 4 * delays[1]) < 1e-9);
  assert.ok(Math.abs(delays[0.1] - 10 * delays[1]) < 1e-9);
});

test("対応外の再生倍率は拒否する", () => {
  assert.throws(() => PC.setRate(PC.createState(), 2));
  assert.throws(() => PC.setRate(PC.createState(), 0));
});

test("VFRでは次フレームまでの待ち時間がPTS差 / 倍率になる（固定FPS前提にしない）", () => {
  const snap = demo("sprint-vfr");
  const model = PC.loadSnapshot(snap);
  let s = PC.setRate(PC.createState(), 0.5);
  const seen = new Set();
  for (let i = 0; i < 30; i++) {
    const a = model.frames[i], b = model.frames[i + 1];
    const expected = ((PC.parseFraction(b.t_exact) - PC.parseFraction(a.t_exact)) * 1000) / 0.5;
    const got = PC.nextDelayMs(model, s);
    assert.ok(Math.abs(got - expected) < 1e-9, `frame ${i}: ${got} != ${expected}`);
    seen.add(got.toFixed(6));
    s = PC.tick(model, s);
  }
  assert.ok(seen.size > 3, "VFR素材なので待ち時間は一定でない");
});

test("再生の経過時間は元動画時刻と別に積算される（FR-PLAY-005）", () => {
  const model = PC.loadSnapshot(demo("sprint-240fps"));
  let s = PC.setPlaying(PC.setRate(PC.createState(), 0.25), true);
  for (let i = 0; i < 24; i++) s = PC.tick(model, s);
  const v = PC.view(model, s);
  assert.ok(Math.abs(v.sourceTime - 24 / 240) < 1e-9);
  assert.ok(Math.abs(v.playbackElapsedMs - 400) < 1e-6); // 0.1秒の動きを0.4秒かけて表示
});

test("末尾で再生が止まり、ループ中は範囲内を回る（FR-PLAY-006）", () => {
  const model = PC.loadSnapshot(demo("sprint-30fps"));
  let s = PC.setPlaying(PC.seekFrameId(model, PC.createState(), model.frames.length - 1), true);
  assert.equal(PC.nextDelayMs(model, s), null);
  s = PC.tick(model, s);
  assert.equal(s.playing, false);

  const ev = model.events[0];
  s = PC.setPlaying(PC.loopAroundEvent(model, PC.createState(), ev, 3), true);
  assert.equal(PC.view(model, s).frameId, ev.evidence_frame_id);
  const [lo, hi] = s.loop;
  for (let i = 0; i < 50; i++) {
    s = PC.tick(model, s);
    assert.ok(s.position >= lo && s.position <= hi);
  }
  s = PC.step(model, s, 1000);
  assert.equal(s.position, hi);
  assert.equal(PC.clearLoop(s).loop, null);
});

test("存在しないフレームへの移動・不正なスナップショットは拒否する", () => {
  const model = PC.loadSnapshot(demo("sprint-30fps"));
  assert.throws(() => PC.seekFrameId(model, PC.createState(), 99999));
  assert.throws(() => PC.step(model, PC.createState(), 0.5));
  assert.throws(() => PC.loadSnapshot({}));
  assert.throws(() => PC.loadSnapshot({ frames: [] }));
  const dup = demo("sprint-30fps");
  dup.frames[1].source_frame_id = dup.frames[0].source_frame_id;
  assert.throws(() => PC.loadSnapshot(dup));
});

test("指標の根拠フレームはすべて移動できる（FR-SINGLE-003）", () => {
  const model = PC.loadSnapshot(demo("sprint-240fps"));
  for (const m of model.metrics) {
    for (const id of m.evidence_refs.frame_ids) {
      const v = PC.view(model, PC.seekFrameId(model, PC.createState(), id));
      assert.equal(v.frameId, id);
    }
  }
});
