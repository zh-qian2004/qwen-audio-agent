import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import {
  buildConnectorEnv,
  buildGatewayEnv,
  buildSshArgs,
  loadLauncherConfig,
} from '../launcher-config.mjs'

function configFile(overrides = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'm1-launcher-'))
  const value = {
    ssh: {
      enabled: true,
      target: 'root@robot',
      forwards: [
        { localPort: 18091, remoteHost: '127.0.0.1', remotePort: 8091 },
        { localPort: 18080, remoteHost: '127.0.0.1', remotePort: 8080 },
      ],
    },
    gateway: {
      url: 'ws://127.0.0.1:18888/api/realtime',
      frontendProfile: './profile.json',
      accessToken: 'token',
    },
    connector: {
      audioServiceOrigin: 'http://127.0.0.1:18091',
      dashboardUrl: 'http://127.0.0.1:18080',
    },
    ...overrides,
  }
  const path = join(directory, 'config.json')
  writeFileSync(path, JSON.stringify(value))
  return path
}

test('launcher config resolves paths and builds process environments', () => {
  const config = loadLauncherConfig(configFile())
  assert.ok(config.gateway.frontendProfile.endsWith('profile.json'))
  assert.deepEqual(buildSshArgs(config.ssh).slice(-5), [
    '-L', '18091:127.0.0.1:8091', '-L', '18080:127.0.0.1:8080', 'root@robot',
  ])
  const gatewayEnv = buildGatewayEnv(config.gateway, {})
  assert.equal(gatewayEnv.HOST, '127.0.0.1')
  assert.equal(gatewayEnv.PORT, '18888')
  const connectorEnv = buildConnectorEnv(config, {})
  assert.equal(connectorEnv.M1_GATEWAY_URL, 'ws://127.0.0.1:18888/api/realtime')
  assert.equal(connectorEnv.M1_AUDIO_SERVICE_ORIGIN, 'http://127.0.0.1:18091')
  assert.equal(connectorEnv.M1_HALF_DUPLEX, '1')
})

test('launcher config rejects duplicate local SSH ports', () => {
  const path = configFile({
    ssh: {
      enabled: true,
      target: 'root@robot',
      forwards: [
        { localPort: 18091, remotePort: 8091 },
        { localPort: 18091, remotePort: 8080 },
      ],
    },
  })
  assert.throws(() => loadLauncherConfig(path), /localPort values must be unique/)
})

test('local Gateway requires a loopback ws URL', () => {
  const path = configFile({
    gateway: { startLocal: true, url: 'wss://gateway.example.com/api/realtime' },
  })
  assert.throws(() => loadLauncherConfig(path), /loopback ws:\/\//)
})
