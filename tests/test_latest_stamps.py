import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ros2_recorder.communication import CommunicationSettings, PROFILE_VARIABLE
from ros2_recorder.environment import load_environment
from ros2_recorder.latest_stamps import LatestStampMonitor, format_stamp, latest_difference, sample_status
from ros2_recorder.stamp_probe import LatestStampState


def test_latest_arrival_can_go_backwards_and_invalid_newest_clears_previous():
    state = LatestStampState(9)
    reader = SimpleNamespace(read=lambda raw: SimpleNamespace(sec=raw[0], nanosec=raw[1]))
    state.receive((100, 123), reader, now_ns=1000)
    state.receive((99, 456), reader, now_ns=2000)
    assert state.stamp_ns == 99_000_000_456
    assert state.received_count == 2 and state.received_ns == 2000
    assert latest_difference(state.snapshot(), {'status': 'ok', 'stamp_ns': 99_010_000_456}) == -10
    state.receive((0, 0), reader, now_ns=3000)
    assert state.stamp_ns == 0 and state.status == 'invalid'
    assert latest_difference(state.snapshot(), {'status': 'ok', 'stamp_ns': 1}) is None
    state.receive((1, 1_000_000_000), reader, now_ns=4000)
    assert state.stamp_ns is None and state.status == 'invalid'
    reader.read = lambda raw: (_ for _ in ()).throw(ValueError('bad buffer'))
    state.receive(None, reader, now_ns=5000)
    assert state.stamp_ns is None and state.status == 'decode_error'
    assert state.received_count == 5


def test_precision_sign_stale_and_no_header_are_explicit():
    assert format_stamp(1_793_581_200_123_456_789) == '1793581200.123456789'
    assert format_stamp(-1) == '-0.000000001'
    assert format_stamp(None) == '—'
    first = {'status': 'ok', 'stamp_ns': 1_793_581_200_123_456_789, 'received_ns': 0}
    second = dict(first, stamp_ns=first['stamp_ns'] - 7_250_000)
    assert latest_difference(first, second) == 7.25
    assert '未更新' in sample_status(first, now_ns=3_000_000_000)
    assert '已停止' in sample_status(first, running=False)
    state = LatestStampState(1, error='消息没有 header.stamp')
    state.receive(b'payload', None, now_ns=123)
    assert state.received_count == 1 and state.stamp_ns is None and state.status == 'no_header'


def test_control_tokens_do_not_reuse_removed_topics(tmp_path):
    settings = CommunicationSettings(reliability='reliable', depth=2, transport='shm', udp_kib=256)
    monitor = LatestStampMonitor({}, {'/a': 'sensor_msgs/msg/Imu'}, on_snapshot=lambda _: None,
                                 on_done=lambda _: None, settings=settings)
    try:
        token = monitor.tokens['/a']
        monitor.update({})
        monitor.update({'/a': 'sensor_msgs/msg/Imu'})
        assert monitor.tokens['/a'] != token
        request = json.loads(monitor.control.read_text())
        assert request['generation'] == monitor.generation
        assert (request['reliability'], request['depth']) == ('reliable', 2)
        assert request['topics']['/a']['token'] == monitor.tokens['/a']
        profile = Path(monitor.runner.env[PROFILE_VARIABLE]).read_text()
        assert '<segment_size>33554432</segment_size>' in profile
        assert '<receiveBufferSize>262144</receiveBufferSize>' in profile
        with pytest.raises(ValueError, match='仅本机'):
            settings.monitor_environment({'ROS_LOCALHOST_ONLY': '1'}, tmp_path)
    finally:
        monitor.directory.cleanup()


def test_live_statistics_count_gaps_and_only_valid_frame_ages(monkeypatch):
    monkeypatch.setattr('ros2_recorder.stamp_probe.time.time_ns', lambda: 1_000_000_000)
    state = LatestStampState(1)
    now = state.started_ns
    reader = SimpleNamespace(read=lambda stamp: SimpleNamespace(sec=0, nanosec=stamp))
    state.receive(980_000_000, reader, now_ns=now + 100_000_000)
    state.receive(950_000_000, reader, now_ns=now + 200_000_000)
    state.receive(0, reader, now_ns=now + 500_000_000)
    snapshot = state.snapshot(now + 900_000_000)
    assert snapshot['received_count'] == 3 and snapshot['hz'] == 2.5
    assert snapshot['seconds'] == .9 and snapshot['max_gap_ms'] == 400
    assert snapshot['age_count'] == 2
    assert snapshot['age_p99_ms'] == snapshot['age_max_ms'] == 50


def test_native_header_prefix_matches_ros_decoder_and_checks_schema(tmp_path):
    script = r'''
import struct
from types import SimpleNamespace
from sensor_msgs.msg import Image, PointCloud2, Imu
from nav_msgs.msg import Path
from builtin_interfaces.msg import Time
from rosidl_parser.definition import BasicType, NamespacedType
from rclpy.serialization import serialize_message, deserialize_message
from ros2_recorder.header_stamp import HeaderStampReader
for cls in [Image, PointCloud2, Imu, Path]:
    message=cls(); message.header.stamp.sec=123; message.header.stamp.nanosec=456
    if cls is Image:
        message.data=b'x'*(4*1024*1024)
    payload=serialize_message(message)
    reader=HeaderStampReader(cls)
    actual=reader.read(payload)
    expected=deserialize_message(payload,cls).header.stamp
    assert actual == expected
    assert reader.prefix_count == 1 and reader.full_count == 0
    assert reader.read(b'\x00\x00\x00\x00'+struct.pack('>iI',123,456)) == expected
    seen=[]
    reader.deserialize=lambda data,kind: (seen.append((len(data),kind)) or SimpleNamespace(header=SimpleNamespace(stamp=expected)))
    reader.read(b'\x00\x07\x00\x00'+payload[4:])
    assert seen[-1] == (len(payload),cls), 'Unknown encodings must use the full native decoder'
class LateHeader:
    SLOT_TYPES=(BasicType('uint32'),NamespacedType(('std_msgs','msg'),'Header'))
    @staticmethod
    def get_fields_and_field_types(): return {'sequence':'uint32','header':'std_msgs/Header'}
    def __init__(self): self.header=SimpleNamespace(stamp=Time(sec=12,nanosec=34))
reader=HeaderStampReader(LateHeader)
assert not reader.prefix_supported
reader.deserialize=lambda payload,kind: LateHeader()
assert reader.read(b'\x00\x01\x00\x00'+b'x'*24).sec == 12
'''
    result = subprocess.run([sys.executable, '-c', script], env=load_environment(), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
def test_real_raw_subscriptions_latest_zero_backwards_and_live_updates(tmp_path):
    env = load_environment()
    env.update(ROS_DOMAIN_ID='176', ROS_LOCALHOST_ONLY='1')
    control = tmp_path / 'publisher.json'
    control.write_text(json.dumps({'camera_sec': 100, 'radar_sec': 100, 'radar_ns': 7_250_000}))
    stop = tmp_path / 'stop'
    script = '''
import json,sys,time
from pathlib import Path
import rclpy
from sensor_msgs.msg import Imu, Image, PointCloud2
from std_msgs.msg import String
rclpy.init(); n=rclpy.create_node('latest_stamp_test_publisher')
pubs=[n.create_publisher(cls,topic,10) for cls,topic in [(Image,'/stamp_camera'),(PointCloud2,'/stamp_radar'),(Imu,'/stamp_lidar'),(String,'/stamp_no_header')]]
deadline=time.monotonic()+35
while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:
    try: cfg=json.loads(Path(sys.argv[1]).read_text())
    except ValueError: time.sleep(.02); continue
    camera=Image();camera.header.stamp.sec=cfg['camera_sec']
    radar=PointCloud2();radar.header.stamp.sec=cfg['radar_sec'];radar.header.stamp.nanosec=cfg['radar_ns']
    lidar=Imu();lidar.header.stamp.sec=100;lidar.header.stamp.nanosec=3_500_000
    for publisher,message in zip(pubs,[camera,radar,lidar,String(data='no header')]): publisher.publish(message)
    time.sleep(.04)
n.destroy_node();rclpy.shutdown()
'''
    publisher = subprocess.Popen([sys.executable, '-c', script, str(control), str(stop)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    snapshots = []
    done = threading.Event()
    topics = {'/stamp_camera': 'sensor_msgs/msg/Image', '/stamp_radar': 'sensor_msgs/msg/PointCloud2',
              '/stamp_lidar': 'sensor_msgs/msg/Imu', '/stamp_no_header': 'std_msgs/msg/String',
              '/stamp_missing': 'no_such_test_package/msg/Unknown'}
    monitor = LatestStampMonitor(env, topics, on_snapshot=snapshots.append, on_done=lambda _: done.set())
    started_wall_ns = time.time_ns()
    monitor.start()
    def wait_for(predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            for event in reversed(snapshots):
                if predicate(event.get('samples', {})):
                    return event['samples']
            time.sleep(.03)
        raise AssertionError(str(snapshots[-2:]))
    try:
        samples = wait_for(lambda s: all(s.get(name, {}).get('received_count', 0) for name in list(topics)[:4]))
        now_wall_ns = time.time_ns()
        assert all(started_wall_ns <= samples[name]['received_wall_ns'] <= now_wall_ns for name in list(topics)[:4])
        assert samples['/stamp_missing']['received_wall_ns'] is None
        assert latest_difference(samples['/stamp_radar'], samples['/stamp_camera']) == 7.25
        assert latest_difference(samples['/stamp_lidar'], samples['/stamp_camera']) == 3.5
        assert samples['/stamp_no_header']['status'] == 'no_header'
        assert samples['/stamp_missing']['status'] == 'unavailable'
        control.write_text(json.dumps({'camera_sec': 99, 'radar_sec': 0, 'radar_ns': 0}))
        samples = wait_for(lambda s: s.get('/stamp_camera', {}).get('stamp_ns') == 99_000_000_000 and s.get('/stamp_radar', {}).get('stamp_ns') == 0)
        assert samples['/stamp_radar']['status'] == 'invalid'
        assert latest_difference(samples['/stamp_radar'], samples['/stamp_camera']) is None
        pid = monitor.runner.pid
        monitor.update({'/stamp_camera': topics['/stamp_camera']})
        snapshots.clear()
        wait_for(lambda s: set(s) == {'/stamp_camera'} and s['/stamp_camera'].get('received_count', 0))
        assert monitor.runner.pid == pid
    finally:
        stop.touch()
        monitor.stop()
        assert monitor.wait(8) and done.is_set()
        publisher.wait(5)
    assert not monitor.control.exists()
