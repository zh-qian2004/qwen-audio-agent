# 端侧 Agent 发布工具

完整部署步骤见仓库根 [README.md](../../../../README.md)。这些脚本只管理 `/userdata/developer/audio-agent` 下的 Agent，不发布 Python 服务，也不配置密钥或固件。

## 文件

- `sync-edge.ps1`：PowerShell 7.4+ 打包、校验、上传与切换，默认从脚本位置定位仓库。
- `runtime/`：已验证的精简运行依赖清单/锁文件、release 校验器与健康检查。
- `install-release.sh` / `activate-release.sh`：机器人端安装与切换，更新启动失败时尝试回退。
- `edge-run.sh` / `qwen-bare.sh`：可写运行目录和 Qwen Code `--bare` 启动入口。
- `edge-service.sh` / `edge-supervisor.py`：启动、状态、停止与可选守护。

## 使用

在仓库根执行，先 `npm ci` 和 `npm run build`：

```powershell
./examples/m1-robot/deploy/edge/sync-edge.ps1 -PackageOnly
./examples/m1-robot/deploy/edge/sync-edge.ps1 -Robot root@YOUR_ROBOT_IP
./examples/m1-robot/deploy/edge/sync-edge.ps1 -Robot root@YOUR_ROBOT_IP -Deploy
```

`-PackageOnly` 不访问机器人，在本目录 `.sync-work/` 输出归档与 SHA-256；默认模式比较远端 manifest。可传 `-CloudRepo` 指定其他 checkout、`-KnownHostsFile` 指定 SSH known_hosts，默认使用用户的 SSH 配置。首次连接应先手动核对主机指纹。

源端侧配置为 `examples/m1-robot/m1-robot.config.edge.json`。运行时依赖使用 `runtime/package-lock.json`，不使用根目录桌面开发依赖。发布不会同步源码目录里的密钥、data、录音和 node_modules；也不会上传整个开发工作区。

机器人端需要事先创建 config/data/logs/releases/run/runtime/scripts 目录，安装 Node ARM64 和指定版本 Qwen Code，并配置私有 `edge-credentials.env`。首次启动失败没有旧 release 可回退；修复环境后可执行 `edge-service.sh start`。只有后续更新才有旧版本回退路径。

开发机脚本保留发布归档以便复查；`.sync-work/` 不入库。发布文件只做独立版本安装，现有配置、个人数据和服务运行状态应通过各自的管理接口维护。
