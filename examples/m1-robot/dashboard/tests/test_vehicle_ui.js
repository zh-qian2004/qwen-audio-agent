const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

test("cart panel distinguishes feedback, wheel motion, and mount state", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

  for (const id of ["vehicle-link", "vehicle-motion", "vehicle-mount", "vehicle-mount-note"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(source, /stateView\.state\.vehicle_status/);
  assert.match(source, /mount\.known/);
  assert.match(source, /wheelMotion\.state/);
});
