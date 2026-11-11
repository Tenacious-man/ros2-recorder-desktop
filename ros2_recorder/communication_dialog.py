"""Controls and comparison views for live communication diagnostics."""
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .communication import CommunicationSettings, DEFAULT_SHM_MIB, RELIABILITY, TRANSPORTS, host_limits, profile_shell_command
from .capacity_widget import CapacitySpinBox
from .units import KIB, MIB, format_capacity, format_size


class CommunicationForm(QWidget):
    def __init__(self, parent, settings, *, deployment=False):
        super().__init__(parent)
        self.deployment = deployment
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        if not deployment:
            self.reliability = QComboBox()
            for key, label in RELIABILITY.items():
                self.reliability.addItem(label, key)
            self.reliability.setCurrentIndex(self.reliability.findData(settings.reliability))
            self.reliability.setToolTip('优先最新适合实时感知。允许重传要求发布端支持 Reliable，可能增加等待时间。')
            form.addRow('订阅方式', self.reliability)
            self.depth = self.spin(form, '最多保留帧数', 1, 100, settings.depth)
            self.depth.setToolTip('只保留最近几帧。实时预览可从 1～2 帧起步。')
            self.duration = self.spin(form, '每轮监听时长', 0, 3600, settings.duration_s, ' 秒')
            self.duration.setSpecialValueText('手动停止')
            self.run_note = QLineEdit(settings.note)
            self.run_note.setMaxLength(120)
            self.run_note.setPlaceholderText(f'可选，例如：雷达发布端 {format_capacity(DEFAULT_SHM_MIB * MIB, "MB")}')
            form.addRow('对照备注', self.run_note)
        self.transport = QComboBox()
        for key, label in TRANSPORTS.items():
            if not deployment or key != 'environment':
                self.transport.addItem(label, key)
        self.transport.setCurrentIndex(max(0, self.transport.findData(settings.transport)))
        self.transport.setToolTip('共享内存传输适合同机进程，UDP 也可用于跨机通信。仅 UDP 可用于对照排查。')
        form.addRow('Fast DDS 传输方式', self.transport)
        self.shm = CapacitySpinBox(settings.shm_mib, 1, 256, MIB, 'MB')
        self.shm.setToolTip('每个 Fast DDS 参与者创建的共享内存段大小。本机可用共享内存是所有进程共用的总余量。')
        form.addRow('共享内存段大小', self.shm)
        self.udp = CapacitySpinBox(settings.udp_kib, 0, 65536, KIB, 'KB')
        self.udp.setToolTip('Fast DDS 向 Linux 请求的 UDP 套接字接收缓冲。系统默认使用内核默认值，手动值受内核上限约束。')
        form.addRow('UDP 接收缓冲请求值', self.udp)
        self.udp.setSpecialValueText('系统默认')
        shm, self.udp_limit = host_limits()
        self.limits = QLabel()
        self.limits.setWordWrap(True)
        self.host_text = (
            f'本机共享内存可用 {format_size(shm) if shm is not None else "未知"} · '
            f'Linux UDP 接收缓冲上限 {format_size(self.udp_limit) if self.udp_limit is not None else "未知"}')
        layout.addWidget(self.limits)
        self.copy_linux = QPushButton('复制 Linux UDP 上限设置命令')
        self.copy_linux.clicked.connect(self.copy_linux_command)
        layout.addWidget(self.copy_linux)
        if deployment:
            field_help = QLabel('共享内存加 UDP 可用于同机点云和跨机通信，仅 UDP 可用于对照排查。'
                                '共享内存段按 DDS 参与者分配，UDP 缓冲请求值受 Linux 上限约束。')
            field_help.setWordWrap(True)
            layout.addWidget(field_help)
        note = QLabel('驱动和感知程序启动时加载 Fast DDS 配置。每个程序的话题 QoS 由它自己的代码或启动参数决定。' if deployment else
                      '保存后用于软件的下一轮监听。部署到驱动或感知程序时，可在管理员模块“通信环境”中操作。')
        note.setWordWrap(True)
        layout.addWidget(note)
        preset = QPushButton(f'套用同机大点云建议（{format_capacity(DEFAULT_SHM_MIB * MIB, "MB")}）')
        preset.clicked.connect(self.use_lidar_preset)
        layout.addWidget(preset)
        self.export_button = QPushButton('导出 Fast DDS XML 并复制终端加载命令…')
        self.export_button.setToolTip('将表单中的传输参数导出为 XML，可在启动任意 Fast DDS 程序前加载。')
        self.export_button.clicked.connect(self.export_profile)
        self.export_button.setVisible(deployment)
        layout.addWidget(self.export_button)
        self.export_status = QLabel()
        self.export_status.setWordWrap(True)
        layout.addWidget(self.export_status)
        self.transport.currentIndexChanged.connect(self.update_options)
        self.udp.valueChanged.connect(self.update_options)
        self.update_options()

    def spin(self, form, label, minimum, maximum, value, suffix=''):
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSuffix(suffix)
        form.addRow(label, spin)
        return spin

    def use_lidar_preset(self):
        if not self.deployment:
            self.reliability.setCurrentIndex(self.reliability.findData('best_effort'))
            self.depth.setValue(2)
        self.transport.setCurrentIndex(self.transport.findData('shm'))
        self.shm.set_binary_value(DEFAULT_SHM_MIB)
        self.udp.set_binary_value(0)

    def update_options(self):
        transport = self.transport.currentData()
        custom = transport != 'environment'
        self.shm.setEnabled(transport == 'shm')
        self.udp.setEnabled(custom)
        self.export_button.setEnabled(custom)
        limited = custom and self.udp_limit is not None and self.udp.binary_value() * KIB > self.udp_limit
        self.copy_linux.setVisible(self.deployment and limited)
        warning = '。请求值超过 Linux 上限，实际缓冲会受限。' if limited else ''
        self.limits.setText(self.host_text + warning)

    def copy_linux_command(self):
        QApplication.clipboard().setText(f'sudo sysctl -w net.core.rmem_max={self.udp.binary_value() * KIB}')
        self.export_status.setText('已复制临时调整命令，可在本机终端运行。下一轮监听时会重新请求缓冲。')

    def settings(self):
        transport = dict(transport=self.transport.currentData(),
                         shm_mib=self.shm.binary_value(), udp_kib=self.udp.binary_value())
        if self.deployment:
            return CommunicationSettings(**transport)
        return CommunicationSettings(reliability=self.reliability.currentData(), depth=self.depth.value(),
                                     duration_s=self.duration.value(), note=self.run_note.text().strip(), **transport)

    def export_profile(self):
        return self.export_profile_for(profile_shell_command)

    def export_profile_for(self, command_for_path):
        path, _ = QFileDialog.getSaveFileName(self, '导出 Fast DDS 配置', 'fastdds-pointcloud.xml', 'XML 文件 (*.xml)')
        if not path:
            return
        target = Path(path).absolute()
        if not target.suffix:
            target = target.with_suffix('.xml')
            if target.exists() and QMessageBox.question(self, '覆盖已有文件', f'覆盖 {target.name}？') != QMessageBox.Yes:
                return
        try:
            command = command_for_path(target)
            target.write_text(self.settings().fastdds_xml(), encoding='utf-8')
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '导出失败', str(exc))
            return
        QApplication.clipboard().setText(command)
        self.export_status.setText('已保存 XML 并复制加载命令。先在终端执行加载命令，再启动所需的驱动或感知程序。')
        return target


class CommunicationDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle('对照监听设置')
        self.setMinimumWidth(570)
        layout = QVBoxLayout(self)
        self.form = CommunicationForm(self, settings)
        layout.addWidget(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('保存监听设置')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def settings(self):
        return self.form.settings()


class CommunicationComparison(QDialog):
    def __init__(self, parent, baseline, current):
        super().__init__(parent)
        self.setWindowTitle('通信对照')
        self.resize(690, 570)
        self.runs = (baseline, current)
        layout = QVBoxLayout(self)
        self.topic = QComboBox()
        names = sorted({name for run in self.runs if run for name in run['samples']})
        self.topic.addItems(names)
        form = QFormLayout()
        form.addRow('查看话题', self.topic)
        layout.addLayout(form)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(['对照基线', '本轮结果'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.topic.currentTextChanged.connect(self.render)
        self.render()

    def render(self):
        name = self.topic.currentText()
        fields = [
            ('对照备注', 'note', ''), ('订阅方式', 'reliability', ''), ('队列深度', 'depth', ''),
            ('监听传输配置', 'transport', ''),
            ('ROS Domain', 'domain', ''), ('消息类型', 'kind', ''),
            ('观察时长', 'seconds', ' 秒'), ('接收帧数', 'received_count', ' 帧'),
            ('平均接收频率', 'hz', ' Hz'), ('最长接收间隔', 'max_gap_ms', ' ms'),
            ('帧龄 P99', 'age_p99_ms', ' ms'), ('最大帧龄', 'age_max_ms', ' ms'),
            ('有效帧龄样本', 'age_count', ' 帧'),
        ]
        self.table.setRowCount(len(fields))
        self.table.setVerticalHeaderLabels([label for label, _, _ in fields])
        entries = []
        for column, run in enumerate(self.runs):
            if run and name in run['samples']:
                entry = {**run['samples'][name], **run['settings']}
                entries.append(entry)
            else:
                entry = {}
            for row, (_, key, unit) in enumerate(fields):
                value = entry.get(key)
                text = f'{value:.2f}' if isinstance(value, float) else str(value)
                item = QTableWidgetItem('—' if value is None or value == '' else text + unit)
                item.setToolTip(item.text())
                self.table.setItem(row, column, item)
        warning = ''
        if len(entries) == 2:
            if any(entries[0][key] != entries[1][key] for key in ('kind', 'domain')):
                warning = '两轮的消息类型或 ROS Domain 不同。'
            elif abs(entries[0]['seconds'] - entries[1]['seconds']) > 1:
                warning = '两轮时长不同，建议用同样的时长和负载复测。'
        self.note.setText(warning + '接收间隔含末尾等待。帧龄为接收时间减消息时间，需先确认授时。P99 取最近 2048 条有效帧龄。')
