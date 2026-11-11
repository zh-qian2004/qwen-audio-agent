import { EventEmitter } from 'node:events'
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  AudioInputForwarder, AudioOutputBridge, RemoteClockEstimator,
} from '../audio-service-bridge.mjs'

class FakeWebSocket extends EventEmitter {
  static instances = []
  constructor(url) {
    super()
    this.url = url
    this.readyState = 0
    this.bufferedAmount = 0
    this.sent = []
    FakeWebSocket.instances.push(this)
  }
  open() { this.readyState = 1; this.emit('open') }
  send(data, options) { this.sent.push({ data, options }) }
  close() { const wasOpen = this.readyState !== 3; this.readyState = 3; if (wasOpen) this.emit('close') }
  messageJson(value) { this.emit('message', Buffer.from(JSON.stringify(value)), false) }
  messageBinary(value) { this.emit('message', Buffer.from(value), true) }
}

function tick() { return new Promise(resolve => setImmediate(resolve)) }

test.beforeEach(() => { FakeWebSocket.instances = [] })

test('input forwarder validates audio_service metadata and forwards PCM to Gateway', () => {
  const sent = []
  const telemetry = []
  let now = 1_000
  const input = new AudioInputForwarder({
    url: 'ws://audio/ws/audio/input', WebSocketImpl: FakeWebSocket,
    sendGateway: event => { sent.push(event); return true },
    gatewayState: () => ({ ready: true, bufferedAmount: 0 }),
    onTelemetry: event => telemetry.push(event),
    now: () => now,
  })
  input.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  socket.messageJson({ type: 'audio_start', encoding: 'pcm_16000_16_1', sample_rate: 16000, channels: 1 })
  socket.messageBinary(Buffer.from([1, 2, 3, 4]))
  assert.equal(sent.length, 1)
  assert.equal(sent[0].type, 'input_audio_buffer.append')
  assert.deepEqual(Buffer.from(sent[0].audio, 'base64'), Buffer.from([1, 2, 3, 4]))
  input.setSuppressed(true)
  socket.messageBinary(Buffer.from([5, 6]))
  assert.equal(sent.length, 1)
  now = 1_100
  socket.messageJson({
    type: 'audio_stats', last_sequence: 42, sent_frames: 40, dropped_frames: 1,
    queue_depth: 0, capture_to_robot_send_ms: 15, server_sent_at_ms: 1_090,
  })
  assert.deepEqual(telemetry, [{
    event: 'audio.input_latency', direction: 'robot_to_agent', last_sequence: 42,
    sent_frames: 40, dropped_frames: 1, queue_depth: 0,
    capture_to_robot_send_ms: 15, robot_clock_raw_delta_ms: 10,
    clock_offset_baseline_ms: 10, robot_to_connector_relative_ms: 0,
    capture_to_connector_lower_bound_ms: 15, clock_samples: 1,
    server_sent_at_ms: 1_090, connector_received_at_ms: 1_100,
  }])
  input.stop()
})

test('output bridge streams Gateway audio and relays real playback receipts', async () => {
  const receipts = []
  const playback = []
  const telemetry = []
  let now = 1_000
  const clockEstimator = new RemoteClockEstimator()
  clockEstimator.observe(900, 900)
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    sendGateway: event => { receipts.push(event); return true },
    onPlayback: active => playback.push(active),
    onTelemetry: event => telemetry.push(event),
    now: () => now,
    clockEstimator,
  })
  const pcm = Buffer.from([1, 0, 2, 0])
  output.handleGateway({ type: 'audio.delta', responseId: 'r1', sampleRate: 24000, audio: pcm.toString('base64') })
  const socket = FakeWebSocket.instances[0]
  now = 1_010
  socket.open()
  await tick()
  const start = JSON.parse(socket.sent[0].data)
  assert.equal(start.type, 'audio_start')
  assert.equal(start.response_id, 'r1')
  now = 1_020
  socket.messageJson({ type: 'output_ready', stream_id: start.stream_id, response_id: 'r1' })
  assert.deepEqual(socket.sent[1].data, pcm)
  output.handleGateway({ type: 'audio.done', responseId: 'r1' })
  assert.equal(JSON.parse(socket.sent[2].data).type, 'audio_end')
  now = 1_050
  socket.messageJson({ type: 'playback_started', stream_id: start.stream_id, response_id: 'r1', ts: 1.045 })
  socket.messageJson({ type: 'playback_ended', stream_id: start.stream_id, response_id: 'r1' })
  assert.deepEqual(receipts.map(item => item.type), ['playback.started', 'playback.ended'])
  assert.deepEqual(playback, [true, false])
  assert.deepEqual(telemetry, [{
    event: 'audio.output_latency', direction: 'agent_to_robot', response_id: 'r1',
    first_audio_to_start_ms: 10, queue_wait_ms: 0,
    start_to_ready_ms: 10, ready_to_first_pcm_ms: 0,
    first_pcm_to_playback_ack_ms: 30, first_audio_to_playback_ack_ms: 50,
    robot_clock_raw_delta_ms: 5, clock_offset_baseline_ms: 0,
    robot_event_to_connector_relative_ms: 5, first_pcm_to_robot_playback_estimate_ms: 25,
    clock_samples: 2, robot_playback_at_ms: 1_045,
    connector_playback_ack_at_ms: 1_050,
  }])
  assert.equal(socket.readyState, 3)
  output.stop()
})

test('each completed response uses a fresh audio_service WebSocket', async () => {
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    sendGateway: () => true,
  })
  const audio = Buffer.from([1, 0]).toString('base64')

  output.handleGateway({ type: 'audio.delta', responseId: 'r-first', sampleRate: 24000, audio })
  const first = FakeWebSocket.instances[0]
  first.open()
  await tick()
  const firstStart = JSON.parse(first.sent[0].data)
  first.messageJson({ type: 'output_ready', stream_id: firstStart.stream_id, response_id: 'r-first' })
  output.handleGateway({ type: 'audio.done', responseId: 'r-first' })
  first.messageJson({ type: 'playback_started', stream_id: firstStart.stream_id, response_id: 'r-first' })
  first.messageJson({ type: 'playback_ended', stream_id: firstStart.stream_id, response_id: 'r-first' })
  assert.equal(first.readyState, 3)

  output.handleGateway({ type: 'audio.delta', responseId: 'r-second', sampleRate: 24000, audio })
  const second = FakeWebSocket.instances[1]
  assert.notEqual(second, first)
  second.open()
  await tick()
  assert.equal(JSON.parse(second.sent[0].data).response_id, 'r-second')
  output.stop()
})

test('queues a following response until the active playback drains', async () => {
  const receipts = []
  const statuses = []
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    sendGateway: event => { receipts.push(event); return true },
    onStatus: status => statuses.push(status),
  })
  const firstPcm = Buffer.from([1, 0])
  const secondPcm = Buffer.from([2, 0, 3, 0])

  output.handleGateway({
    type: 'audio.delta', responseId: 'r-first', sampleRate: 24000,
    audio: firstPcm.toString('base64'),
  })
  const first = FakeWebSocket.instances[0]
  first.open()
  await tick()
  const firstStart = JSON.parse(first.sent[0].data)
  first.messageJson({ type: 'output_ready', stream_id: firstStart.stream_id, response_id: 'r-first' })
  output.handleGateway({ type: 'audio.done', responseId: 'r-first' })
  first.messageJson({ type: 'playback_started', stream_id: firstStart.stream_id, response_id: 'r-first' })

  output.handleGateway({
    type: 'audio.delta', responseId: 'r-second', sampleRate: 24000,
    audio: secondPcm.toString('base64'),
  })
  output.handleGateway({ type: 'audio.done', responseId: 'r-second' })
  assert.equal(FakeWebSocket.instances.length, 1)
  assert.equal(statuses.at(-1).state, 'queued')

  first.messageJson({ type: 'playback_ended', stream_id: firstStart.stream_id, response_id: 'r-first' })
  const second = FakeWebSocket.instances[1]
  second.open()
  await tick()
  const secondStart = JSON.parse(second.sent[0].data)
  assert.equal(secondStart.response_id, 'r-second')
  second.messageJson({ type: 'output_ready', stream_id: secondStart.stream_id, response_id: 'r-second' })
  assert.deepEqual(second.sent[1].data, secondPcm)
  assert.equal(JSON.parse(second.sent[2].data).type, 'audio_end')
  second.messageJson({ type: 'playback_started', stream_id: secondStart.stream_id, response_id: 'r-second' })
  second.messageJson({ type: 'playback_ended', stream_id: secondStart.stream_id, response_id: 'r-second' })

  assert.deepEqual(receipts.map(event => `${event.type}:${event.responseId}`), [
    'playback.started:r-first',
    'playback.ended:r-first',
    'playback.started:r-second',
    'playback.ended:r-second',
  ])
  output.stop()
})

test('playback.clear cancels both active and queued responses', async () => {
  const receipts = []
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    sendGateway: event => { receipts.push(event); return true }, cancelTimeoutMs: 50,
  })
  const audio = Buffer.from([1, 0]).toString('base64')
  output.handleGateway({ type: 'audio.delta', responseId: 'r-active', sampleRate: 24000, audio })
  const socket = FakeWebSocket.instances[0]
  socket.open()
  await tick()
  const start = JSON.parse(socket.sent[0].data)
  socket.messageJson({ type: 'output_ready', stream_id: start.stream_id, response_id: 'r-active' })
  output.handleGateway({ type: 'audio.delta', responseId: 'r-queued', sampleRate: 24000, audio })
  output.handleGateway({ type: 'audio.done', responseId: 'r-queued' })

  output.handleGateway({ type: 'playback.clear', reason: 'user_interruption' })
  assert.deepEqual(receipts[0], {
    type: 'playback.cancelled', responseId: 'r-queued', reason: 'user_interruption',
  })
  socket.messageJson({
    type: 'playback_cancelled', stream_id: start.stream_id,
    response_id: 'r-active', reason: 'user_interruption',
  })
  assert.deepEqual(receipts[1], {
    type: 'playback.cancelled', responseId: 'r-active', reason: 'user_interruption',
  })
  assert.equal(FakeWebSocket.instances.length, 1)
  output.stop()
})

test('playback.clear cancels audio_service output and returns cancelled receipt', async () => {
  const receipts = []
  const cancelRequests = []
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    cancelUrl: 'http://audio/api/audio/output/cancel',
    fetchImpl: async (url, options) => {
      cancelRequests.push({ url, options })
      return { ok: true, json: async () => ({ ok: true, accepted: true }) }
    },
    sendGateway: event => { receipts.push(event); return true }, cancelTimeoutMs: 50,
  })
  output.handleGateway({ type: 'audio.delta', responseId: 'r2', sampleRate: 24000, audio: Buffer.from([1, 0]).toString('base64') })
  const socket = FakeWebSocket.instances[0]
  socket.open()
  await tick()
  const start = JSON.parse(socket.sent[0].data)
  socket.messageJson({ type: 'output_ready', stream_id: start.stream_id, response_id: 'r2' })
  output.handleGateway({ type: 'playback.clear', reason: 'user_interruption' })
  await tick()
  const cancel = JSON.parse(socket.sent.at(-1).data)
  assert.equal(cancel.type, 'audio_cancel')
  assert.equal(cancelRequests.length, 1)
  assert.equal(cancelRequests[0].url, 'http://audio/api/audio/output/cancel')
  assert.deepEqual(JSON.parse(cancelRequests[0].options.body), {
    stream_id: start.stream_id,
    response_id: 'r2',
    reason: 'user_interruption',
  })
  socket.messageJson({ type: 'playback_cancelled', stream_id: start.stream_id, response_id: 'r2', reason: 'user_interruption' })
  assert.equal(receipts.at(-1).type, 'playback.cancelled')
  assert.equal(receipts.at(-1).responseId, 'r2')
  assert.equal(socket.readyState, 3)
  output.stop()
})

test('audio_service output_busy becomes a cancelled Gateway playback receipt', async () => {
  const receipts = []
  const statuses = []
  const output = new AudioOutputBridge({
    url: 'ws://audio/ws/audio/output', WebSocketImpl: FakeWebSocket,
    sendGateway: event => { receipts.push(event); return true },
    onStatus: status => statuses.push(status),
  })
  output.handleGateway({
    type: 'audio.delta', responseId: 'r-busy', sampleRate: 24000,
    audio: Buffer.from([1, 0]).toString('base64'),
  })
  const socket = FakeWebSocket.instances[0]
  socket.open()
  await tick()
  const start = JSON.parse(socket.sent[0].data)
  socket.messageJson({
    type: 'output_error', stream_id: start.stream_id, response_id: 'r-busy',
    error: { code: 'output_busy', message: '已有实时音频流正在播放' },
  })
  assert.equal(statuses.find(status => status.state === 'error')?.error.code, 'output_busy')
  assert.deepEqual(receipts.at(-1), {
    type: 'playback.cancelled', responseId: 'r-busy', reason: 'output_busy',
  })
  assert.equal(socket.readyState, 3)
  output.stop()
})
