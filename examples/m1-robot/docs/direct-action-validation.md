# 首批可直接调用动作调查与验证方案（direct-action-validation）

- **调查日期**: 2026-11-09 21:40 – 22:30 CST
- **调查方式**: SSH 只读检查。**未发布任何控制消息、未调用任何 Service/Action、未修改任何进程或文件**；机器人在整个调查期间保持充电桩静止状态。
- **输入材料**: [control-interface-inventory.md](control-interface-inventory.md)、[ros2-topics.txt](artifacts/control-interface-inventory/ros2-topics.txt)、[ros2-services-actions-nodes.txt](artifacts/control-interface-inventory/ros2-services-actions-nodes.txt)、机器人端只读复核（见证据节）。
- **交付物**: 本报告 + [action-catalog.yaml](action-catalog.yaml)（机器可读动作目录）

---

## 1. 总结

| 指标 | 数量 | 说明 |
|---|---|---|
| 排查的候选接口 | 15+ | 8 个 Action、相关 Service、BT 任务入口、JSON 高层入口 |
| 可进入受控实机测试（candidate） | **5** | map_segmentation、pause_head_control、start_to_control_head、pause_to_navigation、start_to_navigation（末位、门控） |
| 次批候选（证据完整但风险高/反馈弱） | 3 | start_to_track、start_to_audio_track、（后续）回充 |
| 因停止机制/schema 不完整阻塞（blocked） | 3 | start_to_SLAM、go_charge、follow_polar 独立开关 |
| 明确禁止（forbidden） | 1 组 | 底层电机/工厂/高危动作集合（详见目录） |
| readiness=ready | **0** | 按判定标准，任何动作在实机验证前不得标 ready |

**推荐的首批 5 个动作（按测试顺序）**：

1. `map_segmentation` — 纯计算，验证 Action 调用链（无任何运动）
2. `pause_to_navigation` — 无导航时调用被安全拒绝，验证 Service 调用链（无任何运动）
3. `pause_head_control` — 头部会话未运行时被安全拒绝，验证 SetBool 调用链（无任何运动）
4. `start_to_control_head` — 首个物理动作，仅头部转动，三重停止路径
5. `start_to_navigation` — 首个底盘动作，1–2 m 近距离安全点，需单独批准

### 关键更正（相对既有清单）

- **跟踪类 Action 定义文件并非缺失**。`/app/joyin/share/mini_track_msgs/action/StartTrack.action` 与 `/app/joyin/share/audio_track_msgs/action/StartAudioTrack.action` 实际存在，`ros2 interface show` 在正确环境（先 source `/middleware/humble/setup.sh` 再追加 `AMENT_PREFIX_PATH=/app/joyin`）下可正常解析。既有清单第 7.3 待验证项已解决。
- **`StartAudioTrack.action` 附带厂商完整使用注释**：含启动/停止/取消/暂停的官方 CLI 示例、依赖栈分层（HeadLite/Full）、互斥规则、feedback 数值码表。这是本次调查中证据等级最高的接口。
- `/start_to_navigation` 的提供节点实测为 `/joyin_navigation_node`（既有清单写作 `/navigator_wrapper`，已更正）；goal 实际仅 `navigation_mode`(uint8) + `navigation_goal`(PoseStamped) 两字段，不存在 map_id/angle 字段。
- 另发现未记录的 `navigation_msgs/action/TwoPointNav.action`（未见运行图暴露，暂不启用）。

---

## 2. 候选动作对比表

| 动作 | 接口 | 物理效果 | 前置条件 | 反馈 | 停止方式 | 风险 | 当前结论 |
|---|---|---|---|---|---|---|---|
| map_segmentation | Action `/map_segmentation` (map_optimization_msg/MapSegmentation) | 无运动（纯计算；写 PGM 到 /userdata/log） | 有地图 | result 含分割图；无运行中 feedback | 原生 cancel（未逐行验证） | 低 | **candidate（首批 #1）** |
| pause_to_navigation | Service `/pause_to_navigation` (SetBool 语义 pause_type 1/2) | 无运动 | 无（未导航时被拒） | result+message，文案明确 | 即恢复原语 | 低 | **candidate（首批 #2）** |
| pause_head_control | Service `/audio_track/pause_head_control` (SetBool) | 无运动（冻结头部） | 头部会话运行中 | success+message | 即恢复原语 | 低 | **candidate（首批 #3）** |
| start_to_control_head | Action `/start_to_control_head` (StartAudioTrack, command=1) | 仅头部转动 | 无跌倒、无活动 audio 会话 | feedback 1/2/3/4 码表明确 | command=0 / cancel_goal / pause 三重 | 中 | **candidate（首批 #4）** |
| start_to_navigation | Action `/start_to_navigation` (StartToNavigation, mode=1) | 底盘行走 ≤0.35 m/s | 离桩、有图、定位有效、无活动任务 | feedback/result 有 message 无码表 | cancel_goal / pause service | 高 | **candidate（首批 #5，门控）** |
| start_to_track | Action `/start_to_track` (StartTrack) | 底盘跟随行走 | 离桩、视野有人 | result 0/1；feedback 厂商注明忽略 | command=0 / cancel / pause | 高 | candidate（次批） |
| start_to_audio_track | Action `/start_to_audio_track` (StartAudioTrack) | 头部+底盘（Full 栈） | 离桩、无活动会话 | feedback 码表明确 | command=0 / cancel（Full 时拒控头 Stop） | 高 | candidate（次批） |
| start_to_SLAM | Action `/start_to_SLAM` (StartToSLAM, mode 0/1) | 原地旋转（建图） | — | StatusResponse 语义未确认 | /slam/cancel、BusinessCancelSlam | 高 | **blocked**（重写地图风险未评估） |
| go_charge（回充） | JSON `/BusinessAppReq_topic`（schema 未知） | 离桩+导航+对接 | BT 自查（CheckAbleToCharge 等） | AppRes/AppRpt 格式未知 | 未知 | 高 | **blocked**（schema 与取消路径未知） |
| follow_polar_control（视觉/声源极坐标开关） | Service SetBool ×2（/vision 服务端） | 无直接运动 | 跟踪会话内 | success | — | 中 | **forbidden**（内部组件，独立语义不明） |
| 底层电机/工厂/高危集合 | `/ControlMotor*`、sec_develop 电机命令、BipedMotionCmd/cmd_vel 直发、McuFctCheck、factory_*、上下小车/爬起/跨步/调腿长等 | 直驱电机 | — | — | 无安全路径 | 极高 | **forbidden** |

---

## 3. 首批动作详细定义

### 3.1 map_segmentation（地图房间分割）

| 项 | 内容 |
|---|---|
| 精确接口 | ROS 2 Action `/map_segmentation`，类型 `map_optimization_msg/action/MapSegmentation` |
| 参数结构 | goal: `input_map`(sensor_msgs/Image, **8UC1**，0=障碍 255=自由)、`map_resolution`(float32, m/cell)、`map_origin`(geometry_msgs/Pose)、`return_format_in_pixel`(bool)、`return_format_in_meter`(bool)、`robot_radius`(float32, m, 可选)、`room_segmentation_algorithm`(int32: -1 预设 / 0-5 / 99；运行时预设为 5=随机场)。result: `segmented_map`(32SC1, 房间标签 1..N)、房间信息数组、门点数组。**feedback 段为空**（无运行中反馈） |
| 调用方/服务方 | 服务方 `/room_segmentation`（`/app/joyin/bin/segment/install/lib/room_segmentation/room_segmentation_server`）；当前无活动客户端 |
| 状态反馈 | goal 接受后无运行中反馈；result 返回分割图与房间元数据 |
| 成功条件 | result 含非空 segmented_map 且房间标签 ≥1 |
| 失败条件 | goal 被拒绝或 aborted（输入图格式错误/全图不可分割） |
| 取消方式 | ROS Action 原生 cancel_goal（标准 rclcpp action server；取消回调行为未逐行验证，列入验证项） |
| 超时 | 服务端无超时参数 → 网关侧设 120 s |
| 控制权影响 | 无。不进 robot_motion 组；副作用：服务端写 PGM 到 `/userdata/log/room_segmentation_log/`（厂商既有行为），并更新 `/fused_global_map`、`/processed_occupancy_grid` |
| 实机验证准备 | 静止时调用；输入图由网关订阅 `/map` 转换（OccupancyGrid → 8UC1 Image）；对照 `/get_segment_map` 服务可无副作用查询既有分割结果，用作交叉验证 |
| 证据 | `.action` 文件全文（含字段单位注释）；room_segmentation yaml；node info 确认无运动 Publisher |
| Blockers | 导航中调用对代价地图影响未评估（建议仅静止调用）；取消回调未实机验证 |

### 3.2 pause_to_navigation（导航暂停/恢复）

| 项 | 内容 |
|---|---|
| 精确接口 | Service `/pause_to_navigation`，`navigation_msgs/srv/PauseToNavigation` |
| 参数结构 | request: `pause_type`(uint8: 1=暂停, 2=继续)；response: `result`(uint8) + `message`(string) |
| 调用方/服务方 | 服务方 `/joyin_navigation_node`（`joyin_navigation` 二进制）；无状态副作用 |
| 状态反馈 | message 文案实测（strings 证据）：`Pause succeeded.` / `Hold on succeeded.` / `Pause ignored, navigation is not running` / `Navigation is already paused` / `Invalid value for pause type` |
| 成功条件 | 导航运行中：result 表示受理 |
| 失败条件 | 非法 pause_type；导航未运行（安全拒绝，正是首次无风险验证点） |
| 取消方式 | 即恢复原语（pause_type=2） |
| 超时 | 网关侧 5 s |
| 控制权影响 | 无（仅作用于 joyin_navigation_node 内部会话状态） |
| 实机验证准备 | 导航未运行时调用 pause_type=1，预期收到 "navigation is not running"——验证 Service 链路且零风险 |
| 证据 | `.srv` 文件；joyin_navigation strings |
| Blockers | 无 |

### 3.3 pause_head_control（头部声源跟踪暂停/恢复）

| 项 | 内容 |
|---|---|
| 精确接口 | Service `/audio_track/pause_head_control`，`std_srvs/srv/SetBool` |
| 参数结构 | request: `data`(bool: true=暂停并冻结姿态, false=恢复)；response: success + message |
| 调用方/服务方 | 服务方 `/audio_track_node`（`audio_track_main`） |
| 状态反馈 | strings 证据：`Head control paused (pose frozen, session kept)` / `Head control resumed` / `pauseHeadControl rejected: head control not running` / `pauseHeadControl rejected: planner running`（声源规划 Full 运行时暂停被拒） |
| 成功条件 | 头部会话运行中：success=true |
| 失败条件 | 头部会话未运行 / Full 规划运行中（均安全拒绝） |
| 取消方式 | 即恢复原语（data=false） |
| 超时 | 网关侧 5 s |
| 控制权影响 | 无（作用于 audio_track 内部会话） |
| 实机验证准备 | 头部会话未运行时调用 data=true，预期被拒——验证 Service 链路且零风险 |
| 证据 | `.action` 头部注释（厂商官方 CLI 示例）；audio_track_main strings |
| Blockers | 无 |

### 3.4 start_to_control_head（头部声源跟踪，仅转头）

| 项 | 内容 |
|---|---|
| 精确接口 | ROS 2 Action `/start_to_control_head`，`audio_track_msgs/action/StartAudioTrack` |
| 参数结构 | goal: `command`(int8: 1=启动, 0=停止)、`task`(int8, 此通道忽略)；result: `result`(uint8: 0=失败, 1=成功) + `message`；feedback: `feedback`(uint8) + `status_message` |
| 反馈码表（厂商注释实证） | **1=starting, 2=running, 3=canceling, 4=stopping**（10/11/12 找脸会话码仅声源规划 task=1 使用） |
| 调用方/服务方 | 服务方 `/audio_track_node`；运行时发布 `/ControlHeadMotion_topic`(Int16MultiArray) 驱动头部，**不发布 /cmd_vel、不启 odom/log**（HeadLite 栈，厂商注释明确设计为与导航不冲突） |
| 状态前置 | 节点内建会话互斥（`session already starting or running`、`head control already running`、`Reject ... head control busy, stop head first`）；厂商 BT 要求 `!IsFallen`——直发 action 是否自查未确认，**网关必须先检查 /HardwareSelfFall_topic** |
| 成功条件 | 启动 goal result==1 且 feedback 进入 2(running) |
| 失败条件 | result==0（被拒 / SensorFault：strings `Idle: target %s but SensorFault, hold Idle`） |
| 取消方式 | **三重停止**：① 发 `command=0` goal（厂商注明业务主停机路径）② `ros2 action cancel_goal /start_to_control_head <goal_id>`（仅 HeadOnly 有效）③ `/audio_track/pause_head_control` 冻结姿态。兜底：wbc 层 `head action global timeout (%d cycles), force stop` 与头部舵机过流拒绝（`head_neck servo overcurrent/overload ... reject enable`） |
| 超时 | 启动 goal 网关侧 15 s；会话最长持续时间网关侧建议 300 s 后自动 command=0 |
| 控制权影响 | 仅头部云台；Full(声源规划)运行时头部 Stop/Cancel 被拒——必须先停规划。concurrency_group: head_motion |
| 实机验证准备 | 先离桩静止、场地 1 m 内无人员面部贴近；全程观测 `/audio_track/head_feedback`、`/HardwareHeadPosition_topic`；手柄持有人随时可介入 |
| 证据 | `.action` 文件含厂商完整用法注释；node info；audio_track_main/wbc strings；BT XML |
| Blockers | 跌倒前置需网关自查；充电桩上行为未验证；停止路径未实机验证 |

### 3.5 start_to_navigation（两点导航，近距离安全点，门控）

| 项 | 内容 |
|---|---|
| 精确接口 | ROS 2 Action `/start_to_navigation`，`navigation_msgs/action/StartToNavigation` |
| 参数结构 | goal: `navigation_mode`(uint8: 1=两点导航, 2=自主探索)、`navigation_goal`(geometry_msgs/PoseStamped)；result: `result`(uint8) + `message`；feedback: `feedback`(uint8) + `message` |
| 调用方/服务方 | 服务方 `/joyin_navigation_node` → 内部封装 navigator/Nav2/auto_explore → `/cmd_vel` → wbc 步态 |
| 运动约束（参数实证） | 线速度 ≤0.35 m/s，角速度 ≤0.6 rad/s；到点容差 0.10 m / 0.2618 rad；motion_server 内建卡死/打滑/侧倾检测（stuck/slip/step checker） |
| 状态前置 | 有图（HasMap）、定位有效（/tracked_pose）、**离桩**（BT 流程会先离桩，直发 action 不确定是否自带 → 网关强制检查 on_charger）、无活动任务；单 goal 策略：重复 goal 被拒（`please do not publish goals repeatedly`） |
| 成功/失败条件 | result 数值码表未知，靠 message 字符串（`explore success`、`robot got stuck` 等）；失败时可结合 `/cmd_vel`、`/global_plan` 旁路观测 |
| 取消方式 | ① 原生 cancel_goal（strings: `Receive cancel goal for starting to navigation` / `Navigation is canceled`）② `/pause_to_navigation` 暂停（暂停≠取消，暂停保持会话） |
| 超时 | 网关侧 300 s + 距离上限（首批建议目标 ≤2 m） |
| 控制权影响 | APP/语音使用同一 action → 语义对齐既有业务；与 BT 导航任务并发时单 goal 策略提供互斥，但抢占细节未知。concurrency_group: robot_motion |
| 实机验证准备 | 前四个动作全部通过后，经用户**单独批准**才执行；目标点选开阔地 1–2 m 处，人员退至 3 m 外 |
| 证据 | `.action`/`.srv` 文件；joyin_navigation strings；mini_navigation_params.yaml |
| Blockers | result/feedback 码表未知；充电桩直发行为未知；未实机验证 |

---

## 4. 测试顺序（逐项方案，未经用户逐项批准不得执行）

> 通用安全底线：每项测试前通过 Dashboard 确认（只读）：电池 ≥20%、`/HardwareSelfFall_topic` 正常、`/ControlBipedMotionFeedback_topic.control_mode==0`；全程一名安全员手持手柄（既有 BLE 遥控是当前唯一经验证的物理介入手段）；SSH 会话保持、Dashboard 页面打开观测；异常时立即按该项"异常停止"执行。

**测试 1 — map_segmentation（零运动）**
- 测试前检查：`/map` 有数据（Dashboard 地图/位姿页）；机器人静止于桩
- 调用：`ros2 action send_goal /map_segmentation map_optimization_msg/action/MapSegmentation "{input_map: <由 /map 转换>, map_resolution: 0.05, map_origin: <地图 origin>, return_format_in_pixel: true, return_format_in_meter: true, robot_radius: 0.21, room_segmentation_algorithm: -1}"`
- 预期动作：**机器人完全不动**；CPU 短时上升；返回分割图
- 最大持续时间：120 s（超时视为失败）
- 安全空间：无要求
- 人员要求：1 名观测者
- 正常停止：等待 result；取消用 action cancel_goal
- 异常停止：无需（无运动）；若异常退出重启无需任何恢复
- 通过标准：result 含房间标签 ≥1 的分割图；机器人全程不动；`/userdata/log/room_segmentation_log/` 出现新 PGM（预期副作用）

**测试 2 — pause_to_navigation（零运动）**
- 测试前检查：确认当前无导航任务（`/global_plan` 无更新、Dashboard 无导航迹象）
- 调用：`ros2 service call /pause_to_navigation navigation_msgs/srv/PauseToNavigation "{pause_type: 1}"`
- 预期动作：**无任何运动**；返回 "Pause ignored, navigation is not running" 类 message
- 最大持续时间：5 s
- 通过标准：收到明确 result+message；机器人不动；随后调用 `pause_type: 2` 验证对称性（同样被忽略）

**测试 3 — pause_head_control（零运动）**
- 测试前检查：确认无头部会话（feedback 无 /audio_track/head_feedback 更新）
- 调用：`ros2 service call /audio_track/pause_head_control std_srvs/srv/SetBool "{data: true}"`
- 预期动作：**无任何运动**；success=false / "head control not running"
- 最大持续时间：5 s
- 通过标准：被安全拒绝；机器人不动

**测试 4 — start_to_control_head（首个物理动作，仅头部）**
- 测试前检查：机器人**离桩**（由用户或既有 APP 控制移出充电桩，本测试不负责离桩）、平稳站立、无跌倒标志、头部 0.5 m 内无障碍；安全员到位
- 调用：`ros2 action send_goal /start_to_control_head audio_track_msgs/action/StartAudioTrack "{command: 1}" --feedback`
- 预期动作：在测试者发出声响（拍手/说话）时头部水平/俯仰转向声源，**底盘不动**；无声音时保持
- 最大持续时间：启动 goal 15 s 出 result；会话持续 ≤60 s 即手动停止
- 安全空间：头部运动半径内（约 0.3 m）无障碍；机器人周围 1 m 无人员面部贴近
- 人员要求：1 名安全员持手柄 + 1 名测试者发声
- 正常停止：`ros2 action send_goal /start_to_control_head audio_track_msgs/action/StartAudioTrack "{command: 0}"`（主停机路径）
- 异常停止：`ros2 action cancel_goal /start_to_control_head <goal_id>` → 仍动则 `/audio_track/pause_head_control data=true` 冻结 → 仍异常由手柄介入
- 通过标准：启动 result==1、feedback 进入 2；声源方向与头部转向一致（Dashboard 头部姿态页对照 `/HardwareHeadPosition_topic`）；command=0 后头部 3 s 内停止且 result==1；全程底盘零位移

**测试 5 — start_to_navigation（首个底盘动作；需单独批准，不得由测试 1–4 的批准覆盖）**
- 测试前检查：**必须已离桩**、有图、`/tracked_pose` 有效（定位）、电池 ≥50%、场地 ≥3×3 m 清障、无其他任务
- 调用：`ros2 action send_goal /start_to_navigation navigation_msgs/action/StartToNavigation "{navigation_mode: 1, navigation_goal: {header: {frame_id: map}, pose: {position: {x: <1-2m 内安全点>, y: <...>}, orientation: {z: 0, w: 1.0}}}}" --feedback`
- 预期动作：机器人缓步（≤0.35 m/s）走向目标点并在 0.10 m 容差内停止转向到位
- 最大持续时间：目标距离 1–2 m → 90 s 上限，超时手动取消
- 安全空间：路径上无台阶/线缆/易碎物；测试区无人
- 人员要求：安全员持手柄，所有人距机器人 ≥3 m
- 正常停止：到达自动结束；或提前 `ros2 action cancel_goal /start_to_navigation <goal_id>`
- 异常停止：cancel_goal 无效 → `/pause_to_navigation {pause_type: 1}` 暂停 → 再 cancel；最后手柄介入
- 通过标准：机器人到达目标点容差内、result 的 message 表成功、全程无跌倒（`/HardwareSelfFall_topic` 不变）、`/ControlBipedMotionFeedback_topic` 最终回到待机；取消路径在测试中至少演练一次（走到一半取消，机器人应在 3 s 内停止）

---

## 5. Dashboard 接入建议（本阶段不实施，仅设计）

```text
浏览器 (127.0.0.1:8080)
   │  HTTPS/WS（现有只读通道，新增动作指令走同一后端）
   ▼
Dashboard 后端（FastAPI，现有 src/main.py 内新增 action_gateway 模块）
   │  ① 白名单：仅加载 action-catalog.yaml 中 readiness∈{candidate,ready} 且 dashboard_enabled=true 的动作
   │  ② 前置条件检查（后端强制）：robot_online、not_fallen、on_charger 语义按动作要求、
   │     no_active_action（查询 feedback 状态而非前端禁用）、battery 阈值
   │  ③ 互斥：concurrency_group=robot_motion 同时仅一个；head_motion 与 robot_motion 可并存但 Full 栈除外
   │  ④ 超时与审计：每动作 timeout_seconds；日志记录 发起者/动作/参数/开始/结束/结果 到项目 logs 目录
   │  ⑤ 刷新/断连保护：动作生命周期驻留后端（浏览器断开不影响监管，页面重连后恢复状态显示）
   ▼
ROS 2 调用层（rclpy Action/Service client，仅白名单接口，无通用"发任意 ROS 消息"接口）
   ▼
反馈与审计回写 → StateStore → WebSocket 推送 → UI 状态展示
```

关键原则（落实为网关代码约束）：
- 默认全部禁用（`dashboard_enabled: false` 起步，逐项开启）；`confirmed_on_hardware: true` 才显示为正式按钮，此前仅显示"待验证"状态。
- 取消优先用 Action 原生 cancel；head_motion 动作 UI 必须常驻"停止"按钮（发 command=0）。
- 高风险动作（robot_motion 组）二次确认对话框 + 显示实时前置条件检查结果。
- 后端校验一切前置条件，前端禁用仅是体验优化。
- 审计日志每条记录：`timestamp, actor, action_id, params, goal_id, started_at, ended_at, result, cancel_reason`。

## 6. 证据清单（本次新增只读证据）

| 证据 | 位置 |
|---|---|
| StartToNavigation/StartToSLAM/MapSegmentation/StartTrack/StartAudioTrack/TwoPointNav 定义全文 | 机器人 `/app/joyin/share/{navigation_msgs,cartographer_ros_msgs,map_optimization_msg,mini_track_msgs,audio_track_msgs}/action/` |
| PauseToNavigation/PauseToSLAM/PauseToNav srv 定义 | `/app/joyin/share/navigation_msgs/srv/`、`/app/joyin/share/cartographer_ros_msgs/srv/` |
| 节点接口画像（订阅/发布/Service/Action） | `ros2 node info /audio_track_node /vision_track_node /joyin_navigation_node /room_segmentation /vision /utils /joyin_slam_pose_node` |
| 取消/暂停/互斥/看门狗字符串证据 | `strings joyin_navigation / audio_track_main / vision_track_main / libwbc_ctrl_plugin.so`（关键行已录入本报告各节） |
| 行为树前置条件 | `/app/joyin/config/audioLocationHeadControlTree.xml`、`audioLocationTree.xml`、`naviSubTree.xml`、`leaveChargerSubTree.xml`、`openChargeTree.xml` |
| 导航运动学上限与安全检查器 | `/app/joyin/bin/navigation/install/share/joyin_interactor/param/mini_navigation_params.yaml` |
| 分割算法参数 | `/app/joyin/bin/segment/install/share/room_segmentation/yaml/room_segmentation_action_server_params.yaml` |
| 调查脚本存档（机器人端，只读操作） | `/userdata/developer/dashboard/scripts/investigate-actions*.sh`（1–6 号，均在项目目录内） |

## 7. 遗留问题（移交厂商文档需求清单）

1. `/start_to_navigation`、`/start_to_track` result/feedback 的数值码表（现仅有 message 字符串兜底）。
2. 直发 Action（不经 BT）时跌倒/在桩检查是否由服务端自查（网关先按"不自查"处理）。
3. 与 APP/语音/手柄并发时 action 抢占的确定性规则（单 goal 策略已提供部分保证）。
4. `start_to_SLAM` 建图对现有地图的覆盖/回滚机制。
5. 回充任务（goCharge）的 JSON 触发 schema 与任务中止路径。
6. （新增）独立起立指令为何仅允许语音对话路径执行（instruction_task.cc:229），是否有官方授权的外部起立入口。
7. （新增）BipedMotionCmd 中 GAIT_RESET(12) 未实现（wbc 回调报 unknown gait_type），实际可用的 gait 全集与姿态适用表。

---

## 8. 实机验证记录（2026-11-09 22:42，用户明确授权解除禁止项后执行）

用户下达"让它站起来"指令并解除禁止项后，按以下顺序实测，全程带状态监控：

| 时间 | 尝试 | 结果 | 结论 |
|---|---|---|---|
| 22:29 | `BipedMotionCmd gait 12`（姿态复位）10Hz×1.5s | 无反应；wbc 日志 `unknown gait_type: 12, robot stop` | GAIT_RESET 在 wbc 中**未实现**，枚举存在≠可调用 |
| 22:37 | webctl `GET_UP`（厂商 12321 端口控制台） | HTTP 受理，头灯亮/表情切换，但身体未动；业务日志 `outside contains StandBy, ignore this command` | 厂商**有意忽略**外部注入的独立起立指令（仅语音对话路径放行）；webctl 实际发布 `/BusinessAppReq_topic` |
| 22:42 | `BipedMotionCmd gait 10`（跌倒爬起）10Hz×1.5s | **成功**：8 秒完成起身，IMU 俯仰 -68°→0.9°，`Motor_Disable_mode → Bipedal_Movement_mode`，电流峰值 812 | 机器人躺卧+舵机失能时，wbc 原生等待的 get-up 信号；**首个 confirmed_on_hardware: true 的运动动作** |

### 关键新事实

1. **厂商本地 Web 控制台**：`http://127.0.0.1:12321`（utils_webctl_plugin），POST `/api/control` `{"command":"..."}`，命令集含 GET_UP / MOVE_* / ROTATE_* / 上下车 / 头与手的控制 / RETURN_TO_CHARGE / MOTION_STOP。对 StandBy 类指令受业务层"仅语音"策略约束，但运动类命令直发 `/ControlBipedMotion_topic`。
2. **控制权门控分层**：业务层（instruction_task）拦外部起立指令；wbc 层接受 gait 10/11 get-up 与手柄信号（joy_velocity_ctrl_callback），无业务层介入。
3. **`boot_control` 是开关机命令**（`publish boot control: on/off`，电量≤6% 跳过），**不是起立**，发送 off 会直接关机，任何网关设计必须将其列入黑名单。
4. 机器人趴坐/停靠（gait 53，Motor_Disable_mode）是设计态：双足持续站立功耗大，无任务时厂商固件让舵机失能趴坐，任何业务任务经 BT BodyReady 自动起身。

### 对动作目录的影响

- `get_up_gait10`：新增，readiness **ready**，confirmed_on_hardware **true**（首个实机验证运动动作）。
- `stand_up_via_business`：新增，**blocked**（厂商政策仅语音路径）。
- 原则不变：Dashboard 接入仍须走动作网关 + 白名单 + 单动作互斥，gait 10 属中层直发，正式接入前需补测"爬起中途取消"与 gait 10/11 姿态适用性。

---

*本报告仅为只读调查与验证方案设计。在用户逐项明确批准前，机器人保持现状，不发送任何控制命令；对某一动作的批准不扩展为对其他动作的批准。*
