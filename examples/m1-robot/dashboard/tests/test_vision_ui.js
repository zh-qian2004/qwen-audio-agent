const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function harness() {
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const start = source.indexOf("function containRect");
  const end = source.indexOf("function clearVisionOverlay", start);
  const context = vm.createContext({ Number, Math, Array, Boolean });
  vm.runInContext(source.slice(start, end), context);
  return code => vm.runInContext(code, context);
}

test("contain mapping accounts for horizontal letterboxing", () => {
  const run = harness();
  const rect = run("containRect(800, 480, 640, 480)");
  assert.deepEqual({ ...rect }, { x: 80, y: 0, width: 640, height: 480 });
  const box = run("detectionCanvasBox({x1:.25,y1:.25,x2:.75,y2:.75}, containRect(800,480,640,480))");
  assert.deepEqual({ ...box }, { x: 240, y: 120, width: 320, height: 240 });
});

test("normalized boxes are clamped to visible content", () => {
  const run = harness();
  const box = run("detectionCanvasBox({x1:-1,y1:-1,x2:2,y2:2}, {x:10,y:20,width:100,height:50})");
  assert.deepEqual({ ...box }, { x: 10, y: 20, width: 100, height: 50 });
});

test("only fresh available detection payloads are drawable", () => {
  const run = harness();
  assert.equal(run("visionResultUsable({available:true,stale:false,result:{detections:[]}})"), true);
  assert.equal(run("visionResultUsable({available:true,stale:true,result:{detections:[]}})"), false);
  assert.equal(run("visionResultUsable({available:false})"), false);
});

test("camera markup includes overlay and detection controls", () => {
  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  for (const id of ["camera-stage", "detection-overlay", "vision-toggle", "vision-status", "vision-service-pill", "vision-processor-list"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /data-view="media">音视频流<\/button>/);
  assert.match(html, /data-view="video-services">Video Service<\/button>/);
  assert.match(html, /id="video-services"/);
});

test("vision service panel helpers format rates and BPU frequency", () => {
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  const start = source.indexOf("function visionProcessorSummary");
  const end = source.indexOf("function renderVisionServicePanel", start);
  const context = vm.createContext({ Number });
  vm.runInContext(source.slice(start, end), context);
  assert.equal(vm.runInContext("visionProcessorSummary({input_rate_hz:9.96,output_rate_hz:9.91})", context), "10.0 → 9.9 Hz");
  assert.equal(vm.runInContext("visionFrequency(1000000000)", context), "1.00 GHz");
  assert.equal(vm.runInContext("visionFrequency(null)", context), "—");
});
