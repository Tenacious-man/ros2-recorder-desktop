"""Recording results presented only after the recorder has saved its report."""
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (
    QApplication, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)


class RecordingSummaryDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        status = report.get('status')
        title = {'completed': '录制已保存', 'empty': '录制结束：未写入消息'}.get(status, '录制结束：保存异常')
        self.setWindowTitle(title)
        self.resize(880, 540)
        self.setMinimumSize(640, 420)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet('font-size: 18px; font-weight: bold;')
        layout.addWidget(heading)
        form = QFormLayout()
        layout.addLayout(form)
        path = report.get('bag_path', '')
        topics = report.get('topics', [])
        metadata = report.get('metadata_present', False)
        duration = report.get('duration_seconds', 0)
        count = report.get('message_count', 0)
        active_topics = sum(t.get('count', 0) > 0 for t in topics)
        details = [
            ('录制名称', Path(path).name if path else '未知'),
            ('保存位置', path or '未知'),
            ('话题数量', f'{active_topics} 个有消息 / {len(topics)} 个已登记' if metadata else '元数据不可用'),
            ('数据时长', f'{duration:.3f} 秒' if metadata else '元数据不可用'),
            ('消息总数', f'{count:,} 条' if metadata else '元数据不可用'),
        ]
        self.path_editor = None
        for label, value in details:
            editor = QLineEdit(value)
            editor.setReadOnly(True)
            editor.setCursorPosition(0)
            editor.setToolTip(value)
            form.addRow(label, editor)
            if label == '保存位置':
                self.path_editor = editor
        self.topic_table = QTableWidget(len(topics), 4)
        self.topic_table.setHorizontalHeaderLabels(['话题名称', '消息类型', '消息数', '平均频率（估算）'])
        self.topic_table.verticalHeader().hide()
        self.topic_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.topic_table.setAlternatingRowColors(True)
        self.topic_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.topic_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.topic_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.topic_table.setTextElideMode(Qt.ElideMiddle)
        lines = [title, *[f'{label}：{value}' for label, value in details], '']
        for row, topic in enumerate(topics):
            hz = topic.get('bag_average_hz')
            hz_text = f'{hz:.2f} Hz' if metadata and duration > 0 and hz is not None else '—'
            values = [topic['name'], topic['type'], f"{topic['count']:,}", hz_text]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.topic_table.setItem(row, column, item)
            self.topic_table.setRowHeight(row, 32)
            lines.append(f"{topic['name']} · {topic['type']} · {topic['count']:,} 条 · {hz_text}")
        layout.addWidget(self.topic_table, 1)
        if report.get('error') or status not in ('completed', 'empty'):
            error = report.get('error') or '录制未正常结束，已保留现有文件，请检查录制日志。'
            if report.get('forced_stop'):
                error = '录制被强制停止，文件和元数据可能未写完。' + error
            label = QLabel(error)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setStyleSheet('color: #b44b36;')
            layout.addWidget(label)
            lines += ['', error]
        if report.get('warnings'):
            warning = f"日志告警：{len(report['warnings'])} 条，可在录制记录中查看。"
            layout.addWidget(QLabel(warning))
            lines.append(warning)
        note = '平均频率 = 各话题消息数 ÷ bag 数据时长（首末消息间隔），包含话题起停差异；不代表数据完整性。'
        label = QLabel(note)
        label.setWordWrap(True)
        label.setProperty('secondary', True)
        layout.addWidget(label)
        self.summary_text = '\n'.join([*lines, '', note])
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.open_button = QPushButton('打开保存目录')
        self.open_button.setEnabled(bool(path) and Path(path).is_dir())
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        buttons.addWidget(self.open_button)
        self.copy_button = QPushButton('复制汇总')
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.summary_text))
        buttons.addWidget(self.copy_button)
        buttons.addStretch()
        self.confirm_button = QPushButton('知道了')
        self.confirm_button.setProperty('primary', True)
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.confirm_button)
