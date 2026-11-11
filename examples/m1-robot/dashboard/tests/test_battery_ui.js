const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

test("power panel renders six-hour battery history and charge estimates", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

  for (const id of ["battery-chart", "charge-rate", "charge-sample", "charge-eta-30", "charge-eta-full"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(source, /function drawBatteryChart/);
  assert.match(source, /battery_status/);
  assert.match(source, /6 \* 60 \* 60/);
  assert.match(source, /drawBatteryChart\(\)/);
});
