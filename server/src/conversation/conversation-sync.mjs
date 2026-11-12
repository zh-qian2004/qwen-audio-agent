import { projectFrontendConversation } from './frontend-conversation-projection.mjs'
import { captureCoordinatorConversation } from '../core/work-conversation-context.mjs'

function sessionKey(ownerId, sessionId) {
  return `${ownerId}\u0000${sessionId}`
}

function clean(value) {
  return String(value || '').replace(/\s+/g, ' ').trim()
}

function finiteOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function speakerSnapshot(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const status = ['verified', 'unknown', 'error'].includes(value.status)
    ? value.status
    : 'unknown'
  const speakerId = clean(value.speaker_id).slice(0, 80) || null
  return Object.freeze({
    status: status === 'verified' && !speakerId ? 'unknown' : status,
    speaker_id: speakerId,
    display_name: clean(value.display_name).slice(0, 120) || null,
    role: clean(value.role).slice(0, 40) || null,
    confidence: finiteOrNull(value.confidence),
    margin: finiteOrNull(value.margin),
    speaker_turn_id: clean(value.speaker_turn_id).slice(0, 120) || null,
    audio_start_sequence: finiteOrNull(value.audio_start_sequence),
    audio_end_sequence: finiteOrNull(value.audio_end_sequence),
    audio_ms: finiteOrNull(value.audio_ms),
    identity_ready_ms: finiteOrNull(value.identity_ready_ms),
    occurred_at: finiteOrNull(value.occurred_at),
    revision: Math.max(1, Number(value.revision) || 1),
  })
}

function recordedSnapshot(message) {
  return {
    ...message,
    ...(message.speaker ? { speaker: { ...message.speaker } } : {}),
    taskIds: [...(message.taskIds || [])],
    inputs: (message.inputs || []).map(input => ({ ...input })),
    ...(message.citations
      ? { citations: message.citations.map(citation => ({ ...citation })) }
      : {}),
  }
}

function speechKey(value) {
  return clean(value)
    .toLocaleLowerCase()
    .replace(/[\p{P}\p{S}\s]+/gu, '')
}

function speechNgrams(value, size = 2) {
  const grams = new Set()
  for (let index = 0; index <= value.length - size; index += 1) {
    grams.add(value.slice(index, index + size))
  }
  return grams
}

function equivalentSpeech(left, right) {
  if (left === right) return true
  if (left.length < 8 || right.length < 8) return false
  const leftGrams = speechNgrams(left)
  const rightGrams = speechNgrams(right)
  const shorterSize = Math.min(leftGrams.size, rightGrams.size)
  if (!shorterSize) return false
  let shared = 0
  for (const gram of leftGrams) {
    if (rightGrams.has(gram)) shared += 1
  }
  // Delegated acknowledgements often add details around the same short action
  // preview. One third of the shorter message is enough to recognize that
  // paraphrase without suppressing a genuinely different update.
  return shared / shorterSize >= 1 / 3
}

export class ConversationSync {
  constructor({
    maxMessages = 100,
    maxSessions = 500,
    sessionTtlMs = 6 * 60 * 60 * 1000,
    onRecord = null,
  } = {}) {
    this.maxMessages = maxMessages
    this.maxSessions = maxSessions
    this.sessionTtlMs = sessionTtlMs
    this.sessions = new Map()
    this.sequence = 0
    this.onRecord = onRecord
  }

  configureRetention(options = {}) {
    Object.assign(this, options)
  }

  setRecordObserver(observer) {
    this.onRecord = typeof observer === 'function' ? observer : null
    return this
  }

  state(ownerId, sessionId) {
    this.prune()
    const key = sessionKey(ownerId, sessionId)
    let state = this.sessions.get(key)
    if (!state) {
      this.enforceSessionLimit()
      state = {
        ownerId: String(ownerId),
        messages: [],
        byId: new Map(),
        lastAccessedAt: Date.now(),
        recorded: new Map(),
        speakerByTurn: new Map(),
        recordSequence: 0,
        recordEpoch: 0,
        historyPosition: 0,
        consumers: new WeakMap(),
      }
      this.sessions.set(key, state)
    }
    state.lastAccessedAt = Date.now()
    return state
  }

  peek(ownerId, sessionId) {
    const state = this.sessions.get(sessionKey(ownerId, sessionId))
    if (state) state.lastAccessedAt = Date.now()
    return state || null
  }

  prune(now = Date.now()) {
    this.sessions.forEach((state, key) => {
      if (now - state.lastAccessedAt >= this.sessionTtlMs) this.sessions.delete(key)
    })
  }

  enforceSessionLimit() {
    while (this.sessions.size >= this.maxSessions) {
      const oldest = [...this.sessions.entries()]
        .sort((a, b) => a[1].lastAccessedAt - b[1].lastAccessedAt)[0]
      if (!oldest) break
      this.sessions.delete(oldest[0])
    }
  }

  upsert({
    ownerId,
    sessionId,
    id,
    role,
    content,
    source,
    turnId = null,
    taskId = null,
    taskIds = [],
    inputs = [],
    citations = [],
    createdAt = null,
    historyPosition = null,
    final = true,
    speaker = undefined,
  }, { notify = true } = {}) {
    const normalized = clean(content)
    // Runtime sources record final text only. Do not let an accidental partial
    // create (or overwrite) a durable message or consume its history position.
    if (final === false || !id || !normalized) return null
    const state = this.state(ownerId, sessionId)
    const resolvedSpeaker = role === 'user'
      ? speakerSnapshot(speaker) || state.speakerByTurn.get(String(turnId || '')) || null
      : null
    const existing = state.byId.get(id)
    if (existing) {
      const changed = existing.role !== role
        || existing.content !== normalized
        || JSON.stringify(existing.speaker || null) !== JSON.stringify(resolvedSpeaker)
      Object.assign(existing, {
        role,
        content: normalized,
        source,
        turnId,
        taskId,
        taskIds: [...new Set((taskIds || []).filter(Boolean))],
        inputs: (inputs || []).map(input => ({ ...input })),
        ...(resolvedSpeaker ? { speaker: resolvedSpeaker } : {}),
      })
      if (!resolvedSpeaker) delete existing.speaker
      if (citations?.length) {
        existing.citations = citations.map(citation => ({ ...citation }))
      }
      const snapshot = { ...existing }
      if (notify) {
        if (changed) this.#recordPending(state, snapshot)
        try { this.onRecord?.(snapshot, { ownerId, sessionId }) } catch { /* observers must not affect sync */ }
      }
      return snapshot
    }
    const message = {
      seq: ++this.sequence,
      id,
      role,
      content: normalized,
      source,
      turnId,
      taskId,
      taskIds: [...new Set((taskIds || []).filter(Boolean))],
      inputs: (inputs || []).map(input => ({ ...input })),
      ...(resolvedSpeaker ? { speaker: resolvedSpeaker } : {}),
      ...(citations?.length
        ? { citations: citations.map(citation => ({ ...citation })) }
        : {}),
      createdAt: Number(createdAt) || Date.now(),
      // Unlike seq, this watermark survives Session Journal restoration. Its
      // clock origin keeps legacy history order; increments handle equal times
      // and clock rollback. Updating the same final message keeps its position.
      historyPosition: Number.isSafeInteger(historyPosition) && historyPosition > 0
        ? historyPosition
        : Math.max(state.historyPosition + 1, Number(createdAt) || Date.now()),
    }
    state.historyPosition = Math.max(state.historyPosition, message.historyPosition)
    state.messages.push(message)
    state.byId.set(id, message)
    while (state.messages.length > this.maxMessages) {
      const removed = state.messages.shift()
      state.byId.delete(removed.id)
      state.recorded.delete(removed.id)
    }
    const snapshot = { ...message }
    if (notify) {
      this.#recordPending(state, snapshot)
      try { this.onRecord?.(snapshot, { ownerId, sessionId }) } catch { /* observers must not affect sync */ }
    }
    return snapshot
  }

  record(message) {
    return this.upsert(message)
  }

  annotateSpeaker({ ownerId, sessionId, turnId, messageId, speaker } = {}) {
    const safeTurnId = String(turnId || '').trim()
    const annotation = speakerSnapshot(speaker)
    if (!ownerId || !sessionId || !safeTurnId || !annotation) return null
    const state = this.state(ownerId, sessionId)
    const previous = state.speakerByTurn.get(safeTurnId)
    if (previous && previous.revision > annotation.revision) return { ...previous }
    state.speakerByTurn.set(safeTurnId, annotation)
    const message = state.messages.find(item => (
      item.role === 'user'
      && (item.turnId === safeTurnId || (messageId && item.id === messageId))
    ))
    if (message) {
      message.speaker = annotation
      this.#recordPending(state, message)
    }
    try {
      this.onRecord?.(null, {
        ownerId,
        sessionId,
        eventType: 'speaker/annotation',
        turnId: safeTurnId,
        messageId: String(messageId || message?.id || `voice:user:${safeTurnId}`),
        speaker: { ...annotation },
      })
    } catch { /* observers must not affect sync */ }
    return { ...annotation }
  }

  #recordPending(state, message) {
    if (!state.byId.has(message.id)) return
    state.recorded.delete(message.id)
    state.recorded.set(message.id, {
      version: ++state.recordSequence,
      message: recordedSnapshot(message),
    })
    while (state.recorded.size > this.maxMessages) {
      state.recorded.delete(state.recorded.keys().next().value)
    }
  }

  // Durable history is presentation context, not new learning evidence. Each
  // consumer independently claims only live records, before awaiting its work.
  pendingRecords({ ownerId, sessionId }, consumer) {
    if ((!consumer || typeof consumer !== 'object') && typeof consumer !== 'function') {
      throw new TypeError('a record consumer object is required')
    }
    this.prune()
    const key = sessionKey(ownerId, sessionId)
    const state = this.peek(ownerId, sessionId)
    if (!state) return { messages: [], consume() {}, isCurrent: () => false }
    const cursor = state.consumers.get(consumer) || 0
    const version = state.recordSequence
    const epoch = state.recordEpoch
    const isCurrent = () => {
      this.prune()
      return this.sessions.get(key) === state && state.recordEpoch === epoch
    }
    return {
      messages: [...state.recorded.values()]
        .filter(record => record.version > cursor)
        .map(record => recordedSnapshot(record.message)),
      consume: () => {
        if (!isCurrent()) return
        state.consumers.set(consumer, Math.max(state.consumers.get(consumer) || 0, version))
      },
      isCurrent,
    }
  }

  discardRecorded(ownerId) {
    this.prune()
    for (const state of this.sessions.values()) {
      if (state.ownerId !== String(ownerId)) continue
      state.recorded.clear()
      state.recordEpoch += 1
    }
  }

  restore({ ownerId, sessionId, messages = [], historyPosition = 0 }) {
    for (const message of messages) {
      this.upsert({ ownerId, sessionId, ...message }, { notify: false })
    }
    if (Number.isSafeInteger(historyPosition) && historyPosition > 0) {
      const state = this.state(ownerId, sessionId)
      state.historyPosition = Math.max(state.historyPosition, historyPosition)
    }
    return this.frontendContext({ ownerId, sessionId })
  }

  list({ ownerId, sessionId }) {
    this.prune()
    return (this.peek(ownerId, sessionId)?.messages || [])
      .map(message => ({
        ...message,
        ...(message.speaker ? { speaker: { ...message.speaker } } : {}),
        inputs: (message.inputs || []).map(input => ({ ...input })),
        ...(message.citations
          ? { citations: message.citations.map(citation => ({ ...citation })) }
          : {}),
      }))
  }

  hasEquivalentAssistantSpeech({
    ownerId,
    sessionId,
    turnId,
    content,
  }) {
    const target = speechKey(content)
    if (!target) return false
    return this.list({ ownerId, sessionId }).some(message => (
      message.role === 'assistant'
      && message.turnId === turnId
      && equivalentSpeech(speechKey(message.content), target)
    ))
  }

  frontendContext({ ownerId, sessionId }, messages = this.list({ ownerId, sessionId })) {
    return projectFrontendConversation(messages)
  }

  coordinatorSnapshot({ ownerId, sessionId, excludeTurnId, maxChars } = {}) {
    this.prune()
    return captureCoordinatorConversation(this.peek(ownerId, sessionId)?.messages || [], {
      sessionId, excludeTurnId, maxChars,
    })
  }

}

export const conversationSync = new ConversationSync()
