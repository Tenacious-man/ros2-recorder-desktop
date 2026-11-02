import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from ros2_recorder.environment import load_environment
from ros2_recorder.frequency import FrequencyMonitor


@pytest.mark.integration
def test_shared_monitor_measures_more_than_eight_topics_and_updates_subscriptions(tmp_path):
    env = load_environment()
    env.update(ROS_DOMAIN_ID='175', ROS_LOCALHOST_ONLY='1')
    names = [f'/shared_hz_{i}' for i in range(12)]
    stop = tmp_path / 'stop'
    script = '''
import sys,time
from pathlib import Path
import rclpy
from std_msgs.msg import String
rclpy.init()
n=rclpy.create_node('shared_frequency_test_publisher')
pubs=[n.create_publisher(String,'/shared_hz_'+str(i),100) for i in range(12)]
deadline=time.monotonic()+25
while not Path(sys.argv[1]).exists() and time.monotonic()<deadline:
    for p in pubs:
        p.publish(String(data='sample'))
    time.sleep(.04)
n.destroy_node()
rclpy.shutdown()
'''
    publisher = subprocess.Popen([sys.executable, '-c', script, str(stop)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    results = {}
    monitor = FrequencyMonitor(env, dict.fromkeys(names, 'std_msgs/msg/String'),
                               on_result=lambda name, value: results.__setitem__(name, value), on_done=lambda _: None)
    monitor.start()
    try:
        deadline = time.monotonic() + 15
        while not all(results.get(name, {}).get('hz') for name in names) and time.monotonic() < deadline:
            time.sleep(.05)
        assert all(15 < results.get(name, {}).get('hz', 0) < 35 for name in names), results
        pid = monitor.runner.pid
        monitor.update(dict.fromkeys(names[:4], 'std_msgs/msg/String'))
        time.sleep(.4)
        monitor.update(dict.fromkeys(names, 'std_msgs/msg/String'))
        results.clear()
        deadline = time.monotonic() + 10
        while not all(results.get(name, {}).get('hz') for name in names) and time.monotonic() < deadline:
            time.sleep(.05)
        assert all(results.get(name, {}).get('hz') for name in names)
        assert monitor.runner.pid == pid, 'updating topics restarted the shared process'
    finally:
        stop.touch()
        monitor.stop()
        assert monitor.wait(10)
        publisher.wait(5)
    assert not monitor.control.exists()
