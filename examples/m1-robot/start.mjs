#!/usr/bin/env node
import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { parseEnv } from 'node:util'
import { fileURLToPath } from 'node:url'
import { userConfigDirectory } from '../../shared/runtime-paths.mjs'
import {
  buildConnectorEnv,
  buildGatewayEnv,
  buildSshArgs,
  loadLauncherConfig,
} from './launcher-config.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(scriptDir, '..', '..')
const localConfig = join(scriptDir, 'm1-robot.config.local.json')
const defaultConfig = existsSync(localConfig) ? localConfig : join(scriptDir, 'm1-robot.config.json')

function usage() {
  console.log(`Usage: npm run start:m1-robot -- [--config FILE] [--no-build] [--dry-run]

Starts the optional SSH tunnel, local Gateway, and M1 Connector from one JSON config.`)
}

function parseArgs(argv) {
  const options = { configPath: defaultConfig, noBuild: false, dryRun: false }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--config') {
      const value = argv[++index]
      if (!value) throw new Error('--config requires a file path')
      options.configPath = resolve(value)
    } else if (arg === '--no-build') options.noBuild = true
    else if (arg === '--dry-run') options.dryRun = true
    else if (arg === '--help' || arg === '-h') options.help = true
    else throw new Error(`Unknown option: ${arg}`)
  }
  return options
}

function delay(ms) {
  return new Promise(resolveDelay => setTimeout(resolveDelay, ms))
}

function canonicalRealtimeCredential(env = process.env) {
  const explicit = String(env.QWEN_AUDIO_REALTIME_API_KEY || '').trim()
  if (explicit) return explicit
  const configPath = join(userConfigDirectory(env, homedir()), 'config.env')
  try {
    return String(parseEnv(readFileSync(configPath, 'utf8')).QWEN_AUDIO_REALTIME_API_KEY || '').trim()
  } catch (error) {
    if (error.code === 'ENOENT') return ''
    throw error
  }
}

async function reachable(url, timeoutMs = 2_000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  timer.unref?.()
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' })
    return response.ok
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

async function jsonResponse(url, timeoutMs = 2_000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  timer.unref?.()
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' })
    if (!response.ok) return null
    return await response.json()
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

async function waitFor(name, url, timeoutMs, child) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (child?.exitCode !== null || child?.signalCode) {
      throw new Error(`${name} process exited before becoming ready`)
    }
    if (await reachable(url)) return
    await delay(250)
  }
  throw new Error(`${name} did not become ready within ${timeoutMs} ms: ${url}`)
}

function spawnService(name, command, args, options = {}) {
  console.log(`[m1-start] starting ${name}`)
  const child = spawn(command, args, {
    cwd: repoRoot,
    stdio: 'inherit',
    windowsHide: true,
    ...options,
  })
  child.m1Name = name
  child.m1Exited = false
  child.m1Exit = new Promise(resolveExit => {
    child.once('error', error => {
      child.m1Exited = true
      resolveExit({ name, code: 1, error })
    })
    child.once('exit', (code, signal) => {
      child.m1Exited = true
      resolveExit({ name, code, signal })
    })
  })
  return child
}

async function runBuild(env) {
  console.log('[m1-start] building Gateway web assets')
  const npmExecPath = String(process.env.npm_execpath || '').trim()
  const invocation = npmExecPath
    ? {
        command: process.env.npm_node_execpath || process.execPath,
        args: [npmExecPath, 'run', 'build'],
      }
    : process.platform === 'win32'
      ? {
          command: process.env.ComSpec || 'cmd.exe',
          args: ['/d', '/s', '/c', 'npm run build'],
        }
      : { command: 'npm', args: ['run', 'build'] }
  const child = spawn(invocation.command, invocation.args, {
    cwd: repoRoot, env, stdio: 'inherit', windowsHide: true,
  })
  const result = await new Promise((resolveResult, reject) => {
    child.once('error', reject)
    child.once('exit', (code, signal) => resolveResult({ code, signal }))
  })
  if (result.code !== 0) throw new Error(`Gateway build failed (${result.signal || result.code})`)
}

async function terminate(child) {
  if (!child || child.m1Exited) return
  child.kill('SIGTERM')
  await Promise.race([
    new Promise(resolveExit => child.once('exit', resolveExit)),
    delay(3_000),
  ])
  if (!child.m1Exited) child.kill('SIGKILL')
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  if (options.help) { usage(); return }
  const config = loadLauncherConfig(options.configPath)
  if (options.noBuild) config.gateway.buildBeforeStart = false

  console.log(`[m1-start] config: ${config.configPath}`)
  if (options.dryRun) {
    console.log(JSON.stringify({
      ssh: { enabled: config.ssh.enabled, target: config.ssh.target, forwards: config.ssh.forwards },
      gateway: { startLocal: config.gateway.startLocal, url: config.gateway.url, buildBeforeStart: config.gateway.buildBeforeStart },
      connector: {
        audioServiceOrigin: config.connector.audioServiceOrigin,
        dashboardUrl: config.connector.dashboardUrl,
        halfDuplex: config.connector.halfDuplex,
      },
    }, null, 2))
    return
  }

  const owned = []
  let requestedStop
  const stopRequested = new Promise(resolveStop => { requestedStop = resolveStop })
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.once(signal, () => requestedStop({ name: signal, code: 0 }))
  }

  try {
    const audioStatusUrl = `${config.connector.audioServiceOrigin}/api/audio/status`
    const dashboardStatusUrl = `${config.connector.dashboardUrl}/api/health`
    const robotReady = await Promise.all([reachable(audioStatusUrl), reachable(dashboardStatusUrl)])
    if (robotReady.every(Boolean)) {
      console.log('[m1-start] reusing reachable audio_service and Dashboard endpoints')
    } else if (config.ssh.enabled) {
      const tunnel = spawnService('SSH tunnel', config.ssh.executable, buildSshArgs(config.ssh))
      owned.push(tunnel)
      await Promise.all([
        waitFor('audio_service', audioStatusUrl, config.connector.preflightTimeoutMs, tunnel),
        waitFor('Dashboard', dashboardStatusUrl, config.connector.preflightTimeoutMs, tunnel),
      ])
      console.log('[m1-start] robot services are reachable through SSH')
    } else {
      throw new Error('audio_service or Dashboard is unreachable and ssh.enabled is false')
    }

    const gatewayReadyUrl = `${config.gateway.httpOrigin}/readyz`
    let gateway = null
    if (await reachable(gatewayReadyUrl)) {
      if (!config.gateway.reuseExisting) throw new Error(`Gateway is already running: ${gatewayReadyUrl}`)
      const expectedProfile = JSON.parse(readFileSync(config.gateway.frontendProfile, 'utf8')).name
      const health = await jsonResponse(`${config.gateway.httpOrigin}/api/health`)
      const activeProfile = health?.frontendProfile?.name
      if (!expectedProfile || activeProfile !== expectedProfile) {
        throw new Error(`Existing Gateway profile is ${activeProfile || 'unknown'}, expected ${expectedProfile || 'm1-robot'}`)
      }
      console.log('[m1-start] reusing existing Gateway')
    } else if (config.gateway.startLocal) {
      const gatewayEnv = buildGatewayEnv(config.gateway)
      const realtimeCredential = canonicalRealtimeCredential(gatewayEnv)
      if (realtimeCredential) gatewayEnv.QWEN_AUDIO_REALTIME_API_KEY = realtimeCredential
      if (config.gateway.buildBeforeStart) await runBuild(gatewayEnv)
      gateway = spawnService(
        'Gateway',
        process.execPath,
        ['cli/bin/qwenaudio.mjs', 'gateway', '--url', config.gateway.httpOrigin],
        { env: gatewayEnv },
      )
      owned.push(gateway)
      await waitFor('Gateway', gatewayReadyUrl, config.gateway.readyTimeoutMs, gateway)
      console.log('[m1-start] Gateway is ready')
    } else {
      console.log('[m1-start] Gateway is remote; Connector will handle reconnection')
    }

    const connector = spawnService(
      'M1 Connector',
      process.execPath,
      ['examples/m1-robot/main.mjs'],
      { env: buildConnectorEnv(config) },
    )
    owned.push(connector)
    console.log('[m1-start] M1 Robot Agent is running; press Ctrl+C to stop')

    const exits = owned.map(child => child.m1Exit)
    const terminal = await Promise.race([stopRequested, ...exits])
    if (terminal.name !== 'SIGINT' && terminal.name !== 'SIGTERM') {
      const reason = terminal.error?.message || terminal.signal || terminal.code
      console.error(`[m1-start] ${terminal.name} exited (${reason})`)
      process.exitCode = terminal.code || 1
    }
  } finally {
    console.log('[m1-start] stopping managed services')
    for (const child of [...owned].reverse()) await terminate(child)
  }
}

main().catch(error => {
  console.error(`[m1-start] ${error.message}`)
  process.exitCode = 1
})
