import assert from 'node:assert/strict'
import test from 'node:test'
import { AgentStatusReporter, publicGatewayHealth } from '../agent-status-reporter.mjs'

test('public Gateway health keeps model and connection state', () => {
  const value = publicGatewayHealth({
    status: 'ready', voiceConfigured: true, realtimeProvider: 'dashscope',
    realtimeModel: 'qwen-audio-realtime', voiceClients: { connected: 1, activeOwners: 1 },
    backend: { ok: true, status: 'ready', kind: 'acp', model: 'qwen3', secret: 'hidden' },
  })
  assert.equal(value.realtime.model, 'qwen-audio-realtime')
  assert.equal(value.backend.model, 'qwen3')
  assert.equal(value.voice_clients.connected, 1)
  assert.equal(value.backend.secret, undefined)
})

test('reporter posts connector and Gateway status to Dashboard', async () => {
  const heartbeats = []
  const reporter = new AgentStatusReporter({
    dashboard: { audioAgentHeartbeat: async value => heartbeats.push(value) },
    healthUrl: 'http://gateway/api/health',
    instanceId: 'm1-primary', label: 'M1 Robot', deploymentMode: 'edge',
    connectorStatus: () => ({ gateway: { state: 'ready' } }),
    fetchImpl: async () => ({ ok: true, json: async () => ({
      status: 'ready', realtimeProvider: 'dashscope', realtimeModel: 'qwen-audio-realtime',
      backend: { ok: true, status: 'ready', model: 'qwen3' },
    }) }),
  })
  await reporter.report()
  assert.equal(heartbeats.length, 1)
  assert.equal(heartbeats[0].instance_id, 'm1-primary')
  assert.equal(heartbeats[0].deployment_mode, 'edge')
  assert.equal(heartbeats[0].connector.gateway.state, 'ready')
  assert.equal(heartbeats[0].gateway.backend.model, 'qwen3')
})
