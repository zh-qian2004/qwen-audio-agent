# vision_service — M1 视觉能力网关

`vision_service` 是独立于 Dashboard、Agent 和厂商视觉插件的二次开发视觉能力网关。
目标检测是第一个生产 Processor；后续的人脸、姿态、OCR、分割和跟踪能力在相同边界内扩展。

服务的核心原则是：**统一控制面和结果面，原始图像数据面就近直连**。它不会成为集中复制原始
视频帧的 Python 总线，也不会接管厂商 `media_video`。

## 1. 当前能力

- `object_detection` Processor：复用厂商已经运行在 X5 BPU 上的多任务检测模型。
- 输入：`/vision/multitask_result`（`std_msgs/msg/String` JSON）。
- 开关：`/vision/multitask_detect_flag`（`std_msgs/msg/Int32`，`1` 开、`0` 关）。
- 类别：`person`、`head`、`face`、`hand`。
- 坐标：厂商模型 960×544 像素坐标，同时输出归一化坐标。
- 输出：统一 DetectionFrame、最近结果、有限历史和 WebSocket 事件流。
- 指标：输入/输出 FPS、丢弃/解析错误、结果年龄、CPU/RSS、BPU 利用率和频率。
- Processor 默认不自动启动，避免机器人启动后无条件增加 BPU 负载。

现有厂商 80 类 `scene_tag` 模型只对外提供 `Int32MultiArray` 场景标签，未提供通用检测框，
因此第一版没有把它伪装成通用目标检测。以后接入自研 80 类 BPU Processor 时，可以保持
本文定义的 DetectionFrame 和 API 不变。

## 2. 架构与边界

```text
厂商 media_video → /camera/image_nv12 ───────────────→ 后续自研 BPU Processor
       │
       └→ 厂商 multitask BPU → /vision/multitask_result
                                      │
                              object_detection
                              解析 / 过滤 / 标准化
                                      │
                                 ResultBus
                            latest + bounded history
                                      │
                    HTTP / WebSocket → Dashboard / Agent
```

`vision_service` 负责：

- Processor 白名单、启停、状态和错误归一化；
- 厂商结果适配和统一 DetectionFrame；
- 最新结果、有限历史、事件流和慢消费者隔离；
- 模型、话题、帧率、延迟、BPU 和进程资源的可观测性。

`vision_service` 不负责：

- 打开相机设备、停止或重启 `media_video`；
- 在 FastAPI 线程中执行模型推理；
- 为每个消费者复制 NV12 原始帧；
- 服务端绘框、重新编码 JPEG、录像或云端上传；
- 把 2 fps Dashboard 预览用于机器人闭环控制。

## 3. 源开关所有权

厂商检测开关不是带租约的接口。服务采用保守所有权策略：

1. Processor 的 ROS 订阅始终存在，可观察已有结果流。
2. 启动前等待 `probe_existing_stream_seconds`。
3. 若已有新鲜结果，认为其他模块已经启用检测，本服务不发布 `1`，停止时也不发布 `0`。
4. 若无结果，本服务发布 `1` 并记录 `owns_source_enable=true`。
5. 只有本服务拥有开关时，停止才发布 `0`。

这能避免常见的“观察者关闭了其他模块正在使用的检测”问题，但厂商接口没有真正的跨进程
引用计数。如果未来多个二次开发服务都需要控制该模型，应将开关仲裁完全收口到本服务。

## 4. DetectionFrame

```json
{
  "type": "detection_frame",
  "processor": "object_detection",
  "source": "vendor_multitask",
  "model": "multitask_body_head_face_hand_kps_960x544.hbm",
  "source_frame_sequence": 638688,
  "source_timestamp": null,
  "received_at": 1794299000.123,
  "source_width": 960,
  "source_height": 544,
  "detection_count": 1,
  "detections": [
    {
      "id": "638688:body:0",
      "class_id": 0,
      "label": "person",
      "source_label": "body",
      "confidence": 0.998809,
      "bbox": {"x1": 9, "y1": 212, "x2": 434, "y2": 544, "width": 425, "height": 332},
      "bbox_normalized": {"x1": 0.009375, "y1": 0.389706, "x2": 0.452083, "y2": 1.0}
    }
  ]
}
```

厂商消息目前只有 `frame_seq`，没有 ROS Header 时间戳，因此 `source_timestamp` 明确为 `null`；
`received_at` 是本服务收到结果的墙钟时间。Dashboard 后续叠框时必须结合结果年龄做过期隐藏，
不能声称它与 JPEG 帧具有严格的逐帧同步关系。

## 5. API

服务只监听 `127.0.0.1:8092`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务、ROS、Processor、ResultBus、CPU/RSS/BPU |
| GET | `/api/vision/status` | 与 health 相同的视觉全量状态 |
| GET | `/api/vision/processors` | Processor 列表与状态 |
| POST | `/api/vision/processors/object_detection/start` | 幂等启动目标检测 |
| POST | `/api/vision/processors/object_detection/stop` | 幂等停止目标检测 |
| GET | `/api/vision/detections/latest` | 最新检测及年龄/过期标记 |
| GET | `/api/vision/detections/history?limit=20` | 有界历史 |
| WS | `/ws/vision/events` | 最新优先事件流，5 秒心跳 |

服务不接受任意 Processor 名称、模型路径、ROS 话题或 shell 命令。Processor 只能来自配置白名单。

## 6. 配置

配置文件为 `config/service.json`。关键字段：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `service.port` | 8092 | 仅回环监听 |
| `service.history_size` | 100 | 只保存结构化结果，不保存图像 |
| `service.result_stale_after_seconds` | 1.5 | API 最新结果过期阈值 |
| `auto_start` | false | 是否随服务启动 Processor |
| `confidence_threshold` | 0.5 | 低于阈值的框不输出 |
| `source_stale_after_seconds` | 1.0 | 厂商结果流在线阈值 |
| `probe_existing_stream_seconds` | 0.75 | 启动前观察已有流的时间 |
| `manage_source_enable` | true | 是否管理厂商 0/1 开关 |

## 7. 本地测试

```powershell
cd D:\projects\m1-robot\vision_service
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile src\*.py src\processors\*.py
```

本地未安装 FastAPI 时，3 项 API 测试会跳过；解析器、Processor 和 ResultBus 测试仍会执行。
机器人端应使用与服务完全相同的 ROS/动态库环境运行全部测试：

```bash
/userdata/vision_service/scripts/test.sh
```

测试覆盖 JSON 解析、阈值过滤、越界裁剪、标准化坐标、非法消息、ResultBus 有界历史、
等待唤醒、Processor 幂等启停和源开关所有权。

## 8. 机器人部署

机器人端目录：`/userdata/vision_service/`。

推荐创建独立环境：

```bash
python3 -m venv --system-site-packages /userdata/vision_service/.venv
/userdata/vision_service/.venv/bin/pip install \
  -r /userdata/vision_service/config/requirements.txt
```

过渡期若独立环境不存在，`run.sh` 会复用 Dashboard 的 Python 环境；这是为了当前机器无需再次
下载相同依赖即可启动，不是长期依赖关系。

```bash
/userdata/vision_service/scripts/run.sh
/userdata/vision_service/scripts/status.sh
/userdata/vision_service/scripts/stop.sh
```

启动检测：

```bash
busybox wget -qO- --post-data='' \
  http://127.0.0.1:8092/api/vision/processors/object_detection/start
```

停止检测：

```bash
busybox wget -qO- --post-data='' \
  http://127.0.0.1:8092/api/vision/processors/object_detection/stop
```

停止服务时先停止 Processor；只有 `owns_source_enable=true` 时才关闭厂商多任务检测。

## 9. 后续 Processor 接入约束

自研 BPU Processor 应作为独立工作进程或原生 ROS 2 节点，直接订阅 `/camera/image_nv12`，
使用 `KeepLast(1)` 和 latest-frame 语义。它只把结构化结果交给 `vision_service`，不得把模型
推理放入 FastAPI 事件循环，也不得让慢客户端反压相机入口。

新增 Processor 时必须提供：固定配置 schema、统一生命周期、DetectionFrame 或专用结果 schema、
输入/输出 FPS、延迟、丢帧、CPU/RSS/BPU 指标、离线降级、测试和回滚说明。

## 10. 2026-11-10 实机验收

验收平台为当前 M1 机器人。检测启用后连续输出标准化结果，实测如下：

| 指标 | Processor 停止 | Processor 运行 |
|---|---:|---:|
| vision_service CPU | 约 0.7–1.2% 单核 | 约 9–15% 单核 |
| vision_service RSS | 约 86 MiB | 约 86 MiB |
| BPU 利用率 | 0% | 约 3–5% |
| 标准化结果频率 | 0 Hz | 约 10 Hz |
| 解析错误 | 0 | 0 |

实测结果包含 `person/head/face/hand` 检测框，坐标被正确限制在 960×544 模型范围内。
停止 Processor 后 BPU 回到 0%，ROS Runtime 和 HTTP 服务保持在线。最终交付状态为：
`vision_service` 正在运行，`object_detection` 为 `stopped`，不会持续占用 BPU。

## 11. 回滚

本服务没有修改厂商 `/app`、`/opt`、相机或视觉配置，也没有配置开机自启。最小回滚只需：

```bash
/userdata/vision_service/scripts/stop.sh
```

停止服务会先停止 Processor；如果本服务拥有厂商检测开关，会发布 `0` 后再销毁 ROS 节点。
Dashboard、`media_video` 和厂商视觉进程不会被停止或重启。
