const base = 'http://127.0.0.1:8093'
const response = await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(5000) })
if (!response.ok) throw new Error(`audio input unhealthy: HTTP ${response.status}`)
const health = await response.json()
const consumers = health.input?.consumer_details
if (!Array.isArray(consumers)) throw new Error('audio input consumer details unavailable')
const agentConsumers = consumers.filter(consumer => String(consumer.name || '').startsWith('ws-input-'))
if (agentConsumers.length !== 0) {
  throw new Error(`audio input already has ${agentConsumers.length} Agent consumer(s); keep cloud Agent active`)
}
console.log('audio input available for edge Connector')
