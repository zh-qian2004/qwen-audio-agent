import assert from 'node:assert/strict'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'

const testDir = dirname(fileURLToPath(import.meta.url))
const serverPath = join(testDir, '..', 'backend-mcp.mjs')

test('backend MCP exposes background robot tools over stdio', async t => {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    env: {
      ...process.env,
      M1_DASHBOARD_URL: 'http://127.0.0.1:1',
      M1_DASHBOARD_TIMEOUT_MS: '500',
    },
    stderr: 'pipe',
  })
  const client = new Client({ name: 'm1-backend-mcp-test', version: '1.0.0' })
  t.after(async () => client.close())
  await client.connect(transport)

  const catalog = await client.listTools()
  assert.ok(catalog.tools.some(tool => tool.name === 'm1_get_state'))
  assert.ok(catalog.tools.some(tool => tool.name === 'm1_wait_for_motion'))
})
