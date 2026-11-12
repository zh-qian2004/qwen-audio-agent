import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

// Background-only projection. No text from this store is pushed to the model.
export class SpeakerTurnStore {
  constructor({ path = '', sessionId = 'main', now = Date.now, settleMs = 5000 } = {}) {
    Object.assign(this, { path, sessionId, now, settleMs })
    this.turns = []
    this.segments = []
    this.ready = false
    this.load()
  }

  load() {
    if (!this.path) return
    try {
      const data = JSON.parse(readFileSync(this.path, 'utf8'))
      if (data.sessionId !== this.sessionId) return
      this.turns = data.turns || []
      this.segments = data.segments || []
      this.ready = data.ready === true && this.now() - data.updatedAt < 15000
    } catch { /* A new session starts with no evidence. */ }
  }

  save() {
    if (!this.path) return
    mkdirSync(dirname(this.path), { recursive: true })
    const temporary = `${this.path}.${process.pid}.tmp`
    writeFileSync(temporary, JSON.stringify({
      sessionId: this.sessionId, updatedAt: this.now(), ready: this.ready,
      turns: this.turns.slice(-200), segments: this.segments.slice(-400),
    }), { mode: 0o600 })
    renameSync(temporary, this.path)
  }

  observe(event = {}) {
    if (!['turn.started', 'transcript.final', 'transcript.discard'].includes(event.type)) return
    const id = String(event.turnId || event.turn_id || '')
    if (!id) return
    let turn = this.turns.find(item => item.id === id)
    if (event.type === 'turn.started' && !turn) {
      turn = { id, start: this.now(), end: null, text: '' }
      this.turns.push(turn)
      this.turns = this.turns.slice(-200)
    }
    if (!turn) return
    if (event.type === 'transcript.final' && event.role === 'user') {
      turn.text = String(event.content || '').slice(0, 16000)
      turn.end = this.now()
    } else if (event.type === 'transcript.discard') {
      this.turns = this.turns.filter(item => item !== turn)
    }
    this.save()
  }

  speakerEvent(event) {
    if (event.type === 'speaker.status') this.ready = event.ready === true
    const id = event.speaker_turn_id
    if (id && ['speaker.turn.started', 'speaker.identity'].includes(event.type)) {
      let segment = this.segments.find(item => item.id === id)
      if (!segment) {
        segment = { id, start: null, end: null, identity: null }
        this.segments.push(segment)
      }
      const at = Number(event.occurred_at) * 1000 || this.now()
      if (event.type === 'speaker.turn.started') segment.start = at
      else {
        segment.identity = event
        // Sequence span describes the captured segment, excluding inference delay.
        const frames = Number(event.audio_end_sequence) - Number(event.audio_start_sequence)
        segment.end = segment.start !== null && frames >= 0 ? segment.start + frames * 20 : at
      }
      this.segments = this.segments.slice(-400)
    }
    this.save()
  }

  query({ target = 'current', turn_id, query } = {}, { refresh = false } = {}) {
    if (refresh) this.load()
    const turns = this.turns
    let selected
    if (turn_id) selected = turns.find(turn => turn.id === turn_id)
    else if (target === 'previous') selected = turns.at(-2)
    else if (target === 'search') {
      const text = String(query || '').trim()
      // Exclude the current question itself from historical text searches.
      const matches = text ? turns.slice(0, -1).filter(turn => turn.text.includes(text)) : []
      if (matches.length !== 1) return {
        status: 'unavailable', reason: matches.length ? 'ambiguous_turn' : 'turn_not_found',
        candidates: matches.slice(-10).map(turn => ({ turn_id: turn.id, transcript: turn.text })),
      }
      selected = matches[0]
    } else selected = turns.at(-1)
    if (!selected) return { status: 'unavailable', reason: 'turn_not_found', speaker_id: null }

    const base = { turn_id: selected.id, transcript: selected.text, speaker_id: null, display_name: null }
    const matched = this.segments.filter(segment => {
      if (segment.start === null) return false
      // Match using capture onset, never identity completion time. Each segment
      // belongs to the nearest plausible Gateway onset; long turns have no 5s cap.
      const possible = turns.filter(turn => segment.start >= turn.start - 3000
        && segment.start <= (turn.end ?? this.now()) + 250)
        .sort((a, b) => Math.abs(a.start - segment.start) - Math.abs(b.start - segment.start))
      return possible[0]?.id === selected.id
    })
    const results = matched.map(segment => segment.identity).filter(Boolean)
    const verified = results.filter(item => item.status === 'verified' && item.speaker_id)
    const ids = new Set(verified.map(item => item.speaker_id))
    if (ids.size > 1) return { ...base, status: 'unknown', reason: 'conflicting_speakers' }
    // An open or unsettled turn may still receive new/conflicting segment evidence.
    const waiting = selected.end === null || matched.some(segment => !segment.identity)
    if (waiting && this.now() - (selected.end ?? selected.start) < this.settleMs) {
      return { ...base, status: 'pending', reason: 'processing', retry_after_ms: 500 }
    }
    if (verified.length) {
      const best = verified.reduce((a, b) => a.confidence >= b.confidence ? a : b)
      return { ...base, status: 'verified', speaker_id: best.speaker_id,
        display_name: best.display_name, role: best.role, confidence: best.confidence,
        evidence: verified.map(item => item.speaker_turn_id) }
    }
    if (results.length) {
      const failure = results.find(item => item.status === 'failed')
      if (failure) return { ...base, status: 'unavailable', reason: 'recognition_failed' }
      return { ...base, status: 'unknown', reason: results.at(-1).reason || results.at(-1).status }
    }
    if (this.ready && this.now() - (selected.end ?? selected.start) < this.settleMs) {
      return { ...base, status: 'pending', reason: 'processing', retry_after_ms: 500 }
    }
    return { ...base, status: 'unavailable', reason: this.ready ? 'no_correlated_audio' : 'speaker_service_unavailable' }
  }
}
