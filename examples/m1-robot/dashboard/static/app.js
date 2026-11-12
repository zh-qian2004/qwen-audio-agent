"use strict";

const $ = id => document.getElementById(id);
const stateView = { metadata: null, state: null, ws: null, lastSlowRender: 0 };
const histories = { joints: [], imu: [], battery: [] };
const statusLabels = { online: "在线", stale: "数据过期", no_data: "无数据", offline: "离线", starting: "启动中", error: "错误" };
const groupColors = { left_arm: "#0d9488", right_arm: "#2f7fd6", left_leg: "#7a5cd6", right_leg: "#d98324" };
const cameraView = {
  timer: null, failures: 0, visionStatusTimer: null, visionResultTimer: null,
  processorState: "unknown", result: null, controlling: false, resizeObserver: null,
};
const visionColors = { person: "#22a55c", head: "#2f7fd6", face: "#d98324", hand: "#7a5cd6" };
const visionLabels = { person: "人体", head: "头部", face: "人脸", hand: "手部" };
const visionProcessorLabels = { object_detection: "目标检测" };
const visionProcessorStateLabels = {
  stopped: "已停止", starting: "启动中", running: "运行中", degraded: "结果流异常", failed: "启动失败",
};
const motionView = {
  capabilities: null,
  defs: new Map(),
  history: [],
  historyTimer: null,
  hold: null,
  confirmResolve: null,
  lastRender: 0,
  lastPhase: null,
  lastHistoryRender: 0,
  stopPromise: null,
};
const phaseLabels = {
  created: "已创建",
  sending: "发送中",
  vendor_accepted: "厂商已受理",
  observing: "等待 ROS 观测",
  observed_running: "ROS 观测到运动",
  observed_stopped: "ROS 观测到停止",
  completed: "已完成",
  vendor_rejected: "厂商拒绝",
  connection_failed: "连接失败",
  timed_out: "观测超时",
  state_mismatch: "状态不一致",
  cancelled: "已取消",
  emergency_stop: "急停触发",
  emergency_reset: "急停解除",
};
const phaseFlow = ["created", "sending", "vendor_accepted", "observing", "observed_running", "observed_stopped", "completed"];
const terminalMotionPhases = ["completed", "timed_out", "vendor_rejected", "connection_failed", "state_mismatch", "cancelled"];
const motionActionCategories = ["body", "head_arm", "business", "mode", "extended"];
const interactionLabels = { hold: "按住", trigger: "单次", toggle: "切换", workflow: "长任务" };
const ttsView = { config: null, limits: null, submitting: false, historySignature: "", lastErrorSignature: "", controlsSignature: "" };
const volumeView = { dragging: false, saving: false, pending: null, revision: 0, dbDirty: false };
const ttsPhaseLabels = {
  queued: "等待播放",
  synthesizing: "生成语音",
  decoding: "音频解码",
  publishing: "提交音频",
  waiting_playback: "等待播放完成",
  succeeded: "播放成功",
  failed: "播放失败",
  cancelled: "已取消",
  playing: "播放中",
};
const ttsActivePhases = ["queued", "synthesizing", "decoding", "publishing", "waiting_playback"];


function containRect(containerWidth, containerHeight, contentWidth, contentHeight) {
  if (![containerWidth, containerHeight, contentWidth, contentHeight].every(value => Number(value) > 0)) {
    return { x: 0, y: 0, width: 0, height: 0 };
  }
  const scale = Math.min(containerWidth / contentWidth, containerHeight / contentHeight);
  const width = contentWidth * scale, height = contentHeight * scale;
  return { x: (containerWidth - width) / 2, y: (containerHeight - height) / 2, width, height };
}

function detectionCanvasBox(normalized, rect) {
  const clamp = value => Math.max(0, Math.min(1, Number(value) || 0));
  const x1 = clamp(normalized?.x1), y1 = clamp(normalized?.y1);
  const x2 = clamp(normalized?.x2), y2 = clamp(normalized?.y2);
  return {
    x: rect.x + x1 * rect.width,
    y: rect.y + y1 * rect.height,
    width: Math.max(0, (x2 - x1) * rect.width),
    height: Math.max(0, (y2 - y1) * rect.height),
  };
}

function visionResultUsable(payload) {
  return Boolean(payload?.available && !payload?.stale && payload?.result && Array.isArray(payload.result.detections));
}

function visionProcessorSummary(processor) {
  const input = Number(processor?.input_rate_hz || 0).toFixed(1);
  const output = Number(processor?.output_rate_hz || 0).toFixed(1);
  return `${input} → ${output} Hz`;
}

function visionFrequency(value) {
  const hz = Number(value);
  if (!Number.isFinite(hz) || hz <= 0) return "—";
  return hz >= 1e9 ? `${(hz / 1e9).toFixed(2)} GHz` : `${Math.round(hz / 1e6)} MHz`;
}

function renderVisionServicePanel(data) {
  const online = Boolean(data?.online);
  const processors = Array.isArray(data?.processors) ? data.processors : [];
  const running = processors.filter(item => ["starting", "running", "degraded"].includes(item.state)).length;
  const pill = $("vision-service-pill");
  if (pill) {
    pill.className = `status-pill ${!online ? "offline" : data?.status === "degraded" ? "warn" : "online"}`;
    pill.querySelector("span").textContent = !online ? "离线" : data?.status === "degraded" ? "降级" : "在线";
  }
  text("vision-service-version", online ? `v${data.version || "—"}` : "服务离线");
  text("vision-service-uptime", online ? `已运行 ${duration(data.uptime_seconds)}` : (data?.error || "无法连接"));
  text("vision-service-processors", `${running} / ${processors.length}`);
  const resources = data?.resources || {};
  const rss = resources.rss_kib === null || resources.rss_kib === undefined ? "—" : `${(resources.rss_kib / 1024).toFixed(1)} MB`;
  text("vision-service-resource", online ? `${number(resources.cpu_percent, 1, "%")} / ${rss}` : "—");
  const bpu = resources.bpu || {};
  text("vision-service-bpu", bpu.available ? number(bpu.utilization_percent, 0, "%") : "—");
  text("vision-service-bpu-detail", bpu.available ? `利用率 / ${visionFrequency(bpu.frequency_hz)}` : "指标不可用");
  const ros = data?.ros || {};
  text("vision-service-ros", `ROS：${ros.available ? "在线" : "离线"}${ros.node ? ` · ${ros.node}` : ""}`);
  const bus = data?.result_bus || {};
  text("vision-service-bus", `结果总线：${bus.published ?? 0} 帧 · 最近 ${age(bus.latest_age_seconds)}`);
  text("vision-service-endpoint", `服务地址：${data?.service_url || "—"}`);

  const list = $("vision-processor-list");
  if (!list) return;
  list.replaceChildren();
  if (!online || !processors.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = online ? "当前没有注册视频处理服务" : "vision_service 不可用，视频预览仍可独立工作";
    list.append(empty);
    return;
  }
  for (const processor of processors) {
    const item = document.createElement("div");
    item.className = `vision-processor-item ${processor.state || "stopped"}`;
    const name = document.createElement("div"); name.className = "vision-processor-name";
    const nameStrong = document.createElement("strong"); nameStrong.textContent = visionProcessorLabels[processor.id] || processor.id || "未命名 Processor";
    const state = document.createElement("span"); state.className = "vision-processor-state"; state.textContent = visionProcessorStateLabels[processor.state] || processor.state || "未知";
    name.append(nameStrong, state);
    const stream = document.createElement("div"); stream.className = "vision-processor-stream";
    const source = document.createElement("span"); source.textContent = `输入 ${processor.source || "—"}`;
    const topicLine = document.createElement("span"); topicLine.textContent = `结果 ${processor.result_topic || "—"}`;
    const model = document.createElement("span"); model.textContent = `模型 ${processor.model || "—"}`;
    stream.append(source, topicLine, model);
    const rate = document.createElement("div"); rate.className = "vision-processor-rate";
    const rateStrong = document.createElement("strong"); rateStrong.textContent = visionProcessorSummary(processor);
    const counters = document.createElement("span"); counters.textContent = `输入 ${processor.input_messages || 0} · 输出 ${processor.output_frames || 0} · 错误 ${processor.parse_errors || 0}`;
    rate.append(rateStrong, counters);
    item.append(name, stream, rate);
    if (processor.id === "object_detection") {
      const action = document.createElement("button");
      const active = ["starting", "running", "degraded"].includes(processor.state);
      action.type = "button"; action.className = `vision-toggle vision-processor-action${active ? " running" : ""}`;
      action.disabled = cameraView.controlling;
      action.textContent = active ? "停止" : "启动";
      action.addEventListener("click", () => setVisionDetection(active ? "stop" : "start"));
      item.append(action);
    }
    list.append(item);
  }
}

function clearVisionOverlay(summary) {
  const canvas = $("detection-overlay");
  if (canvas) canvas.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
  if (summary !== undefined) text("vision-result-summary", summary);
}

function drawVisionOverlay() {
  const canvas = $("detection-overlay"), stage = $("camera-stage"), img = $("camera-preview");
  if (!canvas || !stage || !img || !img.naturalWidth || !img.naturalHeight) return;
  const cssWidth = stage.clientWidth, cssHeight = stage.clientHeight;
  if (!cssWidth || !cssHeight) return;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.max(1, Math.round(cssWidth * ratio));
  const pixelHeight = Math.max(1, Math.round(cssHeight * ratio));
  if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
    canvas.width = pixelWidth; canvas.height = pixelHeight;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  if (!cameraView.result) return;
  const rect = containRect(cssWidth, cssHeight, img.naturalWidth, img.naturalHeight);
  ctx.font = '600 12px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textBaseline = "top";
  for (const detection of cameraView.result.detections || []) {
    const box = detectionCanvasBox(detection.bbox_normalized, rect);
    if (box.width < 2 || box.height < 2) continue;
    const color = visionColors[detection.label] || "#0d9488";
    const label = `${visionLabels[detection.label] || detection.label || "目标"} ${(Number(detection.confidence || 0) * 100).toFixed(0)}%`;
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.strokeRect(box.x, box.y, box.width, box.height);
    const labelWidth = Math.ceil(ctx.measureText(label).width) + 10;
    const labelY = Math.max(rect.y, box.y - 21);
    ctx.fillStyle = color; ctx.fillRect(box.x, labelY, labelWidth, 20);
    ctx.fillStyle = "#fff"; ctx.fillText(label, box.x + 5, labelY + 3);
  }
}

function refreshCameraPreview() {
  const img = $("camera-preview"), status = $("camera-preview-status");
  if (!img) return;
  img.onload = () => { cameraView.failures = 0; if (status) status.textContent = "实时画面 · 约 2 fps"; drawVisionOverlay(); };
  img.onerror = () => { cameraView.failures += 1; if (status && cameraView.failures > 1) status.textContent = "无数据（相机流未启动）"; };
  img.src = `/api/camera/frame?t=${Date.now()}`;
}

function startCameraPreview() {
  if (cameraView.timer) return;
  refreshCameraPreview();
  cameraView.timer = setInterval(refreshCameraPreview, 500);
}

function stopCameraPreview() {
  if (cameraView.timer) { clearInterval(cameraView.timer); cameraView.timer = null; }
}

function applyVisionStatus(data) {
  const button = $("vision-toggle"), dot = $("vision-dot"), status = $("vision-status");
  const processor = (data?.processors || []).find(item => item.id === "object_detection");
  const online = Boolean(data?.online);
  cameraView.processorState = processor?.state || (online ? "stopped" : "offline");
  const active = ["starting", "running", "degraded"].includes(cameraView.processorState);
  if (button) {
    button.disabled = !online || cameraView.controlling;
    button.textContent = cameraView.controlling ? "处理中…" : active ? "停止目标检测" : "启动目标检测";
    button.classList.toggle("running", active);
  }
  if (dot) dot.className = `vision-dot ${!online ? "offline" : cameraView.processorState === "running" ? "running" : cameraView.processorState === "degraded" ? "degraded" : ""}`;
  if (status) {
    if (!online) status.textContent = "视觉服务离线";
    else if (cameraView.processorState === "running") status.textContent = `检测运行中 · ${Number(processor.output_rate_hz || 0).toFixed(1)} Hz`;
    else if (cameraView.processorState === "degraded") status.textContent = "检测结果流异常";
    else status.textContent = "视觉服务在线 · 检测已停止";
  }
  renderVisionServicePanel(data);
  if (!active) { cameraView.result = null; clearVisionOverlay(online ? "检测未启动" : "视觉服务离线"); }
}

async function refreshVisionStatus() {
  try {
    const response = await fetch(`/api/vision/status?t=${Date.now()}`, { cache: "no-store" });
    applyVisionStatus(await response.json());
  } catch (_error) {
    applyVisionStatus({ online: false, processors: [] });
  }
}

async function refreshVisionDetections() {
  if (!["starting", "running", "degraded"].includes(cameraView.processorState)) return;
  try {
    const response = await fetch(`/api/vision/detections/latest?t=${Date.now()}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !visionResultUsable(payload)) {
      cameraView.result = null;
      clearVisionOverlay(payload?.stale ? "检测结果已过期" : "等待检测结果");
      return;
    }
    cameraView.result = payload.result;
    text("vision-result-summary", `${payload.result.detection_count || 0} 个目标 · ${Math.round((payload.age_seconds || 0) * 1000)} ms`);
    drawVisionOverlay();
  } catch (_error) {
    cameraView.result = null;
    clearVisionOverlay("检测结果不可用");
  }
}

async function setVisionDetection(action) {
  if (cameraView.controlling) return;
  cameraView.controlling = true;
  applyVisionStatus({ online: true, processors: [{ id: "object_detection", state: cameraView.processorState }] });
  try {
    const response = await fetch(`/api/vision/processors/object_detection/${action}`, { method: "POST" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  } catch (_error) {
    text("vision-result-summary", "检测控制失败");
  } finally {
    cameraView.controlling = false;
    await refreshVisionStatus();
    if (action === "start") await refreshVisionDetections();
  }
}

function startVisionOverlay() {
  refreshVisionDetections();
  if (!cameraView.visionResultTimer) cameraView.visionResultTimer = setInterval(refreshVisionDetections, 200);
}

function stopVisionOverlay() {
  if (cameraView.visionResultTimer) { clearInterval(cameraView.visionResultTimer); cameraView.visionResultTimer = null; }
  clearVisionOverlay();
}

function startVisionStatus() {
  refreshVisionStatus();
  if (!cameraView.visionStatusTimer) cameraView.visionStatusTimer = setInterval(refreshVisionStatus, 1000);
}

function stopVisionStatus() {
  if (cameraView.visionStatusTimer) { clearInterval(cameraView.visionStatusTimer); cameraView.visionStatusTimer = null; }
}

function initVisionControls() {
  $("vision-toggle")?.addEventListener("click", () => {
    const active = ["starting", "running", "degraded"].includes(cameraView.processorState);
    setVisionDetection(active ? "stop" : "start");
  });
  const stage = $("camera-stage");
  if (stage && window.ResizeObserver) {
    cameraView.resizeObserver = new ResizeObserver(drawVisionOverlay);
    cameraView.resizeObserver.observe(stage);
  } else {
    window.addEventListener("resize", drawVisionOverlay);
  }
  refreshVisionStatus();
}

const audioView = { timer: null, history: [] };

function refreshAudioLevel() {
  fetch(`/api/audio/level?t=${Date.now()}`)
    .then(r => r.json())
    .then(data => {
      const bar = $("mic-level-bar"), dbEl = $("mic-level-db"), st = $("mic-status");
      const db = data.level_db;
      if (bar) bar.style.width = (db === null ? 0 : Math.max(2, Math.min(100, (db + 60) / 60 * 100))) + "%";
      if (dbEl) dbEl.textContent = db === null ? "—" : db.toFixed(1);
      const online = data.online !== undefined ? data.online : data.active;
      if (st) st.textContent = data.error_code === "audio_service_unavailable"
        ? "音频服务离线"
        : (online ? `实时流 · ${data.topic}` : (data.error ? "音频流异常" : "等待音频流"));
      text("mic-source", `${data.source_label || data.device} · ${data.sample_rate_hz || "—"} Hz · ${data.channels || "—"} 通道 · 峰值 ${data.peak_db === null || data.peak_db === undefined ? "—" : data.peak_db.toFixed(1) + " dBFS"}`);
      const channelGrid = $("mic-channel-grid");
      if (channelGrid) {
        channelGrid.replaceChildren();
        (data.channel_levels_db || []).forEach((level, index) => {
          const item = document.createElement("div");
          const label = document.createElement("span"); label.textContent = `CH ${index + 1}`;
          const value = document.createElement("strong"); value.textContent = level === null ? "—" : `${level.toFixed(1)} dB`;
          const meter = document.createElement("i"); meter.style.width = `${level === null ? 0 : Math.max(1, Math.min(100, (level + 60) / 60 * 100))}%`;
          item.append(label, value, meter); channelGrid.append(item);
        });
      }
      audioView.history = data.history || [];
      drawMicHistory(audioView.history);
    })
    .catch(() => { });
}

function drawMicHistory(history) {
  const canvas = $("mic-history-canvas"); if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight || 90;
  if (!w) return;
  canvas.width = w * ratio; canvas.height = h * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = "#e2e8f0"; ctx.lineWidth = 1;
  [-20, -40].forEach(db => {
    const y = (db + 60) / 60 * h;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  });
  ctx.fillStyle = "#93a2b1"; ctx.font = "9px monospace";
  ctx.fillText("-20 dB", 4, (-20 + 60) / 60 * h - 3);
  ctx.fillText("-40 dB", 4, (-40 + 60) / 60 * h - 3);
  if (!history || history.length < 2) return;
  const step = w / 600, start = Math.max(0, history.length - 600);
  ctx.strokeStyle = "#0d9488"; ctx.lineWidth = 1.5; ctx.beginPath();
  for (let i = start; i < history.length; i++) {
    const db = Math.max(-60, Math.min(0, history[i]));
    const x = w - (history.length - i) * step;
    const y = h - (db + 60) / 60 * h;
    i === start ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function startAudioLevel() {
  if (audioView.timer) return;
  refreshAudioLevel();
  audioView.timer = setInterval(refreshAudioLevel, 500);
}

function stopAudioLevel() {
  if (audioView.timer) { clearInterval(audioView.timer); audioView.timer = null; }
}

const audioServicesView = { timer: null, loading: false };

function audioServiceStatusLabel(status) {
  return ({
    online: "在线", idle: "待机", mock: "Mock", degraded: "降级",
    offline: "离线", stale: "心跳过期", not_configured: "未配置", ok: "正常",
  })[status] || status || "未知";
}

function audioServiceStatusClass(status) {
  if (["online", "ok"].includes(status)) return "online";
  if (["idle", "mock", "degraded", "stale", "not_configured"].includes(status)) return "warn";
  return "offline";
}

function audioServiceMetricValue(metric) {
  const value = metric?.value;
  if (value === null || value === undefined) return "—";
  if (typeof value === "number" && !Number.isInteger(value)) {
    return `${value.toFixed(1)}${metric.unit ? " " + metric.unit : ""}`;
  }
  return `${value}${metric.unit ? " " + metric.unit : ""}`;
}

function renderAudioServiceList(items) {
  const container = $("audio-services-grid");
  if (!container) return;
  container.replaceChildren();
  (items || []).forEach(item => {
    const card = document.createElement("article");
    card.className = `card audio-service-item is-${item.status || "unknown"}`;
    const heading = document.createElement("div");
    heading.className = "audio-service-item-heading";
    const titleBox = document.createElement("div");
    const role = document.createElement("span"); role.textContent = item.role || "服务";
    const title = document.createElement("strong"); title.textContent = item.name || item.id;
    titleBox.append(role, title);
    const badge = document.createElement("span");
    badge.className = `audio-service-badge ${audioServiceStatusClass(item.status)}`;
    badge.textContent = audioServiceStatusLabel(item.status);
    heading.append(titleBox, badge);
    const detail = document.createElement("p"); detail.textContent = item.detail || "—";
    card.append(heading, detail);
    if (item.implementation) {
      const implementation = document.createElement("code");
      implementation.textContent = item.implementation;
      card.append(implementation);
    }
    if (item.metrics?.length) {
      const metrics = document.createElement("div"); metrics.className = "audio-service-metrics";
      item.metrics.forEach(metric => {
        const cell = document.createElement("div");
        const label = document.createElement("span"); label.textContent = metric.label;
        const value = document.createElement("strong"); value.textContent = audioServiceMetricValue(metric);
        cell.append(label, value); metrics.append(cell);
      });
      card.append(metrics);
    }
    container.append(card);
  });
}

function renderAudioServiceConnections(items) {
  const container = $("audio-service-connections");
  if (!container) return;
  container.replaceChildren();
  if (!items?.length) {
    const empty = document.createElement("p"); empty.className = "empty-note"; empty.textContent = "暂无连接";
    container.append(empty); return;
  }
  items.forEach(item => {
    const row = document.createElement("div"); row.className = "audio-service-list-row";
    const copy = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = item.name || "未命名消费者";
    const detail = document.createElement("span");
    detail.textContent = `${item.policy || "—"} · 队列 ${item.queue_depth ?? "—"}/${item.capacity ?? "—"}`;
    copy.append(name, detail);
    const metrics = document.createElement("small");
    metrics.textContent = `丢帧 ${item.dropped_frames ?? 0} · 延迟 ${item.lag_ms == null ? "—" : Number(item.lag_ms).toFixed(1) + " ms"}`;
    row.append(copy, metrics); container.append(row);
  });
}

function renderAudioServiceSessions(items, availableTypes) {
  const container = $("audio-service-session-list");
  if (!container) return;
  container.replaceChildren();
  if (!items?.length) {
    const empty = document.createElement("p"); empty.className = "empty-note";
    empty.textContent = availableTypes?.length
      ? `暂无运行会话 · 已注册 ${availableTypes.join(" / ")}`
      : "暂无运行中的 ASR/TSE 会话";
    container.append(empty); return;
  }
  items.forEach(item => {
    const row = document.createElement("div"); row.className = "audio-service-list-row";
    const copy = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = item.processor_type || item.session_id;
    const detail = document.createElement("span"); detail.textContent = item.session_id || "—";
    copy.append(name, detail);
    const metrics = document.createElement("small");
    metrics.textContent = `${audioServiceStatusLabel(item.state)} · ${item.received_frames ?? 0} 帧 · 丢帧 ${item.dropped_frames ?? 0}`;
    row.append(copy, metrics); container.append(row);
  });
}

function applyAudioServices(data) {
  const pill = $("audio-service-pill");
  const service = data.service || {};
  if (pill) {
    pill.className = `status-pill ${data.online ? audioServiceStatusClass(service.status) : "offline"}`;
    pill.querySelector("span").textContent = data.online ? audioServiceStatusLabel(service.status) : "离线";
  }
  text("audio-service-version", service.version || "—");
  text("audio-service-uptime", `运行时间 ${service.uptime_seconds == null ? "—" : duration(service.uptime_seconds)}`);
  text("audio-service-capture", data.summary?.capture_online ? "在线" : "离线");
  text("audio-service-consumers", raw(data.summary?.consumer_count));
  text("audio-service-sessions", raw(data.summary?.session_count));
  text("audio-service-output", data.summary?.tts_available && data.summary?.playback_available ? "正常" : "降级");
  const agent = data.audio_agent || {};
  const realtime = agent.gateway?.realtime || {};
  const backend = agent.gateway?.backend || {};
  const deployment = { cloud: "云端", edge: "端侧" }[agent.deployment_mode] || "未标记";
  text("audio-agent-state", audioServiceStatusLabel(agent.state));
  text("audio-agent-models", `${deployment} · ${realtime.model || realtime.provider || "前台未连接"} / ${backend.model || backend.label || backend.kind || "后台未配置"}`);
  renderAudioServiceList(data.services);
  renderAudioServiceConnections(data.connections);
  renderAudioServiceSessions(data.sessions, data.available_types);
  const error = $("audio-service-error");
  if (error) {
    error.hidden = data.online;
    error.textContent = data.online ? "" : `audio_service 不可达：${service.error || service.url || "未知错误"}`;
  }
}

async function refreshAudioServices() {
  if (audioServicesView.loading) return;
  audioServicesView.loading = true;
  const button = $("audio-service-refresh");
  if (button) button.disabled = true;
  try {
    const response = await fetch(`/api/audio/services?t=${Date.now()}`, { cache: "no-store" });
    const data = await response.json();
    applyAudioServices(data);
  } catch (error) {
    applyAudioServices({
      online: false,
      service: { status: "offline", error: error.message },
      summary: {}, services: [], connections: [], sessions: [], available_types: [],
    });
  } finally {
    audioServicesView.loading = false;
    if (button) button.disabled = false;
  }
}

function startAudioServices() {
  refreshAudioServices();
  if (!audioServicesView.timer) audioServicesView.timer = setInterval(refreshAudioServices, 2000);
}

function stopAudioServices() {
  if (audioServicesView.timer) { clearInterval(audioServicesView.timer); audioServicesView.timer = null; }
}

function initAudioServiceControls() {
  $("audio-service-refresh")?.addEventListener("click", refreshAudioServices);
}

function initViewTabs() {
  document.querySelectorAll("#view-tabs .tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll("#view-tabs .tab").forEach(item => item.classList.toggle("active", item === tab));
    document.querySelectorAll("main .view").forEach(view => view.classList.toggle("active", view.id === tab.dataset.view));
    if (tab.dataset.view === "media") { startCameraPreview(); startAudioLevel(); startVisionOverlay(); }
    else { stopCameraPreview(); stopAudioLevel(); stopVisionOverlay(); }
    if (["media", "video-services"].includes(tab.dataset.view)) startVisionStatus();
    else stopVisionStatus();
    if (tab.dataset.view === "audio-services") startAudioServices();
    else stopAudioServices();
    if (tab.dataset.view === "motion") startMotionPolling();
    else { stopMotionPolling(); releaseMotionHold("label switched"); }
  }));
}

function text(id, value) { const element = $(id); if (element) element.textContent = value; }
function number(value, digits = 1, suffix = "") { return value === null || value === undefined || Number.isNaN(value) ? "无数据" : `${Number(value).toFixed(digits)}${suffix}`; }
function raw(value) { return value === null || value === undefined ? "无数据" : String(value); }
function age(value) { return value === null || value === undefined ? "无数据" : value < 1 ? `${Math.round(value * 1000)} ms` : `${value.toFixed(1)} s`; }
function clock(timestamp) { return timestamp ? new Date(timestamp * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "无数据"; }
function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  return `${days ? `${days}d ` : ""}${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}
function minutesLabel(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const minutes = Math.max(0, Math.round(Number(value)));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return `${hours} 小时${rest ? ` ${rest} 分钟` : ""}`;
}
function bytes(value) { return value === undefined ? "—" : `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`; }
function kib(value) { return value === undefined ? "—" : `${(value / 1024 / 1024).toFixed(2)} GB`; }
function topic(key) { return stateView.state?.topics?.[key] || { status: "no_data", age_seconds: null, rate_hz: 0 }; }
function statusTag(status) { return `${statusLabels[status] || status}`; }

function buildJointGroups() {
  const container = $("joint-groups");
  container.replaceChildren();
  for (const group of stateView.metadata.groups) {
    const article = document.createElement("article");
    article.className = "card joint-group";
    const heading = document.createElement("div");
    heading.className = "joint-group-heading";
    const title = document.createElement("h3"); title.textContent = group.label;
    const note = document.createElement("span");
    note.textContent = group.id === "head" ? "0.01° 原始反馈 / 100" : "位置 · 速度 · 附加电机反馈";
    heading.append(title, note); article.append(heading);
    const wrap = document.createElement("div"); wrap.className = "table-wrap";
    if (group.id === "head") wrap.append(buildHeadTable()); else wrap.append(buildBodyTable(group.id));
    article.append(wrap); container.append(article);
  }
}

function tableShell() {
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const row = document.createElement("tr");
  ["索引", "关节", "角度", "速度", "电机反馈", "更新时间", "60 秒角度曲线"].forEach(label => { const th = document.createElement("th"); th.textContent = label; row.append(th); });
  thead.append(row); table.append(thead);
  const tbody = document.createElement("tbody"); table.append(tbody);
  return { table, tbody };
}

function buildBodyTable(groupId) {
  const { table, tbody } = tableShell();
  stateView.metadata.joints.filter(joint => joint.group === groupId).forEach(joint => {
    const row = document.createElement("tr"); row.dataset.joint = joint.index;
    const cells = Array.from({ length: 7 }, () => document.createElement("td"));
    cells[0].className = "index"; cells[0].textContent = String(joint.index).padStart(2, "0");
    cells[1].className = "joint-name";
    const name = document.createElement("b"); name.textContent = joint.label;
    const detail = document.createElement("small"); detail.textContent = `${joint.name} · ${joint.min}° ~ ${joint.max}°`;
    cells[1].append(name, detail);
    cells[2].className = "value-cell"; cells[2].id = `joint-angle-${joint.index}`;
    cells[3].className = "value-cell"; cells[3].id = `joint-velocity-${joint.index}`;
    cells[4].className = "feedback-cell"; cells[4].id = `joint-feedback-${joint.index}`;
    cells[5].className = "joint-age"; cells[5].id = `joint-age-${joint.index}`;
    const canvas = document.createElement("canvas"); canvas.width = 150; canvas.height = 34; canvas.className = "sparkline"; canvas.id = `joint-chart-${joint.index}`;
    cells[6].append(canvas); row.append(...cells); tbody.append(row);
  });
  return table;
}

function buildHeadTable() {
  const { table, tbody } = tableShell();
  [["H0", "头部偏航", "head_yaw", "yaw", "yaw_speed_raw", "-50° ~ 85°"], ["H1", "头部俯仰", "head_pitch", "pitch", "pitch_speed_raw", "-30° ~ 30°"]].forEach(item => {
    const row = document.createElement("tr");
    const cells = Array.from({ length: 7 }, () => document.createElement("td"));
    cells[0].className = "index"; cells[0].textContent = item[0];
    cells[1].className = "joint-name"; cells[1].innerHTML = `<b>${item[1]}</b><small>${item[2]} · ${item[5]}</small>`;
    cells[2].className = "value-cell"; cells[2].id = `head-${item[3]}`;
    cells[3].className = "value-cell"; cells[3].id = `head-${item[4]}`;
    cells[4].className = "feedback-cell"; cells[4].textContent = "独立 MCU 头部反馈";
    cells[5].id = `head-age-${item[3]}`;
    cells[6].colSpan = 1; cells[6].className = "feedback-cell"; cells[6].textContent = "头部状态见电池上方独立反馈";
    row.append(...cells); tbody.append(row);
  });
  return table;
}

function buildSensors() {
  const container = $("sensor-grid"); container.replaceChildren();
  ["lidar", "itof", "camera", "audio"].forEach(key => {
    const card = document.createElement("article"); card.className = "card sensor-card"; card.id = `sensor-${key}`;
    card.innerHTML = `<div class="sensor-title"><h3></h3><span class="health-label"></span></div><div class="sensor-topic"></div><div class="sensor-metrics"><div><span>更新频率</span><strong class="sensor-rate"></strong></div><div><span>最后更新</span><strong class="sensor-age"></strong></div><div><span>原始流发布者</span><strong class="sensor-publishers"></strong></div><div><span>状态</span><strong class="sensor-state"></strong></div></div><div class="sensor-summary"></div>`;
    container.append(card);
  });
}

function renderOverview() {
  const state = stateView.state, system = state.system || {}, ros = state.ros;
  const pill = $("ws-pill");
  if (pill.classList.contains("online")) { pill.querySelector("span").textContent = `实时推送 ${stateView.metadata.push_rate_hz} Hz`; }
  text("ros-status", statusTag(ros.status));
  $("ros-dot").className = `big-dot ${ros.connected ? "online" : ros.status === "starting" ? "warn" : "offline"}`;
  text("last-update", `${clock(ros.last_update)}${ros.last_update ? " · " + age(state.topics.joints.age_seconds) + "前" : ""}`);
  text("firmware", system.firmware || "—"); text("hostname", system.hostname || "—");
  text("ip-address", Object.entries(system.ip_addresses || {}).map(([name, ip]) => `${name}: ${ip}`).join(" · ") || "—");
  text("uptime", duration(system.uptime_seconds)); text("root-mount", system.root_mount === "ro" ? "只读 (ro)" : system.root_mount || "—");
  text("cpu", number(system.cpu_percent, 1, "%")); text("load", `LOAD ${(system.load_average || []).map(v => v.toFixed(2)).join(" / ") || "—"}`);
  $("cpu-bar").style.width = `${system.cpu_percent || 0}%`;
  const bpu = system.bpu || {};
  text("bpu", bpu.available ? number(bpu.utilization_percent, 1, "%") : "不可用");
  const bpuFrequencies = bpu.frequency_hz === null || bpu.frequency_hz === undefined
    ? "频率无数据"
    : `${(bpu.frequency_hz / 1e6).toFixed(0)}${bpu.max_frequency_hz ? ` / ${(bpu.max_frequency_hz / 1e6).toFixed(0)}` : ""} MHz`;
  text("bpu-detail", bpu.available ? `${bpuFrequencies} · ${bpu.user_count ?? 0} 个进程` : "检测硬件不可用");
  $("bpu-bar").style.width = `${bpu.utilization_percent || 0}%`;
  text("memory", number(system.memory?.used_percent, 1, "%")); text("memory-detail", `${kib(system.memory?.used_kib)} / ${kib(system.memory?.total_kib)}`);
  $("memory-bar").style.width = `${system.memory?.used_percent || 0}%`;
  text("disk", number(system.disk?.used_percent, 1, "%")); text("disk-detail", `${bytes(system.disk?.used_bytes)} / ${bytes(system.disk?.total_bytes)}`);
  $("disk-bar").style.width = `${system.disk?.used_percent || 0}%`;
  const temps = system.temperatures_c || {};
  text("temperature", number(temps["thermal-cpu"], 1, "°C"));
  text("temperature-detail", Object.entries(temps).map(([name, value]) => `${name.replace("thermal-", "").toUpperCase()} ${value}°C`).join(" · ") || "—");
  const service = system.service || {};
  text("service-usage", `${number(service.cpu_percent, 1, "%")} · ${service.rss_kib === undefined ? "—" : (service.rss_kib / 1024).toFixed(1) + " MB"}`);
}

function renderJoints() {
  const state = stateView.state, jointTopic = topic("joints");
  state.joints.forEach(joint => {
    const angleElement = $(`joint-angle-${joint.index}`), velocityElement = $(`joint-velocity-${joint.index}`), ageElement = $(`joint-age-${joint.index}`);
    angleElement.textContent = number(joint.angle, 2, "°"); velocityElement.textContent = raw(joint.velocity);
    ageElement.textContent = age(jointTopic.age_seconds); ageElement.title = clock(jointTopic.last_update);
    const stale = jointTopic.status !== "online";
    angleElement.classList.toggle("stale-text", stale && joint.angle !== null); velocityElement.classList.toggle("stale-text", stale && joint.velocity !== null);
    ageElement.className = `joint-age ${stale ? "stale-text" : ""}`;
    text(`joint-feedback-${joint.index}`, `I ${raw(joint.current)} · L ${raw(joint.load)} · T ${raw(joint.temperature)} · E ${raw(joint.error)}`);
  });
  const head = state.head, headTopic = topic("head");
  text("head-yaw", number(head.yaw, 2, "°")); text("head-pitch", number(head.pitch, 2, "°"));
  text("head-yaw_speed_raw", raw(head.yaw_speed_raw)); text("head-pitch_speed_raw", raw(head.pitch_speed_raw));
  text("head-age-yaw", age(headTopic.age_seconds)); text("head-age-pitch", age(headTopic.age_seconds));
}

function renderVector(id, vector) {
  const element = $(id); element.replaceChildren();
  ["x", "y", "z"].forEach(axis => {
    const item = document.createElement("div"); item.className = "vector-item";
    const label = document.createElement("span"); label.textContent = axis.toUpperCase();
    const value = document.createElement("strong"); value.textContent = number(vector?.[axis], 3);
    item.append(label, value); element.append(item);
  });
}

function renderImu() {
  const imu = stateView.state.imu;
  text("roll", number(imu.roll, 1)); text("pitch", number(imu.pitch, 1)); text("yaw", number(imu.yaw, 1));
  renderVector("angular-velocity", imu.angular_velocity); renderVector("linear-acceleration", imu.linear_acceleration);
  text("imu-frame", imu.frame_id || "—"); text("imu-topic-status", `${statusTag(topic("imu").status)} · ${number(topic("imu").rate_hz, 1, " Hz")} · ${age(topic("imu").age_seconds)}`);
  drawAttitude(imu);
}

function renderPower() {
  const battery = stateView.state.battery, vehicle = stateView.state.vehicle;
  const batteryStatus = stateView.state.battery_status || {};
  const vehicleStatus = stateView.state.vehicle_status || {};
  text("battery-power", battery.power === undefined ? "—" : `${battery.power}%`);
  $("battery-ring").style.setProperty("--power", `${(battery.power || 0) * 3.6}deg`);
  text("battery-voltage", number(battery.voltage, 3, " V"));
  let charging = "无数据";
  if (battery.is_charging !== undefined) charging = battery.charge_error ? "充电错误" : battery.charge_full ? "已充满" : battery.is_charging ? "充电中" : "未充电";
  if (battery.on_charger_raw !== undefined) charging += ` · 座充原始状态 ${battery.on_charger_raw}`;
  text("charging-status", charging); text("plate-voltage", number(battery.plate_voltage, 3, " V"));
  text("battery-extra", `${raw(battery.current_ma)} mA · ${raw(battery.temperature_raw)} raw`);
  const chargeRate = batteryStatus.charging_rate_percent_per_hour;
  text("charge-rate", chargeRate === null || chargeRate === undefined
    ? (battery.is_charging ? "充电速度采样中" : "当前未充电")
    : `约 ${Number(chargeRate).toFixed(1)} 个百分点 / 小时`);
  text("charge-sample", batteryStatus.soc_delta === null || batteryStatus.soc_delta === undefined
    ? "—" : `+${batteryStatus.soc_delta}% / ${batteryStatus.duration_minutes} 分钟`);
  text("charge-eta-30", battery.power >= 30 ? "已达到" : minutesLabel(batteryStatus.eta_30_minutes));
  text("charge-eta-full", battery.charge_full ? "已充满" : minutesLabel(batteryStatus.eta_full_minutes));
  const vehicleTopic = vehicleStatus.topic || topic("vehicle");
  const vehicleRaw = vehicleStatus.raw || vehicle;
  const mount = vehicleStatus.mount || {};
  const wheelMotion = vehicleStatus.wheel_motion || {};
  text("vehicle-status", `${statusTag(vehicleTopic.status)} · ${age(vehicleTopic.age_seconds)}`);
  text("vehicle-link", vehicleStatus.communication?.feedback_online ? "反馈在线" : vehicleTopic.status === "stale" ? "反馈超时" : "无反馈");
  text("vehicle-motion", wheelMotion.state === "moving" ? "车轮运动中" : wheelMotion.state === "stopped" ? "车轮静止" : "未知");
  text("vehicle-mount", mount.known ? ({on_vehicle: "已在车上", off_vehicle: "未在车上", mounting: "上车中", dismounting: "下车中"}[mount.state] || mount.state) : "未知");
  text("vehicle-mount-note", mount.reason || "尚无可用的在车状态来源");
  text("left-speed", raw(vehicleRaw.left_speed_raw)); text("right-speed", raw(vehicleRaw.right_speed_raw));
  text("left-mileage", raw(vehicleRaw.left_mileage_raw)); text("right-mileage", raw(vehicleRaw.right_mileage_raw));
  text("vehicle-comm", raw(vehicleStatus.communication?.raw ?? vehicleRaw.comm_status)); text("vehicle-fault", `${raw(vehicleRaw.left_fault)} / ${raw(vehicleRaw.right_fault)}`);
  text("vehicle-current", `${raw(vehicleRaw.left_current_raw)} / ${raw(vehicleRaw.right_current_raw)}`);
}

function sensorSummary(key, sensor) {
  const s = sensor.summary || {};
  if (key === "lidar") return s.points === undefined ? "等待轻量 LaserScan 摘要" : `${s.frame_id || "—"} · ${s.valid_points}/${s.points} 有效点 · ${number(s.range_min, 2)}–${number(s.range_max, 2)} m`;
  if (key === "itof" || key === "camera") return s.width === undefined ? "等待 CameraInfo 摘要" : `${s.width} × ${s.height} · ${s.frame_id || "—"} · ${s.distortion_model || "—"}`;
  return s.last_event === undefined ? "监听关键词事件；无事件不代表麦克风离线" : `最近事件码：${s.last_event.join(", ")}`;
}

function renderSensors() {
  Object.entries(stateView.state.sensors).forEach(([key, sensor]) => {
    const card = $(`sensor-${key}`); if (!card) return;
    card.className = `card sensor-card ${sensor.status}`;
    card.querySelector("h3").textContent = sensor.label;
    card.querySelector(".health-label").textContent = statusTag(sensor.status);
    card.querySelector(".sensor-topic").textContent = sensor.topic.name;
    card.querySelector(".sensor-rate").textContent = number(sensor.rate_hz, 2, " Hz");
    card.querySelector(".sensor-age").textContent = age(sensor.age_seconds);
    card.querySelector(".sensor-age").title = clock(sensor.topic.last_update);
    card.querySelector(".sensor-publishers").textContent = raw(sensor.publisher_count);
    card.querySelector(".sensor-state").textContent = statusTag(sensor.status);
    card.querySelector(".sensor-summary").textContent = sensorSummary(key, sensor);
  });
}

function renderGraph() {
  const ros = stateView.state.ros;
  text("graph-count", `${ros.node_count} 节点 · ${ros.topic_count} 话题 · 图每 2 秒刷新`);
  const nodes = $("node-list"); nodes.replaceChildren();
  ros.nodes.forEach(name => { const chip = document.createElement("span"); chip.className = "node-chip"; chip.textContent = name; nodes.append(chip); });
  if (!ros.nodes.length) nodes.textContent = "等待 ROS 图发现";
  const tbody = $("topic-table"); tbody.replaceChildren();
  Object.entries(stateView.state.topics).forEach(([key, item]) => {
    const row = document.createElement("tr");
    [item.name, item.type, number(item.rate_hz, 2, " Hz"), age(item.age_seconds), statusTag(item.status)].forEach((value, index) => {
      const cell = document.createElement("td"); cell.textContent = value;
      if (index === 0) cell.className = "topic-name"; if (index === 1) cell.className = "type-name";
      if (index === 4 && item.status !== "online") cell.className = "stale-text";
      row.append(cell);
    });
    row.dataset.topic = key; tbody.append(row);
  });
}

function appendLiveHistory(state) {
  const t = state.server_time;
  if (!histories.joints.length || t - histories.joints[histories.joints.length - 1].t > 0.08) {
    histories.joints.push({ t, angles: state.joints.map(joint => joint.angle), velocities: state.joints.map(joint => joint.velocity) });
  }
  if (!histories.imu.length || t - histories.imu[histories.imu.length - 1].t > 0.08) {
    histories.imu.push({ t, roll: state.imu.roll, pitch: state.imu.pitch, yaw: state.imu.yaw });
  }
  const lastBattery = histories.battery[histories.battery.length - 1];
  const batteryChanged = !lastBattery
    || lastBattery.power !== state.battery.power
    || lastBattery.is_charging !== state.battery.is_charging;
  if (batteryChanged || t - lastBattery.t >= 15) {
    histories.battery.push({
      t, power: state.battery.power, voltage: state.battery.voltage,
      is_charging: state.battery.is_charging, current_ma: state.battery.current_ma, source: "websocket",
    });
  }
  trimHistory(t);
}
function trimHistory(now = Date.now() / 1000) {
  for (const key of ["joints", "imu"]) {
    while (histories[key].length && now - histories[key][0].t > 60) histories[key].shift();
  }
  while (histories.battery.length && now - histories.battery[0].t > 6 * 60 * 60) histories.battery.shift();
}
function canvasContext(canvas) { const ctx = canvas.getContext("2d"); ctx.clearRect(0, 0, canvas.width, canvas.height); return ctx; }
function drawNoData(ctx, canvas) { ctx.fillStyle = "#9aa7b5"; ctx.font = "12px sans-serif"; ctx.textAlign = "center"; ctx.fillText("无数据", canvas.width / 2, canvas.height / 2); }

function drawSparklines() {
  if (!stateView.state) return;
  stateView.state.joints.forEach(joint => {
    const canvas = $(`joint-chart-${joint.index}`); if (!canvas) return;
    const ctx = canvasContext(canvas), points = histories.joints.map(item => ({ t: item.t, value: item.angles[joint.index] })).filter(item => item.value !== null);
    ctx.strokeStyle = "#dbe3ec"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, 17); ctx.lineTo(150, 17); ctx.stroke();
    if (points.length < 2) { drawNoData(ctx, canvas); return; }
    const values = points.map(point => point.value); let min = Math.min(...values), max = Math.max(...values);
    if (max - min < 1) { min -= 0.5; max += 0.5; }
    const latest = points[points.length - 1].t, start = latest - 60;
    ctx.strokeStyle = topic("joints").status === "online" ? groupColors[joint.group] : "#9aa7b5"; ctx.lineWidth = 1.4; ctx.beginPath();
    points.forEach((point, index) => { const x = Math.max(0, Math.min(150, (point.t - start) / 60 * 150)), y = 31 - (point.value - min) / (max - min) * 28; if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
    ctx.stroke();
  });
}

function drawImuChart() {
  const canvas = $("imu-chart"), ctx = canvasContext(canvas), w = canvas.width, h = canvas.height;
  ctx.strokeStyle = "#dbe3ec"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const y = 20 + i * (h - 50) / 4; ctx.beginPath(); ctx.moveTo(35, y); ctx.lineTo(w - 10, y); ctx.stroke(); }
  const points = histories.imu.filter(item => [item.roll, item.pitch, item.yaw].every(value => value !== null));
  if (points.length < 2) { drawNoData(ctx, canvas); return; }
  const latest = points[points.length - 1].t, start = latest - 60;
  [["roll", "#0d9488"], ["pitch", "#d98324"], ["yaw", "#7a5cd6"]].forEach(([key, color]) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.beginPath();
    points.forEach((point, index) => { const x = 35 + Math.max(0, Math.min(1, (point.t - start) / 60)) * (w - 45), y = h / 2 - Math.max(-180, Math.min(180, point[key])) / 360 * (h - 50); if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
    ctx.stroke();
  });
  ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif"; ctx.textAlign = "left"; ctx.fillText("+180°", 2, 24); ctx.fillText("0°", 15, h / 2); ctx.fillText("-180°", 2, h - 20); ctx.textAlign = "right"; ctx.fillText("60 s", w - 10, h - 5);
}

function drawBatteryChart() {
  const canvas = $("battery-chart");
  if (!canvas) return;
  const ctx = canvasContext(canvas), w = canvas.width, h = canvas.height;
  const left = 42, right = 16, top = 16, bottom = 30;
  ctx.strokeStyle = "#dbe3ec"; ctx.lineWidth = 1;
  ctx.fillStyle = "#8a97a8"; ctx.font = "11px sans-serif";
  for (let value = 0; value <= 100; value += 25) {
    const y = top + (100 - value) / 100 * (h - top - bottom);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(`${value}%`, left - 7, y + 4);
  }
  const points = histories.battery
    .filter(item => Number.isFinite(Number(item.t)) && Number.isFinite(Number(item.power)))
    .map(item => ({ ...item, t: Number(item.t), power: Number(item.power) }));
  if (points.length < 2) { drawNoData(ctx, canvas); return; }
  const latest = Math.max(stateView.state?.server_time || 0, points[points.length - 1].t);
  const start = latest - 6 * 60 * 60;
  const xOf = t => left + Math.max(0, Math.min(1, (t - start) / (6 * 60 * 60))) * (w - left - right);
  const yOf = power => top + (100 - Math.max(0, Math.min(100, power))) / 100 * (h - top - bottom);
  const visible = points.filter(point => point.t >= start);
  ctx.strokeStyle = "#0d9488"; ctx.lineWidth = 2.2; ctx.beginPath();
  visible.forEach((point, index) => {
    const x = xOf(point.t), y = yOf(point.power);
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#22a55c";
  visible.filter(point => point.is_charging === true).forEach(point => {
    ctx.beginPath(); ctx.arc(xOf(point.t), yOf(point.power), 2.3, 0, Math.PI * 2); ctx.fill();
  });
  ctx.fillStyle = "#8a97a8"; ctx.textAlign = "center";
  for (let hour = 0; hour <= 6; hour += 2) {
    const timestamp = start + hour * 60 * 60;
    ctx.fillText(new Date(timestamp * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }), xOf(timestamp), h - 7);
  }
}

function drawAttitude(imu) {
  const canvas = $("attitude-canvas"), ctx = canvasContext(canvas), w = canvas.width, h = canvas.height;
  if (imu.roll === null) { drawNoData(ctx, canvas); return; }
  ctx.save(); ctx.translate(w / 2, h / 2); ctx.rotate(-imu.roll * Math.PI / 180); ctx.translate(0, imu.pitch * 2.2);
  ctx.fillStyle = "#cfe4f7"; ctx.fillRect(-w * 2, -h * 3, w * 4, h * 3); ctx.fillStyle = "#b7c4d2"; ctx.fillRect(-w * 2, 0, w * 4, h * 3);
  ctx.strokeStyle = "#5a7186"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(-w * 2, 0); ctx.lineTo(w * 2, 0); ctx.stroke();
  ctx.strokeStyle = "#41618026"; ctx.lineWidth = 1;
  for (let pitch = -60; pitch <= 60; pitch += 15) { if (pitch === 0) continue; const y = -pitch * 2.2; ctx.beginPath(); ctx.moveTo(-35, y); ctx.lineTo(35, y); ctx.stroke(); }
  ctx.restore();
  ctx.strokeStyle = "#0f8f88"; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(w / 2 - 75, h / 2); ctx.lineTo(w / 2 - 20, h / 2); ctx.lineTo(w / 2, h / 2 + 10); ctx.lineTo(w / 2 + 20, h / 2); ctx.lineTo(w / 2 + 75, h / 2); ctx.stroke();
  ctx.fillStyle = "#3b4a5e"; ctx.font = "12px monospace"; ctx.textAlign = "center"; ctx.fillText(`YAW ${number(imu.yaw, 1, "°")}`, w / 2, h - 18);
}

function renderAudioCard() {
  const audio = stateView.state.sensors?.audio; if (!audio) return;
  const keyword = stateView.state.topics?.audio_keyword;
  text("audio-publishers", raw(audio.publisher_count));
  text("audio-kw-status", statusTag(keyword?.status));
  text("audio-last-event", audio.summary?.last_event ? audio.summary.last_event.join(", ") : "无事件");
}

function uuid() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, char => {
    const value = Math.random() * 16 | 0;
    return (char === "x" ? value : (value & 0x3 | 0x8)).toString(16);
  });
}

function notify(message, kind = "info") {
  const stack = $("toast-stack"); if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  toast.textContent = message;
  stack.append(toast);
  setTimeout(() => toast.classList.add("out"), 4200);
  setTimeout(() => toast.remove(), 4800);
}

function confirmMotion(title, detail) {
  return new Promise(resolve => {
    motionView.confirmResolve = resolve;
    text("motion-confirm-title", title);
    text("motion-confirm-text", detail || "该动作可能改变机器人姿态或底盘状态，请确认周围安全。");
    $("motion-confirm-modal").classList.add("open");
    $("motion-confirm-modal").setAttribute("aria-hidden", "false");
    $("motion-confirm-ok").focus();
  });
}

function closeMotionConfirm(value) {
  $("motion-confirm-modal").classList.remove("open");
  $("motion-confirm-modal").setAttribute("aria-hidden", "true");
  const resolve = motionView.confirmResolve;
  motionView.confirmResolve = null;
  if (resolve) resolve(value);
}

async function apiMotionPost(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  let data = null;
  try { data = await response.json(); } catch (error) { data = null; }
  if (!response.ok || !data || data.accepted === false) {
    const message = data?.error?.message || `请求失败 HTTP ${response.status}`;
    throw new Error(message);
  }
  return data;
}

async function sendMotionCommand(commandId, reason) {
  const def = motionView.defs.get(commandId);
  if (!def) { notify(`未知命令：${commandId}`, "error"); return false; }
  if (!def.dashboard_enabled) { notify(`${def.display_name} 未在 Dashboard 启用`, "warn"); return false; }
  if (def.confirmation === "confirm") {
    const confirmed = await confirmMotion(
      `确认执行：${def.display_name}`,
      `${def.notes || "该动作可能改变机器人姿态或底盘状态。"}（${def.vendor_command} · ${interactionLabels[def.interaction] || def.interaction}）`
    );
    if (!confirmed) return false;
  }
  try {
    const result = await apiMotionPost("/api/motion/command", {
      command_id: commandId,
      reason: reason || "dashboard manual control",
      client_request_id: uuid(),
    });
    const latency = result.latency_ms === null || result.latency_ms === undefined ? "—" : `${Math.round(result.latency_ms)} ms`;
    notify(`${def.display_name}：厂商已受理 · HTTP ${result.vendor_response?.http_status ?? "—"} · ${latency}`, "ok");
    return true;
  } catch (error) {
    notify(`${def.display_name}：${error.message}`, "error");
    return false;
  }
}

async function sendMotionStop(reason) {
  try {
    const result = await apiMotionPost("/api/motion/stop", {
      reason: reason || "dashboard stop",
      client_request_id: uuid(),
    });
    notify(`MOTION_STOP 已发送 · HTTP ${result.vendor_response?.http_status ?? "—"} · ${Math.round(result.latency_ms || 0)} ms`, "warn");
  } catch (error) {
    notify(`停止失败：${error.message}`, "error");
  }
}

async function sendEmergencyStop() {
  try {
    const result = await apiMotionPost("/api/motion/emergency-stop", { reason: "dashboard emergency stop" });
    notify(`紧急停止已发送 · HTTP ${result.vendor_response?.http_status ?? "—"} · ${result.route || ""}`, "error");
  } catch (error) {
    notify(`紧急停止失败：${error.message}`, "error");
  }
}

async function sendEmergencyReset() {
  const confirmed = await confirmMotion("解除急停", "确认机器人周围安全、底盘稳定后再解除急停。解除后会回退发送 RESET。");
  if (!confirmed) return;
  try {
    const result = await apiMotionPost("/api/motion/emergency-reset", { reason: "dashboard emergency reset" });
    notify(`急停解除已发送 · HTTP ${result.vendor_response?.http_status ?? "—"}`, "ok");
  } catch (error) {
    notify(`解除急停失败：${error.message}`, "error");
  }
}

function startMotionHold(button, event) {
  if (event && event.button !== undefined && event.button !== 0) return;
  if (motionView.hold) return;
  const commandId = button.dataset.commandId;
  if (!commandId) return;
  if (event) event.preventDefault();
  const pendingStop = motionView.stopPromise || Promise.resolve();
  motionView.hold = { commandId, button, timer: null, commandPromise: null };
  button.classList.add("active");
  motionView.hold.commandPromise = pendingStop.then(() => sendMotionCommand(commandId, "hold-to-run")).then(ok => {
    if (!ok) releaseMotionHold("command rejected");
  });
  motionView.hold.timer = setTimeout(() => {
    notify("按住保护触发，自动发送 MOTION_STOP", "warn");
    releaseMotionHold("hold watchdog");
  }, 5500);
}

function releaseMotionHold(reason) {
  const hold = motionView.hold;
  if (!hold) return;
  motionView.hold = null;
  clearTimeout(hold.timer);
  hold.button.classList.remove("active");
  motionView.stopPromise = Promise.resolve(hold.commandPromise)
    .catch(() => null)
    .then(() => sendMotionStop(reason || "hold released"));
}

function registerMotionDefinitions(capabilities) {
  motionView.defs.clear();
  for (const category of capabilities?.categories || []) {
    for (const def of category.commands || []) motionView.defs.set(def.id, def);
  }
}

function buildMotionActions() {
  const container = $("motion-action-groups");
  if (!container) return;
  const capabilities = motionView.capabilities;
  if (!capabilities) { container.innerHTML = '<p class="empty-note">命令目录不可用</p>'; return; }
  registerMotionDefinitions(capabilities);
  container.replaceChildren();
  const byCategory = new Map(capabilities.categories.map(category => [category.id, category]));
  for (const categoryId of motionActionCategories) {
    const category = byCategory.get(categoryId);
    if (!category || !category.commands.length) continue;
    const group = document.createElement("div");
    group.className = "motion-action-group";
    const heading = document.createElement("div");
    heading.className = "motion-action-heading";
    const title = document.createElement("h3"); title.textContent = category.label;
    const note = document.createElement("span"); note.textContent = category.description || "";
    heading.append(title, note); group.append(heading);
    const grid = document.createElement("div"); grid.className = "motion-action-grid";
    for (const def of category.commands) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "motion-action-button";
      button.dataset.commandId = def.id;
      button.dataset.modeId = def.category === "mode" ? def.id : "";
      if (!def.dashboard_enabled) button.classList.add("disabled");
      const name = document.createElement("strong"); name.textContent = def.display_name;
      const meta = document.createElement("small");
      meta.textContent = `${def.vendor_command} · ${interactionLabels[def.interaction] || def.interaction}`;
      button.append(name, meta);
      if (def.category === "extended" || def.verification_state === "frontend_observed") {
        const badge = document.createElement("em"); badge.className = "cmd-badge"; badge.textContent = def.dashboard_enabled ? "待验证" : "未启用";
        button.append(badge);
      }
      if (!def.dashboard_enabled) button.title = def.notes || "该命令尚未在 Dashboard 启用";
      else if (def.confirmation === "confirm") button.title = def.notes || "执行前需要确认";
      button.addEventListener("click", () => { sendMotionCommand(def.id, "dashboard manual control"); });
      grid.append(button);
    }
    group.append(grid);
    container.append(group);
  }
}

function buildMotionStepper(command) {
  const stepper = $("motion-stepper"); if (!stepper) return;
  stepper.replaceChildren();
  const phase = command?.phase;
  const failure = phase && !phaseFlow.includes(phase);
  const activeIndex = phaseFlow.indexOf(phase);
  phaseFlow.forEach((item, index) => {
    const step = document.createElement("span");
    step.className = "motion-step";
    if (phase && (index < activeIndex || (activeIndex === -1 && index <= phaseFlow.indexOf("observing")))) step.classList.add("done");
    if (index === activeIndex) step.classList.add("active");
    step.textContent = phaseLabels[item] || item;
    stepper.append(step);
  });
  if (failure) {
    const failed = document.createElement("span");
    failed.className = "motion-step failure";
    failed.textContent = phaseLabels[phase] || phase;
    stepper.append(failed);
  }
}

function renderMotionStatus() {
  const control = stateView.state?.motion_control;
  if (!control) return;
  const vendor = control.vendor || {};
  const service = control.vendor_service || "unknown";
  const pill = $("motion-vendor-pill");
  if (pill) {
    pill.className = `status-pill ${service === "online" ? "online" : service === "degraded" ? "warn" : "offline"}`;
    pill.querySelector("span").textContent = service === "online" ? "在线" : service === "degraded" ? "降级" : service === "unknown" ? "检测中" : "离线";
  }
  const battery = vendor.battery || {};
  const voltage = typeof battery.voltage === "number" ? (battery.voltage > 1000 ? battery.voltage / 1000 : battery.voltage) : null;
  text("motion-vendor-battery", battery.power === undefined ? "—" : `${battery.power}%${voltage === null ? "" : ` · ${voltage.toFixed(2)} V`}`);
  text("motion-vendor-latency", vendor.latency_ms === null || vendor.latency_ms === undefined ? "—" : `${Math.round(vendor.latency_ms)} ms`);
  text("motion-emergency-state", control.emergency?.active ? "已触发" : "正常");
  $("motion-emergency-state")?.classList.toggle("danger-text", Boolean(control.emergency?.active));

  const command = control.command;
  const bar = $("motion-stopbar");
  if (bar) {
    bar.classList.toggle("is-emergency", Boolean(control.emergency?.active));
    bar.classList.toggle("is-offline", service !== "online" && !control.emergency?.active);
  }
  let barText = "系统就绪 · 可发送运动命令";
  if (control.emergency?.active) barText = "急停已触发 · 仅允许停止 / 解除急停";
  else if (service === "offline") barText = "厂商控制服务离线 · 命令将失败";
  else if (service === "degraded") barText = "厂商服务降级 · 仅健康探测可用";
  else if (command && !terminalMotionPhases.includes(command.phase)) {
    barText = `${command.display_name} · ${phaseLabels[command.phase] || command.phase}`;
  }
  text("motion-stopbar-text", barText);

  if (!command) {
    text("motion-phase", "空闲");
    text("motion-phase-detail", "尚未发送命令");
    text("motion-http", "—");
    text("motion-observed", "—");
    text("motion-request-id", "—");
  } else {
    text("motion-phase", `${command.display_name} · ${phaseLabels[command.phase] || command.phase}`);
    text("motion-phase-detail", command.detail || "—");
    text("motion-http", command.http_status === null || command.http_status === undefined
      ? "—"
      : `HTTP ${command.http_status} · ${Math.round(command.latency_ms || 0)} ms · ${clock(command.vendor_responded_at)}`);
    text("motion-observed", command.motion_observed_at
      ? `运动 ${clock(command.motion_observed_at)}`
      : command.stopped_at
        ? `停止 ${clock(command.stopped_at)}`
        : command.phase === "timed_out" ? "未观测到运动" : "—");
    text("motion-request-id", command.request_id ? command.request_id.slice(0, 8) : "—");
  }
  if (motionView.lastPhase !== (command?.phase || null)) {
    motionView.lastPhase = command?.phase || null;
    buildMotionStepper(command);
  }

  const robot = control.robot || {};
  const ages = robot.data_age_seconds || {};
  text("motion-control-mode", robot.control_mode === null || robot.control_mode === undefined ? "—" : `${robot.control_mode} · ${robot.control_mode_label || "未知"}`);
  text("motion-gait-type", robot.gait_type === null || robot.gait_type === undefined ? "—" : `${robot.gait_type} · ${robot.gait_type_label || "未知"}`);
  text("motion-fall-state", robot.fall_state === null || robot.fall_state === undefined ? "无数据" : robot.fall_state === 0 ? "正常 (0)" : `异常 (${robot.fall_state})`);
  $("motion-fall-state")?.classList.toggle("danger-text", Boolean(robot.fall_state));
  text("motion-left-speed", raw(robot.left_wheel_speed));
  text("motion-right-speed", raw(robot.right_wheel_speed));
  text("motion-linear-velocity", raw(robot.linear_velocity_x));
  text("motion-angular-rate", raw(robot.angular_rate_z));
  text("motion-head", `${number(robot.head_yaw, 1)} / ${number(robot.head_pitch, 1)}`);
  const feedbackAge = ages.motion_feedback;
  const agePill = $("motion-feedback-age");
  if (agePill) {
    const stale = feedbackAge === null || feedbackAge === undefined || feedbackAge > 1.5;
    agePill.className = `inline-status ${stale ? "stale" : ""}`;
    agePill.textContent = feedbackAge === null || feedbackAge === undefined ? "运动反馈无数据" : feedbackAge > 1.5 ? "运动反馈过期" : "运动反馈在线";
  }
  text("motion-feedback-detail", age(ages.motion_feedback));
  text("motion-vehicle-detail", age(ages.vehicle));
  text("motion-fall-detail", age(ages.self_fall));

  document.querySelectorAll(".motion-action-button[data-mode-id]").forEach(button => {
    const modeId = button.dataset.modeId;
    if (!modeId) return;
    button.classList.toggle("selected", Boolean(command && command.command_id === modeId && !terminalMotionPhases.includes(command.phase)));
  });
}

function renderMotionTimeline() {
  const container = $("motion-timeline"); if (!container) return;
  const events = motionView.history || [];
  text("motion-timeline-status", events.length ? `${events.length} 条事件` : "待机");
  if (!events.length) { container.innerHTML = '<p class="empty-note">暂无运动命令</p>'; return; }
  container.replaceChildren();
  const sourceLabels = { dashboard: "DASH", vendor: "HTTP", ros: "ROS" };
  events.slice(0, 60).forEach(event => {
    const row = document.createElement("div");
    row.className = `motion-event source-${event.source || "dashboard"}`;
    const time = document.createElement("span");
    time.className = "motion-event-time"; time.textContent = clock(event.at);
    const source = document.createElement("span");
    source.className = "motion-event-source"; source.textContent = sourceLabels[event.source] || event.source || "—";
    const body = document.createElement("div");
    body.className = "motion-event-body";
    const title = document.createElement("strong");
    title.textContent = phaseLabels[event.phase] || event.phase || "事件";
    const detail = document.createElement("small"); detail.textContent = event.detail || "—";
    body.append(title, detail);
    const request = document.createElement("span");
    request.className = "motion-event-id"; request.textContent = event.request_id ? event.request_id.slice(0, 8) : "";
    row.append(time, source, body, request);
    container.append(row);
  });
}

async function refreshMotionHistory() {
  try {
    const response = await fetch(`/api/motion/history?limit=80&t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    motionView.history = data.events || [];
    renderMotionTimeline();
  } catch (error) { /* polling only */ }
}

function startMotionPolling() {
  if (motionView.historyTimer) return;
  refreshMotionHistory();
  motionView.historyTimer = setInterval(refreshMotionHistory, 2000);
}

function stopMotionPolling() {
  if (motionView.historyTimer) { clearInterval(motionView.historyTimer); motionView.historyTimer = null; }
}

function initMotionControls() {
  document.querySelectorAll("[data-hold]").forEach(button => {
    button.addEventListener("pointerdown", event => startMotionHold(button, event));
    button.addEventListener("contextmenu", event => event.preventDefault());
    button.addEventListener("keydown", event => {
      if (event.code === "Space" || event.code === "Enter") { event.preventDefault(); startMotionHold(button, event); }
    });
    button.addEventListener("keyup", event => {
      if (event.code === "Space" || event.code === "Enter") releaseMotionHold("key released");
    });
  });
  window.addEventListener("pointerup", () => releaseMotionHold("pointer released"));
  window.addEventListener("pointercancel", () => releaseMotionHold("pointer cancelled"));
  window.addEventListener("blur", () => releaseMotionHold("window blurred"));
  document.addEventListener("visibilitychange", () => { if (document.hidden) releaseMotionHold("page hidden"); });
  $("motion-stop-button")?.addEventListener("click", () => sendMotionStop("stop button"));
  $("motion-dpad-stop")?.addEventListener("click", () => sendMotionStop("dpad stop"));
  $("motion-emergency-button")?.addEventListener("click", sendEmergencyStop);
  $("motion-reset-button")?.addEventListener("click", sendEmergencyReset);
  $("motion-confirm-cancel")?.addEventListener("click", () => closeMotionConfirm(false));
  $("motion-confirm-ok")?.addEventListener("click", () => closeMotionConfirm(true));
  $("motion-confirm-modal")?.addEventListener("click", event => {
    if (event.target === $("motion-confirm-modal")) closeMotionConfirm(false);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && motionView.confirmResolve) closeMotionConfirm(false);
  });
}

function ttsActiveJob(tts) {
  return Boolean(tts && tts.current && ttsActivePhases.includes(tts.current.phase));
}

function setTtsError(message) {
  const box = $("tts-error");
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

function updateTtsCharCount() {
  const input = $("tts-text");
  if (!input) return;
  const max = ttsView.limits?.max_text_length || Number(input.maxLength) || 300;
  text("tts-char-count", `${input.value.length} / ${max}`);
}

function populateTtsSelect(select, options, fallbackValue) {
  if (!select || !Array.isArray(options) || !options.length) return;
  const current = select.value || fallbackValue;
  select.replaceChildren();
  options.forEach(item => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label || item.id;
    select.append(option);
  });
  const allowed = options.some(item => item.id === current);
  select.value = allowed ? current : (fallbackValue || options[0].id);
}

function applyTtsControls(controls) {
  if (!controls) return;
  const signature = JSON.stringify(controls);
  const defaults = controls.defaults || {};
  const firstApply = !ttsView.controlsSignature;
  if (signature !== ttsView.controlsSignature) {
    populateTtsSelect($("tts-voice-select"), controls.voices, defaults.voice);
    populateTtsSelect($("tts-emotion-select"), controls.emotions, defaults.emotion || "neutral");
    ttsView.controlsSignature = signature;
  }
  const slider = $("tts-speed");
  const rate = controls.speech_rate || {};
  if (slider) {
    if (rate.min !== undefined) slider.min = rate.min;
    if (rate.max !== undefined) slider.max = rate.max;
    if (rate.step !== undefined) slider.step = rate.step;
    if (firstApply && defaults.speech_rate !== undefined) slider.value = defaults.speech_rate;
    text("tts-speed-value", `${Number(slider.value).toFixed(2)}×`);
  }
}

function applyTtsConfig(status) {
  if (!status) return;
  ttsView.config = status;
  ttsView.limits = status.limits || {};
  const input = $("tts-text");
  if (input && ttsView.limits.max_text_length) input.maxLength = ttsView.limits.max_text_length;
  text("tts-model", status.engine?.model || "—");
  text("tts-voice", status.engine?.voice || "—");
  text("tts-device", status.engine?.topic || "/audio/speaker_pcm");
  applyTtsControls(status.controls);
  updateTtsCharCount();
  const pill = $("tts-status-pill");
  if (pill && status.online === false) {
    pill.className = "status-pill offline";
    pill.querySelector("span").textContent = "audio_service 离线";
  }
}

async function stopTts() {
  const button = $("tts-stop-button");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/tts/stop", { method: "POST" });
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok || !data || data.ok === false) {
      throw new Error(data?.error?.message || `请求失败 HTTP ${response.status}`);
    }
    notify(`已停止：清空 ${data.cancelled_queued || 0} 条排队任务`, "ok");
    renderTts();
    loadTtsStatus();
  } catch (error) {
    setTtsError(error.message);
    notify(`停止失败：${error.message}`, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadTtsStatus() {
  try {
    const response = await fetch("/api/tts/status", { cache: "no-store" });
    if (!response.ok) return;
    applyTtsConfig(await response.json());
  } catch (error) { }
}

async function submitTts() {
  const input = $("tts-text");
  if (!input) return;
  const value = input.value.trim();
  const max = ttsView.limits?.max_text_length || Number(input.maxLength) || 300;
  if (!value) {
    setTtsError("请输入要播报的文字");
    input.focus();
    return;
  }
  if (value.length > max) {
    setTtsError(`播报文字超过 ${max} 字符限制（当前 ${value.length}）`);
    return;
  }
  if (ttsView.submitting) return;
  ttsView.submitting = true;
  ttsView.lastErrorSignature = "";
  setTtsError("");
  const button = $("tts-play-button");
  if (button) { button.disabled = true; button.textContent = "提交中…"; }
  try {
    const response = await fetch("/api/tts/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: value,
        voice: $("tts-voice-select")?.value || null,
        emotion: $("tts-emotion-select")?.value || "neutral",
        speech_rate: Number($("tts-speed")?.value || 1),
        reason: "dashboard manual tts",
        client_request_id: uuid(),
      }),
    });
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok || !data || data.accepted === false) {
      throw new Error(data?.error?.message || `请求失败 HTTP ${response.status}`);
    }
    const ahead = data.ahead_in_queue || 0;
    notify(ahead > 0 ? `已加入播放队列，前面还有 ${ahead} 条` : "播报任务已提交", "ok");
    renderTts(stateView.state?.tts, data.job);
    loadTtsStatus();
  } catch (error) {
    setTtsError(error.message);
    notify(`播报失败：${error.message}`, "error");
  } finally {
    ttsView.submitting = false;
    renderTts(stateView.state?.tts);
  }
}

function renderTtsHistory(status) {
  const container = $("tts-history");
  if (!container) return;
  const items = status.history || [];
  const signature = items.map(item => `${item.job_id}:${item.phase}`).join("|");
  if (signature === ttsView.historySignature) return;
  ttsView.historySignature = signature;
  container.replaceChildren();
  if (!items.length) {
    const note = document.createElement("p");
    note.className = "empty-note";
    note.textContent = "暂无播报记录";
    container.append(note);
    return;
  }
  items.slice(0, 5).forEach(item => {
    const row = document.createElement("div");
    row.className = `tts-history-item ${item.phase}`;
    const head = document.createElement("div");
    head.className = "tts-history-head";
    const state = document.createElement("strong");
    state.textContent = ttsPhaseLabels[item.phase] || item.phase;
    const time = document.createElement("span");
    time.textContent = clock(item.finished_at || item.created_at);
    head.append(state, time);
    const body = document.createElement("p");
    body.textContent = item.text;
    row.append(head, body);
    const style = document.createElement("small");
    style.className = "tts-history-style";
    style.textContent = `${item.voice || "默认音色"} · ${item.emotion || "neutral"} · ${Number(item.speech_rate || 1).toFixed(2)}×`;
    row.append(style);
    if (item.error) {
      const error = document.createElement("small");
      error.textContent = `${item.error.message}${item.error.detail ? "：" + item.error.detail : ""}`;
      row.append(error);
    }
    container.append(row);
  });
}

function renderTts(status, fallbackJob) {
  const tts = status || stateView.state?.tts;
  if (!tts) return;
  applyTtsConfig(tts);
  const active = ttsActiveJob(tts);
  const job = tts.current || fallbackJob || tts.last;
  const button = $("tts-play-button");
  if (button) {
    button.disabled = active || ttsView.submitting || tts.online === false;
    button.textContent = active ? `${ttsPhaseLabels[tts.current.phase] || "处理中"}…` : "播放";
  }
  const stopButton = $("tts-stop-button");
  if (stopButton) stopButton.disabled = tts.online === false;

  const phase = job?.phase || null;
  text("tts-state", phase ? (ttsPhaseLabels[phase] || phase) : "等待播放");
  const pill = $("tts-status-pill");
  if (pill && tts.online !== false) {
    const label = phase === "queued" ? "排队中"
      : phase === "failed" ? "失败"
        : phase === "cancelled" ? "已取消"
          : phase === "succeeded" ? "成功"
            : phase ? (ttsPhaseLabels[phase] || phase) : "待机";
    const kind = phase === "failed" ? "offline"
      : phase === "queued" || !phase ? "warn" : "online";
    pill.className = `status-pill ${kind}`;
    pill.querySelector("span").textContent = label;
  }

  let detail = "输入文字后点击播放，由 audio_service 合成并经 /audio/speaker_pcm 播放。";
  if (job) {
    const preview = job.text.length > 40 ? `${job.text.slice(0, 40)}…` : job.text;
    const bits = [];
    if (job.elapsed_ms !== null && job.elapsed_ms !== undefined) bits.push(`${Math.round(job.elapsed_ms)} ms`);
    const publish = job.publish || {};
    if (publish.packets) bits.push(`${publish.packets} 包 × ${publish.chunk_bytes} B`);
    if (publish.duration_seconds !== undefined && publish.duration_seconds !== null) bits.push(`${Number(publish.duration_seconds).toFixed(2)} s`);
    if (tts.queue?.pending) bits.push(`队列 ${tts.queue.pending}`);
    if (tts.provider?.last_latency_ms) bits.push(`合成 ${Math.round(tts.provider.last_latency_ms)} ms`);
    detail = `${preview}${bits.length ? " · " + bits.join(" · ") : ""}`;
  }
  text("tts-detail", detail);

  if (tts.online === false) {
    const signature = "offline:audio_service";
    if (signature !== ttsView.lastErrorSignature) {
      ttsView.lastErrorSignature = signature;
      const url = tts.service?.url || "http://127.0.0.1:8091";
      setTtsError(`audio_service 未运行或不可达（${url}），请先执行 audio_service/scripts/run.sh`);
    }
  } else if (job?.phase === "failed" && job.error) {
    const signature = `${job.job_id}:${job.error.code}`;
    if (signature !== ttsView.lastErrorSignature) {
      ttsView.lastErrorSignature = signature;
      setTtsError(`${job.error.message}${job.error.detail ? "：" + job.error.detail : ""}`);
    }
  }
  renderTtsHistory(tts);
}

function initTtsControls() {
  const input = $("tts-text");
  input?.addEventListener("input", () => { updateTtsCharCount(); setTtsError(""); });
  input?.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitTts();
    }
  });
  $("tts-speed")?.addEventListener("input", event => {
    text("tts-speed-value", `${Number(event.target.value).toFixed(2)}×`);
  });
  $("tts-play-button")?.addEventListener("click", submitTts);
  $("tts-stop-button")?.addEventListener("click", stopTts);
}

function applyVolumeStatus(status) {
  if (!status) return;
  const slider = $("volume-slider"), output = $("volume-value");
  const pill = $("volume-state"), detail = $("volume-detail"), errorBox = $("volume-error");
  const card = document.querySelector(".volume-card");
  const dbInput = $("volume-db-input"), dbButton = $("volume-db-apply");
  const dbSupported = status.supported && typeof status.db_gain === "number";
  if (dbInput) {
    dbInput.disabled = !dbSupported;
    if (status.db_min !== undefined) dbInput.min = status.db_min;
    if (status.db_max !== undefined) dbInput.max = status.db_max;
    if (status.db_step !== undefined) dbInput.step = status.db_step;
    if (dbSupported && !volumeView.dbDirty) dbInput.value = status.db_gain;
  }
  if (dbButton) dbButton.disabled = !dbSupported;
  if (slider) {
    slider.disabled = !status.supported;
    if (status.min !== undefined) slider.min = status.min;
    if (status.max !== undefined) slider.max = status.max;
    if (typeof status.percent === "number" && !volumeView.dragging) slider.value = status.percent;
  }
  const percent = typeof status.percent === "number" ? status.percent : null;
  const muted = status.muted ?? percent === 0;
  if (!volumeView.dragging) {
    if (output) output.textContent = percent === null ? "--" : `${percent}%`;
    card?.classList.toggle("is-muted", muted);
  }
  if (pill) {
    const ok = status.supported;
    pill.className = `status-pill ${ok ? (muted ? "warn" : "online") : "offline"}`;
    pill.querySelector("span").textContent = ok ? (muted ? "静音" : "正常") : "不可用";
  }
  if (detail) {
    detail.textContent = status.supported
      ? `${status.scale === "perceptual" ? "听感刻度 · " : ""}ALSA ${(status.controls || []).join(" / ")} · ${status.card}${typeof status.db_gain === "number" ? " · " + status.db_gain.toFixed(1) + " dB" : ""}`
      : (status.error?.message || "扬声器音量不可用");
  }
  if (errorBox) {
    const message = status.error?.message || "";
    errorBox.hidden = !message;
    errorBox.textContent = message
      ? `错误：${message}${status.error?.detail ? "（" + status.error.detail + "）" : ""}`
      : "";
  }
}

async function loadVolume(refresh = false) {
  if (volumeView.dragging || volumeView.saving) return;
  const revision = volumeView.revision;
  try {
    const response = await fetch(`/api/audio/volume${refresh ? "?t=" + Date.now() : ""}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (revision === volumeView.revision && !volumeView.dragging && !volumeView.saving) applyVolumeStatus(data);
  } catch (error) {
    if (revision === volumeView.revision && !volumeView.dragging && !volumeView.saving) {
      applyVolumeStatus({ supported: false, error: { message: `读取音量失败：${error.message}` } });
    }
  }
}

function saveVolume(percent) {
  return saveVolumeRequest({ percent });
}

function saveVolumeDb() {
  const input = $("volume-db-input");
  if (!input || input.value.trim() === "" || !input.checkValidity()) {
    input?.reportValidity();
    return;
  }
  volumeView.dbDirty = false;
  return saveVolumeRequest({ db_gain: Number(input.value) });
}

async function saveVolumeRequest(payload) {
  volumeView.pending = payload;
  volumeView.revision += 1;
  if (volumeView.saving) return;
  volumeView.saving = true;
  try {
    while (volumeView.pending !== null) {
      const value = volumeView.pending;
      volumeView.pending = null;
      try {
        const response = await fetch("/api/audio/volume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(value),
        });
        const data = await response.json();
        if (!response.ok || !data || data.ok === false || !data.supported) {
          throw new Error(data?.error?.message || `请求失败 HTTP ${response.status}`);
        }
        if (volumeView.pending === null && !volumeView.dragging) applyVolumeStatus(data);
      } catch (error) {
        if (volumeView.pending === null && !volumeView.dragging) {
          const errorBox = $("volume-error");
          if (errorBox) { errorBox.hidden = false; errorBox.textContent = `设置音量失败：${error.message}`; }
          notify(`设置音量失败：${error.message}`, "error");
        }
      }
    }
  } finally {
    volumeView.saving = false;
  }
}

function initVolumeControls() {
  const slider = $("volume-slider");
  if (!slider) return;
  slider.addEventListener("input", () => {
    volumeView.dragging = true;
    volumeView.revision += 1;
    const value = Number(slider.value);
    text("volume-value", `${value}%`);
    document.querySelector(".volume-card")?.classList.toggle("is-muted", value === 0);
  });
  slider.addEventListener("change", () => {
    volumeView.dragging = false;
    volumeView.dbDirty = false;
    saveVolume(Number(slider.value));
  });
  $("volume-db-input")?.addEventListener("input", () => {
    volumeView.dbDirty = true;
    volumeView.revision += 1;
  });
  $("volume-db-input")?.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); saveVolumeDb(); }
  });
  $("volume-db-apply")?.addEventListener("click", saveVolumeDb);
}

function render() {
  if (!stateView.state) return;
  renderOverview(); renderJoints(); renderImu(); renderPower();
  const motionNow = performance.now();
  if (motionNow - motionView.lastRender > 150) { motionView.lastRender = motionNow; renderMotionStatus(); renderTts(); }
  const now = performance.now();
  if (now - stateView.lastSlowRender > 900) {
    stateView.lastSlowRender = now; renderSensors(); renderAudioCard(); renderGraph(); drawSparklines(); drawImuChart(); drawBatteryChart();
  }
}

function connectWebSocket() {
  if (stateView.ws) stateView.ws.close();
  const pill = $("ws-pill"); pill.className = "status-pill warn"; pill.querySelector("span").textContent = "正在连接";
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/state`);
  stateView.ws = ws;
  ws.onopen = () => { pill.className = "status-pill online"; pill.querySelector("span").textContent = "实时推送"; };
  ws.onmessage = event => { stateView.state = JSON.parse(event.data); appendLiveHistory(stateView.state); render(); };
  ws.onerror = () => ws.close();
  ws.onclose = () => { if (stateView.ws !== ws) return; pill.className = "status-pill offline"; pill.querySelector("span").textContent = "连接断开 · 重连中"; setTimeout(connectWebSocket, 2000); };
}

async function initialize() {
  initViewTabs();
  initVisionControls();
  initMotionControls();
  initTtsControls();
  initAudioServiceControls();
  initVolumeControls();
  loadVolume();
  setInterval(loadVolume, 2000);
  try {
    const [metadataResponse, stateResponse, capabilityResponse, ttsResponse] = await Promise.all([
      fetch("/api/metadata", { cache: "no-store" }),
      fetch("/api/state", { cache: "no-store" }),
      fetch("/api/motion/capabilities", { cache: "no-store" }).catch(() => null),
      fetch("/api/tts/status", { cache: "no-store" }).catch(() => null),
    ]);
    if (!metadataResponse.ok || !stateResponse.ok) throw new Error("Initial API request failed");
    stateView.metadata = await metadataResponse.json(); stateView.state = await stateResponse.json();
    if (capabilityResponse && capabilityResponse.ok) {
      motionView.capabilities = await capabilityResponse.json();
      buildMotionActions();
    }
    if (ttsResponse && ttsResponse.ok) applyTtsConfig(await ttsResponse.json());
    Object.keys(histories).forEach(key => { histories[key] = stateView.state.history?.[key] || []; });
    trimHistory(stateView.state.server_time); buildJointGroups(); buildSensors(); render(); drawSparklines(); drawImuChart(); drawBatteryChart();
    renderMotionStatus(); renderTts();
  } catch (error) {
    console.error(error); const pill = $("ws-pill"); pill.className = "status-pill offline"; pill.querySelector("span").textContent = "初始化失败";
    return;
  }
  connectWebSocket();
}

initialize();
