"""Latest header sampler used by probe.py; no payload history or matching."""
from __future__ import annotations

from collections import deque
import json
import time

try:
    from .header_stamp import HeaderStampReader
except ImportError:
    from header_stamp import HeaderStampReader


class LatestStampState:
    def __init__(self, token, *, error=''):
        self.token = token
        self.started_ns = time.monotonic_ns()
        self.first_ns = None
        self.max_gap_ns = 0
        self.ages_ns = deque(maxlen=2048)
        self.max_age_ns = None
        self.age_count = 0
        self.received_count = 0
        self.received_ns = None
        self.received_wall_ns = None
        self.stamp_ns = None
        self.status = 'no_header' if error else 'waiting'
        self.error = error

    def receive(self, payload, reader, *, now_ns=None):
        self.received_wall_ns = time.time_ns()
        arrival = time.monotonic_ns() if now_ns is None else now_ns
        if self.received_ns is None:
            self.first_ns = arrival
        else:
            self.max_gap_ns = max(self.max_gap_ns, arrival - self.received_ns)
        self.received_ns = arrival
        self.received_count += 1
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
                age = self.received_wall_ns - self.stamp_ns
                self.ages_ns.append(age)
                self.age_count += 1
                self.max_age_ns = age if self.max_age_ns is None else max(self.max_age_ns, age)
        except Exception as exc:
            self.status, self.error = 'decode_error', '最新消息时间戳解码失败：' + str(exc)[:300]

    def snapshot(self, now_ns=None):
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        span = now_ns - self.first_ns if self.first_ns is not None else 0
        ages = sorted(self.ages_ns)
        p99 = ages[(99 * len(ages) + 99) // 100 - 1] / 1e6 if ages else None
        return {'token': self.token, 'received_count': self.received_count, 'received_ns': self.received_ns,
                'received_wall_ns': self.received_wall_ns, 'stamp_ns': self.stamp_ns,
                'status': self.status, 'error': self.error,
                'seconds': max(0, now_ns - self.started_ns) / 1e9,
                'hz': (self.received_count - 1) * 1e9 / span if self.received_count > 1 and span > 0 else None,
                'max_gap_ms': max(self.max_gap_ns, now_ns - self.received_ns) / 1e6 if self.received_ns is not None else None,
                'age_count': self.age_count, 'age_p99_ms': p99,
                'age_max_ms': self.max_age_ns / 1e6 if self.max_age_ns is not None else None}


def watch_stamps(node, executor, context, control, emit):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
    from rosidl_runtime_py.utilities import get_message

    settings = json.loads(control.read_text(encoding='utf-8'))
    reliable = settings.get('reliability') == 'reliable'
    qos = QoSProfile(history=HistoryPolicy.KEEP_LAST, depth=settings.get('depth', 5),
                     reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
                     durability=DurabilityPolicy.VOLATILE)
    subscriptions, states, requested = {}, {}, {}
    generation = -1
    last_control = last_report = last_qos_check = 0.0
    incompatible = set()
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
                        incompatible.discard(name)
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
        if reliable and now - last_qos_check >= 1:
            last_qos_check = now
            incompatible.clear()
            for name in subscriptions:
                offered = node.get_publishers_info_by_topic(name)
                if offered and all(pub.qos_profile.reliability == ReliabilityPolicy.BEST_EFFORT for pub in offered):
                    incompatible.add(name)
        if now - last_report >= .1:
            now_ns = time.monotonic_ns()
            samples = {name: state.snapshot(now_ns) for name, state in states.items()}
            for name in incompatible:
                samples[name].update(status='waiting', error='发布端仅支持 Best Effort，请切换订阅方式')
            emit({'generation': generation, 'samples': samples})
            last_report = now
    return 0
