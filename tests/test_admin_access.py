import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from ros2_recorder.admin_access import AdminCredentials
from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.device_timing import PortState, ServiceState


def test_password_storage_and_change(tmp_path):
    store = AdminCredentials(tmp_path / "admin.json")
    assert not store.configured
    with pytest.raises(ValueError):
        store.setup("short")
    assert not store.configured
    store.setup("first-password")
    assert store.configured
    assert "first-password" not in store.path.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.verify("first-password")
    assert not store.verify("wrong-password")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.setup("replacement")
    with pytest.raises(ValueError):
        store.change("wrong-password", "replacement")
    with pytest.raises(ValueError):
        store.change("first-password", "short")
    assert store.path.read_bytes() == before
    store.change("first-password", "second-password")
    assert not store.verify("first-password")
    assert store.verify("second-password")
    second = AdminCredentials(tmp_path / "second.json")
    second.setup("second-password")
    assert json.loads(store.path.read_text())["salt"] != json.loads(second.path.read_text())["salt"]


@pytest.mark.parametrize("content", ["broken", "[]", '{"version": 2}',
    '{"version":1,"algorithm":"pbkdf2-sha256","iterations":999999999,"salt":"00","digest":"00"}'])
def test_damaged_credentials_do_not_allow_new_setup(tmp_path, content):
    store = AdminCredentials(tmp_path / "admin.json")
    store.path.write_text(content)
    assert store.configured
    with pytest.raises(ValueError):
        store.verify("password")
    with pytest.raises(ValueError):
        store.setup("replacement")
    assert store.path.read_text() == content


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(MainWindow, "_initialize_environment", lambda *args: None)
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    widget = MainWindow(auto_discover=False)
    widget.timer.stop()
    widget.show()
    app.processEvents()
    yield widget
    if widget.admin_dialog is not None:
        widget.admin_dialog.reject()
    widget.close()
    app.processEvents()


def clicks(widget, count):
    for _ in range(count):
        QTest.mouseClick(widget, Qt.LeftButton)


def test_six_clicks_timeout_other_click_and_double_click(window, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("ros2_recorder.admin_ui.time.monotonic", lambda: clock[0])
    clicks(window.brand, 5)
    assert window.admin_dialog is None
    clock[0] += 2
    clicks(window.brand, 1)
    assert window.admin_dialog is None
    clicks(window.brand, 4)
    QTest.mouseClick(window.header_env, Qt.LeftButton)
    clicks(window.brand, 1)
    assert window.admin_dialog is None
    # One initial press + a double-click event represents two physical presses.
    QTest.mouseClick(window.header_env, Qt.LeftButton)
    for _ in range(3):
        QTest.mouseClick(window.brand, Qt.LeftButton)
        QTest.mouseDClick(window.brand, Qt.LeftButton)
    assert window.admin_dialog.mode == "setup"
    assert window.stack.count() == 5
    window.admin_dialog.reject()
    assert not window.admin_unlocked
    clicks(window.brand, 5)
    assert window.admin_dialog is None
    clicks(window.brand, 1)
    assert window.admin_dialog.mode == "setup"


def test_native_window_mouse_dispatch_opens_admin_after_six_clicks(window):
    def native_click(target):
        position = target.mapTo(window, target.rect().center())
        QTest.mouseClick(window.windowHandle(), Qt.LeftButton, pos=position)

    for _ in range(5):
        native_click(window.brand)
    assert window.admin_dialog is None
    native_click(window.header_env)
    native_click(window.brand)
    assert window.brand._clicks == 1 and window.admin_dialog is None
    for _ in range(4):
        native_click(window.brand)
    assert window.admin_dialog is None
    native_click(window.brand)
    assert window.admin_dialog is not None
    assert window.admin_dialog.mode == "setup"


def test_setup_unlock_lock_change_and_restart(window):
    basic_names = [window.nav.item(i).text() for i in range(5)]
    clicks(window.brand, 6)
    dialog = window.admin_dialog
    dialog.new_password.setText("first-password")
    dialog.confirm_password.setText("different-password")
    dialog._submit()
    assert not window.admin_unlocked and not window.admin_credentials.configured
    dialog.confirm_password.setText("first-password")
    dialog._submit()
    assert window.admin_unlocked
    assert dialog.new_password.text() == ""
    assert [window.nav.item(i).text() for i in range(5)] == basic_names
    assert [window.nav.item(i).text() for i in range(5, 9)] == ["设备授时", "点云标定", "通信环境", "管理员设置"]
    assert window.nav.currentRow() == 8
    window.nav.setCurrentRow(5)
    window._lock_admin()
    assert window.nav.currentRow() == 0
    assert all(window.nav.item(i).isHidden() for i in range(5, 9))
    assert all(not window.stack.widget(i).isEnabled() for i in range(5, 9))
    window.nav.setCurrentRow(6)
    assert window.stack.currentIndex() == 0
    window._show_admin_password("change")
    assert window.admin_dialog is None
    clicks(window.brand, 6)
    dialog = window.admin_dialog
    assert dialog.mode == "unlock"
    dialog.current_password.setText("incorrect")
    dialog._submit()
    assert not window.admin_unlocked and dialog.error.text()
    dialog.current_password.setText("first-password")
    dialog._submit()
    assert window.admin_unlocked and window.nav.count() == 9
    window._show_admin_password("change")
    dialog = window.admin_dialog
    dialog.current_password.setText("incorrect")
    dialog.new_password.setText("second-password")
    dialog.confirm_password.setText("second-password")
    dialog._submit()
    assert dialog.error.text() and window.admin_credentials.verify("first-password")
    dialog.current_password.setText("first-password")
    dialog._submit()
    assert window.admin_credentials.verify("second-password")
    assert not window.admin_credentials.verify("first-password")
    other = MainWindow(auto_discover=False)
    other.timer.stop()
    try:
        assert not other.admin_unlocked and other.nav.count() == other.stack.count() == 5
        assert other.admin_credentials.verify("second-password")
    finally:
        other.close()


def test_device_timing_page_controls_multiple_ports(window, monkeypatch):
    states = {}
    changes = []
    counters = [0]

    def snapshot(_profiles):
        counters[0] += 2
        return 'yes', [
            PortState(name, i, '1', ServiceState(True, states.get(name, 'inactive'), 'current'),
                      ServiceState(), ServiceState(), role='MASTER', offset=10, servo='2',
                      counters=(counters[0], 0), device='未设置设备查询')
            for i, name in enumerate(('eth1', 'eth2', 'eth3'))
        ]

    def change(enabled, profiles):
        changes.append((enabled, list(profiles)))
        for name in profiles:
            states[name] = 'active' if enabled else 'inactive'
        return '操作完成'

    def wait_for_refresh():
        for _ in range(100):
            if not window.tasks:
                break
            QTest.qWait(10)
        assert not window.tasks

    monkeypatch.setattr('ros2_recorder.device_timing_page.timing_snapshot', snapshot)
    monkeypatch.setattr('ros2_recorder.device_timing_page.change_ports', change)
    window._unlock_admin()
    window.nav.setCurrentRow(5)
    page = window.timing_widget
    wait_for_refresh()
    assert page.timer.isActive()
    assert page.table.rowCount() == 3
    page.table.item(0, 0).setCheckState(Qt.Checked)
    page.table.item(2, 0).setCheckState(Qt.Checked)
    window.preflight_busy = True
    page.update_controls()
    assert not page.start_button.isEnabled()
    page._change_output(True)
    assert changes == []
    window.preflight_busy = False
    page.update_controls()
    clicks(page.start_button, 1)
    errors = []
    monkeypatch.setattr(window, '_error', errors.append)
    window.start_recording()
    assert errors == ['正在启停 PTP 广播，请等待操作完成后再录制。']
    wait_for_refresh()
    assert changes == [(True, ['eth1', 'eth3'])]
    assert page.table.item(0, 4).text() == 'PHC 已收敛 · Sync 在发送'
    assert page.table.item(1, 4).text() == '未启动'
    assert page.table.item(2, 0).checkState() == Qt.Checked
    assert page.stop_button.isEnabled()
    page.table.item(2, 0).setCheckState(Qt.Unchecked)
    clicks(page.stop_button, 1)
    wait_for_refresh()
    assert changes[-1] == (False, ['eth1'])
    assert page.table.item(0, 4).text() == '未启动'
    assert page.table.item(2, 4).text() == 'PHC 已收敛 · Sync 在发送'
    window._lock_admin()
    assert not page.timer.isActive()
    page._change_output(True)
    assert len(changes) == 2
