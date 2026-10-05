#!/usr/bin/env bash
set -eo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source /opt/ros/humble/setup.bash
source "$root/mfr3duo_ros2/install/setup.bash"
python="${DCLPY_PYTHON:-$root/dcl/dclpy/.venv/bin/python}"
pybind_dir="$("$python" -m pybind11 --cmakedir)"
cmake -S "$root/dcl/dmw" -B "$root/build/dcl-dmw" \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF \
    -DCMAKE_INSTALL_PREFIX="$root/deps/dcl-install"
cmake --build "$root/build/dcl-dmw" -j "${DCLPY_BUILD_JOBS:-2}"
cmake --install "$root/build/dcl-dmw"
cmake -S "$root/dcl/dclpy" -B "$root/build/dclpy-roserver" \
    -DCMAKE_BUILD_TYPE=Release -DPython_EXECUTABLE="$python" -Dpybind11_DIR="$pybind_dir" \
    -Ddmw_DIR="$root/deps/dcl-install/lib/cmake/dmw" \
    -DCMAKE_INSTALL_PREFIX="$root/deps/dclpy-install" \
    -DDCLPY_BUILD_MFR3DUO_INTERFACES=ON \
    -DDCLPY_MFR3DUO_INTERFACE_PREFIX="$root/mfr3duo_ros2/install/mfr3duo_msgs"
cmake --build "$root/build/dclpy-roserver" -j "${DCLPY_BUILD_JOBS:-2}"
cmake --install "$root/build/dclpy-roserver"
PYTHONPATH="$root/deps/dclpy-install" "$python" -c \
    'from dclpy import build_info; from mfr3duo_msgs_dclpy.action import ExecuteTask; from sensor_msgs_dclpy.msg import Image; print(build_info())'
