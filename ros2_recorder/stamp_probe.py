"""Latest header sampler used by probe.py; no payload history or matching."""
from __future__ import annotations

import json
import time

try:
    from .header_stamp import HeaderStampReader
except ImportError:
    from header_stamp import HeaderStampReader


class LatestStampState:
    def __init__(self, token, *, error=''):
        self.token = token
        self.received_count = 0
        self.received_ns = None
        self.stamp_ns = None
        self.status = 'no_header' if error else 'waiting'
        self.error = error

    def receive(self, payload, reader, *, now_ns=None):
        self.received_count += 1
        self.received_ns = time.monotonic_ns() if now_ns is None else now_ns
        # Invalid newest messages replace the old value too. Never display an
        # earlier good timestamp as if it belonged to the latest arrival.
        self.stamp_ns = None
        if reader is None:
            self.status = 'no_header'
            return
        try:
            stamp = reader.read(payload)
            if not 0 <= stamp.nanosec < 1_000_000_000:
                self.status, self.error = 'invalid', '最新消息的 header.nanosec 无效'
                return
            self.stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
            if self.stamp_ns == 0:
                self.status, self.error = 'invalid', '最新消息的 header.stamp 为零，暂不比较'
            else:
                self.status, self.error = 'ok', ''
        except Exception as exc:
            self.status, self.error = 'decode_error', '最新消息时间戳解码失败：' + str(exc)[:300]

    def snapshot(self):
        return {'token': self.token, 'received_count': self.received_count, 'received_ns': self.received_ns,
                'stamp_ns': self.stamp_ns, 'status': self.status, 'error': self.error}


def watch_stamps(node, executor, context, control, emit):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from rosidl_runtime_py.utilities import get_message

    # Best effort can receive either reliable or best-effort sensor publishers.
    # A shallow queue keeps this view about recent arrivals, rather than backlog.
    qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=5,
                     reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
    subscriptions, states, requested = {}, {}, {}
    generation = -1
    last_control = last_report = 0.0
    while rclpy.ok(context=context):
        now = time.monotonic()
        if now - last_control >= .1:
            last_control = now
            desired = json.loads(control.read_text(encoding='utf-8'))
            if desired['generation'] != generation:
                wanted = desired['topics']
                for name in list(requested):
                    if wanted.get(name) != requested[name]:
                        if name in subscriptions:
                            node.destroy_subscription(subscriptions.pop(name))
                        states.pop(name, None)
                        requested.pop(name)
                for name, item in wanted.items():
                    if name in requested:
                        continue
                    requested[name] = item
                    state = LatestStampState(item['token'])
                    states[name] = state
                    try:
                        cls = get_message(item['type'])
                        try:
                            reader = HeaderStampReader(cls)
                        except ValueError as exc:
                            reader = None
                            state.status, state.error = 'no_header', str(exc)
                        subscriptions[name] = node.create_subscription(
                            cls, name, lambda payload, s=state, r=reader: s.receive(payload, r), qos, raw=True)
                    except Exception as exc:
                        state.status, state.error = 'unavailable', '无法订阅，请检查消息类型：' + str(exc)[:300]
                generation = desired['generation']
        try:
            executor.spin_once(timeout_sec=.02)
        except rclpy.executors.ExternalShutdownException:
            break
        now = time.monotonic()
        if now - last_report >= .1:
            emit({'generation': generation, 'samples': {name: state.snapshot() for name, state in states.items()}})
            last_report = now
    return 0
