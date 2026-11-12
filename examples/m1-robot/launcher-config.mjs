import { readFileSync } from 'node:fs'
import { dirname, isAbsolute, resolve } from 'node:path'

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]'])

function object(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${name} must be an object`)
  return value
}

function text(value, name, { optional = false } = {}) {
  const result = String(value ?? '').trim()
  if (!result && !optional) throw new Error(`${name} must not be empty`)
  return result
}

function boolean(value, fallback) {
  return typeof value === 'boolean' ? value : fallback
}

function integer(value, fallback, name, { min = 1, max = 2_147_483_647 } = {}) {
  const result = value === undefined ? fallback : Number(value)
  if (!Number.isInteger(result) || result < min || result > max) {
    throw new Error(`${name} must be an integer from ${min} to ${max}`)
  }
  return result
}

function absolutePath(value, configDir) {
  const result = text(value, 'path', { optional: true })
  if (!result) return ''
  return isAbsolute(result) ? result : resolve(configDir, result)
}

function url(value, name, protocols) {
  let result
  try { result = new URL(text(value, name)) } catch { throw new Error(`${name} is not a valid URL`) }
  if (!protocols.includes(result.protocol)) throw new Error(`${name} must use ${protocols.join(' or ')}`)
  return result
}

function environment(value, name) {
  if (value === undefined) return {}
  const source = object(value, name)
  return Object.fromEntries(Object.entries(source).map(([key, item]) => {
    if (!key.trim() || item === null || !['string', 'number', 'boolean'].includes(typeof item)) {
      throw new Error(`${name} values must be strings, numbers, or booleans`)
    }
    return [key, String(item)]
  }))
}

export function httpOriginFromGateway(gatewayUrl) {
  const result = new URL(gatewayUrl)
  result.protocol = result.protocol === 'wss:' ? 'https:' : 'http:'
  result.pathname = '/'
  result.search = ''
  result.hash = ''
  return result.toString().replace(/\/$/, '')
}

export function loadLauncherConfig(configPath) {
  const resolvedPath = resolve(configPath)
  let source
  try { source = JSON.parse(readFileSync(resolvedPath, 'utf8')) } catch (error) {
    throw new Error(`Cannot read launcher config ${resolvedPath}: ${error.message}`)
  }
  const root = object(source, 'config')
  const configDir = dirname(resolvedPath)

  const sshSource = object(root.ssh ?? {}, 'ssh')
  const sshEnabled = boolean(sshSource.enabled, false)
  const forwards = (sshSource.forwards ?? []).map((item, index) => {
    const value = object(item, `ssh.forwards[${index}]`)
    return {
      localPort: integer(value.localPort, undefined, `ssh.forwards[${index}].localPort`, { max: 65_535 }),
      remoteHost: text(value.remoteHost ?? '127.0.0.1', `ssh.forwards[${index}].remoteHost`),
      remotePort: integer(value.remotePort, undefined, `ssh.forwards[${index}].remotePort`, { max: 65_535 }),
    }
  })
  if (sshEnabled && !forwards.length) throw new Error('ssh.forwards must contain at least one forward when SSH is enabled')
  if (new Set(forwards.map(item => item.localPort)).size !== forwards.length) {
    throw new Error('ssh.forwards localPort values must be unique')
  }

  const gatewaySource = object(root.gateway ?? {}, 'gateway')
  const gatewayUrl = url(gatewaySource.url ?? 'ws://127.0.0.1:18888/api/realtime', 'gateway.url', ['ws:', 'wss:'])
  const startLocal = boolean(gatewaySource.startLocal, true)
  if (startLocal && (gatewayUrl.protocol !== 'ws:' || !LOOPBACK_HOSTS.has(gatewayUrl.hostname))) {
    throw new Error('gateway.url must be a loopback ws:// URL when gateway.startLocal is true')
  }

  const connectorSource = object(root.connector ?? {}, 'connector')
  const audioOrigin = url(connectorSource.audioServiceOrigin ?? 'http://127.0.0.1:18091', 'connector.audioServiceOrigin', ['http:', 'https:'])
  const dashboardUrl = url(connectorSource.dashboardUrl ?? 'http://127.0.0.1:18080', 'connector.dashboardUrl', ['http:', 'https:'])

  return {
    configPath: resolvedPath,
    configDir,
    ssh: {
      enabled: sshEnabled,
      executable: text(sshSource.executable ?? 'ssh', 'ssh.executable'),
      target: text(sshSource.target, 'ssh.target', { optional: !sshEnabled }),
      identityFile: absolutePath(sshSource.identityFile, configDir),
      knownHostsFile: absolutePath(sshSource.knownHostsFile, configDir),
      connectTimeoutSeconds: integer(sshSource.connectTimeoutSeconds, 10, 'ssh.connectTimeoutSeconds', { max: 300 }),
      forwards,
    },
    gateway: {
      startLocal,
      buildBeforeStart: boolean(gatewaySource.buildBeforeStart, true),
      reuseExisting: boolean(gatewaySource.reuseExisting, true),
      url: gatewayUrl.toString(),
      httpOrigin: httpOriginFromGateway(gatewayUrl),
      frontendProfile: absolutePath(gatewaySource.frontendProfile ?? './profile/frontend-profile.json', configDir),
      accessToken: text(gatewaySource.accessToken, 'gateway.accessToken', { optional: true }),
      readyTimeoutMs: integer(gatewaySource.readyTimeoutMs, 120_000, 'gateway.readyTimeoutMs', { min: 1_000, max: 600_000 }),
      environment: environment(gatewaySource.environment, 'gateway.environment'),
    },
    connector: {
      audioServiceOrigin: audioOrigin.toString().replace(/\/$/, ''),
      dashboardUrl: dashboardUrl.toString().replace(/\/$/, ''),
      halfDuplex: boolean(connectorSource.halfDuplex, true),
      clientInstanceId: text(connectorSource.clientInstanceId ?? 'm1-robot-primary', 'connector.clientInstanceId'),
      clientLabel: text(connectorSource.clientLabel ?? 'M1 Robot', 'connector.clientLabel'),
      locale: text(connectorSource.locale ?? 'zh-CN', 'connector.locale'),
      timeZone: text(connectorSource.timeZone ?? 'Asia/Shanghai', 'connector.timeZone'),
      preflightTimeoutMs: integer(connectorSource.preflightTimeoutMs, 20_000, 'connector.preflightTimeoutMs', { min: 1_000, max: 300_000 }),
      environment: environment(connectorSource.environment, 'connector.environment'),
    },
  }
}

export function buildSshArgs(config) {
  const args = [
    '-o', 'BatchMode=yes',
    '-o', `ConnectTimeout=${config.connectTimeoutSeconds}`,
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=15',
  ]
  if (config.identityFile) args.push('-i', config.identityFile)
  if (config.knownHostsFile) args.push('-o', `UserKnownHostsFile=${config.knownHostsFile}`)
  args.push('-N')
  for (const forward of config.forwards) {
    args.push('-L', `${forward.localPort}:${forward.remoteHost}:${forward.remotePort}`)
  }
  args.push(config.target)
  return args
}

export function buildGatewayEnv(config, base = process.env) {
  const gatewayUrl = new URL(config.url)
  return {
    ...base,
    ...config.environment,
    QWEN_AUDIO_FRONTEND_PROFILE: config.frontendProfile,
    HOST: gatewayUrl.hostname,
    PORT: gatewayUrl.port || (gatewayUrl.protocol === 'wss:' ? '443' : '80'),
  }
}

export function addBackendMcpServer(env, descriptor) {
  const key = 'QWEN_AUDIO_BACKEND_MCP_SERVERS'
  let current = []
  const raw = String(env[key] || '').trim()
  if (raw) {
    try { current = JSON.parse(raw) } catch (error) {
      throw new Error(`${key} is invalid JSON: ${error.message}`)
    }
    if (!Array.isArray(current)) throw new Error(`${key} must be a JSON array`)
  }
  const name = text(descriptor?.name, 'backend MCP server name')
  const servers = current.filter(item => item?.name !== name)
  servers.push(descriptor)
  return { ...env, [key]: JSON.stringify(servers) }
}

export function buildConnectorEnv(config, base = process.env) {
  return {
    ...base,
    M1_GATEWAY_URL: config.gateway.url,
    M1_GATEWAY_ACCESS_TOKEN: config.gateway.accessToken,
    M1_AUDIO_SERVICE_ORIGIN: config.connector.audioServiceOrigin,
    M1_DASHBOARD_URL: config.connector.dashboardUrl,
    M1_CLIENT_INSTANCE_ID: config.connector.clientInstanceId,
    M1_CLIENT_LABEL: config.connector.clientLabel,
    M1_LOCALE: config.connector.locale,
    M1_TIME_ZONE: config.connector.timeZone,
    M1_HALF_DUPLEX: config.connector.halfDuplex ? '1' : '0',
    ...config.connector.environment,
  }
}
