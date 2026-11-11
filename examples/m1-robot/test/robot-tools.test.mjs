import test from 'node:test'
import assert from 'node:assert/strict'
import { M1RobotTools } from '../robot-tools.mjs'

function fakeDashboard(state = {
  battery: { power: 80, is_charging: false, on_charger_raw: 0 },
}, motionStatus = { phase: 'idle' }) {
  const calls = []
  return {
    calls,
    motionCapabilities: async () => ({ commands: [
      { id: 'wave_hand', dashboard_enabled: true, interaction: 'trigger', requires_confirmation: false },
      { id: 'move_forward', dashboard_enabled: true, interaction: 'hold' },
      { id: 'move_backward', dashboard_enabled: true, interaction: 'hold' },
      { id: 'strafe_left', dashboard_enabled: true, interaction: 'hold' },
      { id: 'strafe_right', dashboard_enabled: true, interaction: 'hold' },
      { id: 'rotate_left', dashboard_enabled: true, interaction: 'hold' },
      { id: 'rotate_right', dashboard_enabled: true, interaction: 'hold' },
      { id: 'visual_get_on_car', dashboard_enabled: true, interaction: 'workflow', confirmation: 'confirm' },
      { id: 'get_off_car', dashboard_enabled: true, interaction: 'workflow', confirmation: 'confirm' },
      { id: 'return_to_charge', dashboard_enabled: true, interaction: 'workflow', confirmation: 'confirm' },
      { id: 'off_charger', dashboard_enabled: true, interaction: 'workflow', confirmation: 'confirm' },
    ] }),
    currentState: async () => state,
    motionStatus: async () => motionStatus,
    vehicleStatus: async () => ({
      status: 'online',
      mount: { state: 'on_vehicle', known: true },
      wheel_motion: { state: 'stopped', moving: false },
    }),
    visionStatus: async () => ({
      status: 'ok', online: true, version: '0.1.0',
      processors: [{
        id: 'object_detection', state: 'running', source_online: true,
        model: 'detector.hbm', input_rate_hz: 10.1, output_rate_hz: 10,
        confidence_threshold: 0.5, last_error: null,
      }],
      result_bus: { latest_age_seconds: 0.08 },
    }),
    visionDetections: async () => ({
      available: true, stale: false, age_seconds: 0.08,
      result: {
        source_frame_sequence: 12, source_width: 960, source_height: 544, received_at: 123.4,
        detections: [
          { id: '12:body:0', label: 'person', confidence: 0.99, bbox_normalized: { x1: 0, y1: 0.2, x2: 0.4, y2: 1 } },
          { id: '12:hand:0', label: 'hand', confidence: 0.9, bbox_normalized: { x1: 0.4, y1: 0.3, x2: 0.6, y2: 0.7 } },
        ],
      },
    }),
    command: async (id, options) => { calls.push(['command', id, options]); return {
      accepted: true, command_id: id, request_id: `op-${id}`,
      operation_id: `op-${id}`, execution_state: 'running', completion_confirmed: false,
    } },
    stop: async options => { calls.push(['stop', options]); return {
      accepted: true, request_id: 'op-stop', operation_id: 'op-stop', execution_state: 'stop_requested',
    } },
    emergencyStop: async options => { calls.push(['emergency', options]); return {
      accepted: true, request_id: 'op-emergency', operation_id: 'op-emergency', execution_state: 'accepted',
    } },
    setVolume: async percent => { calls.push(['volume', percent]); return { percent } },
  }
}

test('tool catalog exposes only safe trigger actions to generic execution', async () => {
  const dashboard = fakeDashboard()
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 20 })
  await tools.initialize()
  assert.deepEqual(tools.safeActionIds, ['wave_hand'])
  const definition = tools.definitions().find(item => item.name === 'm1_execute_action')
  assert.deepEqual(definition.inputSchema.properties.action_id.enum, ['wave_hand'])
  const result = await tools.handle({ name: 'client.tool.m1_execute_action', arguments: { action_id: 'return_to_charge' } })
  assert.equal(result.status, 'failed')
  assert.equal(result.error.code, 'action_not_available')
})

test('prearranged action returns operation semantics without claiming completion', async () => {
  const dashboard = fakeDashboard()
  const tools = new M1RobotTools({ dashboard })
  await tools.initialize()
  const result = await tools.handle({
    name: 'client.tool.m1_execute_action', arguments: { action_id: 'wave_hand' },
  })
  assert.equal(result.status, 'completed')
  assert.equal(result.output.operation_id, 'op-wave_hand')
  assert.equal(result.output.execution_state, 'running')
  assert.equal(result.output.completion_confirmed, false)
  assert.match(result.output.user_message_rule, /not that it finished/)
})

test('vehicle tools expose status and map to authoritative dashboard workflows', async () => {
  const dashboard = fakeDashboard()
  const tools = new M1RobotTools({ dashboard })
  await tools.initialize()
  const names = tools.definitions().map(item => item.name)
  assert.ok(names.includes('m1_get_vehicle_status'))
  assert.ok(names.includes('m1_mount_cart'))
  assert.ok(names.includes('m1_dismount_cart'))
  assert.ok(names.includes('m1_return_to_charging_dock'))
  assert.ok(names.includes('m1_leave_charging_dock'))

  const status = await tools.handle({ name: 'client.tool.m1_get_vehicle_status', arguments: {} })
  assert.equal(status.status, 'completed')
  assert.equal(status.output.mount.state, 'on_vehicle')

  const expected = [
    ['m1_mount_cart', 'visual_get_on_car'],
    ['m1_dismount_cart', 'get_off_car'],
    ['m1_return_to_charging_dock', 'return_to_charge'],
    ['m1_leave_charging_dock', 'off_charger'],
  ]
  for (const [toolName, commandId] of expected) {
    const result = await tools.handle({
      name: `client.tool.${toolName}`,
      arguments: { reason: 'unit test' },
    })
    assert.equal(result.status, 'completed')
    assert.equal(result.output.command_id, commandId)
    assert.equal(result.output.completion_confirmed, false)
  }
  assert.deepEqual(
    dashboard.calls.filter(call => call[0] === 'command').map(call => call[1]),
    expected.map(([, commandId]) => commandId),
  )
})

test('vision tools expose health and compact semantic detections', async () => {
  const tools = new M1RobotTools({ dashboard: fakeDashboard() })
  await tools.initialize()
  const names = tools.definitions().map(item => item.name)
  assert.ok(names.includes('m1_get_vision_status'))
  assert.ok(names.includes('m1_get_detections'))

  const status = await tools.handle({ name: 'client.tool.m1_get_vision_status', arguments: {} })
  assert.equal(status.status, 'completed')
  assert.equal(status.output.processor.model, 'detector.hbm')
  assert.equal(status.output.processor.latest_age_seconds, 0.08)

  const detections = await tools.handle({ name: 'client.tool.m1_get_detections', arguments: {} })
  assert.equal(detections.status, 'completed')
  assert.deepEqual(detections.output.counts, { person: 1, hand: 1 })
  assert.equal(detections.output.detections[0].position, 'left')
  assert.equal(detections.output.detections[1].position, 'center')
})

test('bounded movement returns immediately and delegates its deadline to Dashboard', async () => {
  const dashboard = fakeDashboard()
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  const result = await tools.handle({
    name: 'client.tool.m1_move', arguments: { direction: 'forward', duration_ms: 200 },
  })
  assert.equal(result.status, 'completed')
  assert.deepEqual(dashboard.calls.map(call => call[0]), ['command'])
  assert.equal(dashboard.calls[0][2].durationMs, 200)
  assert.equal(result.output.operation_id, 'op-move_forward')
  assert.equal(result.output.execution_state, 'running')
  assert.equal(result.output.completion_confirmed, false)
  assert.equal(result.output.safety_owner, 'dashboard')
  assert.match(result.output.user_message_rule, /started/)
})

test('movement owns its precheck and action tools request immediate spoken acknowledgement', async () => {
  const tools = new M1RobotTools({ dashboard: fakeDashboard(), maxMoveMs: 250 })
  await tools.initialize()
  const definitions = new Map(tools.definitions().map(item => [item.name, item]))

  assert.match(definitions.get('m1_move').description, /不要预先调用 m1_get_vehicle_status 或 m1_get_state/)
  assert.match(definitions.get('m1_get_vehicle_status').description, /普通前进、后退、横移或转向不要先调用/)
  for (const name of [
    'm1_move', 'm1_execute_action', 'm1_mount_cart', 'm1_dismount_cart',
    'm1_return_to_charging_dock', 'm1_leave_charging_dock',
  ]) {
    assert.match(definitions.get(name).description, /同一响应中立即调用工具/)
  }
})

test('walking motion status does not expose zero wheel speeds as evidence of no movement', async () => {
  const dashboard = fakeDashboard(undefined, {
    vendor_service: 'online',
    command: {
      command_id: 'move_forward',
      phase: 'observed_running',
      observation_result: 'observed_moving',
      motion_evidence: ['joint moved Δ2.4°'],
    },
    robot: {
      control_mode: 1,
      control_mode_label: '步态行走',
      locomotion_mode: 'walking',
      wheel_speed_applicable: false,
      left_wheel_speed: 0,
      right_wheel_speed: 0,
      gait_type: 1,
      gait_type_label: '前进',
    },
  })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  const result = await tools.handle({ name: 'client.tool.m1_get_motion_status', arguments: {} })
  assert.equal(result.status, 'completed')
  assert.equal(result.output.movement.observed, true)
  assert.equal(result.output.locomotion_mode, 'walking')
  assert.equal(result.output.robot.wheel_telemetry.applicable, false)
  assert.equal(Object.hasOwn(result.output.robot.wheel_telemetry, 'left_speed'), false)
})

test('motion status explains that unconfirmed telemetry is not a failed move', async () => {
  const dashboard = fakeDashboard(undefined, {
    command: { command_id: 'move_forward', phase: 'completed', observation_result: 'not_confirmed' },
    robot: { control_mode: 1, locomotion_mode: 'walking', wheel_speed_applicable: false },
  })
  const tools = new M1RobotTools({ dashboard })
  await tools.initialize()
  const result = await tools.handle({ name: 'client.tool.m1_get_motion_status', arguments: {} })
  assert.equal(result.output.movement.observed, null)
  assert.match(result.output.movement.interpretation, /not proof/)
})

test('charging interlock blocks locomotion before the vendor command', async () => {
  const dashboard = fakeDashboard({
    battery: { power: 8, is_charging: true, on_charger_raw: 2 },
  })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  const result = await tools.handle({
    name: 'client.tool.m1_move', arguments: { direction: 'forward', duration_ms: 200 },
  })
  assert.equal(result.status, 'failed')
  assert.equal(result.error.code, 'robot_charging')
  assert.equal(result.error.detail.executed, false)
  assert.equal(result.error.detail.blocked_by, 'charging_interlock')
  assert.match(result.error.detail.user_message, /充电/)
  assert.deepEqual(dashboard.calls, [])
})

test('wheel mode blocks rotate and strafe before the Dashboard command', async () => {
  const dashboard = fakeDashboard({
    battery: { power: 80, is_charging: false, on_charger_raw: 0 },
    business: { is_on_car: 1 },
  })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  for (const direction of ['rotate_left', 'rotate_right', 'strafe_left', 'strafe_right']) {
    const result = await tools.handle({
      name: 'client.tool.m1_move', arguments: { direction, duration_ms: 200 },
    })
    assert.equal(result.status, 'failed')
    assert.equal(result.error.code, 'unsupported_in_wheeled_mode')
    assert.equal(result.error.detail.executed, false)
    assert.equal(result.error.detail.blocked_by, 'wheeled_mode')
    assert.match(result.error.detail.user_message, /轮式小车/)
  }
  assert.deepEqual(dashboard.calls, [])
})

test('wheel mode still permits forward and backward movement', async () => {
  const dashboard = fakeDashboard({
    battery: { power: 80, is_charging: false, on_charger_raw: 0 },
    business: { is_on_car: 1 },
  })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  for (const direction of ['forward', 'backward']) {
    const result = await tools.handle({
      name: 'client.tool.m1_move', arguments: { direction, duration_ms: 200 },
    })
    assert.equal(result.status, 'completed')
  }
  assert.deepEqual(dashboard.calls.map(call => call[1]), ['move_forward', 'move_backward'])
})

test('unknown mount state fails closed for rotate and strafe movement', async () => {
  const dashboard = fakeDashboard({
    battery: { power: 80, is_charging: false, on_charger_raw: 0 },
  })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  const result = await tools.handle({
    name: 'client.tool.m1_move', arguments: { direction: 'rotate_left', duration_ms: 200 },
  })
  assert.equal(result.status, 'failed')
  assert.equal(result.error.code, 'motion_mode_unavailable')
  assert.equal(result.error.detail.blocked_by, 'mount_state_unknown')
  assert.deepEqual(dashboard.calls, [])
})

test('unknown charging state fails closed before the vendor command', async () => {
  const dashboard = fakeDashboard({ battery: 100 })
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 250 })
  await tools.initialize()
  const result = await tools.handle({
    name: 'client.tool.m1_move', arguments: { direction: 'forward', duration_ms: 200 },
  })
  assert.equal(result.status, 'failed')
  assert.equal(result.error.code, 'motion_precondition_unavailable')
  assert.deepEqual(dashboard.calls, [])
})

test('explicit stop returns after vendor acceptance without waiting for physical settling', async () => {
  const dashboard = fakeDashboard()
  const tools = new M1RobotTools({ dashboard, maxMoveMs: 3_000 })
  await tools.initialize()
  const stopped = await tools.handle({ name: 'client.tool.m1_stop', arguments: {} })
  assert.equal(stopped.status, 'completed')
  assert.equal(stopped.output.operation_id, 'op-stop')
  assert.equal(stopped.output.completion_confirmed, false)
  assert.deepEqual(dashboard.calls.map(call => call[0]), ['stop'])
})

test('failed movement submission returns a tool failure', async () => {
  const dashboard = fakeDashboard()
  dashboard.command = async () => { throw new Error('vendor offline') }
  const tools = new M1RobotTools({ dashboard })
  await tools.initialize()
  const failed = await tools.handle({ name: 'client.tool.m1_move', arguments: { direction: 'forward' } })
  assert.equal(failed.status, 'failed')
})
