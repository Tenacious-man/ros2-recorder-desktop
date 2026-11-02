"""Deterministic bag timing fixtures; no publishers or DDS participants needed."""
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from ros2_recorder.analysis import MAX_BINS, NS, TopicStatistics, inspect_bag, source_files
from ros2_recorder.analysis import query_timestamps
from ros2_recorder.environment import load_environment

WORKER = Path(__file__).resolve().parents[1] / 'ros2_recorder' / 'analysis.py'
T = 1_760_000_000 * NS + 123
WRITER = '''
import json, sys
import rosbag2_py
request=json.loads(sys.argv[1])
writer=rosbag2_py.SequentialWriter()
writer.open(rosbag2_py.StorageOptions(uri=request['path'],storage_id=request['storage']),rosbag2_py.ConverterOptions('',''))
for topic in ['/sensor', '/other', '/empty']:
    writer.create_topic(rosbag2_py.TopicMetadata(name=topic,type='uninstalled_test_msgs/msg/Sample',serialization_format='cdr'))
for topic,stamp in request['messages']:
    writer.write(topic,b'\\x00\\x01\\x00\\x00'+b'x'*80,stamp)
del writer
'''


@pytest.fixture(scope='module')
def ros_env():
    return load_environment()


def write_bag(path, storage, messages, env):
    result = subprocess.run([sys.executable, '-c', WRITER, json.dumps({
        'path': str(path), 'storage': storage, 'messages': messages})], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return path


HEADER_WRITER = '''
import json, sys
import rosbag2_py
from sensor_msgs.msg import Imu
from std_msgs.msg import String
from rclpy.serialization import serialize_message
request=json.loads(sys.argv[1])
writer=rosbag2_py.SequentialWriter()
writer.open(rosbag2_py.StorageOptions(uri=request['path'],storage_id=request['storage']),rosbag2_py.ConverterOptions('',''))
for topic,kind in [('/sensor','sensor_msgs/msg/Imu'),('/sensor2','sensor_msgs/msg/Imu'),('/empty','sensor_msgs/msg/Imu'),
                   ('/no_header','std_msgs/msg/String'),('/missing','uninstalled_test_msgs/msg/Sample')]:
    writer.create_topic(rosbag2_py.TopicMetadata(name=topic,type=kind,serialization_format='cdr'))
for topic,record_stamp,header_stamp in request['messages']:
    if topic=='/missing' or header_stamp == 'bad':
        payload=b'bad'
    elif topic=='/no_header':
        payload=serialize_message(String(data='no header'))
    else:
        message=Imu()
        message.header.stamp.sec=header_stamp//1000000000
        message.header.stamp.nanosec=header_stamp%1000000000
        payload=serialize_message(message)
    writer.write(topic,payload,record_stamp)
del writer
'''


def write_header_bag(path, storage, messages, env):
    result = subprocess.run([sys.executable, '-c', HEADER_WRITER, json.dumps({
        'path': str(path), 'storage': storage, 'messages': messages})], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return path


def worker(action, request, directory, env, success=True):
    request.setdefault('time_source', 'bag_record_timestamp')
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / 'request.json'
    request_path.write_text(json.dumps(request))
    result = subprocess.run([sys.executable, str(WORKER), action, str(request_path), str(directory)],
                            env=env, capture_output=True, text=True, timeout=30)
    events = [json.loads(line[len('@@ANALYSIS@@'):]) for line in result.stdout.splitlines() if line.startswith('@@ANALYSIS@@')]
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads((directory / 'result.json').read_text())
    assert result.returncode != 0
    return events[-1]['error']


@pytest.mark.parametrize('storage', ['sqlite3', 'mcap'])
def test_real_split_scan_unknown_message_type_and_gap_export(tmp_path, ros_env, storage):
    # A gap and duplicate straddle files. Another topic extends global duration,
    # so using bag-wide time to estimate this topic's rate would be incorrect.
    first = write_bag(tmp_path / 'part one', storage, [('/other', T - NS), ('/sensor', T),
                      ('/sensor', T + NS // 10), ('/sensor', T + NS // 5)], ros_env)
    second = write_bag(tmp_path / 'part two', storage, [('/sensor', T + 6 * NS // 10),
                       ('/sensor', T + 6 * NS // 10), ('/sensor', T + 7 * NS // 10)], ros_env)
    meta = yaml.safe_load((first / 'metadata.yaml').read_text())
    other_meta = yaml.safe_load((second / 'metadata.yaml').read_text())['rosbag2_bagfile_information']
    info = meta['rosbag2_bagfile_information']
    second_name = other_meta['relative_file_paths'][0]
    import shutil
    shutil.copyfile(second / second_name, first / second_name)
    info['relative_file_paths'].append(second_name)
    info['message_count'] = 7
    info['duration']['nanoseconds'] = 17 * NS // 10
    for topic in info['topics_with_message_count']:
        topic['message_count'] = {'/sensor': 6, '/other': 1, '/empty': 0}[topic['topic_metadata']['name']]
    info['files'] = info.get('files', []) + other_meta.get('files', [])
    (first / 'metadata.yaml').write_text(yaml.safe_dump(meta))
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in first.iterdir()}
    dataset = worker('inspect', {'path': str(first)}, tmp_path / 'inspect', ros_env)
    assert dataset['start_ns'] == T - NS
    report = worker('scan', {'path': str(first), 'fingerprint': dataset['fingerprint'],
                            'topics': {'/sensor': {'gap_ms': 200, 'expected_hz': 10}, '/empty': {}}},
                    tmp_path / 'scan', ros_env)
    sensor, empty = report['topics']
    assert sensor['count'] == 6
    assert sensor['duration_seconds'] == .7
    assert sensor['average_hz'] == pytest.approx(5 / .7)
    assert sensor['mean_interval_ms'] == 175
    assert sensor['std_interval_ms'] == pytest.approx(math.sqrt(16875))
    assert sensor['max_interval_ms'] == 400
    assert sensor['duplicate_timestamps'] == 1 and sensor['gap_count'] == 1
    assert sensor['warnings'] and sensor['positive_intervals'] == 4
    assert empty['count'] == 0 and empty['average_hz'] is None and empty['first_timestamp_ns'] is None
    with (tmp_path / 'scan' / 'anomalies.csv').open(encoding='utf-8-sig', newline='') as stream:
        events = list(csv.DictReader(stream))
    assert [e['kind'] for e in events] == ['gap', 'duplicate_timestamp']
    assert int(events[0]['timestamp_ns']) == T + 6 * NS // 10
    assert float(events[0]['relative_seconds']) == 1.6
    assert sum(p['count'] for p in sensor['series']) == 6
    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in first.iterdir()} == before


@pytest.mark.parametrize('storage', ['sqlite3', 'mcap'])
def test_standalone_files_without_metadata_and_single_message(tmp_path, ros_env, storage):
    bag = write_bag(tmp_path / '单条 空格', storage, [('/sensor', T)], ros_env)
    file = next(bag.glob('*.db3' if storage == 'sqlite3' else '*.mcap'))
    (bag / 'metadata.yaml').unlink()
    data = worker('inspect', {'path': str(bag)}, tmp_path / 'inspect', ros_env)
    assert data['path'] == str(file)
    assert data['start_ns'] == T
    report = worker('scan', {'path': str(file), 'fingerprint': data['fingerprint'], 'topics': {'/sensor': {}}}, tmp_path / 'scan', ros_env)
    topic = report['topics'][0]
    assert topic['count'] == 1 and topic['average_hz'] is None
    assert topic['max_interval_ms'] is None and topic['anomaly_count'] == 0
    assert not (bag / 'metadata.yaml').exists()


def test_changed_source_is_rejected_before_scan(tmp_path, ros_env):
    bag = write_bag(tmp_path / 'bag', 'sqlite3', [('/sensor', T)], ros_env)
    data = inspect_bag(bag)
    metadata = bag / 'metadata.yaml'
    metadata.write_text(metadata.read_text() + '\n')
    error = worker('scan', {'path': str(bag), 'fingerprint': data['fingerprint'], 'topics': {'/sensor': {}}}, tmp_path / 'scan', ros_env, False)
    assert '变化' in error
    assert not (tmp_path / 'scan' / 'result.json').exists()


def test_anomaly_preview_is_bounded_but_csv_is_complete(tmp_path, ros_env):
    bag = write_bag(tmp_path / 'bag', 'sqlite3', [('/sensor', T + i * NS) for i in range(2000)], ros_env)
    dataset = inspect_bag(bag)
    result = worker('scan', {'path': str(bag), 'fingerprint': dataset['fingerprint'],
                            'topics': {'/sensor': {'gap_ms': 200}}}, tmp_path / 'scan', ros_env)
    assert result['anomaly_count'] == 1999
    assert len(result['anomaly_preview']) == 500
    assert len(result['topics'][0]['series']) <= MAX_BINS
    with (tmp_path / 'scan' / 'anomalies.csv').open(encoding='utf-8-sig') as stream:
        assert len(list(csv.DictReader(stream))) == 1999


def test_unsafe_incomplete_metadata_and_compression_are_explicit(tmp_path):
    bag = tmp_path / 'bag'
    bag.mkdir()
    outside = tmp_path / 'outside.db3'
    outside.touch()
    meta = {'rosbag2_bagfile_information': {'storage_identifier': 'sqlite3', 'relative_file_paths': ['../outside.db3']}}
    metadata = bag / 'metadata.yaml'
    metadata.write_text(yaml.safe_dump(meta))
    with pytest.raises(ValueError, match='缺失或位于'):
        source_files(bag)
    meta['rosbag2_bagfile_information']['relative_file_paths'] = ['missing.db3']
    metadata.write_text(yaml.safe_dump(meta))
    with pytest.raises(ValueError, match='缺失'):
        source_files(bag)
    (bag / 'missing.db3').touch()
    meta['rosbag2_bagfile_information']['compression_mode'] = 'file'
    metadata.write_text(yaml.safe_dump(meta))
    data = inspect_bag(bag)
    assert not data['scan_supported'] and '压缩' in data['warnings'][-1]
    (bag / 'missing.db3-wal').touch()
    assert not inspect_bag(bag)['scan_supported']
    (bag / 'missing.db3-wal').write_bytes(b'uncheckpointed data')
    with pytest.raises(ValueError, match='WAL'):
        inspect_bag(bag)


def test_bounded_charts_preserve_counts_and_show_silent_periods():
    stats = TopicStatistics({'name': '/topic', 'type': 'unknown', 'count': 20001}, T, NS, {})
    for i in range(20000):
        stats.add(T + i * NS)
    # Metadata can be stale by many orders of magnitude; keep memory bounded.
    stats.add(T + 1_000_000 * NS)
    data = stats.result()
    assert len(stats.bins) <= MAX_BINS
    assert len(data['series']) <= MAX_BINS
    assert sum(p['count'] for p in data['series']) == 20001
    assert any(p['count'] == 0 for p in data['series'])


def test_zero_duplicate_reverse_and_nanosecond_precision():
    stats = TopicStatistics({'name': '/topic', 'count': 4}, T, NS, {'gap_ms': .01})
    for stamp in [T, T + 1, T + 1, T]:
        stats.add(stamp)
    result = stats.result()
    assert result['min_interval_ms'] == -.000001
    assert result['min_positive_interval_ms'] == .000001
    assert result['duplicate_timestamps'] == 1 and result['read_order_reversals'] == 1
    zero = TopicStatistics({'name': '/topic', 'count': 1}, 0, NS, {})
    assert zero.add(0)[0]['kind'] == 'zero_timestamp'
    assert zero.result()['zero_timestamps'] == 1


@pytest.mark.parametrize('storage', ['sqlite3', 'mcap'])
def test_header_analysis_uses_header_time_and_retains_all_messages(tmp_path, ros_env, storage):
    # Bag time is a steady 10 Hz; header time has 100, 100, 400, 0, -200 ms
    # intervals, a zero timestamp and an undecodable message.
    header_origin = 7 * NS + 123
    header_times = [header_origin + n for n in [0, NS // 10, NS // 5, 6 * NS // 10, 6 * NS // 10, 4 * NS // 10]]
    messages = [('/sensor', T + i * NS // 10, value) for i, value in enumerate(header_times + [0, 'bad'])]
    messages += [('/no_header', T + NS, 0), ('/missing', T + 2 * NS, 0)]
    bag = write_header_bag(tmp_path / 'headers', storage, messages, ros_env)
    dataset = inspect_bag(bag)
    report = worker('scan', {'path': str(bag), 'fingerprint': dataset['fingerprint'],
                            'time_source': 'header_stamp', 'topics': {t['name']: {} for t in dataset['topics']}}, tmp_path / 'scan', ros_env)
    topics = {t['name']: t for t in report['topics']}
    sensor = topics['/sensor']
    assert report['time_source'] == 'header_stamp' and report['analysis_origin_ns'] == 7 * NS
    assert report['message_count'] == 10
    assert sensor['count'] == 8 and sensor['valid_timestamp_count'] == 6
    assert sensor['invalid_header_count'] == 1 and sensor['decode_error_count'] == 1
    assert sensor['duration_seconds'] == .6
    assert sensor['mean_interval_ms'] == 200
    assert sensor['interval_hz'] == 5
    assert sensor['max_interval_ms'] == 400 and sensor['min_interval_ms'] == -200
    assert sensor['duplicate_timestamps'] == 1 and sensor['read_order_reversals'] == 1
    assert topics['/no_header']['count'] == 1 and topics['/no_header']['valid_timestamp_count'] == 0
    assert '不含 header' in topics['/no_header']['analysis_error']
    assert '类型未加载' in topics['/missing']['analysis_error']
    assert topics['/missing']['interval_hz'] is None
    points = query_timestamps(tmp_path / 'scan' / 'timestamps.sqlite', ['/sensor'], 7 * NS, 8 * NS)
    assert points['/sensor']['total'] == 6
    # Duplicate timestamps remain represented in the point's multiplicity.
    assert sum(p['count'] for p in points['/sensor']['points']) == 6
    zoom = query_timestamps(tmp_path / 'scan' / 'timestamps.sqlite', ['/sensor'], header_origin + 5 * NS // 10, header_origin + 7 * NS // 10)
    assert zoom['/sensor']['total'] == 2
    assert zoom['/sensor']['points'][0]['first_ns'] == header_origin + 6 * NS // 10
    assert not list(bag.glob('*.sqlite'))
