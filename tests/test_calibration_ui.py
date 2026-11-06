import os
import time
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5.QtCore import QPoint, QPointF, QTimer, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QCheckBox, QPushButton, QLabel

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.calibration_math import load_ini, save_ini, transform_matrix
from ros2_recorder.calibration_cloud import preview_cloud
from test_calibration import make_cloud


class FakeMonitor:
    def __init__(self, env, settings, *, on_snapshot, on_done):
        self.env = env
        self.settings = settings
        self.stopping = False
        self.generation = 1
        self.started = False
        self.on_snapshot, self.on_done = on_snapshot, on_done

    def start(self):
        self.started = True

    def update(self, settings):
        if settings["topics"] != self.settings["topics"]:
            self.generation += 1
        self.settings = settings

    def stop(self, *, force=False):
        if not self.stopping:
            self.stopping = True
            QTimer.singleShot(0, lambda: self.on_done(0))

    def wait(self, timeout=None):
        return True


def pump(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline
    app.processEvents()


CATALOG = [{"name": name, "types": [kind]} for name, kind in (
    ("/lidar", "sensor_msgs/msg/PointCloud2"), ("/radar", "sensor_msgs/msg/PointCloud2"),
    ("/camera", "sensor_msgs/msg/Image"), ("/ros2_recorder/calibration/input", "sensor_msgs/msg/PointCloud2"))]


def make_window(tmp_path, monkeypatch, *, select=True):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_initialize_environment", lambda *args: None)
    monkeypatch.setattr("ros2_recorder.calibration_page.CalibrationMonitor", FakeMonitor)
    monkeypatch.setattr("ros2_recorder.calibration_page.discover_topics", lambda _env: CATALOG)
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    window.auto_frequency.setChecked(False)
    window.active_env = {"ROS_DOMAIN_ID": "181"}
    window._unlock_admin()
    window.nav.setCurrentRow(6)
    window.show()
    app.processEvents()
    page = window.calibration_widget
    assert page is not None
    pump(app, lambda: page.topics["input"].count() == 3 and not page.discovering)
    if select:
        page.topics["reference"].setEditText("/lidar")
        page.topics["input"].setEditText("/radar")
    return app, window, page


def test_automatic_subscription_cached_adjust_pick_and_permission_cleanup(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        assert page.topics["input"].count() == 3
        pump(app, lambda: page.monitor is not None and page.monitor.started)
        monitor = page.monitor
        assert window.frequency_suspended
        assert "标定订阅" in window._busy_message()
        errors = []
        window._error = errors.append
        window.start_recording()
        assert errors and window.session is None
        cloud = preview_cloud(make_cloud())
        for role in ("reference", "input"):
            page.accept_snapshot(monitor, {"generation": 1, "role": role, "cloud": cloud})
        app.processEvents()
        assert "未做时间配对" in page.sample_info.text()
        replacement = dict(cloud, xyz=cloud["xyz"] + 100)
        # No further frames arrive after the external source is paused.
        page.axes["input"][3].setValue(1.)
        np.testing.assert_allclose(page.view.transformed_points("input"), cloud["xyz"] + [1, 0, 0])
        page.axes["input"][3].setValue(2.)
        np.testing.assert_allclose(page.view.transformed_points("input"), cloud["xyz"] + [2, 0, 0])
        page.layer_checks["reference"].setChecked(False)
        screen, _ = page.view._project(page.view.transformed_points("input"))
        page.view.pick_point(QPoint(round(screen[0, 0]), round(screen[0, 1])))
        assert page.view.selection is not None
        assert "intensity" in page.properties.toPlainText() and "变换后 XYZ" in page.properties.toPlainText()
        assert monitor.settings == {"topics": {"reference": "/lidar", "input": "/radar"}}
        window._lock_admin()
        pump(app, lambda: not page.busy)
        assert monitor.stopping and not window.frequency_suspended
        assert all(window.nav.item(i).isHidden() for i in window.admin_rows)
        page.reconcile_subscription()
        assert page.monitor is None
        window._unlock_admin()
        assert page.topics["input"].isEnabled() and not page.active
        # Queued frames from an older process cannot overwrite the retained cloud.
        page.accept_snapshot(monitor, {"generation": 1, "role": "input", "cloud": replacement})
        np.testing.assert_array_equal(page.view.clouds["input"]["xyz"], cloud["xyz"])
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_ini_base_delta_relative_export_and_close_while_subscribing(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        source = tmp_path / "base.ini"
        save_ini(source, transform_matrix(x=1))
        page.load_path("input", source)
        assert page.axes["input"][3].value() == 0
        page.axes["input"][2].setValue(90)
        page.axes["reference"][3].setValue(2)
        output = tmp_path / "output.ini"
        page.save_path("input", output)
        np.testing.assert_allclose(load_ini(output)[:3, 3], [0, 1, 0], atol=1e-10)
        np.testing.assert_allclose(page.relative_matrix()[:3, 3], [-2, 1, 0], atol=1e-10)
        page.reset_adjustment("input")
        np.testing.assert_allclose(page.states["input"].matrix[:3, 3], [1, 0, 0])
        page.reset_identity("input")
        np.testing.assert_allclose(page.states["input"].matrix, np.eye(4))
        pump(app, lambda: page.monitor is not None)
        monitor = page.monitor
        window.close()
        pump(app, lambda: not window.isVisible())
        assert monitor.stopping and page.monitor is None
    finally:
        window.close()


def test_native_window_camera_orbit_pan_and_zoom(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        view = page.view
        start = view.mapTo(window, view.rect().center())
        native = window.windowHandle()
        old_yaw = view.yaw
        QTest.mousePress(native, Qt.LeftButton, pos=start)
        QTest.mouseMove(native, start + QPoint(40, 30))
        QTest.mouseRelease(native, Qt.LeftButton, pos=start + QPoint(40, 30))
        assert view.yaw != old_yaw
        center = view.center.copy()
        QTest.mousePress(native, Qt.RightButton, pos=start)
        QTest.mouseMove(native, start + QPoint(40, 30))
        QTest.mouseRelease(native, Qt.RightButton, pos=start + QPoint(40, 30))
        assert not np.array_equal(view.center, center)
        distance = view.distance
        event = QWheelEvent(QPointF(start), QPointF(window.mapToGlobal(start)), QPoint(), QPoint(0, 120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        app.sendEvent(native, event)
        assert view.distance < distance
    finally:
        window.close()


def test_background_and_manual_discovery_then_live_topic_selection(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch, select=False)
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def delayed_discovery(env):
        calls.append(env)
        entered.set()
        assert release.wait(3)
        return CATALOG + [{"name": "/late", "types": ["sensor_msgs/msg/PointCloud2"]}]

    monkeypatch.setattr("ros2_recorder.calibration_page.discover_topics", delayed_discovery)
    try:
        texts = {widget.text() for widget in page.findChildren(QPushButton) + page.findChildren(QCheckBox) + page.findChildren(QLabel)}
        assert not {"开始订阅", "停止", "冻结预览", "发布预览点云和外参", "目标坐标系"} & texts
        assert page.findChild(QLabel, "pageTitle").text() == "点云标定"
        assert page.monitor is None and page.topics["input"].currentText() == ""
        page.refresh_timer.setInterval(30)
        pump(app, entered.is_set)
        page.refresh_timer.stop()
        assert page.discovering and page.refresh_button.text() == "刷新"
        page.refresh_button.click()
        assert page.refresh_button.text() == "正在刷新…" and len(calls) == 1
        release.set()
        pump(app, lambda: not page.discovering and page.topics["input"].findText("/late") >= 0)
        assert page.topics["input"].currentText() == "" and page.monitor is None

        reference = page.topics["reference"]
        reference.setCurrentIndex(reference.findText("/lidar"))
        reference.activated.emit(reference.currentIndex())
        pump(app, lambda: page.monitor is not None and page.monitor.started)
        monitor = page.monitor
        assert monitor.settings["topics"] == {"reference": "/lidar", "input": ""}
        page.topics["input"].setEditText("/radar")
        pump(app, lambda: monitor.settings["topics"]["input"] == "/radar")
        generation = monitor.generation
        cloud = preview_cloud(make_cloud())
        page.accept_snapshot(monitor, {"generation": generation, "role": "input", "cloud": cloud})
        assert "input" in page.view.clouds and page.topics["input"].isEnabled()

        page.topics["input"].setEditText("/late")
        page.accept_snapshot(monitor, {"generation": generation, "role": "input", "cloud": cloud})
        assert "input" not in page.view.clouds
        pump(app, lambda: monitor.settings["topics"]["input"] == "/late")
        assert page.monitor is monitor and monitor.generation > generation
        page.accept_snapshot(monitor, {"generation": generation, "role": "input", "cloud": cloud})
        assert "input" not in page.view.clouds
        page.accept_snapshot(monitor, {"generation": monitor.generation, "role": "input", "cloud": cloud})
        page.refresh_button.click()
        pump(app, lambda: not page.discovering)
        assert page.topics["input"].currentText() == "/late"
        assert page.monitor is monitor and page.view.clouds["input"] is cloud

        reference.lineEdit().clear()
        pump(app, lambda: monitor.settings["topics"]["reference"] == "")
        assert page.monitor is monitor
        page.topics["input"].lineEdit().clear()
        pump(app, lambda: page.monitor is None and not page.busy)
        assert monitor.stopping and not window.frequency_suspended
    finally:
        release.set()
        window.close()
        pump(app, lambda: not window.isVisible())


def test_navigation_stops_subscription_and_return_resubscribes(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch)
    try:
        pump(app, lambda: page.monitor is not None)
        first = page.monitor
        cloud = preview_cloud(make_cloud())
        page.accept_snapshot(first, {"generation": first.generation, "role": "input", "cloud": cloud})
        window.nav.setCurrentRow(0)
        pump(app, lambda: not page.busy)
        assert first.stopping and not page.active and not page.refresh_timer.isActive()
        assert not window.frequency_suspended and not window._busy_message()
        assert page.view.clouds["input"] is cloud
        window.active_env = {"ROS_DOMAIN_ID": "182"}
        window.nav.setCurrentRow(6)
        pump(app, lambda: page.monitor is not None)
        assert page.monitor is not first and page.monitor.env == window.active_env
        assert not page.view.clouds  # Do not present another Domain's cached data as current.
        page.accept_snapshot(first, {"generation": first.generation, "role": "input", "cloud": cloud})
        assert not page.view.clouds
        second = page.monitor
        window._lock_admin()
        pump(app, lambda: not page.busy)
        assert second.stopping and not page.refresh_timer.isActive()
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_leaving_and_returning_while_subscription_prepares(tmp_path, monkeypatch):
    app, window, page = make_window(tmp_path, monkeypatch, select=False)
    entered, release = threading.Event(), threading.Event()
    original_pause = window._pause_frequency_for_recording
    calls = []

    class WaitingMonitor:
        def wait(self, _timeout):
            entered.set()
            return release.wait(3)

    def pause():
        original_pause()
        calls.append(True)
        return WaitingMonitor() if len(calls) == 1 else None

    monkeypatch.setattr(window, "_pause_frequency_for_recording", pause)
    try:
        page.topics["reference"].setEditText("/lidar")
        pump(app, entered.is_set)
        assert page.preparing and page.monitor is None
        window.nav.setCurrentRow(0)
        window.nav.setCurrentRow(6)
        release.set()
        pump(app, lambda: page.monitor is not None)
        assert len(calls) == 2 and page.monitor.settings["topics"]["reference"] == "/lidar"
        assert window.frequency_suspended
    finally:
        release.set()
        window.close()
        pump(app, lambda: not window.isVisible())
