# Zeroth M1 实时状态与运动控制 Dashboard

运行在机器人本机的观测与控制服务。通过 ROS 2 订阅机器人实时状态，在浏览器中查看；通过厂商控制服务 `127.0.0.1:12321` 下发运动命令。远程设备通过 SSH 端口转发访问。

> 运动控制已启用。所有命令经过 Dashboard 白名单、急停/跌倒门控、按住超时保护和 JSONL 审计；浏览器不直接访问厂商服务。
> 厂商 HTTP 成功只代表命令入口已受理，动作是否真正执行必须以 ROS 步态、底盘、头部与关节反馈为准。

## 功能

### 页面模块

| 模块 | 内容 |
|---|---|
| 运行总览 | Dashboard 与 ROS 2 连接状态、最后更新时间、固件、主机名、IP、运行时间、CPU、BPU 利用率/频率/占用进程、内存、磁盘、温度 |
| 运动控制 | 厂商服务状态、急停状态、命令生命周期、HTTP 与 ROS 双状态、方向按住控制、身体/头部/手臂/业务/模式动作、底盘反馈、请求响应时间线 |
| 关节状态 | 20 路关节名称、角度（°）、速度（MCU 原始值）、电流/负载/温度/错误反馈、数据年龄、stale/offline 状态、60 秒角度曲线；按头部、左臂、右臂、左腿、右腿分组 |
| 姿态与 IMU | Roll/Pitch/Yaw、角速度（rad/s）、线加速度（m/s²）、人工地平线可视化、60 秒姿态曲线 |
| 电池与底盘 | 电量 SOC、电压、充电状态、底板电压、车轮速度/里程、通信状态与故障码 |
| 音视频流 | RGB 相机实时画面（约 2 fps，服务端只保留最新一帧）；通过 `vision_service` 启停目标检测并用浏览器 Canvas 叠加人体/头部/人脸/手部检测框；厂商 `media_audio` 共享 PCM 的实时电平、峰值和历史曲线；提供 Agent WebSocket 音频入口 |
| Video Service | 独立导航页，自动展示 `vision_service` 当前注册的各个 Processor、模型、数据流、帧率、累计结果、ROS/结果总线以及 CPU/内存/BPU 开销；可启停目标检测 Processor |
| 传感器健康 | LiDAR、iTOF、RGB 相机、麦克风/音频的在线状态、话题频率、发布者数量、最后更新时间 |
| ROS 2 图 | 实际运行节点列表、监控话题的类型、频率、数据年龄与状态（每 2 秒刷新） |

### 运动控制命令目录

命令定义在 `config/vendor_commands.json`，每条包含 `vendor_command`、分类、物理影响部位、交互方式、停止命令、是否需要确认、超时、验证状态与反馈话题。

| 分类 | 命令 |
|---|---|
| 方向运动（hold） | `MOVE_FORWARD` `MOVE_BACKWARD` `STRAFE_LEFT` `STRAFE_RIGHT` `ROTATE_LEFT` `ROTATE_RIGHT`，`MOTION_STOP` 为单次停止 |
| 身体动作 | `GET_UP` `BEND_WAIST` `STRAIGHTEN_WAIST`；上下车仅保留正式流程 `VISUAL_GET_ON_CAR`（上车）和 `/BusinessAppReq_topic` 的 `on-off-car/get-off`（下车） |
| 头部与手臂 | `HEAD_UP` `HEAD_RESET` `WAVE_HAND` `HAND_RESET` |
| 充电业务 | `RETURN_TO_CHARGE`（完整回充行为树）与 `OFF_CHARGER`（通过 `/BusinessAppReq_topic` 的 `off-charger` 指令执行正式下桩流程） |
| 其他业务动作（已停用） | `ENTER_PRESENTATION_MODE` `SELF_INTRODUCTION` `ME_TOO_HAPPY`；保留在目录中但 `dashboard_enabled=false` |
| 交互模式 | `SET_MODE_COMMAND-ONLY` `SET_MODE_VOICE-ONLY` `SET_MODE_MIXED` |
| 紧急操作 | `EMERGENCY_STOP` |
| 扩展命令（待验证） | `SAY_WOZAI` `MUTE` `RESET` `ENTER-FOLLOW` `REMOTE_CONTROL_MODE`；高风险且语义未确认的命令默认 `dashboard_enabled=false` |

- `hold`：按下发送一次命令，松开发送 `MOTION_STOP`；后端另有最长按住保护（默认 5 秒，方向命令超时值），超时自动停止。
- `trigger`：单次触发；`toggle`：模式切换；`workflow`：长任务，按 ROS 观测判断。
- 高风险动作（上下小车、回充、复位）需要页面二次确认。`GET_ON_CAR` 是 gait 62 分腿/卡脚调试动作；厂商 HTTP `GET_OFF_CAR` 是 gait 63 腿部解锁动作，二者均不作为 Dashboard 的正式上下车入口。
- 回充与下充电桩已启用并需要页面二次确认；其他未验证业务动作仍返回 403。
- 上/下小车、回充和下桩必须同时收到厂商业务成功事件与目标硬件状态，普通 ROS“运动后停止”不再被当作完成。
- 后端只接受命令目录中的 `command_id`，不提供任意厂商命令透传。

### 命令生命周期

```text
created → sending → vendor_accepted → observing
        → observed_running → observed_stopped → completed
```

异常阶段：`vendor_rejected`、`connection_failed`、`timed_out`、`state_mismatch`。

- `vendor_accepted` 仅表示厂商 HTTP 接口返回成功。
- `observed_running / observed_stopped` 来自 `/ControlBipedMotionFeedback_topic`、`/HardwareVehicleFeedback_topic`、`/HardwareKinematic_topic`、`/HardwareHeadPosition_topic` 的实际变化。
- 若厂商受理后没有 ROS 观测，阶段变为 `timed_out`，页面同时保留 HTTP 成功与 ROS 未观测两条信息。

### 接入的实际话题（16 个 subscriber）

| 话题 | 消息类型 | 说明 |
|---|---|---|
| `/HardwareKinematic_topic` | `hardware/msg/KinematicState` | 20 路关节角度与速度，约 97 Hz |
| `/HardwareImuStd_topic` | `sensor_msgs/msg/Imu` | IMU 姿态/角速度/加速度 |
| `/HardwareBat_topic` | `hardware/msg/Bat` | 电池电量、电压、充电状态 |
| `/HardwareHeadPosition_topic` | `hardware/msg/HeadPosition` | 头部 yaw/pitch 独立反馈 |
| `/HardwareVehicleFeedback_topic` | `hardware/msg/VehicleFeedback` | 小车速度、里程、故障 |
| `/ControlBipedMotionFeedback_topic` | `plan/msg/BipedMotionCmd` | 控制模式、步态、线速度、角速度、单次转向角、腿长设定 |
| `/HardwareSelfFall_topic` | `std_msgs/msg/UInt32` | 跌倒状态码（当前无发布者时显示无数据） |
| `/HardwareMotorCurrent_topic` | `std_msgs/msg/Int16MultiArray` | 关节电流反馈 |
| `/HardwareMotorLoad_topic` | `std_msgs/msg/Int16MultiArray` | 关节负载反馈 |
| `/HardwareMotorTemp_topic` | `std_msgs/msg/UInt8MultiArray` | 关节温度反馈 |
| `/HardwareMotorErr_topic` | `std_msgs/msg/UInt8MultiArray` | 关节错误码 |
| `/HardwareLidar_topic` | `sensor_msgs/msg/LaserScan` | 仅轻量摘要，不推送点云 |
| `/HardwareItof/camera_info` | `sensor_msgs/msg/CameraInfo` | iTOF 在线状态摘要 |
| `/camera/camera_info` | `sensor_msgs/msg/CameraInfo` | RGB 相机在线状态摘要 |
| `/camera/image_jpeg` | `sensor_msgs/msg/CompressedImage` | 视频预览，JPEG 直通，最多每 0.5 秒存最新一帧 |
| `/audio/kw_detect` | `std_msgs/msg/Int32MultiArray` | 关键词事件 |

> 注意：Dashboard **不再订阅** `/audio/capture_pcm`。麦克风电平与 PCM 流全部通过
> `audio_service`（`http://127.0.0.1:8091`）代理获取，以保证二次开发侧只有一个音频输入订阅。

### 20 路关节映射（来自实际消息与官方配置）

| 索引 | 名称 | 分组 |
|---:|---|---|
| 0–3 | 左肩俯仰、左肩侧摆、左肘侧摆、左夹爪（未使用） | 左臂 |
| 4–7 | 右肩俯仰、右肩侧摆、右肘侧摆、右夹爪（未使用） | 右臂 |
| 8–13 | 左髋偏航/侧摆/俯仰、左膝俯仰、左踝俯仰/侧摆 | 左腿 |
| 14–19 | 右髋偏航/侧摆/俯仰、右膝俯仰、右踝俯仰/侧摆 | 右腿 |

- 角度单位：度（°）；速度：MCU `int16` 原始值（官方未定义物理单位，不做换算）。
- 头部 yaw/pitch 为额外独立反馈（原始值 / 100 = 度），不计入 20 路。
- 完整名称、限位与单位见 `config/dashboard.json`。

## 机器人端目录结构

```text
/userdata/developer/dashboard/
├── src/           # FastAPI 后端、ROS 2 采集、厂商控制客户端、运动状态机、审计
├── static/        # HTML/CSS/JS，全部本地资源，无 CDN
├── config/        # dashboard.json、vendor_commands.json、requirements.txt
├── scripts/       # run.sh / stop.sh / status.sh
├── tests/         # 运动控制单元测试（stdlib unittest，无需 ROS）
├── logs/          # dashboard.log、console.log、motion_control.jsonl
├── data/          # dashboard.pid、ros_home
└── .venv/         # 项目虚拟环境（--system-site-packages 复用 ROS Python 包）
```

依赖：FastAPI、Uvicorn、websockets（纯 Python/aarch64 可装），ROS 2 相关包复用系统 `--system-site-packages`。新增运动控制模块只使用 Python 标准库。

## 使用方法

### 启动 / 停止 / 状态（在机器人上，SSH 登录后）

```bash
/userdata/developer/dashboard/scripts/run.sh      # 启动（防重复启动，记录 PID）
/userdata/developer/dashboard/scripts/status.sh    # 检查 PID 与 /api/health
/userdata/developer/dashboard/scripts/stop.sh      # 停止（TERM，必要时 KILL，清理 PID 文件）
```

`run.sh` 自动完成：加载 `/middleware/humble/setup.sh` → 设置厂商消息包路径（`/app/joyin`）与运行库路径 → 激活 `.venv` → 后台启动 → 健康检查（约 15 秒超时）。

服务未配置自动启动，机器人重启后需手动执行 `run.sh`。

### 远程访问（本地电脑）

```powershell
ssh -N -L 8080:127.0.0.1:8080 root@192.168.137.228
```

浏览器打开：`http://127.0.0.1:8080`，切换到“运动控制”标签页。

### HTTP / WebSocket 接口

| 路径 | 说明 |
|---|---|
| `GET /` | Dashboard 页面 |
| `GET /api/health` | 健康检查（ROS、厂商服务、急停、资源占用） |
| `GET /api/metadata` | 关节定义、分组、话题、单位、实际 ROS 节点与话题图 |
| `GET /api/state` | 当前完整状态（含 60 秒历史与 `motion_control`） |
| `GET /api/state/current` | 当前轻量状态（不含历史，供实时查询和运动安全预检查） |
| `GET /api/vehicle/status` | 小车规范化状态：反馈链路、车轮运动、里程、电流和故障；保留完整 MCU 原始值 |
| `GET /api/camera/frame` | 最新一帧相机 JPEG（无数据时返回 503） |
| `GET /api/vision/status` | 代理 `vision_service` 状态与 Processor 指标 |
| `GET /api/vision/detections/latest` | 最新标准化检测框及结果年龄 |
| `POST /api/vision/processors/object_detection/start` | 启动白名单目标检测 Processor |
| `POST /api/vision/processors/object_detection/stop` | 停止目标检测 Processor |
| `GET /api/audio/level` | 代理 `audio_service`：处理后 PCM 的 RMS 电平、峰值、流格式、数据帧与音量历史；`audio_service` 离线时返回 503 + `error_code=audio_service_unavailable` |
| `GET /api/audio/services` | 聚合 `audio_service` 健康、输入 Gateway、消费者/会话、TTS 与播放桥状态 |
| `GET /api/audio/agent/status` | Audio Agent 最近心跳、Gateway、前台实时模型、后台模型与音频桥状态 |
| `POST /api/audio/agent/heartbeat` | M1 Connector 每 2 秒上报一次经过裁剪的 Agent/Gateway 运行状态 |
| `GET /api/audio/volume` | 扬声器实际 DAC 音量、可用状态与配置范围 |
| `POST /api/audio/volume` | 设置听感音量（`{"percent":20}`）或声卡增益（`{"db_gain":-32}`），两者只能提供一个；写入后回读校验 |
| `WS /ws/audio/pcm` | Agent 音频入口（兼容保留）；内部代理 `audio_service` 的 20 ms / 640 B PCM 流，先发一条 JSON 格式说明，随后发送二进制帧；服务离线时先发 `audio_unavailable` 再关闭 |
| `GET /api/motion/capabilities` | 命令目录、分类、步态/控制模式标签、生命周期阶段 |
| `GET /api/motion/status` | 统一运动状态：厂商服务、急停、当前命令、机器人反馈 |
| `GET /api/motion/history?limit=100` | 命令历史、阶段事件、审计尾部记录 |
| `POST /api/motion/command` | 发送目录内命令 |
| `POST /api/motion/stop` | 高优先级发送 `MOTION_STOP` |
| `POST /api/motion/emergency-stop` | 紧急停止；优先 `/api/emergency_stop`，404/405 时回退 `/api/control` 的 `EMERGENCY_STOP` |
| `POST /api/motion/emergency-reset` | 解除急停；优先 `/api/reset_emergency`，404/405 时回退 `/api/control` 的 `RESET` |
| `GET /api/tts/status` | 语音服务状态（代理 `audio_service`：在线、模型、音色、当前阶段、队列、历史） |
| `POST /api/tts/speak` | 文字播报（支持 `voice`、`emotion`、`speech_rate`，并保留幂等、优先级和插队字段） |
| `POST /api/tts/stop` | 停止播报并清空语音服务队列 |
| `GET /api/tts/history?limit=20` | 最近播报任务 |
| `WS /ws/state` | 实时推送，约 10 Hz（含 `motion_control`，不含历史大载荷） |

`VehicleFeedback` 本身没有“机器人是否已在小车上”的字段。Dashboard 订阅
`/BusinessAppRpt_topic`，以厂商 `system_status.data.is_on_car` 作为在车状态权威来源；
反馈在线或车轮静止不会被误判为在车。充电桩状态继续以 `/HardwareBat_topic` 的
`on_charger` 与 `is_charging` 为硬件判据。

命令请求示例：

```json
{
  "command_id": "move_forward",
  "reason": "dashboard manual control",
  "client_request_id": "b0f6f3d1-6e2a-4a5b-9c0e-2f3f0e6a9d11",
  "duration_ms": 1000
}
```

- `client_request_id` 为幂等键，15 秒内重复提交同一键不会重复下发；同一键用于不同命令返回 409。
- `duration_ms` 仅用于方向 `hold` 命令，范围为 200ms 至 `limits.max_hold_seconds`；计时和自动停止由 Dashboard 持有，即使调用方断开也会继续执行。
- 写操作返回 `operation_id`、`execution_state` 和 `completion_confirmed`。`accepted=true` 只表示厂商已受理；只有 `completion_confirmed=true` 才能宣称动作已经完成。
- 页面仍可在松开时调用 `/api/motion/stop`；后台超时停止与显式停止都会写入审计。

## 语音播报（TTS）

播报能力已从 Dashboard 内部实现迁移为独立服务，Dashboard 只是本地客户端：

```text
浏览器 → Dashboard（127.0.0.1:8080）
       → audio_service（127.0.0.1:8091）
       → DashScope qwen-audio-3.1-tts-flash / longanhuan_v3.1
       → 48 kHz / 16-bit / 双声道 PCM → ROS /audio/speaker_pcm
       → 厂商 media_audio → 扬声器
```

- Dashboard 不再包含 eSpeak、`aplay`、ALSA 设备访问，也不再停止 `media_audio`；
  `src/tts_player.py` 只有 HTTP 客户端（标准库 `urllib`），原 `VendorAudioOps` 已从运行路径移除。
- 页面展示 `audio_service` 在线状态、模型、当前阶段、队列长度、错误与最近任务；音色和情绪选项由
  `audio_service` 动态下发，可用滑杆在 `0.5～2.0×` 之间调整语速。最近任务同时展示实际音色、情绪和语速。
  音色共 10 个中文常用音色；离线时下拉框回退到 `static/index.html` 内置的同一份 10 项清单。
- 阶段与 `audio_service` 一致：`queued / synthesizing / decoding / publishing /
  waiting_playback / succeeded / failed / cancelled`。

## Audio Service 栏目

导航中的 **Audio Service** 页面集中呈现当前音频链路：

- 音频输入 Gateway、20 ms PCM 分发与 5 秒环形缓存；
- 当前 WebSocket/Processor 消费者及队列、丢帧和延迟；
- DashScope TTS Provider、串行播放队列和 `/audio/speaker_pcm` 播放桥；
- ASR、TSE 的实际会话与 Provider 状态。当前仅注册 Mock Processor 时明确标记为 `Mock`，不会显示为正式在线服务；
- `audio_service` 离线时保留完整栏目结构并显示结构化错误。

页面激活时每 2 秒轮询 Dashboard 的 `/api/audio/services`，切换到其他栏目后停止轮询。
- M1 Connector 会把 Audio Agent 状态主动上报给 Dashboard；超过 6 秒未收到心跳时显示“心跳过期”，无需从机器人反向访问本机 Gateway。
- `audio_service` 离线或超时时，Dashboard 返回 `503` 与结构化错误：

  ```json
  {
    "accepted": false,
    "error": {
      "code": "service_unavailable",
      "message": "audio_service 不可用，请检查 /userdata/audio_service/scripts/run.sh",
      "detail": {"transport": "[Errno 111] Connection refused", "backend": "http://127.0.0.1:8091"}
    }
  }
  ```

- 启动顺序：先 `/userdata/audio_service/scripts/run.sh`，再启动 Dashboard；两者都是手动启动，
  机器人重启后需要各自执行。`audio_service` 未启动时 Dashboard 其余功能不受影响。

## 架构与数据流

- **ROS 采集线程**：独立线程运行 `SingleThreadedExecutor`，只调用 `create_subscription`；高频话题（关节约 97 Hz、IMU 约 100 Hz）在服务端降采样至 10 Hz 写入状态。
- **状态存储**：内存环形缓冲保留最近 60 秒关节/IMU/电池历史；`time.monotonic()` 记录数据年龄，超过阈值显示 stale/offline。
- **厂商控制客户端**（`src/vendor_control.py`）：固定访问 `127.0.0.1:12321`，连接/读取分开超时，逐请求 UUID，统一转换连接失败、超时、HTTP 错误与 JSON 解析错误，并记录延迟。
- **运动状态机**（`src/motion_state.py`）：以命令提交前反馈为基线，使用轮速、控制模式、步态、头部角度与关节变化判断 `observed_running/observed_stopped`；厂商 HTTP 成功与 ROS 运动完成是两个独立状态。
- **控制编排**（`src/motion_control.py`）：白名单、急停/跌倒门控、忙碌互斥、幂等缓存、按住看门狗、健康探测（约 2 秒）与运动监控（10 Hz）。
- **审计**（`src/control_audit.py`）：每个请求、厂商响应、阶段变化、急停/复位与自动停止写入 `logs/motion_control.jsonl`，4 MiB 轮转 × 3 份。
- **Web 服务**：Uvicorn 仅监听 `127.0.0.1:8080`；WebSocket 以 10 Hz 推送快照，浏览器端断线每 2 秒重连。
- **系统监控**：从 `/proc`、`/sys`、`statvfs` 采集 CPU、内存、磁盘、温度与运行时间，无第三方依赖。
- **BPU 监控**：每秒从 `/sys/devices/system/bpu` 读取利用率、当前/最高频率和占用进程数；不启动外部监控进程，指标不可用时页面明确降级显示。

## 安全边界

- 浏览器只访问 Dashboard，不直接访问厂商 `12321` 端口；后端固定回环地址，命令只能来自 `vendor_commands.json` 白名单。
- `MOTION_STOP` 走独立高优先级调用路径，不受当前命令忙碌状态限制；急停与恢复另有独立路径。
- 方向命令按住执行、松开停止；后端看门狗在超时后自动发送 `MOTION_STOP`，避免浏览器断连导致持续运动。
- 急停激活时只允许停止/解除；跌倒状态非 0 时只允许 `GET_UP` 与停止。
- 高风险命令（上下小车、回充、下充电桩、复位）在页面二次确认；`ENTER-FOLLOW`、`REMOTE_CONTROL_MODE`、`RESET` 默认不在 Dashboard 启用。
- 服务只监听 `127.0.0.1:8080`，不暴露到 Wi-Fi 网络；远程访问必须经 SSH 端口转发。
- 不停止或修改 `control_pm` 等默认控制节点，不修改根文件系统与系统 Python。

## 无数据说明（非故障）

- **跌倒反馈**：`/HardwareSelfFall_topic` 当前可能无发布消息，页面 `fall_state` 显示“无数据”；此时不会启用跌倒门控，但急停与停止始终可用。
- **LiDAR**：话题存在发布者但当前固件可能无扫描消息（`hardware.Lidar.status=3` 不保证实时扫描），页面显示"无数据"。本服务不调用 `/lidar_enable`。
- **音频**：厂商 `media_audio` 是唯一硬件采集者，读取 NationalChip USB 六通道阵列并完成 ANC/AEC 处理。处理后的 16 kHz、16-bit、单声道 PCM 通过 `/audio/capture_pcm` 分发，**由 `audio_service` 单一订阅**并与厂商 `business_audio_dialogue_plugin` 并存：
  - Dashboard 不再订阅该 ROS 话题，改为代理 `audio_service`：`GET /api/audio/level`（10 Hz 轮询）与 `WS /ws/audio/input`（20 ms / 640 B 帧）。
  - Dashboard 对外接口保持兼容：`GET /api/audio/level` 与 `WS /ws/audio/pcm`；旧 Agent 客户端无需改动。
  - `audio_service` 离线时 `/api/audio/level` 返回 503（`error_code=audio_service_unavailable`），WebSocket 先发 `audio_unavailable` 再 close 1013；代理自动指数退避重连（0.5→5 s），恢复后无需重启 Dashboard。
  - Agent 也可直接连接 `ws://127.0.0.1:8091/ws/audio/input`，支持 `pre_roll_ms` 与 `delivery=latest|continuous`。
  - `scripts/ensure_media_audio.sh` 在 Dashboard 启动时兜底拉起厂商采集进程，并把 ROS 日志写入 `/userdata`，适配只读根文件系统。
- **电机错误话题**：更新不连续时按实际年龄显示 stale，不伪造在线状态。
- **厂商受理但无 ROS 观测**：部分动作（如 `HEAD_UP`/`HEAD_RESET`）在 2026-11-09 的实机验证中返回 HTTP 200，但头部反馈没有明显变化。Dashboard 会显示 `timed_out`，这是真实观测结果，不代表 HTTP 调用失败。

## 扬声器音量

- `speaker_volume.controls` 使用厂商 `/app/joyin/config/media_audio.json` 指定的 `DAC`。当前 ES8326 的 `HPVol SPKVol` 路由使用 `HPL`，`DAC` 与其联动；`SPKL / SPKR` 虽存在，却不控制当前扬声器通路。之前仅修改这两个控件会造成页面显示静音、真实播放音量仍很高。
- 页面先读取实际值，再启用滑块；拖动预览、松开提交，连续提交按顺序处理并保留最新值。每 2 秒刷新实际音量，写入后允许 ALSA 量化带来的 1% 误差，回读失败或明显不一致会报错。
- 百分比采用 ALSA `amixer -M` 听感刻度，读取与写入使用同一映射。旧硬件寄存器刻度的 20% 对应 −75 dB，几乎听不见；新听感刻度 20% 对应当前声卡 −32 dB。
- 页面也可直接输入输出增益。真机 `DAC Playback Volume` 的范围为 −95.5 至 +8 dB，步进 0.5 dB；接口返回 `db_gain`、`db_min`、`db_max`、`db_step` 和 `scale=perceptual`。这是声卡增益，现场声压级还取决于音源、功放、扬声器和距离。
- 2026-11-10 经用户同意切换到听感刻度，在新刻度 20% 下播放约 0.34 秒的 `hi`，现场反馈可听到、声音较轻。后续新增 dB 控制只做 −40 dB、−32 dB 的无声读写验证，保持在 −32 dB（20%）；本轮测试未超过 20%。

## 资源占用（参考值）

| 指标 | 数值 |
|---|---|
| CPU | 约 1.0–1.1 个单核核心 |
| 内存（RSS） | 约 105 MiB |
| 日志 | `dashboard.log` 单文件 2 MiB × 3；`motion_control.jsonl` 4 MiB × 3 |

## 测试

```bash
cd /userdata/developer/dashboard
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_volume_ui.js  # 前端异步请求回归测试，无需机器人
```

测试覆盖：命令目录完整性、厂商 HTTP 成功/HTTP 错误/无效 JSON/连接失败/读超时、急停与恢复路由、命令生命周期、无观测超时、跌倒中止、幂等重放、忙碌互斥、禁用命令、急停回退与审计写入；TTS 客户端在线状态聚合、离线降级、缓存、幂等重放、文本校验、后端错误映射、停止与历史、离线音色回退清单（10 项）、密钥不泄漏。

## 本地开发目录

本地工作区：`d:\projects\m1-robot\dashboard\`，与机器人端目录一一对应。修改后用 `scp` 同步对应文件到 `/userdata/developer/dashboard/`，再执行 `stop.sh` + `run.sh` 生效。
