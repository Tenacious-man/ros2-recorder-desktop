"""Bounded latest-arrival header snapshots over native ROS raw subscriptions."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time

from .config import atomic_json
from .environment import probe_argv
from .processes import ProcessRunner


class LatestStampMonitor:
    """Reuse the recorder's independent ROS helper/process lifecycle."""
    def __init__(self, env, topics, *, on_snapshot, on_done):
        self.directory = tempfile.TemporaryDirectory(prefix='ros-latest-stamps-')
        self.control = Path(self.directory.name) / 'topics.json'
        self.topics = {}
        self.tokens = {}
        self.token_counter = 0
        self.generation = 0
        self.stopping = False
        self.update(topics)

        def line(raw):
            if raw.startswith('@@RESULT@@'):
                try:
                    value = json.loads(raw[10:])
                except ValueError:
                    return
                if 'error' in value or value.get('generation') == self.generation:
                    on_snapshot(value)

        def done(code):
            self.directory.cleanup()
            on_done(code)

        self.runner = ProcessRunner(probe_argv('stamps', '--control', str(self.control), env=env), env,
                                    line=line, done=done)

    def update(self, topics):
        topics = dict(topics)
        if self.generation and topics == self.topics:
            return
        tokens = {}
        for name, kind in topics.items():
            if self.topics.get(name) == kind:
                tokens[name] = self.tokens[name]
            else:
                self.token_counter += 1
                tokens[name] = self.token_counter
        self.topics = topics
        self.tokens = tokens
        self.generation += 1
        atomic_json(self.control, {'generation': self.generation,
                                  'topics': {name: {'type': kind, 'token': tokens[name]} for name, kind in topics.items()}})

    def start(self):
        self.runner.start()

    def stop(self, *, force=False):
        self.stopping = True
        self.runner.stop(force=force)

    def wait(self, timeout=None):
        return self.runner.wait(timeout)


def format_stamp(value):
    if value is None:
        return '—'
    sign = '-' if value < 0 else ''
    value = abs(value)
    return f'{sign}{value // 1_000_000_000}.{value % 1_000_000_000:09d}'


def latest_difference(sample, reference):
    """Compare last *arrivals*, never maxima, receipt clocks or matched frames."""
    if not sample or not reference or sample.get('status') != 'ok' or reference.get('status') != 'ok':
        return None
    return (sample['stamp_ns'] - reference['stamp_ns']) / 1_000_000


def sample_status(sample, *, running=True, now_ns=None):
    if not sample:
        return '等待消息' if running else '未监听'
    state = sample.get('status')
    if state != 'ok':
        return sample.get('error') or {'waiting': '等待消息', 'no_header': '消息没有 header.stamp',
                                     'invalid': '时间戳无效', 'decode_error': '时间戳解码失败',
                                     'unavailable': '消息类型未加载'}.get(state, '等待消息')
    if not running:
        return '已停止，保留最后消息'
    now_ns = time.monotonic_ns() if now_ns is None else now_ns
    age = max(0, now_ns - sample['received_ns']) / 1e9
    return f'{age:.1f} 秒未更新' if age > 2 else '接收中'
