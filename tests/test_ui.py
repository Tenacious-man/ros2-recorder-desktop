import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import time
import threading
import pytest

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.config import ProfileStore, RecordConfig, import_command


def test_native_workbench_profile_roundtrip_and_monitor_render(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    errors = []
    window._error = errors.append
    deadline = time.monotonic() + 10
    while (window.tasks or not window.active_env) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.02)
    assert not errors
    original = import_command('ros2 bag record /radar --max-cache-size 1000000000 -b 3000000000')
    window.apply_config(original, load_env=False)
    assert window.get_config() == original
    window.cache.setValue(256)
    assert window.get_config().cache_bytes == -1
    window.topics = [{'name': '/radar', 'types': ['radar_msgs/msg/Target'], 'publishers': 1, 'available': False, 'reason': 'not loaded'}]
    window._render_topics()
    assert not window.topic_table.cellWidget(0, 5).isEnabled()
    assert window.stack.count() == 5
    window.build_cancelled = True
    window._build_done(0)
    assert '已取消编译' in window.env_status.text()
    window.close()


def test_topic_search_preserves_recording_selection_and_compact_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    errors = []
    window._error = errors.append
    deadline = time.monotonic() + 10
    while window.tasks or not window.active_env:
        app.processEvents(); time.sleep(.02)
        assert time.monotonic() < deadline
    window.topics = [
        {'name': '/camera/image', 'types': ['sensor_msgs/msg/Image'], 'publishers': 1, 'available': True},
        {'name': '/radar/targets', 'types': ['radar_msgs/msg/TargetList'], 'publishers': 1, 'available': True},
        {'name': '/lidar/points', 'types': ['sensor_msgs/msg/PointCloud2'], 'publishers': 1, 'available': True},
    ]
    window.selected_topics = {'/radar/targets'}
    window._render_topics()
    window.topic_search.setText('CAMERA')
    visible = lambda: [t['name'] for row, t in enumerate(window.display_topics) if not window.topic_table.isRowHidden(row)]
    assert visible() == ['/camera/image']
    assert window.get_config().topics == ['/radar/targets']
    assert '搜索结果外 1 个' in window.topic_count.text()
    window.select_visible_button.click()
    assert window.get_config().topics == ['/camera/image', '/radar/targets']
    window.clear_visible_button.click()
    assert window.get_config().topics == ['/radar/targets']
    window.topic_search.setText('PointCloud2')
    assert visible() == ['/lidar/points']
    window.select_visible_button.click()
    window.topic_search.clear()
    window._render_topics()  # A graph refresh must not reset selection or search behavior.
    config = window.get_config()
    assert config.topics == ['/lidar/points', '/radar/targets']
    assert not config.all_topics and not config.regex and not config.exclude
    command = window.capabilities.command(config, tmp_path / 'bag')
    assert '--all' not in command and '--regex' not in command and '--exclude' not in command
    for row in range(window.topic_table.rowCount()):
        assert '\n' not in window.topic_table.item(row, 1).text()
        assert '\n' not in window.topic_table.item(row, 2).text()
        assert window.topic_table.rowHeight(row) == 36
    window.topic_search.setText('[')  # Literal text, not a regex parser.
    assert visible() == [] and len(window.selected_topics) == 2
    window.auto_frequency.setChecked(False)
    window.apply_config(RecordConfig(all_topics=True, exclude='^/radar/targets$', auto_detect_frequency=False), load_env=False)
    assert not window.selected_topics  # Legacy implicit scopes must not silently broaden recording.
    with pytest.raises(ValueError):
        window.get_config()
    assert not errors
    window.close()


def test_detection_startup_ignores_old_profile_but_preserves_session_choice(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    ProfileStore().save(RecordConfig(all_topics=False, topics=['/later'], auto_detect_frequency=False))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    try:
        assert window.auto_frequency.isChecked(), 'old saved False overrode startup default'
        assert window.topic_table.rowCount() == 0
        assert window.topic_count.text() == '显示 0/0 · 已勾选 0'
        assert window.get_config().topics == ['/later']
        window.apply_config(RecordConfig(auto_detect_frequency=False), load_env=False)
        assert window.auto_frequency.isChecked()
        window.auto_frequency.setChecked(False)
        window.apply_config(RecordConfig(auto_detect_frequency=True), load_env=False)
        assert not window.auto_frequency.isChecked(), 'profile changed manual session choice'
    finally:
        window.close()


def test_discovery_retries_latest_environment_when_previous_probe_is_busy(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    monkeypatch.setattr(MainWindow, '_sync_frequency_monitor', lambda *args: None)
    old_started, release_old = threading.Event(), threading.Event()
    calls = []
    def discover(env, hidden):
        calls.append(env['test_environment'])
        if env['test_environment'] == 'base':
            old_started.set()
            assert release_old.wait(5)
        return [{'name': '/' + env['test_environment'], 'types': ['std_msgs/msg/String'],
                 'publishers': 1, 'available': True}]
    monkeypatch.setattr('ros2_recorder.app.discover_topics', discover)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.timer.stop()
    app.processEvents()
    try:
        window._set_environment({'test_environment': 'base'}, '', '', [])
        assert old_started.wait(2)
        window._set_environment({'test_environment': 'overlay'}, '/overlay/setup.bash', '', [])
        assert window.topic_table.rowCount() == 0
        release_old.set()
        deadline = time.monotonic() + 5
        while window.tasks or window.discovery_busy or '/overlay' not in window.monitors:
            app.processEvents()
            time.sleep(.01)
            assert time.monotonic() < deadline
        assert calls == ['base', 'overlay']
        assert [t['name'] for t in window.topics] == ['/overlay']
        assert window.monitors == {'/overlay': 'std_msgs/msg/String'}
        assert window.topic_table.cellWidget(0, 5).text() == '关闭'
    finally:
        release_old.set()
        window.close()


def test_profiles_only_select_real_topics_without_adding_offline_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    try:
        profile = RecordConfig(all_topics=False, topics=['/lidar', '/offline'], cache_mib=256)
        window.apply_config(profile, load_env=False)
        assert window.get_config().topics == ['/lidar', '/offline']
        assert window.cache.value() == 256
        assert window.display_topics == [] and window.topic_table.rowCount() == 0
        assert window.topic_count.text() == '显示 0/0 · 已勾选 0'
        window.topics = [dict(name=name, types=['std_msgs/msg/String'], publishers=1, available=True)
                         for name in ['/lidar', '/radar']]
        window._render_topics()
        assert [t['name'] for t in window.display_topics] == ['/lidar', '/radar']
        assert window.topic_table.item(0, 0).checkState() == Qt.Checked
        assert window.topic_table.item(1, 0).checkState() == Qt.Unchecked
        assert window.topic_count.text() == '显示 2/2 · 已勾选 1'
        window.apply_config(RecordConfig(all_topics=False, topics=['/radar', '/another_offline']), load_env=False)
        assert window.topic_table.rowCount() == 2
        assert window.topic_table.item(0, 0).checkState() == Qt.Unchecked
        assert window.topic_table.item(1, 0).checkState() == Qt.Checked
        window.topics = []
        window._render_topics()
        assert window.topic_table.rowCount() == 0
        assert window.topic_count.text() == '显示 0/0 · 已勾选 0'
        assert window.get_config().topics == ['/another_offline', '/radar']
        monkeypatch.setattr('ros2_recorder.app.QInputDialog.getText', lambda *args: ('/manual_later', True))
        window.add_topic()
        assert window.topic_table.rowCount() == 0
        assert '/manual_later' in window.get_config().topics
        assert '发现后会显示并自动勾选' in window.event_log.toPlainText()
        window.topics = [dict(name='/manual_later', types=['std_msgs/msg/String'], publishers=1, available=True)]
        window._render_topics()
        assert window.topic_table.rowCount() == 1
        assert window.topic_table.item(0, 0).checkState() == Qt.Checked
        assert window.topic_count.text() == '显示 1/1 · 已勾选 1'
    finally:
        window.close()
