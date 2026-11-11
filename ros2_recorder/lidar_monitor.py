"""Record point-cloud delivery, network counters, and timing logs on one timeline."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import subprocess
from threading import Thread
import time

from .header_stamp import HeaderStampReader, stamp_ns


DEFAULT_STREAMS = [('/zvlidar_points_xyzirt', 'enp4s0', 2368), ('/iv_points', 'enp5s0', 8010)]
MODES = ('best_effort', 'reliable')
PAIR_WAIT_NS = 2_000_000_000
RX_COUNTERS = ('rx_bytes', 'rx_packets', 'rx_errors', 'rx_dropped', 'rx_crc_errors',
               'rx_missed_errors', 'rx_fifo_errors')


def stream_arg(text):
    try:
        topic, interface, port = text.split(',')
        port = int(port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('请填写 话题,网口,UDP端口') from exc
    if not topic.startswith('/') or not interface or '/' in interface or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('请填写有效的绝对话题名、网口和 UDP 端口')
    return topic, interface, port


def network_snapshot(interfaces, ports):
    network = {}
    for interface in interfaces:
        path = Path('/sys/class/net') / interface
        try:
            stats = {key: int((path / 'statistics' / key).read_text()) for key in RX_COUNTERS}
            stats['carrier_changes'] = int((path / 'carrier_changes').read_text())
            stats['operstate'] = (path / 'operstate').read_text().strip()
            network[interface] = stats
        except OSError as exc:
            network[interface] = {'error': str(exc)}
    sockets = {}
    try:
        lines = Path('/proc/net/udp').read_text().splitlines()[1:]
        for line in lines:
            fields = line.split()
            port = int(fields[1].split(':')[1], 16)
            if port in ports:
                sockets[f'{port}/{fields[9]}'] = {'queue_bytes': int(fields[4].split(':')[1], 16),
                                                'drops': int(fields[-1])}
    except OSError as exc:
        sockets['error'] = str(exc)
    return network, sockets


def timing_journal(interfaces, lines):
    command = ['journalctl', '--follow', '--lines=0', '--output=short-iso-precise', '--no-pager']
    for interface in interfaces:
        for prefix in ('ros-recorder-timing', 'ptp4l', 'phc2sys'):
            command.extend(('--unit', f'{prefix}-{interface}.service'))
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace')
    except OSError as exc:
        lines.put(f'无法读取 PTP 日志：{exc}')
        return None

    def follow():
        with process.stdout:
            for line in process.stdout:
                lines.put(line.rstrip())

    Thread(target=follow, daemon=True).start()
    return process


class Monitor:
    def __init__(self, output, streams, gap_ms, stall_ms):
        self.output = output
        self.gap_ms = gap_ms
        self.stall_ms = stall_ms
        self.keys = [(topic, mode) for topic, _, _ in streams for mode in MODES]
        self.last = {}
        self.stalled = set()
        self.pending = {}
        self.counts = {key: Counter() for key in self.keys}
        self.longest = Counter()
        self.ready = time.monotonic_ns() + PAIR_WAIT_NS

    def emit(self, event, **fields):
        row = {'time': datetime.now().astimezone().isoformat(timespec='milliseconds'), 'event': event, **fields}
        self.output.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')

    def frame(self, key, now, wall, header, info, size):
        topic, mode = key
        source = info.get('source_timestamp', 0)
        received = info.get('received_timestamp', 0)
        self.emit('frame', topic=topic, mode=mode, observed_ns=wall, header_ns=header,
                  dds_source_ns=source, dds_receive_ns=received, bytes=size)
        if now < self.ready:
            return
        previous = self.last.get(key)
        self.last[key] = now, header, source
        self.counts[key]['frames'] += 1
        if previous:
            elapsed = (now - previous[0]) / 1e6
            self.longest[key] = max(self.longest[key], elapsed)
            if elapsed > self.gap_ms:
                self.counts[key]['gaps'] += 1
                self.emit('gap', topic=topic, mode=mode, gap_ms=round(elapsed, 3),
                          header_gap_ms=(header - previous[1]) / 1e6 if header and previous[1] else None,
                          dds_source_gap_ms=(source - previous[2]) / 1e6 if source and previous[2] else None,
                          dds_receive_to_take_ms=(wall - received) / 1e6 if received else None)
        if key in self.stalled:
            self.stalled.remove(key)
            self.emit('resumed', topic=topic, mode=mode)
        if not source:
            self.counts[key]['no_dds_source'] += 1
            return
        sample = topic, source
        seen = self.pending.get(sample)
        if seen is None:
            self.pending[sample] = now, mode
        elif seen[1] != mode:
            del self.pending[sample]

    def check(self, now):
        if now < self.ready:
            return
        while self.pending:
            sample = next(iter(self.pending))
            first_seen, mode = self.pending[sample]
            if now - first_seen < PAIR_WAIT_NS:
                break
            topic, source = sample
            del self.pending[sample]
            self.counts[topic, mode]['only_observed_here'] += 1
            self.emit('qos_difference', topic=topic, dds_source_ns=source, observed_in=mode, wait_ms=2000)
        for key in self.keys:
            last = self.last[key][0] if key in self.last else self.ready
            silence = (now - last) / 1e6
            if silence >= self.stall_ms and key not in self.stalled:
                self.stalled.add(key)
                topic, mode = key
                self.counts[key]['stalls'] += 1
                self.emit('stall', topic=topic, mode=mode, silent_ms=round(silence, 3))
                print(f'{topic} {mode} 已 {silence:.0f} ms 未收到帧', flush=True)

    def summary(self):
        return [{'topic': topic, 'mode': mode, 'frames': self.counts[topic, mode]['frames'],
                 'gaps': self.counts[topic, mode]['gaps'], 'stalls': self.counts[topic, mode]['stalls'],
                 'only_observed_here': self.counts[topic, mode]['only_observed_here'],
                 'no_dds_source': self.counts[topic, mode]['no_dds_source'],
                 'longest_gap_ms': round(self.longest[topic, mode], 3)} for topic, mode in self.keys]


def run(args, streams, output):
    import rclpy
    from rcl_interfaces.msg import Log
    from rclpy.logging import LoggingSeverity
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import PointCloud2

    monitor = Monitor(output, streams, args.gap_ms, args.stall_ms)
    interfaces = sorted({interface for _, interface, _ in streams})
    ports = {port for _, _, port in streams}
    topics = [topic for topic, _, _ in streams]
    rclpy.init()
    node = None
    journal = None
    status = 'failed'
    try:
        node = rclpy.create_node(f'ros_recorder_lidar_monitor_{os.getpid()}', enable_rosout=False)
        reader = HeaderStampReader(PointCloud2)
        qos = {'best_effort': ReliabilityPolicy.BEST_EFFORT, 'reliable': ReliabilityPolicy.RELIABLE}
        subscribers = {key: node.create_subscription(PointCloud2, key[0], lambda _: None,
                                                       QoSProfile(depth=10, reliability=qos[key[1]]), raw=True)
                       for key in monitor.keys}
        rosout = node.create_subscription(Log, '/rosout', lambda _: None, 100)
        lines = SimpleQueue()
        journal = timing_journal(interfaces, lines)
        monitor.emit('start', streams=streams, gap_ms=args.gap_ms, stall_ms=args.stall_ms,
                     depth=10, domain=os.environ.get('ROS_DOMAIN_ID', '0'),
                     rmw=rclpy.get_rmw_implementation_identifier(), warmup_ms=2000)
        started = time.monotonic_ns()
        deadline = started + int(args.duration * 1e9) if args.duration else None
        next_sample = started
        next_summary = started + 10_000_000_000
        previous_clock = None
        previous_loop = started
        while rclpy.ok():
            now = time.monotonic_ns()
            if deadline is not None and now >= deadline:
                break
            if now - previous_loop > 50_000_000:
                monitor.emit('monitor_loop_delay', elapsed_ms=(now - previous_loop) / 1e6)
            previous_loop = now
            # Humble's executor discards DDS message metadata before invoking callbacks.
            # Take directly so both subscriptions can be matched by publisher timestamp.
            for key, sub in subscribers.items():
                taking = time.monotonic_ns()
                with sub.handle:
                    message = sub.handle.take_message(sub.msg_type, True)
                observed = time.monotonic_ns()
                if observed - taking > 50_000_000:
                    monitor.emit('take_delay', topic=key[0], mode=key[1], elapsed_ms=(observed - taking) / 1e6)
                if message is None:
                    continue
                wall = time.time_ns()
                payload, info = message
                try:
                    header = stamp_ns(reader.read(payload))
                except (ValueError, RuntimeError) as exc:
                    header = None
                    monitor.emit('invalid_header', topic=key[0], mode=key[1], detail=str(exc))
                monitor.frame(key, observed, wall, header, info, len(payload))

            for _ in range(10):
                with rosout.handle:
                    message = rosout.handle.take_message(rosout.msg_type, False)
                if message is None:
                    break
                log, _ = message
                if log.level >= LoggingSeverity.WARN:
                    monitor.emit('rosout', node=log.name, level=log.level, message=log.msg,
                                 log_ns=log.stamp.sec * 10**9 + log.stamp.nanosec)

            if now >= next_sample:
                next_sample = now + 1_000_000_000
                network, sockets = network_snapshot(interfaces, ports)
                clock = time.time_ns() - time.monotonic_ns()
                if previous_clock is not None and abs(clock - previous_clock) > 10_000_000:
                    monitor.emit('system_clock_step', delta_ms=round((clock - previous_clock) / 1e6, 3))
                previous_clock = clock
                monitor.emit('network', interfaces=network, udp_sockets=sockets,
                             publishers={topic: node.count_publishers(topic) for topic in topics},
                             load_1m=os.getloadavg()[0])
                while True:
                    try:
                        monitor.emit('timing_log', message=lines.get_nowait())
                    except Empty:
                        break
            monitor.check(now)
            if now >= next_summary:
                next_summary = now + 10_000_000_000
                report = monitor.summary()
                monitor.emit('summary', streams=report)
                for entry in report:
                    print('{topic} {mode} 收到 {frames} 帧 · 空档 {gaps} · 单侧收到 {only_observed_here}'
                          .format(**entry), flush=True)
            time.sleep(.001)
        status = 'finished'
    except KeyboardInterrupt:
        status = 'interrupted'
    finally:
        if journal is not None and journal.poll() is None:
            journal.terminate()
            journal.wait()
        monitor.emit('end', status=status, streams=monitor.summary(), pending_comparisons=len(monitor.pending))
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return monitor.summary()


def main(argv=None):
    parser = argparse.ArgumentParser(description='实时对照两种 ROS 订阅模式并记录雷达收帧空档。')
    parser.add_argument('--stream', action='append', type=stream_arg, metavar='话题,网口,端口')
    parser.add_argument('--duration', type=float, default=0, help='运行秒数。0 表示持续到 Ctrl+C。')
    parser.add_argument('--gap-ms', type=float, default=150, help='记录相邻帧空档的阈值，默认 150 ms。')
    parser.add_argument('--stall-ms', type=float, default=500, help='无帧时提示的阈值，默认 500 ms。')
    parser.add_argument('--output', type=Path, help='JSONL 日志路径，默认在 ~/lidar-monitor-logs。')
    args = parser.parse_args(argv)
    streams = args.stream or DEFAULT_STREAMS
    if len({topic for topic, _, _ in streams}) != len(streams):
        parser.error('每个话题只填写一次')
    if (not all(math.isfinite(value) for value in (args.duration, args.gap_ms, args.stall_ms))
            or not 0 <= args.duration or not 0 < args.gap_ms <= args.stall_ms):
        parser.error('时长须大于等于 0，且 0 < 空档阈值 <= 无帧阈值')
    default_path = Path.home() / 'lidar-monitor-logs' / f'{datetime.now():%Y%m%d-%H%M%S-%f}.jsonl'
    path = (args.output or default_path).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8', buffering=1) as output:
            print(f'监测日志 {path}', flush=True)
            report = run(args, streams, output)
    except (ImportError, OSError) as exc:
        parser.exit(1, f'无法开始监测：{exc}\n')
    for entry in report:
        print('{topic} {mode} 共 {frames} 帧 · 最长空档 {longest_gap_ms:.1f} ms'
              ' · 单侧收到 {only_observed_here}'.format(**entry))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
