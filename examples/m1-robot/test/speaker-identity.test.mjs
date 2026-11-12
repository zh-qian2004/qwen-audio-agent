import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { SpeakerIdentityBridge } from '../speaker-identity.mjs'
import { SpeakerTurnStore } from '../speaker-turn-store.mjs'

function fixture() {
  let time = 10000
  const store = new SpeakerTurnStore({ now: () => time })
  store.ready = true
  const start = (id, at) => { time = at; store.observe({ type: 'turn.started', turnId: id }) }
  const end = (id, text, at) => { time = at; store.observe({ type: 'transcript.final', role: 'user', turnId: id, content: text }) }
  const identity = (id, at, status = 'verified', speaker = 'alice') => {
    store.speakerEvent({ type: 'speaker.turn.started', speaker_turn_id: id, occurred_at: at / 1000 })
    store.speakerEvent({ type: 'speaker.identity', speaker_turn_id: id, status,
      speaker_id: status === 'verified' ? speaker : null, display_name: speaker,
      confidence: status === 'verified' ? 0.8 : null, occurred_at: time / 1000 })
  }
  return { store, start, end, identity, at: value => { time = value } }
}

test('queries previous speech rather than borrowing the current speaker', () => {
  const { store, start, end, identity } = fixture()
  start('count', 10000); end('count', '一二三四五六', 22000)
  identity('segment1', 9500, 'unknown')
  start('question', 25000); end('question', '刚刚数数的人是谁', 27000)
  identity('segment2', 24500)
  assert.equal(store.query({ target: 'current' }).speaker_id, 'alice')
  assert.equal(store.query({ target: 'previous' }).status, 'unknown')
  assert.equal(store.query({ target: 'previous' }).speaker_id, null)
  assert.equal(store.query({ target: 'search', query: '一二三' }).turn_id, 'count')
})

test('supports long turns and late identity without locking the first unknown', () => {
  const { store, start, end, identity } = fixture()
  start('long', 10000)
  identity('early', 10000, 'unknown')
  end('long', '长句朗读', 40000)
  identity('late', 22000)
  assert.equal(store.query({ turn_id: 'long' }).status, 'verified')
})

test('returns pending, unavailable and unknown without guessing', () => {
  const { store, start, end, identity, at } = fixture()
  assert.equal(store.query().status, 'unavailable')
  start('one', 10000); end('one', '你好', 11000)
  assert.equal(store.query().status, 'pending')
  at(17000)
  assert.equal(store.query().status, 'unavailable')
  identity('short', 10000, 'insufficient_audio')
  assert.equal(store.query().status, 'unknown')
})

test('conflicting speakers and ambiguous historical matches require clarification', () => {
  const { store, start, end, identity } = fixture()
  start('one', 10000); end('one', '你好', 20000)
  identity('a', 10000, 'verified', 'alice')
  identity('b', 15000, 'verified', 'bob')
  assert.equal(store.query().reason, 'conflicting_speakers')
  start('two', 25000); end('two', '你好', 26000)
  start('question', 30000)
  assert.equal(store.query({ target: 'search', query: '你好' }).reason, 'ambiguous_turn')
})

test('shares records across frontend/backend but isolates sessions', () => {
  const directory = mkdtempSync(join(tmpdir(), 'speaker-turns-'))
  try {
    const path = join(directory, 'turns.json')
    const writer = new SpeakerTurnStore({ path, sessionId: 'one', now: () => 10000 })
    writer.observe({ type: 'turn.started', turnId: 'one-turn' })
    const reader = new SpeakerTurnStore({ path, sessionId: 'one', now: () => 10000 })
    assert.equal(reader.query().turn_id, 'one-turn')
    assert.equal(new SpeakerTurnStore({ path, sessionId: 'two' }).query().reason, 'turn_not_found')
  } finally { rmSync(directory, { recursive: true, force: true }) }
})

test('speaker events remain background-only and registration accepts only a name', async () => {
  const requests = []
  class Socket {
    on(type, handler) { this[type] = handler }
    close() {}
  }
  const bridge = new SpeakerIdentityBridge({ baseUrl: 'http://robot:8093', eventUrl: 'ws://robot/events',
    WebSocketImpl: Socket, fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body); requests.push(body)
      return { ok: true, json: async () => ({ active: true, ...body }) }
    } })
  bridge.start()
  bridge.socket.message(Buffer.from(JSON.stringify({ type: 'speaker.identity',
    speaker_turn_id: 'one', status: 'verified', speaker_id: 'alice' })), false)
  assert.equal(bridge.querySpeaker().status, 'unavailable')
  assert.equal('publishAnnotation' in bridge, false)
  assert.equal((await bridge.manage({ action: 'enroll' })).status, 'needs_input')
  assert.equal(requests.length, 0)
  assert.equal((await bridge.manage({ action: 'enroll', display_name: '豆包' })).active, true)
  assert.match(requests[0].speaker_id, /^speaker-/)
  bridge.stop()
})
