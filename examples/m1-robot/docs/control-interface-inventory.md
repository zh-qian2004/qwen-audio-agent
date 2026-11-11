# Zeroth / 元点智能 M1 机器人控制接口清单

- **调查日期**: 2026-11-09 20:20 – 21:30 CST
- **调查方式**: SSH 只读检查（未发布任何控制消息、未调用任何 Service/Action、未修改任何进程或文件）
- **机器人**: Buildroot 2022.08, AArch64, 内核 6.1.83-DR-PL5.1_V1.1.0 (2026-10-16 构建)
- **ROS 环境**: ROS 2 Humble, `ROS_DOMAIN_ID=0`, `ROS_LOCALHOST_ONLY=1`, RMW = FastDDS (`rmw_fastrtps_cpp`)
- **固件版本线索**: 内核版本串 `DR-PL5.1_V1.1.0`；busybox 2026-09-03 构建；应用层版本文件未找到（见待验证）
- **原始枚举数据**: [ros2-topics.txt](artifacts/control-interface-inventory/ros2-topics.txt)、[ros2-services-actions-nodes.txt](artifacts/control-interface-inventory/ros2-services-actions-nodes.txt)

---

## 1. 结论摘要

| 问题 | 回答 |
|---|---|
| 当前是否存在可调用控制接口 | **是**。运行中的 ROS 图内有 46 个节点、200+ Topic、8 个 Action，其中约 40 个为控制/触发类接口 |
| 控制接口主要形式 | 混合：**高层走 JSON 字符串 Topic（`/BusinessAppReq_topic`、`/sec_develop_cmd_topic`）→ 内部行为树 + ROS Action（`/start_to_navigation` 等 6 个）**；中层走 `plan/msg/BipedMotionCmd` Topic；底层为 `Int16MultiArray` 电机直写 Topic。厂商插件（plugman 框架）承载全部核心逻辑，无公开 HTTP/SDK 接口 |
| 哪些接口在当前运行 | 全部已验证接口均来自当前运行图（见总表"当前状态=已验证"） |
| 哪些接口只是已安装 | 无独立"已安装未运行"的 ROS 接口包；但 `BipedMotionCmd` 中大量预留 gait/action 枚举（70–79、21–30）为"已定义未实现" |
| 是否存在直接关节控制 | **是**。两条路径：① `/sec_develop_cmd_topic` 的 `single_motor_control`/`multi_motor_control`/`motor_torque_control`/`motor_home_set` JSON 命令（按关节名 head_yaw … right_arm_elbow）；② `/ControlMotor*` 系列 Topic 直写电机（订阅者 `/hardware`） |
| 是否存在高层动作/行为控制 | **是**。27 棵行为树（巡逻/漫游/回充/上下小车/建图/跟踪等）由 `/business` 内 `business_task_plugin` 加载，经 `/BusinessAppReq_topic` 或语音/APP 触发 |
| 当前能否确认安全调用流程 | **不能完全确认**。已确认存在模式机（robot_main_mode / robot_state）与 External_Control_mode，但模式切换握手协议、命令超时、控制权抢占规则仅有二进制字符串线索，需厂商文档确认 |

**调查时机器人状态**（只读采样）：电池 100%、`on_charger=2`、`is_charging=1`（充电中）、`control_mode=0 (MODE_IDLE)`、`gait_type=53`、关节电流/速度全 0 —— 静止停靠在充电桩上。

---

## 2. 控制架构（按实测修正）

```text
外部调用者 (APP/BLE遥控器/语音/二次开发)
   │
   ├─ JSON 命令: /BusinessAppReq_topic (String)      ──┐
   ├─ JSON 命令: /sec_develop_cmd_topic (String)        ├─► /business 节点
   │    (回复: /BusinessAppRes_topic, /sec_develop_reply_topic)  │
   ├─ BLE 遥控: /HardwareRemoteControl_topic ──────────► /hardware 节点
   └─ 手柄: /joy → /utils(过滤) → /joy_filtered ────────┘
                                                        │
                          ┌─────────────────────────────┴──────────────────────┐
                          │ /business (business_pm, 9 个插件)                   │
                          │  · business_task_plugin: 行为树引擎(27棵XML)        │
                          │    MoveController / SlamControl / AudioLocation …  │
                          │  · business_app_command_plugin: APP JSON 分发       │
                          │  · business_follow_plugin / atomic_action …        │
                          └───────┬──────────────────────────┬─────────────────┘
                                  │ Action Goal              │ Topic
          /start_to_navigation ◄──┤  /start_to_SLAM          │ /ControlBipedMotion_topic
          /start_to_track         │  /start_to_audio_track   │   (plan/msg/BipedMotionCmd)
          /start_to_control_head  │  /map_segmentation       │ /ControlGoCharge_topic
                                  ▼                          ▼
        ┌──────────────────────────────┐        ┌──────────────────────────────┐
        │ 导航链                        │        │ /control 节点 (control_pm)   │
        │ /navigator_wrapper           │        │  · wbc_ctrl_plugin (全身控制 │
        │ Nav2: planner/controller/    │        │    模式机/步态生成)          │
        │ smoother + costmaps          │        │  · charging_dock_plugin      │
        │ → 输出 /cmd_vel (Twist) ─────┼───────►│  (订阅 /cmd_vel 转步态)      │
        └──────────────────────────────┘        └──────────┬───────────────────┘
                                                           │ /Control* 系列 Topic
                                                           ▼
                                             ┌──────────────────────────────┐
                                             │ /hardware 节点 (hardware_pm) │
                                             │ hardware_manager_plugin      │
                                             │ → MCU / 舵机 / 轮电机        │
                                             └──────────┬───────────────────┘
                                                        ▼
                       状态反馈: /HardwareBat, /HardwareVehicleFeedback, /HardwareKinematic,
                                 /HardwareMotorPosition, /ControlBipedMotionFeedback,
                                 /HardwareHeadPosition, /HardwareSelfFall … (→ 各层回环)
```

要点：
- **`/control`（wbc_ctrl_plugin）是全身控制的唯一汇聚点**：它同时订阅 `/cmd_vel`（来自 Nav2）、`/ControlBipedMotion_topic`（来自 business/utils）和 `Hardware*` 反馈，输出 `/ControlMotorSpeed`、`/ControlVehicleSpeed` 等给 `/hardware`。
- **`/hardware` 不做策略，只做执行**：直接订阅 `Control*` 电机指令并驱动 MCU。
- **`/business` 是高层入口**：所有 APP 命令、行为树、导航/建图 Action 客户端都在这里。

---

## 3. 控制接口总表

> 风险等级：低=只读不影响运动；中=改变功能开关；高=直接引发运动或状态切换；极高=可绕过安全层直控电机。
> "证据"缩写：RG=运行中 ROS 图；MSG=接口定义文件；STR=二进制 strings；BT=行为树 XML；CFG=配置文件。

### 3.1 高层任务/行为入口（JSON Topic，`/business` 处理）

| 功能分类 | 接口名称 | 机制 | 接口类型 | 提供节点 | 调用方向 | 当前状态 | 控制效果 | 关键字段 | 控制权要求 | 风险 | 证据 | 尚缺信息 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 高层任务 | `/BusinessAppReq_topic` | Topic | `std_msgs/msg/String`(JSON) | `/business` | 发布 | 已验证 | APP 命令分发：拍照、语音、WiFi 配置、开关机、二次开发通道开关等 | JSON 字符串；已见命令名：`take-photo-tts` `human-fall-detect` `audio_receive` `enable_listen` `wifi_set` `play_tts` `boot_control` `sec_develop_control` `appping` (STR) | 部分命令受 OTA/跌倒检测状态门控（STR: "In human fall detected, skip app command!"） | 高 | RG+STR | 完整 JSON schema 与全部命令名清单 |
| 二次开发 | `/sec_develop_cmd_topic` | Topic | `std_msgs/msg/String`(JSON) | `/business`(task plugin) | 发布 | 已验证 | **厂商二次开发协议入口**：单/多电机控制、力矩控制、回零、头部运动、动作模仿，转发至 wbc（STR: "publish sec develop cmd: %s"） | 已见命令名：`single_motor_control` `multi_motor_control` `multi_motor_query` `motor_torque_control` `motor_home_set` `head_motion`(4元数组) `action_imitation`；字段 `sync` `timeout_ms` `time_ms`；关节名：`head_yaw` `head_pitch` `left_arm_shoulder_pitch` `left_arm_shoulder_roll` `left_arm_elbow` `right_arm_*` | 需先经 `sec_develop_control`(AppReq) 授权并进入 External_Control_mode（STR） | **极高** | RG+STR | JSON 字段完整定义、数值单位与限幅、模式切换握手 |
| 二次开发应答 | `/sec_develop_reply_topic` | Topic | `std_msgs/msg/String` | `/business` | 订阅 | 已验证 | 二次开发命令的执行结果回报 | JSON 字符串 | 无 | 低 | RG | 应答格式 |
| 高层任务应答 | `/BusinessAppRes_topic` / `/BusinessAppRpt_topic` | Topic | String | `/business` | 订阅 | 已验证 | APP 命令应答/上报 | — | 无 | 低 | RG | — |

### 3.2 ROS 2 Action（6+2 个）

| 功能分类 | 接口名称 | 接口类型 | 提供节点 | Goal 关键字段 | 当前状态 | 控制效果 | 风险 | 证据 |
|---|---|---|---|---|---|---|---|---|
| 导航 | `/start_to_navigation` | `navigation_msgs/action/StartToNavigation` | `/navigator_wrapper` | `map_id`(string), `pose`(PoseStamped), `angle`(float32) | 已验证 | 导航到指定地图上的目标点（封装 Nav2 全流程） | 高 | RG+MSG |
| 建图 | `/start_to_SLAM` | `cartographer_ros_msgs/action/StartToSLAM` | `/joyin_slam_pose_node` | `map_id`, `angle` | 已验证 | 启动 Cartographer 建图并原点对齐 | 高（机器人会原地旋转） | RG+MSG |
| 视觉跟踪 | `/start_to_track` | `mini_track_msgs/action/StartTrack` | `/vision_track_node` | (定义文件缺失，见待验证) | 已验证 | 视觉跟随目标人 | 高 | RG |
| 声源跟踪 | `/start_to_audio_track` | `audio_track_msgs/action/StartAudioTrack` | `/audio_track_node` | (同上) | 已验证 | 声源定位并转向/跟随 | 高 | RG |
| 头部声控 | `/start_to_control_head` | `audio_track_msgs/action/StartAudioTrack` | `/audio_track_node` | (同上) | 已验证 | 仅头部转向声源（不动底盘） | 中 | RG |
| 房间分割 | `/map_segmentation` | `map_optimization_msg/action/MapSegmentation` | `/room_segmentation` | (见包定义) | 已验证 | 对已建地图做房间自动分割 | 低（计算型） | RG |
| 硬件自检 | `/HardwareMcuFctCheck_action` | `hardware/action/McuFctCheck` | `/hardware` | `fct_check_mode`(uint8, 0=舵机) | 已验证 | MCU/舵机出厂自检（**会使舵机动作**） | 高 | RG+MSG |
| Nav2 内部 | `controller_server` 等生命周期 | lifecycle ChangeState | 各 Nav2 节点 | transition id | 已验证 | 重配置导航栈（会暂停导航能力） | 中 | RG |

注：ROS 图中**没有** `navigate_to_pose` / `follow_waypoints` 这类 Nav2 标准 Action 对外暴露——外部导航必须走 `/start_to_navigation` 或 `/goal_pose`。

### 3.3 运动命令 Topic（中层，进入 wbc）

| 功能分类 | 接口名称 | 接口类型 | 订阅方 | 当前状态 | 控制效果 | 关键字段（仅解释） | 风险 | 证据 |
|---|---|---|---|---|---|---|---|---|
| 全身/步态 | `/ControlBipedMotion_topic` | `plan/msg/BipedMotionCmd` | `/control`(wbc) | 已验证 | **核心全身控制命令**：模式+步态+预设动作+速度 | `control_mode`: 0=待机 1=步态行走 2=预设动作 3=速度控制；`gait_type`: 0停止 1前进 2后退 3/4原地转 5/6平移 7中步幅 8上小车 9下小车 10趴倒爬起 11后仰爬起 12姿态复位 13速度保持 14跨步 15单次转向 16调腿长 17阻尼(预留) 62小车上分腿(调试)；`linear_velocity_x` [-40,40]mm/s；`angular_rate_z` [-20,20]deg/s；`one_time_turn_angle` [-180,180]deg；`leg_height_setting_val` [130,210]mm；`action_type`: 1跌倒爬起 2上小车 3下小车…(预留)；跨步坐标/躯干姿态字段若干 | **极高** | RG+MSG(`/app/joyin/share/plan/msg/BipedMotionCmd.msg`) |
| 步态反馈 | `/ControlBipedMotionFeedback_topic` | `plan/msg/BipedMotionCmd` | `/control` 发布 | 已验证 | wbc 回显当前命令状态（采样: control_mode=0, gait=53） | 同上（只读） | 低 | RG+实测 |
| 底盘速度 | `/cmd_vel` | `geometry_msgs/msg/Twist` | `/control`(wbc) | 已验证 | Nav2 controller_server 输出的线速度/角速度，wbc 转为步态 | linear.x / angular.z（wbc 内部限幅，具体值未知） | 高 | RG |
| 底盘速度(轮) | `/ControlVehicleSpeed_topic` `/ControlVehicleVelocity_topic` | `Int16MultiArray` | `/hardware` | 已验证 | 轮电机速度/速度闭环直写（绕过 wbc 步态） | 数组元素对应轮电机，单位未知 | **极高** | RG |
| 头部控制 | `/ControlHeadControl_topic` `/ControlHeadMotion_topic` | `Int16MultiArray` | `/hardware` 与 `/control` | 已验证 | 头部云台（yaw/pitch）位置/运动控制 | 数组=[通道,值]编码，具体顺序未知 | 高 | RG |
| 关节直写 | `/ControlMotorSpeed_topic` `/ControlMotorPositionSet_topic` `/ControlMotorAngles_topic` | `Int16MultiArray` / `Float32MultiArray` | `/hardware` | 已验证 | 单电机速度/位置/角度直写（工厂调试层） | 索引对应关节，单位未知 | **极高** | RG |
| 电机参数 | `/ControlMotorRangeSet_topic` `/ControlMotorTime_topic` `/ControlSaveMotor_topic` `/ControlGetPid_topic` `/ControlPidControl_topic`(`control/msg/PidControl`) `/ControlImuCal_topic` | Int16MultiArray / PidControl / UInt8 | `/hardware` | 已验证 | 电机行程/时间设定、PID 读写、保存、IMU 标定 | — | 高（可永久改变标定） | RG+MSG |
| 电机使能 | `/ControlEnableMotor_topic` | `UInt8MultiArray` | `/hardware` `/control` | 已验证 | 逐电机使能/失能（=伺服上电控制） | 数组元素对应电机开关 | **极高** | RG |
| 手动初始化 | `/manual_enable_servo_and_init_imu` | `Empty` | `/hardware` | 已验证 | 手动模式使能舵机并初始化 IMU（工厂流程） | 无字段 | 高 | RG |
| 回充 | `/ControlGoCharge_topic` → `/ControlGoChargeDone_topic` | `UInt8` | `/control`(charging_dock) 发布 | 已验证 | 触发回充流程（charging_dock_plugin 内部状态机） | 数值=命令码（含义未知） | 高 | RG+CFG(control_pm.json) |
| 充电许可 | `/BusinessEnableCharge_topic` | `UInt8` | `/hardware` | 已验证 | 充电回路使能 | 0/1 | 中 | RG |
| 舞蹈状态 | `/ControlDanceState_topic` | `UInt8` | `/control` 发布 | 已验证 | 舞蹈动作状态广播（wbc 内含 Dance 相关逻辑） | 状态码未知 | 中 | RG+STR |
| 灵敏度 | `/ControlMotionSensitiveCtl_topic` | `UInt8` | `/hardware` | 已验证 | 运动灵敏度档位设置 | 档位值未知 | 低 | RG |
| 手臂动作 | `/arm_action_topic` | `UInt8` | `/utils` 发布 | 已验证 | 手臂预设动作触发（动作码未知） | 动作码未知 | 高 | RG |
| 动作交互 | `/action_interaction` | `UInt8` | `/vision` 订阅 | 已验证 | 情感/交互动作触发（视觉侧消费） | 码表未知 | 中 | RG |

### 3.4 模式切换 / 建图 / 感知控制 Topic

| 功能分类 | 接口名称 | 接口类型 | 方向 | 当前状态 | 控制效果 | 风险 | 证据 |
|---|---|---|---|---|---|---|---|
| SLAM 控制 | `/slam/start`(UInt8) `/slam/cancel` `/slam/pause` `/slam/resume`(Empty) `/slam/save`(Empty) | std_msgs | → `/joyin_slam_pose_node` | 已验证 | 开始/取消/暂停/继续/保存建图（`/business` SlamControl 发布） | 高(start/resume 引发运动) | RG |
| SLAM 状态 | `/slam/status`(UInt8) `/slam/feedback`(String) | std_msgs | ← 订阅 | 已验证 | 建图状态机回报 | 低 | RG |
| 语义建图 | `/semantic_mapping/trigger` | Empty | → `/vision` | 已验证 | 触发语义建图采集 | 中 | RG |
| 导航目标 | `/goal_pose` | `PoseStamped` | → navigator 链 | 已验证 | 单点导航目标（RViz 兼容入口，效果待验证） | 高 | RG |
| 虚拟墙 | `/explore_costmap/forbidden_area` `/global_costmap/forbidden_area` `/local_costmap/forbidden_area` | `navigation_msgs/srv/SetProhibitionAreas` | Service | 已验证 | 设置禁区/虚拟墙（改变导航行为） | 中 | RG+MSG |
| 上下小车 | `/vision/on_car_final_docking/start`(Empty) `/vision/on_car_final_docking/result`(String) | std_msgs | → `/vision` | 已验证 | 上小车最终视觉对接触发 | 高 | RG |
| 视觉开关 | `/vision/face_landmarks_enable/disable` `/vision/hand_gesture_enable/disable` `/vision/collect/control` | Int32 | → `/vision` | 已验证 | 各视觉检测通道开关 | 低 | RG |

### 3.5 Service（控制相关，非 lifecycle/parameter）

| 功能分类 | 接口名称 | 接口类型 | 提供节点 | 当前状态 | 控制效果 | 风险 | 证据 |
|---|---|---|---|---|---|---|---|
| 导航暂停 | `/pause_to_navigation` | `navigation_msgs/srv/PauseToNavigation` | `/joyin_navigation_node` | 已验证 | 暂停/恢复导航会话 | 中 | RG+MSG |
| SLAM 暂停 | `/pause_to_SLAM` | `cartographer_ros_msgs/srv/PauseToSLAM` | `/joyin_slam_pose_node` | 已验证 | 暂停/恢复建图 | 中 | RG+MSG |
| 地图存档 | `/write_state` | `cartographer_ros_msgs/srv/WriteState` | `/cartographer_node` | 已验证 | 写 Cartographer 状态文件 | 低 | RG |
| 跟随开关 | `/follow_polar/control` `/audio_track/follow_polar_control` | `std_srvs/srv/SetBool` | `/vision_track_node` `/audio_track_node` | 已验证 | 视觉/声源跟随启停 | 中 | RG |
| 头部暂停 | `/audio_track/pause_head_control` `/track/pause_track_control` | SetBool | audio_track/vision_track | 已验证 | 暂停头部/跟踪运动 | 中 | RG |
| 感知通知 | `/inform_perception` | `perception_msgs/srv/InformPerception` | `/perception_node` | 已验证 | 通知感知模块当前任务类型 | 低 | RG+MSG |
| 传感器总开关 | `/all_enable` `/all_disable` | `std_srvs/srv/Trigger` | `/media_video_manager` | 已验证 | 一键启停全部相机/雷达/LCD/ToF/4G | 中（会使机器人"致盲"） | RG |
| 单传感器 | `/video_enable/disable` `/lcd_enable/disable` `/lidar_enable/disable` `/itof_enable/disable` `/net_4g_enable/disable` `/video_scene_day/night` `/video_request_idr` `/h264_source_usb/vin` | Trigger | `/media_video_manager` | 已验证 | 各传感器开关/模式切换 | 低-中 | RG |
| **出厂自检** | `/factory_auto` `/factory_jpeg` | Trigger | `/hardware` | 已验证 | **工厂自动化测试（含电机动作），严禁调用** | **极高** | RG |
| **BLE 控制** | `/GattServiceControl` `/RemotePairControl` | `hardware/srv/GattServiceControl` | `/hardware` | 已验证 | BLE 遥控器配对/控制通道（另一条独立控制入口） | 高 | RG+MSG |
| 昼夜测试 | `/day_night_fake` | GattServiceControl | `/hardware` | 已验证 | 强制昼夜模式（测试用） | 低 | RG |
| 音量 | `/speaker_volume_control` | `video_ros_msgs/srv/VolumeControl` | media | 已验证 | 扬声器音量 | 低 | RG |
| 音频指令 | `/UtilsAudioInstruct_srv` `/UtilsAudio_srv` | Trigger | (utils 客户端/他端服务) | 已验证 | 音频唤醒/播报控制 | 低 | RG |
| 地图工具 | `/get_segment_map` `/extract_area_map_from_labeled_map` | map_optimization_msg/srv | `/room_segmentation` | 已验证 | 房间地图查询/导出 | 低 | RG |
| 感知地图 | `/perception/get_global_map` `/perception/rotate_map_priors` | perception_msgs/srv | `/perception_node` | 已验证 | 全局地图获取/旋转先验 | 低 | RG |
| 录制 | `/mp4_save/start|stop` `/cloud_storage/start|stop` `/cloud_multimodal/start|stop` | Trigger | media | 已验证 | 视频/云存储录制开关 | 低 | RG |

### 3.6 行为树入口（不可直接外部调用，经任务触发）

`business_task_plugin`（MoveController）加载 `/app/joyin/config/` 下 27 棵 BT.CPP v4 XML：

`taskCommandMainTree` `taskCommandSubTree` `goChargeTree` `goChargeAfterMapTree` `getOnCarTree` `visualGetOnCarTree` `buildMapMainTree` `patrolMain` `patrolSubTree` `patrolStudyCompanionSubTree` `wanderMainTree` `wanderSubTree` `standByTree` `naviSubTree` `naviSimpleSubTree` `obstacleTrackTree` `audioLocationTree` `audioLocationHeadControlTree` `studyCompanionMainTree/SubTree` `leaveChargerSubTree` `offChargerSubTree` `offChargerSimpleSubTree` `offChargerVehicleSubTree` `shortRangeDockingSubTree` `positionCheckTree` `positiveTwecall` `staticDetect`

关键 BT 原子节点（XML 实证）：`BodyReady` `OffChargerVehicle` `GoCharge` `NavigateToTarget` `GetNextTarget` `Relocation` `StartFaceTrack` `IsOnCharger` `HasMap` `CheckInCharger` `PlayLocalAudio` `StartRtmp/StopRtmp` `InformPerception`。

任务触发来源（STR 实证）：手柄按键、语音指令、`/BusinessAppReq_topic`、`/sec_develop_cmd_topic`、APP（经 tciot/MQTT）。**行为树节点名不是外部接口**，仅证明内部能力存在。

### 3.7 厂商 CLI / HTTP / SDK

- **plugman 框架**：`/app/joyin/bin/plugman <pm.json>` 启动 5 个插件容器进程（hardware/control/vision_node/business/utils）。无对外 CLI 参数文档（`--help` 未测试，避免干扰）。
- **未发现 HTTP REST 控制接口**：进程表中无 web 服务端口监听证据（未做端口扫描，避免越权）；云端交互走 `tciot_node`（MQTT/IoT，`/BusinessMqttOnline_topic` 等线索）。
- **SDK/动态库**：`/opt/joyin/lib/` 下 40+ 业务插件 `.so`（闭源），接口仅通过 ROS Topic/Service/Action 暴露。

---

## 4. 状态与反馈接口表（dashboard 可直接订阅，均为只读）

| 类别 | Topic | 类型 | 发布者 | 采样结果/备注 |
|---|---|---|---|---|
| 电池/充电 | `/HardwareBat_topic` | `hardware/msg/Bat` | `/hardware` | 实测: bat=100%, on_charger=2, is_charging=1；字段含电压/电流/循环 |
| 全身状态 | `/ControlBipedMotionFeedback_topic` | `plan/msg/BipedMotionCmd` | `/control` | 实测: control_mode=0, gait_type=53（静止） |
| 运动学 | `/HardwareKinematic_topic` | `hardware/msg/KinematicState` | `/hardware` | 关节位置/速度/力矩（高频） |
| 底盘反馈 | `/HardwareVehicleFeedback_topic` | `hardware/msg/VehicleFeedback` | `/hardware` | 轮速/里程（高频） |
| 头部位置 | `/HardwareHeadPosition_topic` | `hardware/msg/HeadPosition` | `/hardware` | 字段: index/pitch/yaw |
| 电机遥测 | `/HardwareMotorPosition/Speed/Current/Load/Temp/Err/P/I/D/Range_topic` | MultiArray | `/hardware` | 高频关节遥测；Err 为故障码 |
| IMU | `/HardwareImuStd_topic` `/HardwareImuBipedMotion_topic` | `sensor_msgs/msg/Imu` | `/hardware` | 标准与步态专用两路 |
| 激光 | `/HardwareLidar_topic` | `sensor_msgs/msg/LaserScan` | `/hardware` | 高频，dashboard 不宜长订阅 |
| 跌倒 | `/HardwareSelfFall_topic` | `UInt32` | `/hardware` | 自跌倒状态码 |
| 电机使能状态 | `/MotionEnableMotor_topic` | `UInt8MultiArray` | `/control` | 各电机使能位图 |
| 按键 | `/HardwareKey_topic` | `UInt16` | `/hardware` | 实体按键事件 |
| 遥控状态 | `/SystemRemoteStatus`(Int32) `/HardwareRemoteControl_topic`(String) | std_msgs | `/hardware` | BLE 遥控器连接/命令 |
| SLAM | `/slam/status`(UInt8) `/slam/feedback`(String) | std_msgs | `/joyin_slam_pose_node` | 建图状态机 |
| 位姿 | `/tracked_pose` `/tf` | PoseStamped/TFMessage | slam pose 链 | 定位输出 |
| 导航 | `/global_plan` `/plan_smoothed` `/lookahead_point` `/received_global_plan` | Path/PointStamped | Nav2 | 路径可视化 |
| 稳定性 | `/com_zmp_stabilizer/*`（8 个） | PointStamped/Bool 等 | motion | ZMP/支撑面/稳定标志 |
| 感知 | `/perception/obstacle_cloud` `/perception/narrow_space_obstacle`(UInt8) `/tof_obstacle`(UInt8) | 点云/UInt8 | perception | 障碍/窄道/ToF 状态 |
| 视觉结果 | `/vision/*_result` `/hobot_face_landmarks` `/hobot_hand_landmarks` | 各型 | `/vision` | 检测结果 |
| 跟踪调试 | `/track/*` `/audio_track/*`(path/grid/feedback) | 各型 | track 节点 | 跟随路径与反馈 |
| 系统状态 | `/HardwareStatus`(String) `/SystemWiFiStatus` `/lcd/health_probe` | String/Int32 | 各节点 | 组件健康 |
| APP 上报 | `/BusinessAppRpt_topic` `/BusinessAppRptWithQos_topic`(`business/msg/AppReport`) | String/自定义 | `/business` | 业务事件上报 |
| 电池低电 | `/BusinessLowPowerCtrl_topic`(UInt8) | UInt8 | → `/hardware` | 低电策略控制（控制类，列此供对照） |

---

## 5. 控制权与安全机制

| 机制 | 状态 | 证据与说明 |
|---|---|---|
| **控制模式机** | **已确认存在，语义部分未知** | wbc 字符串实证两级状态机：`robot_main_mode`（`Bipedal_Movement_mode` `Mode_Change_mode` `Robut_Limb_mode` `Move_Robot_To_Car_mode` `Move_Robot_Off_Car_mode` `Move_Robot_to_Charging_Pile_mode` `External_Control_mode` 等）与 `robot_state`（`STOP_STATE` 等）。`BipedMotionCmd.control_mode` 为接口级模式（IDLE/WALK_GAIT/PRESET_ACTION/VELOCITY_CTRL） |
| **二次开发模式切换** | 线索 | STR: "External_Control_mode: robot_state -> STOP_STATE; robot_main_mode -> Bipedal_Movement_mode" —— 进入外部控制前强制先停；`sec_develop_control` 命令（AppReq）疑似总开关。**握手细节未知** |
| **命令超时** | 未知 | wbc 有速度命令处理与定时器逻辑（`ros_vehicle_velocity_control_proc`），但未见明确超时常量；`/cmd_vel` 停发后是否自动停步**未确认** |
| **控制权申请/抢占** | 部分 | 多源并发存在：Nav2(`/cmd_vel`)、business(`/ControlBipedMotion_topic`)、手柄(`/joy_filtered`→utils 也发 `ControlBipedMotion`)、BLE(`/HardwareRemoteControl_topic`)。仲裁规则未见显式 token/lock，疑似靠 robot_main_mode 单一权威 + 最后写入。**风险：并发发布会互相覆盖，需厂商确认** |
| **急停** | 间接证据 | 未发现名为 estop 的接口。等效手段：`GAIT_STOP=0`/`MODE_IDLE=0`（仍为命令，非安全回路）；`/ControlEnableMotor_topic` 失能；`/all_disable`（仅传感器）；硬件层急停可能仅在手柄/MCU 侧（未知） |
| **使能/失能** | 已验证存在 | `/ControlEnableMotor_topic`(逐电机)、`/manual_enable_servo_and_init_imu`、`/MotionEnableMotor_topic`(状态回读) |
| **跌倒保护** | 已验证配置 | `business_abnormal_detect_plugin`（CFG: self_fall=1, self_hang=1 启用）；`miniBotMotionControlNodeParam.json`: `use_fall_down_detection`; BT `IsFallen/IsFallStandupEnabled` 节点（当前注释停用） |
| **充电桩互锁** | 已验证 | BT: `IsOnCharger` → `OffChargerVehicle` 先离桩再执行任务；回充流程 `CheckInCharger/shortRangeDocking` |
| **OTA 门控** | 线索 | STR: "ota running, skip app command" —— OTA 期间拒绝 APP 命令 |
| **人跌倒门控** | 线索 | STR: "In human fall detected, skip app command!"（human_fall 检测当前配置为 0=关） |
| **`control_pm` 与 `wbc_ctrl_plugin` 角色** | 已验证 | control_pm 是插件容器进程（加载 wbc_ctrl_plugin + charging_dock_plugin 两个插件到 `/control` 节点）；wbc_ctrl_plugin 是全身控制器本体（步态生成、模式机、cmd_vel→步态转换、头部控制、电机指令下发）；charging_dock_plugin 管理回充状态机 |
| **姿态前置条件** | 已验证(BT) | 上/下小车、跨步、爬起等动作在 BT 与 gait 枚举中存在前置状态检查（BodyReady/IsOnCharger）；直接发 gait=8/9 时 wbc 是否自行检查**未知** |

---

## 6. 推荐调用层级

**优先级从高到低：**

1. **首选 `/start_to_navigation` Action** —— 目标点导航（导航需求 90% 场景）。
2. **次选 `/BusinessAppReq_topic`** —— 已知安全的高层命令（拍照、播报等非运动类先行验证）。
3. **`/start_to_SLAM`、`/start_to_track`、`/start_to_audio_track`** —— 建图/跟随专用 Action，语义清晰、有完成反馈。
4. **`/slam/pause`、`/pause_to_navigation`、`/follow_polar/control`** —— 会话级暂停/恢复，风险中低。

**禁止/不建议直接调用：**

- `/ControlMotor*`、`/ControlVehicleSpeed`、`/ControlEnableMotor`、`/ControlHeadControl` —— 工厂层直写，绕过 wbc 平衡控制，**极易摔机**。
- `/factory_auto`、`/HardwareMcuFctCheck_action` —— 出厂自检，含电机动作。
- `/GattServiceControl`、`/RemotePairControl` —— 会破坏现有 BLE 遥控配对。
- lifecycle `change_state` 系列 —— 会瘫痪导航栈。
- `BipedMotionCmd` 直发 —— 在确认模式握手与超时行为前不要使用；**尤其不要发"零值"试探**（零值在 MODE_VELOCITY_CTRL 下同样是有效命令）。

**需要厂商文档才能用的：**

- `/sec_develop_cmd_topic` JSON 协议完整 schema（字段/单位/限幅/同步语义）。
- `BipedMotionCmd` 预留枚举（70–79、21–30）的实现状态。
- 控制权仲裁与 External_Control_mode 进入/退出流程。

**实测前安全准备清单：**

1. 物理安全员 + 急停手段（手柄实测有效范围）确认。
2. 场地清障、机器人离桩、有支撑物。
3. 先只读订阅 `/ControlBipedMotionFeedback_topic` + `/HardwareBat_topic` 建立监控。
4. 首次控制测试只允许在 External_Control_mode 下做小幅头部运动（`start_to_control_head` 或 `head_motion`），逐步升级。
5. 全程录 `rosbag`（仅订阅侧）以便复盘。

---

## 7. 待验证事项（需厂商资料或受控实测）

| # | 问题 | 为什么重要 |
|---|---|---|
| 1 | `/sec_develop_cmd_topic` JSON 完整 schema：每条命令的必选字段、电机索引/关节名映射、数值单位、限幅、`sync/timeout_ms` 语义、错误码表 | 这是二次开发主通道，schema 不全则无法安全编码 |
| 2 | 控制权仲裁规则：`robot_main_mode` 状态机转移图、External_Control_mode 进入/退出握手、多发布者并发时谁赢、`/cmd_vel` 断流超时时间 | 决定能否与 Nav2/APP/手柄共存控制，避免互相打架 |
| 3 | `mini_track_msgs/StartTrack` 与 `audio_track_msgs/StartAudioTrack` 的 .action 定义文件在机器人上缺失（`ros2 interface show` 失败），goal 字段只能从运行图确认类型存在 | 两个跟踪 Action 无法离线编写客户端 |
| 4 | `gait_type=53`（当前实测值）不在公开枚举 0–17/62 内 —— 53 及其他内部值的含义 | 正确解读待机/过渡态，dashboard 需要码表 |
| 5 | 急停链路：是否存在 MCU 级硬件急停、手柄急停键码、以及软件层是否有等效安全接口 | 目前没有找到任何软件急停 |
| 6 | 固件/软件版本号获取方式（`/middleware/version` 等路径不存在）；OTA 与 `BusinessOta*` 系列语义 | 变更管理与问题追溯 |
| 7 | `HardwareRemoteControl_topic`（BLE）命令格式 —— 遥控器如何映射到运动 | 另一条控制入口的安全评估 |
| 8 | `/ControlVehicleSpeed` vs `/ControlVehicleSpeed` 数组元素与轮电机映射、单位 | 若必须底层控制需要，但目前建议禁用 |

---

## 8. 证据附录

### 8.1 调查环境与时间

- SSH: `root@192.168.137.228`（密钥认证，已知主机文件 `D:\projects\m1-robot\.tools\robot_known_hosts`）
- 时间: 2026-11-09 20:20–21:30 CST（机器人 uptime 自 14:20:32 起）
- 内核: `6.1.83-DR-PL5.1_V1.1.0 #1 SMP PREEMPT Fri Oct 16 19:09:49 CST 2026`
- Buildroot 2022.08 / BusyBox 1.35.0 (2026-09-03)
- ROS 2 Humble (`/middleware/humble`)，叠加工作空间 `/app/joyin`（AMENT_PREFIX_PATH），插件库 `/opt/joyin/lib`
- `ROS_DOMAIN_ID=0`，`ROS_LOCALHOST_ONLY=1`，RMW=fastrtps

### 8.2 关键运行进程

| PID | 命令行 | 说明 |
|---|---|---|
| 1618 | `plugman /app/joyin/config/control_pm.json` | `/control` 节点（wbc_ctrl_plugin + charging_dock_plugin） |
| 1617 | `plugman /app/joyin/config/hardware_pm.json` | `/hardware` 节点 |
| — | `plugman /app/joyin/config/business_pm.json` | `/business` 节点（9 插件） |
| — | `plugman /app/joyin/config/vision_node_pm.json` `/utils_pm.json` | `/vision` `/utils` |
| — | navigator/cartographer/costmap/perception/room_segmentation/vision_track/audio_track 等导航感知栈 | — |

### 8.3 使用过的只读命令（全部无副作用）

```sh
# 进程与环境
ps aux --sort=-%cpu
tr '\0' '\n' < /proc/<pid>/environ | grep -E 'ROS|RMW|AMENT'
readlink /proc/<pid>/cwd
cat /app/joyin/run.sh

# ROS 图枚举（加载 /middleware/humble/setup.sh + AMENT_PREFIX_PATH=/app/joyin）
ros2 node list
ros2 topic list -t
ros2 service list -t
ros2 action list -t
ros2 node info /control /hardware /business /motion_server /navigator_wrapper /utils /vision /audio_track_node /vision_track_node /auto_explore /room_segmentation /media_video_manager /joyin_navigation_node
ros2 topic info -v /cmd_vel /sec_develop_cmd_topic /HardwareKinematic_topic /ControlBipedMotionFeedback_topic
ros2 action info /start_to_navigation /start_to_SLAM /start_to_track /start_to_audio_track /start_to_control_head
ros2 service type /all_enable /factory_auto /UtilsAudioInstruct_srv

# 状态 Topic 单次采样（--once，带 timeout）
ros2 topic echo --once /HardwareBat_topic /ControlBipedMotionFeedback_topic /HardwareStatus /slam/status /sec_develop_reply_topic

# 静态文件
cat /app/joyin/config/{control_pm,hardware_pm,business_pm,utils_pm,secondary_pm}.json
cat /app/joyin/config/*.xml                      # 27 棵行为树
cat /app/joyin/share/plan/msg/BipedMotionCmd.msg
cat /app/joyin/share/hardware/msg/{VehicleFeedback,HeadPosition,Bat,KinematicState,Motor}.msg
cat /app/joyin/share/navigation_msgs/action/StartToNavigation.action
cat /app/joyin/share/hardware/action/McuFctCheck.action
cat /userdata/joyin/config/control/*.json        # 运动学标定参数

# 二进制只读分析
strings /opt/joyin/lib/libwbc_ctrl_plugin.so | grep ...
strings /opt/joyin/lib/libbusiness_task_plugin.so | grep ...
strings /opt/joyin/lib/libbusiness_app_command_plugin.so | grep ...
grep -rl "taskCommandMainTree" /opt/joyin/lib/*.so
```

### 8.4 关键输出摘要

- `ros2 node list`: 46 个节点（含 2 个重名 `/local_map_node`）
- Topic 总数 236（类型化清单见 artifacts）；Service 非 parameter 类 58；Action 8
- `/control` 订阅: `/cmd_vel` `/sec_develop_cmd_topic`(注:实际订阅者为 business 转发链) `/ControlBipedMotion_topic` `/HardwareVehicleFeedback_topic` …；发布: `/ControlMotorSpeed` `/ControlVehicleSpeed` `/ControlEnableMotor` `/ControlHeadControl` `/MotionEnableMotor_topic` …
- `/hardware` 订阅全部 `/Control*` 指令 + `/Business{EnableCharge,PowerCtl,LowPowerCtrl,HeadLightCtl}`；Action Server: `/HardwareMcuFctCheck_action`
- `/HardwareBat_topic` 实测: `bat: 100, on_charger: 2, is_charging: 1`
- `/ControlBipedMotionFeedback_topic` 实测: `control_mode: 0, gait_type: 53, target_biped_state: 255, current_biped_state: 255`（待机）
- wbc STR: `robot_main_mode` 枚举 7+ 个；`External_Control_mode` 进入序列；`sec_develop` 命令名 6 个 + 关节名 8 个
- task plugin STR: `second develop cmd msg: %s` / `publish sec develop cmd: %s` / APP 命令名 9 个 / BT XML 文件名 27 个
- 附注：调查期间观察到机器人上已有一个第三方 `dashboard.py` 进程（PID 30802, `/userdata/dashboard/`）在运行，非厂商组件，本次未与其交互。

### 8.5 关键文件绝对路径（机器人侧）

| 路径 | 内容 |
|---|---|
| `/app/joyin/run.sh` | 启动脚本 |
| `/app/joyin/config/*.json` | 5 个插件容器配置 |
| `/app/joyin/config/*.xml` | 27 棵行为树 |
| `/app/joyin/share/plan/msg/BipedMotionCmd.msg` | 全身控制消息定义（含全部枚举） |
| `/app/joyin/share/hardware/{msg,srv,action}/` | 硬件层接口定义 |
| `/app/joyin/share/navigation_msgs/{action,srv}/` | 导航 Action/Service 定义 |
| `/opt/joyin/lib/libwbc_ctrl_plugin.so` | 全身控制器（闭源） |
| `/opt/joyin/lib/libbusiness_task_plugin.so` | 行为树引擎（闭源） |
| `/userdata/joyin/config/control/` | 运动学标定（含 .bak） |

### 8.6 证据等级统计

| 等级 | 数量 | 说明 |
|---|---|---|
| 已验证（运行图 + 类型确认） | **47** | 总表所有"已验证"行：4 JSON 入口 + 8 Action + 17 运动 Topic + 4 SLAM/感知 + 24 Service + （其中部分行合并统计） |
| 已安装但未运行 | 0 | 未发现独立的未加载接口包 |
| 线索 | **9** | sec_develop JSON 字段细节、模式机转移、OTA/跌倒门控、gait=53 含义、`/goal_pose` 实际消费方、`arm_action` 码表、`ControlGoCharge` 码值、BLE 命令格式、StartTrack/StartAudioTrack 字段 |
| 未知 | 3 | 命令超时行为、软件急停存在性、并发仲裁赢家 |

---

*报告完。本清单仅用于接口理解与后续开发规划，不构成调用许可；任何控制测试须先完成第 6 节安全准备。*
