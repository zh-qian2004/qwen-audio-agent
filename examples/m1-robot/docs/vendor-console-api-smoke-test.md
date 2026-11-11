# 厂商 Web 控制台 API 冒烟测试报告

- 目标：`http://192.168.137.228:12321`
- 机器人端服务：`/app/joyin/bin/plugman /app/joyin/config/utils_pm.json`（插件 `utils_webctl_plugin`）
- 测试时间：2026-11-09 22:51:11 ~ 23:00:31（Asia/Shanghai）
- 测试方式：从操作机 `192.168.137.1` 直连 12321；SSH 仅用于读取进程、配置与插件日志
- 请求总数：39 条（原始记录 `artifacts/vendor-console-api-smoke-test/requests.jsonl`）
- 单请求超时：8000 ms

## 0. 前置记录

| 项目 | 记录值 |
|---|---|
| 当前时间 | 2026-11-09 22:51:11 +08:00（机器人端 22:55:24 +08:00，时钟一致） |
| 机器人 IP | 192.168.137.228/24（wlan0） |
| 12321 端口 | TCP 可连通（TCP 握手成功；ICMP ping 被丢弃，属正常策略） |
| `utils_webctl_plugin` 进程 | pid 1614，`/app/joyin/bin/plugman /app/joyin/config/utils_pm.json`，uid=gid=0(root)，28 线程，监听 `0.0.0.0:12321` |
| 插件二进制 | `/opt/joyin/lib/libutils_webctl_plugin.so`，1 748 544 字节，2026-10-16 19:08 |
| 机器人是否静止 | 是。左右轮速原始值均为 0，IMU 角速度 `{x:0,y:0,z:0}` |
| 电池 | 电量 83–84%，11.82 V，1629 mA，未充电（`is_charging=false`） |
| Dashboard 状态 | `http://127.0.0.1:8080` 正常，pid 12741，`status=ok, ros=online`，45 节点 / 248 话题 |
| 活动任务 | 未见导航或任务活动。厂商日志显示测试前 22:53:20 有 3 次 `MOTION_STOP`、22:54:53 `HEAD_UP`、22:55:16 `HEAD_RESET`，来自其他客户端，不属于本轮测试 |

> 说明：`ps` 的宽行输出在该系统上不可用，进程信息改用 `/proc/*/cmdline` 扫描获得；`curl` 缺少 `libcurl.so.4`，机器人端改由 `busybox wget` 读取 Dashboard。

## 1. 总结表

| 接口 | 方法 | HTTP状态 | 响应格式 | 延迟 | 分级 | 状态影响 | 结论 |
|---|---|---:|---|---:|---|---|---|
| `/` | GET | 200 | HTML 50 845 B | 132 ms | functional | 无 | 控制台首页正常，标题「智能机器人控制系统」 |
| `/index.html` | GET | 200 | HTML | 15 ms | functional | 无 | 静态目录挂载正常，内容与 `/` 一致 |
| `/api/battery` | GET | 200 | JSON | 52–57 ms | functional | 无（只读） | 字段完整，timestamp/runtime 随调用前进 |
| `/api/status` | GET | 200 | JSON `null` | 48–60 ms | handler_responded | 无 | 处理器存在，但状态内容未实现，三次调用恒为 `null` |
| `/api/control` | POST | 200 | JSON | 101–109 ms | handler_responded | MOTION_STOP：无变化（原本静止） | 请求解析与处理器均正常；**不校验命令名**，未知命令也返回 success |
| `/api/execute_command` | POST | 200 | JSON | 112–124 ms | handler_responded | 无持久化副作用 | 处理器执行了 shell 命令并返回 return_code；**不回传 stdout/stderr** |
| `/ota/mcu` | POST | 400 | JSON | 138–278 ms | request_parsed | 无（未落盘） | multipart 解析与参数校验均正常，缺文件/扩展名错误返回明确 JSON |
| `/api/emergency_stop` | POST（推断） | — | — | — | not_tested | — | 路由字符串存在于插件；按用户选择未发送请求，避免非预期实机急停 |
| `/api/reset_emergency` | POST（推断） | — | — | — | not_tested | — | 同上，需与急停成对验收 |
| `/statistics` | GET | 404 | 空 | 29 ms | route_missing | 无 | 二进制中有该字符串，但路由未注册 |

分级统计（39 条请求）：functional 4、handler_responded 6、request_parsed 10、route_responded 2、route_missing 17、unreachable 0。

## 2. 每个接口详情

### 2.1 `GET /` 与 `GET /index.html`

请求：

```http
GET / HTTP/1.1
Host: 192.168.137.228:12321
```

响应：

```http
HTTP/1.1 200 OK
Keep-Alive: timeout=5, max=100
Content-Length: 50845
Content-Type: text/html
```

检查结果：

| 检查项 | 结果 |
|---|---|
| HTML 标题 | 智能机器人控制系统 |
| 页面长度 | 50 845 字节（Content-Length） |
| 含 `/api/control` | 是（2 处） |
| 含 `/api/battery` | 是（1 处） |
| 含 `/api/execute_command` | 是（1 处） |
| 含 `/ota/mcu` | 是（1 处） |
| 含 `/api/status`、`/api/emergency_stop`、`/api/reset_emergency` | 否（页面未使用这三个路由） |

`/index.html` 与 `/` 内容一致，说明 `dir: /opt/joyin/config` 通过静态目录挂载提供。目录穿越探测 `GET /../etc/passwd` 返回 404，未发现任意文件读取。

状态影响：无。是否适合 Dashboard 调用：否（`status_only`，仅在健康检查里记录可达性）。

### 2.2 `GET /api/battery`

请求：`GET /api/battery`

连续两次响应（间隔约 1.3 s）：

```json
{"current":1631,"power":84,"runtime":2040.22,"timestamp":1794236221.652486,"voltage":11856}
{"current":1632,"power":84,"runtime":2041.54,"timestamp":1794236222.9684353,"voltage":11856}
```

字段解释：

| 字段 | 观测值 | 解释 |
|---|---|---|
| `power` | 84 | 电量百分比（整数） |
| `voltage` | 11856 | **毫伏**，即 11.856 V（不是 11856 V，也不是厘伏） |
| `current` | 1631 | **毫安**，即 1.631 A，正值为放电 |
| `runtime` | 2040.22 | 与 `/proc/uptime` 同一时刻数值一致，实际是**系统运行秒数**，不是剩余续航；插件中确有 `/proc/uptime` 字符串 |
| `timestamp` | 1794236221.65 | Unix epoch 秒（浮点），对应 2026-11-09 22:57:01 |

验证：JSON 可解析；电压 11.856 V 与 Dashboard 由 ROS `hardware/Bat` 话题得到的 11.816 V 一致（毫伏→伏换算）；电量 84% 与 Dashboard 83% 相差 1，属采样时刻差异；两次调用 `timestamp` 与 `runtime` 均前进 1.3 s，说明为实时读数而非缓存。

状态影响：无。是否适合 Dashboard 调用：**适合**，建议 `direct_backend_call`，作为 ROS 电池话题的交叉校验源。

### 2.3 `GET /api/status`

请求：`GET /api/status`；三次调用（22:57、22:57、22:59）响应均只有 4 个字节：

```json
null
```

- HTTP 200、`Content-Type: application/json`、正文为字面量 `null`；
- 说明路由与处理器存在，但内部状态对象从未被赋值；
- 由于恒为 `null`，**无法**验证其是否随机器人状态变化。

分级：`handler_responded`，状态功能尚未提供有效内容。

是否适合 Dashboard 调用：否（`status_only`，不建议依赖）。

### 2.4 `POST /api/control`

未知命令（唯一时间戳）：

```http
POST /api/control
Content-Type: application/json

{"command":"API_PROBE_NOOP_20261109225701"}
```

```json
{"command":"API_PROBE_NOOP_20261109225701","status":"success","timestamp":1794236223.6441123}
```

已知命令（机器人静止）：

```json
{"command":"MOTION_STOP"}
```

```json
{"command":"MOTION_STOP","status":"success","timestamp":1794236224.492861}
```

服务端日志（`/userdata/log/utils/utils_webctl_plugin.log`）：

```text
[I][2026-11-09 22:57:03.644][plugin.cc:745] Received command: API_PROBE_NOOP_20261109225701
[I][2026-11-09 22:57:03.644][plugin.cc:72] command handle:API_PROBE_NOOP_20261109225701
[I][2026-11-09 22:57:04.492][plugin.cc:745] Received command: MOTION_STOP
[I][2026-11-09 22:57:04.492][plugin.cc:72] command handle:MOTION_STOP
```

关键结论（未知命令与已知命令分开判定）：

| 场景 | HTTP | `status` | `message` | 是否明确报告未知命令 | 分级 |
|---|---:|---|---|---|---|
| 未知命令 `API_PROBE_NOOP_<ts>` | 200 | `success` | 无 | **否**：既不报错也不提示，日志中同样没有 `unknown command` | handler_responded |
| 已知命令 `MOTION_STOP` | 200 | `success` | 无 | — | handler_responded（机器人原本静止，前后状态无差异） |

参数校验行为：

| 请求体 / Content-Type | HTTP | 响应 |
|---|---:|---|
| `{}` | 200 | `{"status": "error", "message": "Invalid JSON"}` |
| `{"command":123}` | 200 | 同上（类型错误被归为 Invalid JSON） |
| `{"command": `（语法错误） | 200 | 同上 |
| 无请求体 | 200 | 同上 |
| 合法 JSON 但 `Content-Type: application/x-www-form-urlencoded` | 200 | 同正常成功响应（Content-Type 未强制校验） |

前后状态（`state_before` / `state_after` 中嵌入了 `/api/battery` 与 `/api/status`）：电池 `power`、`voltage` 与状态 `null` 均无差异，符合静止机器人执行停止命令的预期。

是否适合 Dashboard 调用：**可以，但必须封装**（`wrap_as_capability`）：需要本地命令白名单、操作审计与二次确认；不能把原始命令字符串透传给前端。

### 2.5 `POST /api/execute_command`

```http
POST /api/execute_command
Content-Type: application/json

{"command":"printf vendor_api_probe_ok"}
```

```json
{"command":"printf vendor_api_probe_ok","message":"Command executed successfully","return_code":0,"status":"success","timestamp":1794236225.305246}
```

第二个探针 `echo API_PROBE_NOOP_20261109225701` 返回同样结构，`return_code` 为 0。

| 检查项 | 结果 |
|---|---|
| HTTP 状态 | 200 |
| 返回码 | `return_code: 0` |
| stdout | **响应中没有该字段**，`printf vendor_api_probe_ok` 的输出无法回读 |
| stderr / message | 仅有固定文案 `Command executed successfully` |
| 返回的 `command` 字段 | 原样回显请求中的命令 |
| 是否得到 `vendor_api_probe_ok` | **否**，输出被丢弃 |
| 服务端日志 | `[I]...[plugin.cc:844] Executing system command: printf vendor_api_probe_ok` |

服务进程运行用户：**root（uid=0）**。因此该接口等价于「未认证的 root shell 命令执行」。厂商页面本身也把它当作自由输入的系统命令面板（`system-command-input` + `/api/execute_command`）。

是否适合 Dashboard 调用：**否**，标记 `separate_acceptance_test`。即便将来封装，也只能用于只读诊断命令，且必须白名单化。

### 2.6 `POST /ota/mcu`

| 请求体 | HTTP | 响应正文 |
|---|---:|---|
| 空 multipart（零字段） | 400 | 空（Content-Length: 0），由 HTTP 层直接拒绝 |
| `{}`（application/json） | 400 | `{"success": false, "message": "No file provided"}` |
| multipart，只有普通字段 `note=probe` | 400 | `{"success": false, "message": "No file provided"}` |
| multipart，字段 `ota_file` = `_probe.txt` | 400 | `{"success": false, "message": "Invalid file type, only .bin and .hex accepted"}` |
| multipart，字段名 `file`（非 `ota_file`） | 400 | `{"success": false, "message": "No file provided"}` |

结论：

- 路由存在，multipart 解析器工作正常（带任意字段后即可进入业务校验）；
- 字段名必须为 `ota_file`，扩展名只接受 `.bin` / `.hex`；
- 缺少文件时返回 400 + 结构化 JSON，但**零字段的空 multipart 会得到 400 且无正文**，前后行为不一致；
- 本次未提供任何固件内容，未触发落盘与升级；成功分支字符串为 `{"success": true, "message": "OTA success", "filename": ...}` 与 `"OTA fail"`。

分级：参数校验已验证（`request_parsed`）。真实固件升级保留为**独立验收项目**。

### 2.7 `POST /api/emergency_stop` 与 `POST /api/reset_emergency`

按用户决定，本轮**只做协议级检查，不发送任何请求**，避免在实机上触发非预期急停。

可确认的信息：

- 插件二进制中包含 `/api/emergency_stop`、`/api/reset_emergency` 路由字符串，及 `Emergency stop activated` / `Emergency stop reset` 提示文案；
- 相关 ROS 侧痕迹：`/ControlBipedMotion_topic`（命令 `MOTION_STOP` / `EMERGENCY_STOP`）、`/audio_stop/msg`；
- `OPTIONS` 无法用于方法探测：全站任意路径（含不存在的路径）`OPTIONS` 一律 404 且无 `Allow` 头；
- 因未发请求，HTTP 状态、Content-Type、请求体要求、调用顺序均**未确认**，不能记为 `route_responded`（该级别要求真实 HTTP 响应）。

若后续执行成对测试，建议顺序：

```text
记录初始状态（轮速、IMU、/api/battery）
→ POST /api/emergency_stop
→ 读取响应 + 再次读取状态（预期：轮速被抑制、急停标志置位）
→ POST /api/reset_emergency
→ 读取响应 + 恢复后状态（预期回到急停前基线）
```

安全前提：机器人静止、周围无人、操作者可立即断电。

## 3. 控制 API 命令格式

统一格式：`POST /api/control`，`Content-Type: application/json`，请求体 `{"command":"<NAME>"}`；响应 `{"command":"<NAME>","status":"success","timestamp":<epoch>}`。

厂商页面中已出现的命令名（共 27 个，仅整理，本轮未逐一执行）：

| 来源 | 命令 |
|---|---|
| 控制台按钮映射（17） | `MOVE_FORWARD`、`MOVE_BACKWARD`、`STRAFE_LEFT`、`STRAFE_RIGHT`、`ROTATE_LEFT`、`ROTATE_RIGHT`、`GET_UP`、`GET_ON_CAR`、`GET_OFF_CAR`、`HEAD_UP`、`HEAD_RESET`、`WAVE_HAND`、`HAND_RESET`、`BEND_WAIST`、`STRAIGHTEN_WAIST`、`RETURN_TO_CHARGE`、`VISUAL_GET_ON_CAR` |
| 控制台其他入口（7） | `MOTION_STOP`、`EMERGENCY_STOP`、`RESET`、`MUTE`、`SAY_WOZAI`、`SELF_INTRODUCTION`、`ME_TOO_HAPPY`、`ENTER_PRESENTATION_MODE`、`REMOTE_CONTROL_MODE`、`ENTER-FOLLOW` |
| 模式切换（模板） | `SET_MODE_<MODE>`，页面上 `currentMode` 取值为 `command-only` / `mixed` / `voice-only` |
| 插件二进制中的分支名 | `MOTION_STOP`、`RESET`、`EMERGENCY_STOP`、`GET_UP`、`GET_ON_CAR`、`GET_OFF_CAR`、`VISUAL_GET_ON_CAR`、`HEAD_RESET`、`HAND_RESET` |

本轮端点测试实际只发送了两个命令：`API_PROBE_NOOP_<timestamp>`（未知命令，验证解析）与 `MOTION_STOP`（已知命令，机器人静止）。其余命令列入后续逐动作验证清单，需逐项单独批准，且每项都要有 ROS 状态或物理观测作为结果验证。

## 4. 安全与暴露情况

| 项目 | 观测结果 |
|---|---|
| 监听地址 | `0.0.0.0:12321`（**全网卡监听**，不是 127.0.0.1） |
| 认证 | **无**。39 个请求全部未携带 Cookie / Authorization，也未收到 401/403；不存在会话或令牌机制 |
| 服务进程身份 | `plugman`（pid 1614），**uid=0 gid=0 root**，28 线程 |
| `/api/execute_command` 权限 | 等价于**未认证的 root 命令执行**；只受 `return_code` 回传限制，输出不可见但副作用真实存在 |
| OTA 入口状态 | 路由在线；缺文件/扩展名错误均返回 400 JSON；零字段空表单 400 无正文；未验证成功路径 |
| CORS | 无 `Access-Control-Allow-*` 头；`OPTIONS` 全部 404，浏览器跨域预检会失败 |
| 方法语义 | 方法不匹配一律 404（没有 405），错误信息不区分「路由不存在」与「方法不对」 |
| WAF / 限流 | 未见 |
| 网络可达范围 | 机器人 `wlan0` 在 `192.168.137.0/24`。该网段内任意设备（含手机热点客户端）都可直接访问 12321，无需认证即可执行 root 命令 |

风险定级：**同一局域网内未认证的 root 代码执行 + 无鉴权运动控制 + 无鉴权的 MCU 固件升级入口**。在不修改厂商服务的前提下，缓解手段只有网络层隔离：不要把机器人接入不可信 Wi-Fi，操作机侧限制该网段，或在本机加防火墙规则只允许 127.0.0.1 访问 12321。

## 5. Dashboard 集成建议

推荐由机器人本机的 Dashboard 后端通过 `http://127.0.0.1:12321` 调用厂商服务，再把厂商响应与现有 ROS 观测合并展示；浏览器只与 Dashboard 的 127.0.0.1:8080 通信，绝不直连 12321。

| 接口 | 建议 | 说明 |
|---|---|---|
| `GET /`、`GET /index.html` | `status_only` | 仅作为厂商服务存活探针，不展示其页面 |
| `GET /api/battery` | `direct_backend_call` | 以 1–2 s 周期拉取，与 ROS `hardware/Bat` 交叉校验；注意 `voltage` 单位是 mV、`runtime` 是系统 uptime |
| `GET /api/status` | `status_only` | 恒为 `null`，不要依赖；如后续厂商补全再改为 direct_backend_call |
| `POST /api/control` | `wrap_as_capability` | 白名单命令（先只读/停止类）、后端二次确认、操作审计；返回 `success` 不代表命令生效，需配合 ROS 状态验证 |
| `POST /api/execute_command` | `separate_acceptance_test` | 不建议集成。若必须集成，只允许固定参数化的只读诊断命令，且明确「输出不回传」的限制 |
| `POST /ota/mcu` | `separate_acceptance_test` | 升级属高风险不可逆操作，单独验收；Dashboard 最多显示 OTA 接口可达性 |
| `POST /api/emergency_stop` | `separate_acceptance_test` → 通过后再 `wrap_as_capability` | 需先完成成对实机验收，确认急停置位/恢复的可观测信号 |
| `POST /api/reset_emergency` | `separate_acceptance_test` → 通过后再 `wrap_as_capability` | 与急停成对使用，永远不要单独暴露给前端 |
| `GET /statistics` | `status_only` | 路由未注册，不使用 |

## 6. 完成情况

| 问题 | 结论 |
|---|---|
| 一共发现多少个端点 | 8 个（题面清单全部在插件二进制中得到确认）；另发现 1 个未注册的 `/statistics` 字符串，以及 1 个静态路由 `/index.html` |
| 多少个端点有 HTTP 响应 | 8 个中有 6 个（`/`、`/api/battery`、`/api/status`、`/api/control`、`/api/execute_command`、`/ota/mcu`）；急停 2 个按决定未发送请求；`/statistics` 返回 404 |
| 多少个能正确解析请求 | 3 个：`/api/control`、`/api/execute_command`、`/ota/mcu`（含 multipart 与字段/扩展名校验） |
| 多少个完成了功能验证 | 2 个：`/`（标题与关键链接齐全）、`/api/battery`（字段可解析、单位确认、两次调用时间戳前进）。`/api/control` 与 `/api/execute_command` 只到 handler 级：前者无命令名校验、状态无可观测变化，后者不回传输出 |
| 哪些适合直接封装进 Dashboard | `/api/battery`（`direct_backend_call`）；`/api/control` 需封装为受控能力（`wrap_as_capability`），不能直通 |
| 哪些需要单独实机验收 | `/api/emergency_stop`、`/api/reset_emergency`（成对）、`/ota/mcu`（真实固件）、`/api/execute_command`（若确需集成） |
| 是否发现未认证的高权限接口 | **是**：`/api/execute_command` 以 root 身份执行任意 shell 命令，且监听的 `0.0.0.0:12321` 无任何认证，同网段设备均可利用；`/ota/mcu` 与运动控制接口同样无鉴权 |

## 附录 A：产物清单

```text
D:\projects\m1-robot\vendor-console-api-smoke-test.md
D:\projects\m1-robot\artifacts\vendor-console-api-smoke-test\
├── requests.jsonl          39 条请求全文（含 state_before/state_after）
├── endpoint-summary.json   端点分级、基线、发现列表
├── server-process.txt      进程/监听/配置/文件属性快照
└── responses\              每条请求的原始响应（.http.txt，首页另存 .body.txt）
```

`requests.jsonl` 字段说明：`response_chars` 为响应正文的字符数，`content_length_header` 为 HTTP 头中的实际字节数；首页因体积较大在 `response_body` 中截断，完整正文见 `responses/01-A1-home.body.txt`。

## 附录 B：方法与限制

- 使用 PowerShell `System.Net.Http.HttpClient` 发送请求，单请求 8 s 取消令牌，记录状态码、响应头、正文、延迟；
- 所有请求均**未**携带 Cookie / Authorization，因此「无认证时的行为」即为默认观测结果；
- 对 `/api/emergency_stop`、`/api/reset_emergency` 未做任何 HTTP 探测（含 GET/OPTIONS 之外的尝试），仅依据二进制字符串与用户决定标注为未执行；
- 未验证真实 OTA 升级、未逐条执行 27 个控制命令、未做并发/长稳测试；
- 机器人端缺少 `libcurl.so.4`、无 objdump/readelf，二进制分析仅用 `strings`。
