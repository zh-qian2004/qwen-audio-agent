import { randomUUID } from 'node:crypto'

export class DashboardError extends Error {
  constructor(code, message, { status, detail } = {}) {
    super(message)
    this.name = 'DashboardError'
    this.code = code
    this.status = status
    this.detail = detail
  }
}

function bounded(value, maxBytes = 24_000) {
  const encoded = JSON.stringify(value)
  if (Buffer.byteLength(encoded) <= maxBytes) return value
  return {
    truncated: true,
    message: 'Dashboard response exceeded the client-tool result limit',
    keys: value && typeof value === 'object' ? Object.keys(value).slice(0, 64) : [],
  }
}

export function enabledCommands(value) {
  const found = new Map()
  const visit = (item) => {
    if (Array.isArray(item)) {
      for (const child of item) visit(child)
      return
    }
    if (!item || typeof item !== 'object') return
    const looksLikeCommand = Object.hasOwn(item, 'dashboard_enabled')
      || Object.hasOwn(item, 'vendor_command')
      || Object.hasOwn(item, 'interaction')
    if (looksLikeCommand && typeof item.id === 'string' && item.id && item.dashboard_enabled !== false) {
      found.set(item.id, {
        id: item.id,
        label: item.label || item.name || item.id,
        category: item.category || item.category_id,
        interaction: item.interaction || 'trigger',
        risk: item.risk || 'unknown',
        requiresConfirmation: item.requires_confirmation === true || item.confirmation === 'confirm',
      })
    }
    for (const child of Object.values(item)) visit(child)
  }
  visit(value)
  return [...found.values()]
}

export class DashboardClient {
  constructor({ baseUrl, timeoutMs = 8_000, fetchImpl = fetch } = {}) {
    if (!baseUrl) throw new TypeError('Dashboard baseUrl is required')
    this.baseUrl = String(baseUrl).replace(/\/$/, '')
    this.timeoutMs = timeoutMs
    this.fetchImpl = fetchImpl
  }

  async request(path, {
    method = 'GET', body, timeoutMs = this.timeoutMs, maxResponseBytes = 24_000,
  } = {}) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    timer.unref?.()
    try {
      const response = await this.fetchImpl(new URL(path, `${this.baseUrl}/`), {
        method,
        signal: controller.signal,
        headers: body === undefined ? {} : { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const text = await response.text()
      let payload = null
      try { payload = text ? JSON.parse(text) : {} } catch {
        throw new DashboardError('invalid_json', `Dashboard returned invalid JSON for ${path}`, {
          status: response.status,
        })
      }
      if (!response.ok) {
        const error = payload?.error || {}
        throw new DashboardError(
          error.code || `http_${response.status}`,
          error.message || `Dashboard request failed with HTTP ${response.status}`,
          { status: response.status, detail: error.detail || payload },
        )
      }
      return bounded(payload, maxResponseBytes)
    } catch (error) {
      if (error instanceof DashboardError) throw error
      if (error?.name === 'AbortError') {
        throw new DashboardError('dashboard_timeout', `Dashboard request timed out after ${timeoutMs} ms`)
      }
      throw new DashboardError('dashboard_unavailable', error?.message || String(error))
    } finally {
      clearTimeout(timer)
    }
  }

  health() { return this.request('/api/health') }
  currentState() { return this.request('/api/state/current') }
  state() { return this.currentState() }
  motionStatus() { return this.request('/api/motion/status') }
  motionCapabilities() { return this.request('/api/motion/capabilities') }
  vehicleStatus() { return this.request('/api/vehicle/status') }
  visionStatus() { return this.request('/api/vision/status') }
  async visionDetections() {
    try {
      return await this.request('/api/vision/detections/latest')
    } catch (error) {
      // No result yet is a valid observation state, not a tool transport failure.
      if (error instanceof DashboardError && error.status === 503 && error.detail?.available === false) {
        return error.detail
      }
      throw error
    }
  }
  setVolume(percent) {
    return this.request('/api/audio/volume', { method: 'POST', body: { percent } })
  }
  audioAgentHeartbeat(payload) {
    return this.request('/api/audio/agent/heartbeat', {
      method: 'POST', body: payload, timeoutMs: Math.min(this.timeoutMs, 3_000),
    })
  }
  command(commandId, {
    reason = 'qwen audio agent tool call', clientRequestId = randomUUID(), durationMs,
  } = {}) {
    return this.request('/api/motion/command', {
      method: 'POST',
      body: {
        command_id: commandId,
        reason,
        client_request_id: clientRequestId,
        ...(durationMs === undefined ? {} : { duration_ms: durationMs }),
      },
    })
  }
  stop({ reason = 'qwen audio agent stop', clientRequestId = randomUUID() } = {}) {
    return this.request('/api/motion/stop', {
      method: 'POST',
      body: { reason, client_request_id: clientRequestId },
    })
  }
  emergencyStop({ reason = 'qwen audio agent emergency stop' } = {}) {
    return this.request('/api/motion/emergency-stop', { method: 'POST', body: { reason } })
  }
}
