import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from types import SimpleNamespace
import time
import threading
import pytest

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.capacity_widget import CapacitySpinBox
from ros2_recorder.config import ProfileStore, RecordConfig, import_command
from ros2_recorder.environment import workspace_build_root
from ros2_recorder.units import GIB, MIB


def test_capacity_displays_decimal_values_and_preserves_existing_settings():
    cache = CapacitySpinBox(512, 0, 4095, MIB, 'MB')
    reserve = CapacitySpinBox(5, 0, 1000000, GIB, 'GB', precision=2)
    assert cache.text() == '536.87 MB' and cache.binary_value() == 512
    assert reserve.text() == '5.37 GB' and reserve.binary_value() == 5
    cache.lineEdit().selectAll()
    QTest.keyClicks(cache.lineEdit(), '100')
    QTest.keyClick(cache.lineEdit(), Qt.Key_Return)
    assert cache.text() == '99.61 MB' and cache.binary_value() == 95


@pytest.mark.parametrize('entry', ['workspace', 'install', 'setup', 'cache', 'dialog'])
def test_load_compiled_environment_without_sources(tmp_path, monkeypatch, entry):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    workspace = tmp_path / 'workspace'
    install = workspace / 'install'
    if entry == 'cache':
        build = workspace_build_root(workspace, window.base_env) / 'builds' / 'latest'
        build.mkdir(parents=True)
        (build / 'build-success.json').write_text('{}')
        install = build / 'install'
    install.mkdir(parents=True)
    setup = install / 'setup.bash'
    setup.touch()
    entries = {'workspace': workspace, 'install': install, 'setup': setup, 'cache': workspace, 'dialog': ''}
    window.workspace_path.setText(str(entries[entry]))
    monkeypatch.setattr('ros2_recorder.app.QFileDialog.getOpenFileName', lambda *args: (str(setup), ''))
    requests = []
    monkeypatch.setattr(window, '_request_environment', lambda path, source: requests.append((Path(path), source)))
    try:
        window.load_button.click()
        assert requests == [(setup, str(workspace) if entry in ('workspace', 'cache') else '')]
    finally:
        window.close()
        app.processEvents()


def test_clear_overlay_restores_base_and_forgets_last_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    base = {'environment': 'base'}
    window.base_env = base
    profile = RecordConfig(all_topics=False, topics=['/sensor'], environment_setup='/overlay/setup.bash',
                           workspace='/overlay', packages=['sensor_msgs'])
    window.store.save(profile, 'saved')
    window._set_environment({'environment': 'overlay'}, profile.environment_setup, profile.workspace, profile.packages)
    calls = []
    monkeypatch.setattr('ros2_recorder.app.load_environment', lambda setup, env: calls.append((setup, env)) or env)
    monkeypatch.setattr('ros2_recorder.app.detect_capabilities', lambda env: None)
    try:
        assert window.clear_environment_button.isEnabled()
        window.clear_environment_button.click()
        deadline = time.monotonic() + 5
        while window.tasks:
            app.processEvents()
            time.sleep(.01)
            assert time.monotonic() < deadline
        assert calls == [('', base)]
        assert window.active_env == base
        assert not window.loaded_setup and not window.active_workspace and not window.active_packages
        assert not window.clear_environment_button.isEnabled()
        assert not window.selected_topics
        persisted = ProfileStore(window.store.path)
        assert persisted.last['environment_setup'] == ''
        assert persisted.last['topics'] == ['/sensor']
        assert persisted.profiles['saved']['environment_setup'] == profile.environment_setup
    finally:
        window.close()


@pytest.mark.parametrize('probe_failed', [False, True])
def test_missing_message_types_warn_without_blocking_recording(tmp_path, monkeypatch, probe_failed):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    caps = SimpleNamespace(command=lambda config, output: config.argv(output))
    monkeypatch.setattr('ros2_recorder.app.detect_capabilities', lambda env: caps)
    def discover(*args):
        if probe_failed:
            raise RuntimeError('无法检查话题')
        return [dict(name=name, types=['pkg/msg/Type'], available=available, publishers=1)
                for name, available in [('/standard', True), ('/custom', False), ('/unselected', False)]]
    monkeypatch.setattr('ros2_recorder.app.discover_topics', discover)
    commands = []
    monkeypatch.setattr('ros2_recorder.recording.ProcessRunner',
                        lambda argv, env, **kwargs: SimpleNamespace(start=lambda: commands.append(argv)))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    errors = []
    window._error = errors.append
    window.base_env = window.active_env = {'ROS_DISTRO': 'humble'}
    window.apply_config(RecordConfig(output_root=str(tmp_path / 'bags'), min_free_gib=0, all_topics=False,
                                     topics=['/standard', '/custom']), load_env=False)
    try:
        window.start_button.click()
        deadline = time.monotonic() + 5
        while window.tasks:
            app.processEvents()
            time.sleep(.01)
            assert time.monotonic() < deadline
        assert not errors and window.is_recording
        assert len(commands) == 1 and {'/standard', '/custom'} <= set(commands[0])
        expected = '无法检查话题' if probe_failed else '消息类型未加载，可能无法写入 bag：/custom'
        assert len(window.session.warnings) == 1 and expected in window.session.warnings[0]
        assert expected in window.event_log.toPlainText()
        assert '/unselected' not in window.event_log.toPlainText()
    finally:
        if window.session:
            window.session.log_file.close()
            window.session.finished = True
        window.close()


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
    restored = window.get_config()
    assert restored == __import__("dataclasses").replace(
        original, qos_path=window.qos_path.text(), qos_enabled=False,
        storage_config=window.storage_config.text(), storage_config_enabled=False)
    window.cache.set_binary_value(256)
    assert window.get_config().cache_bytes == -1
    window.topics = [{'name': '/radar', 'types': ['radar_msgs/msg/Target'], 'publishers': 1, 'available': False, 'reason': 'not loaded'}]
    window._render_topics()
    assert not window.topic_table.cellWidget(0, 6).isEnabled()
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
    assert window.get_config().topics == ['/lidar/points', '/radar/targets']
    assert not window.get_config().all_topics  # Legacy scopes never broaden the user's selection.
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


@pytest.mark.parametrize('scope', [
    dict(all_topics=True),
    dict(all_topics=True, exclude='^/radar$'),
    dict(all_topics=False, regex='^/lidar'),
])
def test_legacy_profile_load_applies_parameters_without_resetting_checkboxes(tmp_path, monkeypatch, scope):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    monkeypatch.setattr(MainWindow, '_request_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    errors = []
    window._error = errors.append
    profile = RecordConfig(**scope, prefix='test', cache_mib=256, split_mib=1024)
    profile.validate()
    window.store.profiles['test'] = profile.to_dict()
    window.topics = [dict(name=name, types=['std_msgs/msg/String'], publishers=1, available=True)
                     for name in ['/camera', '/radar']]
    window.selected_topics = {'/camera'}
    try:
        for _ in range(2):
            window.cache.set_binary_value(512)
            window._load_profile('test')
            assert window.cache.binary_value() == 256
            assert window.split_size.binary_value() == 1024
            assert window.prefix.text() == 'test'
            config = window.get_config()
            assert config.topics == ['/camera']
            assert not config.all_topics and not config.regex and not config.exclude
            assert window.topic_table.item(0, 0).checkState() == Qt.Checked
            assert window.topic_table.item(1, 0).checkState() == Qt.Unchecked
        assert '旧配置' not in window.event_log.toPlainText()
        assert '已应用配置方案：test' in window.event_log.toPlainText()
        window.selected_topics.clear()
        window._load_profile('test')
        assert not window.selected_topics
        assert all(window.topic_table.item(row, 0).checkState() == Qt.Unchecked
                   for row in range(window.topic_table.rowCount()))
        with pytest.raises(ValueError, match='至少选择'):
            window.get_config()
        assert not errors
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
        assert window.refresh_topics_button.text() == '刷新话题'
        assert window.refresh_topics_button.isEnabled()
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
        assert window.topic_table.cellWidget(0, 6).text() == '关闭'
    finally:
        release_old.set()
        window.close()


@pytest.mark.parametrize('trigger', ['automatic', 'manual', 'join_background'])
@pytest.mark.parametrize('failed', [False, True])
def test_background_discovery_is_quiet_and_manual_click_shows_progress(tmp_path, monkeypatch, trigger, failed):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    monkeypatch.setattr(MainWindow, '_sync_frequency_monitor', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.timer.stop()
    app.processEvents()
    pending = []
    window.run_task = lambda work, done, fail: pending.append((done, fail))
    window.active_env = {'test_environment': 'active'}
    window.last_idle_disk = time.monotonic()
    try:
        if trigger == 'manual':
            window.refresh_topics_button.click()
        else:
            window._tick()
            assert window.refresh_topics_button.text() == '刷新话题'
            assert window.refresh_topics_button.isEnabled()
            window._tick()
            assert len(pending) == 1
            if trigger == 'join_background':
                window.refresh_topics_button.click()
        assert window.discovery_busy and len(pending) == 1
        if trigger != 'automatic':
            assert window.refresh_topics_button.text() == '正在发现…'
            assert not window.refresh_topics_button.isEnabled()
        done, failure = pending[0]
        if failed:
            failure('话题发现失败示例')
            assert '话题发现失败示例' in window.event_log.toPlainText()
        else:
            done([dict(name='/sensor', types=['std_msgs/msg/String'], publishers=1, available=True)])
            assert window.topic_table.rowCount() == 1
            assert window.topic_table.item(0, 1).text() == '/sensor'
        assert not window.discovery_busy
        assert window.refresh_topics_button.text() == '刷新话题'
        assert window.refresh_topics_button.isEnabled()
        assert len(pending) == 1, 'Manual clicks must not create another parallel discovery job'
    finally:
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
        assert window.cache.binary_value() == 256
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
        assert window.topic_table.rowCount() == 1
        assert window.topic_table.item(0, 1).text() == '/manual_later'
        assert window.topic_table.item(0, 2).text() == '手动添加，未发现'
        assert not window.topic_table.cellWidget(0, 6).isEnabled()
        assert window.topic_count.text() == '显示 1/1 · 已勾选 1'
        assert '/manual_later' in window.get_config().topics
        assert '手动添加，未发现' in window.event_log.toPlainText()
        window.add_topic()
        assert window.topic_table.rowCount() == 1  # Repeated additions do not duplicate rows.
        window.topics = [dict(name='/manual_later', types=['std_msgs/msg/String'], publishers=1, available=True)]
        window._render_topics()
        assert window.topic_table.rowCount() == 1
        assert window.topic_table.item(0, 2).text() == 'std_msgs/msg/String'
        assert window.topic_table.cellWidget(0, 6).isEnabled()
        assert window.topic_table.item(0, 0).checkState() == Qt.Checked
        assert window.topic_count.text() == '显示 1/1 · 已勾选 1'
        window.topic_table.item(0, 0).setCheckState(Qt.Unchecked)
        window.topics = []
        window._render_topics()
        assert window.topic_table.item(0, 2).text() == '手动添加，未发现'
        assert window.topic_table.item(0, 0).checkState() == Qt.Unchecked
        assert '/manual_later' not in window.get_config().topics
        window.topics = [dict(name='/manual_later', types=['std_msgs/msg/String'], publishers=1, available=True)]
        window._render_topics()
        assert window.topic_table.item(0, 2).text() == 'std_msgs/msg/String'
        assert window.topic_table.item(0, 0).checkState() == Qt.Unchecked
    finally:
        window.close()


def test_reloading_profile_reuses_active_environment_even_during_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    requests, errors = [], []
    window._error = errors.append
    window._request_environment = lambda *args: requests.append(args)
    window.active_env = {'ROS_DOMAIN_ID': '0'}
    window.loaded_setup = '/workspace/install/setup.bash'
    window.active_workspace = '/workspace'
    window.active_packages = ['radar_msgs', 'lidar_msgs']
    profile = RecordConfig(all_topics=False, topics=['/sensor'], cache_mib=256,
                           environment_setup=window.loaded_setup, workspace='/workspace',
                           packages=['lidar_msgs', 'radar_msgs'])
    window.store.profiles['test'] = profile.to_dict()
    window._refresh_profile_names()
    window.discovery_busy = True
    try:
        for _ in range(2):
            window.cache.set_binary_value(512)
            window.load_profile_button.click()
            assert window.cache.binary_value() == 256
            assert window.load_profile_button.isEnabled()
            assert not window.env_busy
        assert not requests and not errors
        assert window.get_config().topics == ['/sensor']
    finally:
        window.close()


@pytest.mark.parametrize('fail_loading', [False, True])
def test_profile_load_disables_repeat_clicks_and_recovers_after_environment_task(
        tmp_path, monkeypatch, fail_loading):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    app.processEvents()
    window.active_env = window.base_env = {'test_environment': 'old'}
    window.loaded_setup = '/old/setup.bash'
    window._apply_capabilities = lambda: None
    errors, calls = [], []
    window._error = errors.append
    released = threading.Event()
    def load(setup, base):
        calls.append(setup)
        assert released.wait(5)
        if fail_loading:
            raise ValueError('模拟环境加载失败')
        return {'test_environment': 'new'}
    monkeypatch.setattr('ros2_recorder.app.load_environment', load)
    monkeypatch.setattr('ros2_recorder.app.validate_message_types', lambda *args: [])
    monkeypatch.setattr('ros2_recorder.app.detect_capabilities', lambda *args: None)
    profile = RecordConfig(all_topics=False, topics=['/sensor'], cache_mib=256,
                           environment_setup='/new/setup.bash')
    window.store.profiles['test'] = profile.to_dict()
    window._refresh_profile_names()
    try:
        window.load_profile_button.click()
        assert window.env_busy
        assert window.load_profile_button.text() == '加载中…'
        assert not window.load_profile_button.isEnabled()
        assert not window.load_profile_from_list_button.isEnabled()
        assert not window.profile_combo.isEnabled()
        window.load_profile_button.click()
        window.load_profile_from_list_button.click()
        assert not errors
        released.set()
        deadline = time.monotonic() + 5
        while window.tasks:
            app.processEvents()
            time.sleep(.01)
            assert time.monotonic() < deadline
        assert calls == ['/new/setup.bash']
        assert not window.env_busy
        assert window.load_profile_button.isEnabled()
        assert window.load_profile_from_list_button.isEnabled()
        assert window.profile_combo.isEnabled()
        assert window.load_profile_button.text() == '加载'
        if fail_loading:
            assert errors == ['模拟环境加载失败']
            assert window.loaded_setup == '/old/setup.bash'
        else:
            assert not errors and window.loaded_setup == '/new/setup.bash'
            window.load_profile_button.click()
            assert not window.env_busy
            assert calls == ['/new/setup.bash']
    finally:
        released.set()
        deadline = time.monotonic() + 5
        while window.tasks and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        window.close()


def test_reliability_column_and_recording_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    try:
        window.topics = [dict(name='/lidar', types=['sensor_msgs/msg/PointCloud2'],
                              publishers=2, available=True, qos='BEST_EFFORT, RELIABLE')]
        window.selected_topics = {'/lidar'}
        window._render_topics()
        assert window.topic_table.item(0, 4).text() == '混合'
        assert window.recording_reliability.currentData() == 'auto'
        window.recording_reliability.setCurrentIndex(window.recording_reliability.findData('reliable'))
        config = window.get_config()
        assert config.recording_reliability == 'reliable'
        window.apply_config(config, load_env=False)
        assert window.get_config().recording_reliability == 'reliable'
    finally:
        window.close()


def test_yaml_default_paths_enable_and_storage_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    try:
        window.selected_topics = {'/iv_points'}
        assert not window.qos_enabled.isChecked()
        assert not window.storage_config_enabled.isChecked()
        assert window.qos_path.text().endswith('/templates/qos.yaml')
        config = window.get_config()
        assert not config.qos_enabled and not config.storage_config_enabled
        window.apply_config(config, load_env=False)
        assert not window.qos_enabled.isChecked()
        window.storage.setCurrentText('mcap')
        assert window.storage_config.text().endswith('/templates/mcap.yaml')
        window.storage_config.setText('/custom/storage.yaml')
        window.storage.setCurrentText('sqlite3')
        assert window.storage_config.text() == '/custom/storage.yaml'
        window.qos_enabled.setChecked(True)
        assert window.get_config().qos_enabled
    finally:
        window.close()
