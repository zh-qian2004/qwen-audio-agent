import test from 'node:test'
import assert from 'node:assert/strict'
import {
  builtinMcpServers,
  computerUseMcpServer,
  configuredBackendMcpServers,
  createBuiltinMcpLifecycle,
} from '../src/backend/adapters/acp/builtin-mcp.mjs'

test('computer-use MCP server resolves as stdio descriptor by default', () => {
  const descriptor = computerUseMcpServer({})
  assert.ok(descriptor, 'expected descriptor when package is installed')
  assert.equal(descriptor.name, 'open-computer-use')
  assert.equal(descriptor.type, 'stdio')
  assert.equal(descriptor.command, process.execPath)
  assert.equal(descriptor.args.length, 2)
  assert.match(descriptor.args[0], /open-computer-use/)
  assert.equal(descriptor.args[1], 'mcp')
  assert.deepEqual(descriptor.env, [
    { name: 'ELECTRON_RUN_AS_NODE', value: '1' },
  ])
})

test('computer-use MCP server can be disabled via environment', () => {
  for (const value of ['false', 'off', '0', 'no', 'disabled', 'OFF']) {
    assert.equal(
      computerUseMcpServer({ QWEN_AUDIO_AGENT_COMPUTER_USE: value }),
      null,
      `expected null for ${value}`,
    )
  }
})

test('computer-use MCP server stays enabled for truthy values', () => {
  for (const value of ['', 'true', 'on', '1', 'yes']) {
    assert.ok(
      computerUseMcpServer({ QWEN_AUDIO_AGENT_COMPUTER_USE: value }),
      `expected descriptor for "${value}"`,
    )
  }
})

test('builtinMcpServers returns descriptor list and filters disabled entries', () => {
  const enabled = builtinMcpServers({})
  assert.equal(enabled.length, 1)
  assert.equal(enabled[0].name, 'open-computer-use')

  const disabled = builtinMcpServers({ QWEN_AUDIO_AGENT_COMPUTER_USE: 'off' })
  assert.deepEqual(disabled, [])
})

test('configured backend MCP servers are validated and appended', () => {
  const configured = [{
    name: 'm1-robot', type: 'stdio', command: process.execPath,
    args: ['backend-mcp.mjs'], env: [{ name: 'M1_DASHBOARD_URL', value: 'http://127.0.0.1:18080' }],
  }]
  const env = {
    QWEN_AUDIO_AGENT_COMPUTER_USE: 'off',
    QWEN_AUDIO_BACKEND_MCP_SERVERS: JSON.stringify(configured),
  }
  assert.deepEqual(configuredBackendMcpServers(env), configured)
  assert.deepEqual(builtinMcpServers(env), configured)
})

test('configured backend MCP rejects invalid descriptors and duplicate names', () => {
  assert.throws(() => configuredBackendMcpServers({
    QWEN_AUDIO_BACKEND_MCP_SERVERS: '{',
  }), /invalid JSON/)
  assert.throws(() => configuredBackendMcpServers({
    QWEN_AUDIO_BACKEND_MCP_SERVERS: JSON.stringify([
      { name: 'same', command: 'one' },
      { name: 'same', command: 'two' },
    ]),
  }), /Duplicate backend MCP server name/)
})

test('cleans only app-agents created after the builtin MCP lifecycle starts', async () => {
  const servers = builtinMcpServers({})
  let time = 0
  const processes = [
    { pid: 10, command: '/pkg/OpenComputerUse __open-computer-use-app-agent /tmp/old.sock' },
  ]
  const signals = []
  const lifecycle = createBuiltinMcpLifecycle(servers, {
    platform: 'darwin',
    discoveryMs: 100,
    now: () => time,
    delay: async ms => {
      time += ms
    },
    listProcesses: () => processes.map(item => ({ ...item })),
    killImpl(pid, signal) {
      signals.push([pid, signal])
    },
  })
  processes.push({
    pid: 20,
    command: '/pkg/OpenComputerUse __open-computer-use-app-agent /tmp/new.sock',
  })

  lifecycle.markUsed()
  await lifecycle.close()
  assert.deepEqual(signals, [
    [20, 'SIGTERM'],
    [20, 'SIGKILL'],
  ])
})

test('preserves a new app-agent while another MCP process is active', async () => {
  const servers = builtinMcpServers({})
  let time = 0
  const processes = []
  const signals = []
  const lifecycle = createBuiltinMcpLifecycle(servers, {
    platform: 'darwin',
    discoveryMs: 50,
    now: () => time,
    delay: async ms => {
      time += ms
    },
    listProcesses: () => processes.map(item => ({ ...item })),
    killImpl: (...args) => signals.push(args),
  })
  processes.push(
    {
      pid: 20,
      command: '/pkg/OpenComputerUse __open-computer-use-app-agent /tmp/new.sock',
    },
    { pid: 30, command: '/pkg/OpenComputerUse mcp' },
  )

  lifecycle.markUsed()
  await lifecycle.close()
  assert.deepEqual(signals, [])
})

test('builtin MCP lifecycle is a no-op off macOS or without the managed server', async () => {
  let listed = false
  const options = {
    listProcesses: () => {
      listed = true
      return []
    },
  }
  await createBuiltinMcpLifecycle(builtinMcpServers({}), {
    ...options,
    platform: 'linux',
  }).close()
  await createBuiltinMcpLifecycle([], {
    ...options,
    platform: 'darwin',
  }).close()
  assert.equal(listed, false)
})
