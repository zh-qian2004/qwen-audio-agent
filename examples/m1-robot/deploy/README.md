# M1 端侧 Agent 部署记录

截至 2026-11-12，当前机器人运行的是**端侧** qwen-audio-agent：Gateway、M1 Connector 与 Qwen Code 后台均运行在 `/userdata/developer/audio-agent`。前台使用 `qwen-audio-3.1-realtime-flash`，后台使用 `qwen3.8-flash`；模型请求仍需机器人访问云端。Windows 上原有的云端 Connector 已停止，Dashboard 的 Agent 状态显示 `deployment_mode=edge`。

| 项目 | 当前值 |
| --- | --- |
| 机器人 | `root@192.168.137.132` |
| 活动版本 | `edge-20261112-151631-da525de-090ff110` |
| 源码提交 | `da525de37f828398794e695624e1aa104cf6d456` (`feat/m1-robot`，含未提交的说话人 Harness 改动) |
| Gateway | 机器人本机 `127.0.0.1:18889` |
| 音频服务 | `/userdata/audio_service` |
| Dashboard | `/userdata/developer/dashboard` |
| 视觉服务 | `/userdata/developer/vision_service` |

发布版本在 `/userdata/developer/audio-agent/releases/`，`run/active-release` 指向当前版本。每个版本的 `release-manifest.json` 记录 Git 提交、文件校验和源码树哈希。当前源码树哈希为 `090ff110e3377278b71a1e7d8348572e6cbbefbb16ea01bfd9320fc2240e87e1`，manifest 标记 `sourceDirty=true`。后台声纹查询启用干净 session `debug-20261112-2310`。

模型只暴露 `m1_get_speaker` 和 `m1_manage_speaker` 两个说话人工具，并订阅 `127.0.0.1:8093/ws/speakers/events`。识别结果存入后台 `run/m1-speaker-turns.json`，不再向模型注入声纹文本。前后台工具按具体轮次查询 verified、unknown、pending 或 unavailable；记录按 session 隔离。多片段声纹注册由 audio_service 自动完成，运行指标与阈值诊断留在 Dashboard/服务状态接口。

在有机器人 SSH 访问权限的机器上查看状态：

```sh
ssh root@192.168.137.132 'bash /userdata/developer/audio-agent/scripts/edge-service.sh status'
```

端侧服务由 `edge-service.sh` 的 `start|status|stop` 管理；目前尚未加入机器人开机自启。切换 Agent 前应确认音频输入没有另一个 Connector 占用。Dashboard、`audio_service` 和 `vision_service` 是独立服务，Agent 发布不会覆盖或重启它们。

这台机器人的发布和回退脚本目前保存在本地 `D:\projects\m1-robot\deploy\audio-agent-edge\`，其中 `sync-edge.ps1` 会校验和部署 Agent 版本；**该脚本尚未纳入本仓库**。本目录中的 `startup.sh` 和厂商补丁用于机器人服务，不是端侧 Agent 发布脚本。模型 API Key、说话人模型和登记数据保存在机器人私有目录，不随 Git 同步。

较完整的部署过程、历史验证结果与本机发布命令见本地 `D:\projects\m1-robot\deploy\audio-agent-edge\README.md`。
