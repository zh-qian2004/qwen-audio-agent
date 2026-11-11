function addListener(socket, type, listener) {
  if (typeof socket.on === 'function') socket.on(type, listener)
  else socket.addEventListener(type, listener)
}

function asBuffer(value) {
  if (Buffer.isBuffer(value)) return value
  if (value instanceof ArrayBuffer) return Buffer.from(value)
  if (ArrayBuffer.isView(value)) return Buffer.from(value.buffer, value.byteOffset, value.byteLength)
  return Buffer.from(value)
}

function jsonMessage(data, isBinary) {
  if (isBinary) return null
  try { return JSON.parse(asBuffer(data).toString('utf8')) } catch { return null }
}

function boundedDelivery(value) {
  return ['context', 'respond', 'interrupt'].includes(value) ? value : 'respond'
}

function requiresUserAttention(event) {
  const delivery = boundedDelivery(event.suggested_delivery)
  const semantic = String(event.event || '').trim()
  const summary = String(event.summary || '').trim()
  return delivery === 'interrupt'
    && semantic !== ''
    && semantic !== 'vendor_prompt'
    && summary !== ''
}

function eventText(event) {
  return `机器人安全状态：${String(event.summary).trim()}。请仅向用户简短说明当前状态和需要采取的行动，不要描述内部实现。`
}

export class VendorEventForwarder {
  constructor({ url, WebSocketImpl, gateway, instanceId = 'm1-robot-primary',
    gatewayState = () => ({ ready: gateway?.ready === true }), onStatus = () => {},
    reconnectMinMs = 500, reconnectMaxMs = 5_000, heartbeatMs = 1_000 } = {}) {
    if (!url || !WebSocketImpl || !gateway) throw new TypeError('VendorEventForwarder configuration is incomplete')
    Object.assign(this, {
      url, WebSocketImpl, gateway, instanceId, gatewayState, onStatus,
      reconnectMinMs, reconnectMaxMs, heartbeatMs,
    })
    this.socket = null
    this.stopped = true
    this.ready = false
    this.reconnectDelay = reconnectMinMs
    this.reconnectTimer = null
    this.heartbeatTimer = null
    this.metrics = { received: 0, agent: 0, ignored: 0, fallback: 0, errors: 0, reconnects: 0 }
  }

  start() {
    if (!this.stopped) return
    this.stopped = false
    this.#connect()
  }

  stop() {
    this.stopped = true
    clearTimeout(this.reconnectTimer)
    clearInterval(this.heartbeatTimer)
    this.reconnectTimer = null
    this.heartbeatTimer = null
    this.ready = false
    const socket = this.socket
    this.socket = null
    socket?.close?.()
  }

  #connect() {
    if (this.stopped) return
    this.onStatus({ component: 'vendor_events', state: 'connecting' })
    const socket = new this.WebSocketImpl(this.url)
    this.socket = socket
    addListener(socket, 'open', () => {
      if (this.socket !== socket || this.stopped) return
      this.reconnectDelay = this.reconnectMinMs
      socket.send(JSON.stringify({
        type: 'agent_hello', instance_id: this.instanceId,
        ready: this.gatewayState().ready === true,
      }))
      this.#startHeartbeat(socket)
    })
    addListener(socket, 'message', (data, isBinary) => {
      if (this.socket !== socket || this.stopped) return
      const event = jsonMessage(data, isBinary)
      if (!event) return
      if (event.type === 'vendor_event_ready') {
        this.ready = true
        this.onStatus({ component: 'vendor_events', state: 'ready' })
      } else if (event.type === 'vendor_audio_event') {
        this.#handleEvent(socket, event)
      } else if (event.type === 'agent_replaced') {
        socket.close()
      }
    })
    addListener(socket, 'error', error => {
      if (this.socket === socket) {
        this.metrics.errors += 1
        this.onStatus({ component: 'vendor_events', state: 'error', error })
      }
    })
    addListener(socket, 'close', () => {
      if (this.socket !== socket) return
      this.socket = null
      this.ready = false
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
      if (this.stopped) return
      this.metrics.reconnects += 1
      this.onStatus({ component: 'vendor_events', state: 'disconnected' })
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        this.#connect()
      }, this.reconnectDelay)
      this.reconnectTimer.unref?.()
      this.reconnectDelay = Math.min(this.reconnectMaxMs, this.reconnectDelay * 2)
    })
  }

  #startHeartbeat(socket) {
    clearInterval(this.heartbeatTimer)
    this.heartbeatTimer = setInterval(() => {
      if (this.socket !== socket || socket.readyState !== 1) return
      socket.send(JSON.stringify({
        type: 'agent_heartbeat', ready: this.gatewayState().ready === true,
      }))
    }, this.heartbeatMs)
    this.heartbeatTimer.unref?.()
  }

  async #handleEvent(socket, event) {
    this.metrics.received += 1
    if (!requiresUserAttention(event)) {
      this.#decision(socket, event, 'ignore', 'Internal vendor event logged without user delivery')
      return
    }
    if (!this.gatewayState().ready || !this.gateway.ready) {
      this.#decision(socket, event, 'play_vendor', 'Gateway is not ready')
      return
    }
    try {
      const result = await this.gateway.request('client.event.publish', {
        text: eventText(event),
        delivery_hint: 'interrupt',
        occurred_at: Math.max(0, Math.round(Number(event.occurred_at || Date.now() / 1_000) * 1_000)),
      }, { timeoutMs: 1_000 })
      if (this.socket !== socket || socket.readyState !== 1) return
      if (result?.accepted !== true) {
        this.#decision(socket, event, 'play_vendor', 'Gateway rejected the event')
        return
      }
      this.#decision(socket, event, 'agent', 'Gateway accepted semantic safety event')
    } catch (error) {
      this.metrics.errors += 1
      this.#decision(socket, event, 'play_vendor', error?.message || 'Gateway event delivery failed')
    }
  }

  #decision(socket, event, action, reason) {
    if (this.socket !== socket || socket.readyState !== 1) return
    if (action === 'play_vendor') this.metrics.fallback += 1
    else if (action === 'ignore') this.metrics.ignored += 1
    else this.metrics.agent += 1
    socket.send(JSON.stringify({
      type: 'vendor_event_decision', event_id: event.event_id, action, reason,
    }))
    this.onStatus({
      component: 'vendor_events', state: action,
      event: event.event, audio_id: event.audio_id, delivery: event.suggested_delivery,
    })
  }
}
