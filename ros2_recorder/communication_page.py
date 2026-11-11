"""Administrator workspace for deployed ROS 2 transport and verification."""
from PyQt5.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from .account_dds import AccountDDSProfile
from .communication_dialog import CommunicationForm
from .host_diagnostics_dialog import HostDiagnosticsPanel
from .launch_environment_form import LaunchEnvironmentForm


class CommunicationPage(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.profile = AccountDDSProfile(host.store.path.parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel('通信环境')
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        description = QLabel('设置 ROS 启动环境、DDS 传输和发现范围，再核对运行进程和话题 QoS。')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        deployment = QWidget()
        deploy_layout = QVBoxLayout(deployment)
        self.form = CommunicationForm(deployment, self.profile.settings(), deployment=True)
        deploy_layout.addWidget(self.form)
        deploy_layout.addStretch()
        self.tabs.addTab(deployment, 'DDS 与共享内存')

        self.startup_form = LaunchEnvironmentForm(self.profile.startup_settings())
        self.startup_form.copy_button.clicked.connect(self.copy_current_command)
        startup_scroll = QScrollArea()
        startup_scroll.setFrameShape(QFrame.NoFrame)
        startup_scroll.setWidgetResizable(True)
        startup_scroll.setWidget(self.startup_form)
        self.tabs.addTab(startup_scroll, 'ROS 与发现')

        self.diagnostics = HostDiagnosticsPanel(host, self)
        self.tabs.addTab(self.diagnostics, '环境检查与配置参考')

        self.deployment_controls = QWidget()
        deploy_layout = QVBoxLayout(self.deployment_controls)
        deploy_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.deployment_controls)
        controls = QHBoxLayout()
        self.enable_button = QPushButton('设为当前账户默认')
        self.enable_button.setProperty('primary', True)
        self.enable_button.clicked.connect(self.enable_profile)
        controls.addWidget(self.enable_button)
        self.disable_button = QPushButton('撤销账户默认')
        self.disable_button.clicked.connect(self.disable_profile)
        controls.addWidget(self.disable_button)
        self.copy_button = QPushButton('复制已保存的账户加载命令')
        self.copy_button.setToolTip('复制已保存的 ROS 启动参数和 DDS 配置，不读取表单里尚未保存的修改。')
        self.copy_button.clicked.connect(self.copy_command)
        controls.addWidget(self.copy_button)
        controls.addStretch()
        deploy_layout.addLayout(controls)
        self.state = QLabel()
        self.state.setWordWrap(True)
        deploy_layout.addWidget(self.state)
        self.message = QLabel()
        self.message.setWordWrap(True)
        deploy_layout.addWidget(self.message)
        scope = QLabel('账户默认通过 Linux 用户会话加载，重新登录后用于新进程。系统服务和容器使用自己的启动环境。'
                       '话题 QoS 由各程序的发布端和订阅端设置。')
        scope.setWordWrap(True)
        deploy_layout.addWidget(scope)
        compare = QPushButton('前往数据分析做前后对照')
        compare.clicked.connect(lambda: host.nav.setCurrentRow(4))
        deploy_layout.addWidget(compare)
        self.tabs.currentChanged.connect(self.refresh_diagnostics)
        self.update_state()

    def refresh_diagnostics(self, index):
        checking = self.tabs.widget(index) is self.diagnostics
        self.deployment_controls.setVisible(not checking)
        if checking and self.host.admin_unlocked:
            self.diagnostics.refresh()

    def update_state(self):
        active = self.profile.active()
        startup = self.profile.startup_settings()
        has_xml = self.profile.xml_path.is_file()
        profile = f'\n生成的 Fast DDS XML · {self.profile.xml_path}' if startup.generate_fastdds and has_xml else ''
        self.state.setText(f'账户默认 · {"已登记" if active else "未启用"}{profile}')
        self.disable_button.setEnabled(active)
        self.copy_button.setEnabled(has_xml or self.profile.startup_path.is_file() or active)

    def enable_profile(self):
        if not self.host.admin_unlocked:
            return
        try:
            self.profile.enable(self.form.settings(), self.startup_form.settings())
        except (OSError, ValueError) as exc:
            self.message.setText(f'保存失败 · {exc}')
            return
        self.message.setText('已写入当前账户默认配置。重新登录后启动驱动和感知程序，再到“运行进程”核对。')
        self.update_state()

    def disable_profile(self):
        if not self.host.admin_unlocked:
            return
        try:
            self.profile.disable()
        except (OSError, ValueError) as exc:
            self.message.setText(f'撤销失败 · {exc}')
            return
        self.message.setText('已撤销账户默认项。重新登录后启动的程序按各自启动环境加载配置。')
        self.update_state()

    def copy_command(self):
        try:
            command = self.profile.shell_command()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        QApplication.clipboard().setText(command)
        self.message.setText('已复制已保存的账户加载命令。在目标程序的终端执行后，再启动该程序。')

    def copy_current_command(self):
        try:
            startup = self.startup_form.settings()
            if startup.generate_fastdds:
                if self.form.export_profile_for(startup.shell_command) is None:
                    return
            else:
                QApplication.clipboard().setText(startup.shell_command())
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.message.setText('已复制当前表单的加载命令。在目标终端执行后，再启动驱动或感知程序。')
