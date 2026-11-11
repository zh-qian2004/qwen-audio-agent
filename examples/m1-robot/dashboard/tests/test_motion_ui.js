// Run with: node --test tests/test_motion_ui.js (no browser or hardware needed).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function harness() {
  const context = vm.createContext({ Map });
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const preambleEnd = source.indexOf("const phaseLabels");
  const functionStart = source.indexOf("function registerMotionDefinitions");
  const functionEnd = source.indexOf("function buildMotionActions", functionStart);
  vm.runInContext(source.slice(0, preambleEnd) + source.slice(functionStart, functionEnd), context);
  return code => vm.runInContext(code, context);
}

test("direction-pad commands are registered with the other motion definitions", () => {
  const run = harness();
  run(`registerMotionDefinitions({ categories: [
    { id: "direction", commands: [
      { id: "move_forward" }, { id: "move_backward" },
      { id: "strafe_left" }, { id: "strafe_right" },
      { id: "rotate_left" }, { id: "rotate_right" }
    ] },
    { id: "body", commands: [{ id: "get_up" }] }
  ] })`);
  assert.deepEqual(
    Array.from(run("motionView.defs.keys()")),
    ["move_forward", "move_backward", "strafe_left", "strafe_right", "rotate_left", "rotate_right", "get_up"],
  );
});

test("missing command arrays are ignored safely", () => {
  const run = harness();
  run('registerMotionDefinitions({ categories: [{ id: "direction" }] })');
  assert.equal(run("motionView.defs.size"), 0);
});
