#!/usr/bin/env bash
set -eo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source /opt/ros/humble/setup.bash
source "$root/mfr3duo_ros2/install/setup.bash"
export PYTHONPATH="$root/deps/dclpy-install:$root/roserver:$root/roboagent"
export ROSERVER_ROBOT_BACKEND=dclpy
export ROS_LOG_DIR="${ROS_LOG_DIR:-$root/log/roserver}"
cd "$root/roserver"
exec .venv/bin/python -m roserver "$@"
