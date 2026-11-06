"""Select and verify the LinuxPTP grandmaster on each Ethernet interface."""

from dataclasses import replace
from datetime import datetime

from PyQt5.QtCore import QSettings, Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .device_timing import TimingProfile, change_ports, timing_snapshot


class TimingProfileDialog(QDialog):
    def __init__(self, port, profile, parent):
        super().__init__(parent)
        self.setWindowTitle(f'{port.name} · 授时配置')
        form = QFormLayout(self)
        self.domain = QSpinBox()
        self.domain.setRange(0, 127)
        self.domain.setValue(profile.domain)
        self.transport = QComboBox()
        self.transport.addItems(['UDPv4', 'L2'])
        self.transport.setCurrentText(profile.transport)
        self.clock_mode = QComboBox()
        self.clock_mode.addItem('标准 PTP 时标', 'ptp')
        self.clock_mode.addItem('旧设备 UTC 兼容（偏移 0）', 'utc')
        self.clock_mode.setCurrentIndex(self.clock_mode.findData(profile.clock_mode))
        self.falcon_ip = QLineEdit(profile.falcon_ip)
        self.falcon_ip.setPlaceholderText('Seyond Falcon IPv4，留空则跳过设备查询')
        form.addRow('PTP 域', self.domain)
        form.addRow('传输方式（E2E）', self.transport)
        form.addRow('时标', self.clock_mode)
        form.addRow('Falcon 查询地址', self.falcon_ip)
        legacy = port.legacy.loaded and port.legacy_phc.loaded
        for field in (self.domain, self.transport, self.clock_mode):
            field.setEnabled(not legacy)
        if legacy:
            form.addRow(QLabel('此网口使用已部署的 Linux 服务配置。'))
        else:
            form.addRow(QLabel('编辑后点击“保存并启用”，将配置应用到系统。'))
        self.error = QLabel()
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self):
        try:
            self.profile = TimingProfile(
                self.domain.value(), self.transport.currentText(), self.clock_mode.currentData(),
                self.falcon_ip.text().strip(),
            )
        except ValueError as exc:
            self.error.setText(f'配置无效 · {exc}')
            return
        super().accept()


class DeviceTimingPage(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.refreshing = False
        self.changing = False
        self.ports = {}
        self.checked = set()
        self.previous_counters = {}
        self.traffic = {}
        self.settings = QSettings('ROS Recorder', 'Device Timing')
        self.profiles = {}
        self.edited = set()
        for name in self.settings.childGroups():
            try:
                self.profiles[name] = TimingProfile(
                    int(self.settings.value(f'{name}/domain', 0)),
                    self.settings.value(f'{name}/transport', 'UDPv4'),
                    self.settings.value(f'{name}/clock_mode', 'ptp'),
                    self.settings.value(f'{name}/falcon_ip', ''),
                )
            except (TypeError, ValueError):
                continue

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel('设备授时')
        heading.setObjectName('pageTitle')
        layout.addWidget(heading)
        self.status = QLabel('勾选一个或多个网口，然后启动或停止授时。')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton('刷新并检查')
        self.refresh_button.clicked.connect(self.refresh)
        self.start_button = QPushButton('保存并启用')
        self.start_button.clicked.connect(lambda: self._change_output(True))
        self.stop_button = QPushButton('停止并关闭自启')
        self.stop_button.clicked.connect(lambda: self._change_output(False))
        self.config_button = QPushButton('配置当前网口…')
        self.config_button.clicked.connect(self._edit_profile)
        for button in (self.refresh_button, self.start_button, self.stop_button, self.config_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        headers = ('勾选', '网口', '链路', '硬件时钟', '本机授时', '设备锁定', '开机自启')
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.itemChanged.connect(self._update_checks)
        layout.addWidget(self.table, 3)
        self.report = QPlainTextEdit('进入页面后读取网口和授时状态。')
        self.report.setReadOnly(True)
        layout.addWidget(self.report, 2)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        layout.addWidget(self.message)
        note = QLabel('保存并启用后由系统开机启动，关闭录制软件也会继续运行。录制准备或录制期间禁止启停。')
        note.setProperty('secondary', True)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.timer = QTimer(self)
        self.timer.setInterval(4000)
        self.timer.timeout.connect(self.refresh)
        self.update_controls()

    def set_active(self, active):
        if active:
            self.refresh()
            self.timer.start()
        else:
            self.timer.stop()

    def update_controls(self):
        available = self.host.admin_unlocked and not self.changing and not self.host.pending_close
        idle = available and not self.refreshing
        can_change = idle and not self.host.is_recording and not self.host.preflight_busy
        selected = [self.ports[name] for name in self.checked if name in self.ports]
        self.refresh_button.setEnabled(idle)
        self.start_button.setEnabled(can_change and any(p.phc is not None for p in selected))
        self.stop_button.setEnabled(can_change and any(p.stoppable for p in selected))
        self.config_button.setEnabled(idle and self._current_name() in self.ports)

    def _current_name(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.UserRole) if item else None

    def _update_checks(self, item):
        if item.column() != 0:
            return
        name = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.checked.add(name)
        else:
            self.checked.discard(name)
        self.update_controls()

    def refresh(self):
        if self.refreshing or self.changing or not self.host.admin_unlocked or self.host.pending_close:
            return
        profiles = dict(self.profiles)
        self.refreshing = True
        self.host.run_task(lambda _log: timing_snapshot(profiles), self._show_snapshot, self._snapshot_failed)

    def _local_state(self, port):
        if not port.active:
            if port.managed.substate == 'auto-restart':
                return '异常退出 · 正在重试'
            if port.managed.state == 'failed' or port.legacy.state == 'failed':
                return '服务启动失败'
            if port.managed.state == 'activating' or port.legacy.state == 'activating':
                return '启动中'
            if port.legacy_phc.state == 'active':
                return '广播已停 · PHC 仍在运行'
            return '外部进程占用' if port.occupied else '未启动'
        if port.carrier != '1':
            return '运行中 · 链路未连通'
        if port.role not in ('MASTER', 'SERVER'):
            return f'等待主端口（{port.role or "未知"}）'
        if port.servo not in ('2', '3'):
            return '主端口 · PHC 待收敛'
        if self.traffic.get(port.name, (False, False))[0]:
            return 'PHC 已收敛 · Sync 在发送'
        if port.legacy.state == 'active':
            return 'PHC 已收敛 · 无报文计数'
        return 'PHC 已收敛 · 等待发送验证'

    def _show_snapshot(self, snapshot):
        self.refreshing = False
        synced, ports = snapshot
        current = self._current_name()
        self.ports = {port.name: port for port in ports}
        for port in ports:
            local = self.profiles.get(port.name)
            if local and port.saved_profile == replace(local, falcon_ip=''):
                self.edited.discard(port.name)
        next_counters = {}
        self.traffic = {}
        for port in ports:
            if port.carrier != '1' or port.counters is None:
                continue
            previous = self.previous_counters.get(port.name)
            next_counters[port.name] = port.managed.invocation, port.counters
            if previous and previous[0] == port.managed.invocation:
                self.traffic[port.name] = tuple(new > old for new, old in zip(port.counters, previous[1]))
        self.previous_counters = next_counters
        sync_label = {'yes': '已同步', 'no': '未同步'}.get(synced, '未知')
        self.status.setText(f'系统时钟：{sync_label}    更新于 {datetime.now():%H:%M:%S}    勾选要启停的网口，选中行可查看明细。')
        self.table.blockSignals(True)
        self.table.setRowCount(len(ports))
        selection = 0
        for row, port in enumerate(ports):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setData(Qt.UserRole, port.name)
            check.setCheckState(Qt.Checked if port.name in self.checked else Qt.Unchecked)
            self.table.setItem(row, 0, check)
            values = (
                port.name, {'1': '已连接', '0': '未连接'}.get(port.carrier, '未知'),
                f'/dev/ptp{port.phc}' if port.phc is not None else '不支持硬件 PTP',
                self._local_state(port), port.device, '已启用' if port.autostart else '未启用',
            )
            for column, value in enumerate(values, 1):
                self.table.setItem(row, column, QTableWidgetItem(value))
            if port.name == current:
                selection = row
        if ports:
            self.table.selectRow(selection)
        self.table.blockSignals(False)
        self._show_details()

    def _show_details(self):
        port = self.ports.get(self._current_name())
        self.update_controls()
        if port is None:
            self.report.setPlainText('未检测到以太网口。')
            return
        profile = self._profile(port)
        config_label = '已保存到系统' if port.saved_profile and port.name not in self.edited else '编辑配置'
        source = '已有 Linux 服务' if port.legacy.loaded and port.legacy_phc.loaded else (
            f'{config_label} · 域 {profile.domain} · {profile.transport} / E2E · '
            + ('标准 PTP 时标' if profile.clock_mode == 'ptp' else 'UTC 兼容'))
        lines = [
            f'{port.name} · {source}',
            f'本机授时 · {self._local_state(port)}',
            f'PTP 端口角色 · {port.role or "暂无数据"}',
            'PHC 相对系统时钟偏差 · '
            + (f'{port.offset:+d} ns（伺服 s{port.servo}）' if port.offset is not None else '暂无近期数据'),
        ]
        if port.counters is not None:
            sync, requests = port.counters
            sync_grew, requests_grew = self.traffic.get(port.name, (False, False))
            lines.extend((
                f'发出 Sync · {sync}（{"最近有增长" if sync_grew else "本轮未验证增长"}）',
                f'收到 Delay_Req · {requests}（{"最近收到请求" if requests_grew else "本轮未验证收到"}）',
            ))
        lines.append(f'设备端 · {port.device}' + (f' · Falcon {profile.falcon_ip}' if profile.falcon_ip else ''))
        if port.error:
            lines.extend(('', port.error))
        self.report.setPlainText('\n'.join(lines))

    def _snapshot_failed(self, error):
        self.refreshing = False
        self.ports.clear()
        self.previous_counters.clear()
        self.traffic.clear()
        self.table.setRowCount(0)
        self.report.setPlainText(f'读取授时状态失败 · {error}')
        self.update_controls()

    def _edit_profile(self):
        port = self.ports.get(self._current_name())
        if not self.config_button.isEnabled() or port is None:
            return
        self.timer.stop()
        dialog = TimingProfileDialog(port, self._profile(port), self)
        if dialog.exec() == QDialog.Accepted:
            self.profiles[port.name] = dialog.profile
            self.edited.add(port.name)
            for key, value in vars(dialog.profile).items():
                self.settings.setValue(f'{port.name}/{key}', value)
        self.set_active(self.isVisible() and self.host.admin_unlocked)

    def _profile(self, port):
        local = self.profiles.get(port.name, TimingProfile())
        if port.saved_profile and port.name not in self.edited:
            return replace(port.saved_profile, falcon_ip=local.falcon_ip)
        return local

    def _change_output(self, enabled):
        self.update_controls()
        if not (self.start_button if enabled else self.stop_button).isEnabled():
            return
        selected = {name: self._profile(self.ports[name]) for name in sorted(self.checked & self.ports.keys())}
        self.changing = True
        self.message.setText('正在应用所选网口…')
        self.host.run_task(lambda _log: change_ports(enabled, selected), self._change_finished, self._change_finished)

    def _change_finished(self, message):
        self.changing = False
        self.message.setText(message)
        self.update_controls()
        self.refresh()
