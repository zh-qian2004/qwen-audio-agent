// Run with: node --test tests/test_volume_ui.js (no browser or hardware needed).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function harness() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "0", disabled: true, textContent: "", hidden: true,
      checkValidity() { return true; }, reportValidity() {},
      classList: { toggle() {} },
      listeners: {},
      addEventListener(name, callback) { this.listeners[name] = callback; },
      querySelector() { return element(`${id}-label`); },
    });
    return elements.get(id);
  };
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: element, querySelector: element },
    fetch(url, options) {
      return new Promise(resolve => requests.push({ url, options, resolve }));
    },
    notify() {}, Date,
  });
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  vm.runInContext(source.slice(0, source.indexOf("function refreshCameraPreview")) +
    source.match(/^function text\(.*$/m)[0] + "\n" +
    source.slice(source.indexOf("function applyVolumeStatus"), source.indexOf("function render()")), context);
  return { context, element, requests, run: code => vm.runInContext(code, context) };
}

function respond(request, percent) {
  request.resolve({ ok: true, json: async () => ({
    ok: true, supported: true, percent, min: 0, max: 100, controls: ["DAC"], card: "duplexaudio",
  }) });
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("rapid changes serialize writes and keep the latest requested volume", async () => {
  const h = harness();
  const first = h.run("saveVolume(20)");
  h.run("saveVolume(30)");
  h.run("saveVolume(10)");
  assert.equal(h.requests.length, 1);
  respond(h.requests[0], 20);
  await tick();
  assert.equal(h.requests.length, 2);
  assert.equal(JSON.parse(h.requests[1].options.body).percent, 10);
  assert.notEqual(h.element("volume-value").textContent, "20%");
  respond(h.requests[1], 10);
  await first;
  assert.equal(h.element("volume-value").textContent, "10%");
  assert.equal(h.element("volume-slider").value, 10);
});

test("an older status response cannot undo a completed write", async () => {
  const h = harness();
  const read = h.run("loadVolume()");
  const write = h.run("saveVolume(15)");
  respond(h.requests[1], 15);
  await write;
  respond(h.requests[0], 35);
  await read;
  assert.equal(h.element("volume-value").textContent, "15%");
});

test("dragging previews locally and submits once on release", async () => {
  const h = harness();
  h.run("initVolumeControls()");
  const slider = h.element("volume-slider");
  slider.value = "25";
  slider.listeners.input();
  await h.run("loadVolume()");
  assert.equal(h.requests.length, 0);
  assert.equal(h.element("volume-value").textContent, "25%");
  slider.listeners.change();
  assert.equal(h.requests.length, 1);
  respond(h.requests[0], 25);
  await tick();
});

test("unavailable controls stay disabled and recover after a successful read", async () => {
  const h = harness();
  h.run('applyVolumeStatus({ supported: false, error: { message: "unavailable" } })');
  assert.equal(h.element("volume-slider").disabled, true);
  const read = h.run("loadVolume()");
  respond(h.requests[0], 20);
  await read;
  assert.equal(h.element("volume-slider").disabled, false);
  assert.equal(h.element("volume-value").textContent, "20%");
});

test("perceptual scale displays actual gain and preserves the hardware mute state", () => {
  const h = harness();
  h.run('applyVolumeStatus({ supported: true, percent: 0, muted: false, scale: "perceptual", db_gain: -93, controls: ["DAC"], card: "duplexaudio" })');
  assert.equal(h.element("volume-state-label").textContent, "正常");
  assert.match(h.element("volume-detail").textContent, /听感刻度.*-93\.0 dB/);
});

test("direct gain submits dB and refreshes both controls from readback", async () => {
  const h = harness();
  h.element("volume-db-input").value = "-32";
  const write = h.run("saveVolumeDb()");
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { db_gain: -32 });
  h.requests[0].resolve({ ok: true, json: async () => ({
    ok: true, supported: true, percent: 20, db_gain: -32,
    db_min: -95.5, db_max: 8, db_step: 0.5,
  }) });
  await write;
  assert.equal(h.element("volume-slider").value, 20);
  assert.equal(h.element("volume-db-input").value, -32);
  assert.equal(h.element("volume-db-input").disabled, false);
});

test("polling preserves an unsubmitted dB value", () => {
  const h = harness();
  h.run("initVolumeControls()");
  h.element("volume-db-input").value = "-35";
  h.element("volume-db-input").listeners.input();
  h.run('applyVolumeStatus({ supported: true, percent: 20, db_gain: -32 })');
  assert.equal(h.element("volume-db-input").value, "-35");
});

test("percent and dB requests use one ordered queue", async () => {
  const h = harness();
  const first = h.run("saveVolume(10)");
  h.element("volume-db-input").value = "-32";
  h.run("saveVolumeDb()");
  assert.equal(h.requests.length, 1);
  respond(h.requests[0], 10);
  await tick();
  assert.equal(h.requests.length, 2);
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { db_gain: -32 });
  respond(h.requests[1], 20);
  await first;
  assert.equal(h.element("volume-value").textContent, "20%");
});
