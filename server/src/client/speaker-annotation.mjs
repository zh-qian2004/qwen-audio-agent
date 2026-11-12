import { z } from 'zod'
import { createAgentDelivery } from '../delivery/agent-delivery.mjs'

const nullableText = max => z.string().trim().max(max).nullable().optional()
const nullableNumber = z.number().finite().nullable().optional()

export const SpeakerAnnotationSchema = z.object({
  turn_id: z.string().trim().min(1).max(160),
  message_id: z.string().trim().min(1).max(240),
  transcript: z.string().trim().min(1).max(16_000),
  status: z.enum(['verified', 'unknown', 'error']),
  speaker_id: nullableText(80),
  display_name: nullableText(120),
  role: nullableText(40),
  confidence: nullableNumber,
  margin: nullableNumber,
  speaker_turn_id: nullableText(120),
  audio_start_sequence: nullableNumber,
  audio_end_sequence: nullableNumber,
  audio_ms: nullableNumber,
  identity_ready_ms: nullableNumber,
  occurred_at: nullableNumber,
  revision: z.number().int().min(1).max(1_000_000).default(1),
}).strict().superRefine((value, context) => {
  if (value.status === 'verified' && !value.speaker_id) {
    context.addIssue({
      code: 'custom',
      path: ['speaker_id'],
      message: 'verified annotations require speaker_id',
    })
  }
})

export function renderSpeakerAnnotation(data) {
  return [
    '<speaker_annotation>',
    '这是上一条用户语音的隐藏元数据，不是用户的新请求；不要回应、复述或主动播报它。',
    `turn_id=${JSON.stringify(data.turn_id)}`,
    `status=${JSON.stringify(data.status)}`,
    `speaker_id=${JSON.stringify(data.speaker_id ?? null)}`,
    `display_name=${JSON.stringify(data.display_name ?? null)}`,
    `confidence=${JSON.stringify(data.confidence ?? null)}`,
    `utterance=${JSON.stringify(data.transcript)}`,
    '只有 status="verified" 时才能把 speaker_id/display_name 当作声纹身份；unknown/error 不得猜测身份。',
    '</speaker_annotation>',
  ].join('\n')
}

export function speakerAnnotationDefinition() {
  return {
    name: 'speaker.annotation',
    schema: SpeakerAnnotationSchema,
    maxBytes: 24_576,
    rateLimit: { max: 30, windowMs: 10_000 },
    retention: 'transient',
    route: 'context',
    handle: (event, effects) => effects.annotateSpeaker?.(event.data),
    project: event => createAgentDelivery({
      id: `speaker_annotation_${event.id}`,
      causeEventId: event.id,
      origin: 'speaker-annotation',
      mode: 'context',
      text: renderSpeakerAnnotation(event.data),
      correlation: {
        clientEventId: event.id,
        turnId: event.data.turn_id,
      },
      presentation: { contextTiming: 'immediate' },
    }),
  }
}
