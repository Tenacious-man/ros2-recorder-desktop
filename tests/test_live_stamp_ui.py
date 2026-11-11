import os
import shlex
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QDialogButtonBox

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.communication import CommunicationSettings
from ros2_recorder.communication_dialog import CommunicationComparison, CommunicationDialog
from ros2_recorder.host_diagnostics import dds_profile, process_report
from ros2_recorder.live_stamp_page import LOCAL_REFERENCE


def pump(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline
    app.processEvents()


class FakeMonitor:
    def __init__(self, env, topics, *, on_snapshot, on_done, settings=None):
        self.topics = {}
        self.tokens = {}
        self.counter = self.generation = 0
        self.stopping = False
        self.started = False
        self.settings = settings
        self.on_snapshot, self.on_done = on_snapshot, on_done
        self.update(topics)

    def update(self, topics):
        if dict(topics) == self.topics:
            return
        tokens = {}
        for name, kind in topics.items():
            if self.topics.get(name) == kind:
                tokens[name] = self.tokens[name]
            else:
                self.counter += 1
                tokens[name] = self.counter
        self.topics, self.tokens = dict(topics), tokens
        self.generation += 1

    def start(self):
        self.started = True

    def stop(self, *, force=False):
        if not self.stopping:
            self.stopping = True
            QTimer.singleShot(0, lambda: self.on_done(0))


def make_window(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    monkeypatch.setattr('ros2_recorder.live_stamp_page.LatestStampMonitor', FakeMonitor)
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    window.auto_frequency.setChecked(False)
    window.active_env = {'ROS_DOMAIN_ID': '177'}
    window.nav.setCurrentRow(4)
    window.show()
    page = window.analysis_widget
    page.set_discovered_topics([{'name': name, 'types': [kind], 'available': True, 'publishers': 1}
                                for name, kind in [('/camera', 'sensor_msgs/msg/Image'),
                                                   ('/radar', 'sensor_msgs/msg/PointCloud2'),
                                                   ('/lidar', 'sensor_msgs/msg/PointCloud2')]])
    return app, window, page


def select(page, names):
    page.apply_selection(names)


def check_draft(dialog, name, checked):
    dialog.table.item(dialog.rows[name], 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)


def snapshot(monitor, values, *, age_ns=0, wall_times=None):
    now = time.monotonic_ns() - age_ns
    return {'generation': monitor.generation,
            'samples': {name: {'stamp_ns': value, 'status': 'ok' if value else 'invalid',
                               'error': '' if value else 'header.stamp 为零', 'received_ns': now,
                               'received_wall_ns': (wall_times or {}).get(name),
                               'received_count': 1, 'token': monitor.tokens[name]}
                        for name, value in values.items()}}


def test_live_selection_reference_sign_and_stale_last_arrivals(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        assert not page.start_button.isEnabled() and page.reference.count() == 1
        assert page.local_clock.text().startswith('本机当前时间（Unix 秒）')
        select(page, ['/camera', '/radar', '/lidar'])
        page.start_button.click()
        pump(app, lambda: page.monitor is not None and page.monitor.started)
        monitor = page.monitor
        assert window.frequency_suspended
        base = 1_793_581_200_123_456_789
        values = {'/camera': base, '/radar': base + 7_250_000, '/lidar': base - 3_500_000}
        wall_times = {'/camera': base + 1_125_000, '/radar': base + 5_000_000, '/lidar': base}
        monitor.on_snapshot(snapshot(monitor, values, wall_times=wall_times))
        app.processEvents()
        assert page.reference.currentData() == LOCAL_REFERENCE
        assert page.table.item(page.rows['/camera'], 2).text() == '1793581200.124581789'
        assert page.table.item(page.rows['/camera'], 3).text() == '-1.125'
        assert page.table.item(page.rows['/radar'], 3).text() == '+2.250'
        assert page.table.item(page.rows['/lidar'], 3).text() == '-3.500'
        assert page.table.item(page.rows['/camera'], 1).text() == '1793581200.123456789'
        page.reference.setCurrentIndex(page.reference.findData('/camera'))
        assert page.table.item(page.rows['/radar'], 3).text() == '+7.250'
        assert page.table.item(page.rows['/lidar'], 3).text() == '-3.500'
        page.reference.setCurrentIndex(page.reference.findData('/radar'))
        assert page.table.item(page.rows['/camera'], 3).text() == '-7.250'
        assert page.table.item(page.rows['/radar'], 3).text() == '0.000'
        assert page.table.item(page.rows['/lidar'], 3).text() == '-10.750'
        monitor.on_snapshot(snapshot(monitor, values, age_ns=3_000_000_000))
        app.processEvents()
        assert '未更新' in page.table.item(page.rows['/camera'], 4).text()
        # A reference arriving with a zero time invalidates all comparisons.
        monitor.on_snapshot(snapshot(monitor, dict(values, **{'/radar': 0}), wall_times=wall_times))
        app.processEvents()
        assert page.table.item(page.rows['/radar'], 1).text() == '0.000000000'
        assert all(page.table.item(row, 3).text() == '—' for row in page.rows.values())
        page.reference.setCurrentIndex(0)
        assert page.table.item(page.rows['/radar'], 2).text() == '1793581200.128456789'
        assert page.table.item(page.rows['/radar'], 3).text() == '—'
        errors = []
        window._error = errors.append
        window.start_recording()
        assert errors and window.session is None
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.search.setText('radar')
        assert dialog.table.isRowHidden(dialog.rows['/camera'])
        assert not page.table.isRowHidden(page.rows['/camera'])
        assert len(page.selected) == 3
        dialog.reject()
        page.stop_button.click()
        pump(app, lambda: not page.busy)
        assert not window.frequency_suspended
        assert page.samples['/camera']['stamp_ns'] == base
        assert page.table.item(page.rows['/camera'], 3).text() == '-1.125'
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_communication_settings_and_baseline_keep_each_runs_qos(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/radar'])
        dialog = CommunicationDialog(page, page.communication)
        dialog.form.use_lidar_preset()
        dialog.form.duration.setValue(1)
        dialog.form.run_note.setText('发布端 8 MiB')
        monkeypatch.setattr('ros2_recorder.live_stamp_page.CommunicationDialog', lambda *args: dialog)
        monkeypatch.setattr(dialog, 'exec_', lambda: dialog.Accepted)
        page.choose_communication()
        assert CommunicationSettings.load(page.communication_path) == dialog.settings()
        page.start()
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        assert monitor.settings.depth == 2 and monitor.settings.transport == 'shm' and monitor.settings.shm_mib == 32
        data = snapshot(monitor, {'/radar': 100_000_000_000})
        data['samples']['/radar'].update(seconds=.9, hz=9.8, max_gap_ms=104.3, age_p99_ms=57.1)
        page.accept_snapshot(monitor, data)
        pump(app, lambda: not page.busy)
        page.baseline_button.click()
        dialog.form.reliability.setCurrentIndex(dialog.form.reliability.findData('reliable'))
        page.choose_communication()
        page.start()
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        assert monitor.settings.reliability == 'reliable'
        data = snapshot(monitor, {'/radar': 102_000_000_000})
        data['samples']['/radar'].update(seconds=.9, hz=10.0)
        page.accept_snapshot(monitor, data)
        comparison = CommunicationComparison(page, page.baseline, page.current_run())
        assert 'Best Effort' in comparison.table.item(1, 0).text()
        assert 'Reliable' in comparison.table.item(1, 1).text()
        assert '发布端 8 MiB' in comparison.table.item(0, 0).text()
        assert comparison.table.item(8, 0).text() == '9.80 Hz'
        assert comparison.table.item(8, 1).text() == '10.00 Hz'
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_host_diagnostics_shows_running_process_and_graph_without_listening(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        xml = tmp_path / 'fastdds.xml'
        xml.write_text(CommunicationSettings(transport='shm', shm_mib=256).fastdds_xml())
        libraries = ['libfastrtps.so.2.6.12']
        processes = [
            (123, 'seyond_node', libraries, {}, {549408: 5, 33579552: 2}, '0-31', {}),
            (os.getpid(), 'zvlidar_node', libraries,
             {'FASTDDS_DEFAULT_PROFILES_FILE': str(xml), 'FASTRTPS_DEFAULT_PROFILES_FILE': str(xml)},
             {268460576: 1}, '0-31', {}),
        ]
        monkeypatch.setattr('ros2_recorder.host_diagnostics_dialog.host_report',
                            lambda env: ('ROS Domain ' + env['ROS_DOMAIN_ID'], process_report(processes)))
        requests = []

        def graph(env, *args):
            requests.append(args)
            topic = args[-1] or '/camera'
            qos = {'type': 'sensor_msgs/msg/Image', 'reliability': 'RELIABLE', 'history': 'UNKNOWN', 'depth': 0,
                   'durability': 'VOLATILE', 'deadline': 2**63 - 1, 'lifespan': 250_000_000,
                   'liveliness': 'AUTOMATIC', 'lease': 0}
            return {'rmw': 'rmw_fastrtps_cpp', 'names': ['/camera', '/lidar'], 'topic': topic,
                    'endpoints': [dict(qos, direction='发布', node=topic),
                                  dict(qos, direction='订阅', node='/recorder_probe_456')]}

        monkeypatch.setattr('ros2_recorder.host_diagnostics_dialog.graph_probe', graph)
        window._unlock_admin()
        window.nav.setCurrentRow(7)
        module = window.communication_widget
        module.tabs.setCurrentWidget(module.diagnostics)
        dialog = module.diagnostics
        pump(app, lambda: dialog.refresh_button.isEnabled())
        assert 'ROS Domain 177' in dialog.overview.toPlainText()
        process_text = dialog.processes.toPlainText()
        assert 'zvlidar_node' in process_text
        assert '启动环境未指定 Fast DDS XML' in process_text
        assert '5 段（每段 549.41 KB），2 段（每段 33.58 MB）' in process_text
        assert process_text.count('共享内存段 268.44 MB（256 MiB）') == 1
        assert '已映射共享内存段 · 1 段（每段 268.46 MB）' in process_text
        report = dialog.qos.toPlainText()
        assert '发布端（发送） · /camera' in report
        assert '订阅端（接收） · /recorder_probe_456' in report
        assert '历史与深度未上报' in report
        assert '更新时限 无限' not in report
        assert '有效期 250 ms' in report
        assert 'zvlidar_node' not in report
        assert '268.44 MB' not in report
        dialog.topic.setCurrentText('/lidar')
        pump(app, lambda: dialog.refresh_button.isEnabled())
        assert requests[-1] == ('qos', '--topic', '/lidar')
        assert '发布端（发送） · /lidar' in dialog.qos.toPlainText()
        dialog.processes_button.click()
        assert dialog.tabs.currentWidget() is dialog.processes
        dialog.copy_report()
        assert '话题 QoS · /lidar' in app.clipboard().text()
        assert page.monitor is None and not page.busy
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_admin_communication_page_deploys_and_revokes_account_defaults(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr('ros2_recorder.communication_dialog.host_limits', lambda: (32 * 1024**3, 208 * 1024))
        window._unlock_admin()
        window.nav.setCurrentRow(7)
        module = window.communication_widget
        assert module.isVisible()
        module.form.udp.set_binary_value(1024)
        assert not module.form.copy_linux.isHidden()
        module.form.copy_linux.click()
        assert app.clipboard().text() == 'sudo sysctl -w net.core.rmem_max=1048576'

        exported = tmp_path / 'Radar Fast DDS.xml'
        monkeypatch.setattr('ros2_recorder.communication_dialog.QFileDialog.getSaveFileName',
                            lambda *args: (str(exported), 'XML 文件 (*.xml)'))
        module.form.export_profile()
        assert '共享内存段 33.55 MB' in dds_profile(exported)
        assert '<segment_size>33554432</segment_size>' in exported.read_text()
        assert shlex.split(app.clipboard().text()) == [
            'export', f'FASTRTPS_DEFAULT_PROFILES_FILE={exported}', f'FASTDDS_DEFAULT_PROFILES_FILE={exported}',
        ]

        module.form.shm.set_binary_value(8)
        module.enable_button.click()
        assert module.profile.active()
        assert module.profile.settings().udp_kib == 1024
        assert '共享内存段 8.39 MB' in dds_profile(module.profile.xml_path)
        module.copy_button.click()
        assert shlex.split(app.clipboard().text())[1] == f'FASTRTPS_DEFAULT_PROFILES_FILE={module.profile.xml_path}'
        assert page.communication.transport == 'environment'
        module.disable_button.click()
        assert not module.profile.active()
        assert module.profile.xml_path.is_file()
        module.profile.env_path.write_text('# Existing external file\n')
        module.enable_button.click()
        assert module.profile.env_path.read_text() == '# Existing external file\n'
        assert '已有其他配置' in module.message.text()
        window._lock_admin()
        assert not module.isVisible() and window.nav.item(7).isHidden()
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_communication_startup_fields_export_and_persist(tmp_path, monkeypatch):
    app, window, _ = make_window(tmp_path, monkeypatch)
    try:
        window._unlock_admin()
        window.nav.setCurrentRow(7)
        module = window.communication_widget
        module.tabs.setCurrentIndex(1)
        form = module.startup_form
        profile = module.profile
        assert form.isVisible()
        assert not form.fields['FASTRTPS_DEFAULT_PROFILES_FILE'][0].isEnabled()

        setup = tmp_path / 'ROS install' / 'setup.bash'
        form.setup.setEditText(str(setup))
        form.fields['ROS_DOMAIN_ID'][0].setText('233')
        module.enable_button.click()
        assert '0～232' in module.message.text()
        assert not profile.env_path.exists()

        form.fields['ROS_DOMAIN_ID'][0].setText('17')
        exported = tmp_path / 'lidar config.xml'
        monkeypatch.setattr('ros2_recorder.communication_dialog.QFileDialog.getSaveFileName',
                            lambda *args: (str(exported), 'XML 文件 (*.xml)'))
        form.copy_button.click()
        assert shlex.split(app.clipboard().text()) == [
            'source', str(setup), '&&', 'export', 'ROS_DOMAIN_ID=17',
            f'FASTRTPS_DEFAULT_PROFILES_FILE={exported}', f'FASTDDS_DEFAULT_PROFILES_FILE={exported}',
        ]
        assert exported.is_file()
        assert not profile.env_path.exists()

        form.profile_source.setCurrentIndex(1)
        old_xml = form.fields['FASTRTPS_DEFAULT_PROFILES_FILE'][0]
        assert old_xml.isEnabled()
        old_xml.setText(str(exported))
        form.rmw.setEditText('rmw_fastrtps_cpp')
        form.fields['ROS_DISCOVERY_SERVER'][0].setText('192.168.1.50:11811;;192.168.1.52:11811')
        expected = form.settings()
        module.enable_button.click()
        assert profile.active()
        assert profile.startup_settings() == expected
        assert not profile.xml_path.exists()
        content = profile.env_path.read_text()
        assert 'ROS_DOMAIN_ID="17"\n' in content
        assert 'RMW_IMPLEMENTATION="rmw_fastrtps_cpp"\n' in content
        assert 'ROS_DISCOVERY_SERVER="192.168.1.50:11811;;192.168.1.52:11811"\n' in content
        assert f'FASTRTPS_DEFAULT_PROFILES_FILE="{exported}"\n' in content
        assert 'ROS_DISTRO=' not in content
        assert 'FASTDDS_DEFAULT_PROFILES_FILE=' not in content

        form.fields['ROS_DOMAIN_ID'][0].setText('18')
        module.copy_button.click()
        assert shlex.split(app.clipboard().text())[:4] == ['source', str(setup), '&&', 'export']
        assert 'ROS_DOMAIN_ID=17' in shlex.split(app.clipboard().text())
        module.disable_button.click()
        assert not profile.active()
        assert profile.startup_settings() == expected
        assert exported.is_file()
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_stale_generations_reference_removal_and_close_cleanup(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    select(page, ['/camera', '/radar'])
    page.start()
    pump(app, lambda: page.monitor is not None)
    monitor = page.monitor
    page.reference.setCurrentIndex(page.reference.findData('/camera'))
    previous = snapshot(monitor, {'/camera': 100_000_000_000, '/radar': 100_001_000_000})
    page.accept_snapshot(monitor, previous)
    select(page, ['/radar'])
    assert page.reference.currentData() == LOCAL_REFERENCE
    select(page, ['/radar', '/camera'])
    assert '/camera' not in page.samples
    page.accept_snapshot(monitor, previous)
    assert '/camera' not in page.samples, 'A late snapshot must not restore a removed subscription'
    assert page.table.item(page.rows['/camera'], 1).text() == '—'
    window.close()
    pump(app, lambda: not window.isVisible())
    assert monitor.stopping and not page.busy
    assert not page.timer.isActive()


def test_discovery_and_recording_interlock(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    monkeypatch.setattr('ros2_recorder.live_stamp_page.discover_topics', lambda env: [
        {'name': '/new_camera', 'types': ['sensor_msgs/msg/Image'], 'publishers': 1, 'available': True}])
    try:
        select(page, ['/camera'])
        page.refresh_topics()
        pump(app, lambda: not page.discovering)
        assert '/new_camera' in page.catalog and '/new_camera' not in page.rows and '/camera' in page.selected
        class Session:
            finished = False
        window.session = Session()
        page.start()
        assert not page.busy and '录制' in page.status.text()
        window.session = None
        select(page, ['/new_camera'])
        # Cancel before the preparation thread has returned.
        page.start()
        page.cancel()
        pump(app, lambda: not page.busy and not window.tasks)
        assert page.monitor is None and not window.frequency_suspended
    finally:
        window.session = None
        window.close()
        pump(app, lambda: not window.isVisible())


def test_topic_dialog_confirm_cancel_and_only_selected_main_rows(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        assert len(page.catalog) == 3 and page.table.rowCount() == 0
        assert page.result_stack.currentWidget() is page.empty_hint
        page.select_button.click()
        dialog = page.topic_dialog
        assert dialog.isVisible() and dialog.table.rowCount() == 3
        check_draft(dialog, '/camera', True)
        check_draft(dialog, '/radar', True)
        dialog.search.setText('radar')
        assert '筛选结果外' in dialog.count.text()
        assert page.selected == {} and page.table.rowCount() == 0
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        app.processEvents()
        assert not dialog.isVisible()
        assert set(page.rows) == {'/camera', '/radar'} and page.table.rowCount() == 2
        assert not page.table.item(page.rows['/camera'], 0).flags() & Qt.ItemIsUserCheckable
        assert page.reference.currentData() == LOCAL_REFERENCE
        assert page.result_stack.currentWidget() is page.table
        page.select_button.click()
        dialog = page.topic_dialog
        assert dialog.draft == {'/camera', '/radar'}
        check_draft(dialog, '/camera', False)
        check_draft(dialog, '/lidar', True)
        dialog.buttons.button(QDialogButtonBox.Cancel).click()
        assert not dialog.isVisible()
        assert set(page.rows) == {'/camera', '/radar'}
        assert page.reference.currentData() == LOCAL_REFERENCE
        page.set_discovered_topics(list(page.catalog.values()) + [
            {'name': '/unwanted', 'types': ['std_msgs/msg/String'], 'publishers': 1, 'available': True}])
        assert '/unwanted' in page.catalog and '/unwanted' not in page.rows
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_live_dialog_changes_are_atomic_and_preserve_kept_samples(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/camera', '/radar'])
        page.start()
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        page.reference.setCurrentIndex(page.reference.findData('/radar'))
        page.accept_snapshot(monitor, snapshot(monitor, {'/camera': 100_000_000_000, '/radar': 100_007_250_000}))
        generation = monitor.generation
        radar_token = monitor.tokens['/radar']
        page.choose_topics()
        dialog = page.topic_dialog
        check_draft(dialog, '/camera', False)
        check_draft(dialog, '/lidar', True)
        assert monitor.generation == generation and set(monitor.topics) == {'/camera', '/radar'}
        page.accept_snapshot(monitor, snapshot(monitor, {'/camera': 101_000_000_000, '/radar': 101_007_250_000}))
        assert page.samples['/camera']['stamp_ns'] == 101_000_000_000
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert page.monitor is monitor and monitor.started and not monitor.stopping
        assert set(monitor.topics) == set(page.rows) == {'/radar', '/lidar'}
        assert monitor.tokens['/radar'] == radar_token
        assert page.samples['/radar']['stamp_ns'] == 101_007_250_000
        assert '/camera' not in page.samples and '/lidar' not in page.samples
        assert page.reference.currentData() == '/radar'
        page.choose_topics()
        dialog = page.topic_dialog
        for name in list(dialog.draft):
            check_draft(dialog, name, False)
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert page.table.rowCount() == 0 and monitor.topics == {} and page.samples == {}
        assert page.reference.count() == 1
        assert page.result_stack.currentWidget() is page.empty_hint
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_refresh_in_dialog_preserves_draft_and_never_applies_it_early(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/camera'])
        page.choose_topics()
        dialog = page.topic_dialog
        check_draft(dialog, '/lidar', True)
        dialog.search.setText('lidar')
        monkeypatch.setattr('ros2_recorder.live_stamp_page.discover_topics', lambda env: [
            {'name': '/new', 'types': ['sensor_msgs/msg/Imu'], 'publishers': 1, 'available': True}])
        dialog.refresh_button.click()
        pump(app, lambda: not page.discovering)
        assert dialog.draft == {'/camera', '/lidar'}
        assert '/lidar' in dialog.rows and '/new' in dialog.rows
        assert dialog.search.text() == 'lidar'
        assert set(page.rows) == {'/camera'}
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert set(page.rows) == {'/camera', '/lidar'}
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_message_type_filter_combines_with_search_and_keeps_checked_topics(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        page.set_discovered_topics(list(page.catalog.values()) + [
            {'name': '/camera/compressed', 'types': ['sensor_msgs/msg/CompressedImage'], 'publishers': 1, 'available': True},
            {'name': '/radar/custom', 'types': ['radar_msgs/msg/RadarScan'], 'publishers': 1, 'available': True}])
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData('sensor_msgs/msg/Image'))
        assert {name for name, row in dialog.rows.items() if not dialog.table.isRowHidden(row)} == {'/camera'}
        check_draft(dialog, '/camera', True)
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData('sensor_msgs/msg/CompressedImage'))
        assert {name for name, row in dialog.rows.items() if not dialog.table.isRowHidden(row)} == {'/camera/compressed'}
        assert dialog.draft == {'/camera'}
        point_type = 'sensor_msgs/msg/PointCloud2'
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData(point_type))
        assert {name for name, row in dialog.rows.items() if not dialog.table.isRowHidden(row)} == {'/lidar', '/radar'}
        assert dialog.draft == {'/camera'} and '筛选结果外' in dialog.count.text()
        dialog.search.setText('RADAR')
        assert {name for name, row in dialog.rows.items() if not dialog.table.isRowHidden(row)} == {'/radar'}
        check_draft(dialog, '/radar', True)
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData('radar_msgs/msg/RadarScan'))
        assert {name for name, row in dialog.rows.items() if not dialog.table.isRowHidden(row)} == {'/radar/custom'}
        check_draft(dialog, '/radar/custom', True)
        assert page.selected == {}, 'Changing filters must not apply subscriptions'
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert set(page.rows) == {'/camera', '/radar', '/radar/custom'}
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_type_filter_survives_discovery_refresh_without_resetting_to_all(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/camera'])
        page.choose_topics()
        dialog = page.topic_dialog
        point_type = 'sensor_msgs/msg/PointCloud2'
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData(point_type))
        monkeypatch.setattr('ros2_recorder.live_stamp_page.discover_topics', lambda env: [page.catalog['/camera']])
        dialog.refresh_button.click()
        pump(app, lambda: not page.discovering)
        assert dialog.type_filter.currentData() == point_type
        assert '当前未发现' in dialog.type_filter.currentText()
        assert all(dialog.table.isRowHidden(row) for row in dialog.rows.values())
        assert dialog.draft == {'/camera'}
        assert set(page.rows) == {'/camera'}
        page.set_discovered_topics(list(page.catalog.values()) + [
            {'name': '/new_lidar', 'types': [point_type], 'publishers': 1, 'available': True}])
        assert dialog.type_filter.currentData() == point_type
        assert not dialog.table.isRowHidden(dialog.rows['/new_lidar'])
        dialog.type_filter.setCurrentIndex(0)
        assert all(not dialog.table.isRowHidden(row) for row in dialog.rows.values())
        dialog.reject()
        assert set(page.rows) == {'/camera'}
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_bulk_topic_selection_only_changes_filtered_results_and_requires_confirmation(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/camera'])
        page.start()
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        generation = monitor.generation
        page.set_discovered_topics(list(page.catalog.values()) + [
            {'name': '/conflict', 'types': ['sensor_msgs/msg/Image', 'sensor_msgs/msg/Imu'], 'publishers': 2}])
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.type_filter.setCurrentIndex(dialog.type_filter.findData('sensor_msgs/msg/PointCloud2'))
        dialog.search.setText('radar')
        dialog.select_all_button.click()
        assert dialog.draft == {'/camera', '/radar'}
        assert dialog.table.item(dialog.rows['/camera'], 0).checkState() == Qt.Checked
        assert dialog.table.item(dialog.rows['/lidar'], 0).checkState() == Qt.Unchecked
        assert dialog.table.item(dialog.rows['/conflict'], 0).checkState() == Qt.Unchecked
        assert monitor.generation == generation and set(page.selected) == {'/camera'}
        dialog.clear_all_button.click()
        assert dialog.draft == {'/camera'}, 'Clearing filtered results must keep hidden selections'
        dialog.search.clear()
        dialog.select_all_button.click()
        assert dialog.draft == {'/camera', '/radar', '/lidar'}
        dialog.clear_all_button.click()
        assert dialog.draft == {'/camera'}
        dialog.search.setText('no_results')
        assert not dialog.select_all_button.isEnabled() and not dialog.clear_all_button.isEnabled()
        dialog.reject()
        assert set(page.selected) == set(monitor.topics) == {'/camera'}
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.select_all_button.click()
        assert '已跳过 1 个' in dialog.notice.text()
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert set(page.rows) == set(monitor.topics) == {'/camera', '/radar', '/lidar'}
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.clear_all_button.click()
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert page.selected == {} and monitor.topics == {} and page.table.rowCount() == 0
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_row_drag_keeps_latest_samples_reference_and_subscriptions(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        select(page, ['/camera', '/lidar', '/radar'])
        page.start()
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        page.reference.setCurrentIndex(page.reference.findData('/camera'))
        values = {'/camera': 100_000_000_000, '/lidar': 99_996_500_000, '/radar': 100_007_250_000}
        page.accept_snapshot(monitor, snapshot(monitor, values))
        generation, tokens = monitor.generation, dict(monitor.tokens)
        assert page.table.dragEnabled() and page.table.acceptDrops()

        class Drop:
            accepted = False

            def source(self):
                return page.table

            def pos(self):
                # Drop the first row onto the middle of the second row.
                return page.table.visualRect(page.table.model().index(1, 0)).center()

            def setDropAction(self, action):
                assert action == Qt.MoveAction

            def accept(self):
                self.accepted = True

            def ignore(self):
                raise AssertionError('An internal row drop should be accepted')

        drop = Drop()

        class Drag:
            def __init__(self, source):
                assert source is page.table

            def setMimeData(self, data):
                assert data.hasFormat('application/x-qabstractitemmodeldatalist')

            def setPixmap(self, pixmap):
                pass

            def setHotSpot(self, point):
                pass

            def exec_(self, *actions):
                # New data may arrive while Qt's native drag event loop runs.
                page.accept_snapshot(monitor, snapshot(monitor, {name: value + 1_000_000_000 for name, value in values.items()}))
                page.table.dropEvent(drop)
                return Qt.MoveAction

            def deleteLater(self):
                pass

        monkeypatch.setattr('ros2_recorder.live_stamp_page.QDrag', Drag)
        page.table.selectRow(0)
        page.table.startDrag(Qt.MoveAction)
        assert drop.accepted and page.table.dragged_topic is None
        assert page.table.rowCount() == 3
        order = ['/lidar', '/camera', '/radar']
        assert list(page.selected) == list(page.rows) == order
        assert page.reference.currentData() == '/camera'
        assert monitor.generation == generation and monitor.tokens == tokens
        assert page.monitor is monitor and not monitor.stopping
        assert page.table.item(page.rows['/camera'], 1).text() == '101.000000000'
        assert page.table.item(page.rows['/lidar'], 3).text() == '-3.500'
        assert page.table.item(page.rows['/radar'], 3).text() == '+7.250'
        page.render_samples()
        page.set_discovered_topics(list(page.catalog.values()) + [
            {'name': '/a_new_camera', 'types': ['sensor_msgs/msg/Image'], 'publishers': 1, 'available': True}])
        assert list(page.rows) == order
        page.choose_topics()
        dialog = page.topic_dialog
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert list(page.rows) == order
        assert monitor.generation == generation
        page.choose_topics()
        dialog = page.topic_dialog
        check_draft(dialog, '/a_new_camera', True)
        dialog.buttons.button(QDialogButtonBox.Ok).click()
        assert list(page.rows) == order + ['/a_new_camera']
        assert page.reference.currentData() == '/camera'
        assert page.samples['/camera']['stamp_ns'] == 101_000_000_000
        # Moving upwards and dropping past the last row also keeps whole rows.
        page.table.move_row(3, 0)
        assert list(page.rows) == ['/a_new_camera'] + order
        page.table.move_row(0, 3)
        assert list(page.rows) == order + ['/a_new_camera']
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())
