# rodesk / roserver / roboagent 系统集成与重构设计

## 1. 系统定位、真实现状与总体边界

整个系统采用标准的前后端架构，其中：

```text id="59d52c"
rodesk
    Frontend / Presentation Layer

roserver
    Application Backend

roboagent
    Agent Runtime / SDK
```

整体关系为：

```text id="etwtpx"
                         rodesk
                  Web / Desktop Frontend
                    Vue 3 + Tauri
                           │
              HTTP / WebSocket / WebRTC
                           │
                           ▼
                        roserver
                  Application Backend
                      │          │
                      │          │
                      ▼          ▼
                 roboagent   RobotService
                Agent Runtime     │
                                  ▼
                           机器人后端
                                  │
                                  ▼
                        ROS2 / ros2_control
                         MoveIt / Nav2 / SDK
```

三者职责固定为：

### rodesk

负责：

- Web UI；
- Desktop UI；
- 用户交互；
- Session / Conversation 展示；
- Assistant streaming 展示；
- Tool / Approval / Nested Agent 展示；
- Robot State UI；
- Teleoperation UI；
- Call / Camera / Voice UI；
- Browser / Tauri 平台适配；
- 本地 View Model 和 UI Cache。

不负责：

- Agent Runtime；
- Session canonical transcript；
- Robot control semantics；
- ROS2；
- Tool 执行；
- Product state persistence；
- 机器人后端；
- Agent lifecycle。

### roserver

负责：

- Product API；
- Application State；
- Session 产品资源管理；
- RunRecord；
- RunProjection；
- Runtime Event → Product Event 投影；
- Idempotency；
- Approval 产品交互；
- AgentFactory；
- RobotService；
- RobotBackend；
- Media signaling；
- Teleoperation 产品协议；
- Artifact 管理；
- Health / Diagnostics；
- 后续 Authentication / Authorization。

roserver 不重新实现：

```text id="3bf5tu"
Agent Loop
Model Provider abstraction
Tool Runtime
Skill Runtime
MCP Runtime
Speech Runtime
Canonical Transcript
```

这些继续属于 roboagent。

### roboagent

继续定位为：

```text id="aj9goz"
Reusable Python Agent Runtime
```

负责：

- Agent；
- Session；
- Run；
- Model；
- Context；
- Canonical Message；
- Tool；
- Effect；
- ApprovalProvider；
- Skill；
- MCP；
- Persistence；
- Nested Execution；
- Speech Runtime；
- Multimodal Runtime。

roboagent 不负责：

```text id="c0s2qt"
FastAPI Product API
rodesk-specific schema
RobotService
ROS2
Teleoperation API
WebRTC signaling
Product metadata
HTTP authentication
```

### 1.1 真实项目现状

当前三个仓库的成熟度差异很大，后续计划必须基于真实现状，而不是把三者当成同等成熟。

#### roboagent

当前已经是较成熟 Runtime。

已存在：

```text id="5uu1vp"
canonical transcript
6 类 Content
Agent / Session / Run
tool runtime
effect model
approval provider
nested execution
event lineage
session persistence
CAS revision
asyncio.to_thread persistence offload
speech runtime
tests
CI
```

因此 Phase 0 不应重复开发这些能力。

真正需要修改的主要是：

```text id="k60jdr"
stable message_id
message_id runtime propagation
clear_pending()
少量 Application Host convenience API
nested child model.* lineage 验证 / 修补
tool_batch.committed per-tool effect detail
```

#### roserver

当前基本只有设计文档。

因此：

```text id="knj9zs"
ApplicationStore
AgentService
AgentFactory
FastAPI
WebSocket
RobotService
RobotBackend
ArtifactService
Media signaling
TeleoperationService
```

都属于净新增。

不能把 roserver 描述成“重构”。

它实际上是：

> 从零建立整个 Application Backend。

#### rodesk

当前已经拥有成熟度较高的 UI 外壳，但真实业务接入层几乎不存在。

可以复用：

```text id="4bz7hd"
Vue 3 / Pinia / Router
Tauri wrapper
Agent 页面 UI
Call 页面 UI
Teleoperation HUD
useWebRTC
MediaStreamPlayer
CameraSignaling abstraction
visual regression tests
platform adapters
```

需要新建或大幅重构：

```text id="5xya6j"
HTTP client
WebSocket client
Product AgentService
Session API client
Run model
RunProjection reducer
Tool call pairing
Approval
Nested run handling
Artifact transport
Real Call service
Media signaling implementation
Real Teleoperation service
Robot state subscription
```

因此 rodesk 的工作性质应描述为：

> 保留现有 UI 和平台外壳，重新建立真实业务接入层。

---

### 1.2 依赖方向

源码依赖必须保持：

```text id="6pm8le"
rodesk ──network──► roserver ──python dependency──► roboagent
                       │
                       └────► robot backend
                              （当前只有模拟实现）
```

机器人通信方向：

```text id="7grs3y"
roserver
   │
  ROS2
   │
   ▼
mfr3duo 机器人栈
（ros2_control / MoveIt / Nav2 / SDK）
```

禁止：

```text id="m0yhk5"
rodesk 直接依赖 roboagent
rodesk 直接连接 ROS2
roboagent import roserver
roboagent import RobotService
引入 gRPC 或新增 C++ 网关进程
```

### 1.3 roserver ↔ 机器人通信

正式冻结：

> 机器人通信统一使用 **ROS2**。
> **不引入 gRPC**，不新增 proto，不新增独立 C++ 网关进程。

项目当前**尚未进入机器人集成阶段**，因此：

```text id="pm-robot-stage"
现在   roserver 只定义机器人边界，并只提供模拟机器人（SimulatedRobotBackend）
       Product API / 控制权 / 遥操作语义全部真实实现，但驱动的是模拟状态
将来   用 ROS2 实现填充同一个边界，Product API 与前端契约不变
```

边界形态：

```text id="v4d6px"
roserver
Python（≥3.12）
    │
    │ RobotBackend 抽象接口
    ▼
SimulatedRobotBackend      ← 今天只有这个实现
    ┆
    ┆ （将来替换为 ROS2 实现）
    ▼
ROS2 ──► ros2_control / MoveIt / Nav2 / SDK
```

关于进程形态：

```text id="pm-robot-process"
v1 不冻结 roserver 是否内嵌 rclpy。
在机器人集成阶段再决定：
    a) roserver 进程内使用 rclpy
    b) 独立 ROS2 节点/桥接进程，两侧仍以 ROS2 接口通信
无论选哪种，对 roserver 上层暴露的都是 RobotBackend 接口。
```

不引入 gRPC 的原因：

1. 机器人侧本来就是 ROS2 原生栈，再包一层 gRPC 只增加一层协议与一份 proto 维护成本；
2. 项目尚未进入机器人集成阶段，提前冻结跨语言进程边界没有必要；
3. ROS2 已是既有的、可复用的通信与生命周期基础设施。

当前阶段模拟实现必须只暴露真实具备的能力：

```text id="r77xlv"
health
robot state
dummy / simulation command
```

在 `mfr3duo_nav` / `mfr3duo_robot` 完成之前，不允许虚构真实：

```text id="j55zwl"
navigate()
task execution
```

能力（`capabilities` 只能是 `["state", "teleoperation"]`）。

---

### 1.4 状态归属

系统状态明确分为 Runtime State、Application State 和 UI State。

| 状态 | 权威归属 |
|---|---|
| canonical transcript | roboagent |
| canonical message | roboagent |
| tool call/result transcript | roboagent |
| Runtime Session revision | roboagent |
| pending steer/follow_up | roboagent |
| Session title | roserver |
| Session product metadata | roserver |
| RunRecord | roserver |
| active_root_run_id | roserver |
| IdempotencyRecord | roserver |
| ApprovalRecord | roserver |
| Artifact metadata | roserver |
| Public Event sequence | roserver |
| RunProjection | roserver，进程内 |
| Robot state projection | roserver |
| control authority | roserver + 机器人后端 |
| UI view model | rodesk |

一个 Product Session 与一个 Runtime Session：

```text id="ajv9wt"
1 : 1
```

并使用同一个：

```text id="qzuag8"
session_id
```

对外统一称为：

```text id="m9nr44"
Session
```

---

### 1.5 ApplicationStore

roserver 使用 SQLite 作为 V1 ApplicationStore。

至少包含：

```text id="qbin3c"
sessions
runs
idempotency
approvals
artifacts
robot_authorities
```

`robot_authorities` 至少包含：

```text id="pm-robot-authority-fields"
robot_id
owner_id
authority_id
mode
holder_id
acquired_at
expires_at
```

```text id="pm-robot-authority-rules"
authority_id 是 acquire 时生成的稳定标识，客户端命令必须回显它
mode v1 只有 teleoperation
acquire 必须是单锁内的原子 check-and-upsert，否则并发 acquire 会双双成功
```

所有用户资源从第一版保留：

```text id="tj8lcv"
owner_id
```

V1 单用户环境：

```text id="mzvhr2"
owner_id = "local"
```

ApplicationStore 不保存第二份 canonical transcript。

SessionMetadata：

```text id="v8rw9j"
session_id
owner_id
title
internal_state
active_root_run_id
created_at
updated_at
```

内部状态：

```text id="7rrpw1"
provisioning
ready
deleting
```

公开状态：

```text id="4zeed1"
idle
running
```

### 1.6 Runtime metadata 的归属

roboagent 已有：

```text id="xrsbkt"
SessionSnapshot.metadata
Session.set_metadata()
```

为了避免 Product Metadata 双真源，正式规定：

> Runtime metadata 不用于存储 Session title、owner、Product status、active_run_id 等产品字段。

Runtime metadata 仅允许用于：

```text id="uomwqz"
runtime extension
provider/runtime-specific opaque metadata
```

Product metadata 唯一权威：

```text id="eglvpm"
roserver ApplicationStore
```

### 1.7 并发模型

roboagent 明确采用 same-event-loop contract。

V1 固定：

```text id="f6or4g"
ONE roserver process
ONE asyncio event loop
ONE uvicorn worker
```

允许：

```bash id="ju3ea6"
uvicorn roserver.app:app --workers 1
```

禁止：

```bash id="s8jy6n"
uvicorn roserver.app:app --workers 4
```

roserver 入口在检测到 `--workers > 1` 时必须**拒绝启动**（退出码非 0），
而不是在运行时做 hack 掩盖。

推荐启动方式（factory 形式，避免模块级 app 污染测试导入）：

```bash id="pm-uvicorn-factory"
uvicorn roserver.app:create_app --factory
```

需要注意：

> single-event-loop 不等于 single-thread。

roboagent 已经使用：

```text id="3egkqg"
asyncio.to_thread
```

处理文件 I/O 等 blocking task。

允许后台线程处理：

```text id="4fb7zm"
immutable persistence payload
file handles
blocking filesystem work
```

但禁止：

```text id="oibwut"
Session
Run
Agent
mutable Runtime objects
```

离开 owning event loop。

roserver 必须主动遵守这一契约，因为 Runtime 当前不会机械检测跨 loop 访问。

#### 进程生命周期

Startup：

```text id="pm-startup-order"
open ApplicationStore
    ↓
purge expired idempotency
    ↓
startup reconciliation（见 §2.12）
    ↓
start serving
```

Shutdown 必须按所有者关闭，并且**必须取消 active Run**：

```text id="pm-shutdown-order"
close / cancel pending approvals
    ↓
cancel active Runs 并等待其收敛（有界超时）
    ↓
cancel 事件 pump 任务
    ↓
close Runtime Sessions
    ↓
close ApplicationStore
```

要求：

```text id="pm-shutdown-rules"
1  只取消 pump 而不取消 Run 会让 Runtime 任务存活到进程退出，
    表现为不稳定的 teardown 行为，必须避免
2  Run 的收敛等待必须有界（例如 cancel_settle_timeout）
3  shutdown 不得阻塞超过该有界时间
```

---

## 2. Message、Session、Run 与恢复模型

### 2.1 Canonical Message

roboagent canonical transcript 是运行时唯一对话真相。

包含：

```text id="ux86ay"
UserMessage
AssistantMessage
ToolResultMessage
```

以及：

```text id="k57vdg"
TextContent
JsonContent
ImageContent
AudioContent
FileContent
ArtifactReferenceContent
```

Product Snapshot 可以转换字段格式，但必须信息无损。

### 2.2 Stable message_id

当前 roboagent 全面缺少：

```text id="1s4oeq"
message_id
```

这是 Phase 0 最重要的 Runtime 修改。

所有 canonical message：

```text id="fxvdvx"
UserMessage
AssistantMessage
ToolResultMessage
```

增加：

```text id="4uxubm"
message_id
```

要求：

- immutable；
- persistence 后保持；
- reload 后不改变；
- rodesk 不生成 canonical ID；
- roserver 不维护额外 canonical ID 映射；
- ToolResultMessage 同样拥有独立 message_id。

### 2.3 Assistant message_id 生命周期

Assistant identity 必须在 streaming 开始之前创建。

定义：

```text id="w55v0h"
model invocation starts
        ↓
allocate assistant message_id
        ↓
model.started(message_id)
        ↓
model.delta(message_id)
        ↓
model.tool_call_*(message_id)
        ↓
construct AssistantMessage
        ↓
commit same message_id
```

因此：

```text id="c11bch"
identity lifetime
    starts before streaming

durability lifetime
    starts after transcript commit
```

如果未提交就失败：

```text id="1ka8m9"
message_id
```

只存在于 RunProjection。

这是合法状态。

### 2.4 Runtime Event 的 message identity

至少以下 Runtime Event 必须增加 `message_id`：

```text id="98itjh"
model.started
model.delta
model.completed

model.tool_call_started
model.tool_call_arguments_delta
model.tool_call_completed
```

Tool 事件当前已经拥有：

```text id="oipjzu"
tool_call_id
tool_name
```

为了建立稳定 message → tool 关系，Tool lifecycle event 也应携带：

```text id="ju9dwe"
message_id
```

包括：

```text id="ks4q6q"
tool.started
tool.completed
tool.failed
tool.cancelled
```

最终关系：

```text id="s84gpj"
AssistantMessage.message_id
    └── ToolCall.id
            └── ToolResultMessage.tool_call_id
```

### 2.5 Nested child model event lineage

roboagent 已存在完善的：

```text id="0zwm10"
ExecutionLineage
```

但 child run 复用 parent emitter，因此 child `model.*` event 必须显式绑定当前
child 的 ExecutionLineage，不能回退到默认 root lineage。

**验证结果（已完成）**：

```text id="pm-2-5-verified"
测试    roboagent/tests/runtime/test_nested_event_lineage.py
结论    修复前 child model.* 全部为 agent_depth = 0（root lineage）→ 失败
修复    AgentLoop 为每个 turn 取 run_context.execution.lineage，
        并在 model.started / delta / tool_call_* / completed / cancelled / failed
        上显式传递 lineage
现状    通过：child model.* 携带 agent_depth = 1 与 agent_tool_name
```

不变量：

```text id="pm-2-5-invariants"
1  child 的 model.* 事件 lineage.agent_depth >= 1
2  child 的 model.* 事件 lineage.agent_tool_name 指向触发的 Agent-as-Tool
3  root 的 model.* 事件 lineage.agent_depth == 0
4  lineage 不写入业务 payload
```

这属于 Runtime 正确性，不允许由 roserver 猜测。

### 2.6 User optimistic message

rodesk 可以立即展示用户发送的内容，但不能生成 canonical `message_id`。

使用：

```text id="gfcd17"
client_message_id
```

请求：

```json id="gcyjyo"
{
  "input": {
    "client_message_id": "client_123",
    "content": [
      {
        "type": "text",
        "text": "Hello"
      }
    ]
  }
}
```

Run 创建返回：

```json id="wtzw8r"
{
  "root_run_id": "run_123",
  "client_message_id": "client_123",
  "user_message_id": "msg_123"
}
```

rodesk：

```text id="zuv5md"
temporary UI row
 client_message_id
      ↓
bind
      ↓
canonical message_id
```

`Idempotency-Key` 与 `client_message_id` 不同：

```text id="hkc9kv"
client_message_id
    UI correlation

Idempotency-Key
    write deduplication
```

### 2.7 Persistence Schema Migration

新增 `message_id` 需要 roboagent persistence：

```text id="569z1q"
SCHEMA_VERSION 1 → 2
```

当前版本不兼容时直接失败，因此 Phase 0 还需要新增最小 migration framework。

要求：

```text id="l5tdvk"
schema v1
   ↓
migration
   ↓
generate stable message IDs
   ↓
write schema v2
```

旧 Session 只生成一次 ID，之后持久化。

迁移必须：

```text id="b14f99"
deterministic in one migration execution
atomic at snapshot level
never regenerate IDs after successful migration
```

不要求建立复杂通用 migration framework，V1 只需支持：

```text id="l6oy40"
v1 → v2
```

并为未来版本留下显式 dispatch。

### 2.8 Product Snapshot 转换规则

Product Snapshot 不直接 expose roboagent persistence codec。

统一冻结 Product Schema 转换。

#### Timestamp

Runtime：

```text id="ll24ar"
float epoch seconds
```

Product API：

```text id="m8vqd2"
RFC 3339 / ISO 8601 UTC
```

例如：

```text id="auxaol"
2026-10-03T10:00:12.123Z
```

#### ToolCall

Runtime：

```text id="ikmovw"
ToolCall.id
```

Product：

```text id="7i1cm1"
tool_call_id
```

#### ToolResult status

Runtime：

```text id="5hkt0v"
success
error
```

Product Snapshot 建议直接保持：

```text id="x7y9yh"
success
error
```

不要另外映射成：

```text id="ze0shk"
succeeded
failed
```

避免同形近义错误。

#### Content

Product Content Schema 使用稳定的 discriminated union：

```text id="5n4x0i"
type = text
type = json
type = image
type = audio
type = file
type = artifact_reference
```

它不直接复用 persistence codec 内部 message type 字段。

各分支字段：

```text id="pm-content-fields"
{ "type": "text",  "text": string }

{ "type": "json",  "value": any }

{ "type": "image", "artifact_id": "sha256:...",   required
                   "media_type": string,          optional
                   "detail": string | null }      optional

{ "type": "audio", "artifact_id": "sha256:...",   required
                   "media_type": string,          optional
                   "transcript": string | null }  optional

{ "type": "file",  "artifact_id": "sha256:...",   required
                   "media_type": string,          optional
                   "filename": string | null }    optional

{ "type": "artifact_reference",
                   "artifact_id": "sha256:...",   required
                   "media_type": string | null,
                   "size": integer,
                   "digest": "sha256:...",
                   "preview": string | null }
```

可选性规则：

```text id="pm-content-optionality"
1  入站（AgentInput）只需 artifact_id；
    media_type / detail / transcript / filename 均可省略。
2  出站（SessionSnapshot 投影）由服务端依据 artifact 元数据补齐
    media_type，并按 media_type 前缀选择 image / audio / file 分支。
3  roboagent 的 ArtifactReferenceContent 不携带文件名，
    因此 file 分支投影出的 filename 为 null；
    需要原始文件名时读取 GET /api/v1/artifacts/{artifact_id}.filename。
```

规则：

```text id="pm-content-rules"
1  Phase 1 只允许 type = text
2  image / audio / file 一律通过 artifact_id 引用，禁止内联 base64
3  Phase 4 才启用 image / audio / file / artifact_reference
4  未启用分支出现 → 422 unsupported_content_type（见 §3.4）
5  artifact_id / digest 必须是 "sha256:" + 64 位小写十六进制
```

非文本分支的 artifact_id 由 roserver ArtifactService 映射为 roboagent 的
`workspace://` 引用，见 §5.2。

### 2.9 SessionSnapshot

Product Snapshot：

```text id="oa36nd"
Product Session metadata
+
committed canonical transcript projection
+
active root run summary
```

例如：

```json id="wukg5v"
{
  "session_id": "sess_1",
  "title": "Robot assistant",
  "status": "running",
  "active_root_run_id": "run_1",
  "created_at": "...",
  "updated_at": "...",
  "active_run": {
    "run_id": "run_1",
    "status": "running",
    "started_at": "..."
  },
  "messages": []
}
```

SessionSnapshot 字段：

```text id="pm-snapshot-fields"
session_id          string, required
title               string, required
status              "idle" | "running", required（见 §1.5）
active_root_run_id  string, nullable
created_at          RFC 3339, required
updated_at          RFC 3339, required
active_run          ActiveRunSummary, nullable
messages            ProductMessage[], required
```

`active_run`：

```text id="pm-active-run"
run_id      string, required
status      Product RunStatus, required（见 §2.13）
started_at  RFC 3339, nullable
```

`status` 只有两个取值；`interrupted` / `failed` 等属于 Run 状态，不是 Session 状态。

#### Product Message Schema

`messages[]` 的元素是 Product Message，字段固定为：

```text id="pm-msg-fields"
message_id      string, required, canonical，来自 roboagent
role            "user" | "assistant" | "tool", required
timestamp       RFC 3339 UTC, required
content         ContentBlock[], required，可为空数组
tool_calls      ToolCall[], 仅 assistant，缺省为空
tool_call_id    string, 仅 tool
tool_name       string, 仅 tool
status          "success" | "error", 仅 tool
error           null 或 ErrorDetail，仅 tool
```

约束：

```text id="pm-msg-rules"
role = "assistant" 时允许 tool_calls，不允许 tool_call_id / status / error
role = "tool" 时必须具有 tool_call_id / tool_name / status
                status = "success" 时 error 必须为 null
                status = "error"   时 error 必须非 null
role = "user" 时不允许 tool_calls
```

ToolCall：

```text id="pm-msg-toolcall"
tool_call_id    string, required
name            string, required
arguments       object, required，可为空对象
```

ErrorDetail：

```text id="pm-msg-errordetail"
code            string, required, Product 错误码（见 §3.12）
message         string, required，已脱敏
retryable       boolean, optional
```

完整示例：

```json id="pm-msg-example"
{
  "session_id": "sess_1",
  "title": "Robot assistant",
  "status": "running",
  "active_root_run_id": "run_1",
  "created_at": "2026-10-03T10:00:00.000Z",
  "updated_at": "2026-10-03T10:01:00.000Z",
  "active_run": {
    "run_id": "run_1",
    "status": "running",
    "started_at": "2026-10-03T10:00:10.000Z"
  },
  "messages": [
    {
      "message_id": "msg_1",
      "role": "user",
      "timestamp": "2026-10-03T10:00:11.000Z",
      "content": [
        {
          "type": "text",
          "text": "检查机器人状态"
        }
      ]
    },
    {
      "message_id": "msg_2",
      "role": "assistant",
      "timestamp": "2026-10-03T10:00:12.000Z",
      "content": [],
      "tool_calls": [
        {
          "tool_call_id": "call_1",
          "name": "get_robot_state",
          "arguments": {}
        }
      ]
    },
    {
      "message_id": "msg_3",
      "role": "tool",
      "timestamp": "2026-10-03T10:00:13.000Z",
      "tool_call_id": "call_1",
      "tool_name": "get_robot_state",
      "status": "success",
      "error": null,
      "content": [
        {
          "type": "json",
          "value": {}
        }
      ]
    }
  ]
}
```

Snapshot 不包含：

```text id="u7hfqg"
partial assistant text
open tool projection
live approval UI state
```

Snapshot 只包含：

```text id="pm-snap-committed"
committed canonical message
```

未提交的 assistant 内容只存在于 RunProjection，见 §2.10。

### 2.10 RunProjection

RunProjection 是 roserver 进程内实时状态。

正式 schema 至少包含：

```text id="4k0fix"
root_run_id
session_id
status
last_sequence

assistant_messages[]
tools[]
approvals[]
child_runs[]
```

Assistant projection：

```text id="ch8v6s"
message_id
source_run_id
blocks[]
state
```

state：

```text id="v347yw"
streaming
completed
aborted
```

Tool projection：

```text id="t9w9ii"
message_id
tool_call_id
tool_name
execution_status
effect_status
certainty
```

Tool projection 枚举：

```text id="pm-tool-enums"
execution_status
    pending          尚未开始执行
    running          执行中
    completed        执行成功结束
    failed           执行失败结束
    cancelled        执行被取消

effect_status        直接投影 roboagent ToolEffectStatus
    succeeded
    failed
    timed_out
    cancelled
    unknown

certainty            直接投影 roboagent EffectCertainty
    certain
    certain_no_effect
    unknown
```

约束：

```text id="pm-tool-rules"
execution_status 未到达 completed/failed/cancelled 前，effect_status 必须为 unknown
effect_status 只在收到 tool_batch.committed 之后才允许被写为终值
certainty 与 effect_status 必须来自同一次提交，不允许分两次更新
```

Approval projection：

```text id="62pm91"
approval_id
tool_call_id
root_run_id
status
expires_at
```

Approval projection status：

```text id="pm-approval-status"
pending
approved
denied
expired
cancelled
```

Child projection：

```text id="e50bmg"
run_id
parent_run_id
status
agent_tool_name
```

Child projection 字段语义：

```text id="pm-child-fields"
run_id            Product run_id，等于 Runtime execution_run_id
parent_run_id     直接父 run 的 run_id；顶层 child 的父为 root
status            Product RunStatus（见 §2.13）
agent_tool_name   触发该 child 的 Agent-as-Tool 名称
```

RunProjection 主要由以下 Runtime 信息产生：

```text id="63gvl9"
AgentEvent
ExecutionLineage
tool events
approval provider callbacks
run terminal result
tool_batch.committed
```

不需要让 roserver 直接访问 roboagent private ExecutionTree 内部结构。

### 2.11 Durable vs Live

严格区分：

```text id="x7ju3b"
SessionSnapshot
    durable

RunProjection
    ephemeral
```

正常 WebSocket 重连：

```text id="r2umrt"
Public Event replay
    ↓
RunProjection
```

Replay 不足：

```text id="0bt6nd"
RunProjection
```

Projection 不存在：

```text id="pzwdn5"
RunInfo + SessionSnapshot
```

Server restart：

```text id="stnm7v"
RunProjection lost
partial message lost
active run → interrupted
```

### 2.12 pending recovery

roboagent pending 输入会 durable persistence。

V1 规定：

> Server restart 导致 active root Run interrupted 时，旧执行上下文的 pending steer/follow_up 全部清空。

因此 roboagent 新增：

```text id="jifpse"
Session.clear_pending()
```

Startup reconciliation：

```text id="b66vvc"
active run → interrupted

SessionMetadata.active_root_run_id → NULL

ApprovalRecord pending → cancelled

Runtime pending inputs → clear

RunProjection → discard
```

可以记录：

```text id="3kv3m7"
discarded_pending_count
```

用于前端提示。

### 2.13 Public RunStatus

```text id="ffglnk"
created
running
completed
failed
cancelled
interrupted
```

映射：

| Runtime / 系统状态 | Product |
|---|---|
| accepted, runtime 尚未真正启动 | created |
| Runtime active | running |
| Runtime COMPLETED | completed |
| Runtime FAILED | failed |
| Runtime CANCELLED | cancelled |
| server process loss / restart | interrupted |

---

## 3. Agent Product Protocol

Product API 固定：

```text id="weorx8"
/api/v1
```

与 Runtime package version / persistence schema 独立。

### 3.1 Session API

```text id="c3m6bs"
POST   /api/v1/sessions
GET    /api/v1/sessions
GET    /api/v1/sessions/{session_id}
PATCH  /api/v1/sessions/{session_id}
DELETE /api/v1/sessions/{session_id}
```

List：

```text id="l43z80"
limit
cursor
order=updated_desc
```

列表响应对象：

```json id="pm-session-page"
{
  "items": [],
  "next_cursor": null
}
```

```text id="pm-session-page-fields"
items         SessionSnapshot[], required
next_cursor   string, nullable，为 null 表示没有下一页
```

V1 不支持：

```text id="jtwzp4"
search
tag
folder
advanced filters
```

### 3.2 Session Saga

创建：

```text id="de4ugb"
1 generate session_id

2 ApplicationStore:
  insert provisioning

3 Agent.new_session(session_id=...)
  persist Runtime Session

4 mark ApplicationStore ready
```

Startup reconciliation：

```text id="bhl1pr"
provisioning + runtime exists
    → ready

provisioning + runtime missing
    → clean / reprovision according to policy
```

删除：

```text id="9a47sh"
ready
 ↓
deleting
 ↓
delete runtime snapshot
 ↓
delete dependent application records
 ↓
delete session row
```

active Run 时：

```text id="tyewz9"
409 session_busy
```

不自动取消再删除。

### 3.3 Runtime Public API 增补范围

当前 roboagent 已有大部分 Application Host API。

Phase 0 不进行大规模 Public API 重构。

仅增加三个窄能力：

```text id="hr2zd5"
Session.clear_pending()

Session.delete()
    或等价 Session-level persistence deletion helper

Agent/Session open-by-id convenience API
```

open-by-id 本质上只是包装：

```text id="50abj9"
repository.load
Session.restore
```

不是新增新的 Runtime 模型。

共享 `session_id` 已经被现有 API 支持，无需修改。

### 3.4 Run API

```text id="txdods"
POST /api/v1/sessions/{session_id}/runs
GET  /api/v1/runs/{run_id}
GET  /api/v1/runs/{run_id}/projection

POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/steer
POST /api/v1/sessions/{session_id}/follow-ups
```

Phase 1 AgentInput：

```json id="dmdlu7"
{
  "input": {
    "client_message_id": "client_123",
    "content": [
      {
        "type": "text",
        "text": "Hello"
      }
    ]
  }
}
```

AgentInput 字段：

```text id="pm-agentinput-fields"
client_message_id   string, optional, 由 rodesk 生成，仅用于 UI correlation
                               缺省时 startRun 响应中 user_message_id 仍返回，
                               而 client_message_id 为 null
content             ContentBlock[], required, 非空
```

content 类型支持矩阵：

```text id="pm-content-support"
Phase 1     text
Phase 4     text / json / image / audio / file / artifact_reference
```

#### 内容协商与请求校验

请求体必须是 JSON，并且必须按以下顺序校验：

```text id="pm-content-negotiation"
1  HTTP Content-Type 不是 application/json
       → 415 unsupported_media_type

2  body 不是合法 JSON，或缺少 input.content
       → 400 invalid_input

3  content 中出现当前 Phase 尚未支持的 type
       → 422 unsupported_content_type

4  content 为空数组
       → 422 unsupported_content_type

5  content 超过大小上限
       → 413 payload_too_large
```

说明：

- `415` 表达“传输格式不被接受”（HTTP 层）；
- `422` 表达“传输格式合法但语义不被当前版本支持”（应用层）；
- 两者语义不同，不允许合并。

限制：

```text id="pm-request-limits"
max_agent_input_bytes       单次 AgentInput 最大字节数
max_content_blocks          单个 AgentInput 最大 block 数
max_artifact_bytes          单个 artifact 最大字节数
```

超过限制返回 `413 payload_too_large`，不返回 `invalid_input`。

#### 响应

创建 Run：

```json id="pm-startrun-response"
{
  "run_id": "run_1",
  "root_run_id": "run_1",
  "session_id": "sess_1",
  "status": "created",
  "client_message_id": "client_123",
  "user_message_id": "msg_1",
  "created_at": "2026-10-03T10:00:00.000Z"
}
```

- `run_id` 与 `root_run_id` 在顶层 Run 上相同；
- `user_message_id` 是 roboagent 分配的 canonical ID；
- 未提供 `client_message_id` 时该字段为 `null`。

steer / follow_up 响应：

```json id="pm-input-response"
{
  "input_id": "input_1",
  "session_id": "sess_1",
  "run_id": "run_1",
  "kind": "steer",
  "sequence": 3,
  "accepted_at": "2026-10-03T10:00:05.000Z"
}
```

follow_up 在无 active Run 时可以立即进入下一次执行；仍必须返回 `input_id` 与 `sequence` 以便幂等与 UI 关联。

cancel 响应返回 RunInfo（见 §3.15）。

### 3.5 Idempotency

#### 适用范围

以下写操作必须支持幂等：

```text id="975lb6"
create run
steer
follow_up
approval resolve
```

以下操作天然幂等，不要求 `Idempotency-Key`：

```text id="pm-idem-natural"
PATCH session title
DELETE session（最终保持 deleted，见 §3.2）
```

`Idempotency-Key` 是 HTTP 头：

```text id="81ijvt"
Idempotency-Key: <client-generated opaque string>
```

#### IdempotencyRecord

ApplicationStore 必须持久化：

```text id="pm-idem-record"
owner_id            string, required
operation           string, required, 例如 create_run / steer / follow_up / approval_resolve
scope_id            string, required, session_id 或 approval_id（无 scope 操作为 ""）
idempotency_key     string, required
request_digest      string, required, canonical request body 的 sha256
state               "in_progress" | "completed"
response_status     integer, nullable
response_body       object, nullable, 已存储的可重放响应
result_reference    string, nullable, 指向 RunRecord / ApprovalRecord 等
created_at          timestamp, required
expires_at          timestamp, required
```

唯一约束：

```text id="pm-idem-unique"
UNIQUE(owner_id, operation, scope_id, idempotency_key)
```

`request_digest` 的计算规则：

```text id="pm-idem-digest"
1  取请求 body
2  按 canonical JSON 规范化（键排序、无多余空白、UTF-8）
3  sha256 十六进制
```

不纳入 digest：

```text id="pm-idem-digest-exclude"
HTTP headers（含 Idempotency-Key 本身）
query string 中的分页/排序参数
```

#### 行为

```text id="pm-idem-behavior"
key 不存在
    → 记录 in_progress，执行操作，写 completed + response_body
    → 返回真实结果

key 存在，state = completed，request_digest 相同
    → 不重复执行，直接返回存储的 response_status + response_body
    → 必须带响应头 Idempotency-Replayed: true

key 存在，state = completed，request_digest 不同
    → 409 idempotency_conflict

key 存在，state = in_progress
    → 409 idempotency_in_progress
```

要求：

- 重放必须返回**与首次相同**的 status code 与 body；
- 不允许把首次失败结果重放成成功，也不允许相反；
- 未提供 `Idempotency-Key` 时按非幂等处理，正常执行；服务端不得静默生成 key。

#### 保留期

```text id="pm-idem-ttl"
expires_at = created_at + idempotency_retention
```

`idempotency_retention` 为配置项，默认不小于客户端最大重试窗口。过期记录可以被清理；清理后相同 key 视为新请求。

#### approval resolve 的幂等

approval resolve 除遵循上述规则外，还必须满足状态语义：

```text id="pm-idem-approval"
approve → approve
    返回已存在的决议结果，不重复决议

deny → deny
    返回已存在的决议结果

approve → deny（或反向）
    409 approval_already_resolved

记录已因超时终结
    410 approval_expired

记录已因 run cancel / session delete / server restart 终结
    409 approval_already_resolved
```

### 3.6 Pending input limits

配置：

```text id="zy04hn"
max_pending_inputs
max_pending_input_bytes
```

例如初始：

```text id="7lm8tp"
max_pending_inputs = 32
```

超过：

```text id="n0d6cp"
429 pending_queue_full
```

### 3.7 Root / Child Run

统一 Run ID namespace：

```text id="e6jpxd"
root_run_id
source_run_id
parent_run_id
```

root：

```text id="4x2nic"
root_run_id == source_run_id
parent_run_id == null
```

child：

```text id="3qgzbx"
root_run_id = root
source_run_id = child
parent_run_id = direct parent
```

GET `/runs/{id}` 可以查询 root 或 child。

#### Product ↔ Runtime Identity 映射

roboagent 使用 `ExecutionLineage`，字段与 Product 命名不同，必须显式映射，禁止同名假设。

Runtime 字段（`roboagent/runtime/execution.py`）：

```text id="pm-lin-runtime"
root_run_id
execution_run_id
scope_id
parent_scope_id
scope_depth
agent_depth
tool_call_id
agent_tool_name
```

映射表：

| Product | Runtime 来源 | 说明 |
|---|---|---|
| `root_run_id` | `lineage.root_run_id` | 整棵执行树的根 |
| `run_id`（Product 主键） | `lineage.execution_run_id` | 每个 RunRecord 的主键 |
| `source_run_id` | `lineage.execution_run_id` | 事件产生者，等于该事件的 `run_id` |
| `parent_run_id` | 由 `parent_scope_id` 推导 | Runtime 不直接提供，需 roserver 维护 `scope_id → run_id` 映射 |
| `agent_tool_name` | `lineage.agent_tool_name` | Agent-as-Tool 名称 |
| `tool_call_id` | `lineage.tool_call_id` | composite tool 场景 |

不变量：

```text id="pm-lin-invariants"
Runtime root 的 execution_run_id == root_run_id
AgentEvent.run_id == lineage.root_run_id（Runtime 强制）
Product parent_run_id != null  ⇔  agent_depth > 0
```

roserver 必须维护：

```text id="pm-lin-state"
scope_id → run_id 映射
```

因为 Runtime 只暴露 `parent_scope_id`，不暴露 `parent_run_id`。

另外：

- Product `run_id` 直接复用 Runtime `execution_run_id`，不重新分配，避免双 ID 体系；
- 若未来 Runtime 更换 ID 生成策略，Product ID 仍保持不透明字符串，不做格式假设。

### 3.8 Product Event Envelope

```text id="r4esfg"
WS /api/v1/runs/{root_run_id}/events
```

可带 replay 游标（见 §3.13）：

```text id="pm-3-8-cursor"
WS /api/v1/runs/{root_run_id}/events?after_sequence=N
```

Envelope：

```json id="r6tfsi"
{
  "session_id": "sess_1",
  "root_run_id": "run_root",
  "source_run_id": "run_child",
  "parent_run_id": "run_parent",
  "sequence": 12,
  "type": "assistant.delta",
  "timestamp": "...",
  "data": {}
}
```

字段：

```text id="pm-envelope-fields"
session_id      string, required
root_run_id     string, required
source_run_id   string, required, 产生该事件的实际 run
parent_run_id   string, nullable, source_run 的直接父 run
sequence        integer, required, 从 1 开始，对 root 严格递增
type            string, required, dot.case
timestamp       RFC 3339, required
data            object, required, 可为空对象
```

Public sequence：

- roserver 自己生成；
- 对 root stream 严格递增；
- 不复用 Runtime sequence；
- 不要求和 Runtime event 数量一一对应。

#### 命名空间

Event type 与 error code 属于**不同 namespace**，不得混用：

```text id="pm-namespace"
Event type      dot.case
                例如 stream.resync_required

Error code      snake_case
                例如 stream_resync_required
```

因此：

- `assistant.delta`、`tool.effect_committed`、`stream.resync_required` 是事件；
- `run_timeout`、`approval_already_resolved`、`stream_resync_required` 是错误码。

#### 事件流约束

```text id="pm-stream-invariants"
1  每个 root run 恰好一个 terminal 事件
       run.completed / run.failed / run.cancelled / run.interrupted
2  terminal 之后不允许再发送普通事件
3  child run 的 terminal 事件不关闭 root 事件流
4  任何缺号都必须显式以 stream.resync_required 表达，不允许静默跳过
```

#### Run / Child Run 事件

终态事件的来源：runtime 的 `run.completed` payload 只有 `status` / `error_code`，
**不含 usage**。因此 Product 终态事件由 roserver 在 `await run.result()` 之后依据
`RunResult` 合成，而不是直接转发 runtime 事件。

```text id="pm-run-events"
run.started
run.completed
run.failed
run.cancelled
run.interrupted

child_run.started
child_run.completed
child_run.failed
child_run.cancelled
```

Payload：

```json id="pm-run-started"
{ "run_id": "run_1" }
```

```json id="pm-run-completed"
{
  "run_id": "run_1",
  "usage": {
    "input_tokens": 123,
    "output_tokens": 45,
    "total_tokens": 168,
    "usage_known": true
  }
}
```

```json id="pm-run-failed"
{
  "run_id": "run_1",
  "error": {
    "code": "run_timeout",
    "message": "Run exceeded its timeout.",
    "request_id": "req_1",
    "details": {}
  }
}
```

```json id="pm-run-cancelled"
{
  "run_id": "run_1",
  "reason": "user_cancelled"
}
```

```json id="pm-run-interrupted"
{
  "run_id": "run_1",
  "reason": "server_restarted",
  "discarded_pending_count": 2
}
```

child 额外字段：

```json id="pm-child-run-started"
{
  "run_id": "run_child",
  "parent_run_id": "run_1",
  "agent_tool_name": "researcher"
}
```

字段：

```text id="pm-run-event-fields"
run_id                  string, required, 所有 run/child_run 事件
usage                   Usage, required，仅 completed
error                   ErrorDetail, required，仅 failed
reason                  string, required，仅 cancelled / interrupted
parent_run_id           string, required，仅 child_run.started
agent_tool_name         string, required，仅 child_run.started
discarded_pending_count integer, optional，仅 interrupted
```

`run.interrupted` 只由启动对账产生（§2.12），不会在进程存活期间实时发送。

不变量：

```text id="pm-run-event-invariants"
1  run.started 先于该 run 的任何其它事件
2  assistant.completed 必须先于 run.completed
   若存在未提交的 open assistant message，
   则 run.failed / run.cancelled 之前必须先发 assistant.aborted
3  run.interrupted 不会与 run.failed / run.cancelled 同时出现
4  child_run.* 的 run_id 是该 child 自己的 run_id，
   parent_run_id 指向直接父 run
5  child terminal 不结束 root 事件流
```

### 3.9 Assistant Product Events

```text id="gs2blf"
assistant.started
assistant.delta
assistant.completed
assistant.aborted
```

#### Payload

`data` 字段（外层 envelope 已提供 session/run/sequence/timestamp）：

```json id="pm-assistant-started"
{
  "message_id": "msg_2"
}
```

```json id="pm-assistant-delta"
{
  "message_id": "msg_2",
  "block_index": 0,
  "block_type": "text",
  "delta": "Hello"
}
```

```json id="pm-assistant-completed"
{
  "message_id": "msg_2"
}
```

```json id="pm-assistant-aborted"
{
  "message_id": "msg_2",
  "reason": "run_cancelled"
}
```

字段：

```text id="pm-assistant-fields"
message_id    string, required, 所有四个事件
block_index   integer, required, 仅 delta，从 0 开始
block_type    "text", required, 仅 delta（Phase 1–4 delta 只用于 text）
delta         string, required, 仅 delta，增量文本
reason        string, required, 仅 aborted
```

不变量：

```text id="pm-assistant-invariants"
1  assistant.started 必须早于同一 message_id 的任何 delta
2  同一 message_id 不允许 started 两次
3  每个 started 在进程存活期间必须恰好以 completed 或 aborted 之一结束
4  completed 之后不允许再发送该 message_id 的 delta
5  aborted 表示该消息未进入 canonical transcript
```

`assistant.completed` 的判定（roboagent 没有 canonical commit 事件）：

```text id="pm-assistant-complete-detection"
1  该 assistant 带 tool_calls
       → 以 tool_batch.committed 判定其已提交
2  该 assistant 是最终文本输出
       → 以 RunResult.output.message_id 判定其已提交
3  以上都不成立，且 source run 已 failed / cancelled
       → 合成 assistant.aborted
```

`assistant.aborted` **不是 Runtime Event**。

它由 roserver RunProjection 合成。

合成条件：

```text id="u0jp4z"
open assistant projection exists
+
corresponding source run enters failed/cancelled
+
canonical assistant commit not observed
```

则：

```text id="vq4qhn"
emit assistant.aborted
```

reason：

```text id="0c3bfg"
run_failed
run_cancelled
runtime_error
```

`reason` 是**投影原因枚举**，与 §3.12 的 error code 不是同一 namespace，不得混用。

映射关系：

```text id="pm-aborted-reason"
Run 以 run.failed 终止        → reason = run_failed
Run 以 run.cancelled 终止     → reason = run_cancelled
Run 以 run.interrupted 终止   → 不发送 aborted，改由 interrupted 表达
其它内部错误导致未提交结束     → reason = runtime_error
```

Server crash 时无法发送 aborted，reload 后通过：

```text id="cqo8l3"
interrupted Run
```

表达。

### 3.10 Tool Runtime 与 Product 投影

roboagent 当前已经有：

```text id="n78752"
tool.started
tool.completed
tool.failed
tool.cancelled

tool_batch.committed
```

所以不新增一个虚构的：

```text id="mrfk9n"
tool.finished
```

Product 层定义：

```text id="uzx8hn"
tool.started
tool.execution_finished
tool.effect_committed
```

其中：

#### `tool.started`

来自：

```text id="zprlb2"
tool.started
```

#### `tool.execution_finished`

由：

```text id="emoxee"
tool.completed
tool.failed
tool.cancelled
```

投影。

#### `tool.effect_committed`

来自增强后的：

```text id="0xlyv1"
tool_batch.committed
```

Runtime Phase 0/2 需要把现有 batch event 从：

```text id="qtzrzo"
tool_call_ids[]
```

增强为：

```text id="d8q3py"
effects[]:
  tool_call_id
  tool_name
  effect_status
  certainty
```

Product 不重新发明 Effect 枚举。

直接投影：

```text id="mvo4q6"
effect_status:
    succeeded
    failed
    timed_out
    cancelled
    unknown

certainty:
    certain
    certain_no_effect
    unknown
```

这样才能保留 roboagent 真实语义。

#### Payload

```json id="pm-tool-started"
{
  "message_id": "msg_2",
  "tool_call_id": "call_1",
  "tool_name": "navigate"
}
```

```json id="pm-tool-execution-finished"
{
  "message_id": "msg_2",
  "tool_call_id": "call_1",
  "tool_name": "navigate",
  "execution_status": "completed"
}
```

```json id="pm-tool-effect-committed"
{
  "message_id": "msg_2",
  "tool_call_id": "call_1",
  "tool_name": "navigate",
  "effect_status": "unknown",
  "certainty": "unknown"
}
```

字段：

```text id="pm-tool-fields"
message_id         string, required, 三个事件都携带
tool_call_id       string, required
tool_name          string, required
execution_status   completed | failed | cancelled，仅 execution_finished
effect_status      见上，仅 effect_committed
certainty          见上，仅 effect_committed
```

`execution_status` 投影规则：

```text id="pm-tool-exec-map"
Runtime tool.completed  → execution_status = completed
Runtime tool.failed     → execution_status = failed
Runtime tool.cancelled  → execution_status = cancelled
```

`tool.effect_committed` 的展开规则：

```text id="pm-tool-effect-expand"
Runtime 事件是 batch 级（tool_batch.committed，含 effects[]）
Product 事件是 per-tool
因此 roserver 必须把一次 batch 提交展开为 N 个 tool.effect_committed
N = 该 batch 中的 tool 数
```

不变量：

```text id="pm-tool-invariants"
1  tool.started 必须早于同一 tool_call_id 的 execution_finished
2  effect_committed 必须晚于同一 tool_call_id 的 execution_finished
3  同一 tool_call_id 的 execution_finished 恰好一次
4  同一 tool_call_id 的 effect_committed 恰好一次
5  effect_status 与 certainty 必须来自同一次 batch 提交，不允许分两次更新
6  未收到 effect_committed 之前，前端不得展示“结果已确认”
```

并发说明：

```text id="pm-tool-concurrency"
CONCURRENT tool batch 的 execution 完成顺序
    可以与 effect 提交顺序不同

Product 事件顺序以 Runtime 的 batch 提交为准
不允许 roserver 依据完成顺序自行推断 effect
```

### 3.11 Approval

roboagent 现有异步 ApprovalProvider 已满足集成需求。

不需要改 Runtime interface。

roserver 实现：

```text id="cvi8q0"
ProductApprovalProvider
```

Product `approval.requested` 由该 Provider 直接产生，而不是 Runtime event projection。

#### 数据来源

```text id="1owlxv"
approval_id
run_id
session_id
tool_call_id
tool_name
arguments
arguments_digest
reason
lineage
effect_capability
```

来自 Runtime `ApprovalRequest`。

额外字段：

```text id="ayk49i"
summary
```

由 roserver 根据：

```text id="p5od3e"
tool_name
reason
arguments
```

生成展示文本。

```text id="rv7y1n"
expires_at
```

由 roserver Approval Policy 计算。

Runtime 默认 timeout 为 None，不假设 Runtime 自带 expires_at。

#### ApprovalRecord

ApplicationStore 必须持久化：

```text id="pm-approval-record"
approval_id         string, required
owner_id            string, required
session_id          string, required
root_run_id         string, required
source_run_id       string, required
tool_call_id        string, required
tool_name           string, required
arguments           object, required
arguments_digest    string, required
reason              string, nullable
effect_capability   string, nullable
summary             string, required
status              pending | approved | denied | expired | cancelled
decision            approve | deny | null
decision_reason     string, nullable
terminal_reason     string, nullable；取值：
                      server_restarted    服务重启
                      approval_timeout   审批超时
                      session_deleted    会话删除
                      run_ended          Run 终结
created_at          timestamp, required
expires_at          timestamp, required
resolved_at         timestamp, nullable
```

唯一约束：

```text id="pm-approval-unique"
UNIQUE(owner_id, approval_id)
```

#### Approval 状态机

```text id="pm-approval-fsm"
pending
  ├── approve   → approved
  ├── deny      → denied
  ├── timeout   → expired
  ├── run cancel / session delete → cancelled
  └── server restart            → cancelled (reason = server_restarted)
```

终结状态不允许再改变。

安全语义：

```text id="pm-approval-fail-closed"
超时（expires_at 到期）
run cancel
session delete
server shutdown / restart

以上任一情况都必须按“拒绝”处理，使 Tool 不产生副作用。
不允许在未获得明确 approve 的情况下让副作用 Tool 继续执行。
```

#### Product Events

`approval.requested`：

```json id="pm-approval-requested"
{
  "approval_id": "approval_1",
  "session_id": "sess_1",
  "root_run_id": "run_1",
  "source_run_id": "run_1",
  "tool_call_id": "call_1",
  "tool_name": "navigate",
  "summary": "Navigate robot to meeting room",
  "arguments": {},
  "arguments_digest": "sha256:...",
  "effect_capability": "side_effecting",
  "expires_at": "2026-10-03T10:05:00.000Z"
}
```

`approval.resolved`：

```json id="pm-approval-resolved"
{
  "approval_id": "approval_1",
  "session_id": "sess_1",
  "root_run_id": "run_1",
  "tool_call_id": "call_1",
  "status": "approved",
  "decision": "approve",
  "terminal_reason": null,
  "resolved_at": "2026-10-03T10:00:30.000Z"
}
```

`status` 取值与 ApprovalRecord 的终结状态一致；`decision` 在 `expired`/`cancelled` 时为 `null`。

#### API

```text id="pm-approval-api"
POST /api/v1/approvals/{approval_id}/resolve
GET  /api/v1/approvals/{approval_id}
```

resolve 请求：

```json id="pm-approval-resolve-request"
{
  "decision": "approve",
  "arguments_digest": "sha256:...",
  "reason": "operator confirmed"
}
```

字段：

```text id="pm-approval-resolve-fields"
decision           "approve" | "deny", required
arguments_digest   string, required, 必须回显 approval.requested 中的值
reason             string, optional
```

resolve 响应（ApprovalInfo）：

```json id="pm-approval-resolve-response"
{
  "approval_id": "approval_1",
  "session_id": "sess_1",
  "root_run_id": "run_1",
  "source_run_id": "run_1",
  "tool_call_id": "call_1",
  "tool_name": "navigate",
  "summary": "Navigate robot to meeting room",
  "arguments": {},
  "arguments_digest": "sha256:...",
  "effect_capability": "side_effecting",
  "status": "approved",
  "decision": "approve",
  "expires_at": "2026-10-03T10:05:00.000Z",
  "resolved_at": "2026-10-03T10:00:30.000Z"
}
```

`GET /api/v1/approvals/{approval_id}` 返回同一 ApprovalInfo 对象。

这样 rodesk 在页面刷新或 WS 重连后，可以仅凭 `approval_id`（或从 RunProjection）恢复完整的审批 UI，不依赖 `approval.requested` 事件是否曾收到。

字段：

```text id="pm-approval-info-fields"
approval_id        string, required
session_id         string, required
root_run_id        string, required
source_run_id      string, required
tool_call_id       string, required
tool_name          string, required
summary            string, required
arguments          object, required
arguments_digest   string, required, 客户端 resolve 时必须回显
effect_capability  string, nullable
status             pending | approved | denied | expired | cancelled
decision           approve | deny | null
expires_at         RFC 3339, required
resolved_at        RFC 3339, nullable
```

resolve 校验顺序：

```text id="pm-approval-resolve-rules"
1  approval 不存在                       → 404 approval_not_found
2  arguments_digest 与记录不一致          → 409 approval_digest_mismatch
3  status 已是终结态                     → 409 approval_already_resolved
4  status = pending 且 now > expires_at
                                           → 置为 expired
                                           → emit approval.resolved(status=expired)
                                           → 410 approval_expired
5  通过                                  → 置终结状态并唤醒等待中的 Provider
```

说明：

- `approval_already_resolved` 用于“已经被人决议”；
- `approval_expired` 用于“因超时而作废”；
- 两者语义不同，不允许合并。

幂等要求见 §3.5。

#### Provider 与决议的衔接

roserver 内部流程固定为：

```text id="pm-approval-flow"
roboagent Executor
      ↓ ApprovalProvider.request(request, cancellation)
ProductApprovalProvider
      ├── persist ApprovalRecord(pending)
      ├── compute summary / expires_at
      ├── emit Product approval.requested
      └── await 决议或取消
              ↓
      resolve API 到达
              ↓
      ApprovalResponse(approval_id, arguments_digest, decision)
              ↓
      返回给 Runtime Executor
```

约束：

- `ApprovalResponse` 必须回显 Runtime 传入的同一个 `approval_id` 与 `arguments_digest`，否则 Runtime 会判定 `approval_mismatch`；
- decision 映射固定为 `approve → ApprovalDecision.APPROVE`、`deny → ApprovalDecision.REJECT`；
- 超时/取消/关停时必须以 `REJECT` 结束等待，或按 Runtime 的 cancellation 语义取消任务；
- Provider 必须运行在 roserver 的 owning event loop 上（见 §1.7）。

#### 启动对账

server restart 后遗留的 `pending` ApprovalRecord：

```text id="pm-approval-restart"
status          = cancelled
terminal_reason = server_restarted
decision        = null
```

并 emit `approval.resolved`，使 rodesk 清理残留的审批 UI。

### 3.12 Error Mapping

Runtime error 与 Product error 不直接透传。

正式定义映射层。

例如：

| Runtime | Product |
|---|---|
| `timeout` | `run_timeout` |
| `model_error` | `model_failed` |
| `session_persistence_error` | `persistence_conflict` |
| provider auth exception | `provider_auth_failed` |
| provider availability exception | `provider_unavailable` |

Product Error Envelope：

```json id="206vnp"
{
  "error": {
    "code": "model_failed",
    "message": "...",
    "request_id": "req_1",
    "details": {}
  }
}
```

字段：

```text id="pm-err-envelope"
code         string, required, 来自下面的注册表
message      string, required, 面向用户，已脱敏
request_id   string, required, roserver 生成，用于日志关联
details      object, required, 可为空对象；不允许放 traceback / secret / 路径
```

Product code 是稳定 API contract。

Runtime code 可以独立演进。

#### Product 错误码注册表

`scope` 表示该 code 出现的位置：

```text id="pm-err-scope"
HTTP      Error Envelope 的 error.code
RUN       RunInfo.error.code 与 run.failed 事件
STREAM    WebSocket 事件
APPROVAL  ApprovalRecord.terminal_reason
```

| code | HTTP | scope | 含义 |
|---|---|---|---|
| `invalid_input` | 400 | HTTP | 请求体不合法或缺少必填字段 |
| `control_authority_required` | 403 | HTTP | 未持有控制权 |
| `session_not_found` | 404 | HTTP | Session 不存在 |
| `run_not_found` | 404 | HTTP | Run 不存在 |
| `approval_not_found` | 404 | HTTP | Approval 不存在 |
| `artifact_not_found` | 404 | HTTP | Artifact 不存在 |
| `robot_not_found` | 404 | HTTP | Robot 不存在 |
| `media_session_not_found` | 404 | HTTP | Media session 不存在 |
| `projection_not_available` | 404 | HTTP | RunProjection 不存在或已丢弃 |
| `session_busy` | 409 | HTTP | Session 已有 active Run |
| `session_deleting` | 409 | HTTP | Session 处于 deleting |
| `idempotency_conflict` | 409 | HTTP | 同 key 不同 payload |
| `idempotency_in_progress` | 409 | HTTP | 同 key 请求仍在执行 |
| `approval_already_resolved` | 409 | HTTP | Approval 已终结或已超时 |
| `approval_digest_mismatch` | 409 | HTTP | arguments_digest 与记录不一致 |
| `control_authority_conflict` | 409 | HTTP | 控制权被其他持有者占用 |
| `robot_not_ready` | 409 | HTTP | Robot 未就绪 |
| `artifact_in_use` | 409 | HTTP | Artifact 仍被 canonical message 引用 |
| `media_session_closed` | 409 | HTTP | Media session 已终结 |
| `persistence_conflict` | 409 | HTTP / RUN | 乐观并发冲突 |
| `approval_expired` | 410 | HTTP | Approval 已过期 |
| `payload_too_large` | 413 | HTTP | 请求或 artifact 超过大小上限 |
| `unsupported_media_type` | 415 | HTTP | HTTP Content-Type 不是 application/json |
| `unsupported_content_type` | 422 | HTTP | JSON 合法但 content type 当前不支持 |
| `pending_queue_full` | 429 | HTTP | pending 输入队列已满 |
| `rate_limited` | 429 | HTTP | 触发限流 |
| `internal_error` | 500 | HTTP | 未预期内部错误 |
| `model_failed` | 502 | HTTP / RUN | 模型调用失败 |
| `media_negotiation_failed` | 502 | HTTP | SDP/ICE 协商失败 |
| `provider_auth_failed` | 503 | HTTP / RUN | Provider 认证失败 |
| `provider_unavailable` | 503 | HTTP / RUN | Provider 不可用 |
| `robot_unavailable` | 503 | HTTP | 机器人后端 不可达 |
| `robot_timeout` | 504 | HTTP | Robot 命令超过 deadline |
| `run_timeout` | — | RUN | Run 超过 timeout |
| `runtime_error` | — | RUN | Runtime 内部错误 |
| `run_cancelled` | — | RUN | Run 被取消 |
| `stream_resync_required` | — | STREAM | 事件流无法连续，需要重新同步 |
| `server_restarted` | — | APPROVAL | 服务重启导致审批作废 |

回退映射：

```text id="pm-error-fallback"
Runtime 的 tool / child run 错误码不在本表内
    → 统一映射为 runtime_error
```

要求：

- 注册表是**封闭集合**，新增 code 必须同时更新此表与三个仓库的实现；
- 生产环境不要返回未登记的 code；
- `5xx` 的 `message` 不得包含内部细节；
- 同一语义不允许同时使用两个 code。

#### 机器人后端 → Product 映射

见 §7.9。

### 3.13 Event Replay

roserver 自己维护：

```text id="fsbzcc"
PublicEventBuffer
```

不复用 roboagent：

```text id="nsn790"
EventStore
JsonlEventStore
MemoryEventStore
```

原因：

1. Runtime Event 和 Product Event 不同；
2. Product sequence 独立；
3. Runtime Event 会被过滤 / 聚合；
4. Payload 不同；
5. Product replay 生命周期与 HTTP API 绑定。

因此 V1 明确：

> roboagent EventStore 不参与 Product Event replay。

当前 `JsonlEventStore` 未接入 Runtime 主链路，且存在同步文件 I/O，应作为 roboagent 独立技术债，不纳入 roserver V1 设计。

#### 订阅与 Replay 协议

```text id="pm-replay-endpoint"
WS /api/v1/runs/{root_run_id}/events?after_sequence=N
```

`after_sequence` 语义：

```text id="pm-replay-after"
缺省
    只订阅 live 事件，不 replay

N = 0
    从 buffer 中最早保留的事件开始 replay

N > 0
    先 replay sequence > N 的事件，再切换到 live
```

要求：

- `sequence` 从 `1` 开始，对单个 root run 严格递增；
- 已交付的连续流中不得缺号（gap 必须显式以 `stream.resync_required` 表达）；
- replay 与 live 之间不得重复交付同一条事件；
- Product sequence 与 Runtime sequence 独立，不做数值对齐。

#### PublicEventBuffer

```text id="pm-replay-buffer"
作用域      单个 root run
保留策略    active run 全程保留
            terminal run 保留有界时间窗口
上限        max_replay_events
            max_replay_bytes
            replay_retention
```

至少保留：

```text id="pm-replay-min-retain"
run 的 terminal 事件
所有 tool.effect_committed
所有 approval.requested / approval.resolved
```

即：非 terminal 的 delta 事件允许被裁剪，但**状态转移事件不得裁剪**。

#### 无法 replay 时

以下任一情况：

```text id="pm-replay-gap"
after_sequence 早于 buffer 最早保留位置
buffer 已被清理
Product 队列曾丢弃事件
sequence 无法重建
```

必须发送：

```json id="pm-replay-resync"
{
  "type": "stream.resync_required",
  "data": {
    "requested_after_sequence": 12,
    "oldest_available_sequence": 40,
    "last_sequence": 57
  }
}
```

客户端处理见 §3.14。

### 3.14 Backpressure / resync

WebSocket 使用：

```text id="jk1h8n"
bounded send queue
heartbeat
slow consumer detection
```

无法恢复完整 sequence 时：

```text id="gyehfd"
stream.resync_required
```

客户端：

```text id="u9t0yv"
stop applying old deltas
   ↓
GET RunProjection
```

Projection 不存在：

```text id="2r97la"
GET RunInfo
+
GET SessionSnapshot
```

### 3.15 Product 对象 Schema

以下对象是 Product API 的稳定契约。

#### RunInfo

```json id="pm-runinfo"
{
  "run_id": "run_1",
  "session_id": "sess_1",
  "root_run_id": "run_1",
  "parent_run_id": null,
  "source_run_id": "run_1",
  "agent_tool_name": null,
  "status": "completed",
  "created_at": "2026-10-03T10:00:00.000Z",
  "started_at": "2026-10-03T10:00:00.100Z",
  "ended_at": "2026-10-03T10:00:12.000Z",
  "error": null,
  "usage": {
    "input_tokens": 123,
    "output_tokens": 45,
    "total_tokens": 168,
    "usage_known": true
  },
  "effects": [
    {
      "tool_call_id": "call_1",
      "tool_name": "navigate",
      "effect_status": "unknown",
      "certainty": "unknown",
      "summary": "Navigation command outcome could not be confirmed."
    }
  ],
  "retry_safe": false
}
```

字段：

```text id="pm-runinfo-fields"
run_id           string, required
session_id       string, required
root_run_id      string, required
parent_run_id    string, nullable
source_run_id    string, required
agent_tool_name  string, nullable, 仅 child run
status           Product RunStatus, required，见 §2.13
created_at       RFC 3339, required
started_at       RFC 3339, nullable
ended_at         RFC 3339, nullable
error            ErrorDetail, nullable，见 §2.9
usage            Usage, required
effects          EffectSummary[], required，可为空数组
retry_safe       boolean, required
```

#### Usage

Runtime `Usage` 的三个字段都可能为 `null`（模型未上报），因此 Product 必须保留三态：

```text id="pm-usage"
input_tokens    integer, nullable
output_tokens   integer, nullable
total_tokens    integer, nullable
usage_known     boolean, required
```

规则：

```text id="pm-usage-rules"
usage_known = true   → 三个 token 字段均非 null
usage_known = false  → 至少一个字段为 null
恰好为 0 的 token 数必须写成 0，不允许写成 null
child run 的 usage 当前不可得 → usage_known = false，三个字段为 null
```

禁止把未知 usage 写成 `0`。

#### EffectSummary

```text id="pm-effect-summary"
tool_call_id    string, required
tool_name       string, required
effect_status   succeeded | failed | timed_out | cancelled | unknown
certainty       certain | certain_no_effect | unknown
summary         string, required, 面向用户，已脱敏
```

来源：

```text id="pm-effect-source"
RunResult.effects（ToolEffectRecord[]）
```

约束：

- 直接投影 `ToolEffectStatus` 与 `EffectCertainty`，不重新定义枚举；
- 只允许使用 allow-list 投影，禁止直接把 `ToolEffectRecord` 序列化出去；
- 不得包含 raw evidence、provider payload、凭据类 arguments、文件系统路径、内部 traceback。

#### 白名单投影

roserver 不直接 serialize：

```text id="pm-allowlist-deny"
RunResult
ExecutionRecord
ToolEffectRecord
```

必须经过 Product Schema allow-list 投影；Runtime 数据先由 roboagent 脱敏，再进入 roserver 白名单。

### 3.16 rodesk Agent 接入契约

rodesk 侧必须实现与 §3.1–§3.15 对应的客户端接口。

`AgentService` 接口：

```ts id="pm-agentservice"
interface AgentService {
  listSessions(input?: ListSessionsInput): Promise<SessionPage>

  createSession(input?: CreateSessionInput): Promise<SessionSnapshot>

  getSession(sessionId: string): Promise<SessionSnapshot>

  updateSession(
    sessionId: string,
    input: UpdateSessionInput
  ): Promise<SessionSnapshot>

  deleteSession(sessionId: string): Promise<void>

  startRun(
    sessionId: string,
    input: AgentInput,
    options?: WriteOptions
  ): Promise<StartRunResult>

  getRun(runId: string): Promise<RunInfo>

  getRunProjection(runId: string): Promise<RunProjection>

  cancelRun(runId: string): Promise<RunInfo>

  steer(
    runId: string,
    input: AgentInput,
    options?: WriteOptions
  ): Promise<InputReceipt>

  followUp(
    sessionId: string,
    input: AgentInput,
    options?: WriteOptions
  ): Promise<InputReceipt>

  resolveApproval(
    approvalId: string,
    decision: ApprovalDecisionInput,
    options?: WriteOptions
  ): Promise<ApprovalInfo>

  getApproval(approvalId: string): Promise<ApprovalInfo>

  subscribeRun(
    rootRunId: string,
    listener: (event: AgentEvent) => void,
    options?: SubscribeRunOptions
  ): () => void
}
```

关键类型：

```ts id="pm-agentservice-types"
WriteOptions {
  idempotencyKey?: string
}

SubscribeRunOptions {
  afterSequence?: number
}

ApprovalDecisionInput {
  decision: 'approve' | 'deny'
  argumentsDigest: string
  reason?: string
}

InputReceipt {
  inputId: string
  sessionId: string
  runId: string
  kind: 'steer' | 'follow_up'
  sequence: number
  acceptedAt: string
}
```

命名规则（wire 与客户端分层）：

```text id="pm-naming-wire-client"
HTTP / WS wire JSON
    一律 snake_case（与 §2.8、§2.9、§3.15 一致）

TypeScript 客户端本地类型
    wire 映射对象沿用 wire 字段名；
    仅调用参数/返回封装（WriteOptions / SubscribeRunOptions /
    ApprovalDecisionInput / InputReceipt 等本地类型）使用 camelCase
```

即：`SessionSnapshot`、`RunInfo`、`ApprovalInfo`、`RunProjection`、Product Event `data`
都是 snake_case；`InputReceipt` 是客户端本地类型，可以 camelCase，但实现必须保证
`acceptedAt` 与 wire 的 `accepted_at` 一一对应。

实现要求：

```text id="pm-agentservice-impl"
MockAgentService
    用于 UI 开发 / 单测 / 视觉测试 / 离线演示
    必须实现同一 Product Contract

ProductAgentService
    真实 HTTP + WebSocket 实现
```

切换点：

```text id="pm-agentservice-switch"
web/src/modules/agent/services/index.ts
```

禁止在组件内直接 import 具体实现。

约束：

- `subscribeRun` 收到 `stream.resync_required` 后必须停止应用旧 delta，重新拉取投影；
- `resolveApproval` 必须回显 `argumentsDigest`；
- 传 `WriteOptions.idempotencyKey` 时，重试不得产生副作用；
- 前端不解析 roboagent Runtime Event，只消费 Product Event（见 §3.9、§3.10）。

---

## 4. RobotService、机器人后端 与 Teleoperation

这一部分不再作为远期一句话目标，而是正式定义系统边界。

### 4.1 机器人后端边界

**不新增机器人侧进程，不引入 gRPC。**

项目当前未进入机器人集成阶段，因此 roserver 只冻结一个内部抽象边界，并只提供
模拟实现：

```text id="6ptl3a"
RobotBackend
    ├── SimulatedRobotBackend   今天只有这个
    └── （将来）ROS2 实现
```

职责（今日由模拟器承担，将来由 ROS2 实现承担）：

```text id="ahbua3"
robot discovery
robot state aggregation
control authority enforcement
teleoperation command watchdog
robot-side safety
```

**不做的事**：

```text id="pm-no-gateway"
不新增 mfr3duo_gateway
不定义 proto
不引入 gRPC
不新增 C++ 进程
```

将来接入真实机器人时，使用 **dcl**（DDS Client Library）进入 ROS2 生态：

```text id="pm-future-ros2"
仓库      https://github.com/SiYueY/dcl
模块      dclpy  —— DDS Client Library for Python
定位      _dclpy 直接绑定 dmw（DMW 是唯一 language-neutral runtime authority）
          与 dclcpp 平级，不经过 dclcpp
```

接入方式：

```text id="pm-dcl-plan"
1  用 dclpy 实现 RobotBackend
       Topic / Service / Action / Graph 覆盖 state 与 teleoperation
2  复用 mfr3duo_ros2 既有包（mfr3duo_control / moveit / hardware）
3  mfr3duo_nav 完成 Gate 后才接入导航能力
4  Product API 与前端契约不发生任何变化
5  不引入 gRPC，不新增 C++ 网关进程
```

当前状态（据此保持模拟实现）：

```text id="pm-dcl-status"
dclpy 目前只有架构文档（dcl/dclpy/docs/dclpy.md，Architecture Frozen Candidate），
尚无实现代码。因此在 dclpy 可用之前，roserver 一律使用 SimulatedRobotBackend。
```

机器人后端失败到 Product 错误码的映射见 §7.9。

#### 与基础功能开发的隔离约束

机器人层当前是**模拟实现**，且不得影响 agent / session / run / artifact 等基础功能：

```text id="pm-robot-isolation"
1  核心模块（agent / artifact / store）不得 import robot
2  机器人后端启动失败不得阻塞应用启动：
    lifespan 捕获并记录，继续提供核心功能
3  机器人端点在后端不可用时返回已登记的 Product 错误（如 robot_unavailable），
    不得抛出未处理异常
4  RobotBackend 始终可注入，测试与开发不依赖任何真实机器人或网络
```

即：机器人相关开发是**旁路能力**，基础功能开发不需要等待机器人集成。

### 4.2 机器人后端不承担 Agent 逻辑

禁止：

```text id="s8hn4d"
机器人后端
    ├── LLM
    ├── Agent
    ├── Tool runtime
    └── Prompt
```

机器人后端只提供机器人能力。

### 4.3 RobotService

roserver 内定义：

```text id="zge7ah"
RobotService
```

职责：

```text id="y8qmjb"
robot discovery
robot state
control authority
navigation application API
manipulation application API
teleoperation
health
```

内部调用：

```text id="c3ii31"
RobotBackend
```

Agent Robot Tool 也依赖同一个：

```text id="cnzre5"
AgentFactory
   ↓
NavigateTool(robot_service)
```

Manual UI：

```text id="mqhktk"
rodesk
   ↓
Robot Product API
   ↓
RobotService
```

两条路径共用能力层。

### 4.4 Phase 3 前置约束

当前：

```text id="p6pniv"
mfr3duo_nav
mfr3duo_robot
```

尚无实际能力实现。

因此真实：

```text id="xz204q"
navigate
task execution
```

不得在 roserver 中伪造为“已支持”。

Phase 3A 只要求：

```text id="2wmiiw"
机器人后端
RobotService
health
state
dummy/simulation command
```

真实 Navigation Tool 前置条件：

```text id="5a4pfy"
mfr3duo_nav
```

完成自身 Gate 验收。

### 4.5 Robot Product API

基础：

```text id="i4a8nt"
GET /api/v1/robots
GET /api/v1/robots/{robot_id}
GET /api/v1/robots/{robot_id}/state

WS /api/v1/robots/{robot_id}/events
```

Robot event 可以包括：

```text id="dz6n88"
robot.state
robot.connection_changed
robot.mode_changed
robot.fault
control.authority_changed
```

#### Robot 对象

```json id="pm-robot"
{
  "robot_id": "robot_1",
  "name": "MFR3Duo",
  "model": "mfr3duo",
  "protocol_version": "1.0",
  "connection": "online",
  "mode": "idle",
  "capabilities": ["state", "teleoperation"],
  "cameras": [
    {
      "camera_id": "head",
      "name": "Head camera",
      "video_source": "robot_head",
      "available": true
    },
    {
      "camera_id": "wrist",
      "name": "Wrist camera",
      "video_source": "robot_wrist",
      "available": false
    }
  ],
  "last_seen_at": "2026-10-03T10:00:00.000Z"
}
```

字段：

```text id="pm-robot-fields"
robot_id          string, required
name              string, required
model             string, required
protocol_version  string, required, 后端协议版本
connection        online | offline | degraded
mode              idle | teleoperation | agent | maintenance | fault
capabilities      string[], required
cameras           CameraSource[], required，可为空数组
last_seen_at      RFC 3339, nullable
```

`cameras` 字段（frozen contract，docs §5.9 的 `cameraId ↔ video_source` 映射以此为准）：

```text id="pm-robot-camera-fields"
camera_id     string, required, 后端概念；rodesk 的 cameraId 1:1 映射
name          string, required, 人类可读标签
video_source  string, required, 作为 media session 的 video_source 原样发送
available     boolean, required, false = 存在但当前不可推流（UI 应置灰而非隐藏）
```

规则：

```text id="pm-robot-camera-rules"
cameras 始终存在，机器人无相机时为空数组 []，不允许省略该键
POST /api/v1/media/sessions { kind: "camera", video_source } 的取值必须来自该列表
unknown 值使用 null，不使用 0 或空字符串冒充未知
```

`capabilities` 用于显式声明当前真实可用的能力。在 `mfr3duo_nav` 完成 Gate 前：

```text id="pm-robot-capabilities-v1"
capabilities = ["state", "teleoperation"]
```

不允许把 `navigation` / `manipulation` 写入 capabilities。

`cameras` 是 rodesk `CameraSourceMenu` / `SourceCameraAdapter` 的唯一数据源，
rodesk 不再本地硬编码相机列表（映射细节见 §5.9）。

#### RobotState

```json id="pm-robot-state"
{
  "robot_id": "robot_1",
  "timestamp": "2026-10-03T10:00:00.000Z",
  "connection": "online",
  "mode": "teleoperation",
  "battery": {
    "level": 82,
    "charging": false
  },
  "pose": {
    "frame_id": "map",
    "x": 0.0,
    "y": 0.0,
    "z": 0.0
  },
  "velocity": {
    "linear_x": 0.0,
    "linear_y": 0.0,
    "angular_z": 0.0
  },
  "joints": [
    {
      "name": "left_arm_joint_1",
      "position": 0.0,
      "velocity": 0.0,
      "effort": null
    }
  ],
  "faults": []
}
```

字段：

```text id="pm-robot-state-fields"
robot_id     string, required
timestamp    RFC 3339, required, 采集时间
connection   online | offline | degraded
mode         idle | teleoperation | agent | maintenance | fault
battery      { level: 0-100 | null, charging: boolean }
pose         { frame_id, x, y, z } | null
velocity     { linear_x, linear_y, angular_z } | null
joints       JointState[], required，可为空数组
faults       Fault[], required，可为空数组
```

规则：

```text id="pm-robot-state-rules"
数据不可用时使用 null，不使用 0 或空值冒充有效数据
timestamp 由机器人后端采集，不是 roserver 接收时间
rodesk 必须展示 connection 与 timestamp，不能只显示乐观状态
```

`rodesk` 现有 `connecting | connected | failed` 状态机是**连接过程状态**，与 robot 的 `online | offline | degraded` 不是同一维度，两者都必须保留。

#### Event Payload

```text id="pm-robot-events"
robot.state
    { robot_id, state: RobotState }

robot.connection_changed
    { robot_id, previous, current }

robot.mode_changed
    { robot_id, previous, current }

robot.fault
    { robot_id, fault: { code, severity, message } }

control.authority_changed
    { robot_id, authority_id, mode, holder }
```

`severity`：

```text id="pm-robot-fault-severity"
info
warning
error
critical
```

`critical` fault 必须触发：

```text id="pm-robot-fault-critical"
teleoperation velocity → zero
authority 释放
rodesk 强制提示
```

### 4.6 Control Authority

人工和 Agent 都可能操作机器人，因此必须统一 control authority。

API：

```text id="d425po"
POST /api/v1/robots/{robot_id}/control/acquire
POST /api/v1/robots/{robot_id}/control/release
GET  /api/v1/robots/{robot_id}/control
```

Acquire 返回：

```json id="nzrpuz"
{
  "authority_id": "auth_1",
  "robot_id": "robot_1",
  "mode": "teleoperation",
  "expires_at": "..."
}
```

mode 可以包括：

```text id="j6b9z2"
teleoperation
agent
maintenance
```

初期只实现：

```text id="d4xzp9"
teleoperation
```

### 4.7 Teleoperation 通道

遥操作采用 WebSocket，不采用 HTTP 高频 POST。

```text id="ge6ha1"
WS /api/v1/robots/{robot_id}/teleoperation
```

Command：

```json id="a8yexk"
{
  "type": "velocity",
  "authority_id": "auth_1",
  "sequence": 123,
  "client_timestamp": "...",
  "linear_x": 0.2,
  "linear_y": 0.0,
  "angular_z": 0.1,
  "deadman": true
}
```

Server 必须执行：

```text id="4znmw3"
authority validation
sequence monotonicity
stale timestamp rejection
velocity limit
rate limit
watchdog
```

### 4.8 Deadman / Watchdog

正式冻结：

```text id="8gy97t"
deadman=false
    → immediate zero command

connection closed
    → zero command

watchdog timeout
    → zero command

authority expired
    → zero command
```

机器人后端 再执行机器人侧第二层 watchdog。

即：

```text id="qyk8k4"
rodesk
   ↓
roserver watchdog
   ↓
机器人后端 watchdog
   ↓
base controller
```

不能只依赖浏览器端 deadman。

### 4.9 Teleoperation Feedback

同一个 WS 可以返回：

```json id="mygzul"
{
  "type": "teleop.feedback",
  "last_sequence": 123,
  "accepted": true,
  "watchdog_remaining_ms": 150
}
```

也可以发送：

```text id="fmu6hu"
control_lost
authority_expired
command_rejected
robot_fault
```

### 4.10 rodesk Teleoperation

现有：

```text id="0fqrf0"
Teleoperation HUD
TeleoperationService
```

保留。

将 Mock 实现替换为：

```text id="r9uv0x"
ProductTeleoperationService
```

并实际调用：

```text id="ewmbdc"
sendVelocity()
```

时序：

```text id="pm-teleop-switch"
Phase 0   ProductTeleoperationService 实现完成、单测通过、可注入；
          roserver RobotService 尚不存在，默认实例仍是 Mock
Phase 3A  RobotService / 机器人后端 就绪后切换默认实例
```

前端 UI 不直接理解 ROS2。

#### 现有接口必须扩展

rodesk 当前接口（`web/src/modules/teleoperation/services/teleoperationService.ts`）：

```ts id="pm-teleop-old"
interface TeleoperationService {
  connect(): Promise<void>
  disconnect(): Promise<void>
  sendVelocity(command: VelocityCommand): Promise<void>
}

interface VelocityCommand {
  linearX: number
  linearY: number
  angularZ: number
}
```

该接口**不足以表达 §4.6–§4.9 的协议**，必须扩展为：

```ts id="pm-teleop-new"
interface TeleoperationService {
  connect(input: ConnectInput): Promise<TeleoperationSession>
  disconnect(): Promise<void>

  acquireAuthority(): Promise<ControlAuthority>
  releaseAuthority(): Promise<void>

  sendVelocity(command: VelocityCommand): Promise<TeleopFeedback>

  subscribe(listener: (event: TeleopEvent) => void): () => void
}

interface ConnectInput {
  robotId: string
}

interface TeleoperationSession {
  robotId: string
  authorityId: string
  mode: 'teleoperation'
  expiresAt: string
}

interface ControlAuthority {
  authorityId: string
  robotId: string
  mode: 'teleoperation'
  expiresAt: string
}

interface VelocityCommand {
  linearX: number
  linearY: number
  angularZ: number
  sequence: number          // 单调递增，由客户端维护
  clientTimestamp: string   // RFC 3339
  deadman: boolean
}

interface TeleopFeedback {
  lastSequence: number
  accepted: boolean
  watchdogRemainingMs: number
}

type TeleopEvent =
  | { type: 'control_lost' }
  | { type: 'authority_expired' }
  | { type: 'command_rejected'; reason: string }
  | { type: 'robot_fault'; severity: string; message: string }
```

实现要求：

```text id="pm-teleop-rules"
1  connect() 必须同时取得 control authority，否则不得进入可操作状态
2  sequence 由 rodesk 维护，必须单调递增；断线重连后不得回退
3  deadman 必须由 UI 持续置位；松手立即发送 deadman=false
4  收到 control_lost / authority_expired 必须立即停止发送并归零
5  watchdogRemainingMs 必须用于 UI 提示
6  MockTeleoperationService 必须实现同一接口，禁止保留旧签名
```

`robot.state` 与 `control.authority_changed` 由 §4.5 的 Robot event 通道提供，不复用遥操作 WebSocket。

前端 UI 不直接理解 ROS2。

---

## 5. Artifact、Media、Voice 与 WebRTC

### 5.1 Artifact Product Contract

Product API：

```text id="q7fldm"
POST   /api/v1/artifacts
GET    /api/v1/artifacts/{artifact_id}
GET    /api/v1/artifacts/{artifact_id}/content
DELETE /api/v1/artifacts/{artifact_id}
```

Product：

```text id="a10nme"
artifact_id = sha256:...
```

但 roboagent：

```text id="cwcv1m"
ArtifactReferenceContent
    uri
    digest
    size
    media_type
    preview
```

其中 URI 必须是：

```text id="n693e7"
workspace://...
```

所以二者不直接相等。

#### 上传

```text id="pm-artifact-upload"
POST /api/v1/artifacts
Content-Type: multipart/form-data

字段
  file          required, 二进制
  media_type    optional, 缺省时按内容嗅探
  filename      optional
```

响应（Artifact 对象）：

```json id="pm-artifact"
{
  "artifact_id": "sha256:3f2a...",
  "media_type": "image/png",
  "size": 123456,
  "filename": "image.png",
  "created_at": "2026-10-03T10:00:00.000Z"
}
```

字段：

```text id="pm-artifact-fields"
artifact_id     string, required, "sha256:" + 64 位小写十六进制
media_type      string, required
size            integer, required
filename        string, nullable
created_at      RFC 3339, required
```

语义：

```text id="pm-artifact-semantics"
artifact_id 由内容摘要决定，不由客户端指定
相同内容重复上传 → 返回同一个 artifact_id，不产生第二份存储
上传是幂等的，不需要 Idempotency-Key
超过 max_artifact_bytes → 413 payload_too_large
不允许的 media_type → 422 unsupported_content_type
```

`GET /artifacts/{artifact_id}` 返回 Artifact 对象。

`GET /artifacts/{artifact_id}/content` 返回原始字节：

```text id="pm-artifact-content"
Content-Type    = media_type
ETag            = artifact_id
```

`DELETE /artifacts/{artifact_id}`：

```text id="pm-artifact-delete"
仅当没有 canonical message 引用该 artifact 时才允许真正删除
仍被引用时返回 409 artifact_in_use
重复 DELETE 最终保持 deleted
```

### 5.2 Artifact Mapping

映射属于 roserver：

```text id="wyarzy"
Product artifact_id
        ↓
ArtifactService
        ↓
workspace URI + digest
        ↓
roboagent
```

使用 roboagent 已公开扩展点：

```text id="zre1t6"
MediaResolver
ArtifactReader
ArtifactWriter
ArtifactDestination
```

映射表可以概念上保存：

```text id="qi0fjn"
artifact_id
workspace_uri
digest
media_type
size
owner_id
```

#### 实现结论（已按实际代码核对）

映射采用 roboagent 的规范 workspace 路径：

```text id="pm-artifact-path"
artifact_id      sha256:<64 hex>
workspace path   blobs/sha256/<64 hex>
workspace URI    workspace://blobs/sha256/<64 hex>
```

roboagent 已自带 `WorkspaceArtifactReader` / `WorkspaceArtifactDestination`；
roserver 只需实现 `Workspace` 协议，**不要**重复实现 artifact 读写管线：

```text id="pm-workspace-integration"
ArtifactService
    ↓
ArtifactWorkspace(durable = True)
    ├── WorkspaceArtifactReader
    ├── WorkspaceArtifactDestination
    └── WorkspaceToolResultMaterializer
```

`result_materializer.workspace` 与 `Session.workspace` 必须是同一个对象，
否则 Session 构造会拒绝。

Runtime 集成要点（均为实测结论）：

```text id="pm-artifact-runtime-notes"
1  二进制 Tool 输出必须走 Workspace。
   默认 InlineToolResultMaterializer 会以 tool_materialization_error 拒绝
   BinaryToolContent，因此 roserver 必须注入 Workspace 与
   WorkspaceToolResultMaterializer，否则含媒体的 Run 会失败。

2  Tool 返回值必须是 RawToolResult。
   直接返回 BinaryToolContent 违反 Tool 输出契约（tool_contract_error）；
   正确写法：
       RawToolResult((BinaryToolContent(data, media_type),))
   随后由 materializer 落成 ArtifactReferenceContent。

3  ArtifactReferenceContent 的 modality 是 FILE，不是 IMAGE。
   只声明 TEXT 的模型无法接收 artifact 引用，需要声明 FILE 输入。

4  artifact → 模型媒体的解析尚不存在。
   把图片真正作为 image 送给视觉模型，需要 roboagent 提供
   ArtifactReferenceContent → ImageContent 的模型侧解析；
   在此之前图片型 artifact 只能以 FILE modality 进入模型上下文。
   这是 Phase 4 的已知边界，非 roserver 单侧可解决。
```

### 5.3 Multimodal

rodesk AgentInput：

```text id="pjgqwc"
ContentBlock[]
```

Phase 1：

```text id="a7m9b5"
TextContent
```

Phase 4：

```text id="mqqj0k"
ImageContent
AudioContent
FileContent
ArtifactReference
```

避免把大量媒体 base64 塞入 Agent Run JSON。

### 5.4 正式媒体通道

固定：

```text id="a890hw"
HTTP
    signaling setup / metadata

WebSocket
    Agent / control events

WebRTC
    realtime audio / video
```

### 5.5 WebRTC Signaling

rodesk 已经有：

```text id="sxh65c"
CameraSignaling.exchangeOffer()
```

但没有真实实现。

roserver 必须提供对应协议。

建议：

```text id="oh3j4x"
POST   /api/v1/media/sessions
POST   /api/v1/media/sessions/{media_session_id}/offer
DELETE /api/v1/media/sessions/{media_session_id}
```

创建：

```json id="6as3ok"
{
  "kind": "call",
  "session_id": "sess_1",
  "audio": true,
  "video": true,
  "video_source": "robot_head"
}
```

返回：

```json id="of1j0k"
{
  "media_session_id": "media_1"
}
```

Offer：

```json id="uavlgh"
{
  "type": "offer",
  "sdp": "..."
}
```

返回：

```json id="ivod1d"
{
  "type": "answer",
  "sdp": "..."
}
```

V1 优先使用：

```text id="4o87gy"
non-trickle ICE
```

即等待 ICE gathering 完成后交换 SDP，减少协议复杂度。

以后有需要再引入 trickle ICE。

#### MediaSession 对象

创建响应扩展为完整对象：

```json id="pm-media-session"
{
  "media_session_id": "media_1",
  "kind": "call",
  "session_id": "sess_1",
  "state": "created",
  "video_source": "robot_head",
  "audio": true,
  "video": true,
  "created_at": "2026-10-03T10:00:00.000Z",
  "expires_at": "2026-10-03T10:10:00.000Z"
}
```

字段：

```text id="pm-media-session-fields"
media_session_id  string, required
kind              call | camera
session_id        string, nullable, kind = call 时关联 Agent Session
state             created | negotiating | connected | closed | failed
video_source      string | null, 例如 robot_head / robot_wrist / browser
audio             boolean, required
video             boolean, required
created_at        RFC 3339, required
expires_at        RFC 3339, required
close_reason      string, nullable, 仅 closed / failed
```

状态机：

```text id="pm-media-fsm"
created
   ↓ offer 已接受
negotiating
   ↓ ICE connected
connected
   ↓
closed

任一阶段失败 → failed
```

终结条件：

```text id="pm-media-terminal"
DELETE /media/sessions/{id}
客户端断开
ICE 失败
expires_at 到期
server shutdown
```

以上任一情况都必须释放：PeerConnection、track、缓冲、临时资源。

失败映射：

```text id="pm-media-failure-codes"
offer SDP 不合法 / 无法协商     → 422 invalid_input
ICE 或协商失败                  → 502 media_negotiation_failed
media session 不存在            → 404 media_session_not_found
media session 已 closed/failed  → 409 media_session_closed
```

`GET /api/v1/media/sessions/{media_session_id}` 返回同一对象，用于重连与状态恢复。

### 5.6 Media Gateway / Adapter

roserver 当前并没有 WebRTC media implementation。

因此 Phase 5 必须拆成：

```text id="2149fm"
Phase 5A
WebRTC signaling + media infrastructure

Phase 5B
Voice / Camera integration
```

roserver Media Adapter 负责：

```text id="yf7uq0"
PeerConnection lifecycle
SDP
ICE
incoming audio track
outgoing audio track
video track routing
decoded PCM bridge
```

### 5.7 roboagent Speech

roboagent Speech Runtime 已经存在，不重新实现。

职责继续包括：

```text id="z06eu7"
ASR
TTS
VAD
turn detection
barge-in
metrics
SpeechSession
```

Media Adapter 将 WebRTC audio 解码后的 PCM 输入：

```text id="8ipgo4"
SpeechSession
```

TTS PCM 再送入 WebRTC outgoing audio track。

不要求 roboagent 自己实现 WebRTC transport。

正式边界：

```text id="96cpep"
WebRTC
    belongs to roserver Media Layer

Speech semantics
    belongs to roboagent
```

这比在 roboagent 中新增强耦合 WebRTC Transport 更符合分层。

#### 集成点：SpeechTransport

roboagent 已公开集成协议（`roboagent/speech/transport/base.py`）：

```python id="pm-speechtransport"
class SpeechTransport(Protocol):
    def receive_audio(self) -> AsyncIterator[AudioChunk]: ...
    async def send_audio(self, audio: AudioChunk) -> None: ...
    async def send_event(self, event: SpeechEvent) -> None: ...
    async def clear_output(self) -> None: ...
    async def close(self) -> None: ...
```

roserver Media Adapter 必须实现该协议，把 WebRTC track 与 SpeechSession 连起来：

```text id="pm-speech-transport-flow"
WebRTC incoming audio track
        ↓ decode → PCM
MediaSpeechTransport.send_audio / receive_audio
        ↓
SpeechSession（由 create_speech_session 构造）
        ↓ ASR / Agent / TTS
SpeechSession 输出 PCM
        ↓
SpeechTransport.send_audio
        ↓ encode
WebRTC outgoing audio track
```

构造方式：

```python id="pm-speech-construct"
create_speech_session(
    session=session,          # roboagent Session
    transport=media_transport,
    config=SpeechConfig(...),
)
```

要求：

- roserver 只实现 `SpeechTransport`，不实现 ASR / TTS / VAD / turn detection；
- `clear_output()` 必须真正清空 WebRTC 侧待播放缓冲，用于 barge-in；
- 若 transport 提供 `set_render_observer`，应接入以保留 speech metrics；
- 参考实现可复用 roboagent 已有的 `examples/chat/speech_server.py`（`WebSocketSpeechTransport`）；
- roboagent 侧不需要新增 WebRTC transport。

#### Product Speech API（Phase 5B 已实现）

roserver 已按 §5.7 实现 PCM/Speech 桥，遵循与机器人/媒体层相同的原则：**契约真实、媒介模拟**。
当前端点：

```text id="pm-speech-api"
WS /api/v1/media/sessions/{media_session_id}/speech
```

server → client：roboagent 的每个 `SpeechEvent` 投影为一个 Product 事件，统一信封
（snake_case，与 §3.8 Agent 事件信封同风格）：

```json id="pm-speech-envelope"
{
  "media_session_id": "media_1",
  "session_id": "sess_1",
  "sequence": 1,
  "type": "transcript.partial",
  "timestamp": "2026-10-03T10:00:00.000Z",
  "data": {"turn_id": 1, "response_id": null, "text": "你好"}
}
```

字段：

```text id="pm-speech-envelope-fields"
media_session_id  string, required
session_id        string | null, media session 关联的 Agent Session
sequence          int, required, 每次连接从 1 单调递增
type              string, required, 见下表
timestamp         RFC 3339, required
data              object, required, 始终含 turn_id / response_id（可为 null）
```

事件类型映射（一个 roboagent 事件类型对应一个 Product 类型）：

| roboagent `SpeechEvent` | Product `type` | `data` 增量字段 |
|---|---|---|
| `speech.started` | `speech.started` | — |
| `speech.stopped` | `speech.stopped` | — |
| `transcript.partial` | `transcript.partial` | `text` |
| `transcript.final` | `transcript.final` | `text` |
| `response.started` | `response.started` | — |
| `response.delta` | `response.delta` | `delta` |
| `response.completed` | `response.completed` | — |
| `interrupted` | `speech.interrupted` | `reason` |
| `error` | `speech.error` | `message`（`code` 为已注册 Product 错误码时一并给出） |

其余低层事件（`audio.started` / `audio.completed` / `playback.begin` /
`interruption.false` / `speech.diagnostics` / `speech.metrics`）没有 Product
状态投影，只停留在内部通道。`turn_id` / `response_id` 让客户端丢弃迟到的
partial/delta，而不是让旧字幕覆盖更新的一条（roboagent event 注释明确要求这一点）。

client → server：

```text id="pm-speech-client-messages"
二进制帧   原始 PCM16 little-endian、16 kHz、单声道（canonical capture format），推荐路径
文本        {"type": "audio", "audio": "<base64>", "format": "pcm16_16k_mono"}
文本        {"type": "interrupt", "reason": "barge_in"}
```

`interrupt` 触发 barge-in：引擎作废排队与迟到的输出，并调用 roboagent
`SpeechSession.interrupt` -> `SpeechTransport.clear_output`。

错误：

```text id="pm-speech-errors"
media session 不存在            -> 404 media_session_not_found（握手拒绝）
media session 已 closed/failed  -> 409 media_session_closed（握手拒绝）
超大音频帧等中途失败            -> 同一通道发送 speech.error 信封，code 为已注册 Product 错误码
```

语音事件绝不进入 Agent Session 的事件流。配置（`ROSERVER_*`）：

```text id="pm-speech-settings"
ROSERVER_SPEECH_MAX_AUDIO_FRAME_BYTES  默认 65536，单个 client -> server 音频帧上限
ROSERVER_SPEECH_MAX_AUDIO_FRAMES       默认 32，引擎侧 capture 队列上限
ROSERVER_SPEECH_EVENT_QUEUE_BOUND      默认 256，引擎侧事件队列上限
```

#### 当前实现边界（Phase 5B）

```text id="pm-speech-stage"
SpeechEngine            抽象边界（roserver/speech/engine.py）
SimulatedSpeechEngine   离线、确定性、无 DashScope / 无网络 / 无 WebRTC 的模拟实现
```

真实（已实现并有测试覆盖）：Product WS 端点、事件信封与错误映射；MediaSession
到 speech bridge 的生命周期（WS 关闭 / DELETE / 到期 / 协商失败 / server
shutdown 全部释放）；barge-in；与核心功能隔离（speech 引擎故障不阻塞
health / session / run / artifact，语音通道降级为已注册 Product 错误）。

模拟：`SimulatedSpeechEngine` 驱动 roboagent **真实** `SpeechSession`
（`TurnDetector` / `InterruptionDetector` / `TextSegmenter` / `EnergyVAD` /
`PassthroughAudioProcessor` + render observer 记录）与
`create_speech_session` 注入点；只有 ASR（固定 transcript）、TTS
（确定性 PCM 帧）、agent 回合（确定性 `response.delta`）由模拟 provider
提供。

渲染观察者已接入：模拟 transport 提供 `set_render_observer`，`SpeechSession`
渲染出的 TTS PCM 会经 `send_audio` 到达 `audio_processor.observe_render`
（`create_speech_session` 的桥接逻辑真实执行，不再是未接线的代码路径）；
transport 缺少该 hook 时桥接按原样跳过，会话照常工作。

延后：真实媒体引擎的 audio track ↔ PCM 编解码（WebRTC 接入）、真实
ASR/TTS provider、TTS PCM 回传客户端（应由真实 WebRTC outgoing track
承载，本阶段 WS 只承载状态事件）。

### 5.8 Camera

Robot camera：

```text id="k6p0ct"
机器人后端 / camera source
        ↓
Media Layer
        ↓
WebRTC
        ↓
rodesk
```

浏览器本地 camera：

```text id="yflybw"
rodesk local MediaDevices
```

可以继续本地播放。

rodesk 现有：

```text id="a7q2vy"
useWebRTC
MediaStreamPlayer
CameraSignaling
```

均继续复用。

### 5.9 rodesk Media / Camera 接入契约

#### 现有接口与 Product 协议不同形

rodesk 当前（`web/src/modules/teleoperation/services/camera.service.ts`）：

```ts id="pm-camera-old"
interface CameraSignaling {
  exchangeOffer(
    robotId: string,
    cameraId: string,
    offer: RTCSessionDescriptionInit,
    signal: AbortSignal,
  ): Promise<RTCSessionDescriptionInit>
}
```

这是**相机中心**接口：一次调用对应一路相机。

Product 协议（§5.5）是 **media session 中心**：先创建 session，再交换 offer。

两者不冲突，但必须显式桥接，不允许在组件里各写一套。

#### 桥接实现

rodesk 提供：

```text id="pm-camera-adapter"
ProductCameraSignaling implements CameraSignaling
```

内部流程：

```text id="pm-camera-flow"
exchangeOffer(robotId, cameraId, offer, signal)
        ↓
查缓存 (robotId, cameraId) 对应的 media_session_id
        ↓ 未命中
POST /api/v1/media/sessions        { kind: "camera", video_source: ... }
        ↓
POST /api/v1/media/sessions/{id}/offer   { type: "offer", sdp }
        ↓
返回 answer
```

要求：

```text id="pm-camera-rules"
1  同一个 (robotId, cameraId) 复用一个 media session，不重复创建
2  disconnect 或 AbortSignal 触发时必须 DELETE media session
3  ICE / 协商失败必须上抛为可展示错误，不允许静默降级到 Mock
4  媒体层（Phase 5A）就绪后，默认运行时必须注入 ProductCameraSignaling；
    在此之前默认仍为 MockCameraLayer，
    但 ProductCameraSignaling 必须已实现、已单测、可注入
```

#### cameraId ↔ video_source 映射

rodesk 的 `cameraId` 是前端概念，roserver 的 `video_source` 是后端概念。映射由 roserver 配置，rodesk 不硬编码。

```text id="pm-camera-mapping"
GET /api/v1/robots/{robot_id}
    → capabilities / camera sources
```

rodesk 的 `CameraSourceMenu` / `SourceCameraAdapter` 必须以该列表为数据源，而不是本地写死。

规则（已实现，见 §4.5 Robot 对象 `cameras`）：

```text id="pm-camera-mapping-implemented"
camera_id     后端概念，rodesk 的 cameraId 与其 1:1 映射
video_source  直接作为 POST /api/v1/media/sessions { kind: "camera", video_source } 的取值
available     false = 存在但当前不可推流；rodesk 必须置灰，不得静默隐藏
cameras       始终存在，无相机时为 []
```

#### Call 页面

现有 Call 页面（`web/src/modules/agent/call/`）继续复用 UI，但必须替换：

```text id="pm-call-replace"
MockCallService          → ProductCallService
MockCallCameraAdapter    → RealCallCameraAdapter（走 §5.5）
LocalCameraAdapter       → 必须同时请求 audio 轨道
```

注意：当前 `LocalCameraAdapter` 的 `getUserMedia` **只请求 video**；语音通话必须同时请求 `audio`，否则麦克风链路无法建立。

UI 状态：

```text id="pm-call-ui-state"
Mock cyclePhase() 的演示循环
        ↓ 替换为
由 SpeechSession 真实事件驱动
    listening
    thinking
    speaking
```

`CallService` 当前 6 个空方法必须落地为真实调用；不允许保留 no-op 实现进入产品。

---

## 6. 三仓库改造与实施阶段

## Phase 0：契约冻结与最小 Runtime 修改

### roboagent：已有能力保持不动

明确列为“已有”而不是待开发：

```text id="3mpyfp"
ApprovalProvider async interface
nested ExecutionLineage
Speech Runtime
LocalSessionRepository to_thread offload
tool.started/completed/failed/cancelled
Session start/steer/follow_up
Run subscribe/result/cancel
session_id injection
```

### roboagent：真正需要修改

#### 1. stable `message_id`

修改：

```text id="84s5y4"
message model
runtime streaming
persistence
schema migration
tests
```

#### 2. `message_id` Runtime Event propagation

至少 model 生命周期，并补 Tool message attribution。

#### 3. `Session.clear_pending()`

用于 restart recovery。

#### 4. Session-level delete helper

避免 Application Host 直接依赖具体 LocalSessionRepository。

#### 5. open-by-id helper

包装：

```text id="i94pfg"
load + restore
```

#### 6. `tool_batch.committed` effect details

增加 per-tool：

```text id="exuc2s"
tool_call_id
effect_status
certainty
```

#### 7. child `model.*` lineage runtime test

已执行并已修复，见 §2.5：

```text id="pm-phase0-7"
测试    tests/runtime/test_nested_event_lineage.py
修复    AgentLoop 在每个 turn 显式传递 execution lineage
```

### roserver

从零建立：

```text id="y11ncd"
pyproject.toml
FastAPI
config
ApplicationStore
schema
AgentFactory
AgentService
Session Saga
startup reconciliation
WebSocket baseline
tests
CI
```

### rodesk

保留 UI，新增：

```text id="217eqh"
Product types（Message / RunInfo / Usage / EffectSummary / ApprovalInfo）
AgentService interface（§3.16）
HTTP transport
WebSocket transport（含 after_sequence replay）
Run model
RunProjection reducer
client_message_id
Approval UI 与 resolve 调用（§3.11）
MockAgentService based on Product Contract
```

接口调整（不新增模型，只扩展签名）：

```text id="pm-rodesk-phase0-api"
TeleoperationService（§4.10）
CameraSignaling 桥接（§5.9）
```

同时处理文档：

```text id="q8z921"
docs/mock_protocol.md
docs/communication.md
```

原则：

> 不再维护独立 Mock Protocol。

Mock 必须实现：

```text id="5dy6wv"
roserver/docs/roserver.md Product Contract
```

旧 mock_protocol 要么删除，要么改成仅描述：

```text id="dsahkt"
development mock behavior
```

而不是协议真源。

Phase 0 验收：

```text id="pm-rodesk-phase0-accept"
AgentService 全部方法有 Mock 实现并通过单测
client_message_id → canonical message_id 可 reconcile
stream.resync_required 可触发投影重拉
approval resolve 可回显 arguments_digest
TeleoperationService 新签名可用
CameraSignaling 桥接有单测（不依赖真实 WebRTC 服务）
```

---

## Phase 1：Pure Text Agent

实现：

```text id="c343py"
Session CRUD
Session list
title
Run start/get/cancel
steer
follow_up
Idempotency
Assistant streaming
stable message_id
RunProjection
Snapshot
replay/resync
assistant.aborted synthesis
restart interrupted
pending cleanup
```

验收：

```text id="0bfsxp"
rodesk text input
    ↓
roserver
    ↓
roboagent
    ↓
LLM
    ↓
streaming response
```

能够稳定工作。

---

## Phase 2：Tool / Approval / Nested Agent

实现：

```text id="3zsqsq"
Tool execution UI
Tool effect UI
Approval
Agent-as-Tool
Nested Run
child lineage
```

Phase 2 前先执行：

```text id="xcxf0r"
child model.* lineage runtime test
```

若失败，先修 roboagent。

Tool Product 投影保留：

```text id="e8w0d9"
execution_status
effect_status
certainty
```

不压缩 Runtime safety semantics。

---

## Phase 3A：机器人后端边界与模拟机器人

先实现（已完成，见 §4.1）：

```text id="qw4efr"
RobotBackend 抽象接口
SimulatedRobotBackend
RobotService
robot health
robot state
Dummy/Simulation commands
```

不实现真实 navigation，也不引入 gRPC。

### 接口方法

第一版至少：

```text id="wxj3yz"
ListRobots
GetRobotInfo
GetRobotState
WatchRobotState
AcquireControl
ReleaseControl
SendVelocity
Stop
```

说明：

```text id="pm-gateway-methods"
ListRobots        发现能力，支撑 GET /api/v1/robots
GetRobotInfo      返回后端协议版本与机器人静态信息
WatchRobotState   流式状态，使用 interval_s 而非 timeout_ms
其余方法           均带显式 timeout_ms
Robot.mode        由后端聚合返回；模拟器直接给出，真实实现可由 state 合成
```

将来由 ROS2 实现填充同一接口后，才增加：

```text id="t3jerl"
Navigate
Manipulate
Task
```

### 超时

roserver → 机器人后端的每个调用都必须有明确超时。

例如：

```text id="m0n4sv"
GetState
    short timeout

AcquireControl
    short timeout

Navigate
    long-running operation handle
```

长期操作不能靠一个无限阻塞调用。

---
## Phase 3B：Simulation Robot Integration

接：

```text id="04zowu"
MuJoCo / simulated robot
```

验证：

```text id="trcs1h"
rodesk Robot State
rodesk manual command
Agent Tool
```

全部经过：

```text id="id5a61"
RobotService
```

### Navigation 前置条件

真实：

```text id="gq8xsh"
NavigateTool
```

必须等待：

```text id="3qrict"
mfr3duo_nav
```

完成其既定 Gate。

在此之前只能：

```text id="xpyp5n"
DummyNavigate
SimulationNavigate
```

---

## Phase 4：Artifact / Multimodal

完成：

```text id="kc3qwe"
ArtifactService
artifact mapping
upload/download
ImageContent
AudioContent
FileContent
ArtifactReferenceContent
```

rodesk Composer 开始真正支持媒体。

---

## Phase 5A：WebRTC / Media 基础设施

实现：

```text id="is76af"
MediaSession
signaling API
PeerConnection lifecycle
ICE
audio track bridge
video track bridge
resource cleanup
```

rodesk：

```text id="iy257z"
CameraSignaling real implementation
```

接现有：

```text id="21vcjq"
useWebRTC
```

#### 当前阶段的实现边界

与机器人层同样的原则：**契约真实、媒介模拟**。

```text id="pm-media-stage"
现在   定义 MediaEngine 抽象边界 + SimulatedMediaEngine
       MediaSession 状态机、signaling API、到期与终结释放、错误映射
       全部真实实现并有测试覆盖
将来   用真实媒体引擎（WebRTC/ICE/track）填充同一 MediaEngine 接口
       Product API 与 rodesk 客户端契约不变
```

不引入真实 WebRTC 依赖（与「先模拟、不阻塞基础功能开发」一致）。
媒体层与机器人层一样必须与核心功能隔离：引擎失败不得阻塞
health / session / run / artifact。

### Media session lifecycle

必须定义：

```text id="55mxmv"
created
negotiating
connected
closed
failed
```

disconnect / ICE failure / timeout 必须释放资源。

---

## Phase 5B：Voice / Camera

Voice：

```text id="pvu4hx"
WebRTC audio
    ↓
roserver PCM bridge
    ↓
roboagent SpeechSession
    ↓
ASR / Agent / TTS
    ↓
WebRTC audio
```

Camera：

```text id="5eufuj"
Robot camera
    ↓
Media Layer
    ↓
WebRTC
    ↓
rodesk
```

rodesk 当前 Call 页面继续复用，不从零设计。

Mock `cyclePhase()` 被真实：

```text id="ztv7p1"
listening
thinking
speaking
```

Runtime state 替换。

实现（roserver 侧，§5.7.1 Product Speech API）：

```text id="pm-phase5b-impl"
listening   <- speech.started / transcript.partial* / speech.stopped
thinking    <- transcript.final / response.started
speaking    <- response.delta* / response.completed（TTS 播放期间）
ready/idle  <- response.completed 之后，或 speech.interrupted
```

rodesk Call 页只消费注入的 `CallEventSource`；roserver 侧由 §5.7 的 WS
推送 roboagent `SpeechEvent` 投影。当前实现使用 `SimulatedSpeechEngine`
（真实 `SpeechSession` + 模拟 ASR/TTS/agent），真实媒体引擎与 DashScope
provider 仍延后，见 §5.7「当前实现边界（Phase 5B）」与 §7.11。

---

## Phase 6：Real Robot / Teleoperation

前置必须完成：

```text id="x12swb"
机器人后端
Control Authority
Robot State
WebSocket Teleoperation
机器人后端 watchdog
Authentication
Authorization
TLS
```

然后接：

```text id="pozw0f"
real ros2_control
real base
real MoveIt
real camera
real Nav2
```

### Teleoperation 安全

必须同时具有：

```text id="spgx2a"
browser deadman
roserver watchdog
gateway watchdog
controller-level safety
```

任何一层失联：

```text id="6i3c0v"
velocity → zero
```

---

## 7. 工程、测试、版本与文档治理

### 7.1 roboagent EventStore

明确：

```text id="2f0w50"
Runtime EventStore
≠
Product Event Replay
```

V1 roserver 不接：

```text id="c59sh4"
JsonlEventStore
```

原因：

```text id="uvjg8w"
event taxonomy different
sequence different
payload different
projection different
lifecycle different
```

当前 JsonlEventStore 同步文件 I/O 是 roboagent 独立技术债，应单独修复或保持未接线。

### 7.2 rodesk 协议文档

主协议唯一来源：

```text id="ctfbqp"
roserver/docs/roserver.md
```

rodesk：

```text id="sb0f6k"
docs/communication.md
```

改为架构说明，并引用主协议。

```text id="j1c4lw"
docs/mock_protocol.md
```

不再定义独立 wire schema。

建议改成：

```text id="ux79hu"
Mock implementation guide
```

描述：

```text id="glddr8"
MockAgentService
MockTeleoperationService
MockCallService
```

如何模拟正式 Product Contract。

### 7.3 文档文件

跨仓库主规范唯一来源（当前文件名）：

```text id="fq7r7z"
roserver/docs/roserver.md
```

治理建议：将来更名为

```text id="zrm9i0"
roserver/docs/integration.md
```

以与仓库内其它文档区分，并避免"文档名与仓库名相同"带来的歧义。

该重命名**尚未执行**，属于独立动作，需要与三个仓库中的引用同步更新后再做。

要求：

```text id="pm-doc-rules"
1  主规范只保留一份，不并行维护第二份
2  协议变更必须同时更新三个仓库的实现与测试
3  rodesk / roboagent 内只保留本仓库相关说明 + 本文档版本引用
4  重命名前，所有引用仍使用 roserver/docs/roserver.md
```

### 7.4 命名

技术文档**正文**统一使用仓库实际名称：

```text id="1luji3"
rodesk
roserver
roboagent
```

Distribution：

```text id="dnyoir"
RoboAgent
```

Import：

```python id="rd5ujb"
import roboagent
```

规则：

```text id="pm-naming-rules"
1  正文禁止 RoDesk / RoServer / RoboAgent
2  仅 Python distribution name 写 RoboAgent
3  代码标识符遵循语言惯例，且不得内嵌仓库名
     允许   ProductApprovalProvider
             ProductTeleoperationService
             ProductAgentService
     禁止   RoServerApprovalProvider
             RoServerTeleoperationService
```

### 7.5 roboagent Dependency

Phase 0–1 期间没有 release/tag，所以：

```text id="pe9vqm"
exact commit
```

作为可复现依赖。

开发本地可以：

```text id="j7o45r"
editable path
```

正式发布后才切换：

```text id="eu5bk7"
RoboAgent>=X.Y,<next-major
```

其中 X.Y 必须是：

> 第一个完整实现 integration contract 的 roboagent release。

当前不能写：

```text id="gqf3e6"
>=1.4
```

除非 1.4 真正已经发布。

### 7.6 rodesk dependency pinning

当前 `package.json` 大量：

```text id="50yk3m"
latest
```

不符合可复现构建要求。

在 Phase 0 同时处理：

```text id="8nd3ey"
Vue
Pinia
Vite
TypeScript
Tailwind
Tauri JS API
Vitest
Playwright
```

至少固定：

```text id="y3atq0"
明确 major/minor range
```

并保留 pnpm lockfile。

不要求所有 patch 都锁死，但禁止核心依赖长期 `"latest"`。

### 7.7 CI

roboagent：

```text id="g5784q"
ruff
mypy
pytest
```

roserver：

```text id="yn6oxc"
ruff
mypy
pytest
```

rodesk：

```text id="n3xmi4"
lint
typecheck
unit test
build
```

desktop：

```text id="31sefp"
cargo fmt
cargo clippy
cargo check
```

机器人后端：

```text id="ul656n"
colcon build
unit tests
robot backend contract tests
clang-tidy / format according to existing policy
```

### 7.8 Integration Tests

Phase 1：

```text id="mmoufc"
rodesk-like client
    ↓
roserver
    ↓
Fake roboagent model
```

Phase 3：

```text id="mnji1r"
roserver
    ↓
Fake RobotBackend
```

Phase 5：

```text id="tqk7pk"
real RTCPeerConnection integration test
```

CI 不依赖真实：

```text id="cmc8vu"
cloud LLM
physical robot
camera
microphone
```

### 7.9 Error model

Product Error Code 与 Runtime error 分离。

必须维护显式 mapping。

同样，机器人后端错误 → Product Error 也要映射。
当前模拟实现抛出的异常对应关系（将来 ROS2 实现需保持同一语义）：

```text id="eaz1je"
调用超时
    → robot_timeout

后端不可达
    → robot_unavailable

机器人未就绪
    → robot_not_ready

未持有控制权
    → control_authority_required

机器人不存在
    → robot_not_found
```

Agent / Robot / Media Product API 都遵循同一 Error Envelope。

### 7.10 Security baseline

Phase 1：

```text id="hn8bl6"
bind 127.0.0.1
explicit CORS
WS Origin validation
```

LAN 模式：

```text id="k3ipom"
explicit enable
```

正式 Robot control 前：

```text id="7ckwgq"
TLS
Authentication
Authorization
WS Authentication
Control Authority
```

WebRTC signaling 同样需要认证，不能把 camera stream 当成匿名公开资源。

---

### 7.11 当前实现边界（真实 vs 模拟）

项目当前**尚未进入机器人与真实媒体集成阶段**，因此按下表区分。
契约（Product API、事件、错误码）全部真实；只有需要外部硬件/媒体的驱动层是模拟。

| 领域 | 真实实现 | 模拟 / 延后 | 转真实的前置条件 |
|---|---|---|---|
| Agent / Session / Run / Event | 全部 | — | — |
| canonical `message_id` | 全部（含 v1→v2 迁移） | — | — |
| Tool / Effect / Approval | 全部 | — | — |
| Artifact / Multimodal | 存储、上传下载、引用保护、`workspace://` 映射 | — | artifact→视觉模型需 roboagent 侧解析 |
| 机器人后端 | Product API、控制权、遥操作校验、watchdog、故障隔离 | 机器人本体（`SimulatedRobotBackend`） | 用 `dclpy` 实现 `RobotBackend` |
| 机器人 Agent Tool | 工具链路、审批门控、经 `RobotService` | 导航为 `SimulationNavigate` | `mfr3duo_nav` 完成 Gate |
| Media 信令 | MediaSession 状态机、4 个端点、到期与终结释放、隔离 | 媒体引擎（SDP answer / ICE / track） | 真实引擎实现 `MediaEngine` |
| Voice（Phase 5B） | Product Speech WS、事件信封/映射、barge-in、MediaSession 生命周期与隔离；roboagent `SpeechSession` 真实驱动 | ASR/TTS/agent provider（`SimulatedSpeechEngine`）；TTS PCM 未回传客户端 | 真实媒体引擎 + 音轨桥接 + DashScope provider |
| rodesk | 全部（AgentService、投影、媒体块、机器人状态、遥操作） | Mock 数据源为默认 | 配 `VITE_AGENT_SERVICE=http` |
| 安全 | 绑定 127.0.0.1、显式 CORS、WS Origin 校验 | TLS / Authentication / Authorization | 正式机器人控制前必须完成 |

约束（已在代码中落实）：

```text id="pm-boundary-rules"
1  机器人层与媒体层不得 import 核心模块之外的核心逻辑，
   核心模块（agent / artifact / store）不得 import robot / media
2  两层启动或运行失败必须被隔离并记录，不得阻塞应用启动
3  两层都通过可注入接口提供模拟实现，测试与开发不依赖硬件或网络
4  Product API 与前端契约不因替换实现而改变
```

--- 

# 最终系统模型

完整目标架构：

```text id="3qvyca"
                           rodesk
                   Vue 3 / Tauri
             Presentation / Interaction
                            │
          ┌─────────────────┼─────────────────┐
          │ HTTP            │ WS              │ WebRTC
          ▼                 ▼                 ▼
                        roserver
                   Application Backend
          ┌────────────┬────────────┬────────────┐
          │            │            │            │
          ▼            ▼            ▼            ▼
     AgentService  RobotService Artifact     Media /
          │            │        Service     Teleoperation
          │            │                       │
          ▼            ▼                       │
     roboagent    RobotBackend                  │
     Runtime            │                       │
                        ▼                       │
              SimulatedRobotBackend ◄───────────┘
                        ┆
                        ┆ 将来替换为 ROS2 实现
                        ▼
                    ROS2 / SDK
          ros2_control / MoveIt / Nav2
```

Agent path：

```text id="mgzj17"
rodesk
   ↓
roserver
   ↓
roboagent
```

Robot manual path：

```text id="m4mf5b"
rodesk
   ↓
roserver RobotService
   ↓
RobotBackend
   ↓
ROS2
```

Agent robot tool path：

```text id="2vfxpt"
roboagent Tool
   ↓
injected RobotService
   ↓
RobotBackend
   ↓
ROS2
```

Voice path：

```text id="u12rc0"
rodesk
   ↓ WebRTC
roserver Media Layer
   ↓ PCM
roboagent Speech Runtime
```

Teleoperation path：

```text id="d9o6is"
rodesk joystick
   ↓ WebSocket
roserver TeleoperationService
   ↓ RobotBackend
   ↓
robot controller
```

最终职责归纳为：

```text id="yvvxr6"
rodesk
    owns presentation

roserver
    owns application semantics

roboagent
    owns agent runtime semantics

机器人后端
    owns robot runtime integration
```

这是后续所有实现和评审的统一判断标准。

Phase 0–2 的目标是稳定 Agent 主链路；Phase 3A 才正式建立机器人边界；Phase 5A 才正式建立媒体边界。只有这些基础设施完成以后，真实导航、Voice、Camera 和 Teleoperation 才进入产品集成阶段。