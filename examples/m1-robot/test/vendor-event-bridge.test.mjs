import { EventEmitter } from 'node:events'
import test from 'node:test'
import assert from 'node:assert/strict'
import { VendorEventForwarder } from '../vendor-event-bridge.mjs'

class FakeWebSocket extends EventEmitter {
  static instances = []
  constructor(url) {
    super()
    this.url = url
    this.readyState = 0
    this.sent = []
    FakeWebSocket.instances.push(this)
  }
  open() { this.readyState = 1; this.emit('open') }
  send(data) { this.sent.push(JSON.parse(data)) }
  close() { const emit = this.readyState !== 3; this.readyState = 3; if (emit) this.emit('close') }
  message(value) { this.emit('message', Buffer.from(JSON.stringify(value)), false) }
}

function tick() { return new Promise(resolve => setImmediate(resolve)) }

function vendorEvent(delivery = 'respond') {
  return {
    type: 'vendor_audio_event', event_id: `evt-${delivery}`, audio_id: '0092',
    event: 'map_missing', summary: '当前任务需要地图', severity: 'warning',
    suggested_delivery: delivery, occurred_at: 1_794_000_000,
  }
}

function safetyEvent() {
  return {
    ...vendorEvent('interrupt'),
    event_id: 'evt-safety',
    audio_id: '0052',
    event: 'stand_up_failed',
    summary: '机器人尝试起身失败，需要协助',
    severity: 'critical',
  }
}

test.beforeEach(() => { FakeWebSocket.instances = [] })

test('connects with readiness heartbeat and context events stay out of Agent context', async () => {
  const requests = []
  const gateway = {
    ready: true,
    request: async (type, payload) => { requests.push({ type, payload }); return { accepted: true } },
  }
  const forwarder = new VendorEventForwarder({
    url: 'ws://robot/ws/vendor/events', WebSocketImpl: FakeWebSocket, gateway,
    gatewayState: () => ({ ready: true }), instanceId: 'm1-primary', heartbeatMs: 10_000,
  })
  forwarder.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  assert.deepEqual(socket.sent[0], { type: 'agent_hello', instance_id: 'm1-primary', ready: true })
  socket.message({ type: 'vendor_event_ready' })
  socket.message(vendorEvent('context'))
  await tick()

  assert.equal(requests.length, 0)
  assert.deepEqual(socket.sent.at(-1), {
    type: 'vendor_event_decision', event_id: 'evt-context', action: 'ignore',
    reason: 'Internal vendor event logged without user delivery',
  })
  forwarder.stop()
})

test('task warning events stay log-only because tool results own user feedback', async () => {
  const requests = []
  const gateway = {
    ready: true,
    request: async (type, payload) => { requests.push({ type, payload }); return { accepted: true } },
  }
  const forwarder = new VendorEventForwarder({
    url: 'ws://robot/ws/vendor/events', WebSocketImpl: FakeWebSocket, gateway,
    gatewayState: () => ({ ready: true }), heartbeatMs: 10_000,
  })
  forwarder.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  socket.message({ type: 'vendor_event_ready' })
  socket.message(vendorEvent('respond'))
  await tick()
  assert.equal(requests.length, 0)
  assert.equal(socket.sent.at(-1).action, 'ignore')
  assert.equal(forwarder.metrics.ignored, 1)
  forwarder.stop()
})

test('semantic safety events are delegated without exposing vendor prompt metadata', async () => {
  const requests = []
  const gateway = {
    ready: true,
    request: async (type, payload) => { requests.push({ type, payload }); return { accepted: true } },
  }
  const forwarder = new VendorEventForwarder({
    url: 'ws://robot/ws/vendor/events', WebSocketImpl: FakeWebSocket, gateway,
    gatewayState: () => ({ ready: true }), heartbeatMs: 10_000,
  })
  forwarder.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  socket.message({ type: 'vendor_event_ready' })
  socket.message(safetyEvent())
  await tick()

  assert.equal(requests[0].type, 'client.event.publish')
  assert.equal(requests[0].payload.delivery_hint, 'interrupt')
  assert.match(requests[0].payload.text, /机器人尝试起身失败，需要协助/)
  assert.doesNotMatch(requests[0].payload.text, /0052|audio_id|厂商提示音|提示编号/)
  assert.equal(socket.sent.at(-1).action, 'agent')
  assert.equal(forwarder.metrics.agent, 1)
  forwarder.stop()
})

test('unavailable Gateway requests original vendor fallback', async () => {
  let called = false
  const gateway = { ready: false, request: async () => { called = true } }
  const forwarder = new VendorEventForwarder({
    url: 'ws://robot/ws/vendor/events', WebSocketImpl: FakeWebSocket, gateway,
    gatewayState: () => ({ ready: false }), heartbeatMs: 10_000,
  })
  forwarder.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  socket.message({ type: 'vendor_event_ready' })
  socket.message(safetyEvent())
  await tick()
  assert.equal(called, false)
  assert.equal(socket.sent.at(-1).action, 'play_vendor')
  assert.equal(forwarder.metrics.fallback, 1)
  forwarder.stop()
})

test('Gateway delivery failure falls back to original prompt', async () => {
  const gateway = { ready: true, request: async () => { throw new Error('model unavailable') } }
  const forwarder = new VendorEventForwarder({
    url: 'ws://robot/ws/vendor/events', WebSocketImpl: FakeWebSocket, gateway,
    gatewayState: () => ({ ready: true }), heartbeatMs: 10_000,
  })
  forwarder.start()
  const socket = FakeWebSocket.instances[0]
  socket.open()
  socket.message({ type: 'vendor_event_ready' })
  socket.message(safetyEvent())
  await tick()
  assert.equal(socket.sent.at(-1).action, 'play_vendor')
  assert.match(socket.sent.at(-1).reason, /model unavailable/)
  forwarder.stop()
})
