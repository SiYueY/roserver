# roserver

rodesk 的 Product 后端。`simulated` 模式用于离线开发；`dclpy` 模式连接
ROS 2 Humble 的 mfr3duo_ros2，复用现有 Robot SDK 的导航、物理抓取和放置。
Agent、手动任务与遥操作共用 RobotService，仍只启动一个 uvicorn worker。

## 构建与启动

以下命令在父目录 `mfr3duo` 执行。需要先具备 ROS 2 Humble、已构建的
mfr3duo_ros2 与 dclpy 的 CPython 3.12 构建环境。ROS CLI 使用系统 Python 3.10；
roserver/dclpy 使用 Python 3.12，不能混用两套 Python 消息扩展。

```bash
source /opt/ros/humble/setup.bash
cmake -S robot_mujoco/romujoco -B robot_mujoco/romujoco/build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PWD/deps/romujoco-install"
cmake --build robot_mujoco/romujoco/build -j2
cmake --install robot_mujoco/romujoco/build
export CMAKE_PREFIX_PATH="$PWD/deps/romujoco-install:$CMAKE_PREFIX_PATH"
cd mfr3duo_ros2
colcon build --symlink-install --packages-up-to mfr3duo_robot --parallel-workers 2 \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
cd ..
bash roserver/scripts/build-dclpy.sh
cd roserver
uv sync --extra robot
cd ..
```

`build-dclpy.sh` 将 dmw 安装到 `deps/dcl-install`，将 dclpy 与 ROSIDL 生成的
消息、Service、Action 绑定安装到 `deps/dclpy-install`。可用 `DCLPY_PYTHON`
指定 CPython 3.12 的解释器；默认使用 `dcl/dclpy/.venv/bin/python`。

终端一启动完整机器人栈。`serve_tasks` 默认开启，复用已有 `task_demo`
可执行程序的常驻服务模式，和 `run_demo=true` 互斥：

```bash
source /opt/ros/humble/setup.bash
source mfr3duo_ros2/install/setup.bash
export ROS_DOMAIN_ID=42
export ROS_LOG_DIR="$PWD/log/robot"
ros2 launch mfr3duo_robot robot.launch.py viewer_enabled:=true
```

厨房资源由 `mfr3duo_ros2/mfr3duo_scenes` 管理，原始 RoboCasa 导出保留在
`scenes/kitchen/raw`，运行时不依赖原导出目录或 RoboCasa Python 环境。
把上面的启动命令换成以下命令即可使用厨房；roserver 和 rodesk 的连接方式相同：

```bash
ros2 launch mfr3duo_robot robot.launch.py scene:=kitchen viewer_enabled:=true
```

若需活动抽屉和柜门，改用 `scene:=kitchen_interactive`。目前开放一个下柜抽屉
（`bottom_main_group_1_slidejoint`，范围 -0.78..0 米）和一个上柜左门
（`top_main_group_leftdoorhinge`，范围 -1.57..0 弧度）。这是有测量反馈的仿真
环境驱动功能；机械臂抓把手开门属于另外的操作技能。其他厨房关节保持固定。
厨房使用仿真底盘实测位姿校正轮速里程计，并保留激光障碍层、地图及包含双臂的
实体碰撞凸包。固定柜体已合并以降低物理开销，活动场景只保留上述两个关节。
新增消息绑定已包含 `TaskStep.position`，更新 ROS 包后需要重新运行
`build-dclpy.sh` 并重启 roserver。

终端二启动 roserver。此处 `echo` 只用于启动检查，不会调用机器人工具；
实际 Agent 使用 `ROSERVER_MODEL_CONFIG` 和可选 `ROSERVER_MODEL_NAME` 配置模型：

```bash
export ROS_DOMAIN_ID=42
export ROSERVER_MODEL=echo
export ROSERVER_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
bash roserver/scripts/run-dclpy.sh --host 127.0.0.1 --port 8765
```

使用 DeepSeek Flash 时，复制 `config/deepseek-flash.yaml.example` 到仓库外的
私有路径，并在该文件同级 `.env` 或启动环境中设置 `DEEPSEEK_API_KEY`。不要保留
`ROSERVER_MODEL=echo`，否则离线 EchoModel 会优先于模型配置。真实机器人任务默认
在执行前产生审批请求；在 rodesk 中批准后才会发送到 ROS。DeepSeek 的推理内容会在
工具回合之间保留，但不会投影到产品消息；相机工具产物会作为图像发送给支持视觉的
DeepSeek Flash。

```bash
mkdir -p "$HOME/.config/roserver"
cp config/deepseek-flash.yaml.example "$HOME/.config/roserver/deepseek-flash.yaml"
# Use an editor to create $HOME/.config/roserver/.env with:
# DEEPSEEK_API_KEY=your-real-key
chmod 600 "$HOME/.config/roserver/.env"

unset ROSERVER_MODEL
export ROSERVER_MODEL_CONFIG="$HOME/.config/roserver/deepseek-flash.yaml"
export ROSERVER_MODEL_NAME=deepseek-flash
```

rodesk/web 使用以下环境启动，Agent、状态、遥操作和视频统一指向后端：

```bash
VITE_AGENT_SERVICE=http VITE_ROSERVER_BASE_URL=http://127.0.0.1:8765 npm run dev
```

同一局域网中的手机使用电脑的局域网地址。当前电脑地址为 `192.168.10.166`；
地址变化后需同步替换下面两处。保留终端一的厨房仿真，终端二改为：

```bash
cd /home/siyuey/workspace/mfr3duo
export ROS_DOMAIN_ID=42 ROSERVER_ROBOT_DOMAIN_ID=42 ROSERVER_MODEL=echo
export ROSERVER_CORS_ORIGINS=http://192.168.10.166:5173,http://localhost:5173,http://127.0.0.1:5173
bash roserver/scripts/run-dclpy.sh --host 0.0.0.0 --port 8765
```

终端三启动手机可访问的前端，然后在手机打开 `http://192.168.10.166:5173`：

```bash
cd /home/siyuey/workspace/mfr3duo/rodesk/web
VITE_AGENT_SERVICE=http VITE_ROSERVER_BASE_URL=http://192.168.10.166:8765 \
  npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
```

场景、遥操作、相机切换和手动任务不需要 API Key；`echo` 用于连接和手动控制。
自然语言调用机器人工具需要另行配置支持工具调用的模型及该模型所需的凭据。

## 已接入的能力

| 能力 | 实际通道 |
| --- | --- |
| 发现与状态、事件 | 真实 odometry、按关节名合并 JointState、诊断；未知电量返回 null |
| 物体与工具观察 | DDS Graph 发现 PoseStamped；返回新鲜物体 ID、位姿和整机 readiness/诊断 |
| 遥操作 | HTTP 控制租约、续期、WebSocket 速度与反馈；服务端 watchdog、控制器 watchdog、断线归零 |
| 导航、抓取、放置、任务序列 | `/robot/execute_task` Action → Robot SDK → Nav2 / MoveIt / ros2_control |
| 左右夹爪 | 现有 Move / Grasp Action，宽度为整个夹爪开口，单位米 |
| 停止与恢复 | 等待 Action 终态，终止不确定时禁止新任务和遥操作；显式 recover 重新初始化 SDK |
| 相机 | 七路彩色相机按需采集、480×270 / 30 FPS WebRTC、JPEG 快照、切换与自动重连 |
| Agent 工具 | get_robot_state、get_robot_observations、execute_robot_task、stop_robot、get_camera_image（进入 Artifact） |
| 会话、Run、审批、Artifact、幂等 | 保留现有 Product API；物理操作另有持久记录及重启协调 |

手动任务接口：

```bash
curl -s http://127.0.0.1:8765/api/v1/robots/robot_1/state
curl -s -X POST http://127.0.0.1:8765/api/v1/robots/robot_1/operations \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: navigate-once' \
  -d '{"kind":"navigate","pose":{"frame_id":"map","x":-0.35,"y":0.7,"theta":0},"timeout_s":60}'
```

返回 `operation_id` 后，可 GET `/api/v1/robots/robot_1/operations/{operation_id}`，
或 POST 同一路径加 `/cancel`；POST `/api/v1/robots/robot_1/stop` 取消本服务任务
并归零底盘。`kind` 支持 `navigate/pick/place/sequence/scene_joint/gripper_move/gripper_grasp/recover`。
`sequence.steps` 为 1–32 个 navigate/pick/place/scene_joint，整组和单步均有超时。
Pick/Place 使用 `object_id`，可选 `manipulator=auto/left/right`；Place 必须提供 pose，
抓放默认使用 `simulation_world`，导航默认 `map`。

厨房导航目标示例为 `(2.45, -1.45, 1.5707963268)`，抓放物体为 `box`。
厨房中的有效停靠点、出生姿态和放置点见 `mfr3duo_scenes/scenes/kitchen/scene.yaml`，
不要沿用简单测试场景的坐标。活动场景中的 `scene_joint` 使用 `object_id`
指定关节，并通过 `position` 指定开度，例如：

```bash
curl -s -X POST http://127.0.0.1:8765/api/v1/robots/robot_1/operations \
  -H 'Content-Type: application/json' \
  -d '{"kind":"scene_joint","object_id":"bottom_main_group_1_slidejoint","position":-0.2,"timeout_s":20}'
```

关节实时位置和速度通过 observations 返回；任务成功依赖新鲜测量、位置误差
和速度收敛。越限请求会失败，取消会等待保持命令确认；该任务与遥操作、导航
和抓放共用任务所有权。携带物体时禁止驱动场景关节。

操作持久化后才向 ROS 派发；相同 Idempotency-Key 与参数返回原记录，不重复执行，
参数不同返回冲突。进程重启只查询、取消保存的 ROS UUID，不重放动作。最近 256 个
操作可查询，幂等记录按 `ROSERVER_IDEMPOTENCY_RETENTION` 在启动时清理。
若无法确认远端终态，需先执行 `{"kind":"recover"}`；recover 不等于放下所持物体。
夹爪操作的终态仍未知时，recover 先协调原 UUID，无法确认则保留门禁；
成功恢复会持久记录，不会在下一次服务重启时重新打开已处理的故障。
手动遥操作和任务互斥。底盘线速度按平面向量模长限制为 0.3 m/s，角速度 0.5 rad/s；
rodesk 摇杆默认更低，松手、失焦与隐藏页面发送停止。

可配置 `ROSERVER_ROBOT_ID/NAMESPACE/DOMAIN_ID/STATE_TIMEOUT/OPERATION_TIMEOUT/TERMINAL_TIMEOUT`
以及 `ROSERVER_ROBOT_CAMERA_ENABLED`。当前整机 launch 是根 namespace 的单机器人部署；
非根 namespace 必须在 ROS 端提供一致的 topic/service/action 重映射。

## 验证与当前边界

```bash
cd roserver
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -q
.venv/bin/ruff check roserver
.venv/bin/mypy roserver
# 独立 ROS domain 的完整验收；会启动自己的无窗口 MuJoCo 并实际移动、抓放。
source /opt/ros/humble/setup.bash
source ../mfr3duo_ros2/install/setup.bash
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py
# 仅验证遥操作松手/超时后的重新运动，以及 JPEG/WebRTC 视频。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --teleoperation-only
# 逐路测量七台相机的不同采集时间戳及 WebRTC 解码帧率。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --camera-fps
# 真实 Chrome 接收视频；以手机横屏尺寸验证七路切换、解码帧率及快速重选。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --browser-video
# 使用桌面交互窗口的部署方式，验证新画面采集帧率。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --camera-fps --viewer
# 厨房真实导航、抓放，以及活动抽屉／柜门开合、越限和取消保持。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --scene kitchen_interactive
# 厨房七路 WebRTC 帧率、手机横屏浏览器切换，包含桌面 MuJoCo 窗口。
PYTHONPATH=../deps/dclpy-install:.:../roboagent .venv/bin/python tests/ros2_runtime.py --scene kitchen --camera-fps --browser-video --viewer
```

整栈验收使用确定性模型证明 Agent 工具链、真实物理抓放、取消和视频；自然语言
决策效果取决于配置的模型，尚未以外部模型做验收。对象观察复用当前 MuJoCo 真值
通道，当前场景提供 `box`，尚无真实视觉识别、多物体场景或持物导航。
厨房端到端验收已实际完成两段导航、抓取、约 10 cm 物理抬升、放置、抽屉和柜门
双向动作、越限拒绝及取消保持。物理校验失败仍会如实返回失败，不能以 Agent 文本
判断抓放成功。
遥操顶部的「相机」下拉框可以切换头部、前置、后置、左右侧与左右腕相机；
旁边显示浏览器实际解码 FPS。切换时释放旧媒体会话和 DDS 订阅，短暂采集空档
保留视频轨道，连接失败或持续没有解码帧时自动重连。
30 FPS 的目标是单路活动相机（同源多观看者共享采集）；多路同时观看及机器负载
会影响帧率。仿真只渲染有图像订阅者的相机，彩色分辨率 480×270，采集周期
10 ms，为 30 FPS 输出留余量；相机渲染关闭阴影、反射及多重采样。
MuJoCo 交互窗口初始大小 960×540，限制到 15 FPS，并默认关闭阴影与反射，
默认显示碰撞几何，保留遥操视频里的完整视觉模型；仍可使用原有窗口交互。
这部分包含原生库和硬件插件修改，更新后需要重启 ROS 仿真与 roserver。
相机 WebRTC 适用于本机/局域网，尚未配置公网 TURN。
机器人没有麦克风、扬声器或真实 ASR/TTS provider：真实模式音频媒体请求明确返回
`provider_unavailable`；已有 Speech/PCM 桥仍为可注入的模拟 provider。真实机器人故障
不会切回模拟成功，也不影响会话、Run、审批和 Artifact 等核心功能。
验收中曾遇到 Nav2/AMCL 的 DDS 生命周期服务响应超时，干净独立域重新启动后通过；
此时需要先恢复 ROS 栈就绪，roserver 会继续提供状态/视频并拒绝未就绪的导航。
