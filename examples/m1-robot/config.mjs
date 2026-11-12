function integer(value, fallback, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= min && parsed <= max ? parsed : fallback
}

function websocketUrl(value, name) {
  const url = new URL(value)
  if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error(`${name} must use ws:// or wss://`)
  return url.toString()
}

function httpUrl(value, name) {
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error(`${name} must use http:// or https://`)
  return url.toString().replace(/\/$/, '')
}

export function loadM1Config(env = process.env) {
  const gatewayUrl = websocketUrl(env.M1_GATEWAY_URL || 'ws://127.0.0.1:18888/api/realtime', 'M1_GATEWAY_URL')
  const gatewayHealthUrl = new URL('/api/health', gatewayUrl)
  gatewayHealthUrl.protocol = gatewayHealthUrl.protocol === 'wss:' ? 'https:' : 'http:'
  const audioOrigin = String(env.M1_AUDIO_SERVICE_ORIGIN || 'http://127.0.0.1:8091').replace(/\/$/, '')
  const audioUrl = new URL(audioOrigin)
  audioUrl.protocol = audioUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  const defaultInput = new URL('/ws/audio/input?delivery=continuous&pre_roll_ms=0', audioUrl)
  const defaultOutput = new URL('/ws/audio/output', audioUrl)
  const defaultVendorEvents = new URL('/ws/vendor/events', audioUrl)
  const defaultOutputCancel = new URL('/api/audio/output/cancel', audioOrigin)

  return {
    gatewayUrl,
    gatewayHealthUrl: gatewayHealthUrl.toString(),
    gatewayAccessToken: String(env.M1_GATEWAY_ACCESS_TOKEN || '').trim(),
    audioInputUrl: websocketUrl(env.M1_AUDIO_INPUT_URL || defaultInput, 'M1_AUDIO_INPUT_URL'),
    audioOutputUrl: websocketUrl(env.M1_AUDIO_OUTPUT_URL || defaultOutput, 'M1_AUDIO_OUTPUT_URL'),
    vendorEventUrl: websocketUrl(env.M1_VENDOR_EVENT_URL || defaultVendorEvents, 'M1_VENDOR_EVENT_URL'),
    audioOutputCancelUrl: httpUrl(
      env.M1_AUDIO_OUTPUT_CANCEL_URL || defaultOutputCancel,
      'M1_AUDIO_OUTPUT_CANCEL_URL',
    ),
    dashboardUrl: httpUrl(env.M1_DASHBOARD_URL || 'http://127.0.0.1:8080', 'M1_DASHBOARD_URL'),
    dashboardTimeoutMs: integer(env.M1_DASHBOARD_TIMEOUT_MS, 8_000, { min: 500, max: 60_000 }),
    agentStatusIntervalMs: integer(env.M1_AGENT_STATUS_INTERVAL_MS, 2_000, { min: 500, max: 60_000 }),
    vendorEventHeartbeatMs: integer(env.M1_VENDOR_EVENT_HEARTBEAT_MS, 1_000, { min: 500, max: 10_000 }),
    reconnectMinMs: integer(env.M1_RECONNECT_MIN_MS, 500, { min: 100, max: 30_000 }),
    reconnectMaxMs: integer(env.M1_RECONNECT_MAX_MS, 5_000, { min: 500, max: 120_000 }),
    maxGatewayBufferedBytes: integer(env.M1_MAX_GATEWAY_BUFFERED_BYTES, 262_144, { min: 32_768, max: 8_388_608 }),
    maxOutputBufferedBytes: integer(env.M1_MAX_OUTPUT_BUFFERED_BYTES, 4_800_000, { min: 96_000, max: 33_554_432 }),
    outputCancelTimeoutMs: integer(env.M1_OUTPUT_CANCEL_TIMEOUT_MS, 2_000, { min: 100, max: 30_000 }),
    clientInstanceId: String(env.M1_CLIENT_INSTANCE_ID || 'm1-robot-primary').trim(),
    clientLabel: String(env.M1_CLIENT_LABEL || 'M1 Robot').trim(),
    deploymentMode: String(env.M1_AGENT_DEPLOYMENT_MODE || '').trim(),
    locale: String(env.M1_LOCALE || 'zh-CN').trim(),
    timeZone: String(env.M1_TIME_ZONE || 'Asia/Shanghai').trim(),
    halfDuplex: env.M1_HALF_DUPLEX !== '0',
  }
}
