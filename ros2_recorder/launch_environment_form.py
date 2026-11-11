"""Editable ROS startup options and their scope in the communication workspace."""
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .communication import PROFILE_VARIABLES
from .environment import installed_environments
from .launch_environment import BUILTIN_TRANSPORTS, DISCOVERY_RANGES, LaunchEnvironment


class LaunchEnvironmentForm(QWidget):
    def __init__(self, startup, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        note = QLabel('空白或“沿用”表示保留程序原有的启动环境。ROS 版本由 setup.bash 决定。'
                      '切换工作台当前加载的版本可在“消息环境”中操作。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.copy_button = QPushButton('复制当前参数的终端命令')
        layout.addWidget(self.copy_button, alignment=Qt.AlignLeft)
        self.form = QFormLayout()
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        layout.addLayout(self.form)
        self.fields = {}

        self.setup = QComboBox()
        self.setup.setEditable(True)
        self.setup.setInsertPolicy(QComboBox.NoInsert)
        self.setup.addItem('')
        for path in installed_environments():
            self.setup.addItem(str(path))
        self.setup.setEditText(startup.setup)
        self.setup.lineEdit().setPlaceholderText('沿用终端已加载的 ROS 2')
        self.add_field('ROS 版本 / 基础环境', self.setup,
                       '选择对应版本的 setup.bash，例如 /opt/ros/humble/setup.bash。'
                       '复制的终端命令会加载这个脚本，账户默认只保存环境变量。',
                       self.browse_button(self.setup.setEditText, '选择 ROS 2 基础环境 setup.bash', '*.bash'))
        self.rmw = self.choice('RMW_IMPLEMENTATION', 'ROS 中间件',
                               ('rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp', 'rmw_cyclonedds_cpp'),
                               '选择已安装的 RMW。Fast DDS 使用 Fast DDS XML，Cyclone DDS 使用自己的 XML。',
                               startup, editable=True)
        self.text_field('ROS_DOMAIN_ID', 'ROS 通信域',
                        '0～232。需要互相发现的节点使用同一个 Domain，沿用时通常是 0。', startup, '例如 0')
        self.choice('ROS_LOCALHOST_ONLY', '仅本机通信', ('0', '1'),
                    '0 允许 ROS 跨机通信，1 限制在本机。Humble 使用此项。使用下方新版发现选项时，此项留为沿用。', startup)

        self.profile_source = QComboBox()
        self.profile_source.addItem('由工作台生成 Fast DDS XML', True)
        self.profile_source.addItem('手动指定或沿用', False)
        self.profile_source.setCurrentIndex(0 if startup.generate_fastdds else 1)
        self.add_field('DDS 配置来源', self.profile_source,
                       '生成模式采用“DDS 与共享内存”页的参数。手动模式可以指定现有 XML 或使用内建传输。')
        for key, title, help_text in (
            (PROFILE_VARIABLES[0], 'Fast DDS XML（旧版变量）', 'Humble 常用的 Fast DDS 2.6 从这个变量读取配置。'),
            (PROFILE_VARIABLES[1], 'Fast DDS XML（新版变量）', '适用于读取此变量的新版 Fast DDS。跨版本部署时可填写同一文件。'),
        ):
            self.text_field(key, title, help_text, startup, '/绝对路径/fastdds.xml', browse=True)
        self.text_field('CYCLONEDDS_URI', 'Cyclone DDS 配置',
                        '仅 Cyclone DDS 使用。可填写 file:///绝对路径/cyclonedds.xml，也可填写单行 XML。',
                        startup, 'file:///绝对路径/cyclonedds.xml', browse=True)
        self.choice('FASTDDS_BUILTIN_TRANSPORTS', 'Fast DDS 内建传输', BUILTIN_TRANSPORTS,
                    '只在启用内建传输时生效。DEFAULT 为共享内存加 UDPv4，DEFAULTv6 改用 UDPv6。'
                    'SHM、UDPv4、UDPv6 各只启用一种传输。LARGE_DATA 用 UDP 发现并以 TCP 或共享内存传数据。'
                    'NONE 需要由 XML 提供传输。', startup)
        self.text_field('ROS_DISCOVERY_SERVER', 'Fast DDS 发现服务器',
                        '填已经部署的服务器地址和端口。多个地址用分号分隔。普通发现时留空。',
                        startup, '例如 192.168.1.50:11811')
        self.choice('ROS_AUTOMATIC_DISCOVERY_RANGE', '自动发现范围', DISCOVERY_RANGES,
                    'ROS 2 Iron 起支持。SUBNET 使用可达组播，LOCALHOST 只发现本机，OFF 关闭自动发现，'
                    'SYSTEM_DEFAULT 交给中间件。Humble 请使用“仅本机通信”。', startup)
        self.text_field('ROS_STATIC_PEERS', '静态发现对端',
                        'ROS 2 Iron 起支持。多个主机地址用分号分隔，与自动发现范围配合使用。',
                        startup, '例如 192.168.1.50;192.168.1.51')

        self.profile_source.currentIndexChanged.connect(self.update_options)
        self.update_options()

    def add_field(self, title, editor, explanation, browse=None):
        field = QWidget()
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.addWidget(editor, 1)
        if browse is not None:
            row.addWidget(browse)
        layout.addLayout(row)
        hint = QLabel(explanation)
        hint.setProperty('secondary', True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        editor.setToolTip(explanation)
        self.form.addRow(title, field)
        return field

    def browse_button(self, apply, title, pattern, *, uri=False):
        button = QPushButton('浏览…')

        def choose():
            path, _ = QFileDialog.getOpenFileName(self, title, '', f'配置文件 ({pattern})')
            if path:
                apply(Path(path).as_uri() if uri else path)

        button.clicked.connect(choose)
        return button

    def choice(self, key, title, options, help_text, startup, *, editable=False):
        editor = QComboBox()
        editor.setEditable(editable)
        editor.setInsertPolicy(QComboBox.NoInsert)
        editor.addItem('沿用', '')
        for value in options:
            editor.addItem(value, value)
        value = startup.variables.get(key, '')
        index = editor.findData(value)
        editor.setCurrentIndex(max(0, index))
        if editable and index < 0:
            editor.setEditText(value)
        row = self.add_field(f'{title}（{key}）', editor, help_text)
        self.fields[key] = editor, row
        return editor

    def text_field(self, key, title, help_text, startup, placeholder, *, browse=False):
        editor = QLineEdit(startup.variables.get(key, ''))
        editor.setPlaceholderText(placeholder)
        button = (self.browse_button(editor.setText, title, '*.xml', uri=key == 'CYCLONEDDS_URI')
                  if browse else None)
        row = self.add_field(f'{title}（{key}）', editor, help_text, button)
        self.fields[key] = editor, row

    def update_options(self):
        generated = self.profile_source.currentData()
        for key in (*PROFILE_VARIABLES, 'FASTDDS_BUILTIN_TRANSPORTS'):
            self.fields[key][1].setEnabled(not generated)

    def settings(self):
        values = {}
        for key, (editor, _) in self.fields.items():
            if isinstance(editor, QLineEdit):
                values[key] = editor.text()
            elif editor.currentIndex() >= 0 and editor.currentText() == editor.itemText(editor.currentIndex()):
                values[key] = editor.currentData()
            else:
                values[key] = editor.currentText()
        return LaunchEnvironment(self.setup.currentText(), self.profile_source.currentData(), values)
