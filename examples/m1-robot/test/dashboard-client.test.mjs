import test from 'node:test'
import assert from 'node:assert/strict'
import { DashboardClient, DashboardError, enabledCommands } from '../dashboard-client.mjs'

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload) }
}

test('DashboardClient sends idempotent motion payloads and parses results', async () => {
  const calls = []
  const client = new DashboardClient({
    baseUrl: 'http://127.0.0.1:8080',
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options })
      return response(200, { accepted: true })
    },
  })
  const result = await client.command('wave_hand', {
    reason: 'test', clientRequestId: 'req-1', durationMs: 750,
  })
  assert.equal(result.accepted, true)
  assert.equal(calls[0].url, 'http://127.0.0.1:8080/api/motion/command')
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    command_id: 'wave_hand', reason: 'test', client_request_id: 'req-1', duration_ms: 750,
  })
})

test('DashboardClient normalizes API and timeout failures', async () => {
  const api = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async () => response(409, { error: { code: 'motion_busy', message: 'busy' } }),
  })
  await assert.rejects(api.state(), error => error instanceof DashboardError && error.code === 'motion_busy')

  const timeout = new DashboardClient({
    baseUrl: 'http://robot', timeoutMs: 5,
    fetchImpl: (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })))
    }),
  })
  await assert.rejects(timeout.state(), error => error.code === 'dashboard_timeout')
})

test('DashboardClient posts Audio Agent heartbeat', async () => {
  let request
  const client = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async (url, options) => {
      request = { url: String(url), options }
      return response(200, { online: true })
    },
  })
  await client.audioAgentHeartbeat({ instance_id: 'm1-primary' })
  assert.equal(request.url, 'http://robot/api/audio/agent/heartbeat')
  assert.equal(request.options.method, 'POST')
  assert.deepEqual(JSON.parse(request.options.body), { instance_id: 'm1-primary' })
})

test('DashboardClient state uses the compact current-state endpoint', async () => {
  const calls = []
  const client = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async url => {
      calls.push(String(url))
      return response(200, {
      battery: { power: 8, is_charging: true, on_charger_raw: 2 },
      motion: { control_mode: 1 },
      })
    },
  })
  const state = await client.state()
  assert.deepEqual(state.battery, { power: 8, is_charging: true, on_charger_raw: 2 })
  assert.equal(state.history, undefined)
  assert.equal(state.truncated, undefined)
  assert.deepEqual(calls, ['http://robot/api/state/current'])
})

test('DashboardClient exposes the read-only vision endpoints', async () => {
  const calls = []
  const client = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async url => {
      calls.push(String(url))
      return response(200, { status: 'ok' })
    },
  })
  await client.visionStatus()
  await client.visionDetections()
  assert.deepEqual(calls, [
    'http://robot/api/vision/status',
    'http://robot/api/vision/detections/latest',
  ])
})

test('DashboardClient exposes the read-only vehicle endpoint', async () => {
  const calls = []
  const client = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async url => {
      calls.push(String(url))
      return response(200, { mount: { state: 'on_vehicle' } })
    },
  })
  const status = await client.vehicleStatus()
  assert.equal(status.mount.state, 'on_vehicle')
  assert.deepEqual(calls, ['http://robot/api/vehicle/status'])
})

test('DashboardClient treats an empty vision result as an available tool response', async () => {
  const client = new DashboardClient({
    baseUrl: 'http://robot',
    fetchImpl: async () => response(503, { available: false, error: 'no_detection_result' }),
  })
  assert.deepEqual(await client.visionDetections(), {
    available: false,
    error: 'no_detection_result',
  })
})

test('enabledCommands discovers command definitions without treating categories as commands', () => {
  const commands = enabledCommands({ categories: [{ id: 'head_arm', label: 'Head and arm', commands: [
    { id: 'wave_hand', dashboard_enabled: true, interaction: 'trigger' },
    { id: 'reset', dashboard_enabled: false },
  ] }], duplicate: { id: 'wave_hand', dashboard_enabled: true } })
  assert.deepEqual(commands.map(item => item.id), ['wave_hand'])
})

test('enabledCommands recognizes Dashboard confirmation metadata', () => {
  const [command] = enabledCommands({ commands: [{
    id: 'return_to_charge', dashboard_enabled: true, interaction: 'workflow', confirmation: 'confirm',
  }] })
  assert.equal(command.requiresConfirmation, true)
})
