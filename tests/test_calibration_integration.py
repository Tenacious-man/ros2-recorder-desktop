import json
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

from ros2_recorder.calibration_monitor import CalibrationMonitor
from ros2_recorder.config import atomic_json
from ros2_recorder.environment import load_environment


@pytest.mark.integration
def test_real_read_only_subscriptions_fields_topic_switch_and_shutdown(tmp_path):
    env = load_environment()
    env.update(ROS_DOMAIN_ID="181", ROS_LOCALHOST_ONLY="1")
    driver = tmp_path / "driver.json"
    report = tmp_path / "report.json"
    stop = tmp_path / "stop"
    atomic_json(driver, {"emit": True})
    script = r'''
import json,struct,sys,time
from array import array
from pathlib import Path
import rclpy
from rclpy.qos import QoSProfile,ReliabilityPolicy
from sensor_msgs.msg import PointCloud2,PointField
rclpy.init(); node=rclpy.create_node('calibration_acceptance_publisher')
qos=QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT)
pubs=[node.create_publisher(PointCloud2,topic,qos) for topic in ['/calibration_test_reference','/calibration_test_input']]
message=PointCloud2();message.width=2;message.height=1;message.point_step=24;message.row_step=48
message.fields=[PointField(name=name,offset=offset,datatype=kind,count=1) for name,offset,kind in
                [('x',0,7),('y',4,7),('z',8,7),('intensity',12,4),('ring',14,4),('time',16,8)]]
message.data=array('B',struct.pack('<fffHHdfffHHd',1,2,3,321,7,123.456,4,5,6,322,8,123.457))
message.header.stamp.sec=123;message.header.stamp.nanosec=456;message.header.frame_id='raw'
deadline=time.monotonic()+40
while not Path(sys.argv[3]).exists() and time.monotonic()<deadline:
    if json.loads(Path(sys.argv[1]).read_text())['emit']:
        for pub in pubs:pub.publish(message)
    try:
        topics=node.get_publisher_names_and_types_by_node('recorder_manual_calibration','/')
        report={'ready':True,'types':[kind for name,kinds in topics for kind in kinds]}
    except Exception:
        report={'ready':False}
    temp=Path(sys.argv[2]+'.tmp');temp.write_text(json.dumps(report));temp.replace(sys.argv[2])
    rclpy.spin_once(node,timeout_sec=.04)
node.destroy_node();rclpy.shutdown()
'''
    publisher = subprocess.Popen([sys.executable, "-c", script, str(driver), str(report), str(stop)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    settings = {"topics": {"reference": "/calibration_test_reference", "input": "/calibration_test_input"}}
    snapshots, completed = [], threading.Event()
    monitor = CalibrationMonitor(env, settings, on_snapshot=snapshots.append, on_done=lambda _code: completed.set())
    monitor.start()

    def wait_for(condition):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.03)
        raise AssertionError(str([{k: v for k, v in item.items() if k != "cloud"} for item in snapshots[-8:]]))

    def result():
        return json.loads(report.read_text()) if report.exists() else {}

    try:
        wait_for(lambda: {item["role"] for item in snapshots if "cloud" in item} == {"input", "reference"})
        cloud = next(item["cloud"] for item in reversed(snapshots) if item.get("role") == "input" and "cloud" in item)
        np.testing.assert_allclose(cloud["xyz"], [[1, 2, 3], [4, 5, 6]])
        assert cloud["fields"]["ring"].tolist() == [7, 8] and cloud["stamp_ns"] == 123000000456
        assert cloud["fields"]["intensity"].tolist() == [321, 322]
        np.testing.assert_allclose(cloud["fields"]["time"], [123.456, 123.457])
        wait_for(lambda: result().get("ready"))
        assert "sensor_msgs/msg/PointCloud2" not in result()["types"]
        assert "std_msgs/msg/Float64MultiArray" not in result()["types"]
        atomic_json(driver, {"emit": False})
        time.sleep(.3)
        settings = dict(settings, topics={"reference": "/calibration_test_reference", "input": "/calibration_never_published"})
        monitor.update(settings)
        generation = monitor.generation
        wait_for(lambda: any(item.get("ready") and item.get("generation") == generation for item in snapshots))
        time.sleep(.3)
        assert not any(item.get("role") == "input" and "cloud" in item and item.get("generation") == generation for item in snapshots)
        monitor.stop()
        assert monitor.wait(5) and completed.is_set()
        assert monitor.runner.returncode == 0
        assert not monitor.control.exists()
    finally:
        monitor.stop(force=True)
        monitor.wait(5)
        stop.touch()
        try:
            publisher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait()
        stdout, stderr = publisher.communicate()
        assert publisher.returncode == 0, stdout + stderr


@pytest.mark.integration
def test_page_discovers_and_subscribes_automatically_then_stops_on_navigation(tmp_path, monkeypatch):
    from PyQt5.QtWidgets import QApplication
    from ros2_recorder.app import MainWindow

    env = load_environment()
    env.update(ROS_DOMAIN_ID="182", ROS_LOCALHOST_ONLY="1")
    pause, stop = tmp_path / "pause", tmp_path / "stop"
    script = r'''
import struct,sys,time
from array import array
from pathlib import Path
import rclpy
from sensor_msgs.msg import PointCloud2,PointField
from rclpy.qos import QoSProfile,ReliabilityPolicy
rclpy.init();node=rclpy.create_node('calibration_auto_subscription_test')
pubs=[node.create_publisher(PointCloud2,topic,QoSProfile(depth=1,reliability=ReliabilityPolicy.BEST_EFFORT))
      for topic in ['/auto_reference','/auto_input']]
message=PointCloud2();message.width=1;message.height=1;message.point_step=16;message.row_step=16
message.fields=[PointField(name=name,offset=index*4,datatype=7,count=1) for index,name in enumerate(['x','y','z','intensity'])]
message.header.frame_id='raw';message.header.stamp.sec=123
message.data=array('B',struct.pack('<ffff',1,2,3,99))
sequence=0;deadline=time.monotonic()+35
while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:
    if not Path(sys.argv[1]).exists():
        sequence+=1;message.header.stamp.nanosec=sequence*1000000
        for publisher in pubs:publisher.publish(message)
    rclpy.spin_once(node,timeout_sec=.04)
node.destroy_node();rclpy.shutdown()
'''
    publisher = subprocess.Popen([sys.executable, "-c", script, str(pause), str(stop)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(MainWindow, "_initialize_environment", lambda *args: None)
    app = QApplication.instance()
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    window.auto_frequency.setChecked(False)
    window.active_env = env
    window._unlock_admin()
    window.show()
    window.nav.setCurrentRow(6)
    page = window.calibration_widget

    def wait_for(predicate):
        deadline = time.monotonic() + 12
        while not predicate():
            app.processEvents()
            time.sleep(.01)
            assert time.monotonic() < deadline, page.status.text()
        app.processEvents()

    try:
        wait_for(lambda: page.topics["reference"].findText("/auto_reference") >= 0)
        reference = page.topics["reference"]
        reference.setCurrentIndex(reference.findText("/auto_reference"))
        wait_for(lambda: "reference" in page.view.clouds)
        monitor = page.monitor
        assert "input" not in page.view.clouds
        topic = page.topics["input"]
        topic.setCurrentIndex(topic.findText("/auto_input"))
        wait_for(lambda: "input" in page.view.clouds)
        assert page.monitor is monitor
        assert page.view.clouds["input"]["fields"]["intensity"].tolist() == [99.]
        pause.touch()  # External source stops; the application has no playback/freeze action.
        deadline = time.monotonic() + .4
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        stamp = page.view.clouds["input"]["stamp_ns"]
        revision = monitor.revision
        page.axes["input"][3].setValue(2.5)
        app.processEvents()
        np.testing.assert_allclose(page.view.transformed_points("input"), [[3.5, 2, 3]])
        assert monitor.revision == revision
        assert page.view.clouds["input"]["stamp_ns"] == stamp
        window.nav.setCurrentRow(0)
        wait_for(lambda: not page.busy)
        assert monitor.runner.returncode == 0 and not page.refresh_timer.isActive()
        assert not window.frequency_suspended
    finally:
        window.close()
        wait_for(lambda: not window.isVisible())
        stop.touch()
        try:
            publisher.wait(timeout=5)
        except subprocess.TimeoutExpired:
            publisher.kill()
            publisher.wait()
        stdout, stderr = publisher.communicate()
        assert publisher.returncode == 0, stdout + stderr
