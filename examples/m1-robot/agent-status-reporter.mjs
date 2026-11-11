function selected(source, keys) {
  if (!source || typeof source !== 'object') return {}
  return Object.fromEntries(keys.filter(key => source[key] !== undefined).map(key => [key, source[key]]))
}

export function publicGatewayHealth(health = {}) {
  return {
    status: health.status || 'unknown',
    gateway_instance_id: health.gatewayInstanceId || null,
    started_at: health.gatewayStartedAt || null,
    frontend_profile: selected(health.frontendProfile, ['configured', 'name', 'description']),
    realtime: {
      configured: health.voiceConfigured === true,
      provider: health.realtimeProvider || null,
      label: health.realtimeLabel || null,
      model: health.realtimeModel || null,
      input_sample_rate: health.realtimeInputSampleRate || null,
    },
    backend: selected(health.backend, [
      'ok', 'status', 'code', 'kind', 'label', 'model', 'protocol', 'backendAgent', 'error',
    ]),
    voice_clients: selected(health.voiceClients, ['connected', 'activeOwners', 'byType']),
  }
}

export class AgentStatusReporter {
  constructor({ dashboard, healthUrl, accessToken = '', instanceId, label,
    connectorStatus, intervalMs = 2_000, fetchImpl = globalThis.fetch, onStatus = () => {} } = {}) {
    if (!dashboard || !healthUrl || !instanceId || typeof connectorStatus !== 'function') {
      throw new TypeError('AgentStatusReporter configuration is incomplete')
    }
    Object.assign(this, {
      dashboard, healthUrl, accessToken, instanceId, label, connectorStatus,
      intervalMs, fetchImpl, onStatus,
    })
    this.timer = null
    this.running = false
    this.lastState = null
  }

  start() {
    if (this.timer) return
    this.report()
    this.timer = setInterval(() => this.report(), this.intervalMs)
    this.timer.unref?.()
  }

  stop() {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  async report() {
    if (this.running) return
    this.running = true
    let gateway
    try {
      gateway = await this.#readGateway()
    } catch (error) {
      gateway = { status: 'offline', error: error?.message || String(error) }
    }
    try {
      await this.dashboard.audioAgentHeartbeat({
        instance_id: this.instanceId,
        label: this.label,
        connector: this.connectorStatus(),
        gateway,
      })
      this.#note('online')
    } catch (error) {
      this.#note('error', error)
    } finally {
      this.running = false
    }
  }

  async #readGateway() {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), Math.min(this.intervalMs, 3_000))
    timer.unref?.()
    try {
      const response = await this.fetchImpl(this.healthUrl, {
        signal: controller.signal,
        cache: 'no-store',
        headers: this.accessToken ? { authorization: `Bearer ${this.accessToken}` } : {},
      })
      if (!response.ok) throw new Error(`Gateway health returned HTTP ${response.status}`)
      return publicGatewayHealth(await response.json())
    } finally {
      clearTimeout(timer)
    }
  }

  #note(state, error) {
    if (state === this.lastState) return
    this.lastState = state
    this.onStatus({
      component: 'agent_status', state,
      ...(error ? { error: { message: error?.message || String(error) } } : {}),
    })
  }
}
