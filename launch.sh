#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec "${ROS2_RECORDER_PYTHON:-/usr/bin/python3}" -m ros2_recorder "$@"
