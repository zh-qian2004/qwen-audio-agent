#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { WebSocket } from 'ws'
import { join } from 'node:path'
import { resolveRuntimePaths } from '../../shared/runtime-paths.mjs'
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { loadM1Config } from './config.mjs'
import { DashboardClient } from './dashboard-client.mjs'
import { M1RobotTools } from './robot-tools.mjs'
import { SpeakerIdentityBridge } from './speaker-identity.mjs'

const config = loadM1Config()
const dashboard = new DashboardClient({
  baseUrl: config.dashboardUrl,
  timeoutMs: config.dashboardTimeoutMs,
})
const speakers = new SpeakerIdentityBridge({
  baseUrl: config.speakerServiceOrigin,
  eventUrl: config.speakerEventUrl,
  WebSocketImpl: WebSocket,
  fetchImpl: globalThis.fetch,
  timeoutMs: config.speakerRequestTimeoutMs,
  maxIdentityAgeMs: config.speakerIdentityMaxAgeMs,
  reconnectMinMs: config.reconnectMinMs,
  reconnectMaxMs: config.reconnectMaxMs,
  turnStorePath: join(resolveRuntimePaths().stateDirectory, 'm1-speaker-turns.json'),
  sessionId: new URL(config.gatewayUrl).searchParams.get('sessionId') || 'main',
  readOnlyTurnStore: true,
  onStatus: status => {
    if (['error', 'context_error'].includes(status.state)) {
      console.error(`[m1-backend-mcp] speaker service ${status.state}: ${status.error?.message || status.error || 'unknown error'}`)
    }
  },
})
const robotTools = new M1RobotTools({ dashboard, speakers })

try {
  await robotTools.initialize()
} catch (error) {
  // Read-only observation tools remain useful while Dashboard capability
  // discovery is temporarily unavailable. Never write protocol logs to stdout.
  console.error(`[m1-backend-mcp] capability discovery failed: ${error.message}`)
}

const definitions = robotTools.definitions({ includeBackgroundTools: true })
const available = new Set(definitions.map(item => item.name))
const server = new Server({ name: 'm1-robot', version: '1.0.0' }, {
  capabilities: { tools: {} },
  instructions: [
    'These tools observe and control the physical Zeroth M1 robot.',
    'For open-ended goals, use a closed loop: observe, choose one bounded action, wait for its operation_id with m1_wait_for_motion, then observe again and replan.',
    'Do not infer physical completion from command acceptance. Stop when the goal is verified, progress stalls, a safety precondition fails, the task is cancelled, or the available time/action budget is exhausted.',
    'Use m1_stop for an ordinary stop and m1_emergency_stop for imminent danger.',
  ].join(' '),
})

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: definitions.map(item => ({
    name: item.name,
    description: item.description,
    inputSchema: item.inputSchema,
  })),
}))

server.setRequestHandler(CallToolRequestSchema, async request => {
  const name = String(request.params.name || '')
  if (!available.has(name)) {
    return {
      isError: true,
      content: [{ type: 'text', text: JSON.stringify({
        error: { code: 'unknown_m1_tool', message: `Unknown M1 tool: ${name}` },
      }) }],
    }
  }
  const result = await robotTools.handle({
    name,
    arguments: request.params.arguments || {},
  })
  const failed = result.status !== 'completed'
  const payload = failed ? { error: result.error } : result.output
  return {
    isError: failed,
    content: [{ type: 'text', text: JSON.stringify(payload) }],
    ...(failed ? {} : { structuredContent: payload }),
  }
})

const transport = new StdioServerTransport()
speakers.start()
await server.connect(transport)

async function close() {
  speakers.stop()
  try { await robotTools.stopActive('M1 backend MCP shutdown') } catch {}
  await server.close()
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, async () => {
    await close()
    process.exit(0)
  })
}
