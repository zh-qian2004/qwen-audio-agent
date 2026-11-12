# Zeroth M1 机器人集成

本目录是 `feat/m1-robot` 分支的完整 M1 集成入口。它同时包含语音 Agent
连接器、机器人音频网关、状态与运动控制 Dashboard、视觉能力网关、部署脚本以及
实机接口调查文档。使用者只需克隆这一个分支，不需要再从其他仓库拼装代码。

## 组件

| 组件 | 目录 | 作用 |
| --- | --- | --- |
| M1 Agent Connector | 当前目录 | 在 `audio_service`、Dashboard 与 qwen-audio-agent Gateway 之间转发音频、事件、状态和工具调用 |
| audio_service | [`audio_service/`](audio_service/) | 麦克风 PCM 输入、实时播放、TTS、播放仲裁及音频健康状态 |
| Dashboard | [`dashboard/`](dashboard/) | ROS 状态、运动控制、传感器、音视频与 Agent 状态面板 |
| vision_service | [`vision_service/`](vision_service/) | 视觉 Processor、检测结果归一化、状态与 WebSocket 输出 |
| 部署文件 | [`deploy/`](deploy/) | 机器人启动脚本与厂商服务补丁 |
| 调查文档 | [`docs/`](docs/) | 控制接口、动作验证、厂商控制台 API 与集成记录 |

运行架构：

```text
机器人麦克风 / media_audio
          │
          ▼
audio_service /ws/audio/input ──PCM16/16k──► M1 Connector ──GCP──► Gateway
audio_service /ws/audio/output ◄──PCM16/24k── M1 Connector ◄─audio.delta─ Gateway
Dashboard HTTP API             ◄──client tools── M1 Connector ◄────────── Gateway
Dashboard HTTP API             ◄────M1 MCP tools──── 后台 Agent ◄──────── Gateway
Dashboard Agent heartbeat      ◄──status/2s───── M1 Connector ──health──► Gateway
vision_service                 ◄────────Dashboard / Agent tools──────────►
```

连接器不直接访问 ALSA、声卡或 ROS。麦克风采集、分帧、扬声器转换、播放仲裁和
`/audio/speaker_pcm` 发布均由 `audio_service` 负责；运动控制和状态门控由 Dashboard
负责；视觉 Processor 生命周期由 `vision_service` 负责。

## 获取代码

```bash
git clone --branch feat/m1-robot --single-branch \
  git@github.com:zh-qian2004/qwen-audio-agent.git
cd qwen-audio-agent
npm ci
```

Node.js 版本须满足仓库根目录要求。模型 API Key、SSH 私钥和机器人密码不得写入仓库；
请使用 Gateway 的环境变量、本机 SSH agent 或只读权限的本地配置文件。

## 前置条件

- 机器人上的 `audio_service` 已监听 `127.0.0.1:8091`，独立音频输入与声纹服务已监听
  `127.0.0.1:8093`。
- Dashboard 已监听 `127.0.0.1:8080`。
- 需要视觉工具时，`vision_service` 已按其 README 启动。
- Gateway 已配置可用的 Realtime Voice Frontend。
- 开发机可通过 SSH 公钥认证访问机器人。

三个机器人服务各自保持独立项目边界，部署时建议使用：

```text
/userdata/audio_service
/userdata/developer/dashboard
/userdata/developer/vision_service
```

详细依赖、配置和实机启动方式分别见各组件 README。根文件系统为只读时，运行状态、
日志、虚拟环境和模型缓存都必须写入 `/userdata`。

## 一键启动 Agent

默认配置位于 `examples/m1-robot/m1-robot.config.json`，连接当前热点网络中的 M1，
建立 `8091`、`8093`、`8080` 的 SSH 转发，并启动本地 Gateway 与 Connector：

```powershell
npm run start:m1-robot
```

启动器会：

1. 检查 `audio_service` 和 Dashboard，需要时建立 SSH 隧道。
2. 构建并启动本地 Gateway，或复用 profile 为 `m1-robot` 的现有 Gateway。
3. 等待 Gateway 就绪后启动 M1 Connector。
4. 把 M1 MCP 注入后台 Agent，使开放任务能够持续调用机器人原子工具。
5. 每 2 秒向 Dashboard 上报 Gateway、前台模型、后台 Agent 和音频桥状态。
6. 在托管进程退出或收到 `Ctrl+C` 时清理本次创建的进程。

机器地址、SSH key、端口或模型环境不同的使用者，应复制本地配置：

```powershell
Copy-Item examples/m1-robot/m1-robot.config.json `
  examples/m1-robot/m1-robot.config.local.json
npm run start:m1-robot -- --config examples/m1-robot/m1-robot.config.local.json
```

`m1-robot.config.local.json` 已被 Git 忽略。只检查配置而不启动：

```powershell
npm run start:m1-robot -- --dry-run
```

Web 资源已经构建时可以跳过构建：

```powershell
npm run start:m1-robot -- --no-build
```

### 分步启动

```powershell
$env:QWEN_AUDIO_FRONTEND_PROFILE="examples/m1-robot/profile/frontend-profile.json"
npm run gateway
```

另开终端，按 `.env.example` 设置连接参数后运行：

```powershell
npm run example:m1-robot
```

如果 Gateway 运行在远程设备，而机器人服务只监听回环地址，可以使用 SSH 或
Tailscale 隧道转发 `8091`、`8093` 和 `8080`。长期部署可把 Connector 与 Gateway 部署在
机器人 `/userdata`，直接访问本机服务并仅向云端模型建立出站连接。

## Agent 工具

- `m1_get_state`
- `m1_get_motion_status`
- `m1_get_vision_status`
- `m1_get_detections`
- `m1_get_speaker`
- `m1_manage_speaker`
- `m1_list_actions`
- `m1_execute_action`
- `m1_move`
- `m1_stop`
- `m1_emergency_stop`
- `m1_set_volume`

后台 Agent 还会获得 `m1_wait_for_motion`。它按 `operation_id` 等待动作进入终态，防止
后台在物理动作尚未完成时提前进行下一次观察或控制。这个等待工具不暴露给实时前台，
避免占用语音模型的实时响应通道。

动作目录由 Connector 启动时从 Dashboard 发现。`m1_execute_action` 只暴露已启用、
非 hold、无需额外确认的动作；方向运动统一由 `m1_move` 提交，计时和自动停止由
Dashboard 持有。

Connector 常驻订阅 `/ws/speakers/events`，声纹结果和转写存入后台逐轮记录，不向实时
模型注入额外文本。`m1_get_speaker` 支持 current、previous、turn_id 和 search 查询，
返回 verified、unknown、pending 或 unavailable；后台 Agent 读取同一份记录。
缺少证据时不继承上一位身份，历史发言查询不以当前提问者身份代替。记录按 session
隔离，最多保存 200 轮，位于 stateDirectory 下的 `m1-speaker-turns.json`。

声纹管理收敛为 `m1_manage_speaker`。注册开始后，audio_service 从多个合格片段累计有效
人声并自动完成，不再把 finish/status 等底层接口暴露给模型。注册数据由机器人
audio_service 管理，Harness 不接收或保存声纹向量和原始注册 PCM；阈值、过滤原因和
推理时延等诊断信息由 Dashboard/服务状态接口查看。

运动命令返回 `operation_id`、`execution_state` 和 `completion_confirmed`。厂商 HTTP
受理不等于物理动作已经完成；`completion_confirmed=false` 时必须通过
`m1_get_motion_status` 继续观察 ROS 反馈。

## 开放任务

“找人”“移动直到看见人”“巡视并报告异常”等请求由实时前台通过现有
`spawn_thinking` 交给后台 Agent。后台不调用预定义的 `find_person` 工作流，而是使用
通用闭环：

```text
观察状态/视觉 → 选择一个有限动作 → 等待该 operation_id 结束 → 再观察 → 重规划
```

后台 MCP 与前台 Connector 复用同一套 `M1RobotTools` 和 Dashboard 安全门控。每次移动
仍受 Dashboard 的有限时长和自动停止保护；后台只有在视觉或状态证据确认目标后才能
报告成功。用户可通过现有任务工具查询、调整或取消后台任务，取消物理任务时前台同时
调用 `m1_stop`。

一键启动器只会把 MCP 注入它新启动的 Gateway。若复用已经运行的 Gateway，需要先
重启该 Gateway，才能让新的后台 Session 获得 M1 工具。分步启动时可通过
`QWEN_AUDIO_BACKEND_MCP_SERVERS` 传入同等的 stdio MCP 描述。

## 音频行为

`M1_HALF_DUPLEX=0` 时启用全双工。Connector 同时连接：

- `/ws/audio/input`：麦克风 PCM 输入；
- `/ws/audio/output`：Agent 实时音频输出；
- `/ws/speakers/events`：说话人识别结果，仅在 Connector 内缓存供工具查询；
- `/ws/vendor/events`：厂商事件与安全提示；
- 音频取消接口：打断时停止旧播放并释放仲裁租约。

`playback.started`、`playback.ended` 和 `playback.cancelled` 均以 `audio_service`
真实播放事件为准。普通厂商提示默认只记日志，明确的 interrupt 安全事件才会进入
Agent；Gateway 不可用时恢复原厂安全提示。

Connector 的时延遥测写入 qwen-audio-agent 状态目录中的 `logs/m1-connector.log`，
包括采集、跨设备传输、模型首包、输出就绪和真实播放回执等阶段。

## 测试

Agent Connector：

```bash
npm run test:m1-robot
npm run lint:m1-robot
```

机器人服务使用 Python 3.10+，在本目录执行：

```bash
python -m pytest audio_service/tests
python -m pytest dashboard/tests
python -m pytest vision_service/tests
```

实机测试前应先确认机器人没有处于人员靠近、边缘、楼梯、运输或维护状态。动作是否
完成必须以 Dashboard 的 ROS 观测为准。

## 配置与数据安全

仓库只保存配置模板，不保存以下内容：

- DashScope 或后台模型 API Key；
- SSH 私钥、机器人密码和访问令牌；
- `m1-robot.config.local.json`；
- 日志、录音、缓存、备份和机器人运行数据；
- Python 虚拟环境、`node_modules` 和构建输出。

发现密钥被误提交时，应先吊销密钥，再从 Git 历史中清除，不能只做一次普通删除提交。
