# M1 首次连接与 SSH / USB 开发

本文补充根 [部署总手册](../../../README.md)：从首次 USB 接入、网络定位到 SSH 公钥和远程端口转发。长期开发优先使用 SSH，USB 用于首次接入或网络失联时恢复。

适用范围：已有兼容厂商固件、已获设备开发授权的 M1。`adb shell auth` 来自本项目历史设备的厂商调试流程，不能保证所有固件都有此命令。机器人用户通常为 `root`，但用户名、系统密码、调试密码均应由设备所有者或厂商提供；本仓库不包含这些凭据。2026-11-13 整理文档时，历史 IP 连接超时，没有再次验证该设备的当前网络或 SSH 配置。

## 1. 三种连接方式

| 方式 | 前提 | 适合用途 |
| --- | --- | --- |
| 局域网 SSH | 机器人有可达的 Wi-Fi/以太网 IP，SSH 服务在线 | 日常开发、scp、发布和端口转发 |
| USB → ADB | USB 数据线、驱动、厂商开放 adbd 和调试授权 | 检查系统、查询 IP、恢复网络 |
| USB → ADB forward → SSH | ADB 已可用，机器人本机 SSH 端口可达 | 无 Wi-Fi 时沿用 SSH/scp 工作流 |

USB 插线不代表已建立网络。ADB、USB 虚拟网卡（如 RNDIS/ECM）与 SSH 是不同机制；设备是否提供 USB 网卡及其地址取决于固件。不要假设它必定叫 `usb0` 或使用某个固定 IP。

## 2. 开发机准备

安装 OpenSSH 客户端及 [官方 Android SDK Platform Tools](https://developer.android.com/tools/releases/platform-tools)。仅使用 ADB 不需要安装完整 Android Studio。

Windows PowerShell：

```powershell
ssh -V
adb version
Get-Command ssh, scp, adb
```

如果 adb 未加入 PATH，可在 Platform Tools 解压目录使用 `./adb.exe`。以下示例中 `SERIAL`、`YOUR_ROBOT_IP` 均是需要替换的值；Linux/macOS 命令相同，Windows 专属诊断另外标注。

## 3. 首次 USB / ADB 接入

### 3.1 确认设备

使用支持数据传输的线缆连接厂商指定调试口，机器人开机后执行：

```bash
adb start-server
adb devices -l
```

| 结果 | 下一步 |
| --- | --- |
| 没有设备 | 检查数据线、调试口、Windows 设备管理器和厂商 ADB 驱动 |
| unauthorized | 完成该固件的设备端/厂商调试授权；Linux 机器人不一定有 Android 弹窗 |
| offline | 检查线缆和设备状态，必要时重新插线或重启本机 ADB server |
| device | 可以继续尝试 shell；不代表已获得操作系统 root 权限 |
| 多个设备 | 后续命令使用 `adb -s SERIAL ...` 明确目标 |

`adb kill-server` 会影响本机所有 ADB 会话，只在确认可中断时使用，再执行 `adb start-server`。

### 3.2 厂商调试认证与权限确认

本项目历史设备使用：

```bash
adb -s SERIAL shell auth
# 按厂商提示交互输入调试密码
adb -s SERIAL shell
```

进入机器人 shell 后检查：

```bash
id
whoami
uname -a
pwd
```

`auth` 是厂商命令，不是标准 ADB 子命令；其授权范围和持久性需要按固件确认。若提示 `buildroot` 用户密码，说明设备还有自己的认证层，不能用“ADB 已显示 device”代替登录认证。不要把密码放进 shell 参数、启动 JSON 或 Git。

ADB 是传输/调试通道，`root` 是机器人系统身份。标准 `adb root` 请求设备上的 adbd 以 root 运行，能否成功由固件决定；它与 `adb shell auth` 及 SSH 的 root 登录不是同一个认证过程。以当前 shell 的 `id` 为准，部署本文服务不要求一律先运行 `adb root`。

## 4. 获取 IP、配网并切换 SSH

### 4.1 查询机器人网络

在已经授权的机器人 shell 中：

```bash
ip -4 addr show
ip route
```

没有 `ip` 时可尝试 `ifconfig`、`route -n`。从实际无线/以太网接口读取非回环地址，结合默认路由判断机器人当前使用哪个网络。`127.0.0.1` 是机器人自己，不是开发机可直连的机器人地址。

首次配网优先使用厂商 App；也可连接开发电脑共享的 Wi-Fi 热点。热点连接列表/路由器 DHCP 租约可用于查 IP。Windows 热点常见 `192.168.137.0/24`，但网段和机器人地址都不保证固定；历史资料中的 `.132`、`.228` 不是通用配置。

本仓库没有适配所有固件的统一 Wi-Fi 管理命令。通过 USB 配网前先确认设备使用的网络管理服务，不能假设存在 `nmcli`，也不要直接覆盖厂商网络配置文件。

### 4.2 判断 SSH 是否可达

Windows：

```powershell
Test-NetConnection YOUR_ROBOT_IP -Port 22
```

再尝试交互登录：

```bash
ssh root@YOUR_ROBOT_IP
```

首次登录核对主机指纹；可以通过已授权的 USB shell 或厂商渠道对比设备 SSH 主机公钥指纹。不能只为消除报错关闭主机校验。机器人可能禁止 ICMP，`ping` 不通并不能证明 SSH 不通。

如果双方不在可互通局域网，机器人位于热点 NAT、4G 或受隔离内网中，仅“分配了某个端口”不代表开发机能访问。需要实际路由、受控的反向隧道或双方可达的组网方案。USB 可用时可先走下一节，不必开放公网 SSH。

## 5. 没有 USB 网卡时：通过 ADB 转发 SSH

这是标准 [ADB TCP 转发](https://developer.android.com/tools/adb#forwardports)的用法，需要设备端 adbd 允许转发，且机器人 SSH 服务在本机 22 端口监听。它不要求 USB 虚拟网卡，也不依赖 Wi-Fi；本次文档整理未对当前设备重新实测。

在开发机执行：

```bash
adb -s SERIAL forward tcp:10022 tcp:22
adb forward --list
ssh -p 10022 root@127.0.0.1
```

这里 `127.0.0.1:10022` 是开发机的转发入口，连接最终仍需机器人 SSH 用户认证。ADB 认证不会自动替你完成 SSH 登录。若转发存在但 SSH refused，检查机器人 `sshd/dropbear` 是否在运行、实际端口是多少、监听地址是否包含 ADB 转发的本机目标。

复制文件时 scp 使用大写 `-P`：

```bash
scp -P 10022 ./m1-services.tar.gz root@127.0.0.1:/userdata/
```

若精简固件缺少 SFTP 子系统，新版 scp 可能失败；先确认错误原因，再尝试兼容旧协议的 `scp -O -P 10022 ...`。结束后仅移除本次转发：

```bash
adb -s SERIAL forward --remove tcp:10022
```

拔掉 USB 会中断这条 SSH 链路，重新插线后可能需要重新授权和创建 forward。USB 只解决开发连接，不会自动给机器人提供互联网；云端模型调用仍需机器人可出站联网。

## 6. 只读根文件系统下的 SSH 公钥

### 6.1 生成开发机密钥

在开发机执行，已有专用密钥时直接复用，遇到同名文件不要覆盖：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/m1_robot_ed25519 -C m1-development
```

私钥留在开发机，只有 `.pub` 公钥复制到机器人。密钥有 passphrase 时，通过本机 `ssh-agent`/`ssh-add` 解锁；发布脚本的 BatchMode 不能交互询问 passphrase。

### 6.2 确认实际用户目录与 sshd 配置

在机器人 shell 中执行：

```bash
awk -F: '$1 == "root" {print "root home:", $6}' /etc/passwd
ls -ld /root /root/.ssh /userdata/.ssh 2>/dev/null
readlink /root/.ssh
mount
command -v sshd
```

如果使用 OpenSSH，执行 `sshd -T` 查看 `authorizedkeysfile`、`pubkeyauthentication`、`permitrootlogin`、`port`、`listenaddress`。实际程序可能不在 PATH，需要使用设备上的绝对路径；Dropbear 的配置和启动参数不同，不能直接套用 sshd 命令。

本项目采用的布局为：

```text
/userdata/.ssh/authorized_keys   # 持久化公钥
<root 实际 home>/.ssh           # 链接到 /userdata/.ssh
```

通常 home 是 `/root`，应以 `/etc/passwd` 为准。已有正确链接可直接复用；如果 `AuthorizedKeysFile` 已指向 `/userdata/.ssh/authorized_keys`，就不需要额外链接。

**软链不能绕过只读挂载。** 创建 `<home>/.ssh` 仍要求 home 的父目录可写。若只读且没有预置链接，使用厂商支持的可写 overlay/启动钩子，或让现有 OpenSSH 启动配置持久使用 `AuthorizedKeysFile=/userdata/.ssh/authorized_keys`。这属于具体固件适配，不能只用一个 `ln -s` 宣称已解决；不要为公钥认证直接重挂整个根文件系统。

### 6.3 导入公钥并设置权限

先在机器人已授权 shell 中创建目录：

```bash
umask 077
mkdir -p /userdata/.ssh
chmod 700 /userdata/.ssh
```

用 USB 上传**公钥**，示例在 Windows PowerShell：

```powershell
adb -s SERIAL push "$env:USERPROFILE/.ssh/m1_robot_ed25519.pub" /userdata/m1_robot_ed25519.pub
```

已经能密码 SSH 登录时，也可用 scp 代替 ADB 上传。再在机器人 shell 中合并，保留所有已有公钥：

```bash
[ ! -s /userdata/.ssh/authorized_keys ] || printf '\n' >> /userdata/.ssh/authorized_keys
tr -d '\r' < /userdata/m1_robot_ed25519.pub >> /userdata/.ssh/authorized_keys
printf '\n' >> /userdata/.ssh/authorized_keys
chown root:root /userdata/.ssh /userdata/.ssh/authorized_keys
chmod 700 /userdata/.ssh
chmod 600 /userdata/.ssh/authorized_keys
```

公钥应为单行 OpenSSH 格式；只需导入一次，反复追加会形成重复行。上述命令为原文件先补换行，避免与已有最后一行粘连；空白行不影响公钥认证。

仅当实际 home 可写且 `.ssh` 路径尚不存在时建立链接：

```bash
robot_home=$(awk -F: '$1 == "root" {print $6}' /etc/passwd)
if [ -n "$robot_home" ] && [ -d "$robot_home" ] && [ -w "$robot_home" ] \
   && [ ! -e "$robot_home/.ssh" ] && [ ! -L "$robot_home/.ssh" ]; then
  ln -s /userdata/.ssh "$robot_home/.ssh"
else
  echo '请核对现有 .ssh、用户目录可写性和 AuthorizedKeysFile；本步骤没有覆盖现有路径'
fi
```

已有 `.ssh` 目录不能直接替换。先查看内容与权限，保留原 `authorized_keys` 和配置，再由设备维护者选择迁移或配置单独的 AuthorizedKeysFile。root 身份本身也不能写入只读挂载。

### 6.4 验证免交互登录

保留当前 USB/密码会话，在另一个开发机终端验证：

```bash
ssh -i ~/.ssh/m1_robot_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes root@YOUR_ROBOT_IP 'id; echo SSH_KEY_OK'
```

USB 转发则增加 `-p 10022` 并把目标改为 `root@127.0.0.1`。出现 `SSH_KEY_OK` 才说明发布脚本能够复用该认证路径；重启后也应再次验证持久性。不要为了验收先关闭密码认证，以免将自己锁在设备外。

## 7. SSH 别名、端口转发与开发

在开发机 `~/.ssh/config`（Windows 为 `%USERPROFILE%\.ssh\config`）追加自己的配置，保留已有内容：

```sshconfig
Host m1-robot
    HostName YOUR_ROBOT_IP
    User root
    IdentityFile ~/.ssh/m1_robot_ed25519
    IdentitiesOnly yes
    ServerAliveInterval 15
    ServerAliveCountMax 3

Host m1-usb
    HostName 127.0.0.1
    Port 10022
    User root
    IdentityFile ~/.ssh/m1_robot_ed25519
    IdentitiesOnly yes
```

通过别名使用与发布：

```bash
ssh m1-robot
scp ./m1-services.tar.gz m1-robot:/userdata/
```

```powershell
./examples/m1-robot/deploy/edge/sync-edge.ps1 -Robot m1-robot -Deploy
# USB 已 forward 时也可指定 -Robot m1-usb，ssh/scp 都会读取别名端口
```

Dashboard 和端侧 Gateway 转发：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 18080:127.0.0.1:8080 -L 18889:127.0.0.1:18889 m1-robot
```

USB 时将末尾目标改为 `m1-usb`。`-L` 右侧的 `127.0.0.1` 指机器人本机；左侧 18080 是开发机端口。因此浏览器打开 `http://127.0.0.1:18080/`，机器人 Dashboard 实际仍在 8080。

连接器在开发机运行时，按 [M1 开发指南](../README_ZH.md) 的本地 JSON 副本设置 `ssh.target=m1-robot`、`identityFile` 和端口转发，或使用别名自动读取身份设置。与端侧 Agent 切换时先停止旧 Connector，避免双客户端竞争麦克风和 Gateway 语音所有权。

## 8. 断连与恢复

| 现象 | 排查方向 |
| --- | --- |
| USB 仅充电，adb 无设备 | 数据线、调试口、驱动、厂商是否开放调试 |
| auth 不存在/认证失败 | 固件版本与开发权限；使用厂商提供的流程和凭据 |
| USB 有设备但没有网卡 | 可以只提供 ADB；尝试 ADB forward SSH，不猜 USB IP |
| SSH timeout | IP 改变、热点关闭、路由/客户端隔离、内网不可达 |
| SSH refused | 主机可达但目标端口没有 SSH 服务，或端口配置错误 |
| Permission denied (publickey) | 用户、选用私钥、agent 解锁、authorized_keys、目录权限和 sshd 生效配置 |
| Read-only file system | 将运行资料写入 /userdata；用户目录软链仍需合法可写入口 |
| 主机指纹变化 | 先确认设备/固件及指纹，再移除对应旧记录；不要清空整个 known_hosts |
| bind: Address already in use | 开发机转发端口被占用；关闭自己的旧隧道或换本地端口 |
| USB 拔插后 SSH 断开 | 重建 adb forward，再连接；机器人服务是否仍运行需单独检查 |
| scp 报 subsystem request failed | 目标缺少 SFTP；确认后尝试 scp -O |
| Dashboard 可访问但 Agent 模型断连 | 开发隧道只证明局部可达；另查机器人 DNS、HTTPS、模型权限和证书 |

联网变化后先用 USB 查询 IP/路由，再更新 `HostName`；SSH 保活不能恢复已变更的 IP。必要时使用 `ssh -vv m1-robot` 收集认证诊断，分享前去除用户名、IP 和本机路径等不需要公开的信息。

进一步参考：[ADB 官方文档](https://developer.android.com/tools/adb)、[OpenSSH 手册索引](https://www.openssh.com/manual.html)。设备特有授权、Wi-Fi 管理及只读固件持久化仍以厂商说明为准。
