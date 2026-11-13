const origin = 'http://127.0.0.1:18889'
const ready = await fetch(`${origin}/readyz`, { signal: AbortSignal.timeout(3000) })
if (!ready.ok) throw new Error(`Gateway readiness HTTP ${ready.status}`)
const response = await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(5000) })
if (!response.ok) throw new Error(`Gateway health HTTP ${response.status}`)
const health = await response.json()
const summary = {
  gateway: health.status,
  frontendProfile: health.frontendProfile?.name,
  backend: health.backend?.kind,
  backendModel: health.backend?.model,
  backendReady: health.backend?.ok === true && health.backend?.kind === 'qwen' && health.backend?.status === 'ready',
}
console.log(JSON.stringify(summary))
if (summary.gateway !== 'ready' || !summary.backendReady) process.exitCode = 1
