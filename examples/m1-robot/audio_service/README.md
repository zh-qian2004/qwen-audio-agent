# audio_service — M1 机器人音频与语音能力网关

当前版本：`1.4.0`。实时语音链路包含播放感知型基础回声抑制：输出服务通过
本机 UDP 向独立麦克风服务发送 16 kHz 参考帧，麦克风服务仅衰减与播放高度
相关的回声帧；连续近端人声会在 2～3 帧内穿透，以保留全双工打断能力。

`audio_service` 是独立于 Dashboard 和 Agent 的**二次开发音频与语音能力 Gateway**。
TTS 是当前第一个落地模块，但不是本项目的全部定位；后续 ASR、TSE、VAD、说话人能力、
音频流分发和输入输出仲裁都在同一服务边界内扩展。

业务侧（Dashboard / Agent / 其他进程）只调用本服务的 HTTP 或 WebSocket API。
本服务统一封装厂商 ROS 音频接口、实时音频流、算法 Provider、音频格式、任务状态、
资源仲裁和可观测性，使上层业务不直接依赖声卡、ROS 音频消息或具体供应商 SDK。

当前已实现的**输入**链路（v1.2 音频输入 Gateway）：

```text
厂商 media_audio → /audio/capture_pcm (pcm_16000_16_1, 2048 B/包, 约 15.6 Hz)
  → audio_service 单一订阅（RELIABLE / KeepLast 100）
  → 格式校验 + 时间戳（本机单调时钟 + ROS 源时间戳）
  → 20 ms 重分帧（320 samples / 640 bytes）
  → 5 秒环形缓冲（250 帧，支持 pre-roll 回放）
  → 无阻塞 Audio Bus（latest / drop_oldest / fail_on_gap）
  → Dashboard（电平 + /ws/audio/pcm 代理）/ WebSocket / VAD / ASR / TSE
```

当前已实现的 TTS 链路如下：

```text
Dashboard / Agent / 其他业务
  → audio_service 本地 HTTP API (127.0.0.1:8091)
  → DashScope qwen-audio-3.1-tts-flash（voice=longanhuan_v3.1）
  → MP3 解码与格式转换（ffmpeg）
  → 48 kHz / 16-bit / 双声道 raw PCM
  → ROS 2 /audio/speaker_pcm（sensor_msgs/msg/Image, RELIABLE, KeepLast 10）
  → 厂商 media_audio
  → 机器人扬声器
```

`media_audio` 始终是麦克风采集和扬声器播放的唯一硬件进程。本服务：

- 不调用 `aplay`、不打开/占用 `/dev/snd`；
- 不停止、不重启、不接管 `media_audio`；
- 播放期间 `media_audio` PID 不变，`/audio/capture_pcm` 持续发布（实测约 15.6 Hz）；
- 厂商插播通路 `/audio/speaker_pcm_interrupt` 是 `std_msgs/String` 的**预置音频文件名**接口，
  无法承载任意 TTS，未启用（实机证据见第 6 节）。

## 0. Gateway 定位与能力边界

总体架构：

```text
                         ┌─→ 厂商语音插件
USB 麦克风 → media_audio ┤
                         └─→ audio_service
                                ├─→ 电平监控 / WebSocket 音频流
                                ├─→ TSE / VAD / ASR / 说话人能力
                                └─→ Agent / Dashboard / 业务应用

Agent / Dashboard / 业务应用
  → audio_service
  → TTS / 提示音 / 其他音频生成
  → 播放队列与仲裁
  → /audio/speaker_pcm
  → media_audio → 扬声器
```

### 厂商媒体层

`media_audio` 负责 ALSA 和 `/dev/snd`、六通道麦克风阵列、AEC/ANC、扬声器设备和底层
ROS 音频接口。`audio_service` 复用这层能力，不成为第二个硬件采集或播放进程。

### audio_service

本服务是二次开发侧唯一的音频入口、出口和语音算法承载平台，长期负责：

- 单一订阅 `/audio/capture_pcm`，完成缓存、时间戳、格式标准化和多路分发；
- 通过 WebSocket/API 向 Dashboard、Agent和其他服务提供音频输入；
- 承载 TTS、ASR、TSE、VAD、说话人注册/识别/验证等 Provider 或 Processor；
- 统一处理模型配置、云端密钥、任务状态、错误结构、超时和重试；
- 统一处理音频转换、播放队列、优先级、打断和停止；
- 统一提供健康检查、日志、指标、任务历史和资源仲裁。

TSE 在本项目中指 Target Speaker Extraction（目标说话人提取）。目标说话人档案、注册音频、
提取会话及输出流应保存在本项目的数据与 API 边界内，不由各业务项目分别实现。

### 上层应用

Dashboard、Agent和业务应用负责业务决策与交互呈现。它们通过 `audio_service` 使用语音能力，
不需要自行接入 DashScope/ASR/TSE SDK，也不直接访问 ALSA 或厂商 ROS 音频话题。

Agent 本身不属于本项目。`audio_service` 向 Agent 提供音频流、ASR/TSE 结果、TTS和播放控制。

### 当前实现范围

已完成：

- `/audio/capture_pcm` 的统一输入订阅、格式校验、20 ms 重分帧、5 秒环形缓冲；
- 进程内 Audio Bus（`latest` / `drop_oldest` / `fail_on_gap`）与逐消费者指标；
- `GET /api/audio/status`、`GET /api/audio/level`、`WS /ws/audio/input`；
- 输入电平（RMS/Peak dBFS，约 10 Hz）与健康检查中的输入段落；
- Processor / Session 稳定接口 + Mock ASR / Mock TSE 测试消费者（不进入生产 API）；
- 常驻说话人识别 Processor：CAM++ embedding、档案注册与识别事件；模型及数据保存在机器人上；
- Dashboard 迁移：电平与 PCM WebSocket 全部改为代理 audio_service，不再订阅 ROS 话题；
- TTS（合成、转换、队列、ROS 播放、Dashboard 代理）。

尚未实现（正式后续模块）：

- VAD 与流式 ASR（接口已就绪，等真实模型接入）；
- TSE、目标说话人档案和提取管线；
- 进一步的说话人验证和跨设备档案同步；
- 用户说话打断播报（barge-in）；
- 厂商语音与二次开发语音的完整仲裁。

计划使用统一命名空间扩展 API：

```text
/api/audio/*       音频格式、电平、路由和设备状态
/ws/audio/*        实时音频输入、处理结果和事件流
/api/tts/*         语音合成与播放任务
/api/asr/*         识别任务、流式会话和结果
/api/tse/*         目标说话人档案、提取任务和状态
/api/speakers/*    说话人注册、识别与管理
```

说话人识别由独立麦克风进程的 `input_main.py` 注册为常驻 Processor，与 Agent 输入流并行消费 Audio Bus。模型依赖见 `config/speaker-requirements.txt`，配置见 `config/speaker.json`；ONNX 模型文件和个人声纹档案不入库。接口包括 `GET /api/speakers/status`、`GET /api/speakers`、`POST /api/speakers/enroll/start|finish|cancel`、`DELETE /api/speakers/{speaker_id}` 和 `WS /ws/speakers/events`。`scripts/benchmark_speaker.py` 可测量模型加载及推理时延。

所有模块共用统一任务 ID、状态阶段、错误结构、幂等语义、密钥管理和健康检查。

## 1. 目录结构

```text
/userdata/audio_service/            # 机器人端（实际部署目录）
├── config/
│   ├── service.json                # 服务配置（不含任何密钥）
│   ├── requirements.txt            # Python 依赖
│   └── runtime.env                 # DASHSCOPE_API_KEY，权限 600
├── src/
│   ├── main.py                     # FastAPI 入口 + 日志轮转 + CA 配置 + 生命周期
│   ├── audio_gateway.py            # 输入 Gateway 编排（重分帧/缓冲/电平/分发）
│   ├── ros_runtime.py              # 共享 ROS Context/Node/MultiThreadedExecutor
│   ├── input/
│   │   ├── audio_frame.py          # 不可变 AudioFrame + 编码解析
│   │   ├── ros_capture.py          # /audio/capture_pcm 订阅、校验、入口队列
│   │   ├── rechunker.py            # 64 ms 输入 → 20 ms 输出（样本累积器）
│   │   ├── ring_buffer.py          # 5 秒 / 250 帧环形缓冲
│   │   ├── audio_bus.py            # 进程内发布订阅（三种投递策略）
│   │   └── audio_level.py          # RMS/Peak dBFS，约 10 Hz 采样
│   ├── processors/
│   │   ├── base.py                 # AudioProcessor Protocol + BaseProcessor
│   │   ├── mock_asr.py             # 测试消费者（不做真实推理）
│   │   └── mock_tse.py             # 测试消费者（不做真实推理）
│   ├── sessions/
│   │   ├── audio_session.py        # 会话状态机
│   │   └── session_manager.py      # 会话注册/启动/缺口处理/关闭
│   ├── api/
│   │   └── audio_routes.py         # /api/audio/* 与 /ws/audio/input
│   ├── tts_service.py              # 编排：合成→解码→发布→播放等待，串行队列
│   ├── dashscope_provider.py       # DashScope 封装（错误归并 / 脱敏 / 统计）
│   ├── audio_converter.py          # MP3 → 48k/16bit/2ch PCM，格式校验与分包
│   ├── ros_pcm_publisher.py        # /audio/speaker_pcm 发布、节奏控制、状态采集
│   ├── queue_manager.py            # 容量有限、支持优先级的串行队列
│   └── state.py                    # 任务状态机、历史、幂等缓存、密钥脱敏
├── scripts/{run.sh,stop.sh,status.sh}
├── tests/                          # mock Provider / mock ROS publisher
├── logs/                           # audio_service.log（5 MB × 3 轮转）、console.log
├── data/                           # PID 文件、ROS_HOME
└── .venv/                          # Python 虚拟环境
```

本地工作区对应目录：`D:\projects\m1-robot\audio_service`。

## 2. 部署与启动

```bash
# 1) 依赖（机器人端已建好 venv）
python3 -m venv /userdata/audio_service/.venv
LD_LIBRARY_PATH=/middleware/pub/lib /userdata/audio_service/.venv/bin/pip install \
  -r /userdata/audio_service/config/requirements.txt

# 2) 运行密钥（权限 600，不进版本库、不进日志）
cat > /userdata/audio_service/config/runtime.env <<'EOF'
DASHSCOPE_API_KEY=sk-xxxxxxxx
EOF
chmod 600 /userdata/audio_service/config/runtime.env

# 3) 启停与状态
/userdata/audio_service/scripts/run.sh      # 幂等：已在运行则直接返回 0
/userdata/audio_service/scripts/status.sh
/userdata/audio_service/scripts/stop.sh     # 只停止本项目进程（校验 cmdline）
```

`run.sh` 启动前依次检查：venv、`config/service.json`、`ffmpeg`、`websockets`（uvicorn 的
WebSocket 实现依赖，缺失时 `/ws/audio/input` 会直接 404）、`runtime.env` 与
`DASHSCOPE_API_KEY`、`127.0.0.1:8091` 端口占用；随后导出 ROS/厂商库环境、
`SSL_CERT_FILE`（见第 8 节），后台启动并轮询 `/api/health` 确认就绪。

> ROS 输入与 TTS 输出共享同一个 `RosRuntime`：进程内只 `rclpy.init` 一次，
> 单个节点 `audio_service` 同时持有 `/audio/capture_pcm` 订阅与 `/audio/speaker_pcm`
> 发布器，由 `MultiThreadedExecutor`（默认 2 线程）驱动；FastAPI 关闭时按
> 「TTS 服务 → 输入 Gateway → ROS Runtime」顺序销毁。

## 3. 配置（config/service.json）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `service.host/port` | `127.0.0.1` / `8091` | 只允许回环地址，非回环会被强制改回 |
| `tts.model` | `qwen-audio-3.1-tts-flash` | DashScope 模型 |
| `tts.voice` | `longanhuan_v3.1` | 音色 |
| `tts.voices` | 10 个中文常用音色 | `/api/tts/speak` 允许选择的音色及展示名称（龙安欢、龙安灵心、龙安风悦、许南川、于小云、乔小娇、安明远、温怀清、白安然、许言初） |
| `tts.emotions` | 自然、兴奋、严肃等 | 允许的 Qwen-Audio-TTS 情绪标签 |
| `tts.speech_rate` | `0.5～2.0`，默认 `1.0` | SDK `speech_rate` 范围、步长与默认值 |
| `tts.base_websocket_api_url` | 自定义 workspace endpoint | wss 推理地址 |
| `tts.api_key_env` | `DASHSCOPE_API_KEY` | 密钥环境变量名 |
| `tts.max_text_length` | `300` | 文本上限 |
| `tts.synthesis_timeout_seconds` | `30` | 云端合成超时 |
| `tts.decode_timeout_seconds` | `30` | ffmpeg 解码超时 |
| `tts.idempotency_ttl_seconds` | `600` | `client_request_id` 幂等窗口 |
| `queue.capacity` | `8` | 串行播放队列容量，满则 429 |
| `queue.publish_timeout_seconds` | `180` | 单任务发布总超时 |
| `queue.playback_drain_seconds` | `0.4` | 最后一包后的厂商缓冲尾部等待 |
| `audio.chunk_ms` | `60` | 分包时长，48000×0.06×2×2 = 11520 B |
| `audio.publish_lead_chunks` | `2` | 预发 2 包（约 120 ms）抗抖动 |
| `audio.clean_child_env` | `true` | ffmpeg 子进程清空 `LD_LIBRARY_PATH`（见第 8 节） |
| `ros.speaker_topic` | `/audio/speaker_pcm` | 扬声器输入 |
| `ros.encoding` | `pcm_48000_16_2` | 固定 |
| `ros.qos_depth` | `10` | RELIABLE + KeepLast(10) |
| `ros.runtime_node_name` | `audio_service` | 输入订阅与输出发布共用的 ROS 节点名 |
| `ros.executor_threads` | `2` | 共享 `MultiThreadedExecutor` 线程数 |

输入侧（`input` 段，承载 `/audio/capture_pcm`）：

| 键 | 默认 | 校验 |
| --- | --- | --- |
| `input.topic` | `/audio/capture_pcm` | — |
| `input.encoding` | `pcm_16000_16_1` | 必须与消息 `encoding` 一致，否则计为非法包 |
| `input.sample_rate` / `channels` / `sample_width` | `16000` / `1` / `2` | 必须为正整数；`sample_width` 固定 2（S16_LE） |
| `input.input_qos_depth` | `100` | RELIABLE + KeepLast(100)，与厂商发布者兼容 |
| `input.frame_ms` | `20` | 必须能整除为整数采样（`sample_rate × frame_ms % 1000 == 0`） |
| `input.ring_buffer_ms` | `5000` | 1..30000 且不小于一帧 |
| `input.ingress_capacity` | `32` | 1..4096，入口队列满时丢最旧并计数 `input_dropped` |
| `input.level_update_hz` | `10` | 1..50，电平计算频率 |
| `input.level_history_seconds` | `60` | 电平历史窗口 |
| `input.consumer_capacity` | `100` | 默认消费者队列容量（帧） |
| `input.stream_capacity` | `50` | WebSocket `delivery=continuous` 的队列容量（约 1 s） |
| `input.pre_roll_max_ms` | `5000` | 被 `ring_buffer_ms` 截断；WebSocket `pre_roll_ms` 上限 |
| `input.stale_after_seconds` | `1.0` | 超过该时长无帧即判定流过期（电平不再 `active`） |
| `input.source_label` | 厂商处理后麦克风流 | 仅用于 Dashboard/API 展示 |
| `input.mock_sessions` | `[]` | 调试用自启消费者，如 `[{"type":"mock_asr","pre_roll_ms":300}]`；生产留空 |

启动时任一取值非法会直接 `ValueError` 退出（`run.sh` 轮询健康检查失败会报错并停止进程）。

## 4. API

服务只监听 `http://127.0.0.1:8091`。

### `GET /api/health`

```json
{
  "status": "ok",
  "version": "1.2.1",
  "uptime_seconds": 128.4,
  "provider": {"available": true, "reason": null, "model": "qwen-audio-3.1-tts-flash", "voice": "longanhuan_v3.1", "last_error": null},
  "ros": {"available": true, "topic": "/audio/speaker_pcm", "encoding": "pcm_48000_16_2", "subscribers": 1, "capture_rate_hz": 15.62, "last_error": null},
  "queue": {"pending": 0, "capacity": 8},
  "media_audio": {"process": "media_audio", "running": true, "pid": 15606, "pids": [15606]},
  "last_error": null,
  "input": {
    "available": true, "online": true, "topic": "/audio/capture_pcm",
    "publishers": 1, "input_rate_hz": 15.62, "last_frame_age_ms": 18.4,
    "output_rate_hz": 50.4, "rechunker_buffered_bytes": 0,
    "ring": {"capacity_ms": 5000, "available_ms": 5000, "capacity_frames": 250, "available_frames": 250, "frame_ms": 20, "last_sequence": 883},
    "consumers": 1, "consumer_details": [], "received_packets": 276,
    "invalid_packets": 0, "input_dropped": 0, "output_frames": 883, "last_error": null
  },
  "ros_runtime": {"available": true, "node": "audio_service", "executor_threads": 2, "owns_context": true, "error": null}
}
```

`status` 为 `ok` 需同时满足：Provider 可用、ROS 发布器可用、`media_audio` 存活；
若输入流离线但不影响 TTS，则降级为 `degraded`（不会误报 `ok`）。

### 音频输入 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/audio/status` | 输入全量状态：`capture` / `rechunker` / `ring_buffer` / `consumers` / `sessions` / `metrics` |
| GET | `/api/audio/level` | Dashboard 用电平与格式：RMS/Peak dBFS、历史、输入/输出帧率、流年龄、静音比例 |
| GET | `/api/audio/sessions` | 当前 Processor 会话快照与可用类型（`mock_asr` / `mock_tse`） |
| WS | `/ws/audio/input` | 20 ms / 640 B PCM 实时流（详见第 12 节） |
| WS | `/ws/audio/output` | 24 kHz / PCM16 / 单声道实时播放流，统一转换并仲裁到机器人扬声器 |

### `WS /ws/audio/output`

连接后首先发送 `audio_start` JSON；格式固定为
`pcm_24000_16_1`（24000 Hz、单声道、16-bit），随后可用任意大小的 WebSocket
二进制消息发送 PCM。服务按跨消息连续采样状态转换为
`pcm_48000_16_2`，以 20 ms 分包发布到 `/audio/speaker_pcm`。

```json
{"type":"audio_start","stream_id":"stream-1","response_id":"resp-1","encoding":"pcm_24000_16_1","sample_rate":24000,"channels":1,"sample_width":2}
```

正常结束发送 `{"type":"audio_end"}`；取消发送
`{"type":"audio_cancel","reason":"playback.clear"}`。服务端事件顺序为：

```text
output_ready → playback_started → playback_ended
                                ↘ playback_cancelled / output_error
```

`playback_started` 只在第一包成功发布到 ROS 后产生；`playback_ended` 只在最后一包发布且
配置的播放排空时间结束后产生。断开、空闲超时、超长、缓冲溢出或抢占都会停止旧流的后续
发布并可靠释放扬声器租约。

扬声器统一仲裁优先级为：高优先级/interrupt TTS（30）> 实时流（20）> 普通 TTS（10）。
高优先级来源可取消较低优先级所有者；普通 TTS 在实时流期间等待，实时并发连接返回
`output_busy`。`/api/tts/stop` 只管理 TTS，`audio_cancel` 只管理对应实时流。

相关限制位于 `service.json`：`playback.priorities`、`playback.acquire_timeout_seconds`、
`output.start_timeout_seconds`、`output.idle_timeout_seconds`、`output.max_stream_seconds`、
`output.buffer_limit_bytes`、`output.chunk_ms` 和 `output.playback_drain_seconds`。

`/api/audio/level` 示例（实测）：

```json
{
  "active": true, "online": true,
  "topic": "/audio/capture_pcm", "encoding": "pcm_16000_16_1",
  "sample_rate_hz": 16000, "sample_format": "S16_LE", "channels": 1,
  "frame_ms": 20, "chunk_bytes": 640,
  "level_db": -62.0, "peak_db": -54.2, "channel_levels_db": [-62.0],
  "input_rate_hz": 15.63, "output_rate_hz": 50.6, "last_frame_age_ms": 19.3,
  "packet_count": 278, "stream_clients": 1, "consumers": 1,
  "ring_available_ms": 5000, "history_seconds": 60, "update_hz": 10.0,
  "silence_ratio": 1.0, "activity_ratio": 0.0, "active_threshold_dbfs": -50.0,
  "error": null
}
```

### `POST /api/tts/speak`

```bash
curl -sS -X POST http://127.0.0.1:8091/api/tts/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，我是小乐机器人。","voice":"longanhuan_v3.1","emotion":"excited","speech_rate":1.1,"client_request_id":"demo-1","priority":"normal","interrupt":false}'
```

```json
{
  "ok": true,
  "deduplicated": false,
  "task_id": "d7eeea24a1c24b74",
  "phase": "queued",
  "queue_position": 0,
  "queue_size": 1,
  "current_task_id": null,
  "model": "qwen-audio-3.1-tts-flash",
  "voice": "longanhuan_v3.1",
  "emotion": "excited",
  "speech_rate": 1.1,
  "task": {"task_id": "d7eeea24a1c24b74", "phase": "queued", "text": "…", "timestamps": {"queued": 1794291045.836}}
}
```

- 新任务返回 `202`；`client_request_id` 幂等命中返回 `200` 且 `deduplicated=true`；
- 空文本 / 超长分别返回 `invalid_text` / `text_too_long`（400）；
- 不支持的音色、情绪或越界语速分别返回 `invalid_voice`、`invalid_emotion`、`invalid_speech_rate`（400）；
- 队列满返回 `queue_full`（429）；
- `priority=high` 或 `interrupt=true` 会插到队首（见第 6 节）。

`GET /api/tts/status` 的 `controls` 字段返回 Dashboard 可用的音色、情绪、默认值和语速范围。
非 `neutral` 情绪由 Provider 以模型官方标签（如 `[excited]`）加在合成文本前；任务历史保留原始文本及实际控制参数。

### `POST /api/tts/stop`

停止当前任务的后续 PCM 发布并清空队列：

```json
{
  "ok": true,
  "cancelled_queued": 2,
  "current_task_id": "d7eeea24a1c24b74",
  "current_phase": "publishing",
  "estimated_remaining_playback_seconds": 0.12,
  "pcm_publishing_stopped": false,
  "playback_control": {"interrupt_topic": "/audio/speaker_pcm_interrupt", "enabled": false, "reason": "…"}
}
```

本服务保证「不再发送新的 PCM」。已经进入厂商缓冲的音频无法撤回，
因此用 `estimated_remaining_playback_seconds` 如实报告剩余播放量。

### `GET /api/tts/status`

返回模型/音色、Provider 可用性与计数、当前任务、队列、最近 5 条任务、
ROS 发布状态（含 `published_packets`、`capture.rate_hz`）、`media_audio` PID 与最后错误。

### `GET /api/tts/history?limit=20`

最近任务（默认 50 条环形缓存），含阶段时间戳、各阶段耗时、PCM/发布统计与错误摘要。

### 任务阶段

```text
queued → synthesizing → decoding → publishing → waiting_playback → succeeded
                                                                ↘ failed / cancelled
```

| 阶段 | 含义 |
| --- | --- |
| `queued` | 已入队，等待串行 worker |
| `synthesizing` | 调用 DashScope |
| `decoding` | ffmpeg MP3 → PCM |
| `publishing` | 按 60 ms 节奏发 `/audio/speaker_pcm` |
| `waiting_playback` | 最后一包后的厂商缓冲尾部 |
| `succeeded` / `failed` / `cancelled` | 终态 |

## 5. 音频规范与校验

### 输入（Gateway）

| 项 | 厂商输入 | Gateway 输出 |
| --- | --- | --- |
| 编码 | `pcm_16000_16_1` | `pcm_16000_16_1` |
| 采样率 / 位深 / 声道 | 16000 Hz / S16_LE / 1 | 同左 |
| 单包 | 2048 B = 1024 samples ≈ 64 ms | 640 B = 320 samples = 20 ms |
| 帧率 | 约 15.6 Hz | 50 Hz |
| 消息类型 | `sensor_msgs/msg/Image`（`height=1`，`width=step=len(data)`） | WebSocket 二进制 / 进程内 `AudioFrame` |
| 校验 | bigendian=false、encoding 匹配、rate/channels/sample_width 匹配、`height*step == len(data)`、PCM 字节数为帧对齐 | 样本累积器保证不重不漏 |

### 输出（TTS）

| 项 | 值 |
| --- | --- |
| 采样率 | 48000 Hz（`PcmAudio.validate()` 强校验） |
| 位深 | signed 16-bit little-endian（`sample_width=2`） |
| 通道 | 2 |
| 分包 | 11520 B = 2880 帧 = 60 ms |
| 末包 | 若不足 11520 B，按 4 B 帧对齐（必要时补 0，最多 3 B），不补齐到整包 |
| 发布节奏 | 单调时钟 `time.monotonic()`，第 i 包目标时刻 `start + max(0, i-2) × 60 ms` |

每条消息：`height=1`、`width=step=len(pcm)`、`encoding=pcm_48000_16_2`、`is_bigendian=false`、
`header.stamp=now()`。发布统计记录 `packets/expected/dropped/chunk_bytes/duration/rate_hz/
measured_rate_hz/lag_events`，以及 `media_audio_pid_before/after`、
`capture_rate_hz_start/end`，便于验收。

## 6. 播放仲裁

- 服务内**单 worker 串行队列**：任意时刻只有一个任务向 `/audio/speaker_pcm` 发布；
  多 Dashboard / Agent 并发请求只会排队，不会叠音。
- `priority=high` 或 `interrupt=true` 插到队首，但仍需等当前任务发布完（或先 `/api/tts/stop`）。
- **厂商插播通路不能承载 TTS**：`/audio/speaker_pcm_interrupt` 是 `std_msgs/msg/String`，
  载荷是 `/userdata/joyin/interrupt_pcm/` 下预置 PCM 的引用，不是音频数据。实机日志
  2026-11-10 14:20:33 记录到厂商自身插播：

  ```text
  [ros2_speaker_subscriber.cpp:855] clear normal pcm queue
  [ros2_speaker_subscriber.cpp:876] [插播帧2] 找到音频文件: /userdata/joyin/interrupt_pcm/0014_voice.pcm
  [speaker_stream_play.c:1176] 插播切换已清空旧队列: 正常2块(22.5KB), 插播0块(0.0KB)
  [speaker_stream_play.c:1208] 插播批次入队完成: 31块, 约1770ms
  ```

  即插播会**清空 normal 队列并抢占播放**，所以 `interrupt=true` 只做队首插队，
  `playback_control.enabled=false` 如实上报（原因同上）。
- **厂商停止话题未生效**：`/audio_stop/msg`（`std_msgs/String`）在 `publishing` 阶段
  发布空消息后，厂商继续正常消费队列直到 `声卡播放完成通知已发布`，
  无法作为 `/api/tts/stop` 的厂商侧停止手段。未确认语义的控制话题一律不调用。
- **播放完成回执存在但是全局量**：`/audio/speaker_pcm_status`（`std_msgs/msg/Int32`）
  在整段 PCM 播完时发布 `data=1`（实测 14:22:49.671、14:23:00.809），
  但它不携带任务 ID，且厂商插播也会触发，因此只作为观测字段暴露，
  不作为任务成功的判据；`waiting_playback` 仍按包时长 + 固定缓冲估算。

## 7. 错误码

| code | HTTP | 场景 |
| --- | --- | --- |
| `invalid_text` | 400 | 空文本 / 非字符串 / 含 NUL |
| `text_too_long` | 400 | 超 `max_text_length` |
| `invalid_priority` | 400 | priority 非 normal/high |
| `queue_full` | 429 | 队列达到容量 |
| `provider_unavailable` | 503 | 未配置 `DASHSCOPE_API_KEY` 或 SDK 缺失 |
| `provider_error` | 502 | 云端报错 / 返回空音频 |
| `provider_timeout` | 504 | 合成超时 |
| `decode_failed` | 502 | ffmpeg 解码失败 / 无输出 |
| `decode_timeout` | 504 | 解码超时 |
| `decode_unavailable` | 503 | 找不到 ffmpeg |
| `pcm_format_invalid` | 500 | 输出格式或帧对齐不符合规范 |
| `ros_unavailable` | 503 | rclpy / 发布器不可用 |
| `publish_timeout` | 504 | 发布阶段超时 |
| `service_stopping` | 503 | 服务正在停止 |
| `task_not_found` | 404 | `GET /api/tts/task/{id}` 未知任务 |
| `internal_error` | 200→任务 failed | 未预期异常（写入任务错误） |

所有错误响应体统一为 `{"error": {"code", "message", "detail"}}`，
`message` 与 `detail` 中出现的密钥一律替换为 `sk-xxx****`。

## 8. 实机环境注意事项（踩坑记录）

1. **根文件系统只读、默认 CA 库为空**：`/usr/lib/ssl/cert.pem` 不存在，
   直接 `ssl.create_default_context()` 的证书数为 0，wss 握手报
   `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`。
   解决：`main.py` 启动时把 `SSL_CERT_FILE` 指向 venv 内 `certifi` 的 `cacert.pem`
   （`run.sh` 同样导出，`REQUESTS_CA_BUNDLE` / `WEBSOCKET_CLIENT_CA_BUNDLE` 一并设置）。
2. **`libssl.so.3` 不在默认路径**：venv 内的 Python 需要
   `LD_LIBRARY_PATH=/middleware/pub/lib` 才能 `import ssl`。
3. **ROS 的 `LD_LIBRARY_PATH` 会打死系统 ffmpeg**：sourcing `/middleware/humble/setup.sh`
   后 `ffmpeg` 报 `libXrandr.so.2: cannot open shared object file`（该库在镜像中不存在）。
   解决：`AudioConverter` 给 ffmpeg/ffprobe 子进程使用清空 `LD_LIBRARY_PATH` 的环境
   （`audio.clean_child_env=true`），不影响父进程的 ROS/SSL 依赖。
4. **系统 `curl` 缺 `libcurl.so.4`**：该库只存在于 `/app/joyin/lib/thirdparty`，
   默认 `LD_LIBRARY_PATH` 下 `curl` 直接报 `cannot open shared object file`。
   `run.sh` 已把该目录放进 `LD_LIBRARY_PATH`（健康检查可用）；`status.sh` 改为用
   venv Python 的 `urllib` 查询，不再依赖 `curl`。
5. **mp3 源参数**：实测 DashScope 返回 128 kbps / 22.05 kHz / 单声道 MP3，
   经 ffmpeg 转 48 kHz 双声道后字节数为 4 × 采样帧数，帧对齐校验通过。
6. **`media_audio` 会“静默降级”**（本次实机最大的坑）：若它启动瞬间 `hw:2,0` 被占用
   （例如上一次 `aplay`/ESPeak 测试残留），日志会记录

   ```text
   [E][speaker_stream_play.c:322] 错误: 打开PCM设备失败 - Device or resource busy
   [E][ros2_speaker_subscriber.cpp:178] 音频播放器初始化失败: -16，但继续运行（音频播放功能将不可用）
   [I][ros2_speaker_subscriber.cpp:706] 音频播放器未初始化，跳过播放
   ```

   此时进程仍然 running、`/audio/capture_pcm` 正常、订阅者也在，但**所有扬声器 PCM 被丢弃**，
   PID 与 ROS 图都看不出异常。部署和验收前必须确认：

   ```bash
   grep -E '音频播放器初始化成功|音频播放器初始化失败' /userdata/log/media_audio/media_audio.log | tail -2
   # 期望: speaker_stream_play.c:427] 音频播放器初始化成功: rate=48000, period=720帧(~15ms) ...
   ```

   修复方式：确认没有进程占用 `/dev/snd` 后重启 `media_audio`（本次用
   `/userdata/developer/dashboard/scripts/ensure_media_audio.sh`），并确认新进程打印
   “音频播放器初始化成功”。重启属于一次性排障，**本服务正常运行期间不会重启它**。
7. **`media_audio` 重启会重置扬声器音量**：启动日志
   `音量控制初始化成功: 声卡=hw:2, 元素=DAC, 范围=[0, 207], 当前音量=78%`，
   即回到厂商默认 `-15 dB`。重启后需重新设置音量（例如 `amixer -c 2 sset DAC -- -32dB`，
   或走 Dashboard `POST /api/audio/volume`），再验收。

## 9. 测试

```bash
cd /userdata/audio_service && .venv/bin/python -m pytest -q    # 98 项
```

覆盖（输入侧新增 51 项，其余为原有 TTS 回归）：

| 主题 | 用例 |
| --- | --- |
| 消息格式 | encoding/采样率/声道/位宽/大端/`height*step` 与 PCM 长度不一致均计为 `invalid_packets` |
| 解析 | S16_LE 单声道样本值、`AudioFrame` 不可变与连续性 |
| 重分帧 | 5×64 ms → 16×20 ms；跨包余数不丢不重；样本总数守恒；未对齐尾部经 `flush()` 上报 |
| 环形缓冲 | 容量 250 帧、按毫秒读取、pre-roll、`clear()`、非法 `frame_ms` 拒绝 |
| Audio Bus | `latest` 只留最新帧；`drop_oldest` 隔离；`fail_on_gap` 结构化 gap 事件与 fail-closed；溢出；pre-roll seed 去重；慢消费者不阻塞快消费者；多消费者并发；`close()` 释放资源；关闭后继续发布不崩 |
| 电平 | RMS/Peak dBFS 数值、静音比例、约 10 Hz 更新 |
| API | `/api/audio/status` 结构、`/api/audio/level`（含离线）、非法 input 配置拒绝 |
| WebSocket | `audio_start` 元数据、640 B 二进制帧、pre-roll 帧数、非法 `pre_roll_ms`/`delivery` 拒绝、断开清理 |
| 会话 | Mock ASR/Mock TSE 同时消费同一流（含 pre-roll）、gap 会话标记、非法 type/pre_roll |
| ROS 生命周期 | 假 rclpy 模块验证单次 init、共享节点、无重复 capture 订阅、`publisher.stop()` 不销毁共享节点、`runtime.stop()` 销毁并 shutdown、独立模式自持节点、publisher 数量与 QoS、节点创建失败上报 |
| TTS 回归 | 文本校验、幂等、队列容量与优先级、Provider 成功/错误/超时、解码失败、PCM 格式与帧对齐、60 ms 分包、发布节奏、取消、停止清队列、状态/历史、音色目录、密钥脱敏、真实 ffmpeg 解码 |

> 机器人端执行测试时，`.venv/bin/python` 需要能加载 `libssl.so.3`（否则 anyio/FastAPI
> TestClient 报 `LookupError: Backend 'asyncio' is not available`），而系统 ffmpeg 又必须
> 在没有 `/middleware/pub/lib` 的路径下运行。推荐只把 ssl 库单独暴露出来：
>
> ```bash
> mkdir -p /tmp/m1libs && ln -sf /middleware/pub/lib/libssl.so.3 /tmp/m1libs/
> ln -sf /middleware/pub/lib/libcrypto.so.3 /tmp/m1libs/
> cd /userdata/audio_service && LD_LIBRARY_PATH=/tmp/m1libs .venv/bin/python -m pytest -q
> ```

## 10. 实机验收记录

环境：2026-11-10，`ROS_DOMAIN_ID=0`、`ROS_LOCALHOST_ONLY=1`，
`media_audio` PID 15606（重启后新实例，播放器已初始化成功），
扬声器 `DAC` 为厂商默认 −15 dB，测试均为短文本。

### 10.1 验收语句（含唤醒词，被厂商插播打断）

测试文本：`你好，我是小乐机器人，语音服务测试成功。`

| 项 | 结果 |
| --- | --- |
| DashScope | 成功，MP3 61113 B，128 kbps / 22050 Hz / 单声道，latency 1484.6 ms |
| 转换后 PCM | 48 kHz / 16-bit / 2 ch，732268 B，183067 帧，3.814 s |
| ROS 发布 | 64/64 包，每包 11520 B，3.673 s，标称 16.67 Hz（实测 17.42 Hz），丢包 0，滞后 0 |
| `media_audio` PID | 15606 → 15606（未变，running） |
| `/audio/capture_pcm` | 播放期 15.60–15.68 Hz |
| 厂商侧接收 | `[SPEAKER_INPUT] frames=16, pcm=922.0ms`、`[PLAYBACK] queue=180ms(normal=3) … state=RUNNING, xruns=0` |
| 打断情况 | 14:20:33 厂商自身插入 `0014_voice.pcm`（31 块 ≈1770 ms）并清空 normal 队列，本段播报被截断 |
| 任务耗时 | 5.757 s（合成 1.515 s / 解码 0.082 s / 发布 3.723 s / 尾部 0.432 s） |

### 10.2 完整播放（不含唤醒词）

测试文本：`语音服务测试成功`

| 项 | 结果 |
| --- | --- |
| ROS 发布 | 28/28 包，每包 11520 B，1.512 s，标称 16.67 Hz（实测 18.52 Hz），丢包 0 |
| 转换后 PCM | 48 kHz / 16-bit / 2 ch，320996 B，80249 帧，1.672 s |
| 厂商侧播放 | `[PLAYBACK] … state=RUNNING, primed=1, xruns=0`，末尾 `声卡播放完成通知已发布: data=1`（14:22:49.671） |
| `media_audio` PID | 15606 → 15606（未变） |
| `/audio/capture_pcm` | 播放期 15.56–15.65 Hz |
| 任务耗时 | 3.156 s（合成 1.067 s / 解码 0.121 s / 发布 1.563 s / 尾部 0.401 s） |

### 10.3 其他验收项

| 项 | 结果 |
| --- | --- |
| **现场实听** | **确认听到**：14:32:44 以听感 30%（`DAC` −22 dB，低于 50% 限制）播报「语音服务测试成功」，1.51 s / 28 包，厂商日志同步出现 `state=RUNNING` 与 `声卡播放完成通知已发布: data=1` |
| 厂商停止话题 | `/audio_stop/msg` 空消息在播放中发布无效，播放继续到 `data=1` 完成回执 |
| 任务总耗时参考 | 7.04 s（长句 4.7 s 音频，79 包，无丢包） |
| 声学复核 | `/audio/capture_pcm`（AEC 后）播放中位数 −72.8 dB、基线 −68.7 dB；`/audio/capture_pcm_origin` 中位数 −63.8 dB 对基线 −63.9 dB。厂商 AEC 会抵消自身播报，**麦克风不能作为“是否出声”的判据** |
| Dashboard 代理 | `POST /api/tts/speak` → 202，任务成功，PID 未变，history 正常 |
| 离线降级 | 停止 audio_service 后 Dashboard 返回 503 `service_unavailable`（`transport: Connection refused`） |
| 密钥 | 源码 / 日志 / 配置 / API 响应中 0 处明文；`runtime.env` 权限 600 |
| 单元测试 | audio_service 31 项通过；Dashboard TTS 客户端 13 项通过 |

> 说明：本轮 `media_audio` 曾被重启一次以修复播放器初始化失败（见第 8 节第 6 条），
> 重启后厂商把 `DAC` 恢复为默认 −15 dB；测试音频总时长均 ≤ 4.7 s。


### 10.4 音频输入 Gateway（2026-11-10 15:20–15:45）

部署：`scp` 同步 `src/`、`tests/`、`config/service.json`、`scripts/`；`stop.sh` + `run.sh` 重启；
备份 `/userdata/audio_service_backup_20261110_151545.tgz`。

| 验收项 | 实测结果 |
| --- | --- |
| `media_audio` PID | 15606 → 15606（部署、重启、TTS 播报前后均未变化） |
| ROS 图 | 节点 `/audio_service`；`/audio/capture_pcm` 发布者 1（`capture_mic_node`，RELIABLE），订阅者 2：`/business`（厂商语音插件）+ `/audio_service` |
| 输入帧率 | 15.58–15.63 Hz（Gateway 统计），`last_frame_age_ms` 16～57 ms |
| 输出帧率 | 50.0–50.6 Hz（`/api/audio/status` 3 s 滑窗） |
| WebSocket 二进制帧 | 全部 640 B（两个 `continuous` 客户端 7 s 各 350/351 帧，50.0/50.1 Hz） |
| 重分帧样本守恒 | 窗口内 `received_bytes Δ260096 == output_frames Δ406 × 640 + buffered Δ256`（Δ127 包 → Δ406 帧，与 5→16 比例一致） |
| 环形缓冲 | 250 帧 / 5000 ms 满容量，`available_ms=5000` |
| 慢消费者隔离 | `delivery=latest` 客户端 0.3 s/帧（7 s 收 24 帧），同期两个 `continuous` 客户端仍 50 Hz；`dropped_frames` 累计 217，`queue_depth` 0 |
| 消费者清理 | 所有 WS 断开后 `consumer_count=0`，指标与队列释放 |
| 查询参数校验 | `pre_roll_ms=9999 / -5`、`delivery=bogus` 均被拒绝（握手 403/close 1008） |
| Mock ASR + Mock TSE | 同时 running：各 2057 帧 / 0 丢帧 / `gap_free=true`；同一时刻再接一个 WS 客户端 → `consumer_count=3`，WS 仍 640 B |
| TTS 回归 | `POST /api/tts/speak` → 202，任务 `succeeded`（合成+播放 6.0 s），期间输入保持 15.58–15.63 Hz、`online=true`；`media_audio` PID 不变 |
| Dashboard 迁移 | ROS 订阅者从 3 降为 2（`zeroth_dashboard` 消失）；`/api/audio/level` 返回 200 且 `proxy.service_online=true`、`ws_connected=true`；`/ws/audio/pcm` 仍发 `audio_start` + 640 B 帧 |
| 离线降级 | 停止 audio_service 后 Dashboard `/api/audio/level` 返回 503 `audio_service_unavailable`，`/ws/audio/pcm` 先发 `audio_unavailable` 再 close 1013；重启后 8 s 内自动重连（`reconnects` 计数递增） |
| 资源占用 | audio_service：空闲（仅 Gateway）约 15% 单核、带 Dashboard WS 消费者约 38–48% 单核；RSS ≈ 130 MB，15 线程；入口队列深度 0、`ingress_dropped=0`、`invalid_packets=0` |
| 单元测试 | audio_service 86 项通过；Dashboard 68 项通过（本地 3 项 Linux-only 跳过） |

## 11. 已知限制与下一步

1. **厂商插播通路未启用**：`/audio/speaker_pcm_interrupt` 已确认是
   “预置 PCM 文件引用”接口（`String` + `/userdata/joyin/interrupt_pcm/*.pcm`），
   只能播厂商内置提示音，不能播任意合成长句。若后续需要“真打断”，需厂商提供
   `interrupt` 队列的二进制载荷或带任务 ID 的控制协议。当前 `interrupt=true` 只是插队首。
2. **停止粒度受厂商缓冲限制**：已提交的包无法撤回，`/api/tts/stop` 只能保证不再发送
   新 PCM，并用 `estimated_remaining_playback_seconds` 报告残余；`/audio_stop/msg`
   实测无效，未找到可用的厂商侧停止接口。
3. **播放完成回执不可归因**：`/audio/speaker_pcm_status` 的 `data=1` 是全局通知，
   不带任务 ID，且厂商插播也会触发，因此 `waiting_playback` 仍按包时长估算。
4. **厂商插播会抢占普通 TTS**：当厂商业务（如唤醒回应）插入预置 PCM 时，normal 队列会被清空，
   本服务已发布的剩余包被丢弃；任务仍标记成功，但实际被截断。需要在业务层避免
   TTS 文本中包含唤醒词，或后续接入厂商播放状态做失败上报。
5. **合成超时不会中断线程**：`asyncio.to_thread` 无法强制结束底层 wss 调用，
   超时后线程会自行结束时释放；频繁超时建议重启服务。
6. **单实例**：本地只监听 `127.0.0.1:8091`，多机器人部署需各自实例。
7. **服务治理**：给 `/api/tts/speak` 增加按调用方限流，并把 `audio_service` 纳入
   开机自启与守护（当前由 `scripts/run.sh` 手工/脚本拉起）。
8. **统一输入 Gateway 已落地**：本服务已单一订阅 `/audio/capture_pcm` 并向 Dashboard、
   Agent、ASR/TSE 分发；Dashboard 不再直接订阅音频 ROS 话题。
9. **ASR/VAD**：在 `AudioProcessor` 接口上实现真实流式识别（建议 `fail_on_gap` +
   `pre_roll_ms=300`），把增量结果通过 `/ws/audio/*` 事件流推送。
10. **TSE/说话人能力**：增加目标说话人档案、注册、提取、识别和验证 API，并统一管理
    模型、音频样本、任务与输出流。
11. **目标边界**：TTS、ASR、TSE 等是 Gateway 内部可替换能力模块；上层项目全部依赖
    `audio_service` API，不再分别形成绕过 Gateway 的音频通路。
12. **输入侧资源开销**：在 M1 上 Gateway 空闲约 15% 单核；每个 `continuous` WebSocket
    消费者会追加可观开销（Dashboard 代理约 +23~33% 单核）。若后续接入多个高频消费者，
    建议在 Bus 侧合并推送（单广播任务 + `asyncio.Queue` 扇出）或降低非关键消费者帧率。
13. **慢 WebSocket 客户端的 `audio_stats` 延迟**：统计事件按 1 s 周期在发送路径上产生，
    若客户端 TCP 缓冲长期阻塞，统计可能延后；此时以 `GET /api/audio/status` 的
    `consumers[]` 为准（含 `dropped_frames`、`queue_depth`、`lag_ms`）。

## 12. 音频输入 Gateway 详解

### 12.1 组件与数据流

```text
ROS 回调线程        asyncio 事件循环（单线程）                    消费者
────────────        ─────────────────────────────                ────────
_on_message  →  IngressQueue  →  _pump_loop
 校验/构造入口帧      有界、非阻塞       ├─ Rechunker（64→20 ms）
                                       ├─ RingBuffer（5 s / 250 帧）
                                       ├─ LevelMeter（≈10 Hz RMS/Peak）
                                       └─ AudioBus.publish
                                              ├─ ws-input-*（latest/drop_oldest）
                                              └─ session-*（fail_on_gap）
```

ROS 回调只做四件事：字段与 PCM 长度校验、构造入口帧、非阻塞写入入口队列、更新轻量计数。
模型推理、网络、磁盘、WebSocket 发送都在事件循环或消费者任务中完成，回调线程不会被阻塞。

### 12.2 AudioFrame

```python
@dataclass(frozen=True)
class AudioFrame:
    sequence: int                 # Gateway 单调递增序号（20 ms 帧）
    captured_at_ns: int           # 本机 time.monotonic_ns()
    source_timestamp_ns: int|None # ROS header.stamp
    sample_rate: int              # 16000
    channels: int                 # 1
    sample_width: int             # 2
    encoding: str                 # pcm_16000_16_1
    pcm: bytes                    # 640 B
    input_sequence: int = 0       # 来源入口帧序号（连续性诊断）
    discontinuity: bool = False   # 入口层检测到的序号断裂
```

`AudioFormat.parse_encoding("pcm_16000_16_1")` 与配置交叉校验；帧对象不可变，可安全跨任务共享。

### 12.3 20 ms 重分帧与环形缓冲

- 输入 1024 samples/包，20 ms 需要 320 samples（640 B）；每包产出 3 帧、余 64 samples，
  第 5 个包刚好补满 16 帧（5×64 ms = 320 ms = 16×20 ms）。
- `Rechunker` 用字节级累积器保存余数，跨包不丢不重，算法层看不到输入包边界；
  `flush()` 在停止时返回未成帧残留（实测每次停止为 128/256/512 B 的整数倍关系）。
- `RingBuffer` 默认 250 帧（5 s），`recent(ms)` 用于 pre-roll / 新会话 / TSE 上下文；
  写入只在事件循环内追加 deque，消费者读取不持有写锁，不会反压入口。

### 12.4 Audio Bus 策略

| 策略 | 容量 | 队列满时 | 适用 |
| --- | --- | --- | --- |
| `latest` | 1 | 覆盖旧帧 | Dashboard 电平、预览、慢速 WS |
| `drop_oldest` | `stream_capacity`（默认 50） | 丢该消费者最旧帧并计数 | 持续实时处理器、`delivery=continuous` |
| `fail_on_gap` | `consumer_capacity`（默认 100） | 发结构化 gap 事件并 fail-closed | ASR/TSE 等要求连续音频的会话 |

订阅接口：

```python
subscription = audio_bus.subscribe(name="asr-session-xxx", policy="fail_on_gap", capacity=100)
async for frame in subscription:
    ...
# 缺口时 __anext__ 抛 AudioGapError({type, reason, expected_sequence, actual_sequence, dropped_frames, at})
```

`fail_on_gap` 触发后该订阅不再接收新帧（fail-closed），由 `SessionManager` 将会话标记为
`gapped` 并结束，Gateway 与其他消费者不受影响；`close()` 立即释放队列与指标。
任何策略下 `publish()` 都只做非阻塞入队，单消费者异常不会反压 ROS 入口。

逐消费者指标：`received_frames / delivered_frames / dropped_frames / duplicate_frames /
gaps / queue_depth / max_queue_depth / last_sequence / last_delivered_sequence / lag_ms`。

### 12.5 WebSocket 协议 `/ws/audio/input`

查询参数：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `pre_roll_ms` | `0..5000`（受 `pre_roll_max_ms` 限制） | 连接后先补发最近 N 毫秒；越界 close 1008 |
| `delivery` | `latest`（默认）/ `continuous` | `latest` 用 `latest` 策略容量 1；`continuous` 用 `drop_oldest` 容量 `stream_capacity`；非法值 close 1008 |

握手成功后第一条消息是 JSON 元数据：

```json
{"type":"audio_start","stream_id":"4d783ae6…","topic":"/audio/capture_pcm",
 "encoding":"pcm_16000_16_1","sample_rate":16000,"sample_width":2,"channels":1,
 "frame_ms":20,"frame_bytes":640,"delivery":"continuous","pre_roll_ms":0,
 "pre_roll_frames":0,"online":true,"ts":1794295757.981}
```

随后每条二进制消息 = 一帧 640 B PCM。每 1 s 推送一次低开销统计事件：

```json
{"type":"audio_stats","sent_frames":250,"dropped_frames":0,"queue_depth":0,
 "last_sequence":3376,"lag_ms":18.5,"capture_to_robot_send_ms":18.5,
 "server_sent_at_ms":1794295813936,"ts":1794295813.936}
```

`capture_to_robot_send_ms` 是机器人单调时钟测得的采集到统计发送年龄；客户端可将
`server_sent_at_ms` 与本机接收时间比较，估算机器人到客户端的传输与时钟偏移。

客户端断开后订阅立即 `close()`；`/api/audio/status` 的 `consumers` 会同步移除该消费者。

### 12.6 Processor / Session 接口

```python
class AudioProcessor(Protocol):
    async def start(self, session) -> None: ...
    async def accept(self, frame: AudioFrame) -> None: ...
    async def finish(self) -> None: ...
    async def cancel(self) -> None: ...
    def status(self) -> dict: ...
```

`AudioSession` 字段：`session_id / processor_type / state / policy / capacity / pre_roll_ms /
pre_roll_frames / created_at / started_at / finished_at / duration_seconds /
input_sequence_start / input_sequence_end / received_frames / dropped_frames /
gap_detected / error`，状态机为 `pending → running → finished | cancelled | gapped | failed`。

会话启动顺序：先 `subscribe` 再 `seed(ring.recent(pre_roll_ms))` 再 `processor.start()`，
保证 pre-roll 与实时帧连续且不重复；`stop_requested` 时走 `finish()` 正常收尾。

调试用 `mock_asr` / `mock_tse` 只通过 `input.mock_sessions` 配置自启，不暴露生产 API；
Mock ASR 记录 `pre_roll_frames / duration_ms / gap_free`，Mock TSE 记录 `energy_dbfs`。

接入真实 ASR/TSE 时：实现上文 Protocol → 在 `SessionManager` 注册类型 → 用
`fail_on_gap` + 300–500 ms pre-roll 启动会话 → 在 `finish()` 输出结果。

### 12.7 Dashboard 迁移

```text
Dashboard UI → Dashboard /api/audio/level → audio_service /api/audio/level（10 Hz 轮询）
Agent/旧客户端 → Dashboard /ws/audio/pcm → audio_service /ws/audio/input（640 B/帧）
```

- `dashboard/src/ros_collector.py` 已移除 `/audio/capture_pcm` 订阅与 `Image` 依赖。
- `dashboard/src/audio_monitor.py` 改为纯代理：HTTP 轮询 + WebSocket 客户端（指数退避
  0.5→5 s 重连），`snapshot()` 保留原有字段并新增 `proxy` 子对象
  （`service_online / ws_connected / sent_frames / dropped_frames / queue_depth /
  last_sequence / lag_ms / reconnects`）。
- audio_service 离线：HTTP 返回 503 + `error_code=audio_service_unavailable`；
  WebSocket 先发 `{"type":"audio_unavailable","code":"audio_service_unavailable"}` 再 close 1013；
  服务恢复后自动重连，无需重启 Dashboard。
- Dashboard TTS 代理与运动控制不受影响。

### 12.8 指标与故障含义

| 指标 | 异常含义 |
| --- | --- |
| `capture.online=false` / `last_frame_age_ms` 持续增大 | 厂商未发布或订阅丢失；检查 `publishers` 与 `media_audio` |
| `invalid_packets` 增长 | 消息字段/长度不符（encoding、shape、非 16-bit、大端） |
| `input_dropped` 增长 | 入口队列 `ingress_capacity` 溢出，事件循环被长任务阻塞 |
| `rechunker.buffered_bytes` 持续 ≥640 | 重分帧不前进，输入流断流 |
| `rechunker.overflow_bytes` 增长 | 单包过大导致累积器保护性丢弃（正常为 0） |
| `consumer.dropped_frames` 增长 + `queue_depth=0` | 消费者读得慢但未积压（`latest` 覆盖语义） |
| `consumer.gaps>0` / 会话 `gap_detected=true` | 序号断裂或队列溢出，`fail_on_gap` 会话已重置 |
| `ring_buffer.available_ms` 长期 < 5000 | 输入断流或帧率不足 |
| `level.silence_ratio=1.0` 且 `active=false` | 环境音量低于 −50 dBFS 阈值（默认手机/机器人静音环境属正常） |

日志为速率限制：持续音频帧不逐帧打印，错误类日志最多每 5 s 一条。

## 13. 厂商预设语音由 Audio Agent 接管（1.3.0）

`media_audio` 在 ROS domain 43 运行，其他机器人组件保持 domain 0。
`MediaAudioDomainBridge` 转发 PCM、唤醒、播放状态、原始采集、扬声器控制、
Wi-Fi 状态，以及 8 个音频/扬声器启停、音量、麦克风模式服务。
`/audio/speaker_pcm_interrupt` 由 `VendorEventBridge` 接收，经过 Agent 决策后
才可送入私有域，避免厂商提示与 Agent 同时说话。

- 状态：`GET /api/vendor/events/status`；综合状态：`GET /api/health`。
- Agent 连接：`WS /ws/vendor/events`，先发 `agent_hello`，随后定期发
  `agent_heartbeat`，`ready` 表示 Gateway 是否可用。
- 事件：`vendor_audio_event` 包含 `event_id/audio_id/event/summary/severity/
  suggested_delivery/occurred_at`；编号语义未知时保留原编号。
- 决策：`vendor_event_decision` 包含 `event_id/action/reason`。
  `agent` 或 `ignore` 抑制厂商播放，并确认行为树中的音频节点完成；
  `play_vendor` 转发原始提示。这里确认的是提示已处理，不代表运动完成。
- Harness 使用现有 `client.event.publish`；`context` 仅补充上下文，
  `respond/interrupt` 由模型结合对话决定是否向用户说明。
- Agent 离线、未就绪、队列已满或 1.5 秒内没有有效决策时，回退厂商语音。
  音频服务本身是该回退路径的一部分，需要保持运行。

部署时同步使用 `deploy/media-audio-private-domain.patch`（项目根目录）修改厂商
`/app/joyin/run.sh` 和 `watchdog.sh`。Dashboard 的 `ensure_media_audio.sh`
根据 audio_service 配置选择域。重启厂商进程时必须完整保留厂商动态库路径、
`ROS_LOCALHOST_ONLY` 以及可写的 `ROS_HOME/ROS_LOG_DIR`，仅设置域编号不足以启动音频。
`audio_service/deploy/restart-media-audio.sh` 可从正在运行的厂商视频进程继承这些环境。

私有域兼容桥没有代理与视频节点重名的 `/all_enable`、`/all_disable`；
音频应使用明确的 `/audio_enable`、`/audio_disable` 等接口。

实机验收脚本 `audio_service/deploy/verify-vendor-routing.py` 检查在线抑制、
未就绪 Agent 回退、主 Harness 自动重连，以及 0.25 秒静音 PCM 输出。
回退检查会短暂替换事件连接并请求一次厂商唤醒提示；需要在完整 ROS Python 环境下运行。

## 14. 全双工输入隔离（1.3.1）

实时输出的重采样改为 C 实现，并将重采样及 ROS 发布移出 FastAPI 主事件循环。
由于机器人在播放期间的进程负载仍会影响同进程 WebSocket 调度，Agent 的麦克风流进一步
拆分到独立进程：

```text
audio_input_service 127.0.0.1:8093 → /ws/audio/input
audio_service       127.0.0.1:8091 → /ws/audio/output、TTS、仲裁、厂商事件
```

`scripts/run.sh` 会同时启动两个进程，`/userdata/startup.sh` 也分别检查 8091 和 8093。
Harness 应把 `M1_AUDIO_INPUT_URL` 指向 8093；输出与取消接口继续使用 8091。

`deploy/verify-full-duplex.py` 使用 12 秒静音输出压力验证麦克风连续性。验收标准为输入
至少 40 帧/秒、`dropped_frames <= 5`、队列不超过 10、采集到发送延迟不超过 300 ms。

## 15. 实时播放抗抖动（1.3.3）

实时 PCM 的 ROS 发布使用独立单线程执行器，避免与重采样及其他 `to_thread` 工作争用。
播放器在首次发布前至少积累 `output.startup_buffer_chunks` 个完整分片，并按
`output.publish_lead_chunks` 预送。默认 120 ms 分片下为 3 包启动缓冲、2 包预送，约
360 ms。实机上的 RELIABLE ROS 发布调用平均约 64 ms/次，120 ms 分片可避免发布耗时
长期占满每个调度周期，同时覆盖 `media_audio` 观测到的 120–190 ms DDS 到达抖动。

`GET /api/audio/status` 的 `output.stats` 与活动流新增：

- `publish_call_ms_max`：单次 ROS 发布的最大耗时；
- `schedule_late_events / schedule_late_ms_max`：发布调度迟到次数和最大值；
- `startup_buffered_chunks / startup_buffered_ms`：本轮启动时实际积累的音频。

实机验收除原有 `xruns=0`、`seq_gap=0` 外，厂商日志中的 `supply` 应接近 `1.0`，播放
中 `silence_periods` 不应连续增长。
