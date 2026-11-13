# Zeroth M1 Audio Agent：部署与开发总手册

本分支 `feat/m1-robot` 将 Qwen Audio Agent 接入 Zeroth M1：通过机器人的麦克风和扬声器进行实时语音交互，将状态、运动、视觉及声纹管理能力注册为 Agent 工具。本文是本分支的入口，面向第一次部署的开发者；组件 API 和内部实现链接见文末。

推荐部署方式是 **机器人运行 Gateway、Connector、后台 Agent 和机器人服务，云端提供模型推理**。端侧部署不等于离线推理。开发电脑只负责构建、发布、SSH 转发和查看 Dashboard。也支持把 Gateway/Connector 放在开发电脑运行。

本文对应仓库实现，更新日期为 2026-11-13。声纹仍处于实验阶段，存在当前轮查询竞态、短语音覆盖不足和时间匹配误差，见[已知限制](#9-已知限制)。上游通用框架介绍保留在 [README_UPSTREAM.md](README_UPSTREAM.md) 和 [README_UPSTREAM_ZH.md](README_UPSTREAM_ZH.md)。

## 1. 系统组成与数据流

```text
机器人麦克风 → 厂商 media_audio → audio_service 输入进程 :8093
                                           ├→ PCM → M1 Connector → Gateway :18889
                                           │                         ↕ 云端 Qwen Realtime
                                           └→ CAM++ → 后台逐轮声纹记录
机器人扬声器 ← media_audio ← audio_service 输出 :8091 ← Connector ← 模型音频

Gateway 前台工具 → Connector / M1RobotTools → Dashboard :8080 → 厂商 HTTP / ROS → 机器人
Gateway spawn_thinking → Qwen Code 后台 → M1 MCP → 同一套 M1RobotTools
Dashboard / Agent → vision_service :8092 → 厂商视觉结果
```

| 组件 | 职责 | 源码入口 |
| --- | --- | --- |
| Gateway | 连接实时模型、会话、工具调度、后台任务 | [server/src](server/src/README.md) |
| M1 Connector | 双向 PCM 转发、播放回执、身份后台记录、Agent 状态心跳 | [main.mjs](examples/m1-robot/main.mjs) |
| 实时前台 | 自然对话、直接工具调用、打断、提交长任务 | [机器人身份与交互规则](examples/m1-robot/profile/ASSISTANT.md) |
| Qwen Code 后台 | 持续任务、观察—动作—等待—再观察 | [backend-mcp.mjs](examples/m1-robot/backend-mcp.mjs) |
| audio_service | PCM 输入、播放仲裁、TTS、厂商提示路由、声纹 | [音频服务手册](examples/m1-robot/audio_service/README.md) |
| Dashboard | 状态显示、运动控制与预检查、音视频、服务状态 | [Dashboard 手册](examples/m1-robot/dashboard/README.md) |
| vision_service | 接收厂商人体等检测结果，提供目标跟踪 | [视觉服务手册](examples/m1-robot/vision_service/README.md) |

前台不是把所有请求都交给后台。普通聊天直接回答，有限动作直接调用工具，“找人、持续巡视”等开放任务经 `spawn_thinking` 交给后台。运动工具返回受理或 `operation_id` 不等于物理动作完成，后台用 `m1_wait_for_motion` 等待并重新观察。

## 2. 部署前提

### 2.1 支持的机器人环境

从零部署指在**已有兼容厂商系统的 M1** 上安装开发服务，不包含烧录系统或替代 MCU/底层运动控制器。需满足：

- 能通过 SSH 登录机器人，建议预先配置公钥。
- ARM64 Linux；厂商 ROS 2 Humble、`/middleware/humble/setup.sh`、`/app/joyin` 和控制服务已安装。
- 有与厂商 ROS 兼容的 Python 3.10+，能创建 venv；可写目录为 `/userdata`。
- 厂商媒体进程可提供 16 kHz 单声道 PCM；厂商控制服务通常监听 `127.0.0.1:12321`。
- 机器人能访问模型服务、软件包和证书链；有匹配模型/地域的 API Key。
- 系统提供 Bash、tar、curl、sha256sum、flock；TTS 解码还需 ffmpeg。

在机器人检查：

```bash
uname -m
python3 --version
test -r /middleware/humble/setup.sh
test -d /app/joyin
test -w /userdata
command -v bash curl tar sha256sum flock ffmpeg
```

**媒体域必须一致。** 仓库默认 audio_service 跨接 ROS domain 0 与私有媒体 domain 43；机器人 `media_audio` 也必须运行在 domain 43。未迁移的厂商系统可能仍在 domain 0，此时不能直接套用默认配置。先阅读[音频服务手册的媒体私有域说明](examples/m1-robot/audio_service/README.md)，检查 [厂商启动补丁](examples/m1-robot/deploy/media-audio-private-domain.patch)，在兼容且允许修改的系统上完成迁移。不要对不同版本固件盲目应用补丁；`/app` 只读时需通过该固件支持的持久化机制处理。

### 2.2 开发机

安装 Git、Node.js（满足根 package.json 的 engines，已使用过 22.22.2）、npm 10+、SSH/scp、tar。发布脚本使用 PowerShell 7.4+；Linux/macOS 可安装 `pwsh` 后使用相同脚本。

```bash
git clone --branch feat/m1-robot --single-branch https://github.com/zh-qian2004/qwen-audio-agent.git
cd qwen-audio-agent
npm ci
npm run build
```

后文把机器人地址写成 `YOUR_ROBOT_IP`，需要替换为实际 IP。首次用 `ssh root@YOUR_ROBOT_IP` 核对主机指纹并配置公钥；发布脚本使用 BatchMode，不会交互询问密码。

### 2.3 首次连接：USB / ADB → SSH

尚不能 SSH 登录时，先阅读 [首次连接与 SSH / USB 开发](examples/m1-robot/docs/connection-development.md)。完整流程为：

1. 用数据线接入，检查 `adb devices -l`，按固件流程授权。历史设备使用 `adb shell auth`；它是厂商命令，调试密码与 SSH 登录凭据分别获取。
2. 在授权 shell 中检查 `id`、`ip -4 addr show` 和 `ip route`，通过厂商 App/受支持流程完成 Wi-Fi 配网，确认实际可达地址。
3. 使用局域网 SSH；无可达网络时，设备允许的情况下可用 `adb forward tcp:10022 tcp:22`，再 `ssh -p 10022 root@127.0.0.1`。USB 不一定提供虚拟网卡。
4. 公钥保存在 `/userdata/.ssh/authorized_keys`；核对用户 home 和 `AuthorizedKeysFile`，使用可写入口或固件支持的持久化配置。软链不能绕过只读挂载，已有 `.ssh` 路径应保留。
5. 通过独立终端验证 `BatchMode=yes` 公钥登录，再继续部署。密钥、密码与网络地址不应照抄历史设备的数据。

详细文档还包含 Windows SSH 别名、USB 下 scp/发布、端口转发以及超时、认证失败、IP 变化的排查步骤。

## 3. 安装机器人服务

### 3.1 复制源码

在开发机仓库根目录，用 Bash/Git Bash 打包已提交的服务源码：

```bash
git archive --format=tar.gz --output=m1-services.tar.gz HEAD \
  examples/m1-robot/audio_service examples/m1-robot/dashboard examples/m1-robot/vision_service
scp m1-services.tar.gz root@YOUR_ROBOT_IP:/userdata/
```

以下在机器人上执行。首次安装使用这些目录；升级已有设备前保留原配置、模型和 data，不要用整目录覆盖运行数据。

```bash
mkdir -p /userdata/developer/m1-source /userdata/developer/dashboard
tar -xzf /userdata/m1-services.tar.gz -C /userdata/developer/m1-source
cp -a /userdata/developer/m1-source/examples/m1-robot/audio_service /userdata/audio_service
cp -a /userdata/developer/m1-source/examples/m1-robot/vision_service /userdata/vision_service
cp -a /userdata/developer/m1-source/examples/m1-robot/dashboard/. /userdata/developer/dashboard/
chmod +x /userdata/audio_service/scripts/*.sh
chmod +x /userdata/vision_service/scripts/*.sh
chmod +x /userdata/developer/dashboard/scripts/*.sh
```

这里使用 `/userdata/vision_service`，因为仓库内视觉启停脚本默认这个路径。若改为 `/userdata/developer/vision_service`，需一起调整视觉启停脚本和开机启动入口；端口保持不变即可供 Dashboard 使用。

### 3.2 Python 环境

仍在机器人，使用厂商兼容的 Python，独立 venv，不使用 conda：

```bash
. /middleware/humble/setup.sh
export LD_LIBRARY_PATH=/middleware/pub/lib:/app/joyin/lib:/app/joyin/lib/thirdparty:${LD_LIBRARY_PATH:-}
export PYTHONPATH=/app/joyin/local/lib/python3.10/dist-packages:${PYTHONPATH:-}
python3 -m venv --system-site-packages /userdata/audio_service/.venv
python3 -m venv --system-site-packages /userdata/developer/dashboard/.venv
python3 -m venv --system-site-packages /userdata/vision_service/.venv
/userdata/audio_service/.venv/bin/python -m pip install -r /userdata/audio_service/config/requirements.txt
/userdata/audio_service/.venv/bin/python -m pip install numpy -r /userdata/audio_service/config/speaker-requirements.txt
/userdata/developer/dashboard/.venv/bin/python -m pip install -r /userdata/developer/dashboard/config/requirements.txt
/userdata/vision_service/.venv/bin/python -m pip install -r /userdata/vision_service/config/requirements.txt
/userdata/audio_service/.venv/bin/python -c 'import ssl, rclpy, numpy, sherpa_onnx; print("imports ok")'
```

若 `python3` 不是厂商 3.10 解释器，先换用兼容解释器；不能将另一版本 Python 的 ROS 扩展直接加入 sys.path。

### 3.3 音频密钥与模型

用编辑器在机器人创建 `/userdata/audio_service/config/runtime.env`，内容为自己的 TTS 凭据：

```dotenv
DASHSCOPE_API_KEY=YOUR_TTS_KEY
```

```bash
chmod 600 /userdata/audio_service/config/runtime.env
mkdir -p /userdata/audio_service/models/speaker
```

检查 `config/service.json` 的 `tts.model`、`voice` 和 `base_websocket_api_url`。仓库中的 TTS URL 是开发环境 endpoint，其他用户应替换为自己账号/地域可访问的服务地址；API Key 有效不代表对该 endpoint 有权限。

声纹默认开启，但 ONNX 权重不入库。依据 [sherpa-onnx 官方声纹模型说明](https://github.com/k2-fsa/sherpa-onnx/blob/master/sherpa-onnx/c-api/docs/speaker-embedding.dox) 获取 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx`，复制到：

```text
/userdata/audio_service/models/speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
```

从可信发布源获取并校验文件。暂时不用声纹时，把 `/userdata/audio_service/config/speaker.json` 中 `enabled` 改为 `false`，即可先验证基本语音链路；身份工具会报告不可用。视觉默认使用厂商已有检测结果，不在本仓库分发厂商 `.hbm` 权重。

### 3.4 启动与检查

```bash
bash /userdata/audio_service/scripts/run.sh
bash /userdata/audio_service/scripts/run-input.sh
bash /userdata/vision_service/scripts/run.sh
bash /userdata/developer/dashboard/scripts/run.sh
curl -fsS http://127.0.0.1:8091/api/health
curl -fsS http://127.0.0.1:8093/api/health
curl -fsS http://127.0.0.1:8092/api/health
curl -fsS http://127.0.0.1:8080/api/health
```

`run.sh` 正常启动输出服务后也会启动输入服务；显式再次运行 `run-input.sh` 是幂等检查。确认输入 `online`，不仅是 HTTP 200。使用视觉时，再从 Dashboard 启动检测，或按[视觉启动接口](examples/m1-robot/vision_service/README.md#8-机器人部署)调用 Processor start。

## 4. 安装端侧 Agent

### 4.1 Node.js 与后台 harness

在机器人创建运行目录：

```bash
base=/userdata/developer/audio-agent
mkdir -p "$base"/{config,data,logs,releases,run/uploads,run/tmp,runtime,scripts}
chmod 700 "$base/config" "$base/data" "$base/run"
cd "$base/runtime"
curl -fLO https://nodejs.org/dist/v22.22.2/node-v22.22.2-linux-arm64.tar.gz
curl -fLO https://nodejs.org/dist/v22.22.2/SHASUMS256.txt
grep ' node-v22.22.2-linux-arm64.tar.gz$' SHASUMS256.txt | sha256sum -c -
tar -xzf node-v22.22.2-linux-arm64.tar.gz
ln -s node-v22.22.2-linux-arm64 node
export PATH="$base/runtime/node/bin:$PATH"
node --version
npm install --prefix "$base/runtime/qwen-code-0.27.2" @qwen-code/qwen-code@0.27.2
```

Node 包见 [Node.js 官方归档](https://nodejs.org/dist/v22.22.2/)。上面的版本是已部署方案的固定版本，不表示最新版本。其他系统架构应选择对应包；若固件 libc 不兼容，先解决运行时兼容性。

`qwen-bare.sh` 通过 Qwen Code 的 `--bare` 模式运行后台，避免向只读 `/root/.qwen` 写配置。脚本由下一步发布自动上传。

### 4.2 配置前后台模型

在机器人用编辑器创建 `/userdata/developer/audio-agent/config/edge-credentials.env`：

```dotenv
QWEN_AUDIO_REALTIME_PROVIDER=dashscope
QWEN_AUDIO_REALTIME_MODEL=qwen-audio-3.1-realtime-flash
QWEN_AUDIO_REALTIME_API_KEY=YOUR_REALTIME_KEY
QWEN_AUDIO_AGENT_BACKEND_MODEL=qwen3.8-flash
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=YOUR_BACKEND_KEY
```

```bash
chmod 600 /userdata/developer/audio-agent/config/edge-credentials.env
```

两个 Key 可以相同，前提是该凭据对两个模型都有权限。Realtime 自定义 endpoint 可通过 `QWEN_AUDIO_REALTIME_URL` 指定；模型是否可用以自己的账号与地域为准。运行目录、密钥和会话数据与源码 release 分离。

### 4.3 发布

在开发机仓库根目录执行 PowerShell 7.4+ 命令：

```powershell
# 只在本机打包校验，不访问机器人
./examples/m1-robot/deploy/edge/sync-edge.ps1 -PackageOnly

# 安装并启动端侧 Agent
./examples/m1-robot/deploy/edge/sync-edge.ps1 -Robot root@YOUR_ROBOT_IP -Deploy
```

Linux/macOS 的相同入口：

```bash
pwsh -File examples/m1-robot/deploy/edge/sync-edge.ps1 -Robot root@YOUR_ROBOT_IP -Deploy
```

发布工具从自身位置定位仓库，不依赖特定 Windows 盘符。它读取 [端侧配置](examples/m1-robot/m1-robot.config.edge.json)，打包必要源码和已构建 Web 资源，用独立锁文件安装运行依赖，校验每个文件 SHA-256，然后切换 release。首次安装直接启动；更新失败时尝试回到原 release。`-PackageOnly` 输出包路径与校验值；不传 `-Deploy` 时检查本地与机器人源码差异。

端侧配置中的 `sessionId` 是示例值。新部署时改为自己的会话名；换会话不会清除声纹档案或长期记忆。`QWEN_AUDIO_MEMORY_CONTEXT_ENABLED=false` 在该模板中关闭未按说话人隔离的全局记忆注入。服务只允许一个活动语音 Connector；切换部署方式前先停止旧 Connector。

## 5. 访问、启停、更新和回退

在开发机建立隧道，保持该终端运行：

```bash
ssh -N -L 18080:127.0.0.1:8080 -L 18889:127.0.0.1:18889 root@YOUR_ROBOT_IP
```

- Dashboard：`http://127.0.0.1:18080/`，查看状态、音视频、控制台和 Agent 在线情况。
- Gateway 健康检查：`http://127.0.0.1:18889/api/health`，应看到前台配置、后台 `ready`。
- 语音由机器人的麦克风输入。WebUI 若尝试取得语音所有权，可能与机器人 Connector 冲突；不要为了看日志另启第二个语音客户端。

机器人端启停：

```bash
bash /userdata/developer/audio-agent/scripts/edge-service.sh status
bash /userdata/developer/audio-agent/scripts/edge-service.sh stop
bash /userdata/developer/audio-agent/scripts/edge-service.sh start
```

更新：开发机拉取所需分支，运行 `npm ci`（依赖变更时）和 `npm run build`，再执行 `sync-edge.ps1 -Deploy`。该脚本仅发布 Agent，不覆盖三个 Python 服务、权重和个人档案；服务源码更新需独立部署并重启相应服务。

回退：从 `/userdata/developer/audio-agent/releases/` 选择已有版本名：

```bash
bash /userdata/developer/audio-agent/scripts/activate-release.sh edge-YYYYMMDD-HHMMSS-COMMIT-HASH
```

可选守护：

```bash
bash /userdata/developer/audio-agent/scripts/edge-service.sh supervise-start
bash /userdata/developer/audio-agent/scripts/edge-service.sh supervise-status
```

这只启动本次开机的 supervisor；`stop` 会标记人工暂停，避免守护立即拉起。开机自启需要把服务启动与 `supervise-start` 集成到设备现有 `/userdata/startup.sh`。先阅读仓库[启动示例](examples/m1-robot/deploy/startup.sh)，补上独立输入进程与 Agent 入口，保留已有启动内容；不要用示例覆盖其他项目。该示例与 supervisor 不会由本文命令自动注册到系统启动项。

端口约定：

| 机器人本机端口 | 服务 | 开发机转发示例 |
| --- | --- | --- |
| 8080 | Dashboard | 18080 |
| 8091 | 音频输出、TTS、厂商事件 | 按调试需要转发 |
| 8092 | 视觉服务 | 按调试需要转发 |
| 8093 | PCM 输入、声纹 API/事件 | 按调试需要转发 |
| 18889 | 端侧 Gateway | 18889 |
| 12321 | 厂商控制台 | 通常由 Dashboard 本机调用 |

`18080` 是开发机隧道端口，机器人 Dashboard 仍监听 `8080`。服务默认监听回环地址，公网访问应通过受控隧道。

## 6. 可选：开发机运行 Agent

三个 Python 服务仍放在机器人。开发机安装并配置自己选用的后台 harness，设置 Realtime/后台模型环境变量，复制本地启动配置：

```powershell
Copy-Item examples/m1-robot/m1-robot.config.json examples/m1-robot/m1-robot.config.local.json
# 编辑副本的 ssh.target、私钥路径、端口及 gateway.environment
npm run start:m1-robot -- --config examples/m1-robot/m1-robot.config.local.json
```

副本已被 Git 忽略。使用多人语音时也应在副本 `gateway.environment` 设置 `QWEN_AUDIO_MEMORY_CONTEXT_ENABLED=false`。启动器建立 SSH 转发，启动 Gateway/Connector，并注入后台 MCP；机器人端旧 Agent 需要先停止。详见 [M1 开发指南](examples/m1-robot/README_ZH.md)。

## 7. 验收顺序

1. 查看四个 Python 服务端口健康状态及麦克风 online。
2. 查看 Agent status，确认 Gateway、前台、后台和音频桥就绪。
3. 对机器人说“你叫什么名字”，应回答 Zeroth M1 身份。
4. 测试普通对话与说话中打断；普通聊天不应主动报告声纹分数。
5. 问电量/是否检测到人，检查相应状态或视觉工具被调用。
6. 在适合运动测试的场地执行一个短动作，核对 Dashboard 命令和遥测；充电状态可能被门控拒绝。
7. 声纹另行验收：先注册，再让两位说话人交替发言并查询具体轮次；不能仅凭“模型叫对名字”判定识别成功。

## 8. 工具和实现入口

### 8.1 控制与视觉

模型工具定义、执行、充电/模式检查位于 [robot-tools.mjs](examples/m1-robot/robot-tools.mjs)。前后台复用该实现，Dashboard 负责底层控制和动作生命周期。

| 工具类别 | 主要工具 |
| --- | --- |
| 状态 | m1_get_state、m1_get_motion_status、m1_get_vehicle_status |
| 视觉 | m1_get_vision_status、m1_get_detections |
| 运动 | m1_move、m1_execute_action、m1_list_actions、上下小车等动态动作 |
| 停止/音量 | m1_stop、m1_emergency_stop、m1_set_volume |
| 后台等待 | m1_wait_for_motion（仅后台） |
| 声纹 | m1_get_speaker、m1_manage_speaker |

实际工具清单取决于 Dashboard 的已启用命令。普通动作直接走动作工具，安全检查内置于工具/Dashboard。厂商 HTTP 与 ROS 两条控制链路都是现有硬件接口的适配，详情见 [控制接口清单](examples/m1-robot/docs/control-interface-inventory.md)。

### 8.2 音频、打断和厂商提示

[audio-service-bridge.mjs](examples/m1-robot/audio-service-bridge.mjs) 将输入 PCM 转发至 Gateway，将模型音频流发回 `/ws/audio/output`，以真实播放回执完成响应生命周期。全双工使用独立输入进程；打断会取消旧播放并释放统一仲裁租约。

普通厂商提示由 [vendor-event-bridge.mjs](examples/m1-robot/vendor-event-bridge.mjs) 处理，按语义与优先级路由；原始编号不应成为用户回复。Gateway 不可用时，音频服务按策略回退到厂商提示。

### 8.3 声纹

audio_service 中的 [speaker_identity.py](examples/m1-robot/audio_service/src/speaker_identity.py) 负责 CAM++ embedding、多样本注册、质量门限与事件。Connector 的 [speaker-identity.mjs](examples/m1-robot/speaker-identity.mjs) 接入服务；[speaker-turn-store.mjs](examples/m1-robot/speaker-turn-store.mjs) 保存按 session 隔离、最多 200 轮的后台记录。

记录文件为 `/userdata/developer/audio-agent/run/m1-speaker-turns.json`，前后台工具读取同一份记录。默认不向 Realtime 注入额外声纹文本。

`m1_get_speaker` 支持 current、previous、turn_id 或按历史原话 search，返回：

| status | 含义 |
| --- | --- |
| verified | 该轮有匹配到注册档案的证据 |
| unknown | 无法确认身份，包含证据不足或冲突 |
| pending | 尚在等待处理/轮次结束 |
| unavailable | 没有目标记录、关联失败或服务不可用 |

用户说“注册新说话人，名字叫豆包”后，`m1_manage_speaker(action=enroll, display_name=豆包)` 自动分配 ID；服务累计默认 6 秒有效人声，分成 3 份 embedding 后保存中心，未必是 3 次独立发言。开始采集不等于注册完成，完成情况通过档案列表确认。权重和私人档案不入库。

## 9. 已知限制

- 声纹尚未实现稳定的流式多人分离，也没有自动为陌生人建立连续身份；多人同时说话不能保证正确区分。
- 当前轮工具可能早于最终转写执行，而记录用转写到达判断轮次结束，可能错误返回 pending；追问若改查 current，会查到追问本身。固定目标 turn_id、结束事件解耦与有限等待的优化尚未实施。
- 本地声纹分段与云端话轮使用时间窗口关联；长短片段、回声与延迟会影响归属，不能将前一位身份直接继承到下一位。
- 识别需要足够的有效人声；短句、低 SNR 和未经校准的阈值会导致 unknown。较长发言还需等分段结束再推理，暂不能承诺实时识别。
- 当前短片段累计没有完善的同人连续性确认；质量判断、注册样本一致性与真实麦克风阈值仍需进一步验证。
- 视觉工具返回检测/跟踪数据，不提供通用图像理解。固件版本、厂商消息类型和电机接口差异需要适配。
- 密钥、模型权重、固件、厂商 Python/ROS 包不包含在仓库内；这些是明确的外部前提。

## 10. 日志、排障与轻量检查

机器人上的常用文件：

```text
/userdata/developer/audio-agent/logs/agent.log
/userdata/developer/audio-agent/run/logs/gateway.log
/userdata/developer/audio-agent/run/sessions/       # 会话事件，owner/session 编码目录
/userdata/developer/audio-agent/run/m1-speaker-turns.json
/userdata/developer/audio-agent/logs/supervisor.log # 启用守护时
/userdata/audio_service/logs/input_service.log
/userdata/audio_service/logs/audio_service.log
/userdata/developer/dashboard/logs/dashboard.log
/userdata/vision_service/logs/vision_service.log
```

| 现象 | 先检查 |
| --- | --- |
| 有声音转写却不回答 | Gateway 连接、后台状态、工具错误及当前 session |
| 没有麦克风输入 | :8093 online、media_audio 所在 ROS 域、私有域桥、是否双 Connector |
| 听不到播放/不能打断 | :8091 仲裁/输出状态、media_audio 日志、取消回执、halfDuplex=false |
| 401/403 或模型连接失败 | Key 是否对模型和 endpoint 有权限；TTS 与 Realtime 不是同一个接口 |
| Python import ssl/rclpy 失败 | 厂商 Python 版本、ROS setup、LD_LIBRARY_PATH、证书 |
| ffmpeg 缺系统库 | 独立检查 ffmpeg 依赖；服务会为转换子进程清理部分动态库环境 |
| WebUI 显示被占用 | 机器人 Connector 已取得语音所有权；查看状态不需要再抢麦克风 |
| 声纹一直 pending | 固定原 turn_id；核对转写结束时间与后台已有结果，见已知限制 |
| 声纹 unknown | 查看 too_short、low_snr、low_score 等原因，不把缺证据当作名字 |

小改动优先运行定向检查：

```bash
node --test examples/m1-robot/test/speaker-identity.test.mjs examples/m1-robot/test/robot-tools.test.mjs
node --check examples/m1-robot/main.mjs
```

发布脚本本地打包校验使用 `-PackageOnly`；不会连接或重启机器人。音频处理改动才在兼容的机器人 Python 环境运行相关 pytest 文件；完整回归命令见各组件手册。HTTP ready 和单元测试不能代替真人语音、多人轮换与物理动作验收。

## 11. 文档导航与项目来源

- [M1 配置与开发指南](examples/m1-robot/README_ZH.md)
- [端侧发布脚本说明](examples/m1-robot/deploy/edge/README.md)
- [既有设备部署快照](examples/m1-robot/deploy/README.md)：历史实机记录，不是其他机器的默认地址。
- [音频服务](examples/m1-robot/audio_service/README.md)、[Dashboard](examples/m1-robot/dashboard/README.md)、[视觉服务](examples/m1-robot/vision_service/README.md)
- [上游架构详解](docs/architecture/deep-dive.zh.md)、[前台模型配置](docs/configuration/frontend.zh.md)、[后台配置](docs/configuration/backend.zh.md)

本项目基于 [QwenAudio/qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent)，保留原项目 [LICENSE](LICENSE)。本分支增加机器人集成，未替代厂商底层控制器。提交代码时排除 Key、SSH 私钥、录音、个人声纹、运行日志、venv、node_modules 和发布包；运行资料统一放在 `/userdata`。
