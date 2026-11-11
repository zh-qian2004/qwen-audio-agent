const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

test("overview exposes BPU utilization, frequency, users, and meter", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

  for (const id of ["bpu", "bpu-detail", "bpu-bar"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(source, /system\.bpu/);
  assert.match(source, /bpu\.frequency_hz/);
  assert.match(source, /bpu\.user_count/);
  assert.match(source, /bpu\.utilization_percent/);
});
