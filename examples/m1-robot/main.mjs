#!/usr/bin/env node
import { WebSocket } from 'ws'
import { GatewayClient } from '../../shared/gateway/client-sdk.mjs'
import { GatewayClientCapability } from '../../shared/protocol/gateway-client-protocol.mjs'
import { createLogger } from '../../shared/logger.mjs'
import { resolveRuntimePaths } from '../../shared/runtime-paths.mjs'
import { loadM1Config } from './config.mjs'
import { DashboardClient } from './dashboard-client.mjs'
import { M1RobotTools } from './robot-tools.mjs'
import {
  AudioInputForwarder, AudioOutputBridge, M1AudioBridge, RemoteClockEstimator,
} from './audio-service-bridge.mjs'
import { AgentStatusReporter } from './agent-status-reporter.mjs'
import { VendorEventForwarder } from './vendor-event-bridge.mjs'
import { acquireM1ConnectorInstance } from './instance-lock.mjs'

const config = loadM1Config()
const instanceLock = acquireM1ConnectorInstance(
  resolveRuntimePaths().stateDirectory,
  config.clientInstanceId,
)
process.once('exit', () => instanceLock.release())
const latencyLogger = createLogger({
  component: 'm1-connector',
  fileName: 'm1-connector.log',
  consoleEnabled: false,
})
const logTelemetry = ({ event, ...fields }) => latencyLogger.info(event || 'audio.latency', fields)
const robotClock = new RemoteClockEstimator()
const dashboard = new DashboardClient({
  baseUrl: config.dashboardUrl,
  timeoutMs: config.dashboardTimeoutMs,
})
const robotTools = new M1RobotTools({ dashboard })

try {
  await robotTools.initialize()
  console.info(`[m1] loaded ${robotTools.commands.length} enabled Dashboard commands`)
} catch (error) {
  console.warn(`[m1] Dashboard capability discovery failed; control actions start unavailable: ${error.message}`)
}

let gateway
let shuttingDown = false
const componentStatus = {
  gateway: { state: 'starting' },
  audio_input: { state: 'starting' },
  audio_output: { state: 'idle' },
  vendor_events: { state: 'starting' },
}
const logStatus = status => {
  const error = status.error?.message || status.error?.code || status.error
  const component = status.component || 'gateway'
  componentStatus[component] = {
    state: status.state || 'unknown',
    updated_at: Date.now() / 1_000,
    ...(status.responseId ? { response_id: status.responseId } : {}),
    ...(error ? { error: String(error) } : {}),
  }
  console.info(`[m1] ${status.component || 'gateway'} ${status.state}${error ? `: ${error}` : ''}`)
}

const input = new AudioInputForwarder({
  url: config.audioInputUrl,
  WebSocketImpl: WebSocket,
  sendGateway: event => gateway?.send(event) === true,
  gatewayState: () => ({ ready: gateway?.ready === true, bufferedAmount: gateway?.bufferedAmount || 0 }),
  onStatus: logStatus,
  onTelemetry: logTelemetry,
  clockEstimator: robotClock,
  reconnectMinMs: config.reconnectMinMs,
  reconnectMaxMs: config.reconnectMaxMs,
  maxGatewayBufferedBytes: config.maxGatewayBufferedBytes,
})

let audioBridge
const output = new AudioOutputBridge({
  url: config.audioOutputUrl,
  cancelUrl: config.audioOutputCancelUrl,
  WebSocketImpl: WebSocket,
  fetchImpl: globalThis.fetch,
  sendGateway: event => gateway?.send(event) === true,
  onPlayback: active => audioBridge?.setPlaybackActive(active),
  onStatus: logStatus,
  onTelemetry: logTelemetry,
  clockEstimator: robotClock,
  maxBufferedBytes: config.maxOutputBufferedBytes,
  cancelTimeoutMs: config.outputCancelTimeoutMs,
})
audioBridge = new M1AudioBridge({ input, output, halfDuplex: config.halfDuplex })

gateway = new GatewayClient({
  url: config.gatewayUrl,
  createSocket: (url, options) => new WebSocket(url, options),
  clientType: 'm1-robot',
  clientVersion: '1.0.0',
  clientInstanceId: config.clientInstanceId,
  clientLabel: config.clientLabel,
  accessToken: config.gatewayAccessToken,
  capabilities: [
    GatewayClientCapability.INPUT_AUDIO,
    GatewayClientCapability.PLAYBACK_RECEIPTS,
    GatewayClientCapability.SESSION_HEARTBEAT,
    GatewayClientCapability.CLIENT_EVENTS,
  ],
  tools: robotTools.definitions(),
  locale: config.locale,
  timeZone: config.timeZone,
  configure: {
    voiceEnabled: true,
    inputEnabled: true,
    outputEnabled: true,
    textOnly: false,
  },
  reconnectMinMs: config.reconnectMinMs,
  reconnectMaxMs: config.reconnectMaxMs,
  onEvent: event => audioBridge.handleGateway(event),
  onAction: event => robotTools.handle(event),
  onStatus: status => {
    logStatus(status)
    if (['disconnected', 'unavailable', 'occupied', 'replaced', 'revoked'].includes(status.state)) {
      output.cancel(`gateway_${status.state}`)
      robotTools.stopActive(`Gateway ${status.state}`).catch(error => {
        console.warn(`[m1] fail-safe stop failed: ${error.message}`)
      })
    }
  },
})

const vendorEvents = new VendorEventForwarder({
  url: config.vendorEventUrl,
  WebSocketImpl: WebSocket,
  gateway,
  instanceId: config.clientInstanceId,
  gatewayState: () => ({ ready: gateway?.ready === true }),
  onStatus: logStatus,
  reconnectMinMs: config.reconnectMinMs,
  reconnectMaxMs: config.reconnectMaxMs,
  heartbeatMs: config.vendorEventHeartbeatMs,
})

const statusReporter = new AgentStatusReporter({
  dashboard,
  healthUrl: config.gatewayHealthUrl,
  accessToken: config.gatewayAccessToken,
  instanceId: config.clientInstanceId,
  label: config.clientLabel,
  deploymentMode: config.deploymentMode,
  intervalMs: config.agentStatusIntervalMs,
  connectorStatus: () => structuredClone(componentStatus),
  onStatus: logStatus,
})

audioBridge.start()
gateway.start()
vendorEvents.start()
statusReporter.start()

async function close(signal) {
  if (shuttingDown) return
  shuttingDown = true
  console.info(`[m1] shutting down on ${signal}`)
  statusReporter.stop()
  vendorEvents.stop()
  audioBridge.stop()
  gateway.stop()
  try { await robotTools.stopActive(`M1 connector shutdown: ${signal}`) }
  catch (error) { console.warn(`[m1] shutdown stop failed: ${error.message}`) }
  try { await latencyLogger.flush() }
  finally { instanceLock.release() }
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.once(signal, async () => {
    await close(signal)
    process.exit(0)
  })
}
