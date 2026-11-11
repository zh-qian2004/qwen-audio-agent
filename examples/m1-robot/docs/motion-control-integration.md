# M1 Dashboard 运动控制集成报告

日期：2026-11-09
目标：在现有 Dashboard 中集成厂商控制台运动能力，保留原有状态监控、相机、音频、关节、IMU、电池、传感器与 ROS 图功能。

## 1. 集成结果

| 项目 | 结果 |
|---|---|
| 本地代码 | `D:\projects\m1-robot\dashboard\` |
| 机器人端 | `/userdata/developer/dashboard/` |
| 厂商控制服务 | `http://127.0.0.1:12321` |
| Dashboard | `127.0.0.1:8080`，重启后 `read_only=false`，`motion_control=true` |
| ROS 采集 | 16 个 subscriber，新增 `/ControlBipedMotionFeedback_topic` 与 `/HardwareSelfFall_topic` |
| 单元测试 | 25 项全部通过（本地 Python 3.11，stdlib unittest） |
| 浏览器验证 | 运动控制页 PASS，无 console 错误 |
| 实机 API 验证 | 健康、电池、`MOTION_STOP`、`HEAD_RESET`、幂等、禁用/未知命令、审计均验证 |

## 2. 新增与修改文件

### 新增

| 文件 | 作用 |
|---|---|
| `config/vendor_commands.json` | 30 条命令目录、分类、物理影响、交互方式、超时、验证状态、步态/控制模式码表 |
| `src/vendor_control.py` | 厂商 HTTP 客户端；回环地址、连接/读取分离超时、UUID、延迟、统一错误 |
| `src/motion_state.py` | 命令生命周期与 ROS 观测状态机；HTTP 成功与运动完成分离 |
| `src/control_audit.py` | `logs/motion_control.jsonl` 审计，4 MiB × 3 轮转 |
| `src/motion_control.py` | 白名单、门控、幂等、忙碌互斥、按住看门狗、健康探测、运动监控 |
| `tests/test_motion_control.py` | 25 项单元测试 |
| `motion-control-integration.md` | 本报告 |

### 修改

| 文件 | 变更 |
|---|---|
| `src/main.py` | 新增 7 个 `/api/motion/*` 接口；`/api/state` 与 WebSocket 附带 `motion_control`；健康检查与控制能力 |
| `src/state.py` | 新增运动反馈/跌倒状态、`motion_observation()`、版本 2.0.0、只读标志关闭 |
| `src/ros_collector.py` | 订阅 `plan/msg/BipedMotionCmd` 与 `std_msgs/msg/UInt32`；节点名改为 `zeroth_dashboard` |
| `config/dashboard.json` | 新增两个话题定义 |
| `static/index.html` | 新增“运动控制”页、安全停止条、方向键盘、动作分组、机器人反馈、时间线、确认弹窗与通知 |
| `static/app.js` | 命令目录渲染、按住/松开、停止/急停/恢复、生命周期与时间线渲染、2 秒历史轮询 |
| `static/styles.css` | 运动控制页面与安全停止样式、响应式布局 |
| `README.md` | 更新功能、接口、架构、安全边界、无数据说明与测试说明 |

## 3. 命令目录与验证状态

- `vendor_confirmed`：方向、身体、头部/手臂、业务、模式、急停共 25 条，来自厂商二进制与网页共同确认。
- `frontend_observed`：`SAY_WOZAI`、`MUTE`、`RESET`、`ENTER-FOLLOW`、`REMOTE_CONTROL_MODE`；仅在厂商网页源码出现。
- `ENTER-FOLLOW`、`REMOTE_CONTROL_MODE`、`RESET` 默认 `dashboard_enabled=false`；前两者会驱动底盘或抢占控制权，`RESET` 语义未确认。
- 方向命令全部为 `hold`，停止命令 `MOTION_STOP`；`MOTION_STOP` 为独立高优先级路径。
- 高风险动作需要页面二次确认：上下小车、视觉上小车、回充、`get_up`、`reset`。

## 4. 实机验证记录

### 4.1 服务与反馈

| 检查 | 结果 |
|---|---|
| `/api/health` | `status=ok`、`ros=online`、`vendor_service=online`、`read_only=false` |
| 厂商电池 `/api/battery` | 实机返回 `power`、`voltage`（mV）、`current`、`runtime`；页面按 V 显示 |
| 运动反馈 | `control_mode=1`（步态行走）、`gait_type=0`（停止）、`linear_velocity_x=0`、`angular_rate_z=0` |
| 底盘反馈 | 左右轮速 0；数据年龄约 0.01–0.03 s |
| 头部反馈 | yaw 0.6–0.9°、pitch -3.07°，数据年龄约 0.02 s |
| 跌倒反馈 | `/HardwareSelfFall_topic` 为 `std_msgs/msg/UInt32`；当前无消息，页面显示“无数据” |

### 4.2 `MOTION_STOP`

```text
POST /api/motion/stop
HTTP 200 · vendor /api/control HTTP 200 · latency ≈ 47 ms
phase = completed（轮速为 0，ROS 确认已停止）
```

### 4.3 `HEAD_RESET`

```text
POST /api/motion/command {"command_id":"head_reset"}
HTTP 200 · vendor HTTP 200 · latency ≈ 28.5 ms
phase = observing → timed_out
detail = vendor accepted but no ROS observation within 10.1s
```

结论：厂商接口受理与头部实际运动是两个独立事实；Dashboard 正确显示 `timed_out`，没有把 HTTP 200 当作动作完成。

### 4.4 安全与校验

| 场景 | 结果 |
|---|---|
| 相同 `client_request_id` 重复提交 | 第二次返回 `idempotent_replay=true`，仅下发一次 |
| `enter_follow`（默认禁用） | HTTP 403 `command_disabled` |
| 未知 `command_id` | HTTP 400 `unknown_command` |
| 命令进行中再次发送其他运动 | HTTP 409 `busy` |
| 跌倒状态非 0 | 底盘命令 409 `fall_detected`；`GET_UP`/停止仍允许 |
| 按住超时 | 后端自动发送 `MOTION_STOP` 并写审计 |
| 急停路由不存在 | 自动回退 `/api/control` 的 `EMERGENCY_STOP`；恢复回退 `RESET` |

### 4.5 浏览器

- 打开运动控制页，厂商状态显示“在线”，急停状态“正常”。
- 安全停止条、方向键盘、五个动作分组、机器人反馈、14 条历史事件均正常显示。
- 浏览器 console 无错误。
- 验证过程中未通过浏览器点击任何运动命令或急停按钮。

## 5. 未完成的实机验证

以下项目因涉及真实运动或安全风险，未在本次集成中主动触发：

| 项目 | 原因 | 建议 |
|---|---|---|
| 六向运动与旋转 | 会驱动真实底盘 | 在空旷、有人监护的场地逐个短按验证，先确认 `MOTION_STOP` |
| `GET_UP` | 需要倒地姿态；厂商业务层已知可能忽略外部独立起立 | 在受控倒地状态下验证，结合 `HardwareSelfFall_topic` |
| 上下小车 / 视觉上小车 / 回充 | 需要小车、场地、地图与定位条件 | 按业务 SOP 验证，保持急停可达 |
| `EMERGENCY_STOP` / 恢复 | 可能导致站立机器人失能 | 在机器人稳定支撑或有人保护时成对验证 |
| `SAY_WOZAI` / `MUTE` | 仅语音/静音，无 ROS 反馈 | 需要人工听感确认；当前标记 `frontend_observed` |
| `ENTER-FOLLOW` / `REMOTE_CONTROL_MODE` | 会驱动底盘或抢占控制权 | 默认禁用，完成接口与停止路径验证后再启用 |

## 6. 部署

```bash
# 本地打包（src/config/static/tests）
tar -cf dashboard-update.tar -C dashboard src config static tests

# 上传并解压
scp dashboard-update.tar root@192.168.137.228:/tmp/
ssh root@192.168.137.228 \
  "tar -xf /tmp/dashboard-update.tar -C /userdata/developer/dashboard"

# 重启
ssh root@192.168.137.228 \
  "/userdata/developer/dashboard/scripts/stop.sh && /userdata/developer/dashboard/scripts/run.sh"
```

本次部署后 PID 19092，`/api/health` 正常，ROS 16 个订阅，厂商服务在线。

## 7. 运维提示

- 审计文件：`/userdata/developer/dashboard/logs/motion_control.jsonl`。
- 运行日志：`/userdata/developer/dashboard/logs/dashboard.log`。
- 页面停止按钮与方向松手都会发送 `MOTION_STOP`；急停按钮独立调用 `/api/motion/emergency-stop`。
- 若厂商服务离线，页面安全条变为离线状态，命令返回 502 并写入审计。
- 若 ROS 反馈中断，命令会在观测超时后结束为 `timed_out`，不会误报 `completed`。
