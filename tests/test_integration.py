"""Real ROS 2 roundtrips: finite publisher -> recorder -> metadata + decoded sequences."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from ros2_recorder.config import RecordConfig
from ros2_recorder.environment import build_command, discover_topics, inspect_workspace, load_environment, probe_argv, validate_message_types
from ros2_recorder.processes import ProcessRunner
from ros2_recorder.recording import RecordingSession


PUBLISHER = '''
import json, sys, time
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rosidl_runtime_py.utilities import get_message
rclpy.init()
node = rclpy.create_node('recorder_finite_test_publisher')
kind, topic, count, subscribers = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
cls = get_message(kind)
pub = node.create_publisher(cls, topic, QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE))
deadline=time.monotonic()+20
while pub.get_subscription_count()<subscribers:
    if time.monotonic()>deadline:
        raise RuntimeError('recorder/monitor subscription not discovered')
    time.sleep(.05)
for i in range(count):
    msg=cls()
    if kind=='std_msgs/msg/String':
        msg.data=str(i)+'|'+('x'*12000)
    else:
        msg.seq=i
        msg.header.stamp=node.get_clock().now().to_msg()
        msg.payload=[i%256]*128
    pub.publish(msg)
    time.sleep(1/30)
time.sleep(.5)
print(json.dumps({'published':count}),flush=True)
node.destroy_node()
rclpy.shutdown()
'''

READER = '''
import json, sys
import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
reader=rosbag2_py.SequentialReader()
reader.open(rosbag2_py.StorageOptions(uri=sys.argv[1],storage_id=sys.argv[2]),rosbag2_py.ConverterOptions('',''))
seq=[]
while reader.has_next():
    topic, payload, stamp=reader.read_next()
    msg=deserialize_message(payload,get_message(sys.argv[3]))
    seq.append(int(msg.data.split('|',1)[0]) if sys.argv[3]=='std_msgs/msg/String' else msg.seq)
print(json.dumps({'seq':seq}))
'''


def isolated_env(domain):
    env = load_environment()
    env.update(ROS_DOMAIN_ID=str(domain), ROS_LOCALHOST_ONLY='1')
    return env


def roundtrip(tmp_path, env, storage, message_type='std_msgs/msg/String', setup='', workspace='', reliability='auto', use_yaml=False):
    publisher_file = tmp_path / 'publisher.py'
    publisher_file.write_text(PUBLISHER)
    topic = f'/recorder_acceptance_{storage}'
    count = 220 if message_type == 'std_msgs/msg/String' else 100
    report = []
    complete = threading.Event()
    from ros2_recorder.yaml_templates import ensure_templates
    templates = ensure_templates() if use_yaml else {}
    session = RecordingSession(
        RecordConfig(qos_path=str(templates['qos']) if use_yaml else '',
                     storage_config=str(templates[storage]) if use_yaml else '',
                     output_root=str(tmp_path / 'bags'), all_topics=False, topics=[topic],
                     storage=storage, cache_mib=8, split_mib=1, min_free_gib=0,
                     recording_reliability=reliability,
                     environment_setup=setup, workspace=workspace), env,
        on_done=lambda value: (report.append(value), complete.set()),
    )
    events = []
    monitor = ProcessRunner(probe_argv('hz', '--topic', topic), env,
                            line=lambda line: events.append(json.loads(line[10:])) if line.startswith('@@RESULT@@') else None)
    publisher = subprocess.Popen([sys.executable, str(publisher_file), message_type, topic, str(count), '2'],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        session.start()
        monitor.start()
        stdout, stderr = publisher.communicate(timeout=35)
        assert publisher.returncode == 0, stderr
        assert json.loads(stdout)['published'] == count
        session.stop()
        assert complete.wait(20), 'recorder did not finish flushing'
        monitor.stop()
        assert monitor.wait(10)
        assert report[0]['status'] == 'completed', report[0]
        assert report[0]['message_count'] == count
        assert report[0]['topics'][0]['type'] == message_type
        hz = [e['hz'] for e in events if e.get('hz') and not e.get('stale')]
        assert hz and 20 < hz[-1] < 40, events
        read = subprocess.run([sys.executable, '-c', READER, str(session.output), storage, message_type],
                              env=env, capture_output=True, text=True, timeout=20)
        assert read.returncode == 0, read.stderr
        decoded = json.loads(read.stdout.splitlines()[-1])
        assert decoded['seq'] == list(range(count)), 'missing, duplicate or reordered recorded messages'
        assert (session.session_dir / 'recorder.log').exists()
        assert (session.session_dir / 'report.json').exists()
        if workspace:
            assert (session.session_dir / 'interfaces' / 'recorder_test_msgs' / 'msg' / 'Sample.msg').exists()
        return report[0]
    finally:
        if publisher.poll() is None:
            publisher.terminate()
            publisher.wait(5)
        if session.runner and not session.finished:
            session.stop()
            if not session.runner.wait(10):
                session.stop(force=True)
                session.runner.wait(5)
        monitor.stop()
        monitor.wait(5)


@pytest.mark.integration
@pytest.mark.parametrize('storage', ['sqlite3', 'mcap'])
def test_real_recording_frequency_and_sequence_integrity(tmp_path, storage):
    roundtrip(tmp_path, isolated_env(171 if storage == 'sqlite3' else 172), storage)


@pytest.mark.integration
def test_compile_load_and_record_custom_message(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    workspace = tmp_path / 'custom ws with spaces'
    package = workspace / 'src' / 'recorder_test_msgs'
    (package / 'msg').mkdir(parents=True)
    (package / 'msg' / 'Sample.msg').write_text('std_msgs/Header header\nuint32 seq\nuint8[] payload\n')
    (package / 'package.xml').write_text('''<?xml version="1.0"?>
<package format="3"><name>recorder_test_msgs</name><version>0.0.1</version>
<description>Recorder integration test interfaces</description><maintainer email="test@example.com">Test</maintainer>
<license>Apache-2.0</license><buildtool_depend>ament_cmake</buildtool_depend>
<buildtool_depend>rosidl_default_generators</buildtool_depend><depend>std_msgs</depend>
<exec_depend>rosidl_default_runtime</exec_depend><member_of_group>rosidl_interface_packages</member_of_group>
<export><build_type>ament_cmake</build_type></export></package>''')
    (package / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.8)
project(recorder_test_msgs)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME} "msg/Sample.msg" DEPENDENCIES std_msgs)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
''')
    info = inspect_workspace(str(workspace))
    info.build_root.mkdir(parents=True)
    logs = []
    build = ProcessRunner(build_command(info, ['recorder_test_msgs']), isolated_env(173),
                          cwd=str(info.build_root), line=logs.append)
    build.start()
    try:
        assert build.wait(100), 'custom message build timed out'
        assert build.returncode == 0, '\n'.join(logs[-40:])
        assert info.setup.exists()
        env = load_environment(str(info.setup), isolated_env(173))
        assert validate_message_types(env, ['recorder_test_msgs']) == ['recorder_test_msgs/msg/Sample']
        roundtrip(tmp_path, env, 'sqlite3', 'recorder_test_msgs/msg/Sample', str(info.setup), str(workspace))
    finally:
        if build.returncode is None:
            build.stop(force=True)
            build.wait(5)


@pytest.mark.integration
def test_forced_reliable_recording_roundtrip(tmp_path):
    roundtrip(tmp_path, isolated_env(174), 'sqlite3', reliability='reliable')


@pytest.mark.integration
@pytest.mark.parametrize('storage', ['sqlite3', 'mcap'])
def test_default_yaml_templates_real_recording(tmp_path, monkeypatch, storage):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    roundtrip(tmp_path, isolated_env(171 if storage == 'sqlite3' else 172),
              storage, use_yaml=True)
