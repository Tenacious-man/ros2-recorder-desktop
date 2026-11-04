"""Isolated ROS subscriber for the integrated manual calibration page."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

try:
    from .calibration_cloud import preview_cloud
except ImportError:
    from calibration_cloud import preview_cloud


def emit(value):
    print("@@RESULT@@" + json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def run(control, directory):
    import rclpy
    from rclpy.context import Context
    from rclpy.executors import SingleThreadedExecutor, ExternalShutdownException
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import PointCloud2

    context = Context()
    rclpy.init(args=[], context=context)
    node = rclpy.create_node("recorder_manual_calibration", context=context)
    executor = SingleThreadedExecutor(context=context)
    executor.add_node(node)
    qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
    subscriptions, cached = {}, {}
    selected, dirty = {}, set()
    settings = None
    last_control = last_report = 0.
    revision = -1
    generation = 0

    def receive(role, message):
        cached[role] = message
        dirty.add(role)

    try:
        while rclpy.ok(context=context):
            now = time.monotonic()
            if now - last_control >= .05:
                last_control = now
                desired = json.loads(control.read_text(encoding="utf-8"))
                if desired["revision"] != revision:
                    for role in ("reference", "input"):
                        topic = desired["topics"].get(role, "")
                        if selected.get(role, "") != topic:
                            if role in subscriptions:
                                node.destroy_subscription(subscriptions.pop(role))
                            cached.pop(role, None)
                            dirty.discard(role)
                            if topic:
                                subscriptions[role] = node.create_subscription(
                                    PointCloud2, topic, lambda message, role=role: receive(role, message), qos)
                    selected = dict(desired["topics"])
                    settings = desired
                    generation, revision = desired["generation"], desired["revision"]
                    dirty.update(cached)
                    emit({"generation": generation, "ready": True})
            executor.spin_once(timeout_sec=.02)
            now = time.monotonic()
            if settings is None or now - last_report < .2:
                continue
            last_report = now
            for role in tuple(dirty):
                dirty.discard(role)
                message = cached.get(role)
                if message is None:
                    continue
                try:
                    value = preview_cloud(message)
                    destination = directory / (role + ".npz")
                    temporary = directory / (role + ".tmp")
                    with temporary.open("wb") as output:
                        np.savez(output, xyz=value["xyz"], fields=value["fields"], indexes=value["indexes"],
                                 generation=generation, total=value["total"], frame=value["frame"], stamp_ns=value["stamp_ns"])
                    temporary.replace(destination)
                    emit({"generation": generation, "role": role, "sample": True})
                except Exception as exc:
                    emit({"generation": generation, "role": role, "error": str(exc)[:800]})
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown(timeout_sec=2)
        node.destroy_node()
        if context.ok():
            rclpy.shutdown(context=context)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        return run(arguments.control, arguments.directory)
    except Exception as exc:
        emit({"error": str(exc)[:800]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
