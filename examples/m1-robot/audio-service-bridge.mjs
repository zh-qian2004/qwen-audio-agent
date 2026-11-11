import { randomUUID } from 'node:crypto'

function addListener(socket, type, listener) {
  if (typeof socket.on === 'function') socket.on(type, listener)
  else socket.addEventListener(type, listener)
}

function isOpen(socket) {
  return socket?.readyState === 1
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

function idsMatch(record, event) {
  const streamId = event.stream_id || event.streamId
  const responseId = event.response_id || event.responseId
  return (!streamId || streamId === record.streamId) && (!responseId || responseId === record.responseId)
}

function roundMs(value) {
  return Number.isFinite(value) ? Math.round(value * 10) / 10 : null
}

export class RemoteClockEstimator {
  constructor() {
    this.offsetBaselineMs = null
    this.samples = 0
  }

  observe(remoteSentAtMs, localReceivedAtMs) {
    const rawDeltaMs = Number(localReceivedAtMs) - Number(remoteSentAtMs)
    if (!Number.isFinite(rawDeltaMs)) return null
    this.samples += 1
    if (this.offsetBaselineMs === null || rawDeltaMs < this.offsetBaselineMs) {
      this.offsetBaselineMs = rawDeltaMs
    }
    return {
      rawDeltaMs,
      offsetBaselineMs: this.offsetBaselineMs,
      relativeTransitMs: Math.max(0, rawDeltaMs - this.offsetBaselineMs),
      samples: this.samples,
    }
  }

  remoteToLocal(remoteAtMs) {
    if (this.offsetBaselineMs === null || !Number.isFinite(Number(remoteAtMs))) return null
    return Number(remoteAtMs) + this.offsetBaselineMs
  }
}

export class AudioInputForwarder {
  constructor({ url, WebSocketImpl, sendGateway, gatewayState, onStatus = () => {},
    onTelemetry = () => {}, now = Date.now, clockEstimator = new RemoteClockEstimator(),
    reconnectMinMs = 500, reconnectMaxMs = 5_000, maxGatewayBufferedBytes = 262_144 } = {}) {
    if (!url || !WebSocketImpl || !sendGateway || !gatewayState) throw new TypeError('AudioInputForwarder configuration is incomplete')
    Object.assign(this, { url, WebSocketImpl, sendGateway, gatewayState, onStatus, onTelemetry, now, clockEstimator,
      reconnectMinMs, reconnectMaxMs, maxGatewayBufferedBytes })
    this.socket = null
    this.stopped = true
    this.suppressed = false
    this.ready = false
    this.reconnectDelay = reconnectMinMs
    this.reconnectTimer = null
    this.metrics = { frames: 0, bytes: 0, dropped: 0, reconnects: 0 }
  }

  start() {
    if (!this.stopped) return
    this.stopped = false
    this.#connect()
  }

  stop() {
    this.stopped = true
    clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.ready = false
    const socket = this.socket
    this.socket = null
    socket?.close?.()
  }

  setSuppressed(value) {
    this.suppressed = value === true
    this.onStatus({ component: 'audio_input', state: this.suppressed ? 'suppressed' : 'forwarding' })
  }

  #connect() {
    if (this.stopped) return
    this.onStatus({ component: 'audio_input', state: 'connecting' })
    const socket = new this.WebSocketImpl(this.url)
    this.socket = socket
    addListener(socket, 'open', () => {
      if (this.socket !== socket || this.stopped) return
      this.reconnectDelay = this.reconnectMinMs
      this.onStatus({ component: 'audio_input', state: 'connected' })
    })
    addListener(socket, 'message', (data, isBinary) => {
      if (this.socket !== socket || this.stopped) return
      if (!isBinary) {
        const event = jsonMessage(data, false)
        if (event?.type === 'audio_start') {
          const valid = event.encoding === 'pcm_16000_16_1'
            && Number(event.sample_rate) === 16_000
            && Number(event.channels) === 1
          this.ready = valid
          this.onStatus({ component: 'audio_input', state: valid ? 'ready' : 'invalid_format', event })
        } else if (event?.type === 'audio_stats') this.#reportTiming(event)
        return
      }
      const pcm = asBuffer(data)
      if (!this.ready || this.suppressed || !this.gatewayState().ready
        || this.gatewayState().bufferedAmount > this.maxGatewayBufferedBytes) {
        this.metrics.dropped += 1
        return
      }
      if (this.sendGateway({ type: 'input_audio_buffer.append', audio: pcm.toString('base64') })) {
        this.metrics.frames += 1
        this.metrics.bytes += pcm.length
      } else this.metrics.dropped += 1
    })
    addListener(socket, 'error', error => {
      if (this.socket === socket) this.onStatus({ component: 'audio_input', state: 'error', error })
    })
    addListener(socket, 'close', () => {
      if (this.socket !== socket) return
      this.socket = null
      this.ready = false
      if (this.stopped) return
      this.onStatus({ component: 'audio_input', state: 'disconnected' })
      this.metrics.reconnects += 1
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        this.#connect()
      }, this.reconnectDelay)
      this.reconnectTimer.unref?.()
      this.reconnectDelay = Math.min(this.reconnectMaxMs, this.reconnectDelay * 2)
    })
  }

  #reportTiming(event) {
    const receivedAtMs = this.now()
    const serverSentAtMs = Number(event.server_sent_at_ms ?? Number(event.ts) * 1_000)
    const captureToRobotSendMs = Number(event.capture_to_robot_send_ms ?? event.lag_ms)
    if (!Number.isFinite(serverSentAtMs) || !Number.isFinite(captureToRobotSendMs)) return
    const timing = this.clockEstimator.observe(serverSentAtMs, receivedAtMs)
    if (!timing) return
    this.onTelemetry({
      event: 'audio.input_latency',
      direction: 'robot_to_agent',
      last_sequence: event.last_sequence ?? null,
      sent_frames: event.sent_frames ?? null,
      dropped_frames: event.dropped_frames ?? null,
      queue_depth: event.queue_depth ?? null,
      capture_to_robot_send_ms: roundMs(captureToRobotSendMs),
      robot_clock_raw_delta_ms: roundMs(timing.rawDeltaMs),
      clock_offset_baseline_ms: roundMs(timing.offsetBaselineMs),
      robot_to_connector_relative_ms: roundMs(timing.relativeTransitMs),
      capture_to_connector_lower_bound_ms: roundMs(captureToRobotSendMs + timing.relativeTransitMs),
      clock_samples: timing.samples,
      server_sent_at_ms: serverSentAtMs,
      connector_received_at_ms: receivedAtMs,
    })
  }
}

export class AudioOutputBridge {
  constructor({ url, cancelUrl = '', WebSocketImpl, fetchImpl = globalThis.fetch,
    sendGateway, onPlayback = () => {}, onStatus = () => {},
    onTelemetry = () => {}, now = Date.now, clockEstimator = new RemoteClockEstimator(),
    maxBufferedBytes = 4_800_000, maxPendingResponses = 4,
    maxPendingBufferedBytes = maxBufferedBytes, cancelTimeoutMs = 2_000 } = {}) {
    if (!url || !WebSocketImpl || !sendGateway) throw new TypeError('AudioOutputBridge configuration is incomplete')
    Object.assign(this, {
      url, cancelUrl, WebSocketImpl, fetchImpl, sendGateway, onPlayback, onStatus, onTelemetry, now, clockEstimator,
      maxBufferedBytes, maxPendingResponses, maxPendingBufferedBytes, cancelTimeoutMs,
    })
    this.socket = null
    this.connectPromise = null
    this.connectResolve = null
    this.connectReject = null
    this.stopped = false
    this.record = null
    this.pending = new Map()
    this.ignoredResponses = new Set()
    this.cancelTimer = null
    this.metrics = { responses: 0, bytes: 0, cancelled: 0, errors: 0 }
  }

  handleGateway(event) {
    if (this.stopped || !event) return
    if (event.type === 'audio.delta') this.#audioDelta(event)
    else if (event.type === 'audio.done') this.#audioDone(event)
    else if (event.type === 'playback.clear') this.cancel(event.reason || 'gateway_clear')
    else if (event.type === 'response.interrupted') this.cancel(event.reason || 'response_interrupted', event.responseId)
  }

  stop() {
    this.stopped = true
    this.#cancelPending('connector_stopped')
    this.#finishCancelled('connector_stopped')
    this.#disconnectSocket('connector stopped')
  }

  cancel(reason = 'cancelled', responseId) {
    if (responseId && this.pending.has(responseId)) {
      this.#discardPending(this.pending.get(responseId), reason)
      return
    }
    if (!responseId) this.#cancelPending(reason)
    const record = this.record
    if (!record || (responseId && responseId !== record.responseId)) return
    if (record.cancelRequested) return
    record.cancelRequested = true
    this.#requestRemoteCancel(record, reason)
    if (isOpen(this.socket)) {
      this.socket.send(JSON.stringify({
        type: 'audio_cancel', stream_id: record.streamId, response_id: record.responseId, reason,
      }))
      clearTimeout(this.cancelTimer)
      this.cancelTimer = setTimeout(() => this.#finishCancelled(reason), this.cancelTimeoutMs)
      this.cancelTimer.unref?.()
    } else this.#finishCancelled(reason)
  }

  #audioDelta(event) {
    const responseId = String(event.responseId || '')
    if (!responseId || !event.audio) return
    let pcm
    try { pcm = Buffer.from(event.audio, 'base64') } catch { return }
    if (!pcm.length) return
    if (Number(event.sampleRate || 24_000) !== 24_000) {
      this.#fail('unsupported_sample_rate', `Expected 24000 Hz output, got ${event.sampleRate}`, responseId)
      return
    }
    if (this.ignoredResponses.has(responseId)) return
    const record = this.#recordFor(responseId)
    if (!record || record.cancelRequested || record.inputEnded) return
    const pendingBytes = this.#pendingBufferedBytes()
    if (record.bufferedBytes + pcm.length > this.maxBufferedBytes
      || (record !== this.record && pendingBytes + pcm.length > this.maxPendingBufferedBytes)) {
      if (record === this.record) this.cancel('output_buffer_limit', responseId)
      else this.#discardPending(record, 'output_queue_buffer_limit', true)
      return
    }
    record.queue.push(pcm)
    record.bufferedBytes += pcm.length
    this.metrics.bytes += pcm.length
    if (record === this.record) this.#flush()
  }

  #audioDone(event) {
    const responseId = String(event.responseId || this.record?.responseId || '')
    if (this.ignoredResponses.delete(responseId)) return
    const record = this.record?.responseId === responseId
      ? this.record
      : this.pending.get(responseId)
    if (!record) return
    record.inputEnded = true
    if (record === this.record) this.#flush()
  }

  #recordFor(responseId) {
    if (this.record?.responseId === responseId) return this.record
    if (this.pending.has(responseId)) return this.pending.get(responseId)
    const record = this.#createRecord(responseId)
    if (!this.record) {
      this.#begin(record)
      return record
    }
    if (this.pending.size >= this.maxPendingResponses) {
      this.#rejectResponse(responseId, 'output_queue_full')
      return null
    }
    this.pending.set(responseId, record)
    this.onStatus({
      component: 'audio_output', state: 'queued', responseId,
      pendingResponses: this.pending.size,
    })
    return record
  }

  #createRecord(responseId) {
    this.metrics.responses += 1
    return {
      responseId,
      streamId: null,
      queue: [],
      bufferedBytes: 0,
      ready: false,
      started: false,
      inputEnded: false,
      endSent: false,
      cancelRequested: false,
      firstAudioAtMs: this.now(),
      activatedAtMs: null,
      startSentAtMs: null,
      readyAtMs: null,
      firstPcmSentAtMs: null,
    }
  }

  #pendingBufferedBytes() {
    let bytes = 0
    for (const record of this.pending.values()) bytes += record.bufferedBytes
    return bytes
  }

  #rejectResponse(responseId, reason) {
    this.ignoredResponses.add(responseId)
    this.metrics.cancelled += 1
    this.sendGateway({ type: 'playback.cancelled', responseId, reason })
    this.onStatus({ component: 'audio_output', state: 'cancelled', responseId, reason })
  }

  #discardPending(record, reason, ignoreRemainder = false) {
    if (!record || !this.pending.delete(record.responseId)) return
    record.queue.length = 0
    record.bufferedBytes = 0
    if (ignoreRemainder && !record.inputEnded) this.ignoredResponses.add(record.responseId)
    this.metrics.cancelled += 1
    this.sendGateway({ type: 'playback.cancelled', responseId: record.responseId, reason })
    this.onStatus({ component: 'audio_output', state: 'cancelled', responseId: record.responseId, reason })
  }

  #cancelPending(reason) {
    for (const record of [...this.pending.values()]) this.#discardPending(record, reason, true)
  }

  #requestRemoteCancel(record, reason) {
    if (!this.cancelUrl || typeof this.fetchImpl !== 'function') return
    const body = JSON.stringify({
      stream_id: record.streamId,
      response_id: record.responseId,
      reason,
    })
    Promise.resolve(this.fetchImpl(this.cancelUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
      signal: AbortSignal.timeout(Math.max(500, this.cancelTimeoutMs)),
    })).then(async response => {
      if (!response?.ok) throw new Error(`cancel control returned HTTP ${response?.status || 'unknown'}`)
      const result = await response.json()
      if (result?.accepted === false && this.record === record) {
        this.onStatus({
          component: 'audio_output', state: 'cancel_control_rejected',
          responseId: record.responseId, reason: result.reason,
        })
      }
    }).catch(error => {
      if (this.record === record) {
        this.onStatus({
          component: 'audio_output', state: 'cancel_control_error',
          responseId: record.responseId, error,
        })
      }
    })
  }

  #begin(record) {
    const { responseId } = record
    record.streamId = `m1_${randomUUID().replaceAll('-', '')}`
    record.activatedAtMs = this.now()
    this.record = record
    this.onStatus({ component: 'audio_output', state: 'connecting', responseId })
    this.#ensureSocket().then(() => {
      if (this.record !== record || record.cancelRequested || !isOpen(this.socket)) return
      this.socket.send(JSON.stringify({
        type: 'audio_start',
        stream_id: record.streamId,
        response_id: record.responseId,
        encoding: 'pcm_24000_16_1',
        sample_rate: 24_000,
        channels: 1,
        sample_width: 2,
      }))
      record.startSentAtMs = this.now()
      this.onStatus({ component: 'audio_output', state: 'starting', responseId })
    }).catch(error => {
      if (this.record === record && !record.cancelRequested) {
        this.#fail('audio_output_unavailable', error.message, responseId)
      }
    })
  }

  #ensureSocket() {
    if (isOpen(this.socket)) return Promise.resolve(this.socket)
    if (this.connectPromise) return this.connectPromise
    this.connectPromise = new Promise((resolve, reject) => {
      const socket = new this.WebSocketImpl(this.url)
      this.socket = socket
      this.connectResolve = resolve
      this.connectReject = reject
      const fail = error => {
        if (this.socket !== socket) return
        this.socket = null
        this.connectPromise = null
        this.connectResolve = null
        this.connectReject = null
        reject(error instanceof Error ? error : new Error('audio_service output connection failed'))
      }
      addListener(socket, 'open', () => {
        if (this.socket !== socket) return
        this.connectPromise = null
        this.connectResolve = null
        this.connectReject = null
        resolve(socket)
      })
      addListener(socket, 'message', (data, isBinary) => {
        if (this.socket !== socket || isBinary) return
        const event = jsonMessage(data, false)
        if (event) this.#serviceEvent(event)
      })
      addListener(socket, 'error', fail)
      addListener(socket, 'close', () => {
        if (this.socket !== socket) return
        this.socket = null
        this.connectPromise = null
        this.connectResolve = null
        this.connectReject = null
        if (this.record) this.#finishCancelled('audio_service_disconnected')
      })
    })
    return this.connectPromise
  }

  #serviceEvent(event) {
    const record = this.record
    if (!record || !idsMatch(record, event)) return
    if (event.type === 'output_ready') {
      record.ready = true
      record.readyAtMs = this.now()
      this.onStatus({ component: 'audio_output', state: 'ready', responseId: record.responseId })
      this.#flush()
    } else if (event.type === 'playback_started') {
      if (!record.started) {
        record.started = true
        this.#reportPlaybackTiming(record, event)
        this.sendGateway({ type: 'playback.started', responseId: record.responseId })
        this.onPlayback(true, record)
      }
      this.onStatus({ component: 'audio_output', state: 'playing', responseId: record.responseId })
    } else if (event.type === 'playback_ended') {
      if (!record.started) {
        record.started = true
        this.sendGateway({ type: 'playback.started', responseId: record.responseId })
      }
      this.sendGateway({ type: 'playback.ended', responseId: record.responseId })
      this.#clearRecord('ended')
    } else if (event.type === 'playback_cancelled') {
      this.#finishCancelled(event.reason || 'audio_service_cancelled')
    } else if (event.type === 'output_error') {
      this.#fail(event.error?.code || 'audio_output_error', event.error?.message || event.message || 'audio_service output error', record.responseId)
    }
  }

  #flush() {
    const record = this.record
    if (!record || !record.ready || record.cancelRequested || !isOpen(this.socket)) return
    while (record.queue.length && this.socket.bufferedAmount < 262_144) {
      const chunk = record.queue.shift()
      record.bufferedBytes -= chunk.length
      if (record.firstPcmSentAtMs === null) record.firstPcmSentAtMs = this.now()
      this.socket.send(chunk, { binary: true })
    }
    if (record.inputEnded && !record.queue.length && !record.endSent) {
      record.endSent = true
      this.socket.send(JSON.stringify({
        type: 'audio_end', stream_id: record.streamId, response_id: record.responseId,
      }))
    }
    if (record.queue.length) setTimeout(() => this.#flush(), 5).unref?.()
  }

  #reportPlaybackTiming(record, event) {
    const playbackAckAtMs = this.now()
    const robotPlaybackAtMs = Number(event.ts) * 1_000
    const timing = this.clockEstimator.observe(robotPlaybackAtMs, playbackAckAtMs)
    const robotPlaybackLocalEstimateMs = this.clockEstimator.remoteToLocal(robotPlaybackAtMs)
    this.onTelemetry({
      event: 'audio.output_latency',
      direction: 'agent_to_robot',
      response_id: record.responseId,
      first_audio_to_start_ms: roundMs(record.startSentAtMs - record.firstAudioAtMs),
      queue_wait_ms: roundMs(record.activatedAtMs - record.firstAudioAtMs),
      start_to_ready_ms: roundMs(record.readyAtMs - record.startSentAtMs),
      ready_to_first_pcm_ms: roundMs(record.firstPcmSentAtMs - record.readyAtMs),
      first_pcm_to_playback_ack_ms: roundMs(playbackAckAtMs - record.firstPcmSentAtMs),
      first_audio_to_playback_ack_ms: roundMs(playbackAckAtMs - record.firstAudioAtMs),
      robot_clock_raw_delta_ms: roundMs(timing?.rawDeltaMs),
      clock_offset_baseline_ms: roundMs(timing?.offsetBaselineMs),
      robot_event_to_connector_relative_ms: roundMs(timing?.relativeTransitMs),
      first_pcm_to_robot_playback_estimate_ms: robotPlaybackLocalEstimateMs === null
        ? null
        : roundMs(robotPlaybackLocalEstimateMs - record.firstPcmSentAtMs),
      clock_samples: timing?.samples ?? this.clockEstimator.samples,
      robot_playback_at_ms: Number.isFinite(robotPlaybackAtMs) ? robotPlaybackAtMs : null,
      connector_playback_ack_at_ms: playbackAckAtMs,
    })
  }

  #fail(code, message, responseId) {
    this.metrics.errors += 1
    this.onStatus({ component: 'audio_output', state: 'error', responseId, error: { code, message } })
    if (this.record && (!responseId || this.record.responseId === responseId)) this.#finishCancelled(code)
  }

  #finishCancelled(reason) {
    const record = this.record
    if (!record) return
    this.metrics.cancelled += 1
    this.sendGateway({ type: 'playback.cancelled', responseId: record.responseId, reason })
    this.#clearRecord('cancelled', reason)
  }

  #clearRecord(state, reason) {
    const record = this.record
    if (!record) return
    clearTimeout(this.cancelTimer)
    this.cancelTimer = null
    record.queue.length = 0
    record.bufferedBytes = 0
    this.record = null
    // audio_service handles exactly one stream per WebSocket. Closing here also
    // prevents a following response from racing the server-side close and
    // accidentally sending its audio_start on the completed connection.
    this.#disconnectSocket(`stream ${state}`)
    this.onPlayback(false, record)
    this.onStatus({ component: 'audio_output', state, responseId: record.responseId, reason })
    this.#startNext()
  }

  #startNext() {
    if (this.stopped || this.record || !this.pending.size) return
    const record = this.pending.values().next().value
    this.pending.delete(record.responseId)
    this.#begin(record)
  }

  #disconnectSocket(reason) {
    const socket = this.socket
    const reject = this.connectReject
    this.socket = null
    this.connectPromise = null
    this.connectResolve = null
    this.connectReject = null
    if (reject) reject(new Error(reason))
    socket?.close?.()
  }
}

export class M1AudioBridge {
  constructor({ input, output, halfDuplex = true } = {}) {
    this.input = input
    this.output = output
    this.halfDuplex = halfDuplex
  }
  start() { this.input.start() }
  stop() { this.input.stop(); this.output.stop() }
  handleGateway(event) { this.output.handleGateway(event) }
  setPlaybackActive(active) { if (this.halfDuplex) this.input.setSuppressed(active) }
}
