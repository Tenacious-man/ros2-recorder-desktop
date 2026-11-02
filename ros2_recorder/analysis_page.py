"""Focused header-time analysis: topic picker and one large metric plot."""
from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import threading

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QProgressBar, QPushButton, QSizePolicy, QSplitter, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .analysis import query_timestamps
from .analysis_chart import COLORS, METRICS, MetricChart
from .analysis_job import AnalysisJob


class StatusLabel(QLabel):
    def __init__(self, text=''):
        super().__init__()
        self.value = text
        self.setProperty('secondary', True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        self.value = text
        self.setToolTip(text)
        super().setText(self.fontMetrics().elidedText(text.replace('\n', ' '), Qt.ElideRight, max(0, self.width())))

    def resizeEvent(self, event):
        tooltip = self.toolTip()
        self.setText(self.value)
        self.setToolTip(tooltip)
        super().resizeEvent(event)


def number(value, precision=2):
    return '—' if value is None else f'{value:,.{precision}f}'


class AnalysisPage(QWidget):
    busyChanged = pyqtSignal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.job = None
        self.result_job = None
        self.dataset = None
        self.report = None
        self.results = {}
        self.topic_items = {}
        self.exporting = False
        self.export_cancel = threading.Event()
        self.query_running = False
        self.query_generation = 0
        self.query_cancel = threading.Event()
        self.query_timer = QTimer(self)
        self.query_timer.setSingleShot(True)
        self.query_timer.timeout.connect(self.load_timestamps)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        toolbar = QHBoxLayout()
        title = QLabel('数据分析')
        title.setObjectName('pageTitle')
        toolbar.addWidget(title)
        self.file_name = StatusLabel('打开一个 bag 开始分析')
        toolbar.addWidget(self.file_name, 1)
        self.open_button = QToolButton()
        self.open_button.setText('打开 bag…')
        self.open_button.setPopupMode(QToolButton.MenuButtonPopup)
        menu = QMenu(self.open_button)
        menu.addAction('打开 bag 目录…', self.choose_directory)
        menu.addAction('打开 .db3 / .mcap 文件…', self.choose_file)
        self.open_button.setMenu(menu)
        self.open_button.clicked.connect(self.choose_directory)
        toolbar.addWidget(self.open_button)
        self.scan_button = QPushButton('读取所选')
        self.scan_button.clicked.connect(self.scan)
        toolbar.addWidget(self.scan_button)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.cancel)
        toolbar.addWidget(self.cancel_button)
        self.export_button = QPushButton('导出…')
        self.export_button.clicked.connect(self.choose_export)
        toolbar.addWidget(self.export_button)
        outer.addLayout(toolbar)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        sidebar = QWidget()
        sidebar.setMinimumWidth(185)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(7)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索话题')
        self.search.textChanged.connect(self.filter_topics)
        sidebar_layout.addWidget(self.search)
        hint = QLabel('勾选话题，再点“读取所选”')
        hint.setProperty('secondary', True)
        sidebar_layout.addWidget(hint)
        self.topics = QTreeWidget()
        self.topics.setHeaderHidden(True)
        self.topics.setRootIsDecorated(False)
        self.topics.setSelectionMode(QAbstractItemView.SingleSelection)
        self.topics.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.topics.setStyleSheet('QTreeWidget::item { padding: 9px 3px; }')
        self.topics.itemChanged.connect(self.show_topics)
        sidebar_layout.addWidget(self.topics, 1)
        self.topic_count = StatusLabel('尚未打开数据')
        sidebar_layout.addWidget(self.topic_count)
        self.splitter.addWidget(sidebar)
        workspace = QWidget()
        workspace.setMinimumWidth(420)
        work = QVBoxLayout(workspace)
        work.setContentsMargins(3, 0, 0, 0)
        work.setSpacing(8)
        metrics = QHBoxLayout()
        metrics.setSpacing(5)
        self.metric_group = QButtonGroup(self)
        self.metric_buttons = {}
        for key, definition in METRICS.items():
            button = QPushButton(definition[0])
            button.setCheckable(True)
            button.setToolTip(definition[3])
            button.setStyleSheet('QPushButton:checked { background: #176963; color: white; border-color: #176963; }')
            self.metric_group.addButton(button)
            self.metric_buttons[key] = button
            button.clicked.connect(lambda checked, metric=key: self.set_metric(metric))
            metrics.addWidget(button)
        self.metric_buttons['frequency'].setChecked(True)
        work.addLayout(metrics)
        self.explanation = QLabel(METRICS['frequency'][3])
        self.explanation.setProperty('secondary', True)
        self.explanation.setWordWrap(True)
        work.addWidget(self.explanation)
        self.metric_summary = StatusLabel('分析时间来源：header.stamp')
        self.metric_summary.setStyleSheet('font-size: 16px; color: #253142; padding: 4px 0;')
        work.addWidget(self.metric_summary)
        self.chart = MetricChart()
        self.chart.rangeChanged.connect(self.schedule_timestamps)
        self.chart.cursorChanged.connect(self.show_cursor)
        work.addWidget(self.chart, 1)
        self.cursor_text = StatusLabel('滚轮缩放 · 拖动平移 · 双击恢复 · 单击固定游标')
        work.addWidget(self.cursor_text)
        self.splitter.addWidget(workspace)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([255, 850])
        outer.addWidget(self.splitter, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        outer.addWidget(self.progress)
        self.status = StatusLabel('分析使用 header.stamp；回放将使用 bag 记录时间。')
        outer.addWidget(self.status)
        self.update_controls()

    @property
    def busy(self):
        return self.job is not None or self.exporting

    def update_controls(self):
        self.open_button.setEnabled(not self.busy)
        self.scan_button.setEnabled(not self.busy and self.dataset is not None and self.dataset['scan_supported'] and bool(self.checked_names()))
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(self.busy)
        self.progress.setVisible(self.busy)
        self.export_button.setEnabled(not self.busy and self.report is not None)

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, '选择 bag 目录', (self.dataset or {}).get('path', ''))
        if path:
            self.open_bag(path)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 bag 文件', '', 'ROS 2 bag (*.db3 *.mcap)')
        if path:
            self.open_bag(path)

    def _start(self, action, request):
        if self.busy or self.host._busy_guard():
            return
        self.cancel_queries()
        try:
            job = AnalysisJob(action, request, dict(self.host.active_env or os.environ), self)
        except Exception as exc:
            self.status.setText(f'无法读取数据：{exc}')
            return
        self.job = job
        self.progress.setRange(0, 0)
        self.status.setText('正在打开 bag…' if action == 'inspect' else '正在读取 header.stamp…')
        job.progress.connect(self.on_progress)
        job.finished.connect(lambda: self._finished(job))
        self.update_controls()
        self.busyChanged.emit()
        job.start()

    def open_bag(self, path):
        if str(path).strip():
            self._start('inspect', {'path': str(path)})

    def scan(self):
        if self.dataset and self.dataset['scan_supported']:
            names = self.checked_names()
            if not names:
                self.status.setText('请先勾选需要读取的话题。')
                return
            self._start('scan', {'path': self.dataset['path'], 'fingerprint': self.dataset['fingerprint'],
                                 'time_source': 'header_stamp', 'topics': {name: {} for name in names}})

    def cancel(self):
        self.cancel_queries()
        if self.job:
            self.job.cancel()
            self.status.setText('正在取消读取…')
        elif self.exporting:
            self.export_cancel.set()
            self.status.setText('正在取消导出…')
        self.cancel_button.setEnabled(False)

    def on_progress(self, data):
        if not self.job or self.job.cancelled:
            return
        if data['total']:
            self.progress.setRange(0, 1000)
            self.progress.setValue(min(999, int(data['processed'] / data['total'] * 1000)))
        self.status.setText(f"正在读取 header.stamp · {data['processed']:,} / {data['total']:,} 条消息")

    def _finished(self, job):
        self.job = None
        if job.cancelled:
            self.status.setText('已取消读取，已有结果保留。')
        elif job.error:
            self.status.setText('操作失败：' + job.error)
        elif job.action == 'inspect':
            self.clear_report()
            self.dataset = job.result
            self.file_name.setText(Path(self.dataset['path']).name)
            self.file_name.setToolTip(self.dataset['path'])
            self.render_dataset()
            self.status.setText('；'.join(self.dataset['warnings']) or '已打开话题列表，请勾选相机和毫米波话题后点击“读取所选”。')
        else:
            self.clear_report()
            self.report = job.result
            self.result_job = job
            self.results = {t['name']: t for t in self.report['topics']}
            self.render_report()
        if job is not self.result_job:
            job.cleanup()
        self.update_controls()
        self.busyChanged.emit()

    def clear_report(self):
        self.cancel_queries()
        if self.result_job:
            self.result_job.cleanup()
        self.result_job = None
        self.report = None
        self.results = {}
        self.chart.set_topics([], 0)
        self.metric_summary.setText('分析时间来源：header.stamp')

    def render_dataset(self):
        self.topics.blockSignals(True)
        self.topics.clear()
        self.topic_items = {}
        for i, topic in enumerate(sorted(self.dataset['topics'], key=lambda t: t['name'])):
            item = QTreeWidgetItem([topic['name']])
            item.setData(0, Qt.UserRole, topic['name'])
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if i == 0 else Qt.Unchecked)
            item.setToolTip(0, f"{topic['name']}\n{topic['type']}\n消息数（元数据）：{topic['count']:,}")
            self.topics.addTopLevelItem(item)
            self.topic_items[topic['name']] = item
        self.topics.blockSignals(False)
        self.topic_count.setText(f"{len(self.topic_items)} 个话题")
        self.filter_topics()

    def render_report(self):
        self.topics.blockSignals(True)
        checked = self.checked_names()
        first_valid = next((name for name in self.topic_items if self.results.get(name, {}).get('valid_timestamp_count', 0)), None)
        if first_valid and not any(self.results.get(n, {}).get('valid_timestamp_count', 0) for n in checked):
            self.topics.blockSignals(True)
            for name, item in self.topic_items.items():
                item.setCheckState(0, Qt.Checked if name == first_valid else Qt.Unchecked)
            self.topics.blockSignals(False)
        for name, item in self.topic_items.items():
            result = self.results.get(name)
            if result:
                problem = result['analysis_error']
                suffix = ''
                if problem:
                    suffix = ' · 无 header' if '不含 header' in problem else ' · 类型未加载'
                elif result['count'] == 0:
                    suffix = ' · 无消息'
                elif result['valid_timestamp_count'] == 0:
                    suffix = ' · header 无效'
                item.setText(0, name + suffix)
                item.setToolTip(0, f"{name}\n{result['type']}\n消息数：{result['count']:,}\n"
                                f"有效 header 时间：{result['valid_timestamp_count']:,}\n" + problem + '\n' + '\n'.join(result['warnings']))
                if problem:
                    item.setForeground(0, QColor('#8d98a4'))
        self.topics.blockSignals(False)
        self.show_topics()

    def checked_names(self):
        return [name for name, item in self.topic_items.items() if item.checkState(0) == Qt.Checked]

    def filter_topics(self):
        term = self.search.text().strip().lower()
        for name, item in self.topic_items.items():
            item.setHidden(term not in name.lower())

    def show_topics(self):
        names = self.checked_names()
        selected = [self.results[n] for n in names if n in self.results]
        self.chart.set_topics(selected, (self.report or {}).get('analysis_origin_ns'))
        self.topics.blockSignals(True)
        for name, item in self.topic_items.items():
            item.setForeground(0, QColor('#8d98a4') if self.results.get(name, {}).get('analysis_error') else QColor('#253142'))
        for i, name in enumerate(names):
            if not self.results.get(name, {}).get('analysis_error'):
                self.topic_items[name].setForeground(0, QColor(COLORS[i % len(COLORS)]))
        self.topics.blockSignals(False)
        self.topic_count.setText(f'{len(names)} / {len(self.topic_items)} 个话题已勾选')
        self.update_summary()
        missing = [name for name in names if name not in self.results]
        if missing:
            self.status.setText(f'已勾选 {len(names)} 个话题，其中 {len(missing)} 个尚未读取；点击“读取所选”。')
        self.update_controls()

    def set_metric(self, metric):
        self.metric_buttons[metric].setChecked(True)
        self.explanation.setText(METRICS[metric][3])
        self.chart.set_metric(metric)
        self.update_summary()

    def update_summary(self):
        selected = self.chart.topics
        if not selected:
            self.metric_summary.setText('勾选左侧话题查看数据')
            return
        metric = self.chart.metric
        explanation = METRICS[metric][3]
        if metric != 'timestamps':
            widths = sorted({t['bucket_seconds'] for t in selected if t['series']})
            if widths:
                explanation += ' 当前窗口：' + ' / '.join(f'{width:g} s' for width in widths) + '。'
        self.explanation.setText(explanation)
        fields = {'frequency': 'interval_hz', 'count': 'count', 'maximum': 'max_interval_ms', 'minimum': 'min_interval_ms', 'timestamps': 'valid_timestamp_count'}
        values = []
        for topic in selected[:3]:
            value = topic[fields[metric]]
            unit = '条' if metric in ('count', 'timestamps') else METRICS[metric][1]
            text = f'{value:,}' if metric in ('count', 'timestamps') else number(value)
            prefix = topic['name'].rstrip('/').rsplit('/', 1)[-1] + '  ' if len(selected) > 1 else ''
            values.append(prefix + text + ' ' + unit)
        self.metric_summary.setText('    /    '.join(values))
        problems = [t['analysis_error'] for t in selected if t['analysis_error']]
        warnings = [w for t in selected for w in t['warnings']]
        if problems:
            self.status.setText(problems[0])
        elif warnings:
            self.status.setText('；'.join(warnings))
        elif self.report:
            self.status.setText(f"header.stamp 分析 · {self.report['message_count']:,} 条消息 · 勾选多个话题可比较同一指标")
        if self.dataset and self.dataset['warnings']:
            self.status.setText(self.status.value + ' · ' + self.dataset['warnings'][0])
        self.metric_summary.setToolTip('\n'.join(f"{t['name']}: {t[fields[metric]]}" for t in selected))

    def show_cursor(self, text):
        self.cursor_text.setText(text or '滚轮缩放 · 拖动平移 · 双击恢复 · 单击固定游标')

    def cancel_queries(self):
        self.query_generation += 1
        self.query_cancel.set()
        self.query_timer.stop()
        self.chart.loading = False

    def schedule_timestamps(self):
        self.cancel_queries()
        if self.chart.metric == 'timestamps' and self.chart.topics and self.result_job and not self.busy and not self.host.pending_close:
            self.query_timer.start(80)

    def load_timestamps(self):
        if self.query_running:
            self.query_timer.start(40)
            return
        if not self.result_job or self.chart.metric != 'timestamps' or self.host.pending_close:
            return
        self.query_running = True
        self.query_cancel = threading.Event()
        cancelled = self.query_cancel
        generation = self.query_generation
        origin = self.chart.origin_ns
        start = max(-2**63, origin + math.floor(self.chart.view[0] * 1e9))
        end = min(2**63 - 1, origin + math.ceil(self.chart.view[1] * 1e9))
        path = self.result_job.directory / 'timestamps.sqlite'
        names = [t['name'] for t in self.chart.topics]
        self.chart.loading = True
        self.chart.update()

        def done(result=None, error=''):
            self.query_running = False
            if generation != self.query_generation:
                return
            self.chart.loading = False
            if not error:
                self.chart.stamps = result
                self.chart.describe_cursor()
            elif not cancelled.is_set():
                self.status.setText('读取时间戳失败：' + error)
            self.chart.update()

        self.host.run_task(lambda log: query_timestamps(path, names, start, end, cancelled),
                           lambda result: done(result), lambda error: done(error=error))

    def choose_export(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出分析报告（同时生成异常 CSV）', 'bag_analysis.json', 'JSON 报告 (*.json)',
                                             options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        destination = Path(path).with_suffix('.json')
        csv_path = destination.with_name(destination.stem + '_anomalies.csv')
        existing = [str(p) for p in (destination, csv_path) if p.exists()]
        if existing:
            from PyQt5.QtWidgets import QMessageBox
            answer = QMessageBox.question(self, '覆盖报告文件', '以下文件已存在，是否覆盖？\n' + '\n'.join(existing),
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.start_export(destination)

    def start_export(self, destination):
        if self.busy or not self.report or self.host._busy_guard():
            return
        self.exporting = True
        self.export_cancel.clear()
        self.progress.setRange(0, 0)
        self.status.setText('正在导出报告与完整异常明细…')
        self.update_controls()
        self.busyChanged.emit()
        def complete(error=''):
            self.exporting = False
            self.progress.setRange(0, 100)
            self.progress.setValue(0 if error else 100)
            self.status.setText(('导出失败：' + error) if error else f'已导出：{destination} 及对应的 _anomalies.csv')
            self.update_controls()
            self.busyChanged.emit()
        self.host.run_task(lambda log: self.export_report(destination, self.export_cancel),
                           lambda _: complete(), lambda error: complete(error))

    def export_report(self, destination, cancelled=None):
        if not self.result_job or self.job:
            raise ValueError('请先完成扫描。')
        destination = Path(destination).expanduser().resolve()
        csv_path = destination.with_name(destination.stem + '_anomalies.csv')
        # Never overwrite an input file, even if a caller bypasses the file dialog.
        inputs = {Path(f['path']).resolve() for f in self.dataset['fingerprint']}
        if destination in inputs or csv_path in inputs:
            raise ValueError('报告不能覆盖 bag 输入文件。')
        staged = []
        try:
            for source, target in ((self.result_job.directory / 'anomalies.csv', csv_path),
                                   (self.result_job.directory / 'result.json', destination)):
                with tempfile.NamedTemporaryFile(dir=str(target.parent), prefix='.analysis-export-', delete=False) as stream:
                    staged.append((Path(stream.name), target))
                    with source.open('rb') as incoming:
                        while True:
                            if cancelled is not None and cancelled.is_set():
                                raise ValueError('已取消，原有导出文件保留。')
                            chunk = incoming.read(1024 * 1024)
                            if not chunk:
                                break
                            stream.write(chunk)
            if cancelled is not None and cancelled.is_set():
                raise ValueError('已取消，原有导出文件保留。')
            for temporary, target in staged:
                temporary.replace(target)
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)

    def dispose(self):
        self.clear_report()
