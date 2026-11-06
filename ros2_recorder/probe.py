"""Disposable ROS graph/frequency helpers. JSON protocol keeps ROS out of the GUI process."""
import argparse
from collections import deque
import json
import math
import signal
import sys
import time
from pathlib import Path


class FrequencyWindow:
    def __init__(self):
        self.arrivals = deque(maxlen=2000)
        self.last_received = None

    def received(self, _):
        now = time.monotonic()
        self.arrivals.append(now)
        self.last_received = now

    def sample(self, now):
        while self.arrivals and self.arrivals[0] < now - 5:
            self.arrivals.popleft()
        intervals = [b - a for a, b in zip(self.arrivals, list(self.arrivals)[1:])]
        mean = sum(intervals) / len(intervals) if intervals else None
        age = now - self.last_received if self.last_received is not None else None
        return {"hz": 1 / mean if mean and mean > 0 else None,
                "stale": age is None or age > 2, "age": age, "samples": len(self.arrivals),
                "min_ms": min(intervals) * 1000 if intervals else None,
                "max_ms": max(intervals) * 1000 if intervals else None,
                "jitter_ms": math.sqrt(sum((v - mean)**2 for v in intervals) / len(intervals)) * 1000 if intervals else None}


def subscription_qos(node, topic):
    """Request Reliable only when every discovered publisher explicitly offers it."""
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    publishers = node.get_publishers_info_by_topic(topic)
    reliable = bool(publishers) and all(
        p.qos_profile.reliability == ReliabilityPolicy.RELIABLE for p in publishers)
    policy = ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT
    return QoSProfile(depth=10, reliability=policy)


def watch_topics(node, executor, context, control):
    import rclpy
    from rosidl_runtime_py.utilities import get_message
    subscriptions, windows, requested = {}, {}, {}
    last_report, last_control = time.monotonic(), 0
    while rclpy.ok(context=context):
        now = time.monotonic()
        if now - last_control >= 1:
            last_control = now
            desired = json.loads(control.read_text())['topics']
            targets = {name: (message_type, subscription_qos(node, name))
                       for name, message_type in desired.items()}
            for name in list(requested):
                target = targets.get(name)
                signature = (target[0], target[1].reliability) if target else None
                if signature != requested[name]:
                    if name in subscriptions:
                        node.destroy_subscription(subscriptions.pop(name))
                    windows.pop(name, None)
                    requested.pop(name)
            for name, (message_type, qos) in targets.items():
                if name in requested:
                    continue
                requested[name] = (message_type, qos.reliability)
                try:
                    window = FrequencyWindow()
                    subscriptions[name] = node.create_subscription(
                        get_message(message_type), name, window.received, qos, raw=True)
                    windows[name] = window
                except Exception as exc:
                    emit({"topic": name, "error": str(exc)})
        try:
            executor.spin_once(timeout_sec=.05)
        except rclpy.executors.ExternalShutdownException:
            break
        now = time.monotonic()
        if now - last_report >= 1:
            for name, window in windows.items():
                emit({"topic": name, "subscription_reliability": requested[name][1].name,
                      **window.sample(now)})
            last_report = now
    return 0


def emit(value):
    print("@@RESULT@@" + json.dumps(value, ensure_ascii=False), flush=True)


def endpoint_qos(node, topic):
    endpoints = []
    for direction, getter in (('发布', node.get_publishers_info_by_topic),
                              ('订阅', node.get_subscriptions_info_by_topic)):
        for endpoint in getter(topic):
            qos = endpoint.qos_profile
            endpoints.append({
                'direction': direction, 'node': endpoint.node_namespace.rstrip('/') + '/' + endpoint.node_name,
                'type': endpoint.topic_type, 'reliability': qos.reliability.name,
                'history': qos.history.name, 'depth': qos.depth, 'durability': qos.durability.name,
                'deadline': qos.deadline.nanoseconds, 'lifespan': qos.lifespan.nanoseconds,
                'liveliness': qos.liveliness.name, 'lease': qos.liveliness_lease_duration.nanoseconds,
            })
    return endpoints


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["topics", "hz", "watch", "stamps", "qos"])
    parser.add_argument("--control")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--topic", default="")
    args = parser.parse_args()
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from rosidl_runtime_py.utilities import get_message
    context = rclpy.context.Context()
    rclpy.init(context=context)
    node = rclpy.create_node(f"recorder_probe_{__import__('os').getpid()}", context=context,
                             enable_rosout=False, start_parameter_services=False)
    executor = rclpy.executors.SingleThreadedExecutor(context=context)
    executor.add_node(node)
    try:
        if args.mode == 'watch':
            return watch_topics(node, executor, context, Path(args.control))
        if args.mode == 'stamps':
            if __package__:
                from .stamp_probe import watch_stamps
            else:
                from stamp_probe import watch_stamps
            return watch_stamps(node, executor, context, Path(args.control), emit)
        until = time.monotonic() + 1.2
        while time.monotonic() < until:
            executor.spin_once(timeout_sec=0.1)
        if args.mode == 'qos':
            from rclpy.utilities import get_rmw_implementation_identifier
            names = sorted(name for name, _ in node.get_topic_names_and_types())
            preferred = next((name for name in names if name not in ('/rosout', '/parameter_events')), '')
            topic = args.topic or ('/iv_points' if '/iv_points' in names else preferred)
            emit({'rmw': get_rmw_implementation_identifier(), 'names': names, 'topic': topic,
                  'endpoints': endpoint_qos(node, topic) if topic in names else []})
            return 0
        topics = []
        for name, types in sorted(node.get_topic_names_and_types()):
            if not args.hidden and any(part.startswith('_') for part in name.split('/')):
                continue
            all_pubs = node.get_publishers_info_by_topic(name)
            pubs = [p for p in all_pubs if not p.node_name.startswith('recorder_probe_')]
            if all_pubs and not pubs:
                continue
            available, reason = True, ""
            for msg_type in types:
                try:
                    get_message(msg_type)
                except Exception as exc:
                    available, reason = False, str(exc)
            topics.append({"name": name, "types": types, "publishers": len(pubs),
                           "available": available, "reason": reason,
                           "qos": ", ".join(sorted({p.qos_profile.reliability.name for p in pubs}))})
        if args.mode == "topics":
            emit({"topics": topics})
            return 0
        topic = next((t for t in topics if t['name'] == args.topic), None)
        if not topic or len(topic['types']) != 1:
            raise ValueError("未发现话题，或话题存在多种消息类型")
        if not topic['available']:
            raise ValueError("消息类型未加载：" + topic['reason'])
        window = FrequencyWindow()
        node.create_subscription(get_message(topic['types'][0]), args.topic, window.received,
                                 subscription_qos(node, args.topic), raw=True)
        last_report = time.monotonic()
        while rclpy.ok(context=context):
            try:
                executor.spin_once(timeout_sec=0.1)
            except rclpy.executors.ExternalShutdownException:
                break
            now = time.monotonic()
            if now - last_report < 1:
                continue
            emit({"topic": args.topic, **window.sample(now)})
            last_report = now
        return 0
    finally:
        executor.shutdown()
        node.destroy_node()
        if context.ok():
            rclpy.shutdown(context=context)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        emit({"error": str(exc)})
        sys.exit(1)
