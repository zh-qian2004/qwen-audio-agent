import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const root = process.argv[2] || process.cwd()
const manifest = JSON.parse(await readFile(join(root, 'release-manifest.json'), 'utf8'))
if (manifest.schema !== 'm1-edge-release/v1' || !Array.isArray(manifest.files)) {
  throw new Error('Invalid release manifest')
}
const lines = []
for (const file of manifest.files) {
  if (!/^[A-Za-z0-9._/-]+$/.test(file.path) || file.path.includes('..')) {
    throw new Error(`Invalid release path: ${file.path}`)
  }
  const digest = createHash('sha256').update(await readFile(join(root, file.path))).digest('hex')
  if (digest !== file.sha256) throw new Error(`Release file mismatch: ${file.path}`)
  lines.push(`${digest} ${file.path}\n`)
}
const tree = createHash('sha256').update(lines.join('')).digest('hex')
if (tree !== manifest.sourceTreeSha256) throw new Error('Release tree mismatch')
console.log(JSON.stringify({ release: manifest.release, commit: manifest.sourceCommit,
  dirty: manifest.sourceDirty, files: manifest.files.length, tree }))
