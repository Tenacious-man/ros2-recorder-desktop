"""Integrated administrator-only manual dual-PointCloud2 calibration."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

import numpy as np
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea,
    QSlider, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .calibration_math import CalibrationState, load_ini, save_ini
from .calibration_monitor import CalibrationMonitor
from .calibration_view import CalibrationView, NAMES
from .environment import discover_topics
from .latest_stamps import format_stamp

class CalibrationPage(QWidget):
    busyChanged = pyqtSignal()
    _snapshot = pyqtSignal(object, object)
    _exited = pyqtSignal(object, int)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.states = {role: CalibrationState() for role in NAMES}
        self.topics, self.axes, self.sliders, self.paths, self.layer_checks = {}, {}, {}, {}, {}
        self.monitor = None
        self.preparing = self.discovering = self.cancelled_start = False
        self.paused_frequency = False
        self.last_error = ""
        self.active = False
        self.discovery_manual = False
        self.retry_after = 0.
        self.sample_env = None
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.setInterval(250)
        self.selection_timer.timeout.connect(self.reconcile_subscription)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.poll)
        self._snapshot.connect(self.accept_snapshot)
        self._exited.connect(self.monitor_done)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.setInterval(2500)
        self.kill_timer.timeout.connect(lambda: self.stop(force=True))
        self._build()
        self.set_discovered_topics(host.topics)
        self.update_controls()

    @property
    def busy(self):
        return self.preparing or self.monitor is not None

    def _button(self, text, callback):
        widget = QPushButton(text)
        widget.setProperty("compact", True)
        widget.clicked.connect(callback)
        return widget

    def _note(self, text):
        widget = QLabel(text)
        widget.setProperty("secondary", True)
        widget.setWordWrap(True)
        return widget

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        title_row = QHBoxLayout()
        title = QLabel("点云标定")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addWidget(self._note("双点云手动外参 · 仅订阅话题 · 外部播放 / 驱动供数"), 1)
        root.addLayout(title_row)
        row = QHBoxLayout()
        for role in ("reference", "input"):
            row.addWidget(QLabel("基准" if role == "reference" else "待标定"))
            combo = QComboBox()
            combo.setEditable(True)
            combo.setMinimumContentsLength(16)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.lineEdit().setPlaceholderText("选择 PointCloud2 话题")
            combo.lineEdit().setClearButtonEnabled(True)
            combo.setObjectName("calibration_" + role + "_topic")
            combo.editTextChanged.connect(lambda _text, role=role: self.topic_changed(role))
            combo.activated.connect(lambda _index: self.schedule_subscription(immediate=True))
            combo.lineEdit().editingFinished.connect(lambda: self.schedule_subscription(immediate=True))
            self.topics[role] = combo
            row.addWidget(combo, 1)
        self.refresh_button = self._button("刷新", lambda: self.refresh_topics(manual=True))
        row.addWidget(self.refresh_button)
        root.addLayout(row)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        tools = QHBoxLayout()
        tools.addWidget(self._button("适配点云", lambda: self.view.fit_clouds()))
        tools.addWidget(self._button("复位视角", lambda: self.view.reset_camera()))
        tools.addStretch()
        self.properties_toggle = QCheckBox("点属性")
        tools.addWidget(self.properties_toggle)
        left_layout.addLayout(tools)
        self.view = CalibrationView()
        self.view.pointPicked.connect(self.show_point)
        left_layout.addWidget(self.view, 1)
        self.sample_info = self._note("尚未接收点云。外部暂停播放后，可继续调整最后收到的帧。")
        left_layout.addWidget(self.sample_info)
        self.properties = QPlainTextEdit()
        self.properties.setReadOnly(True)
        self.properties.setMaximumHeight(145)
        self.properties.setPlaceholderText("单击预览中的点，查看原始 / 变换后 XYZ，以及 intensity、ring、time 等实际存在的字段。")
        self.properties.hide()
        self.properties_toggle.toggled.connect(self.properties.setVisible)
        left_layout.addWidget(self.properties)
        split.addWidget(left)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        scroll.setMaximumWidth(420)
        side = QWidget()
        controls = QVBoxLayout(side)
        controls.setContentsMargins(8, 0, 8, 0)
        controls.setSpacing(8)
        tabs = QTabWidget()
        for role in ("input", "reference"):
            page = QWidget()
            layout = QVBoxLayout(page)
            self.layer_checks[role] = QCheckBox("显示" + NAMES[role])
            self.layer_checks[role].setChecked(True)
            self.layer_checks[role].toggled.connect(lambda value, role=role: self.set_layer_visible(role, value))
            layout.addWidget(self.layer_checks[role])
            layout.addWidget(self._note("平移：米；角度：度。" + ("微调增量左乘已加载外参。" if role == "input" else "调整基准点云在共同坐标系中的姿态。")))
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(6)
            self.axes[role], self.sliders[role] = [], []
            for index, name in enumerate(("Roll", "Pitch", "Yaw", "X", "Y", "Z")):
                spin = QDoubleSpinBox()
                angle = index < 3
                spin.setDecimals(5)
                spin.setRange(-180 if angle else -1e6, 180 if angle else 1e6)
                spin.setSingleStep(.1 if angle else .01)
                spin.setSuffix(" °" if angle else " m")
                spin.setFixedWidth(140)
                spin.setObjectName("calibration_" + role + "_" + name.lower())
                slider = QSlider(Qt.Horizontal)
                slider.setRange(-18000 if angle else -5000, 18000 if angle else 5000)
                slider.setToolTip("拖动范围：±180°" if angle else "拖动范围：±5 m；数值框支持更大范围。")
                scale = 100 if angle else 1000
                spin.valueChanged.connect(lambda value, role=role, index=index, scale=scale: self.axis_changed(role, index, value, scale))
                slider.valueChanged.connect(lambda value, spin=spin, scale=scale: spin.setValue(value / scale))
                self.axes[role].append(spin)
                self.sliders[role].append(slider)
                grid.addWidget(QLabel(name), index, 0)
                grid.addWidget(slider, index, 1)
                grid.addWidget(spin, index, 2)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)
            row = QHBoxLayout()
            row.addWidget(self._button("读取 INI…", lambda _checked=False, role=role: self.choose_load(role)))
            row.addWidget(self._button("保存 INI…", lambda _checked=False, role=role: self.choose_save(role)))
            layout.addLayout(row)
            self.paths[role] = self._note("未读取文件 · 单位矩阵")
            layout.addWidget(self.paths[role])
            row = QHBoxLayout()
            row.addWidget(self._button("清零调整", lambda _checked=False, role=role: self.reset_adjustment(role)))
            row.addWidget(self._button("单位矩阵", lambda _checked=False, role=role: self.reset_identity(role)))
            layout.addLayout(row)
            layout.addStretch()
            tabs.addTab(page, NAMES[role])
        controls.addWidget(tabs)
        controls.addWidget(self._button("导出待标定 → 基准 INI…", self.export_relative))
        controls.addWidget(self._note("每组外参均表示：原始点云 → 共同目标坐标系。INI 的平移按旧工具约定使用厘米。"))
        controls.addWidget(self._note("手动调整不包含自动求解、点云去畸变或时间配对；请自行选择静态场景或合适的外部暂停帧。"))
        controls.addStretch()
        scroll.setWidget(side)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([850, 340])
        root.addWidget(split, 1)
        self.status = self._note("后台自动发现 PointCloud2 话题，选择后自动订阅；外部暂停后保留最后一帧。")
        root.addWidget(self.status)

    def authorized(self):
        return self.host.admin_unlocked and not self.host.pending_close

    def set_active(self, active):
        if self.active == active:
            return
        self.active = active
        if active:
            self.retry_after = 0.
            self.refresh_timer.start()
            QTimer.singleShot(0, self.poll)
        else:
            self.refresh_timer.stop()
            self.selection_timer.stop()
            self.stop()
        self.update_controls()

    def poll(self):
        if self.active and self.authorized():
            self.refresh_topics(manual=False)
            self.reconcile_subscription()

    def schedule_subscription(self, *, immediate=False):
        if self.active and self.authorized():
            self.retry_after = 0.
            self.selection_timer.start(0 if immediate else 250)

    def set_discovered_topics(self, topics):
        names = sorted({item["name"] for item in topics if "sensor_msgs/msg/PointCloud2" in item["types"]})
        for combo in self.topics.values():
            if [combo.itemText(index) for index in range(combo.count())] == names:
                continue
            selected = combo.currentText()
            editor = combo.lineEdit()
            cursor, selection_start, selection_length = editor.cursorPosition(), editor.selectionStart(), len(editor.selectedText())
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentIndex(-1)
            combo.setEditText(selected)
            editor.setCursorPosition(cursor)
            if selection_start >= 0:
                editor.setSelection(selection_start, selection_length)
            combo.blockSignals(False)

    def topic_changed(self, role):
        if hasattr(self, "view"):
            self.view.set_cloud(role, None)
            self.render_sample_info()
            self.schedule_subscription()

    def refresh_topics(self, *, manual=True):
        if not self.active or not self.authorized():
            return
        if self.discovering:
            self.discovery_manual = self.discovery_manual or manual
            self.update_controls()
            return
        if (not self.host.active_env or self.host.env_busy or self.host.is_recording or self.host.preflight_busy
                or self.host.build_runner or self.host.benchmark_busy or self.host.analysis_widget.busy):
            if manual:
                self.status.setText("请先加载消息环境，并等待当前录制或环境操作结束。")
            return
        env = dict(self.host.active_env)
        self.discovering = True
        self.discovery_manual = manual
        self.update_controls()

        def finish(topics=None, error=""):
            requested = self.discovery_manual
            self.discovering = False
            self.discovery_manual = False
            if self.active and self.authorized() and env == self.host.active_env:
                if error:
                    if requested or not self.monitor:
                        self.status.setText("话题刷新失败：" + error)
                else:
                    self.set_discovered_topics(topics)
                    if not self.monitor:
                        self.status.setText("话题已刷新，选择点云后自动订阅。")
                    self.schedule_subscription(immediate=True)
            elif self.active and self.authorized():
                QTimer.singleShot(0, self.poll)
            self.update_controls()
        self.host.run_task(lambda _log: discover_topics(env), lambda value: finish(value), lambda error: finish(error=error))

    def subscription_topics(self):
        topics = {}
        for role, combo in self.topics.items():
            name = combo.currentText().strip()
            valid = re.fullmatch(r"/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*", name)
            topics[role] = name if valid else ""
        if topics["input"] == topics["reference"]:
            topics["input"] = ""
        return topics

    def settings(self):
        return {"topics": self.subscription_topics()}

    def reconcile_subscription(self):
        if not self.active or not self.authorized() or self.preparing:
            return
        desired = self.settings()
        if not any(desired["topics"].values()):
            self.stop()
            self.status.setText("选择有效的 PointCloud2 话题后自动订阅。")
            return
        if self.monitor:
            if not self.monitor.stopping and desired != self.monitor.settings:
                try:
                    self.monitor.update(desired)
                except (ValueError, OSError) as exc:
                    self.status.setText("点云订阅更新失败：" + str(exc))
        elif time.monotonic() >= self.retry_after:
            self.start()

    def start(self):
        if not self.active or not self.authorized() or self.busy:
            return
        reason = self.host._busy_message()
        if reason:
            self.status.setText(reason)
            return
        topics = self.settings()["topics"]
        if not self.host.active_env:
            self.status.setText("请先在“消息环境”加载 ROS 2 环境。")
            return
        if not any(topics.values()):
            self.status.setText("选择有效的 PointCloud2 话题后自动订阅。")
            return
        self.preparing = True
        self.cancelled_start = False
        self.last_error = ""
        env = dict(self.host.active_env)
        if env != self.sample_env:
            for role in self.states:
                self.view.set_cloud(role, None)
            self.render_sample_info()
            self.sample_env = env
        previous = self.host._pause_frequency_for_recording()
        self.paused_frequency = True
        self.status.setText("正在释放频率检测订阅，准备标定…")
        self.update_controls()
        self.busyChanged.emit()

        def prepare(_log):
            if previous and not previous.wait(8):
                previous.stop(force=True)
                if not previous.wait(3):
                    raise RuntimeError("原频率检测进程尚未退出。")

        def prepared(_value=None, error=""):
            self.preparing = False
            if (not self.active or not self.authorized() or self.cancelled_start or error
                    or env != self.host.active_env or not any(self.subscription_topics().values())):
                self.resume_frequency()
                self.status.setText("标定未启动：" + error if error else "已取消标定订阅。")
                if error:
                    self.retry_after = time.monotonic() + 5
                elif self.active and self.authorized():
                    self.schedule_subscription()
            else:
                try:
                    monitor = CalibrationMonitor(env, self.settings(),
                        on_snapshot=lambda value: self._snapshot.emit(monitor, value),
                        on_done=lambda code: self._exited.emit(monitor, code))
                    self.monitor = monitor
                    monitor.start()
                    self.status.setText("正在订阅点云 · Domain " + env.get("ROS_DOMAIN_ID", "0"))
                except Exception as exc:
                    self.monitor = None
                    self.resume_frequency()
                    self.retry_after = time.monotonic() + 5
                    self.status.setText("标定订阅启动失败：" + str(exc))
            self.update_controls()
            self.busyChanged.emit()
        self.host.run_task(prepare, prepared, lambda error: prepared(error=error))

    def accept_snapshot(self, monitor, value):
        if monitor is not self.monitor or monitor.stopping or not self.authorized() or value.get("generation", monitor.generation) != monitor.generation:
            return
        if "error" in value:
            self.last_error = value["error"]
            self.status.setText((NAMES.get(value.get("role"), "标定订阅") + "：" + value["error"]))
        elif "cloud" in value:
            role = value["role"]
            if (not self.active or not self.subscription_topics().get(role)
                    or monitor.settings["topics"].get(role) != self.subscription_topics()[role]):
                return
            self.view.set_cloud(role, value["cloud"])
            self.render_sample_info()
            self.status.setText("订阅中 · 可实时微调；外部暂停后保留最后一帧。")

    def stop(self, _checked=False, *, force=False):
        self.cancelled_start = True
        self.selection_timer.stop()
        if self.monitor:
            self.monitor.stop(force=force)
            if not force:
                self.kill_timer.start()
            self.status.setText("正在停止标定订阅…")
        self.update_controls()

    def monitor_done(self, monitor, code):
        if monitor is not self.monitor:
            return
        self.kill_timer.stop()
        self.monitor = None
        self.resume_frequency()
        if not monitor.stopping:
            self.retry_after = time.monotonic() + 5
        self.status.setText(("订阅退出：" + (self.last_error or str(code))) if code and not monitor.stopping
                            else "订阅已停止，保留最后一帧，可继续微调和保存外参。")
        self.update_controls()
        self.busyChanged.emit()
        if self.active and self.authorized() and monitor.stopping:
            self.schedule_subscription(immediate=True)

    def resume_frequency(self):
        if self.paused_frequency:
            self.paused_frequency = False
            self.host._resume_frequency_after_recording()

    def update_controls(self):
        allowed = self.authorized()
        self.refresh_button.setEnabled(allowed and self.active)
        self.refresh_button.setText("正在刷新…" if self.discovering and self.discovery_manual else "刷新")
        for combo in self.topics.values():
            combo.setEnabled(allowed)

    def axis_changed(self, role, index, value, scale):
        self.states[role].adjustment[index] = value
        slider = self.sliders[role][index]
        slider.blockSignals(True)
        slider.setValue(round(value * scale))
        slider.blockSignals(False)
        self.view.set_matrix(role, self.states[role].matrix)
        self.render_sample_info()

    def _sync_axes(self, role):
        for index, value in enumerate(self.states[role].adjustment):
            spin = self.axes[role][index]
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
            slider = self.sliders[role][index]
            slider.blockSignals(True)
            slider.setValue(round(value * (100 if index < 3 else 1000)))
            slider.blockSignals(False)
        self.view.set_matrix(role, self.states[role].matrix)
        self.render_sample_info()

    def load_path(self, role, path):
        if not self.authorized():
            return
        matrix = load_ini(path)
        self.states[role].load(matrix, reference=role == "reference")
        self.paths[role].setText(str(path))
        self._sync_axes(role)
        self.status.setText("已读取 " + NAMES[role] + " 外参。")

    def choose_load(self, role):
        if not self.authorized():
            return
        path, _ = QFileDialog.getOpenFileName(self, "读取" + NAMES[role] + "外参", "", "标定 INI (*.ini);;所有文件 (*)")
        if path:
            try:
                self.load_path(role, path)
            except (ValueError, OSError) as exc:
                self.status.setText("读取失败：" + str(exc))

    def save_path(self, role, path):
        if not self.authorized():
            return
        save_ini(path, self.states[role].matrix)
        self.status.setText("已保存 " + NAMES[role] + " 外参：" + str(path))

    def choose_save(self, role):
        if not self.authorized():
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存" + NAMES[role] + "外参", role + "_to_target.ini", "标定 INI (*.ini)")
        if path:
            try:
                self.save_path(role, path if Path(path).suffix else path + ".ini")
            except (ValueError, OSError) as exc:
                self.status.setText("保存失败：" + str(exc))

    def relative_matrix(self):
        return np.linalg.inv(self.states["reference"].matrix) @ self.states["input"].matrix

    def export_relative(self):
        if not self.authorized():
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出待标定点云到基准点云的外参", "input_to_reference.ini", "标定 INI (*.ini)")
        if path:
            try:
                save_ini(path if Path(path).suffix else path + ".ini", self.relative_matrix())
                self.status.setText("已导出待标定 → 基准外参；INI 平移单位为厘米。")
            except (ValueError, OSError) as exc:
                self.status.setText("导出失败：" + str(exc))

    def reset_adjustment(self, role):
        if self.authorized():
            self.states[role].adjustment = [0.] * 6
            self._sync_axes(role)

    def reset_identity(self, role):
        if self.authorized():
            self.states[role] = CalibrationState()
            self.paths[role].setText("未读取文件 · 单位矩阵")
            self._sync_axes(role)

    def set_layer_visible(self, role, visible):
        self.view.visible_layers[role] = visible
        self.view.update()

    def show_point(self, value):
        if value is None:
            self.properties.clear()
            return
        self.properties_toggle.setChecked(True)
        rows = [NAMES[value["role"]] + f" · 原始点索引 {value['point_index']}",
                "原始 XYZ (m)：" + ", ".join(f"{v:.6f}" for v in value["raw_xyz"]),
                "变换后 XYZ (m)：" + ", ".join(f"{v:.6f}" for v in value["xyz"]),
                "原始字段：" + json.dumps(value["attributes"], ensure_ascii=False)]
        self.properties.setPlainText("\n".join(rows))

    def render_sample_info(self):
        rows = []
        for role in ("reference", "input"):
            cloud = self.view.clouds.get(role)
            if cloud is not None:
                rows.append(f"{NAMES[role]}：{cloud['total']:,} 点 / 预览 {len(cloud['xyz']):,} 点 · frame={cloud['frame']} · stamp={format_stamp(cloud['stamp_ns'])}")
        if len(self.view.clouds) == 2:
            difference = (self.view.clouds["input"]["stamp_ns"] - self.view.clouds["reference"]["stamp_ns"]) / 1e6
            rows.append(f"最后收到两帧的时间戳差：{difference:+.3f} ms（未做时间配对）")
        self.sample_info.setText("\n".join(rows) if rows else "尚未接收点云。外部暂停播放后，可继续调整最后收到的帧。")

    def dispose(self):
        self.active = False
        self.refresh_timer.stop()
        self.selection_timer.stop()
        self.kill_timer.stop()
        self.cancelled_start = True
        if self.monitor:
            self.monitor.stop(force=True)
            self.monitor.wait(3)
