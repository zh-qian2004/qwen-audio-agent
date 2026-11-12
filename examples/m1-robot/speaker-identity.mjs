import { randomUUID } from 'node:crypto'
import { SpeakerTurnStore } from './speaker-turn-store.mjs'

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

function cleanText(value, maxLength = 120) {
  return String(value ?? '').trim().slice(0, maxLength)
}

function finite(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function identityRecord(event, receivedAtMs) {
  return Object.freeze({
    status: cleanText(event.status, 40) || 'unknown',
    speaker_id: cleanText(event.speaker_id, 80) || null,
    display_name: cleanText(event.display_name, 120) || null,
    role: cleanText(event.role, 40) || null,
    confidence: finite(event.confidence),
    margin: finite(event.margin),
    speaker_turn_id: cleanText(event.speaker_turn_id, 120) || null,
    audio_start_sequence: finite(event.audio_start_sequence),
    audio_end_sequence: finite(event.audio_end_sequence),
    audio_ms: finite(event.audio_ms),
    identity_ready_ms: finite(event.identity_ready_ms),
    occurred_at: finite(event.occurred_at),
    received_at_ms: receivedAtMs,
  })
}

export class SpeakerServiceError extends Error {
  constructor(code, message, { status, detail } = {}) {
    super(message)
    this.name = 'SpeakerServiceError'
    this.code = code
    this.status = status
    this.detail = detail
  }
}

export class SpeakerIdentityBridge {
  constructor({ baseUrl, eventUrl, WebSocketImpl, fetchImpl = globalThis.fetch,
    onStatus = () => {}, now = Date.now, turnStorePath = '', sessionId = 'main', readOnlyTurnStore = false,
    timeoutMs = 5_000,
    reconnectMinMs = 500, reconnectMaxMs = 5_000 } = {}) {
    if (!baseUrl || !eventUrl || !WebSocketImpl || typeof fetchImpl !== 'function') {
      throw new TypeError('SpeakerIdentityBridge configuration is incomplete')
    }
    Object.assign(this, {
      baseUrl: String(baseUrl).replace(/\/$/, ''), eventUrl, WebSocketImpl, fetchImpl,
      onStatus, now, timeoutMs, readOnlyTurnStore,
      reconnectMinMs, reconnectMaxMs,
    })
    this.socket = null
    this.stopped = true
    this.ready = false
    this.reconnectDelay = reconnectMinMs
    this.reconnectTimer = null
    this.latestIdentity = null
    this.turnStore = new SpeakerTurnStore({ path: turnStorePath, sessionId, now })
    this.metrics = {
      received: 0, verified: 0, unknown: 0, errors: 0, reconnects: 0,
    }
  }

  start() {
    if (!this.stopped) return
    this.stopped = false
    this.#connect()
  }

  stop() {
    this.stopped = true
    this.ready = false
    clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    const socket = this.socket
    this.socket = null
    socket?.close?.()
  }

  async status() { return this.#request('/api/speakers/status') }

  async list() { return this.#request('/api/speakers') }

  async beginEnrollment({ speaker_id, display_name = '', role = 'guest' } = {}) {
    const id = cleanText(speaker_id, 80)
    const name = cleanText(display_name, 120)
    if (!id && !name) {
      return {
        active: false,
        status: 'needs_input',
        required_fields: ['display_name'],
        user_message: '请先告诉我这位新说话人的名字。',
        guidance: 'Ask for the name and wait. Capture has not started; do not retry unchanged arguments.',
      }
    }
    const speakerId = id || `speaker-${randomUUID()}`
    const result = await this.#request('/api/speakers/enroll/start', {
      method: 'POST',
      body: { speaker_id: speakerId, display_name: name || id, role: cleanText(role, 40) || 'guest' },
    })
    return {
      ...result,
      speaker_id: result?.speaker_id || speakerId,
      guidance: result?.active === true
        ? 'Capture has started. Ask the user to speak naturally; enrollment completes automatically. This is not enrollment success yet.'
        : 'Capture was not confirmed. Do not announce that recording has started.',
    }
  }

  async finishEnrollment() {
    return this.#request('/api/speakers/enroll/finish', { method: 'POST' })
  }

  async cancelEnrollment() {
    return this.#request('/api/speakers/enroll/cancel', { method: 'POST' })
  }

  async deleteProfile(speakerId) {
    return this.#request(`/api/speakers/${encodeURIComponent(String(speakerId || ''))}`, {
      method: 'DELETE',
    })
  }

  async manage({ action, speaker_id, display_name = '', role = 'guest' } = {}) {
    if (action === 'enroll') {
      return this.beginEnrollment({ speaker_id, display_name, role })
    }
    if (action === 'cancel_enrollment') return this.cancelEnrollment()
    if (action === 'list') return this.list()
    if (action === 'delete') {
      if (!String(speaker_id || '').trim()) {
        throw new SpeakerServiceError(
          'speaker_id_required',
          'speaker_id is required when deleting a speaker profile',
        )
      }
      return this.deleteProfile(speaker_id)
    }
    throw new SpeakerServiceError(
      'invalid_speaker_action',
      `Unsupported speaker management action: ${String(action || '')}`,
    )
  }

  currentSpeaker() {
    return this.querySpeaker({ target: 'current' })
  }

  querySpeaker(args = {}) {
    return this.turnStore.query(args, { refresh: this.readOnlyTurnStore })
  }

  observeGatewayEvent(event = {}) {
    if (!this.readOnlyTurnStore) this.turnStore.observe(event)
  }

  async #request(path, { method = 'GET', body } = {}) {
    let response
    try {
      response = await this.fetchImpl(new URL(path, `${this.baseUrl}/`), {
        method,
        headers: body === undefined ? {} : { 'content-type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.timeout(this.timeoutMs),
      })
    } catch (error) {
      throw new SpeakerServiceError(
        error?.name === 'TimeoutError' ? 'speaker_service_timeout' : 'speaker_service_unavailable',
        error?.message || 'Speaker service is unavailable',
      )
    }
    let payload = null
    try { payload = await response.json() } catch { /* preserve HTTP status below */ }
    if (!response.ok) {
      const detail = payload?.detail ?? payload
      const validation = Array.isArray(detail)
        ? detail.map(item => `${(item.loc || []).join('.')}: ${String(item.msg || item.type || 'invalid value')}`).join('; ')
        : null
      throw new SpeakerServiceError(
        `speaker_service_http_${response.status}`,
        typeof detail === 'string' ? detail
          : validation || `Speaker service returned HTTP ${response.status}`,
        { status: response.status, detail },
      )
    }
    return payload
  }

  #connect() {
    if (this.stopped) return
    this.onStatus({ component: 'speaker_identity', state: 'connecting' })
    const socket = new this.WebSocketImpl(this.eventUrl)
    this.socket = socket
    addListener(socket, 'open', () => {
      if (this.socket !== socket || this.stopped) return
      this.reconnectDelay = this.reconnectMinMs
      this.onStatus({ component: 'speaker_identity', state: 'connected' })
    })
    addListener(socket, 'message', (data, isBinary) => {
      if (this.socket !== socket || this.stopped) return
      const event = jsonMessage(data, isBinary)
      if (!event) return
      if (!this.readOnlyTurnStore) this.turnStore.speakerEvent(event)
      if (event.type === 'speaker.status') {
        this.ready = event.ready === true
        this.onStatus({
          component: 'speaker_identity',
          state: this.ready ? 'ready' : 'degraded',
          error: event.last_error || undefined,
        })
      } else if (event.type === 'speaker.identity') {
        this.#handleIdentity(event)
      }
    })
    addListener(socket, 'error', error => {
      if (this.socket !== socket) return
      this.metrics.errors += 1
      this.onStatus({ component: 'speaker_identity', state: 'error', error })
    })
    addListener(socket, 'close', () => {
      if (this.socket !== socket) return
      this.socket = null
      this.ready = false
      if (!this.readOnlyTurnStore) {
        this.turnStore.ready = false
        this.turnStore.save()
      }
      if (this.stopped) return
      this.metrics.reconnects += 1
      this.onStatus({ component: 'speaker_identity', state: 'disconnected' })
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        this.#connect()
      }, this.reconnectDelay)
      this.reconnectTimer.unref?.()
      this.reconnectDelay = Math.min(this.reconnectMaxMs, this.reconnectDelay * 2)
    })
  }

  #handleIdentity(event) {
    const identity = identityRecord(event, this.now())
    this.latestIdentity = identity
    this.metrics.received += 1
    if (identity.status === 'verified') this.metrics.verified += 1
    else this.metrics.unknown += 1
    this.onStatus({
      component: 'speaker_identity',
      state: 'ready',
      identityStatus: identity.status,
    })
  }
}

export { identityRecord }
