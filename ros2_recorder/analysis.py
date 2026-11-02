"""Read-only bag inspection and bounded streaming statistics, also used as a worker.

Run this file with the selected ROS Python. Header analysis deserializes messages
without creating ROS nodes. All output files live in a caller-owned folder.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import time

import yaml

if __package__:
    from .header_stamp import HeaderStampReader
else:
    from header_stamp import HeaderStampReader

NS = 1_000_000_000
MAX_BINS = 600
MAX_PREVIEW = 500
CSV_FIELDS = ['topic', 'kind', 'timestamp_ns', 'previous_timestamp_ns',
              'relative_seconds', 'interval_ms', 'threshold_ms', 'time_source', 'record_timestamp_ns']


def source_files(raw_path):
    """Resolve only a complete metadata-backed directory or a single bag file."""
    path = Path(raw_path).expanduser().resolve()
    info = None
    warnings = []
    metadata = path / 'metadata.yaml'
    if path.is_dir() and metadata.is_file():
        if metadata.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('metadata.yaml 超过 16 MiB，无法读取。')
        data = yaml.safe_load(metadata.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('rosbag2_bagfile_information'), dict):
            raise ValueError('metadata.yaml 不是有效的 rosbag2 元数据。')
        info = data['rosbag2_bagfile_information']
        names = info.get('relative_file_paths', [])
        if not isinstance(names, list) or not names:
            raise ValueError('元数据中没有数据文件列表，无法确认录制是否完整。')
        files = []
        for name in names:
            file = (path / str(name)).resolve()
            if path not in file.parents or not file.is_file():
                raise ValueError(f'分包文件缺失或位于 bag 目录以外：{name}')
            files.append(file)
        if len(set(files)) != len(files):
            raise ValueError('元数据中的分包文件重复。')
        storage = str(info.get('storage_identifier', ''))
    elif path.is_dir():
        files = sorted([*path.glob('*.db3'), *path.glob('*.mcap')])
        if len(files) != 1:
            raise ValueError('目录缺少 metadata.yaml。请打开单个 .db3 / .mcap 文件；多分包目录需要完整元数据。')
        path = files[0].resolve()
        storage = 'sqlite3' if path.suffix == '.db3' else 'mcap'
        warnings.append('目录缺少元数据，本次仅检查找到的单个文件，无法确认是否缺少其他分包。')
    elif path.is_file() and path.suffix.lower() in ('.db3', '.mcap'):
        files = [path]
        storage = 'sqlite3' if path.suffix.lower() == '.db3' else 'mcap'
        warnings.append('当前打开单个存储文件，统计不包含同目录其他分包。')
    else:
        raise ValueError('请选择 rosbag2 目录，或 .db3 / .mcap 文件。')
    if storage not in ('sqlite3', 'mcap'):
        raise ValueError(f'首版数据体检暂不支持此存储格式：{storage or "未知"}')
    tracked = [*files, *([metadata] if info is not None else [])]
    # Read-only SQLite readers may create empty WAL sidecars. Only a nonempty
    # WAL indicates database content outside the main file; do not reject a bag
    # merely because another reader has opened it.
    for file in files:
        wal = Path(str(file) + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('检测到非空 SQLite WAL 文件，请先停止录制并正常保存后再分析。')
    return path, files, storage, info, tracked, warnings


def fingerprint(files):
    return [{'path': str(p), 'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
            for p in files]


def open_reader(path, storage):
    try:
        import rosbag2_py
    except ImportError as exc:
        raise RuntimeError('当前 Python 无法加载 rosbag2_py，请在“消息环境”选择匹配的 ROS 2 环境和 Python。') from exc
    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(rosbag2_py.StorageOptions(uri=str(path), storage_id=storage),
                    rosbag2_py.ConverterOptions('', ''))
    except Exception as exc:
        raise RuntimeError(f'无法读取 {storage} 数据，请检查 bag 是否已保存及当前 ROS 环境的存储插件：{exc}') from exc
    return reader


def reader_metadata(reader, path, storage):
    import rosbag2_py
    if hasattr(reader, 'get_metadata'):
        return reader.get_metadata()
    return rosbag2_py.Info().read_metadata(str(path), storage)


def inspect_bag(raw_path):
    path, files, storage, info, tracked, warnings = source_files(raw_path)
    before = fingerprint(tracked)
    compression = str((info or {}).get('compression_mode', '')).lower()
    scan_supported = compression in ('', 'none')
    if not scan_supported:
        warnings.append('首版暂不扫描 rosbag 外层文件/消息压缩。请在其他目录解压或转换后分析；MCAP 内部块压缩不受此限制。')
    if info is not None:
        topics = []
        for item in info.get('topics_with_message_count', []):
            topic = item['topic_metadata']
            topics.append({'name': str(topic['name']), 'type': str(topic['type']),
                           'count': int(item.get('message_count', 0))})
        count = int(info.get('message_count', 0))
        start = int(info.get('starting_time', {}).get('nanoseconds_since_epoch', 0))
        duration = int(info.get('duration', {}).get('nanoseconds', 0))
    else:
        reader = open_reader(path, storage)
        metadata = reader_metadata(reader, path, storage)
        topics = [{'name': t.topic_metadata.name, 'type': t.topic_metadata.type,
                   'count': int(t.message_count)} for t in metadata.topics_with_message_count]
        count = int(metadata.message_count)
        # Releases expose either rclpy Time/Duration or Python datetime/timedelta.
        # Use integer nanoseconds where provided and correct the origin from the
        # first serialized message on older bindings.
        start_value, duration_value = metadata.starting_time, metadata.duration
        start = int(start_value.nanoseconds) if hasattr(start_value, 'nanoseconds') else int(start_value.timestamp() * NS)
        duration = int(duration_value.nanoseconds) if hasattr(duration_value, 'nanoseconds') else (
            (duration_value.days * 86400 + duration_value.seconds) * NS + duration_value.microseconds * 1000)
        if reader.has_next():
            start = int(reader.read_next()[2])
        del reader
    if count < 0 or duration < 0 or any(t['count'] < 0 for t in topics):
        raise ValueError('元数据中的计数或时长无效。')
    if len({t['name'] for t in topics}) != len(topics):
        raise ValueError('同名话题存在多个元数据项，首版暂不支持类型冲突的数据。')
    if before != fingerprint(tracked):
        raise ValueError('读取期间 bag 文件发生变化，请停止录制后重新打开。')
    return {'path': str(path), 'storage': storage, 'files': [str(p) for p in files],
            'fingerprint': before, 'size_bytes': sum(p.stat().st_size for p in files),
            'message_count': count, 'start_ns': start, 'duration_ns': duration,
            'topics': topics, 'warnings': warnings, 'scan_supported': scan_supported,
            'compression_mode': compression, 'time_source': 'bag_record_timestamp'}


class TopicStatistics:
    """Constant-space interval moments plus bounded time buckets."""
    def __init__(self, topic, origin_ns, duration_ns, settings):
        self.topic = topic
        self.origin = origin_ns
        self.expected_hz = float(settings.get('expected_hz', 0))
        self.gap_ms = float(settings.get('gap_ms', 200))
        if not math.isfinite(self.expected_hz) or not math.isfinite(self.gap_ms) or self.expected_hz < 0 or self.gap_ms <= 0:
            raise ValueError('期望频率必须非负，间断阈值必须大于 0。')
        self.gap_ns = round(self.gap_ms * 1_000_000)
        self.detect_gaps = bool(settings.get('detect_gaps', True))
        self.count = self.positive_count = self.duplicate_count = self.reverse_count = self.zero_count = self.gaps = 0
        self.first = self.last = self.previous = None
        self.mean = self.m2 = 0.0
        self.minimum = self.maximum = None
        self.width = max(NS, math.ceil(max(0, duration_ns) / (MAX_BINS * NS)) * NS)
        self.bins = {}
        self.low_bin = self.high_bin = None
        self.anomaly_count = 0
        self.interval_min = self.interval_max = None

    def add(self, stamp):
        stamp = int(stamp)
        if self.origin is None:
            self.origin = stamp // NS * NS
        self.count += 1
        self.first = stamp if self.first is None else min(self.first, stamp)
        self.last = stamp if self.last is None else max(self.last, stamp)
        interval = None if self.previous is None else stamp - self.previous
        anomalies = []
        if stamp == 0:
            self.zero_count += 1
            anomalies.append('zero_timestamp')
        if interval is not None:
            self.interval_min = interval if self.interval_min is None else min(self.interval_min, interval)
            self.interval_max = interval if self.interval_max is None else max(self.interval_max, interval)
            if interval > 0:
                self.positive_count += 1
                delta = interval - self.mean
                self.mean += delta / self.positive_count
                self.m2 += delta * (interval - self.mean)
                self.minimum = interval if self.minimum is None else min(self.minimum, interval)
                self.maximum = interval if self.maximum is None else max(self.maximum, interval)
                if self.detect_gaps and interval > self.gap_ns:
                    self.gaps += 1
                    anomalies.append('gap')
            elif interval == 0:
                self.duplicate_count += 1
                anomalies.append('duplicate_timestamp')
            else:
                self.reverse_count += 1
                anomalies.append('read_order_reversal')
        index = (stamp - self.origin) // self.width
        self._bin(index, interval)
        self.low_bin = index if self.low_bin is None else min(self.low_bin, index)
        self.high_bin = index if self.high_bin is None else max(self.high_bin, index)
        # Include empty buckets in the bound; stale metadata cannot grow memory.
        while self.high_bin - self.low_bin >= MAX_BINS:
            old = self.bins
            self.bins = {}
            self.width *= 2
            for key, value in old.items():
                self._merge_bin(key // 2, value)
            self.low_bin //= 2
            self.high_bin //= 2
        events = [{'topic': self.topic['name'], 'kind': kind, 'timestamp_ns': stamp,
                   'previous_timestamp_ns': self.previous,
                   'relative_seconds': (stamp - self.origin) / NS,
                   'interval_ms': None if interval is None else interval / 1_000_000,
                   'threshold_ms': self.gap_ms if kind == 'gap' else None}
                  for kind in anomalies]
        self.anomaly_count += len(events)
        self.previous = stamp
        return events

    def _merge_bin(self, key, value):
        entry = self.bins.setdefault(key, [0, 0, 0, 0, None, None])
        for i in range(3):
            entry[i] += value[i]
        entry[3] = max(entry[3], value[3])
        if value[4] is not None:
            entry[4] = value[4] if entry[4] is None else min(entry[4], value[4])
            entry[5] = value[5] if entry[5] is None else max(entry[5], value[5])

    def _bin(self, key, interval):
        positive = interval is not None and interval > 0
        self._merge_bin(key, [1, int(positive), interval if positive else 0,
                              interval if positive else 0, interval, interval])

    def result(self, recorded_count=None):
        duration = self.last - self.first if self.count else 0
        hz = (self.count - 1) * NS / duration if self.count >= 2 and duration > 0 else None
        warnings = []
        count = self.count if recorded_count is None else recorded_count
        if count != self.topic['count']:
            warnings.append(f'元数据计数 {self.topic["count"]} 与实际扫描 {count} 不一致。')
        if self.expected_hz and hz is not None and abs(hz / self.expected_hz - 1) > .1:
            warnings.append(f'平均频率偏离期望 {self.expected_hz:g} Hz 超过 10%。')
        points = []
        if self.bins:
            for key in range(min(self.bins), max(self.bins) + 1):
                samples, n, total, maximum, minimum_all, maximum_all = self.bins.get(key, [0, 0, 0, 0, None, None])
                points.append({'seconds': key * self.width / NS, 'count': samples,
                               'frequency_hz': n * NS / total if total else None,
                               'mean_interval_ms': total / n / 1_000_000 if n else None,
                               'max_positive_interval_ms': maximum / 1_000_000 if n else None,
                               'min_interval_ms': None if minimum_all is None else minimum_all / 1_000_000,
                               'max_interval_ms': None if maximum_all is None else maximum_all / 1_000_000})
        return {**self.topic, 'count': count, 'valid_timestamp_count': self.count, 'metadata_count': self.topic['count'],
                'first_timestamp_ns': self.first, 'last_timestamp_ns': self.last,
                'duration_seconds': duration / NS, 'average_hz': hz,
                'interval_hz': NS / self.mean if self.positive_count else None,
                'mean_interval_ms': self.mean / 1_000_000 if self.positive_count else None,
                'std_interval_ms': math.sqrt(max(0, self.m2 / self.positive_count)) / 1_000_000 if self.positive_count else None,
                'min_interval_ms': None if self.interval_min is None else self.interval_min / 1_000_000,
                'max_interval_ms': None if self.interval_max is None else self.interval_max / 1_000_000,
                'min_positive_interval_ms': None if self.minimum is None else self.minimum / 1_000_000,
                'positive_intervals': self.positive_count, 'duplicate_timestamps': self.duplicate_count,
                'read_order_reversals': self.reverse_count, 'zero_timestamps': self.zero_count,
                'gap_count': self.gaps, 'anomaly_count': self.anomaly_count,
                'settings': {'expected_hz': self.expected_hz, 'gap_ms': self.gap_ms, 'detect_gaps': self.detect_gaps},
                'bucket_seconds': self.width / NS, 'series': points, 'warnings': warnings}


def scan_bag(request, output_dir, progress=lambda _: None):
    dataset = inspect_bag(request['path'])
    if dataset['fingerprint'] != request['fingerprint']:
        raise ValueError('bag 已变化，请重新打开后再分析。')
    if not dataset['scan_supported']:
        raise ValueError(dataset['warnings'][-1])
    settings = request.get('topics', {})
    available = {t['name']: t for t in dataset['topics']}
    if not settings or not set(settings) <= available.keys():
        raise ValueError('请至少选择一个有效话题。')
    time_source = request.get('time_source', 'header_stamp')
    if time_source not in ('header_stamp', 'bag_record_timestamp'):
        raise ValueError('未知的分析时间来源。')
    header_mode = time_source == 'header_stamp'
    origin = None if header_mode else dataset['start_ns']
    stats = {name: TopicStatistics(available[name], origin, dataset['duration_ns'],
                                  dict(value, detect_gaps=not header_mode or 'gap_ms' in value))
             for name, value in settings.items()}
    classes, problems = {}, {}
    counts = {name: 0 for name in settings}
    invalid = {name: 0 for name in settings}
    decode_errors = {name: 0 for name in settings}
    if header_mode:
        from rosidl_runtime_py.utilities import get_message
        for name in settings:
            try:
                cls = get_message(available[name]['type'])
                sample = cls()
                if not hasattr(getattr(sample, 'header', None), 'stamp'):
                    problems[name] = '消息不含 header.stamp，仅提供消息总数。'
                else:
                    classes[name] = HeaderStampReader(cls)
            except Exception as exc:
                problems[name] = f'消息类型未加载：{available[name]["type"]}。请在“消息环境”加载对应类型。({exc})'
    reader = open_reader(dataset['path'], dataset['storage'])
    import rosbag2_py
    if hasattr(reader, 'set_filter'):
        reader.set_filter(rosbag2_py.StorageFilter(topics=list(settings)))
    total = sum(available[name]['count'] for name in settings)
    processed = anomaly_count = 0
    preview = []
    last_progress = time.monotonic()
    output_dir = Path(output_dir)
    with closing(sqlite3.connect(str(output_dir / 'timestamps.sqlite'))) as index:
        index.execute('CREATE TABLE stamps(topic TEXT NOT NULL, stamp_ns INTEGER NOT NULL, record_ns INTEGER NOT NULL)')
        batch = []
        with (output_dir / 'anomalies.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            while reader.has_next():
                topic, payload, stamp = reader.read_next()
                if topic not in stats:
                    continue
                counts[topic] += 1
                record_stamp = stamp
                if header_mode:
                    if topic not in classes:
                        stamp = None
                    else:
                        try:
                            header = classes[topic].read(payload)
                            if not 0 <= header.nanosec < NS or header.sec * NS + header.nanosec == 0:
                                invalid[topic] += 1
                                stamp = None
                            else:
                                stamp = int(header.sec) * NS + int(header.nanosec)
                        except Exception:
                            decode_errors[topic] += 1
                            stamp = None
                del payload
                events = []
                if stamp is not None:
                    if origin is None:
                        origin = stamp // NS * NS
                        for state in stats.values():
                            state.origin = origin
                    events = stats[topic].add(stamp)
                    for event in events:
                        event.update(time_source=time_source, record_timestamp_ns=record_stamp)
                    batch.append((topic, stamp, record_stamp))
                    if len(batch) >= 4096:
                        index.executemany('INSERT INTO stamps VALUES(?,?,?)', batch)
                        batch.clear()
                writer.writerows(events)
                anomaly_count += len(events)
                if len(preview) < MAX_PREVIEW:
                    preview.extend(events[:MAX_PREVIEW - len(preview)])
                processed += 1
                now = time.monotonic()
                if now - last_progress >= .25:
                    progress({'processed': processed, 'total': total})
                    last_progress = now
        index.executemany('INSERT INTO stamps VALUES(?,?,?)', batch)
        index.execute('CREATE INDEX stamps_topic_time ON stamps(topic, stamp_ns)')
        index.commit()
    del reader
    current = inspect_bag(dataset['path'])
    if current['fingerprint'] != dataset['fingerprint']:
        raise ValueError('扫描期间 bag 文件发生变化，结果已作废，请停止录制后重新分析。')
    topics = []
    for name, state in stats.items():
        result = state.result(counts[name])
        result.update(analysis_error=problems.get(name, ''), invalid_header_count=invalid[name], decode_error_count=decode_errors[name])
        extractor = classes.get(name)
        result['header_prefix_reads'] = extractor.prefix_count if extractor else 0
        result['full_message_decodes'] = extractor.full_count if extractor else 0
        if invalid[name]:
            result['warnings'].append(f'{invalid[name]} 条消息的 header 时间戳为零或无效，未用于时间分析。')
        if decode_errors[name]:
            result['warnings'].append(f'{decode_errors[name]} 条消息无法解码，未用于时间分析。')
        topics.append(result)
    result = {'schema_version': 2, 'created_at': datetime.now().astimezone().isoformat(),
              'dataset': dataset, 'time_source': time_source, 'analysis_origin_ns': origin,
              'read_order': 'storage_reader_order', 'message_count': processed,
              'anomaly_count': anomaly_count, 'anomaly_preview': preview,
              'anomaly_preview_limit': MAX_PREVIEW, 'topics': topics,
              'integrity': '时间间断不等于已证实的丢帧；读取顺序不证明原始发布或写入顺序。',
              'interval_statistics': '频率为平均正间隔的倒数；最大/最小间隔包含零和负间隔。无效 header 不参与时间分析。'}
    progress({'processed': processed, 'total': total})
    return result


def query_timestamps(index_path, topics, start_ns, end_ns, cancelled=None, limit=1600):
    """Read raw timestamp ranges at display resolution; zooming reveals detail."""
    if start_ns > end_ns:
        return {}
    connection = sqlite3.connect(Path(index_path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if cancelled is not None:
            connection.set_progress_handler(lambda: int(cancelled.is_set()), 5000)
        width = max(1, (end_ns - start_ns + limit - 1) // limit)
        result = {}
        for topic in topics:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError('时间戳查询已取消。')
            rows = connection.execute(
                'SELECT MIN(stamp_ns), MAX(stamp_ns), COUNT(*) FROM stamps '
                'WHERE topic=? AND stamp_ns>=? AND stamp_ns<=? '
                'GROUP BY CAST((stamp_ns-?)/? AS INTEGER) ORDER BY MIN(stamp_ns)',
                (topic, start_ns, end_ns, start_ns, width)).fetchall()
            result[topic] = {'points': [{'first_ns': a, 'last_ns': b, 'count': n} for a, b, n in rows],
                             'total': sum(r[2] for r in rows)}
        return result
    finally:
        connection.close()


def emit(event):
    print('@@ANALYSIS@@' + json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'scan'])
    parser.add_argument('request')
    parser.add_argument('output_dir')
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8'))
        result = inspect_bag(request['path']) if args.action == 'inspect' else scan_bag(
            request, args.output_dir, lambda data: emit({'progress': data}))
        destination = Path(args.output_dir) / 'result.json'
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        emit({'completed': True})
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        emit({'error': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
