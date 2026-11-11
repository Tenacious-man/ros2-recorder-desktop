"""Select live topics and compare their last-arriving header timestamps."""
from __future__ import annotations

from dataclasses import asdict
import time

from PyQt5.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDrag
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .environment import discover_topics
from .communication import CommunicationSettings, PROFILE_VARIABLE, RELIABILITY, TRANSPORTS
from .communication_dialog import CommunicationComparison, CommunicationDialog
from .config import atomic_json
from .item_delegate import TopicItemDelegate, configure_checkable_rows
from .latest_stamps import LatestStampMonitor, format_stamp, latest_difference, sample_status
from .units import KIB, MIB, format_capacity

LOCAL_REFERENCE = 'local_receipt'


class ReorderableTopicTable(QTableWidget):
    rowsReordered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(0, 5, parent)
        self.setItemDelegate(TopicItemDelegate(self))
        self.dragged_topic = None
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setToolTip('按住话题行拖动可手动排序；排序不会更换比较基准。')

    def startDrag(self, supported_actions):
        row = self.currentRow()
        item = self.item(row, 0)
        if item is None:
            return
        self.dragged_topic = item.data(Qt.UserRole)
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(self.selectedIndexes()))
        rect = self.visualRect(self.model().index(row, 0))
        drag.setPixmap(self.viewport().grab(rect))
        drag.setHotSpot(QPoint(16, max(0, rect.height() // 2)))
        try:
            # Own this drag rather than calling QAbstractItemView.startDrag:
            # Qt 5 would clear the source cells after our custom Move drop.
            drag.exec_(Qt.MoveAction, Qt.MoveAction)
        finally:
            self.dragged_topic = None
            self.setState(QAbstractItemView.NoState)
            self.viewport().update()
            drag.deleteLater()

    def dropEvent(self, event):
        if event.source() is not self or self.dragged_topic is None:
            event.ignore()
            return
        source = next((row for row in range(self.rowCount())
                       if self.item(row, 0).data(Qt.UserRole) == self.dragged_topic), -1)
        if source < 0:
            event.ignore()
            return
        index = self.indexAt(event.pos())
        slot = index.row()
        if slot < 0:
            slot = self.rowCount()
        elif event.pos().y() >= self.visualRect(index).center().y():
            slot += 1
        destination = slot - 1 if source < slot else slot
        self.move_row(source, destination)
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def move_row(self, source, destination):
        """Move complete row objects to a final zero-based row position."""
        if not 0 <= source < self.rowCount() or not 0 <= destination < self.rowCount() or source == destination:
            return
        blocked = self.blockSignals(True)
        try:
            height = self.rowHeight(source)
            items = [self.takeItem(source, column) for column in range(self.columnCount())]
            self.removeRow(source)
            self.insertRow(destination)
            for column, item in enumerate(items):
                if item is not None:
                    self.setItem(destination, column, item)
            self.setRowHeight(destination, height)
            self.selectRow(destination)
        finally:
            self.blockSignals(blocked)
        self.rowsReordered.emit()


class TopicSelectionDialog(QDialog):
    """Edit a draft selection; only acceptance changes live subscriptions."""
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.catalog = {}
        self.draft = set(page.selected)
        self.rows = {}
        self.setWindowTitle('选择监听话题')
        self.resize(820, 580)
        self.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('勾选需要对比的话题，确认后主界面只显示这些话题。'))
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索话题名称或消息类型')
        self.search.textChanged.connect(self.filter_topics)
        toolbar.addWidget(self.search, 1)
        self.refresh_button = QPushButton('刷新话题')
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel('消息类型'))
        self.type_filter = QComboBox()
        self.type_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.type_filter.setMinimumContentsLength(24)
        self.type_filter.setMaxVisibleItems(12)
        self.type_filter.setToolTip('按完整消息类型筛选，可与上方关键词搜索同时使用。')
        self.type_filter.currentIndexChanged.connect(self.filter_topics)
        type_row.addWidget(self.type_filter, 1)
        self.select_all_button = QPushButton('全选当前结果')
        self.select_all_button.setToolTip('仅勾选当前筛选结果；筛选外的话题保持原样，类型未知或冲突的话题会跳过。')
        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        type_row.addWidget(self.select_all_button)
        self.clear_all_button = QPushButton('取消当前结果')
        self.clear_all_button.setToolTip('仅取消当前筛选结果的勾选，筛选外的话题保持原样。')
        self.clear_all_button.clicked.connect(lambda: self.set_all_checked(False))
        type_row.addWidget(self.clear_all_button)
        layout.addLayout(type_row)
        self.table = QTableWidget(0, 2)
        configure_checkable_rows(self.table)
        self.table.setHorizontalHeaderLabels(['话题（勾选）', '消息类型'])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.itemChanged.connect(self.change_draft)
        layout.addWidget(self.table, 1)
        self.count = QLabel()
        layout.addWidget(self.count)
        self.notice = QLabel('筛选不会清除已勾选的话题；取消后保留原来的选择。')
        self.notice.setWordWrap(True)
        self.notice.setProperty('secondary', True)
        layout.addWidget(self.notice)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText('确认选择')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.set_catalog(page.catalog)
        self.update_controls()

    def set_catalog(self, catalog):
        updated = {name: dict(topic) for name, topic in catalog.items()}
        # Keep draft choices when a publisher disappears during a refresh.
        for name in self.draft:
            if name not in updated and name in self.catalog:
                updated[name] = dict(self.catalog[name], publishers=0)
        self.catalog = updated
        selected_type = self.type_filter.currentData()
        kinds = {kind for topic in updated.values() for kind in topic['types']}
        available_kinds = set(kinds)
        if selected_type:
            # A refresh must not silently broaden the active filter to all
            # topics when its type temporarily disappears from discovery.
            kinds.add(selected_type)
        common = {'sensor_msgs/msg/PointCloud2': '点云', 'sensor_msgs/msg/Image': '图像',
                  'sensor_msgs/msg/CompressedImage': '压缩图像', 'sensor_msgs/msg/CameraInfo': '相机信息',
                  'sensor_msgs/msg/Imu': 'IMU'}
        priority = {kind: index for index, kind in enumerate(common)}
        self.type_filter.blockSignals(True)
        self.type_filter.clear()
        self.type_filter.addItem('全部消息类型', None)
        for kind in sorted(kinds, key=lambda k: (priority.get(k, len(common)), k)):
            text = f'{common[kind]} · {kind}' if kind in common else kind
            if kind not in available_kinds:
                text += '（当前未发现）'
            self.type_filter.addItem(text, kind)
            self.type_filter.setItemData(self.type_filter.count() - 1, kind, Qt.ToolTipRole)
        self.type_filter.setCurrentIndex(max(0, self.type_filter.findData(selected_type)))
        self.type_filter.blockSignals(False)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.rows = {}
        for row, name in enumerate(sorted(updated)):
            self.rows[name] = row
            self.table.insertRow(row)
            topic = updated[name]
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in self.draft else Qt.Unchecked)
            item.setToolTip(name + '\n' + topic.get('reason', ''))
            self.table.setItem(row, 0, item)
            kinds = QTableWidgetItem(', '.join(topic['types']))
            kinds.setToolTip(', '.join(topic['types']))
            self.table.setItem(row, 1, kinds)
            self.table.setRowHeight(row, 37)
        self.table.blockSignals(False)
        self.filter_topics()
        self.update_controls()

    def set_all_checked(self, checked):
        visible = {name for name, row in self.rows.items() if not self.table.isRowHidden(row)}
        eligible = {name for name in visible if len(self.catalog[name]['types']) == 1}
        if checked:
            self.draft.update(eligible)
        else:
            self.draft.difference_update(visible)
        self.table.blockSignals(True)
        for name in visible:
            row = self.rows[name]
            self.table.item(row, 0).setCheckState(Qt.Checked if name in self.draft else Qt.Unchecked)
        self.table.blockSignals(False)
        self.update_count()
        skipped = len(visible - eligible) if checked else 0
        self.notice.setText(('已勾选当前筛选结果' if checked else '已取消当前筛选结果的勾选') +
                            '，筛选外话题保持原样，确认选择后生效。' +
                            (f'已跳过 {skipped} 个类型未知或冲突的话题。' if skipped else ''))

    def change_draft(self, item):
        if item.column() != 0:
            return
        name = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            if len(self.catalog[name]['types']) != 1:
                self.table.blockSignals(True)
                item.setCheckState(Qt.Unchecked)
                self.table.blockSignals(False)
                self.notice.setText('该话题的消息类型未知或存在冲突，请先处理类型后再选择。')
                return
            self.draft.add(name)
        else:
            self.draft.discard(name)
        self.update_count()

    def filter_topics(self):
        term = self.search.text().strip().casefold()
        kind = self.type_filter.currentData()
        for name, row in self.rows.items():
            text = name + ' ' + ' '.join(self.catalog[name]['types'])
            matches_type = kind is None or kind in self.catalog[name]['types']
            self.table.setRowHidden(row, not (matches_type and term in text.casefold()))
        self.update_count()

    def update_count(self):
        hidden = sum(self.table.isRowHidden(self.rows[name]) for name in self.draft if name in self.rows)
        visible = sum(not self.table.isRowHidden(row) for row in self.rows.values())
        self.select_all_button.setEnabled(bool(visible))
        self.clear_all_button.setEnabled(bool(visible))
        self.count.setText(f'当前显示 {visible} / {len(self.rows)} 个话题 · 已勾选 {len(self.draft)} 个话题' +
                           (f'（其中 {hidden} 个在筛选结果外）' if hidden else ''))

    def update_controls(self):
        self.refresh_button.setEnabled(not self.page.discovering and not self.page.host.pending_close)
        self.refresh_button.setText('正在发现…' if self.page.discovering else '刷新话题')

    def refresh(self):
        self.page.refresh_topics()
        if not self.page.discovering:
            self.notice.setText(self.page.status.text())


class LiveStampPage(QWidget):
    busyChanged = pyqtSignal()
    _snapshot = pyqtSignal(object, object)
    _exited = pyqtSignal(object, int)

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.catalog = {}
        self.selected = {}
        self.samples = {}
        self.rows = {}
        self.communication_path = host.store.path.with_name('communication.json')
        self.communication = CommunicationSettings.load(self.communication_path)
        self.run_settings = {}
        self.baseline = None
        self.monitor = None
        self.preparing = False
        self.cancelled_start = False
        self.discovering = False
        self.paused_frequency = False
        self.offline_page = None
        self.offline_dialog = None
        self.topic_dialog = None
        self._snapshot.connect(self.accept_snapshot)
        self._exited.connect(self.monitor_exited)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.force_stop)
        self.limit_timer = QTimer(self)
        self.limit_timer.setSingleShot(True)
        self.limit_timer.timeout.connect(self.cancel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel('实时消息时间戳')
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        description = QLabel('选择需要对比的话题和基准，查看最新消息的 header.stamp 和时间差。')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.local_clock = QLabel()
        self.local_clock.setToolTip('本机当前系统时间，单位为 Unix 秒。各消息的差值使用接收时刻。')
        layout.addWidget(self.local_clock)
        controls = QHBoxLayout()
        self.select_button = QPushButton('选择话题…')
        self.select_button.clicked.connect(self.choose_topics)
        controls.addWidget(self.select_button)
        self.start_button = QPushButton('开始监听')
        self.start_button.setProperty('primary', True)
        self.start_button.clicked.connect(self.start)
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton('停止监听')
        self.stop_button.clicked.connect(self.cancel)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        controls.addWidget(QLabel('比较基准'))
        self.reference = QComboBox()
        self.reference.setMinimumWidth(220)
        self.reference.currentIndexChanged.connect(self.render_samples)
        controls.addWidget(self.reference, 1)
        layout.addLayout(controls)
        communication_row = QHBoxLayout()
        self.communication_button = QPushButton('对照监听设置…')
        self.communication_button.clicked.connect(self.choose_communication)
        communication_row.addWidget(self.communication_button)
        self.baseline_button = QPushButton('将本轮设为基线')
        self.baseline_button.clicked.connect(self.save_baseline)
        communication_row.addWidget(self.baseline_button)
        self.comparison_button = QPushButton('查看通信对照…')
        self.comparison_button.clicked.connect(self.show_comparison)
        communication_row.addWidget(self.comparison_button)
        communication_row.addStretch()
        self.communication_label = QLabel()
        self.communication_label.setWordWrap(True)
        communication_row.addWidget(self.communication_label, 1)
        layout.addLayout(communication_row)
        self.table = ReorderableTopicTable(self)
        self.table.rowsReordered.connect(self.apply_table_order)
        self.table.setHorizontalHeaderLabels(
            ['已选话题', '最新 header.stamp（秒）', '本机接收时间（秒）', '相对基准（ms）', '状态'])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.result_stack = QStackedWidget()
        self.empty_hint = QLabel('尚未选择话题\n点击“选择话题”添加需要对比的传感器话题。')
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setProperty('secondary', True)
        self.result_stack.addWidget(self.empty_hint)
        self.result_stack.addWidget(self.table)
        layout.addWidget(self.result_stack, 1)
        self.formula = QLabel()
        self.formula.setWordWrap(True)
        layout.addWidget(self.formula)
        note = QLabel('本机差值也包含消息传输和接收处理耗时。话题间比较使用各自最新的消息，发布频率不同会影响差值。')
        note.setWordWrap(True)
        note.setProperty('secondary', True)
        layout.addWidget(note)
        self.status = QLabel('点击“选择话题”，勾选确认后开始监听。')
        self.status.setWordWrap(True)
        self.status.setProperty('secondary', True)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.render_samples)
        self.timer.start()
        self.update_reference_options()
        self.render_samples()
        self.update_controls()

    @property
    def busy(self):
        return self.preparing or self.monitor is not None or bool(self.offline_page and self.offline_page.busy)

    def update_controls(self):
        self.start_button.setEnabled(bool(self.selected) and not self.busy and not self.host.pending_close)
        self.stop_button.setEnabled((self.preparing or self.monitor is not None) and
                                    not (self.monitor and self.monitor.stopping))
        self.select_button.setEnabled(not self.host.pending_close)
        self.reference.setEnabled(bool(self.selected))
        idle = not self.busy and not self.host.pending_close
        measured = any('seconds' in sample for sample in self.samples.values())
        self.communication_button.setEnabled(idle)
        self.baseline_button.setEnabled(idle and measured)
        self.comparison_button.setEnabled(not self.host.pending_close and (measured or self.baseline is not None))
        settings = self.communication
        duration = f' · {settings.duration_s} 秒/轮' if settings.duration_s else ''
        self.communication_label.setText(f'{RELIABILITY[settings.reliability]} · {TRANSPORTS[settings.transport]}{duration}')
        if self.topic_dialog:
            self.topic_dialog.update_controls()

    def choose_communication(self):
        dialog = CommunicationDialog(self, self.communication)
        if dialog.exec_() != QDialog.Accepted:
            return
        settings = dialog.settings()
        try:
            atomic_json(self.communication_path, asdict(settings))
        except OSError as exc:
            self.status.setText(f'保存通信设置失败：{exc}')
            return
        self.communication = settings
        self.status.setText('已保存通信设置，下一轮监听时生效。')
        self.update_controls()

    def current_run(self):
        samples = {name: dict(sample, kind=self.selected[name]) for name, sample in self.samples.items()
                   if name in self.selected and 'seconds' in sample}
        return {'settings': dict(self.run_settings), 'samples': samples} if samples else None

    def save_baseline(self):
        run = self.current_run()
        if run:
            self.baseline = run
            self.status.setText('已设为对照基线。修改通信设置后再监听一轮，即可查看对照。')
            self.update_controls()

    def show_comparison(self):
        CommunicationComparison(self, self.baseline, self.current_run()).exec_()

    def choose_topics(self):
        if self.topic_dialog and self.topic_dialog.isVisible():
            self.topic_dialog.raise_()
            return
        if self.topic_dialog:
            self.topic_dialog.deleteLater()
        dialog = TopicSelectionDialog(self)
        self.topic_dialog = dialog
        dialog.accepted.connect(lambda: self.apply_selection(dialog.draft, dialog.catalog))
        dialog.open()
        if not self.catalog:
            dialog.refresh()

    def set_discovered_topics(self, topics):
        catalog = {t['name']: dict(t) for t in topics}
        # A selected subscription stays available when a publisher temporarily
        # disappears. It can receive again without recreating its selection.
        for name in self.selected:
            if name not in catalog and name in self.catalog:
                catalog[name] = dict(self.catalog[name], publishers=0)
        if catalog == self.catalog:
            return
        self.catalog = catalog
        if self.topic_dialog and self.topic_dialog.isVisible():
            self.topic_dialog.set_catalog(catalog)
        self.apply_selection(self.selected)

    def apply_selection(self, names, extra_catalog=None):
        selected = {}
        wanted = set(names)
        order = [name for name in self.selected if name in wanted]
        order.extend(sorted(wanted - self.selected.keys()))
        for name in order:
            topic = self.catalog.get(name) or (extra_catalog or {}).get(name)
            if not topic:
                continue
            self.catalog.setdefault(name, dict(topic, publishers=0))
            kinds = topic['types']
            if len(kinds) == 1:
                selected[name] = kinds[0]
            elif name in self.selected:
                selected[name] = self.selected[name]
        for name in list(self.samples):
            if self.selected.get(name) != selected.get(name) or self.catalog[name]['types'] != [selected.get(name)]:
                self.samples.pop(name, None)
        self.selected = selected
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.rows = {}
        for row, name in enumerate(selected):
            topic = self.catalog[name]
            self.rows[name] = row
            self.table.insertRow(row)
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
            item.setToolTip(name + '\n' + ', '.join(topic['types']) + '\n' + topic.get('reason', ''))
            self.table.setItem(row, 0, item)
            for column in range(1, self.table.columnCount()):
                self.table.setItem(row, column, QTableWidgetItem('—'))
            self.table.setRowHeight(row, 43)
        self.table.blockSignals(False)
        self.result_stack.setCurrentWidget(self.table if selected else self.empty_hint)
        self.update_reference_options()
        if self.monitor and not self.monitor.stopping:
            self.monitor.update(self.subscription_topics())
            self.status.setText(f'正在监听 {len(self.subscription_topics())} 个话题。')
        elif selected and not self.preparing:
            self.status.setText(f'已选择 {len(selected)} 个话题，点击“开始监听”。' if not self.samples else
                                f'已选择 {len(selected)} 个话题，保留最后收到的消息。')
        elif not selected and not self.preparing:
            self.status.setText('点击“选择话题”，勾选确认后开始监听。')
        self.render_samples()
        self.update_controls()

    def apply_table_order(self):
        order = [self.table.item(row, 0).data(Qt.UserRole) for row in range(self.table.rowCount())]
        self.rows = {name: row for row, name in enumerate(order)}
        self.selected = {name: self.selected[name] for name in order}
        self.update_reference_options()
        self.render_samples()

    def update_reference_options(self):
        old_reference = self.reference.currentData()
        self.reference.blockSignals(True)
        self.reference.clear()
        self.reference.addItem('本机系统时间（接收时）', LOCAL_REFERENCE)
        for name in self.selected:
            self.reference.addItem(name, name)
        index = self.reference.findData(old_reference)
        self.reference.setCurrentIndex(max(0, index))
        self.reference.blockSignals(False)

    def subscription_topics(self):
        return {name: kind for name, kind in self.selected.items() if self.catalog[name]['types'] == [kind]}

    def refresh_topics(self):
        if self.discovering or self.host.pending_close:
            return
        if not self.host.active_env or self.host.env_busy:
            self.status.setText('请先在“消息环境”加载 ROS 2 环境。')
            return
        if self.host.is_recording or self.host.preflight_busy or self.host.build_runner:
            self.status.setText('请等待当前录制或环境操作结束后再刷新。')
            return
        env = dict(self.host.active_env)
        self.discovering = True
        self.update_controls()
        def finish(topics=None, error=''):
            self.discovering = False
            if env == self.host.active_env and not self.host.pending_close:
                if error:
                    self.status.setText('发现话题失败：' + error)
                else:
                    self.set_discovered_topics(topics)
                    self.status.setText(f"已发现 {len(topics)} 个话题 · Domain {env.get('ROS_DOMAIN_ID', '0')}")
                if self.topic_dialog and self.topic_dialog.isVisible():
                    self.topic_dialog.notice.setText(self.status.text())
            self.update_controls()
        self.host.run_task(lambda log: discover_topics(env), lambda topics: finish(topics), lambda error: finish(error=error))

    def start(self):
        reason = self.host._busy_message()
        if reason:
            self.status.setText(reason)
            return
        if not self.selected or not self.host.active_env:
            self.status.setText('请加载 ROS 环境并勾选需要监听的话题。')
            return
        self.preparing = True
        self.cancelled_start = False
        self.samples.clear()
        env = dict(self.host.active_env)
        settings = self.communication
        transport = TRANSPORTS[settings.transport]
        if settings.transport == 'shm':
            transport += f' · SHM {format_capacity(settings.shm_mib * MIB, "MB")}'
        if settings.transport != 'environment' and settings.udp_kib:
            transport += f' · UDP 请求 {format_capacity(settings.udp_kib * KIB, "KB")}'
        if settings.transport == 'environment':
            profile = env.get('FASTDDS_DEFAULT_PROFILES_FILE') or env.get(PROFILE_VARIABLE)
            if profile:
                transport += f' · {profile}'
        self.run_settings = {'reliability': RELIABILITY[settings.reliability], 'depth': settings.depth,
                             'transport': transport, 'domain': env.get('ROS_DOMAIN_ID', '0'), 'note': settings.note}
        previous = self.host._pause_frequency_for_recording()
        self.paused_frequency = True
        self.status.setText('正在释放频率检测订阅，准备监听…')
        self.update_controls()
        self.busyChanged.emit()
        def prepare(log):
            if previous and not previous.wait(8):
                previous.stop(force=True)
                if not previous.wait(3):
                    raise RuntimeError('原频率检测进程尚未退出。')
        def prepared(_=None, error=''):
            self.preparing = False
            if self.cancelled_start or self.host.pending_close or error or not self.selected:
                self.resume_frequency()
                self.status.setText('监听未启动：' + error if error else '已取消监听。')
            else:
                try:
                    monitor = LatestStampMonitor(
                        env, self.subscription_topics(), settings=settings,
                        on_snapshot=lambda value: self._snapshot.emit(monitor, value),
                        on_done=lambda code: self._exited.emit(monitor, code))
                    self.monitor = monitor
                    monitor.start()
                    if settings.duration_s:
                        self.limit_timer.start(settings.duration_s * 1000)
                    self.status.setText(f"正在监听 {len(self.selected)} 个话题 · Domain {env.get('ROS_DOMAIN_ID', '0')}")
                except Exception as exc:
                    self.monitor = None
                    self.resume_frequency()
                    self.status.setText('启动监听失败：' + str(exc))
            self.update_controls()
            self.busyChanged.emit()
        self.host.run_task(prepare, prepared, lambda error: prepared(error=error))

    def accept_snapshot(self, monitor, value):
        if monitor is not self.monitor or monitor.stopping:
            return
        if 'error' in value:
            self.status.setText('监听错误：' + value['error'])
            return
        if value.get('generation') != monitor.generation:
            return
        for name, sample in value.get('samples', {}).items():
            if name in self.selected and sample.get('token') == monitor.tokens.get(name):
                self.samples[name] = sample
        self.render_samples()
        if not self.comparison_button.isEnabled() and any('seconds' in sample for sample in self.samples.values()):
            self.comparison_button.setEnabled(not self.host.pending_close)

    def render_samples(self):
        reference_name = self.reference.currentData()
        local_reference = reference_name == LOCAL_REFERENCE
        reference = self.samples.get(reference_name)
        basis = '该条消息的本机接收时间' if local_reference else '基准话题最新 header.stamp'
        self.formula.setText(f'差值 = 本话题最新 header.stamp − {basis}。正值表示消息时间戳更大。')
        self.local_clock.setText(f'本机当前时间（Unix 秒）  {format_stamp(time.time_ns())}')
        running = self.monitor is not None and not self.monitor.stopping
        now = time.monotonic_ns()
        self.table.blockSignals(True)
        for name, row in self.rows.items():
            sample = self.samples.get(name)
            stamp = format_stamp(sample.get('stamp_ns')) if sample else '—'
            receipt_ns = sample.get('received_wall_ns') if sample else None
            delta = None
            if not local_reference:
                delta = latest_difference(sample, reference)
            elif receipt_ns is not None and sample['status'] == 'ok':
                delta = (sample['stamp_ns'] - receipt_ns) / 1_000_000
            difference = '—' if delta is None else ('0.000' if name == reference_name else f'{delta:+.3f}')
            state = sample_status(sample, running=running or self.preparing, now_ns=now)
            if len(self.catalog[name]['types']) != 1:
                state = '同名话题存在多个类型'
            elif not running and not self.preparing and not sample:
                state = '等待开始监听'
            for column, value in ((1, stamp), (2, format_stamp(receipt_ns)), (3, difference), (4, state)):
                self.table.item(row, column).setText(value)
                self.table.item(row, column).setForeground(QColor('#253142'))
            if name == reference_name:
                self.table.item(row, 3).setForeground(QColor('#176963'))
            if sample and (sample.get('status') != 'ok' or '未更新' in state):
                self.table.item(row, 4).setForeground(QColor('#a66a2b'))
            if sample:
                detail = f"header 整数纳秒：{sample.get('stamp_ns')}\n已接收：{sample['received_count']} 条"
                if sample.get('received_ns') is not None:
                    detail += f"\n距本机最近接收：{max(0, now - sample['received_ns']) / 1e9:.3f} 秒"
                self.table.item(row, 1).setToolTip(detail)
                self.table.item(row, 4).setToolTip(detail + '\n' + sample.get('error', ''))
        self.table.blockSignals(False)

    def cancel(self):
        self.limit_timer.stop()
        if self.offline_page:
            self.offline_page.cancel()
        self.cancelled_start = True
        if self.monitor and not self.monitor.stopping:
            self.monitor.stop()
            self.kill_timer.start(3000)
            self.status.setText('正在停止监听…')
        self.update_controls()

    def force_stop(self):
        if self.monitor:
            self.monitor.stop(force=True)

    def monitor_exited(self, monitor, code):
        if monitor is not self.monitor:
            return
        self.kill_timer.stop()
        self.limit_timer.stop()
        stopped = monitor.stopping
        self.monitor = None
        self.resume_frequency()
        self.status.setText('已停止监听，保留最后收到的消息。' if stopped else f'监听进程已退出（{code}），可重新开始。')
        self.render_samples()
        self.update_controls()
        self.busyChanged.emit()

    def resume_frequency(self):
        if self.paused_frequency:
            self.paused_frequency = False
            self.host._resume_frequency_after_recording()

    def open_bag(self, path):
        """Keep existing offline work accessible only from recording history."""
        if self.host._busy_guard():
            return
        if self.offline_page is None:
            from .analysis_page import AnalysisPage
            self.offline_dialog = QDialog(self)
            self.offline_dialog.setWindowTitle('离线 bag 统计')
            self.offline_dialog.resize(1280, 850)
            layout = QVBoxLayout(self.offline_dialog)
            self.offline_page = AnalysisPage(self.host)
            layout.addWidget(self.offline_page)
            self.offline_page.busyChanged.connect(self.busyChanged.emit)
            self.offline_dialog.finished.connect(lambda _: self.offline_page.cancel())
        self.offline_dialog.show()
        self.offline_dialog.raise_()
        self.offline_page.open_bag(path)

    def dispose(self):
        self.timer.stop()
        self.limit_timer.stop()
        self.kill_timer.stop()
        if self.topic_dialog:
            self.topic_dialog.reject()
        if self.offline_page:
            self.offline_page.dispose()
            self.offline_dialog.hide()
