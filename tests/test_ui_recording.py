import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import subprocess
import sys
import time

import pytest
from PyQt5.QtWidgets import QApplication

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.config import RecordConfig


def until(app, condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.02)
    assert condition(), 'UI operation did not finish'


@pytest.mark.integration
def test_start_and_stop_from_native_workbench(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'settings'))
    monkeypatch.setenv('ROS_DOMAIN_ID', '174')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    errors = []
    window._error = errors.append
    publisher = None
    go = tmp_path / 'publish-recording'
    sent = tmp_path / 'recording-sent'
    stop = tmp_path / 'stop-publisher'
    try:
        until(app, lambda: bool(window.active_env) and not window.tasks)
        window.apply_config(RecordConfig(output_root=str(tmp_path / 'bags'), all_topics=False,
                                         topics=['/ui_acceptance'], min_free_gib=0, cache_mib=8), load_env=False)
        window._append(window.event_log, '上一轮录制的界面日志')
        window.clear_log_button.click()
        assert window.event_log.toPlainText() == ''
        window.refresh_topics()
        until(app, lambda: not window.discovery_busy)
        assert '/ui_frequency_a' not in window.monitors
        window.auto_discover = True
        code = '''
import time, sys
from pathlib import Path
import rclpy
from std_msgs.msg import String
rclpy.init()
n=rclpy.create_node('ui_test_publisher')
p=n.create_publisher(String,'/ui_acceptance',100)
a=n.create_publisher(String,'/ui_frequency_a',100)
b=n.create_publisher(String,'/ui_frequency_b',100)
go,sent,stop=map(Path,sys.argv[1:])
deadline=time.monotonic()+40
i=0
while not stop.exists() and time.monotonic()<deadline:
    a.publish(String(data='a'))
    b.publish(String(data='b'))
    if go.exists() and i<120 and p.get_subscription_count()>0:
        assert p.get_subscription_count()==1, 'recording still has a frequency subscription'
        assert a.get_subscription_count()==0 and b.get_subscription_count()==0, 'other frequency subscriptions still active'
        p.publish(String(data=str(i)))
        i+=1
        if i==120:
            sent.touch()
    time.sleep(.02)
n.destroy_node()
rclpy.shutdown()
'''
        publisher = subprocess.Popen([sys.executable, '-c', code, str(go), str(sent), str(stop)], env=window.active_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Topics appear after startup: discovery automatically measures all of them.
        until(app, lambda: window.frequency_results.get('/ui_frequency_a', {}).get('hz') is not None)
        until(app, lambda: window.frequency_results.get('/ui_frequency_b', {}).get('hz') is not None)
        assert window.auto_frequency.isChecked()
        assert '/ui_acceptance' in window.monitors
        previous_monitor = window.frequency_monitor
        window.toggle_frequency('/ui_frequency_b')
        assert '/ui_frequency_b' not in window.monitors
        window.refresh_topics()
        until(app, lambda: not window.discovery_busy)
        assert '/ui_frequency_b' not in window.monitors, 'manual opt-out was reset by refresh'
        window.toggle_frequency('/ui_frequency_b')
        window.topic_search.setText('ui_frequency_a')
        assert window.get_config().topics == ['/ui_acceptance']
        window.start_button.click()
        until(app, lambda: window.session is not None or bool(errors))
        assert not errors
        assert not window.parameter_box.isEnabled()
        assert window.topic_table.isEnabled()
        assert window.stop_button.isEnabled()
        assert previous_monitor.runner.returncode is not None
        assert window.frequency_suspended
        assert not window.monitors and not window.frequency_monitor
        assert not window.auto_frequency.isEnabled()
        window.toggle_frequency('/ui_acceptance')
        assert not window.monitors
        assert window._frequency_text('/ui_acceptance') == '检测已暂停'
        log_path = window.session.session_dir / 'recorder.log'
        until(app, lambda: log_path.is_file() and log_path.stat().st_size > 0)
        saved_log = log_path.read_text()
        window.clear_log_button.click()
        assert window.event_log.toPlainText() == ''
        assert log_path.read_text().startswith(saved_log)
        assert window.is_recording
        go.touch()
        until(app, lambda: sent.exists() or publisher.poll() is not None)
        assert sent.exists(), publisher.stderr.read()
        time.sleep(.2)
        window.stop_button.click()
        until(app, lambda: window.session.finished and window.start_button.isEnabled() and not window.tasks)
        report = json.loads((window.session.session_dir / 'report.json').read_text())
        assert report['message_count'] == 120
        assert report['status'] == 'completed'
        assert '录制结束' in window.event_log.toPlainText()
        assert log_path.read_text().startswith(saved_log)
        assert window.parameter_box.isEnabled()
        assert window.metric_values['state'].text() == '已结束'
        summary = window.recording_summary
        assert summary is not None and summary.isVisible()
        assert summary.windowTitle() == '录制已保存'
        assert summary.path_editor.text() == str(window.session.output)
        assert '1 个有消息 / 1 个已登记' in summary.summary_text
        assert f"{report['duration_seconds']:.3f} 秒" in summary.summary_text
        assert summary.topic_table.item(0, 0).text() == '/ui_acceptance'
        assert summary.topic_table.item(0, 2).text() == '120'
        assert summary.topic_table.item(0, 3).text() == f"{120 / report['duration_seconds']:.2f} Hz"
        summary.confirm_button.click()
        assert window.recording_summary is None
        assert window.sessions[0]['report']['message_count'] == 120
        assert not window.frequency_suspended
        assert window.auto_frequency.isEnabled()
        until(app, lambda: window.frequency_results.get('/ui_frequency_a', {}).get('hz') is not None)
        until(app, lambda: window.frequency_results.get('/ui_frequency_b', {}).get('hz') is not None)
        assert '/ui_acceptance' in window.monitors
        assert not errors
    finally:
        stop.touch()
        if publisher and publisher.poll() is None:
            try:
                publisher.wait(5)
            except subprocess.TimeoutExpired:
                publisher.terminate()
                publisher.wait(5)
        if window.is_recording:
            window.session.stop()
            window.session.runner.wait(10)
        window.stop_all_monitors()
        until(app, lambda: not window.monitors and window.frequency_monitor is None)
        until(app, lambda: not window.tasks)
        window.close()
