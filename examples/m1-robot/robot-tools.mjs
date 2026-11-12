import { DashboardError, enabledCommands } from './dashboard-client.mjs'

const ACTION_ACK = '用户明确要求执行本动作时，禁止预先调用 m1_get_state、m1_get_motion_status、m1_get_vehicle_status 或其他状态查询工具；安全预检查由本动作工具和 Dashboard 内部完成。先用一句极短口语确认（如“好，我现在执行”），并在同一响应中立即调用工具，不要等工具结果才开始说话。'

const MOVE_IDS = Object.freeze({
  forward: 'move_forward',
  backward: 'move_backward',
  strafe_left: 'strafe_left',
  strafe_right: 'strafe_right',
  rotate_left: 'rotate_left',
  rotate_right: 'rotate_right',
})

const WHEELED_UNSUPPORTED_DIRECTIONS = new Set([
  'rotate_left', 'rotate_right',
])

const TERMINAL_MOTION_PHASES = new Set([
  'completed', 'timed_out', 'vendor_rejected', 'connection_failed',
  'state_mismatch', 'cancelled',
])

const VEHICLE_ACTIONS = Object.freeze({
  m1_mount_cart: {
    commandId: 'visual_get_on_car',
    description: `${ACTION_ACK}让 M1 通过厂商完整视觉流程上小车。仅在机器人不在小车上、未连接背部直充且小车已在机器人前方准备好时调用。返回受理状态；只有 completion_confirmed=true 才表示确实上车成功。`,
  },
  m1_dismount_cart: {
    commandId: 'get_off_car',
    description: `${ACTION_ACK}让 M1 从轮式小车上下车，切回双足模式。它不是“驶离充电桩”；仅在机器人已在小车上且不在充电桩上时调用。`,
  },
  m1_return_to_charging_dock: {
    commandId: 'return_to_charge',
    description: `${ACTION_ACK}让已在小车上的 M1 执行厂商完整回充流程。近距离可通过激光反光标识对接；只有业务成功事件、在车状态和硬件充电状态同时成立才确认完成。`,
  },
  m1_leave_charging_dock: {
    commandId: 'off_charger',
    description: `${ACTION_ACK}让仍在轮式小车上的 M1 驶离充电桩，机器人不会从小车上下车。仅在机器人已在小车上且正在充电桩上时调用，并确保驶离方向有足够空间。`,
  },
})

const noArguments = Object.freeze({ type: 'object', properties: {}, additionalProperties: false })

function tool(name, description, inputSchema = noArguments, responseOnSuccess = 'auto') {
  return { name, description, inputSchema, response_on_success: responseOnSuccess }
}

function chargingInterlock(state) {
  const battery = state?.battery
  if (!battery || typeof battery !== 'object' || Array.isArray(battery)) {
    return { known: false, blocked: true }
  }
  const raw = Number(battery.on_charger_raw)
  const isCharging = battery.is_charging === true
  const onCharger = Number.isFinite(raw) && raw > 0
  return {
    known: true,
    blocked: isCharging || onCharger,
    isCharging,
    onCharger,
    onChargerRaw: Number.isFinite(raw) ? raw : null,
    power: Number.isFinite(Number(battery.power)) ? Number(battery.power) : null,
  }
}

function vehicleMountInterlock(state) {
  const raw = state?.business?.is_on_car ?? state?.motion_control?.robot?.is_on_car
  if (raw === true || raw === 1 || raw === '1') return { known: true, onVehicle: true, raw }
  if (raw === false || raw === 0 || raw === '0') return { known: true, onVehicle: false, raw }
  return { known: false, onVehicle: null, raw: raw ?? null }
}

function summarizeOperation(result, { commandId, interaction, durationMs } = {}) {
  const accepted = result?.accepted === true
  const completionConfirmed = result?.completion_confirmed === true
  return {
    accepted,
    operation_id: result?.operation_id || result?.request_id || null,
    command_id: result?.command_id || commandId || null,
    interaction: interaction || null,
    execution_state: result?.execution_state
      || (accepted ? completionConfirmed ? 'completed' : 'running' : 'failed'),
    completion_confirmed: completionConfirmed,
    phase: result?.phase ?? null,
    ...(durationMs === undefined ? {} : { duration_ms: result?.duration_ms ?? durationMs }),
    user_message_rule: completionConfirmed
      ? 'The operation is confirmed complete.'
      : 'The command was accepted but completion is not confirmed; say it has started, not that it finished.',
  }
}

function inferLocomotionMode(robot = {}) {
  const wheelSpeeds = [robot.left_wheel_speed, robot.right_wheel_speed]
    .filter(value => Number.isFinite(Number(value)))
    .map(value => Math.abs(Number(value)))
  if (wheelSpeeds.some(value => value >= 10)) return 'wheeled'
  if (typeof robot.locomotion_mode === 'string') return robot.locomotion_mode
  if (robot.control_mode === 1) return 'walking'
  if (robot.control_mode === 3) return 'velocity_control'
  if (robot.control_mode === 0) return 'idle'
  return 'unknown'
}

function summarizeMotionStatus(status) {
  if (!status || typeof status !== 'object' || status.truncated) return status
  const robot = status.robot && typeof status.robot === 'object' ? status.robot : {}
  const command = status.command && typeof status.command === 'object' ? status.command : null
  const locomotionMode = inferLocomotionMode(robot)
  const wheelSpeedApplicable = typeof robot.wheel_speed_applicable === 'boolean'
    ? robot.wheel_speed_applicable
    : locomotionMode === 'wheeled' ? true : locomotionMode === 'walking' ? false : null
  const observationResult = command?.observation_result
    || (command?.motion_observed_at ? 'observed_moving' : command ? 'pending' : 'not_available')
  const movementObserved = observationResult === 'observed_moving' ? true : null
  const wheelTelemetry = wheelSpeedApplicable === true
    ? {
        applicable: true,
        left_speed: robot.left_wheel_speed ?? null,
        right_speed: robot.right_wheel_speed ?? null,
      }
    : {
        applicable: wheelSpeedApplicable,
        reason: locomotionMode === 'walking'
          ? 'M1 is in walking mode; wheel speed is not evidence of whether its legs moved.'
          : 'Wheel telemetry is not currently authoritative for locomotion.',
      }
  return {
    vendor_service: status.vendor_service ?? status.vendor?.service ?? null,
    emergency: status.emergency ?? null,
    locomotion_mode: locomotionMode,
    movement: {
      observed: movementObserved,
      observation_result: observationResult,
      evidence: Array.isArray(command?.motion_evidence) ? command.motion_evidence : [],
      interpretation: movementObserved === true
        ? 'Robot movement was confirmed by mode-appropriate telemetry.'
        : 'Movement has not been confirmed by available telemetry; this is not proof that the robot stayed still.',
    },
    command: command && {
      operation_id: command.request_id ?? null,
      command_id: command.command_id ?? null,
      phase: command.phase ?? null,
      stop_requested: command.stop_requested === true,
      detail: command.detail ?? null,
    },
    robot: {
      control_mode: robot.control_mode ?? null,
      control_mode_label: robot.control_mode_label ?? null,
      gait_type: robot.gait_type ?? null,
      gait_type_label: robot.gait_type_label ?? null,
      linear_velocity_x: robot.linear_velocity_x ?? null,
      angular_rate_z: robot.angular_rate_z ?? null,
      wheel_telemetry: wheelTelemetry,
    },
  }
}

function summarizeVisionStatus(status) {
  if (!status || typeof status !== 'object' || status.truncated) return status
  const processors = Array.isArray(status.processors) ? status.processors : []
  const processor = processors.find(item => item?.id === 'object_detection') || processors[0] || null
  const resultBus = status.result_bus && typeof status.result_bus === 'object' ? status.result_bus : {}
  const online = status.online !== false && status.status !== 'offline'
  const running = processor?.state === 'running'
  const sourceOnline = processor?.source_online === true
  const latestAgeSeconds = resultBus.latest_age_seconds ?? processor?.source_age_seconds ?? null
  const summary = !online
    ? '视觉服务离线。'
    : running && sourceOnline
      ? `视觉服务正常，目标检测正在运行，结果约为每秒 ${processor.output_rate_hz ?? 0} 帧。`
      : `视觉服务已连接，但目标检测状态为 ${processor?.state || 'unknown'}。`
  return {
    ok: online && status.status !== 'error',
    online,
    status: status.status ?? null,
    version: status.version ?? null,
    processor: processor && {
      id: processor.id ?? null,
      state: processor.state ?? null,
      source_online: processor.source_online ?? null,
      model: processor.model ?? null,
      input_rate_hz: processor.input_rate_hz ?? null,
      output_rate_hz: processor.output_rate_hz ?? null,
      latest_age_seconds: latestAgeSeconds,
      confidence_threshold: processor.confidence_threshold ?? null,
      last_error: processor.last_error ?? null,
    },
    summary,
  }
}

function detectionPosition(box) {
  if (!box || typeof box !== 'object') return 'unknown'
  const x1 = Number(box.x1)
  const x2 = Number(box.x2)
  if (!Number.isFinite(x1) || !Number.isFinite(x2)) return 'unknown'
  const center = (x1 + x2) / 2
  if (center < 1 / 3) return 'left'
  if (center > 2 / 3) return 'right'
  return 'center'
}

function summarizeDetections(payload) {
  if (!payload || typeof payload !== 'object' || payload.truncated) return payload
  const result = payload.result && typeof payload.result === 'object' ? payload.result : {}
  const detections = Array.isArray(result.detections) ? result.detections.map(item => ({
    id: item?.id ?? null,
    label: item?.label ?? null,
    confidence: item?.confidence ?? null,
    bbox_normalized: item?.bbox_normalized ?? null,
    position: detectionPosition(item?.bbox_normalized),
  })) : []
  const counts = {}
  for (const detection of detections) {
    if (detection.label) counts[detection.label] = (counts[detection.label] || 0) + 1
  }
  const labels = Object.entries(counts).map(([label, count]) => `${count} 个 ${label}`)
  const available = payload.available === true
  const stale = payload.stale === true
  const summary = !available || stale
    ? '当前没有可靠的实时视觉检测结果。'
    : labels.length ? `当前检测到${labels.join('、')}。` : '当前未检测到人体、头部、人脸或手部。'
  return {
    ok: available && !stale,
    available,
    stale,
    age_seconds: payload.age_seconds ?? null,
    frame: {
      sequence: result.source_frame_sequence ?? null,
      width: result.source_width ?? null,
      height: result.source_height ?? null,
      received_at: result.received_at ?? null,
    },
    counts,
    detections,
    summary,
  }
}

export class M1RobotTools {
  constructor({ dashboard, maxMoveMs = 3_000, delay = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
    if (!dashboard) throw new TypeError('dashboard is required')
    this.dashboard = dashboard
    this.maxMoveMs = maxMoveMs
    this.delay = delay
    this.commands = []
    this.safeActionIds = []
    this.moveDirections = []
    this.vehicleActionNames = []
  }

  async initialize() {
    const capabilities = await this.dashboard.motionCapabilities()
    this.commands = enabledCommands(capabilities)
    this.safeActionIds = this.commands
      .filter(item => item.interaction !== 'hold'
        && !item.requiresConfirmation
        && !['emergency_stop', 'motion_stop'].includes(item.id))
      .map(item => item.id)
      .sort()
    const available = new Set(this.commands.map(item => item.id))
    this.moveDirections = Object.entries(MOVE_IDS)
      .filter(([, commandId]) => available.has(commandId))
      .map(([direction]) => direction)
    this.vehicleActionNames = Object.entries(VEHICLE_ACTIONS)
      .filter(([, action]) => available.has(action.commandId))
      .map(([name]) => name)
    return capabilities
  }

  definitions({ includeBackgroundTools = false } = {}) {
    const actionSchema = {
      type: 'object',
      properties: {
        action_id: {
          type: 'string',
          ...(this.safeActionIds.length ? { enum: this.safeActionIds } : {}),
          description: 'Dashboard 已启用且无需二次确认的动作 ID',
        },
        reason: { type: 'string', maxLength: 200, description: '简短说明用户为什么要求执行该动作' },
      },
      required: ['action_id'],
      additionalProperties: false,
    }
    const definitions = [
      tool('m1_get_state', '查询 M1 机器人的当前电量、姿态、传感器、运动和故障状态。仅在用户明确询问当前状态时调用；禁止把本工具用作任何动作的前置检查，动作工具会自行完成安全预检查。'),
      tool('m1_get_motion_status', '查询当前运动命令、急停和运动观测。M1 可处于步态或轮式模式：步态模式下轮速不适用；“遥测未确认”不代表机器人没有移动。'),
      tool('m1_get_vehicle_status', '仅当用户明确询问小车、在车、车轮、里程、电流、故障或充电桩状态时调用。普通前进、后退、横移或转向不要先调用本工具，也不要先调用 m1_get_state；m1_move 自己完成安全预检查。'),
      tool('m1_get_vision_status', '查询 M1 视觉服务、目标检测模型和结果流是否正常。该工具不返回原始图像。'),
      tool('m1_get_detections', '查询 M1 当前最新的人体、头部、人脸和手部检测结果。只能描述未过期的结果，不要将它当作通用图像理解。'),
      tool('m1_list_actions', '列出当前 Dashboard 已启用的机器人动作和风险、交互方式。'),
      tool('m1_stop', '立即停止 M1 当前普通运动。用户说停下、别动、停止运动时直接调用。', noArguments, 'none'),
      tool('m1_emergency_stop', '触发 M1 紧急停止。用于危险、失控、碰撞风险或用户明确要求急停的情况。', {
        type: 'object',
        properties: { reason: { type: 'string', maxLength: 200 } },
        additionalProperties: false,
      }),
      tool('m1_set_volume', '设置 M1 扬声器音量百分比。', {
        type: 'object',
        properties: { percent: { type: 'integer', minimum: 0, maximum: 100 } },
        required: ['percent'],
        additionalProperties: false,
      }),
    ]
    const vehicleActionSchema = {
      type: 'object',
      properties: {
        reason: { type: 'string', maxLength: 200, description: '简短说明用户为什么要求执行该操作' },
      },
      additionalProperties: false,
    }
    const vehicleTools = this.vehicleActionNames.map(name => tool(
      name,
      VEHICLE_ACTIONS[name].description,
      vehicleActionSchema,
    ))
    definitions.splice(3, 0, ...vehicleTools)
    if (this.safeActionIds.length) {
      definitions.splice(3, 0,
        tool('m1_execute_action', `${ACTION_ACK}提交一个已启用、无需二次确认的 M1 预编排动作。工具在厂商受理后立即返回 operation_id；completion_confirmed=false 时只能说动作已开始，不能说已经完成。`, actionSchema))
    }
    if (this.moveDirections.length) {
      definitions.splice(this.safeActionIds.length ? 4 : 3, 0,
        tool('m1_move', `${ACTION_ACK}普通方向移动直接调用本工具，不要预先调用 m1_get_vehicle_status 或 m1_get_state；本工具内部完成充电和在车模式安全预检查。轮式小车模式支持前进、后退及向左/向右平移，但原地左右转会被拒绝。Dashboard 按 duration_ms 在后台自动停止。厂商受理后立即返回；completion_confirmed=false 时只能说已开始运动。步态模式下轮速为 0 不是失败。`, {
          type: 'object',
          properties: {
            direction: { type: 'string', enum: this.moveDirections },
            duration_ms: { type: 'integer', minimum: 200, maximum: this.maxMoveMs, default: 1_000 },
            reason: { type: 'string', maxLength: 200 },
          },
          required: ['direction'],
          additionalProperties: false,
        }))
    }
    if (includeBackgroundTools) {
      for (const definition of definitions) {
        if (definition.description.startsWith(ACTION_ACK)) {
          definition.description = definition.description.slice(ACTION_ACK.length)
        }
      }
      definitions.push(tool('m1_wait_for_motion', '等待指定 M1 运动操作进入终态，并返回最终运动观测。开放任务中每次调用 m1_move 或运动动作后都应先等待完成，再进行下一次视觉或状态观察。', {
        type: 'object',
        properties: {
          operation_id: { type: 'string', minLength: 1, description: 'm1_move 或动作工具返回的 operation_id' },
          timeout_ms: { type: 'integer', minimum: 200, maximum: 30_000, default: 10_000 },
          poll_interval_ms: { type: 'integer', minimum: 100, maximum: 1_000, default: 250 },
        },
        required: ['operation_id'],
        additionalProperties: false,
      }))
    }
    return definitions
  }

  async handle(event) {
    const name = String(event?.name || '').replace(/^client\.tool\./, '')
    const args = event?.arguments && typeof event.arguments === 'object' ? event.arguments : {}
    try {
      let output
      if (name === 'm1_get_state') output = await this.dashboard.currentState()
      else if (name === 'm1_get_motion_status') output = summarizeMotionStatus(await this.dashboard.motionStatus())
      else if (name === 'm1_get_vehicle_status') output = await this.dashboard.vehicleStatus()
      else if (name === 'm1_get_vision_status') output = summarizeVisionStatus(await this.dashboard.visionStatus())
      else if (name === 'm1_get_detections') output = summarizeDetections(await this.dashboard.visionDetections())
      else if (name === 'm1_list_actions') output = { commands: this.commands }
      else if (name === 'm1_execute_action') output = await this.#executeAction(args)
      else if (Object.hasOwn(VEHICLE_ACTIONS, name)) output = await this.#executeVehicleAction(name, args)
      else if (name === 'm1_move') output = await this.#move(args)
      else if (name === 'm1_wait_for_motion') output = await this.#waitForMotion(args)
      else if (name === 'm1_stop') output = await this.stopActive('user requested stop')
      else if (name === 'm1_emergency_stop') output = await this.#emergencyStop(args)
      else if (name === 'm1_set_volume') output = await this.#setVolume(args)
      else return { status: 'unsupported', error: { code: 'unknown_m1_tool', message: `Unknown M1 tool: ${name}` } }
      return { status: 'completed', output }
    } catch (error) {
      return {
        status: 'failed',
        error: {
          code: error instanceof DashboardError ? error.code : 'm1_tool_failed',
          message: error?.message || String(error),
          ...(error?.detail === undefined ? {} : { detail: error.detail }),
        },
      }
    }
  }

  async #executeAction(args) {
    if (!this.safeActionIds.includes(args.action_id)) {
      throw new DashboardError('action_not_available', `Action is not available to the voice agent: ${args.action_id}`)
    }
    const command = this.commands.find(item => item.id === args.action_id)
    const result = await this.dashboard.command(args.action_id, {
      reason: args.reason || 'qwen audio agent action',
    })
    return summarizeOperation(result, {
      commandId: args.action_id,
      interaction: command?.interaction || 'trigger',
    })
  }

  async #executeVehicleAction(name, args) {
    if (!this.vehicleActionNames.includes(name)) {
      throw new DashboardError('vehicle_action_not_available', `Vehicle action is not available: ${name}`)
    }
    const action = VEHICLE_ACTIONS[name]
    const command = this.commands.find(item => item.id === action.commandId)
    const result = await this.dashboard.command(action.commandId, {
      reason: args.reason || `qwen audio agent ${name}`,
    })
    return summarizeOperation(result, {
      commandId: action.commandId,
      interaction: command?.interaction || 'workflow',
    })
  }

  async #move(args) {
    const commandId = MOVE_IDS[args.direction]
    if (!commandId || !this.moveDirections.includes(args.direction)) {
      throw new DashboardError('invalid_direction', `Unsupported or unavailable direction: ${args.direction}`)
    }
    await this.#assertLocomotionAllowed(args.direction)
    const durationMs = Math.min(this.maxMoveMs, Math.max(200, Number(args.duration_ms) || 1_000))
    const result = await this.dashboard.command(commandId, {
      reason: args.reason || `qwen audio agent bounded move ${args.direction}`,
      durationMs,
    })
    return {
      ...summarizeOperation(result, { commandId, interaction: 'hold', durationMs }),
      direction: args.direction,
      safety_owner: 'dashboard',
      automatic_stop: true,
    }
  }

  async #assertLocomotionAllowed(direction) {
    const state = await this.dashboard.currentState()
    if (WHEELED_UNSUPPORTED_DIRECTIONS.has(direction)) {
      const mount = vehicleMountInterlock(state)
      if (!mount.known) {
        throw new DashboardError(
          'motion_mode_unavailable',
          '无法确认机器人是否处于轮式小车模式，未执行原地转向。请检查在车状态后重试。',
          { detail: { executed: false, blocked_by: 'mount_state_unknown', mount } },
        )
      }
      if (mount.onVehicle) {
        throw new DashboardError(
          'unsupported_in_wheeled_mode',
          '轮式小车模式暂不支持原地左右转，未执行命令。请先下车切换到双足模式。',
          {
            detail: {
              executed: false,
              blocked_by: 'wheeled_mode',
              direction,
              user_message: '我当前在轮式小车上，暂不支持原地左右转。可以让我向左或向右平移，或者先下车切换到双足模式。',
              mount,
            },
          },
        )
      }
    }
    const interlock = chargingInterlock(state)
    if (!interlock.known) {
      throw new DashboardError(
        'motion_precondition_unavailable',
        '无法确认机器人是否离开充电桩，未执行移动。请检查机器人状态后重试。',
        { detail: { executed: false, blocked_by: 'charging_state_unknown' } },
      )
    }
    if (interlock.blocked) {
      throw new DashboardError(
        'robot_charging',
        '机器人正在充电或仍停在充电桩上，未执行移动。请先将机器人移离充电桩后再试。',
        {
          detail: {
            executed: false,
            blocked_by: 'charging_interlock',
            user_message: '我现在正在充电或仍停在充电桩上，不能移动。请先把我移离充电桩。',
            battery: interlock,
          },
        },
      )
    }
  }

  async #waitForMotion(args) {
    const operationId = String(args.operation_id || '').trim()
    if (!operationId) throw new DashboardError('invalid_operation_id', 'operation_id is required')
    const timeoutMs = Math.min(30_000, Math.max(200, Number(args.timeout_ms) || 10_000))
    const pollIntervalMs = Math.min(1_000, Math.max(100, Number(args.poll_interval_ms) || 250))
    const deadline = Date.now() + timeoutMs
    let status
    do {
      status = await this.dashboard.motionStatus()
      const command = status?.command && typeof status.command === 'object' ? status.command : null
      const currentOperationId = command?.request_id || command?.operation_id || null
      if (currentOperationId && currentOperationId !== operationId) {
        return {
          operation_id: operationId,
          terminal: true,
          completion_confirmed: false,
          outcome: 'superseded',
          current_operation_id: currentOperationId,
          motion_status: summarizeMotionStatus(status),
        }
      }
      const phase = command?.phase || null
      if (phase && TERMINAL_MOTION_PHASES.has(phase)) {
        return {
          operation_id: operationId,
          terminal: true,
          completion_confirmed: phase === 'completed',
          outcome: phase,
          error: command?.error || null,
          motion_status: summarizeMotionStatus(status),
        }
      }
      if (Date.now() >= deadline) break
      await this.delay(Math.min(pollIntervalMs, Math.max(0, deadline - Date.now())))
    } while (Date.now() <= deadline)
    return {
      operation_id: operationId,
      terminal: false,
      completion_confirmed: false,
      outcome: 'wait_timed_out',
      timeout_ms: timeoutMs,
      motion_status: summarizeMotionStatus(status),
    }
  }

  async stopActive(reason = 'agent connector stopping') {
    const result = await this.dashboard.stop({ reason })
    return summarizeOperation(result, { commandId: 'motion_stop', interaction: 'trigger' })
  }

  async #emergencyStop(args) {
    const result = await this.dashboard.emergencyStop({
      reason: args.reason || 'qwen audio agent emergency stop',
    })
    return summarizeOperation(result, { commandId: 'emergency_stop', interaction: 'trigger' })
  }

  async #setVolume(args) {
    const percent = Number(args.percent)
    if (!Number.isInteger(percent) || percent < 0 || percent > 100) {
      throw new DashboardError('invalid_volume', 'Volume percent must be an integer from 0 to 100')
    }
    return this.dashboard.setVolume(percent)
  }
}

export {
  MOVE_IDS, summarizeDetections, summarizeMotionStatus, summarizeOperation, summarizeVisionStatus,
  vehicleMountInterlock,
}
