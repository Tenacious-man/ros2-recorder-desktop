#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

installer_python="${ROS2_RECORDER_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$installer_python" ]]; then
  for installer_arg in "$@"; do
    case "$installer_arg" in
      --check|--dry-run|--help|-h)
        echo "需要 Python 3 才能检查安装计划；本次未修改系统。"
        exit 1
        ;;
    esac
  done
  installer_admin=()
  if [[ "$EUID" -ne 0 ]]; then installer_admin=(sudo); fi
  "${installer_admin[@]}" apt-get update
  "${installer_admin[@]}" apt-get install -y python3 ca-certificates
  installer_python=/usr/bin/python3
fi
exec "$installer_python" -m ros2_recorder.installer "$@"
