"use strict";
// 画面JSの静的検査: 参照する要素IDがHTMLにあること、解析データをHTMLとして挿入しないこと、外部スクリプトがないこと。
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const WEB = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const app = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const core = fs.readFileSync(path.join(WEB, "player-core.js"), "utf8");

test("app.js が参照する要素IDはすべて index.html にある", () => {
  const ids = [...app.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]);
  assert.ok(ids.length >= 15);
  for (const id of ids) assert.ok(html.includes(`id="${id}"`), `index.html に id="${id}" がない`);
});

test("HTML文字列として挿入するAPIを使わない", () => {
  for (const [name, src] of [["app.js", app], ["player-core.js", core]]) {
    for (const sink of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
      assert.ok(!src.includes(sink), `${name} が ${sink} を使っている`);
    }
  }
});

test("外部スクリプト・外部スタイルを読み込まない", () => {
  const srcs = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(srcs, ["player-core.js", "app.js"]);
  assert.ok(!/href="https?:/.test(html));
});

test("プレイヤーは <video>.currentTime の概算シークに依存しない", () => {
  assert.ok(!app.includes("currentTime"));
  assert.ok(!app.includes("playbackRate"));
});
