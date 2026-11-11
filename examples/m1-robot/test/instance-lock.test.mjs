import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { acquireM1ConnectorInstance } from '../instance-lock.mjs'

test('allows only one M1 Connector per client instance and recovers stale locks', t => {
  const directory = mkdtempSync(join(tmpdir(), 'm1-connector-lock-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const alive = pid => {
    if (pid === 101) return
    throw Object.assign(new Error('missing'), { code: 'ESRCH' })
  }
  const first = acquireM1ConnectorInstance(directory, 'robot-a', {
    pid: 101, token: 'first', killImpl: alive,
  })
  assert.equal(existsSync(first.path), true)
  assert.throws(
    () => acquireM1ConnectorInstance(directory, 'robot-a', {
      pid: 202, token: 'second', killImpl: alive,
    }),
    /M1 Connector robot-a 已在运行/,
  )
  const other = acquireM1ConnectorInstance(directory, 'robot-b', {
    pid: 202, token: 'other', killImpl: alive,
  })
  other.release()
  first.release()

  const recovered = acquireM1ConnectorInstance(directory, 'robot-a', {
    pid: 303,
    token: 'recovered',
    killImpl: () => { throw Object.assign(new Error('missing'), { code: 'ESRCH' }) },
  })
  recovered.release()
})
