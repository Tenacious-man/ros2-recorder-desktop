"""Chinese desktop workbench. Recording and analysis use independent backends."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time
import uuid

from PyQt5.QtCore import Qt, QTimer, QUrl, QSize
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QPalette, QIcon
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy,
)

from .config import MIB, ProfileStore, RecordConfig, atomic_json, import_command
from .environment import (
    build_command, discover_topics, inspect_workspace, load_environment, validate_message_types, installed_environments,
)
from .processes import ProcessRunner
from .recording import RecordingSession, benchmark_directory, list_sessions
from .frequency import FrequencyMonitor
from .compatibility import detect_capabilities
from .tasks import Signals, Task
from .summary_dialog import RecordingSummaryDialog
from .live_stamp_page import LiveStampPage
from .item_delegate import TopicItemDelegate
from .admin_access import AdminCredentials
from .admin_ui import AdminEntryLabel, AdminPasswordDialog


def button(text, callback=None, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setProperty("primary", True)
    if callback:
        widget.clicked.connect(callback)
    return widget


def text_label(text, secondary=False):
    label = QLabel(text)
    label.setWordWrap(True)
    if secondary:
        label.setProperty("secondary", True)
    return label


class ElidingLabel(QLabel):
    """Keep long environment/output paths on one line without widening the page."""
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideMiddle, max(0, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def minimumSizeHint(self):
        return QSize(0, self.fontMetrics().height())


def spin(value, maximum, suffix="", minimum=0):
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(value)
    box.setSuffix(suffix)
    return box


def path_row(editor, callback):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(editor, 1)
    layout.addWidget(button("浏览…", callback))
    return row


def group(title):
    box = QGroupBox(title)
    layout = QVBoxLayout(box)
    layout.setSpacing(10)
    return box, layout


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QTableWidget.SelectRows)
    widget.setEditTriggers(QTableWidget.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def size_text(value):
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    return f"{value / MIB:.1f} MiB"


def disk_space_text(value):
    return f"{value / 1_000_000_000:.2f} GB"


class MainWindow(QMainWindow):
    def __init__(self, *, auto_discover=True):
        super().__init__()
        self.setWindowTitle("ROS Recorder · ROS 2")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name('assets') / 'icon.svg')))
        self.resize(1440, 900)
        self.setMinimumSize(1080, 740)
        self.auto_discover = auto_discover
        self.tasks = []
        self.active_env = None
        self.base_env = None
        self.base_setup = ""
        self.capabilities = None
        self.loaded_setup = ""
        self.active_workspace = ""
        self.active_packages = []
        self.workspace = None
        self.env_busy = False
        self.discovery_busy = False
        self.discovery_manual = False
        self.discovery_requested = False
        self.last_discovery = 0.0
        self.preflight_busy = False
        self.benchmark_busy = False
        self.build_runner = None
        self.build_cancelled = False
        self.session = None
        self.recording_summary = None
        self.monitors = {}
        self.frequency_monitor = None
        self.frequency_disabled = set()
        self.frequency_failed = set()
        self.frequency_suspended = False
        self.frequency_resume = {}
        self.frequency_results = {}
        self.topics = []
        self.selected_topics = set()
        self.manual_topics = set()
        self.sessions = []
        self.metric_busy = False
        self.idle_disk_busy = False
        self.last_idle_disk = 0
        self.pending_close = False
        self.stop_time = None
        self.admin_credentials = AdminCredentials()
        self.admin_unlocked = False
        self.admin_dialog = None
        self.admin_rows = []
        self.calibration_widget = None
        self.signals = Signals(self)
        self.signals.line.connect(self._record_line)
        self.signals.done.connect(self._record_done)
        self.signals.frequency.connect(self._frequency_update)
        self.signals.monitor_done.connect(self._monitor_done)
        self.signals.build_done.connect(self._build_done)
        self.build_signals = Signals(self)
        self.build_signals.line.connect(self._build_log)
        try:
            self.store = ProfileStore()
            self.store_error = ""
        except Exception as exc:
            # Preserve an invalid user file until the user explicitly saves a replacement.
            self.store = ProfileStore(Path("/dev/null/nonexistent"))
            self.store.path = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ros2-recorder" / "profiles-recovered.json"
            self.store_error = f"原配置未读取成功：{exc}。原文件保留，新配置将另存。"
        self._build_window()
        self._refresh_profile_names()
        initial = RecordConfig(all_topics=False)
        if self.store.last:
            try:
                initial = RecordConfig.from_dict(self.store.last)
            except Exception as exc:
                self.store_error = f"上次配置无效，已使用默认值：{exc}"
        self.apply_config(initial, load_env=False)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        if self.store_error:
            self._append(self.event_log, self.store_error)
        QTimer.singleShot(0, lambda: self._initialize_environment(initial))

    def _build_window(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName("appHeader")
        top = QHBoxLayout(header)
        top.setContentsMargins(22, 15, 22, 15)
        self.brand = AdminEntryLabel("◉  ROS Recorder")
        self.brand.setObjectName("brand")
        self.brand.activated.connect(self._open_admin)
        top.addWidget(self.brand)
        top.addStretch()
        self.header_env = text_label("正在检测 ROS 2 环境…", True)
        top.addWidget(self.header_env)
        outer.addWidget(header)
        middle = QHBoxLayout()
        middle.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        self.nav.setFixedWidth(156)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text in ("录制工作台", "录制记录", "消息环境", "配置管理", "数据分析"):
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(140, 48))
            self.nav.addItem(item)
        self.stack = QStackedWidget()
        self.nav.currentRowChanged.connect(self._navigate)
        middle.addWidget(self.nav)
        middle.addWidget(self.stack, 1)
        for builder in (self._record_page, self._history_page, self._environment_page, self._profiles_page, self._analysis_page):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(8)
            builder(layout)
            self.stack.addWidget(page)
        outer.addLayout(middle, 1)
        self.setCentralWidget(root)
        self.nav.setCurrentRow(0)

    def _heading(self, layout, title, subtitle):
        label = QLabel(title)
        label.setObjectName("pageTitle")
        layout.addWidget(label)
        layout.addWidget(text_label(subtitle, True))

    def _open_admin(self):
        if self.admin_unlocked:
            self.nav.setCurrentRow(self.admin_rows[-1])
            return
        self._show_admin_password("unlock" if self.admin_credentials.configured else "setup")

    def _show_admin_password(self, mode):
        if mode == "change" and not self.admin_unlocked:
            return
        if self.admin_dialog is not None:
            self.admin_dialog.raise_()
            self.admin_dialog.activateWindow()
            return
        dialog = AdminPasswordDialog(self.admin_credentials, mode, self)
        self.admin_dialog = dialog
        if mode != "change":
            dialog.accepted.connect(self._unlock_admin)
        dialog.finished.connect(lambda _result: self._admin_dialog_finished(dialog))
        dialog.open()

    def _admin_dialog_finished(self, dialog):
        if self.admin_dialog is dialog:
            self.admin_dialog = None
        dialog.deleteLater()

    def _unlock_admin(self):
        self.admin_unlocked = True
        if not self.admin_rows:
            for title, subtitle in (
                ("设备授时", "管理员功能 · 尚未接入硬件授时；后续按设备型号和网口适配。"),
                ("点云标定", "双点云手动外参标定"),
                ("管理员设置", "管理员模式仅对本次运行有效，退出或重启后自动隐藏功能入口。"),
            ):
                page = QWidget()
                layout = QVBoxLayout(page)
                layout.setContentsMargins(16, 12, 16, 12)
                layout.setSpacing(8)
                if title == "点云标定":
                    try:
                        from .calibration_page import CalibrationPage
                        self.calibration_widget = CalibrationPage(self)
                        self.calibration_widget.busyChanged.connect(self._analysis_state_changed)
                        layout.addWidget(self.calibration_widget)
                    except ImportError:
                        self._heading(layout, title, "标定依赖未安装。请安装 python3-numpy 后重启软件。")
                else:
                    self._heading(layout, title, subtitle)
                if title == "管理员设置":
                    actions = QHBoxLayout()
                    actions.addWidget(button("修改管理员密码…", lambda: self._show_admin_password("change")))
                    actions.addWidget(button("退出管理员模式", self._lock_admin))
                    actions.addStretch()
                    layout.addLayout(actions)
                if title != "点云标定":
                    layout.addStretch()
                row = self.stack.addWidget(page)
                item = QListWidgetItem(title)
                item.setSizeHint(QSize(140, 48))
                self.nav.addItem(item)
                self.admin_rows.append(row)
        for row in self.admin_rows:
            self.nav.item(row).setHidden(False)
            self.stack.widget(row).setEnabled(True)
        if self.calibration_widget:
            self.calibration_widget.update_controls()
        self.nav.setCurrentRow(self.admin_rows[-1])

    def _lock_admin(self):
        self.admin_unlocked = False
        if self.calibration_widget:
            self.calibration_widget.set_active(False)
        if self.admin_dialog is not None:
            self.admin_dialog.reject()
        if self.stack.currentIndex() >= 5:
            self.nav.setCurrentRow(0)
        for row in self.admin_rows:
            self.nav.item(row).setHidden(True)
            self.stack.widget(row).setEnabled(False)

    def _record_page(self, layout):
        title_row = QHBoxLayout()
        title = QLabel("录制工作台")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        self.environment_label = ElidingLabel("消息环境：正在加载…")
        self.environment_label.setProperty("secondary", True)
        title_row.addWidget(self.environment_label, 1)
        self.start_button = button("●  开始录制", self.start_recording, True)
        self.start_button.setObjectName("startRecording")
        self.stop_button = button("■  停止并保存", self.stop_recording)
        self.stop_button.setEnabled(False)
        self.force_button = button("强制停止…", self.force_stop)
        self.force_button.hide()
        title_row.addWidget(self.start_button)
        title_row.addWidget(self.stop_button)
        title_row.addWidget(self.force_button)
        layout.addLayout(title_row)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("配置方案"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        profile_row.addWidget(self.profile_combo, 1)
        self.load_profile_button = button("加载", self.load_selected_profile)
        profile_row.addWidget(self.load_profile_button)
        profile_row.addWidget(button("保存为…", self.save_profile))
        profile_row.addWidget(button("导入命令…", self.import_record_command))
        profile_row.addWidget(button("消息环境", lambda: self.nav.setCurrentRow(2)))
        layout.addLayout(profile_row)
        self.workbench_splitter = QSplitter(Qt.Vertical)
        self.workbench_splitter.setObjectName("workbenchSplitter")
        self.workbench_splitter.setChildrenCollapsible(False)
        self.workbench_splitter.setHandleWidth(6)
        self.record_settings = QWidget()
        panes = QHBoxLayout(self.record_settings)
        panes.setContentsMargins(0, 0, 0, 0)
        panes.setSpacing(14)
        topic_box, topic_layout = group("话题与频率")
        topic_layout.setContentsMargins(10, 8, 10, 8)
        topic_layout.setSpacing(6)
        selection_row = QHBoxLayout()
        self.topic_search = QLineEdit()
        self.topic_search.setObjectName('topicSearch')
        self.topic_search.setPlaceholderText('搜索话题名称或消息类型…')
        self.topic_search.setClearButtonEnabled(True)
        self.topic_search.setToolTip('仅筛选显示，不改变勾选状态；搜索结果之外已勾选的话题仍会录制。')
        self.topic_search.textChanged.connect(self._filter_topic_rows)
        selection_row.addWidget(self.topic_search, 1)
        self.refresh_topics_button = button("刷新话题", lambda: self.refresh_topics())
        selection_row.addWidget(self.refresh_topics_button)
        self.add_topic_button = button("手动添加…", self.add_topic)
        selection_row.addWidget(self.add_topic_button)
        topic_layout.addLayout(selection_row)
        self.select_visible_button = button('全选当前结果', lambda: self.select_visible_topics(True))
        self.clear_visible_button = button('取消当前结果', lambda: self.select_visible_topics(False))
        self.select_visible_button.setProperty('compact', True)
        self.clear_visible_button.setProperty('compact', True)
        self.topic_table = table(["录制", "话题名称", "消息类型", "发布端", "接收频率", "检测"])
        self.topic_table.setObjectName("topicTable")
        self.topic_table.setItemDelegate(TopicItemDelegate(self.topic_table))
        self.topic_table.setStyleSheet('QTableWidget::indicator { width: 20px; height: 20px; }')
        self.topic_table.setMinimumHeight(85)
        self.topic_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.topic_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.topic_table.horizontalHeader().setStretchLastSection(False)
        self.topic_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.topic_table.setWordWrap(False)
        self.topic_table.setTextElideMode(Qt.ElideMiddle)
        self.topic_table.verticalHeader().setDefaultSectionSize(36)
        self.topic_table.setColumnWidth(0, 48)
        self.topic_table.itemChanged.connect(self._topic_selection_changed)
        topic_layout.addWidget(self.topic_table, 1)
        self.topic_count = text_label("等待刷新话题…", True)
        topic_status = QHBoxLayout()
        topic_status.addWidget(self.topic_count, 1)
        topic_status.addWidget(self.select_visible_button)
        topic_status.addWidget(self.clear_visible_button)
        self.auto_frequency = QCheckBox("自动检测频率")
        self.auto_frequency.setChecked(True)
        self.auto_frequency.setToolTip("默认检测已发现的话题；录制前停止，录制结束后恢复。手动关闭的话题在本次软件会话中保持关闭。")
        self.auto_frequency.toggled.connect(self._auto_frequency_changed)
        topic_status.addWidget(self.auto_frequency)
        topic_layout.addLayout(topic_status)
        frequency_note = text_label("仅录制已勾选的话题。默认检测接收频率，录制期间暂停；Hz 不代表落盘完整性。", True)
        frequency_note.setToolTip("使用原始消息订阅，不解析整幅图像或点云。检测仍会增加接收负载；Hz 不能代表落盘完整性。")
        topic_layout.addWidget(frequency_note)
        panes.addWidget(topic_box, 3)
        settings_box, settings_layout = group("本次录制配置")
        self.parameter_box = settings_box
        self.parameter_scroll = QScrollArea()
        self.parameter_scroll.setObjectName("parameterScroll")
        self.parameter_scroll.setWidgetResizable(True)
        self.parameter_scroll.setFrameShape(QFrame.NoFrame)
        self.parameter_scroll.setMinimumWidth(306)
        self.parameter_scroll.setMaximumWidth(410)
        self.parameter_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings = QFormLayout()
        self.settings_form = settings
        settings.setVerticalSpacing(9)
        self.output_root = QLineEdit()
        self.output_root.setObjectName("outputDirectory")
        settings.addRow("保存目录", path_row(self.output_root, self.choose_output))
        self.prefix = QLineEdit("capture")
        settings.addRow("录制名称", self.prefix)
        self.storage = QComboBox()
        self.storage.addItems(["sqlite3", "mcap"])
        self.storage.currentTextChanged.connect(self._storage_changed)
        settings.addRow("存储格式", self.storage)
        self.preset = QComboBox()
        settings.addRow("存储预设", self.preset)
        self.preset.currentTextChanged.connect(self._preset_hint)
        self.cache = spin(512, 4095, " MiB")
        settings.addRow("写入缓存", self.cache)
        self.cache_messages = spin(100, 100000000, " 条")
        settings.addRow("缓存消息数", self.cache_messages)
        self.cache_messages.hide()
        settings.labelForField(self.cache_messages).hide()
        self.split_size = spin(4096, 1048576, " MiB")
        self.split_size.setSpecialValueText("不按大小分包")
        settings.addRow("分包大小", self.split_size)
        self.advanced_toggle = QCheckBox("展开高级参数")
        self.advanced = QWidget()
        advanced_form = QFormLayout(self.advanced)
        self.advanced_form = advanced_form
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.cache_bytes = QLineEdit()
        self.cache_bytes.setPlaceholderText("留空采用 MiB 值")
        self.cache_bytes.setToolTip("导入旧命令时保留原始字节数；修改上方 MiB 值会清除此覆盖值。")
        advanced_form.addRow("精确缓存 / 字节", self.cache_bytes)
        self.split_bytes = QLineEdit()
        self.split_bytes.setPlaceholderText("留空采用 MiB 值")
        advanced_form.addRow("精确分包 / 字节", self.split_bytes)
        self.cache.valueChanged.connect(self.cache_bytes.clear)
        self.split_size.valueChanged.connect(self.split_bytes.clear)
        self.split_seconds = spin(0, 2147483647, " 秒")
        self.split_seconds.setSpecialValueText("不按时间分包")
        advanced_form.addRow("分包时长", self.split_seconds)
        self.compression = QComboBox()
        self.compression.addItems(["none", "file", "message"])
        advanced_form.addRow("zstd 压缩模式", self.compression)
        self.compression_queue = spin(4, 100000, minimum=1)
        advanced_form.addRow("压缩队列", self.compression_queue)
        self.compression_threads = spin(0, 1024)
        self.compression_threads.setSpecialValueText("自动")
        advanced_form.addRow("压缩线程", self.compression_threads)
        self.qos_path = QLineEdit()
        advanced_form.addRow("QoS YAML", path_row(self.qos_path, lambda: self.choose_yaml(self.qos_path)))
        self.storage_config = QLineEdit()
        advanced_form.addRow("存储 YAML", path_row(self.storage_config, lambda: self.choose_yaml(self.storage_config)))
        self.include_hidden = QCheckBox("包含隐藏话题")
        self.use_sim_time = QCheckBox("使用仿真时间（需要 /clock）")
        advanced_form.addRow(self.include_hidden)
        advanced_form.addRow(self.use_sim_time)
        self.min_free = QDoubleSpinBox()
        self.min_free.setRange(0, 1000000)
        self.min_free.setValue(5)
        self.min_free.setSuffix(" GiB")
        advanced_form.addRow("预留空间", self.min_free)
        self.auto_stop_disk = QCheckBox("低于预留空间时停止并保存")
        self.auto_stop_disk.setChecked(True)
        advanced_form.addRow(self.auto_stop_disk)
        settings_layout.addLayout(settings)
        settings_layout.addWidget(self.advanced_toggle)
        settings_layout.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.preset_note = text_label("", True)
        settings_layout.addWidget(self.preset_note)
        self.cache_note = text_label("缓存单位及可用参数将根据当前 ROS 2 录制器自动检测。", True)
        settings_layout.addWidget(self.cache_note)
        actions = QHBoxLayout()
        actions.addWidget(button("查看命令", self.show_command))
        self.speed_button = button("目录写入测速", self.benchmark)
        actions.addWidget(self.speed_button)
        settings_layout.addLayout(actions)
        settings_layout.addStretch()
        self.parameter_scroll.setWidget(settings_box)
        panes.addWidget(self.parameter_scroll, 2)
        self.workbench_splitter.addWidget(self.record_settings)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(8)
        metrics = QHBoxLayout()
        self.metric_values = {}
        for key, title in (("state", "录制状态"), ("time", "录制时长"), ("size", "文件大小"),
                           ("growth", "文件增长（参考）"), ("free", "目录剩余空间"), ("warnings", "日志告警")):
            box = QFrame()
            box.setObjectName("metricCard")
            box.setFixedHeight(68)
            inner = QVBoxLayout(box)
            inner.setContentsMargins(10, 6, 10, 6)
            inner.setSpacing(2)
            caption = ElidingLabel(title)
            caption.setProperty("secondary", True)
            inner.addWidget(caption)
            value = QLabel("—")
            value.setObjectName("metricValue")
            inner.addWidget(value)
            metrics.addWidget(box, 1)
            self.metric_values[key] = value
        self.metric_values['state'].setText("未开始")
        lower_layout.addLayout(metrics)
        log_actions = QHBoxLayout()
        log_actions.addWidget(text_label("录制日志", True))
        log_actions.addStretch()
        self.clear_log_button = button("清除日志", self.clear_recording_log)
        self.clear_log_button.setProperty('compact', True)
        self.clear_log_button.setToolTip("清空当前界面显示的日志；已保存的日志文件、录制报告和告警计数会保留。")
        log_actions.addWidget(self.clear_log_button)
        lower_layout.addLayout(log_actions)
        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumBlockCount(3000)
        self.event_log.setMinimumHeight(65)
        self.event_log.setPlaceholderText("录制日志、QoS 不兼容和缓存写入相关告警会显示在这里。")
        lower_layout.addWidget(self.event_log, 1)
        self.workbench_splitter.addWidget(lower)
        self.workbench_splitter.setStretchFactor(0, 3)
        self.workbench_splitter.setStretchFactor(1, 1)
        self.workbench_splitter.setSizes([420, 190])
        layout.addWidget(self.workbench_splitter, 1)
        self.record_path_label = ElidingLabel("每次录制自动保存命令、参数、环境摘要和日志。")
        self.record_path_label.setProperty("secondary", True)
        self.record_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.record_path_label)

    def _environment_page(self, layout):
        self._heading(layout, "消息环境", "选择已有 ROS 2 消息包或工作空间，编译并加载自定义消息。")
        box, content = group("工作空间与消息包")
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel('基础 ROS 2 环境'))
        self.base_environment = QComboBox()
        self.base_environment.setEditable(True)
        self.base_environment.addItem('自动检测当前环境', '')
        for setup in installed_environments():
            self.base_environment.addItem(setup.parent.name, str(setup))
        self.base_environment.setToolTip('优先使用已加载的 ROS 2。也可选择已安装版本或直接填写基础环境 setup.bash 的完整路径。')
        base_row.addWidget(self.base_environment, 1)
        base_row.addWidget(button('加载基础环境', self.select_base_environment))
        content.addLayout(base_row)
        self.workspace_path = QLineEdit()
        self.workspace_path.setPlaceholderText("工作空间（含 src）或消息包目录（含 package.xml）")
        content.addWidget(path_row(self.workspace_path, self.choose_workspace))
        self.scan_button = button("扫描消息包", self.scan_workspace)
        content.addWidget(self.scan_button)
        self.package_list = QListWidget()
        self.package_list.setMinimumHeight(80)
        content.addWidget(self.package_list, 1)
        actions = QHBoxLayout()
        self.build_button = button("编译并加载", self.build_workspace, True)
        self.load_button = button("加载已有编译环境", self.load_existing_environment)
        self.cancel_build_button = button("取消编译", self.cancel_build)
        self.cancel_build_button.setEnabled(False)
        for b in (self.build_button, self.load_button, self.cancel_build_button):
            actions.addWidget(b)
        content.addLayout(actions)
        self.env_status = text_label("尚未选择自定义消息环境", True)
        content.addWidget(self.env_status)
        content.addWidget(text_label("编译输出位于 ~/.cache/ros2-recorder/workspaces，保留原工作空间的 build/install。缺少依赖时显示错误；录制期间禁止编译和切换环境。", True))
        layout.addWidget(box)
        self.environment_log = QPlainTextEdit()
        self.environment_log.setReadOnly(True)
        self.environment_log.setMaximumBlockCount(3000)
        self.environment_log.setMinimumHeight(90)
        self.environment_log.setPlaceholderText("扫描、编译和环境加载日志")
        layout.addWidget(self.environment_log, 1)

    def _profiles_page(self, layout):
        self._heading(layout, "配置管理", "保存已验证过的参数与消息环境，下次直接复用。")
        self.profile_list = QListWidget()
        self.profile_list.setMinimumHeight(100)
        self.profile_list.currentTextChanged.connect(self._profile_details)
        layout.addWidget(self.profile_list)
        row = QHBoxLayout()
        row.addWidget(button("保存当前配置…", self.save_profile, True))
        self.load_profile_from_list_button = button("加载所选配置", self.load_profile_from_list)
        row.addWidget(self.load_profile_from_list_button)
        row.addWidget(button("导出 JSON…", self.export_profile))
        row.addWidget(button("导入 JSON…", self.import_profile))
        row.addWidget(button("删除所选配置…", self.delete_profile))
        layout.addLayout(row)
        self.profile_details = QPlainTextEdit()
        self.profile_details.setReadOnly(True)
        layout.addWidget(self.profile_details, 1)

    def _history_page(self, layout):
        self._heading(layout, "录制记录", "查看实际消息数、录制配置、环境和完整日志。结果不会自动判为“无丢帧”。")
        row = QHBoxLayout()
        self.history_root = QLineEdit()
        row.addWidget(self.history_root, 1)
        row.addWidget(button("选择目录…", self.choose_history))
        row.addWidget(button("刷新记录", self.refresh_history))
        layout.addLayout(row)
        self.history_table = table(["开始时间", "录制名称", "结果", "消息数", "大小"])
        self.history_table.setMinimumHeight(100)
        self.history_table.itemSelectionChanged.connect(self._history_details)
        layout.addWidget(self.history_table, 1)
        row = QHBoxLayout()
        row.addWidget(button("复用所选配置", self.reuse_session, True))
        row.addWidget(button("打开 bag 目录", lambda: self.open_session_location(False)))
        row.addWidget(button("打开日志与报告目录", lambda: self.open_session_location(True)))
        row.addWidget(button("离线统计所选 bag", self.analyze_selected_session))
        layout.addLayout(row)
        self.report_details = QPlainTextEdit()
        self.report_details.setReadOnly(True)
        self.report_details.setMinimumHeight(90)
        layout.addWidget(self.report_details, 1)

    def _analysis_page(self, layout):
        self.analysis_widget = LiveStampPage(self)
        self.analysis_widget.busyChanged.connect(self._analysis_state_changed)
        layout.addWidget(self.analysis_widget)

    def _analysis_state_changed(self):
        self._update_profile_load_controls()
        self._maybe_close()

    def analyze_selected_session(self):
        session = self._selected_session()
        if session and not self._busy_guard():
            self.nav.setCurrentRow(4)
            self.analysis_widget.open_bag(session['bag_path'])

    def run_task(self, function, result=None, failed=None, log=None):
        task = Task(function, self)
        self.tasks.append(task)
        if result:
            task.result.connect(result)
        task.failed.connect(failed or self._error)
        if log:
            task.log.connect(log)
        def ended():
            if task in self.tasks:
                self.tasks.remove(task)
            task.deleteLater()
            self._update_profile_load_controls()
            self._maybe_close()
        task.ended.connect(ended)
        task.start()
        self._update_profile_load_controls()
        return task

    def _append(self, editor, text):
        editor.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    def clear_recording_log(self):
        self.event_log.clear()

    def _error(self, message):
        self._append(self.event_log, message)
        if not self.pending_close:
            QMessageBox.warning(self, "操作未完成", message)

    def _busy_message(self):
        if self.is_recording:
            return "录制尚未结束，请先停止并保存后再操作。"
        if self.preflight_busy:
            return "正在准备录制，请等待准备完成后再操作。"
        if self.build_runner:
            return "正在编译消息包，请等待编译完成或取消后再操作。"
        if self.env_busy:
            return "正在加载或检查消息环境，请等待完成后再操作。"
        if self.benchmark_busy:
            return "正在进行目录写入测速，请等待测速完成后再操作。"
        if self.analysis_widget.busy:
            return "时间戳监听或离线分析仍在运行，请先在“数据分析”中停止监听或取消读取后再操作。"
        if self.calibration_widget and self.calibration_widget.busy:
            return "标定订阅尚未结束；离开标定页面后会自动停止，请稍候再操作。"
        return ""

    def _update_profile_load_controls(self):
        if not hasattr(self, 'load_profile_from_list_button'):
            return
        message = self._busy_message()
        self.profile_combo.setEnabled(not message)
        self.load_profile_button.setEnabled(not message)
        self.load_profile_from_list_button.setEnabled(not message)
        self.load_profile_button.setText("加载中…" if self.env_busy else "加载")
        self.load_profile_button.setToolTip(message or "应用配置方案；相同的消息环境会直接复用。")
        self.load_profile_from_list_button.setToolTip(message)
        if self.calibration_widget:
            self.calibration_widget.update_controls()

    def _busy_guard(self):
        message = self._busy_message()
        if message:
            self._error(message)
            return True
        return False

    @property
    def is_recording(self):
        return self.session is not None and not self.session.finished

    def _navigate(self, index):
        if index >= 5 and not self.admin_unlocked:
            self.nav.setCurrentRow(0)
            return
        self.stack.setCurrentIndex(index)
        if self.calibration_widget:
            self.calibration_widget.set_active(index == 6)
        if index == 1 and hasattr(self, 'history_root') and not self.history_root.text():
            self.history_root.setText(self.output_root.text())
            self.refresh_history()

    def _storage_changed(self):
        if not hasattr(self, 'preset'):
            return
        self.preset.clear()
        self.preset.addItems(["默认", "resilient"] if self.storage.currentText() == "sqlite3" else ["默认", "fastwrite", "zstd_fast", "zstd_small"])
        self._apply_capabilities()

    def _apply_capabilities(self):
        caps = self.capabilities
        if caps is None or not hasattr(self, 'cache_note'):
            return
        def supported(widget, option, neutral=0):
            available = option in caps.options
            if isinstance(widget, QLineEdit):
                active = bool(widget.text())
            elif isinstance(widget, QCheckBox):
                active = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                active = widget.value() != neutral
            else:
                active = False
            widget.setEnabled(available or active)
            widget.setToolTip('' if available else f'当前 ROS 2 录制器不支持 {option}')
        for widget, option in ((self.split_seconds, '--max-bag-duration'),
                               (self.qos_path, '--qos-profile-overrides-path'),
                               (self.storage_config, '--storage-config-file'), (self.include_hidden, '--include-hidden-topics'),
                               (self.use_sim_time, '--use-sim-time'),
                               (self.compression_threads, '--compression-threads')):
            supported(widget, option)
        supported(self.compression_queue, '--compression-queue-size', neutral=4)
        for i in range(self.preset.count()):
            self.preset.model().item(i).setEnabled(i == 0 or '--storage-preset-profile' in caps.options)
        self.preset.setToolTip('' if '--storage-preset-profile' in caps.options else '当前录制器仅支持默认存储配置')
        for widget, option in ((self.qos_path, '--qos-profile-overrides-path'), (self.storage_config, '--storage-config-file')):
            widget.parentWidget().setEnabled(option in caps.options or bool(widget.text()))
        for i in range(self.storage.count()):
            self.storage.model().item(i).setEnabled(self.storage.itemText(i) in caps.storage_ids)
        for i in range(self.compression.count()):
            self.compression.model().item(i).setEnabled(self.compression.itemText(i) in caps.compression_modes)
        for widget, form, visible in ((self.cache, self.settings_form, caps.cache_unit != 'messages'),
                                      (self.cache_messages, self.settings_form, caps.cache_unit == 'messages'),
                                      (self.cache_bytes, self.advanced_form, caps.cache_unit != 'messages')):
            widget.setVisible(visible)
            form.labelForField(widget).setVisible(visible)
        if caps.cache_unit == 'messages':
            self.cache_note.setText('当前录制器按消息条数缓存，内存占用取决于每条消息大小；0 表示直接写入。')
        elif caps.double_buffered:
            self.cache_note.setText('当前录制器采用双缓冲，峰值缓存内存可能约为设置值的两倍，另需留出程序和消息内存。')
        else:
            self.cache_note.setText('缓存仅缓解短时写入堵塞；实际内存与缓存实现、消息大小有关。')

    def _preset_hint(self):
        if not hasattr(self, 'preset_note'):
            return
        hints = {
            "fastwrite": "高速写入：省略消息索引及部分校验。若需高效检索或跳转，录后应转换并补建索引。",
            "zstd_fast": "MCAP 块级快速压缩，节省磁盘但消耗部分 CPU，需结合实测选择。",
            "zstd_small": "更高压缩率，CPU 开销较大；高带宽录制时谨慎使用。",
            "resilient": "SQLite 崩溃恢复优先，可能降低写入速度。",
        }
        self.preset_note.setText(hints.get(self.preset.currentText(), "缓存只缓解短时堵塞；磁盘持续写速仍需跟上数据量。"))

    def get_config(self):
        config = RecordConfig(
            output_root=self.output_root.text().strip(), prefix=self.prefix.text().strip(),
            all_topics=False, topics=sorted(self.selected_topics),
            storage=self.storage.currentText(), preset="" if self.preset.currentIndex() == 0 else self.preset.currentText(),
            cache_mib=self.cache.value(), split_mib=self.split_size.value(), split_seconds=self.split_seconds.value(),
            cache_messages=self.cache_messages.value(),
            cache_bytes=int(self.cache_bytes.text().strip()) if self.cache_bytes.text().strip() else -1,
            split_bytes=int(self.split_bytes.text().strip()) if self.split_bytes.text().strip() else -1,
            compression=self.compression.currentText(), compression_queue=self.compression_queue.value(),
            compression_threads=self.compression_threads.value(), qos_path=self.qos_path.text().strip(),
            storage_config=self.storage_config.text().strip(), include_hidden=self.include_hidden.isChecked(),
            use_sim_time=self.use_sim_time.isChecked(), min_free_gib=self.min_free.value(),
            auto_stop_low_disk=self.auto_stop_disk.isChecked(), workspace=self.active_workspace,
            packages=list(self.active_packages), environment_setup=self.loaded_setup,
            auto_detect_frequency=self.auto_frequency.isChecked(),
            base_environment_setup=self.base_setup,
        )
        config.validate()
        return config

    def apply_config(self, config, *, load_env=True):
        self.output_root.setText(config.output_root)
        self.prefix.setText(config.prefix)
        # Older profiles used implicit scopes instead of an explicit selection.
        # Apply their parameters without clearing the user's current checkboxes
        # or silently enabling an obsolete recording scope.
        if not (config.all_topics or config.regex or config.exclude):
            self.selected_topics = set(config.topics)
        self.storage.setCurrentText(config.storage)
        self._storage_changed()
        self.preset.setCurrentText(config.preset or "默认")
        self.cache.setValue(config.cache_mib)
        self.cache_messages.setValue(config.cache_messages)
        self.split_size.setValue(config.split_mib)
        self.cache_bytes.setText(str(config.cache_bytes) if config.cache_bytes >= 0 else "")
        self.split_bytes.setText(str(config.split_bytes) if config.split_bytes >= 0 else "")
        self.advanced_toggle.setChecked(config.cache_bytes >= 0 or config.split_bytes >= 0)
        self.split_seconds.setValue(config.split_seconds)
        self.compression.setCurrentText(config.compression)
        self.compression_queue.setValue(config.compression_queue)
        self.compression_threads.setValue(config.compression_threads)
        self.qos_path.setText(config.qos_path)
        self.storage_config.setText(config.storage_config)
        self.include_hidden.setChecked(config.include_hidden)
        self.use_sim_time.setChecked(config.use_sim_time)
        self.min_free.setValue(config.min_free_gib)
        self.auto_stop_disk.setChecked(config.auto_stop_low_disk)
        self.workspace_path.setText(config.workspace)
        # Detection is a session control: saved recording profiles must not
        # override the startup default or the user's current manual choice.
        self.history_root.setText(config.output_root)
        self._render_topics()
        self._preset_hint()
        if load_env:
            if config.base_environment_setup != self.base_setup:
                self.stop_all_monitors()
                self._initialize_environment(config)
            else:
                same_environment = (self.active_env is not None
                                    and config.environment_setup == self.loaded_setup
                                    and config.workspace == self.active_workspace
                                    and sorted(config.packages) == sorted(self.active_packages))
                if same_environment:
                    self._auto_frequency_changed(self.auto_frequency.isChecked())
                else:
                    self._request_environment(config.environment_setup, config.workspace, config.packages)
        elif self.active_env:
            self._auto_frequency_changed(self.auto_frequency.isChecked())

    def _initialize_environment(self, config):
        self.env_busy = True
        def initialize(log):
            base = load_environment(base_setup=config.base_environment_setup)
            return base, detect_capabilities(base)
        def loaded(value):
            base, caps = value
            self.base_env = base
            self.workspace = None
            self.package_list.clear()
            self.base_setup = config.base_environment_setup
            base_index = self.base_environment.findData(self.base_setup)
            if base_index >= 0:
                self.base_environment.setCurrentIndex(base_index)
            else:
                self.base_environment.setEditText(self.base_setup)
            self.capabilities = caps
            self._apply_capabilities()
            self.env_busy = False
            self._set_environment(base, "", "", [])
            if config.environment_setup:
                self._request_environment(config.environment_setup, config.workspace, config.packages)
        def failed(message):
            self.env_busy = False
            if not self.active_env:
                self.environment_label.setText("ROS 环境加载失败，请在“消息环境”中选择基础环境")
                self.header_env.setText("环境未就绪")
            else:
                self._auto_detect_frequencies()
            self._error(message)
        self.run_task(initialize, loaded, failed)

    def select_base_environment(self):
        if self._busy_guard():
            return
        self.stop_all_monitors()
        index = self.base_environment.currentIndex()
        text = self.base_environment.currentText().strip()
        setup = self.base_environment.itemData(index) if index >= 0 and text == self.base_environment.itemText(index) else text
        try:
            config = self.get_config()
        except ValueError:
            config = RecordConfig()
        config.base_environment_setup = setup or ''
        config.environment_setup = ''
        config.workspace = ''
        config.packages = []
        self._initialize_environment(config)

    def _request_environment(self, setup, workspace="", packages=None):
        if self._busy_guard():
            return
        self.stop_all_monitors()
        self.env_busy = True
        self.env_status.setText("正在加载环境并验证录制组件…")
        def work(log):
            env = load_environment(setup, self.base_env)
            types = validate_message_types(env, packages or [])
            return env, types, detect_capabilities(env)
        def loaded(value):
            env, types, caps = value
            self.capabilities = caps
            self._apply_capabilities()
            self.env_busy = False
            self._set_environment(env, setup, workspace, packages or [])
            if types:
                self._append(self.environment_log, f"已验证 {len(types)} 个自定义消息类型：" + ', '.join(types))
        def failed(message):
            self.env_busy = False
            self.env_status.setText("加载失败，保留上一套可用环境")
            self._append(self.environment_log, message)
            self._auto_detect_frequencies()
            self._error(message)
        self.run_task(work, loaded, failed)

    def _set_environment(self, env, setup, workspace, packages):
        if env != self.active_env:
            self.topics = []
        self.active_env = env
        self.loaded_setup = setup
        self.active_workspace = workspace
        self.active_packages = list(packages)
        self.frequency_failed.clear()
        domain = env.get('ROS_DOMAIN_ID', '0')
        self.header_env.setText(f"ROS 2 · Domain {domain} · {env.get('RMW_IMPLEMENTATION', '默认 RMW')}")
        self.header_env.setToolTip('实际检测到的 ROS 2 发行版：' + env.get('ROS_DISTRO', '未知'))
        label = f"自定义环境：{setup}" if setup else "当前 ROS 2 基础环境"
        self.environment_label.setText("消息环境：" + label)
        self.env_status.setText(label)
        self._append(self.environment_log, "已加载：" + label)
        self._render_topics()
        self._notify_stamp_topics()
        if self.auto_discover and not self.pending_close:
            self.refresh_topics(manual=False)

    def refresh_topics(self, *, manual=True):
        if self.pending_close or self.frequency_suspended or self.is_recording or self.preflight_busy:
            return
        if self.discovery_busy:
            if manual:
                # A manual click can join an already running background probe.
                self.discovery_manual = True
                self.refresh_topics_button.setEnabled(False)
                self.refresh_topics_button.setText("正在发现…")
            else:
                self.discovery_requested = True
            return
        if not self.active_env or self.env_busy or self.build_runner:
            self.discovery_requested = True
            return
        self.discovery_requested = False
        self.discovery_busy = True
        self.discovery_manual = manual
        self.last_discovery = time.monotonic()
        if manual:
            self.refresh_topics_button.setEnabled(False)
            self.refresh_topics_button.setText("正在发现…")
        env = dict(self.active_env)
        hidden = self.include_hidden.isChecked()
        def finish():
            manual_requested = self.discovery_manual
            self.discovery_busy = False
            self.discovery_manual = False
            self.refresh_topics_button.setEnabled(True)
            self.refresh_topics_button.setText("刷新话题")
            stale = env != self.active_env or hidden != self.include_hidden.isChecked()
            if (stale or self.discovery_requested) and not self.pending_close:
                QTimer.singleShot(0, lambda: self.refresh_topics(manual=manual_requested))
            return stale or self.pending_close
        def done(topics):
            if finish():
                return
            self.topics = topics
            self._auto_detect_frequencies()
            self._render_topics()
            self._notify_stamp_topics()
        def failed(message):
            if finish():
                return
            self._append(self.event_log, message)
            self.topic_count.setText("发现失败，请检查 Domain、网络和消息环境")
        self.run_task(lambda _: discover_topics(env, hidden), done, failed)

    def _render_topics(self):
        if not hasattr(self, 'topic_table'):
            return
        self.topic_table.blockSignals(True)
        rows = {t['name']: t for t in self.topics}
        # Only explicit manual additions get offline rows. Saved profile
        # selections remain separate from the currently displayed topic list.
        for name in self.manual_topics:
            rows.setdefault(name, {"name": name, "types": ["手动添加，未发现"], "publishers": 0,
                                   "available": True, "pending": True,
                                   "reason": "手动添加的话题尚未发现；发现后会自动显示真实消息类型。取消勾选可取消录制。"})
        self.display_topics = [rows[k] for k in sorted(rows)]
        self.topic_table.setRowCount(len(rows))
        for row, topic in enumerate(self.display_topics):
            name = topic['name']
            check = QTableWidgetItem()
            flags = Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
            if not self.is_recording and not self.preflight_busy:
                flags |= Qt.ItemIsEnabled
            check.setFlags(flags)
            check.setCheckState(Qt.Checked if name in self.selected_topics else Qt.Unchecked)
            check.setData(Qt.UserRole, name)
            self.topic_table.setItem(row, 0, check)
            label = QTableWidgetItem(name)
            label.setToolTip(name)
            type_item = QTableWidgetItem(', '.join(topic['types']))
            type_item.setToolTip(topic.get('reason') or ', '.join(topic['types']))
            if not topic['available']:
                label.setForeground(QColor("#b44b36"))
                type_item.setForeground(QColor("#b44b36"))
            self.topic_table.setItem(row, 1, label)
            self.topic_table.setItem(row, 2, type_item)
            pubs = QTableWidgetItem(str(topic['publishers']))
            pubs.setToolTip(topic.get('qos', ''))
            self.topic_table.setItem(row, 3, pubs)
            value = self._frequency_text(name)
            hz_item = QTableWidgetItem(value)
            hz_item.setToolTip(self._frequency_tooltip(name))
            self.topic_table.setItem(row, 4, hz_item)
            reason = ""
            if self.frequency_suspended:
                state, reason = "已暂停", "录制或时间戳监听期间暂停频率检测，结束后恢复。"
            elif self.env_busy or self.build_runner:
                state, reason = "加载中", "请等待消息环境加载完成。"
            elif topic.get('pending'):
                state, reason = "待发现", "当前环境尚未发现该话题。请检查发布程序、Domain 和网络；空闲时会自动刷新。"
            elif not topic['available']:
                state, reason = "类型未加载", topic.get('reason') or "请在消息环境中编译并加载该消息包。"
            elif len(topic['types']) != 1:
                state, reason = "类型冲突", "同名话题存在多个消息类型，请检查发布程序。"
            else:
                state = "关闭" if name in self.monitors else "检测"
            toggle = button(state, lambda checked=False, n=name: self.toggle_frequency(n))
            toggle.setEnabled(not reason)
            toggle.setToolTip(reason or ("关闭此话题的频率检测" if name in self.monitors else "检测此话题的接收频率"))
            self.topic_table.setCellWidget(row, 5, toggle)
            self.topic_table.setRowHeight(row, 36)
        self.topic_table.blockSignals(False)
        self._filter_topic_rows()

    def _notify_stamp_topics(self):
        # Only propagate a new graph result/environment, not incidental redraws
        # of the recording table (e.g. pausing its frequency subscriptions).
        if hasattr(self, 'analysis_widget') and hasattr(self.analysis_widget, 'set_discovered_topics'):
            self.analysis_widget.set_discovered_topics(self.topics)

    def _filter_topic_rows(self):
        if not hasattr(self, 'topic_table'):
            return
        keywords = self.topic_search.text().casefold().split()
        visible = 0
        hidden_selected = 0
        for row, topic in enumerate(self.display_topics):
            text = (topic['name'] + ' ' + ' '.join(topic['types'])).casefold()
            show = all(word in text for word in keywords)
            self.topic_table.setRowHidden(row, not show)
            visible += show
            hidden_selected += not show and topic['name'] in self.selected_topics
        if hasattr(self, 'topic_count'):
            listed_selected = sum(t['name'] in self.selected_topics for t in self.display_topics)
            message = f'显示 {visible}/{len(self.display_topics)} · 已勾选 {listed_selected}'
            if hidden_selected:
                message += f'（含搜索结果外 {hidden_selected} 个）'
            self.topic_count.setText(message)

    def select_visible_topics(self, selected):
        if self.is_recording or self.preflight_busy:
            return
        self.topic_table.blockSignals(True)
        for row, topic in enumerate(self.display_topics):
            if self.topic_table.isRowHidden(row):
                continue
            if selected:
                self.selected_topics.add(topic['name'])
            else:
                self.selected_topics.discard(topic['name'])
            self.topic_table.item(row, 0).setCheckState(Qt.Checked if selected else Qt.Unchecked)
        self.topic_table.blockSignals(False)
        self._filter_topic_rows()

    def _topic_selection_changed(self, item):
        if item.column() != 0:
            return
        name = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.selected_topics.add(name)
        else:
            self.selected_topics.discard(name)
        self._filter_topic_rows()

    def add_topic(self):
        if self.is_recording:
            return
        name, accepted = QInputDialog.getText(self, "添加话题", "完整话题名称（可添加尚未上线的话题）：")
        if accepted and name.strip():
            name = name.strip()
            if not name.startswith('/') or any(c.isspace() for c in name):
                self._error("请填写以 / 开头且没有空格的话题名称")
                return
            self.selected_topics.add(name)
            self.manual_topics.add(name)
            self._render_topics()
            if not any(t['name'] == name for t in self.topics):
                self._append(self.event_log, f"已加入录制选择：{name}；列表已标记为“手动添加，未发现”，发现后会自动更新消息类型。")

    def toggle_frequency(self, name):
        if self.frequency_suspended:
            return
        if name in self.monitors:
            self.frequency_disabled.add(name)
            self.monitors.pop(name)
            self._sync_frequency_monitor()
            self._render_topics()
            return
        if not self.active_env or self.env_busy or self.build_runner:
            self._error("请先完成消息环境加载")
            return
        topic = next((t for t in self.topics if t['name'] == name), None)
        if not topic or not topic['available'] or len(topic['types']) != 1:
            return
        self.frequency_disabled.discard(name)
        self.frequency_failed.discard(name)
        self.frequency_results[name] = {"waiting": True}
        self.monitors[name] = topic['types'][0]
        self._sync_frequency_monitor()
        self._render_topics()

    def _auto_frequency_changed(self, enabled):
        if enabled:
            self._auto_detect_frequencies()
        else:
            self.stop_all_monitors()
        self._render_topics()

    def _auto_detect_frequencies(self):
        if (not self.active_env or self.frequency_suspended or self.pending_close
                or self.env_busy or self.build_runner or not self.auto_frequency.isChecked()):
            return
        available = {t['name']: t['types'][0] for t in self.topics if t['available'] and len(t['types']) == 1}
        for name in list(self.monitors):
            if available.get(name) != self.monitors[name]:
                self.monitors.pop(name)
        for name, message_type in available.items():
            if name not in self.frequency_disabled and name not in self.frequency_failed and name not in self.monitors:
                self.monitors[name] = message_type
                self.frequency_results[name] = {'waiting': True}
        self._sync_frequency_monitor()

    def _sync_frequency_monitor(self):
        if not self.monitors:
            if self.frequency_monitor:
                self.frequency_monitor.stop()
            return
        if self.frequency_suspended or self.pending_close or self.env_busy or self.build_runner:
            return
        try:
            if self.frequency_monitor:
                if not self.frequency_monitor.stopping:
                    self.frequency_monitor.update(self.monitors)
                return
            monitor = FrequencyMonitor(dict(self.active_env), self.monitors,
                                       on_result=self.signals.frequency.emit,
                                       on_done=lambda code: self.signals.monitor_done.emit('', code))
            self.frequency_monitor = monitor
            monitor.start()
        except Exception as exc:
            self._append(self.event_log, '无法启动频率检测：' + str(exc))
            self.frequency_failed.update(self.monitors)
            self.monitors.clear()

    def _pause_frequency_for_recording(self):
        self.frequency_resume = dict(self.monitors)
        self.frequency_suspended = True
        self.auto_frequency.setEnabled(False)
        monitor = self.frequency_monitor
        self.stop_all_monitors()
        self._render_topics()
        return monitor

    def _resume_frequency_after_recording(self):
        self.frequency_suspended = False
        self.auto_frequency.setEnabled(True)
        if not self.pending_close:
            known = {t['name']: t['types'][0] for t in self.topics if t['available'] and len(t['types']) == 1}
            for name, message_type in self.frequency_resume.items():
                if known.get(name) == message_type and name not in self.frequency_disabled:
                    self.monitors[name] = message_type
                    self.frequency_results[name] = {'waiting': True}
            self._auto_detect_frequencies()
            self._sync_frequency_monitor()
        self.frequency_resume.clear()
        self._render_topics()

    def _frequency_update(self, name, result):
        if name not in self.monitors or self.frequency_suspended:
            return
        self.frequency_results[name] = result
        if 'error' in result:
            self.frequency_failed.add(name)
            self.monitors.pop(name, None)
            self._append(self.event_log, f"{name} 频率检测失败：{result['error']}")
            self._sync_frequency_monitor()
            self._render_topics()
            return
        for row, topic in enumerate(self.display_topics):
            if topic['name'] == name:
                item = self.topic_table.item(row, 4)
                item.setText(self._frequency_text(name))
                item.setToolTip(self._frequency_tooltip(name))

    def _frequency_text(self, name):
        if self.frequency_suspended:
            return "检测已暂停"
        if not any(t['name'] == name for t in self.topics):
            return "等待发现"
        result = self.frequency_results.get(name, {})
        if 'error' in result:
            return "检测失败"
        if name not in self.monitors:
            return "未检测"
        if result.get('waiting'):
            return "等待消息…"
        if result.get('stale'):
            age = result.get('age')
            return "尚无消息" if age is None else f"{age:.0f}s 无消息"
        hz = result.get('hz')
        return f"{hz:.2f} Hz" if hz is not None else "采样中…"

    def _frequency_tooltip(self, name):
        if self.frequency_suspended:
            return "准备和进行录制时停止频率检测；停止并保存后恢复之前开启的话题。"
        if not any(t['name'] == name for t in self.topics):
            return "当前环境尚未发现该话题，发现后会按自动检测开关启动检测。"
        result = self.frequency_results.get(name, {})
        if 'error' in result:
            return result['error']
        if result.get('min_ms') is None:
            return "最近 5 秒、最多 2000 个样本；本机接收频率，非发送端计数或 bag 写入频率。"
        return f"样本：{result['samples']}\n帧间隔：{result['min_ms']:.2f}～{result['max_ms']:.2f} ms\n间隔标准差：{result['jitter_ms']:.2f} ms\n最近 5 秒接收窗口；低频话题可能显示无新消息。"

    def _monitor_done(self, name, code):
        monitor = self.frequency_monitor
        self.frequency_monitor = None
        if code and monitor and not monitor.stopping:
            self.frequency_failed.update(self.monitors)
            for topic in self.monitors:
                self.frequency_results[topic] = {'error': f'检测进程退出（{code}）'}
            self.monitors.clear()
            self._append(self.event_log, f'频率检测进程退出（{code}），录制功能不受影响。')
        self._sync_frequency_monitor()
        self._render_topics()
        self._maybe_close()

    def stop_all_monitors(self):
        self.monitors.clear()
        if self.frequency_monitor:
            self.frequency_monitor.stop()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择录制保存目录", self.output_root.text())
        if path:
            self.output_root.setText(path)
            self.history_root.setText(path)

    def choose_yaml(self, target):
        path, _ = QFileDialog.getOpenFileName(self, "选择配置文件", "", "YAML (*.yaml *.yml);;所有文件 (*)")
        if path:
            target.setText(path)

    def choose_workspace(self):
        if self._busy_guard():
            return
        path = QFileDialog.getExistingDirectory(self, "选择 ROS 2 工作空间或消息包", self.workspace_path.text())
        if path:
            self.workspace_path.setText(path)
            self.scan_workspace()

    def scan_workspace(self):
        if self._busy_guard():
            return
        raw = self.workspace_path.text().strip()
        self.env_busy = True
        self.scan_button.setEnabled(False)
        def done(workspace):
            self.workspace = workspace
            self.env_busy = False
            self.scan_button.setEnabled(True)
            self.package_list.clear()
            for package in workspace.packages:
                suffix = f" · {len(package['interfaces'])} 个接口文件" if package['interfaces'] else " · 依赖/普通包"
                item = QListWidgetItem(package['name'] + suffix)
                item.setData(Qt.UserRole, package['name'])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if package['interfaces'] else Qt.Unchecked)
                item.setToolTip(package['path'])
                self.package_list.addItem(item)
            self._append(self.environment_log, f"找到 {len(workspace.packages)} 个包；编译输出：{workspace.build_root}")
        def failed(message):
            self.env_busy = False
            self.scan_button.setEnabled(True)
            self.workspace = None
            self._error(message)
        self.run_task(lambda _: inspect_workspace(raw, self.base_env), done, failed)

    def build_workspace(self):
        if self._busy_guard():
            return
        if not self.workspace or self.workspace.path != Path(self.workspace_path.text()).expanduser().resolve():
            self._error("请先扫描当前消息包或工作空间，并勾选要编译的包")
            return
        selected = [self.package_list.item(i).data(Qt.UserRole) for i in range(self.package_list.count())
                    if self.package_list.item(i).checkState() == Qt.Checked]
        try:
            if not self.base_env:
                raise ValueError("ROS 2 基础环境尚未就绪")
            build_workspace = replace(self.workspace, build_root=self.workspace.build_root / "builds" / f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}")
            argv = build_command(build_workspace, selected)
            build_workspace.build_root.mkdir(parents=True, exist_ok=False)
        except Exception as exc:
            self._error(str(exc))
            return
        self.stop_all_monitors()
        self._building_workspace = build_workspace
        self._building_packages = selected
        self._append(self.environment_log, "编译：" + shlex.join(argv))
        self.env_status.setText("正在编译消息包及其工作空间依赖…")
        self.cancel_build_button.setEnabled(True)
        self.build_button.setEnabled(False)
        self.build_cancelled = False
        self.build_runner = ProcessRunner(argv, dict(self.base_env), cwd=str(build_workspace.build_root),
                                          line=self.build_signals.line.emit, done=self.signals.build_done.emit)
        self.build_runner.start()
        self._update_profile_load_controls()

    def _build_log(self, line):
        self._append(self.environment_log, line)

    def cancel_build(self):
        if self.build_runner:
            self.build_cancelled = True
            self.build_runner.stop()
            self.env_status.setText("正在取消编译…")

    def _build_done(self, code):
        self.build_runner = None
        self._update_profile_load_controls()
        self.cancel_build_button.setEnabled(False)
        self.build_button.setEnabled(True)
        if code == 0 and not self.pending_close and not self.build_cancelled:
            try:
                atomic_json(self._building_workspace.build_root / "build-success.json", {"setup": str(self._building_workspace.setup)})
            except OSError as exc:
                self.env_status.setText("编译完成，但保存编译状态失败")
                self._error(str(exc))
                return
            self._append(self.environment_log, "编译成功，开始加载消息环境")
            self._request_environment(str(self._building_workspace.setup), str(self._building_workspace.path), self._building_packages)
        else:
            reason = "已取消编译" if self.build_cancelled else "编译未完成"
            self.env_status.setText(f"{reason}（退出码 {code}），保留上一套可用环境")
            self._auto_detect_frequencies()
        self._maybe_close()

    def load_existing_environment(self):
        if self._busy_guard():
            return
        try:
            workspace = inspect_workspace(self.workspace_path.text().strip(), self.base_env)
            completed = sorted(workspace.build_root.glob("builds/*/build-success.json"), reverse=True)
            candidates = [p.parent / "install" / "setup.bash" for p in completed]
            candidates += [workspace.setup, workspace.path / "install" / "setup.bash"]
            setup = next((p for p in candidates if p.is_file()), None)
            if setup is None:
                chosen, _ = QFileDialog.getOpenFileName(self, "选择已编译环境 setup.bash", str(workspace.path), "Bash 环境 (*.bash)")
                if not chosen:
                    return
                setup = Path(chosen)
            selected = [p['name'] for p in workspace.packages if p['interfaces']]
            self._request_environment(str(setup), str(workspace.path), selected)
        except Exception as exc:
            self._error(str(exc))

    def start_recording(self):
        if self._busy_guard():
            return
        if not self.active_env:
            self._error("请先加载可用的 ROS 2 环境")
            return
        try:
            config = self.get_config()
        except Exception as exc:
            self._error(str(exc))
            return
        self.preflight_busy = True
        self.start_button.setEnabled(False)
        self.metric_values['state'].setText("检查环境…")
        previous_monitor = self._pause_frequency_for_recording()
        self._set_recording_controls(True)
        env = dict(self.active_env)
        def check(log):
            if previous_monitor and not previous_monitor.wait(8):
                previous_monitor.stop(force=True)
                if not previous_monitor.wait(3):
                    raise ValueError("频率检测进程尚未退出，已取消启动录制")
            import subprocess
            caps = detect_capabilities(env)
            caps.command(config, Path(config.output_root) / 'capability_check')
            topics = discover_topics(env, config.include_hidden)
            def wanted(t):
                return t['name'] in config.topics
            missing = [t['name'] for t in topics if wanted(t) and (not t['available'] or len(t['types']) != 1)]
            if missing:
                raise ValueError("以下话题消息类型未加载或存在类型冲突，请先处理消息环境：\n" + '\n'.join(missing))
            if config.use_sim_time and not any(t['name'] == '/clock' and t['publishers'] for t in topics):
                log("注意：未发现 /clock 发布端，收到仿真时钟前 rosbag 不会写入消息。")
            return config, topics, caps
        def done(value):
            checked, topics, caps = value
            self.preflight_busy = False
            if self.pending_close:
                self.start_button.setEnabled(True)
                self.metric_values['state'].setText("已取消启动")
                self._set_recording_controls(False)
                self._resume_frequency_after_recording()
                return
            try:
                self.store.save(checked)
                self.session = RecordingSession(checked, env, on_line=self.signals.line.emit,
                                                on_done=self.signals.done.emit, capabilities=caps)
                self.session.start()
            except Exception as exc:
                self.session = None
                self.start_button.setEnabled(True)
                self.metric_values['state'].setText("启动失败")
                self._set_recording_controls(False)
                self._resume_frequency_after_recording()
                self._error(str(exc))
                return
            self.stop_time = None
            self.topics = topics
            self._set_recording_controls(True)
            self.stop_button.setEnabled(True)
            self.metric_values['state'].setText("进程运行中")
            self.metric_values['time'].setText("00:00:00")
            self.metric_values['size'].setText("0.0 MiB")
            self.metric_values['warnings'].setText("0")
            self.record_path_label.setText("当前录制：" + str(self.session.output))
            self._append(self.event_log, "开始录制：" + str(self.session.output))
        def failed(message):
            self.preflight_busy = False
            self.start_button.setEnabled(True)
            self.metric_values['state'].setText("未开始")
            self._set_recording_controls(False)
            self._resume_frequency_after_recording()
            self._error(message)
        self.run_task(check, done, failed, lambda text: self._append(self.event_log, text))

    def _record_line(self, line):
        self._append(self.event_log, line)

    def _set_recording_controls(self, recording):
        self.parameter_box.setEnabled(not recording)
        self.select_visible_button.setEnabled(not recording)
        self.clear_visible_button.setEnabled(not recording)
        self.add_topic_button.setEnabled(not recording)
        self._update_profile_load_controls()
        self._render_topics()

    def stop_recording(self):
        if self.is_recording:
            self.session.stop()
            self.stop_time = self.stop_time or time.monotonic()
            self.stop_button.setEnabled(False)
            self.metric_values['state'].setText("等待写完…")
            self._append(self.event_log, "已请求停止；正在等待缓存写入和文件关闭，请勿拔出磁盘。")

    def force_stop(self):
        if not self.is_recording:
            return
        answer = QMessageBox.question(self, "强制停止录制", "强制停止可能留下未写完的数据和不完整元数据。确实要强制结束录制进程？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.session.stop(force=True)
            self._append(self.event_log, "用户选择强制停止，本次录制将标记为失败/未完成。")

    def _record_done(self, report):
        self._set_recording_controls(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.force_button.hide()
        states = {"completed": "已结束", "empty": "空录制", "failed": "录制失败"}
        self.metric_values['state'].setText(states.get(report.get('status'), "结束"))
        self.metric_values['size'].setText(size_text(report.get('size_bytes', 0)))
        self.metric_values['growth'].setText("—")
        self.metric_values['warnings'].setText(str(len(report.get('warnings', []))))
        if self.session and self.session.started_at:
            seconds = int(time.monotonic() - self.session.started_at)
            self.metric_values['time'].setText(f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}")
        self._append(self.event_log, f"录制结束：{states.get(report.get('status'), '未知')} · 消息数 {report.get('message_count', '未知')} · 完整性待核对")
        if report.get('error'):
            self._append(self.event_log, report['error'])
        self._resume_frequency_after_recording()
        self.history_root.setText(self.output_root.text())
        if not self.pending_close:
            self.refresh_history()
            self._show_recording_summary(report)
        self._maybe_close()

    def _show_recording_summary(self, report):
        if self.recording_summary:
            self.recording_summary.close()
        dialog = RecordingSummaryDialog(report, self)
        self.recording_summary = dialog
        def cleared():
            if self.recording_summary is dialog:
                self.recording_summary = None
        dialog.finished.connect(cleared)
        dialog.open()

    def _tick(self):
        if (self.auto_discover and not self.pending_close and not self.is_recording
                and not self.preflight_busy and not self.frequency_suspended
                and not self.env_busy and not self.build_runner and not self.discovery_busy
                and self.active_env and time.monotonic() - self.last_discovery > 5):
            self.refresh_topics(manual=False)
        if not self.is_recording and not self.idle_disk_busy and not self.pending_close and time.monotonic() - self.last_idle_disk > 5:
            self.idle_disk_busy = True
            self.last_idle_disk = time.monotonic()
            raw = self.output_root.text().strip()
            def space(_):
                path = Path(raw).expanduser().absolute()
                while not path.exists() and path != path.parent:
                    path = path.parent
                return shutil.disk_usage(path).free
            def space_done(free):
                self.idle_disk_busy = False
                if not self.is_recording:
                    self.metric_values['free'].setText(disk_space_text(free))
                    self.metric_values['free'].setToolTip("按十进制 GB 显示（1 GB = 1,000,000,000 字节）。保存目录尚不存在时，显示最近已存在父目录的可用空间。")
            def space_failed(_):
                self.idle_disk_busy = False
            if raw:
                self.run_task(space, space_done, space_failed)
            else:
                self.idle_disk_busy = False
        if self.is_recording and not self.metric_busy:
            self.metric_busy = True
            session = self.session
            def done(values):
                self.metric_busy = False
                if session is not self.session or session.finished:
                    return
                duration = int(values['elapsed'])
                self.metric_values['time'].setText(f"{duration // 3600:02}:{duration // 60 % 60:02}:{duration % 60:02}")
                self.metric_values['size'].setText(size_text(values['size']))
                self.metric_values['growth'].setText(f"{values['growth_bytes_per_sec'] / MIB:.1f} MiB/s")
                self.metric_values['free'].setText(disk_space_text(values['free']))
                self.metric_values['warnings'].setText(str(values['warning_count']))
                if session.stopped and not session.finished:
                    self.stop_time = self.stop_time or time.monotonic()
                    self.stop_button.setEnabled(False)
                    self.metric_values['state'].setText("等待写完…")
            def failed(message):
                self.metric_busy = False
                self._append(self.event_log, "无法读取保存目录状态，已请求停止：" + message)
                self.stop_recording()
            self.run_task(lambda _: session.metrics(), done, failed)
        if self.is_recording and self.stop_time and time.monotonic() - self.stop_time > 15:
            self.force_button.show()

    def benchmark(self):
        if self._busy_guard():
            return
        directory = self.output_root.text().strip()
        if not directory:
            self._error("请先选择保存目录")
            return
        self.benchmark_busy = True
        self.speed_button.setEnabled(False)
        def done(result):
            self.benchmark_busy = False
            self.speed_button.setEnabled(True)
            self._append(self.event_log, f"128 MiB 同步写入测速：{result['mib_per_second']:.1f} MiB/s。短时结果受缓存等影响，不能替代持续录制测试。")
        def failed(message):
            self.benchmark_busy = False
            self.speed_button.setEnabled(True)
            self._error(message)
        self.run_task(lambda log: benchmark_directory(directory, log), done, failed, lambda text: self._append(self.event_log, text))

    def show_command(self):
        try:
            config = self.get_config()
            destination = Path(config.output_root).expanduser() / (config.prefix + "_自动时间戳")
            command = shlex.join(self.capabilities.command(config, destination) if self.capabilities else config.argv(destination))
            dialog = QMessageBox(self)
            dialog.setWindowTitle("录制命令预览")
            dialog.setText("实际启动时会生成唯一目录，并为 YAML 配置保存快照。")
            dialog.setDetailedText(command)
            dialog.setStandardButtons(QMessageBox.Ok)
            dialog.exec_()
        except Exception as exc:
            self._error(str(exc))

    def _refresh_profile_names(self):
        current = self.profile_combo.currentText()
        self.profile_combo.clear()
        self.profile_combo.addItems(sorted(self.store.profiles))
        self.profile_combo.setCurrentText(current)
        self.profile_list.clear()
        self.profile_list.addItems(sorted(self.store.profiles))

    def _profile_details(self, name):
        self.profile_details.setPlainText(json.dumps(self.store.profiles.get(name, {}), ensure_ascii=False, indent=2))

    def save_profile(self):
        if self._busy_guard():
            return
        try:
            config = self.get_config()
            name, accepted = QInputDialog.getText(self, "保存配置方案", "方案名称：", text=self.profile_combo.currentText())
            if accepted and name.strip():
                name = name.strip()
                if name in self.store.profiles and QMessageBox.question(self, "覆盖方案", f"覆盖已有方案“{name}”？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
                self.store.save(config, name)
                self._refresh_profile_names()
                self.profile_combo.setCurrentText(name)
                self._append(self.event_log, f"已保存配置：{name}")
        except Exception as exc:
            self._error(str(exc))

    def _load_profile(self, name):
        if self._busy_guard():
            return
        try:
            if name not in self.store.profiles:
                raise ValueError("请先保存或选择一个配置方案")
            self.apply_config(RecordConfig.from_dict(self.store.profiles[name]))
            self.nav.setCurrentRow(0)
            self._append(self.event_log, f"已应用配置方案：{name}；录制话题按当前勾选执行。")
        except Exception as exc:
            self._error(str(exc))

    def load_selected_profile(self):
        self._load_profile(self.profile_combo.currentText())

    def load_profile_from_list(self):
        item = self.profile_list.currentItem()
        if item:
            self._load_profile(item.text())

    def delete_profile(self):
        item = self.profile_list.currentItem()
        if not item or self._busy_guard():
            return
        name = item.text()
        if QMessageBox.question(self, "删除配置", f"删除配置方案“{name}”？录制数据不会删除。", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            previous = self.store.profiles.pop(name)
            try:
                atomic_json(self.store.path, {"version": 1, "last": self.store.last, "profiles": self.store.profiles})
            except OSError as exc:
                self.store.profiles[name] = previous
                self._error(str(exc))
            self._refresh_profile_names()

    def import_record_command(self):
        if self._busy_guard():
            return
        value, accepted = QInputDialog.getMultiLineText(self, "导入录制命令", "粘贴单条 ros2 bag record 命令（不会执行此文本）：")
        if not accepted or not value.strip():
            return
        try:
            config = import_command(value, cache_unit=self.capabilities.cache_unit if self.capabilities else 'bytes')
            config.workspace = self.active_workspace
            config.environment_setup = self.loaded_setup
            config.packages = list(self.active_packages)
            config.auto_detect_frequency = self.auto_frequency.isChecked()
            config.base_environment_setup = self.base_setup
            self.apply_config(config, load_env=False)
            self._append(self.event_log, "已导入命令参数；实际输出目录会增加唯一时间戳。原有消息环境保持不变。")
        except Exception as exc:
            self._error(str(exc))

    def export_profile(self):
        try:
            item = self.profile_list.currentItem()
            config = RecordConfig.from_dict(self.store.profiles[item.text()]) if item else self.get_config()
            path, _ = QFileDialog.getSaveFileName(self, "导出配置", "recording-config.json", "JSON (*.json)")
            if path:
                atomic_json(Path(path), config.to_dict())
        except Exception as exc:
            self._error(str(exc))

    def import_profile(self):
        if self._busy_guard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", "", "JSON (*.json)")
        if not path:
            return
        try:
            if Path(path).stat().st_size > MIB:
                raise ValueError("配置文件超过 1 MiB")
            config = RecordConfig.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
            self.apply_config(config)
            self.nav.setCurrentRow(0)
            self._append(self.event_log, "已导入配置，使用“保存为”可加入方案库。")
        except Exception as exc:
            self._error(str(exc))

    def choose_history(self):
        path = QFileDialog.getExistingDirectory(self, "选择录制记录所在目录", self.history_root.text())
        if path:
            self.history_root.setText(path)
            self.refresh_history()

    def refresh_history(self):
        root = self.history_root.text().strip()
        def done(sessions):
            self.sessions = sessions
            self.history_table.setRowCount(len(sessions))
            states = {'completed': '已结束 / 待核对', 'failed': '失败', 'empty': '空录制', 'starting': '未完成 / 运行中'}
            for row, session in enumerate(sessions):
                report = session.get('report') or {}
                values = [session.get('started_at', ''), Path(session['bag_path']).name,
                          states.get(session.get('state'), '未知'), str(report.get('message_count', '—')),
                          size_text(report.get('size_bytes', 0))]
                for col, value in enumerate(values):
                    self.history_table.setItem(row, col, QTableWidgetItem(value))
            if sessions:
                self.history_table.selectRow(0)
        self.run_task(lambda _: list_sessions(root), done, lambda message: self._append(self.event_log, message))

    def _selected_session(self):
        row = self.history_table.currentRow()
        return self.sessions[row] if 0 <= row < len(self.sessions) else None

    def _history_details(self):
        session = self._selected_session()
        if not session:
            self.report_details.clear()
            return
        report = session.get('report') or {}
        lines = ["保存位置：" + session['bag_path'], "命令：" + session.get('command', ''),
                 "完整性：未验证。消息数和平均频率仅用于核对。", ""]
        if not report:
            lines.append("未找到结束报告：录制可能仍在运行，或上次异常中断。")
        if report.get('error'):
            lines.append(report['error'])
        lines.append(f"退出码：{report.get('returncode', '未知')}，bag 全局时长：{report.get('duration_seconds', 0):.3f} 秒")
        lines.append("录后均频 = 话题消息数 / bag 全局时长，包含话题起停差异，不等同于发布频率。")
        for topic in report.get('topics', []):
            hz = topic.get('bag_average_hz')
            hz_text = f"{hz:.2f} Hz（估算）" if hz is not None else '—'
            lines.append(f"\n{topic['name']}\n  {topic['type']}\n  {topic['count']} 条 · {hz_text}")
        lines += ["", f"日志告警：{len(report.get('warnings', []))} 条（摘要最多保留 1000 条，完整内容见 recorder.log）"]
        lines.extend(report.get('warnings', [])[:30])
        self.report_details.setPlainText('\n'.join(lines))

    def reuse_session(self):
        if self._busy_guard():
            return
        session = self._selected_session()
        if session:
            try:
                self.apply_config(RecordConfig.from_dict(session['config']))
                self.nav.setCurrentRow(0)
            except Exception as exc:
                self._error(str(exc))

    def open_session_location(self, sidecar):
        session = self._selected_session()
        if session:
            path = session.get('session_dir') if sidecar else session['bag_path']
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _maybe_close(self):
        if self.pending_close and not self.is_recording and not self.build_runner and not self.frequency_monitor and not self.tasks and not self.analysis_widget.busy and not (self.calibration_widget and self.calibration_widget.busy):
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        busy = self.is_recording or self.build_runner or self.frequency_monitor or self.tasks or self.analysis_widget.busy or (self.calibration_widget and self.calibration_widget.busy)
        if busy:
            if not self.pending_close:
                if self.is_recording or self.build_runner or self.preflight_busy or self.env_busy or self.benchmark_busy:
                    answer = QMessageBox.question(self, "结束操作后退出", "当前仍有录制或后台操作。停止操作并等待文件保存后退出？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if answer != QMessageBox.Yes:
                        event.ignore()
                        return
                self.pending_close = True
                self.stop_recording()
                self.cancel_build()
                self.stop_all_monitors()
                self.analysis_widget.cancel()
                if self.calibration_widget:
                    self.calibration_widget.set_active(False)
                self.setWindowTitle("ROS Recorder · ROS 2 · 正在等待后台操作结束…")
            event.ignore()
            return
        try:
            self.store.save(self.get_config())
        except Exception:
            pass
        self.analysis_widget.dispose()
        if self.calibration_widget:
            self.calibration_widget.dispose()
        event.accept()


def configure_style(app):
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans CJK SC", 10))
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f4f6f8"))
    palette.setColor(QPalette.WindowText, QColor("#253142"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f7f9fb"))
    palette.setColor(QPalette.Text, QColor("#253142"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#253142"))
    palette.setColor(QPalette.Highlight, QColor("#d9eee9"))
    palette.setColor(QPalette.HighlightedText, QColor("#145b54"))
    app.setPalette(palette)
    app.setStyleSheet("""
        QMainWindow { background: #f4f6f8; }
        #appHeader { background: #ffffff; border-bottom: 1px solid #e0e5ec; }
        #brand { font-size: 20px; font-weight: 600; color: #176963; }
        #pageTitle { font-size: 23px; font-weight: 600; }
        #navigation { background: #ffffff; border: 0; border-right: 1px solid #e0e5ec; padding: 15px 8px; outline: 0; }
        #navigation::item { padding: 14px 9px; margin-bottom: 4px; border-radius: 6px; }
        #navigation::item:selected { background: #e4f2ee; color: #176963; }
        QLabel[secondary=true] { color: #657286; font-size: 12px; }
        QGroupBox { background: #ffffff; border: 1px solid #e0e5ec; border-radius: 8px; margin-top: 10px; padding: 17px 10px 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #344054; font-weight: 500; }
        QPushButton { background: #ffffff; border: 1px solid #d7dfe8; border-radius: 5px; padding: 7px 10px; }
        QPushButton:hover { background: #eef3f5; }
        QPushButton[compact=true] { padding: 4px 8px; }
        QPushButton:disabled { color: #97a1ad; background: #f1f3f5; }
        QPushButton[primary=true] { color: #ffffff; background: #176963; border-color: #176963; }
        QPushButton[primary=true]:hover { background: #205c57; }
        QPushButton[primary=true]:disabled { background: #8caeaa; border-color: #8caeaa; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding: 5px; min-height: 22px; border: 1px solid #d7dfe8; border-radius: 4px; background: #ffffff; }
        QPlainTextEdit, QTableWidget, QListWidget { border: 1px solid #e0e5ec; border-radius: 5px; background: #ffffff; }
        QHeaderView::section { background: #f3f6f8; border: 0; padding: 8px 5px; color: #657286; }
        #metricValue { font-size: 16px; font-weight: 500; }
        #metricCard { background: #ffffff; border: 1px solid #e0e5ec; border-radius: 7px; }
        QScrollArea { background: #f4f6f8; }
    """)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROS 2 中文录制工作台")
    parser.add_argument("--screenshot", help="开发用：保存窗口截图后退出")
    parser.add_argument("--no-discovery", action="store_true", help="启动时不自动发现话题")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ROS Recorder")
    configure_style(app)
    window = MainWindow(auto_discover=not args.no_discovery)
    window.show()
    if args.screenshot:
        def capture():
            Path(args.screenshot).parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(args.screenshot)
            app.quit()
        QTimer.singleShot(1800, capture)
    return app.exec_()


if __name__ == '__main__':
    raise SystemExit(main())
