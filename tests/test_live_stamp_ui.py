import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QDialogButtonBox

from ros2_recorder.app import MainWindow, configure_style


def pump(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline
    app.processEvents()


class FakeMonitor:
    def __init__(self, env, topics, *, on_snapshot, on_done):
        self.topics = {}
        self.tokens = {}
        self.counter = self.generation = 0
        self.stopping = False
        self.started = False
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


def snapshot(monitor, values, *, age_ns=0):
    now = time.monotonic_ns() - age_ns
    return {'generation': monitor.generation,
            'samples': {name: {'stamp_ns': value, 'status': 'ok' if value else 'invalid',
                               'error': '' if value else 'header.stamp 为零', 'received_ns': now,
                               'received_count': 1, 'token': monitor.tokens[name]}
                        for name, value in values.items()}}


def test_live_selection_reference_sign_and_stale_last_arrivals(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        assert not page.start_button.isEnabled() and page.reference.count() == 0
        select(page, ['/camera', '/radar', '/lidar'])
        page.start_button.click()
        pump(app, lambda: page.monitor is not None and page.monitor.started)
        monitor = page.monitor
        assert window.frequency_suspended
        base = 1_793_581_200_123_456_789
        values = {'/camera': base, '/radar': base + 7_250_000, '/lidar': base - 3_500_000}
        monitor.on_snapshot(snapshot(monitor, values))
        app.processEvents()
        assert page.table.item(page.rows['/radar'], 2).text() == '+7.250'
        assert page.table.item(page.rows['/lidar'], 2).text() == '-3.500'
        assert page.table.item(page.rows['/camera'], 1).text() == '1793581200.123456789'
        page.reference.setCurrentIndex(page.reference.findData('/radar'))
        assert page.table.item(page.rows['/camera'], 2).text() == '-7.250'
        assert page.table.item(page.rows['/radar'], 2).text() == '0.000'
        assert page.table.item(page.rows['/lidar'], 2).text() == '-10.750'
        monitor.on_snapshot(snapshot(monitor, values, age_ns=3_000_000_000))
        app.processEvents()
        assert '未更新' in page.table.item(page.rows['/camera'], 3).text()
        # A reference arriving with a zero time invalidates all comparisons.
        monitor.on_snapshot(snapshot(monitor, dict(values, **{'/radar': 0})))
        app.processEvents()
        assert page.table.item(page.rows['/radar'], 1).text() == '0.000000000'
        assert all(page.table.item(row, 2).text() == '—' for row in page.rows.values())
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
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_stale_generations_reference_removal_and_close_cleanup(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    select(page, ['/camera', '/radar'])
    page.start()
    pump(app, lambda: page.monitor is not None)
    monitor = page.monitor
    previous = snapshot(monitor, {'/camera': 100_000_000_000, '/radar': 100_001_000_000})
    page.accept_snapshot(monitor, previous)
    select(page, ['/radar'])
    assert page.reference.currentData() == '/radar'
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
        assert page.reference.currentData() == '/camera'
        assert page.result_stack.currentWidget() is page.table
        page.select_button.click()
        dialog = page.topic_dialog
        assert dialog.draft == {'/camera', '/radar'}
        check_draft(dialog, '/camera', False)
        check_draft(dialog, '/lidar', True)
        dialog.buttons.button(QDialogButtonBox.Cancel).click()
        assert not dialog.isVisible()
        assert set(page.rows) == {'/camera', '/radar'}
        assert page.reference.currentData() == '/camera'
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
        assert page.reference.count() == 0
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
        assert page.table.item(page.rows['/lidar'], 2).text() == '-3.500'
        assert page.table.item(page.rows['/radar'], 2).text() == '+7.250'
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
