"""Administrator workspace for deployed ROS 2 transport and verification."""
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget

from .account_dds import AccountDDSProfile
from .communication_dialog import CommunicationForm
from .host_diagnostics_dialog import HostDiagnosticsPanel


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
        description = QLabel('配置驱动和感知程序使用的 Fast DDS 传输，再核对运行进程和话题 QoS。')
        description.setWordWrap(True)
        layout.addWidget(description)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        deployment = QWidget()
        deploy_layout = QVBoxLayout(deployment)
        self.form = CommunicationForm(deployment, self.profile.settings(), deployment=True)
        deploy_layout.addWidget(self.form)
        controls = QHBoxLayout()
        self.enable_button = QPushButton('设为当前账户默认')
        self.enable_button.setProperty('primary', True)
        self.enable_button.clicked.connect(self.enable_profile)
        controls.addWidget(self.enable_button)
        self.disable_button = QPushButton('撤销账户默认')
        self.disable_button.clicked.connect(self.disable_profile)
        controls.addWidget(self.disable_button)
        self.copy_button = QPushButton('复制终端加载命令')
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
        scope = QLabel('账户默认通过 Linux 用户会话加载。注销并重新登录后启动程序，或在当前终端粘贴加载命令后再启动。'
                       '系统服务、容器和由其他账户运行的程序需在各自启动环境中指定 XML。')
        scope.setWordWrap(True)
        deploy_layout.addWidget(scope)
        compare = QPushButton('前往数据分析做前后对照')
        compare.clicked.connect(lambda: host.nav.setCurrentRow(4))
        deploy_layout.addWidget(compare)
        deploy_layout.addStretch()
        self.tabs.addTab(deployment, 'DDS 与共享内存')

        self.diagnostics = HostDiagnosticsPanel(host, self)
        self.tabs.addTab(self.diagnostics, '环境检查与配置参考')
        self.tabs.currentChanged.connect(self.refresh_diagnostics)
        self.update_state()

    def refresh_diagnostics(self, index):
        if self.tabs.widget(index) is self.diagnostics and self.host.admin_unlocked:
            self.diagnostics.refresh()

    def update_state(self):
        active = self.profile.active()
        self.state.setText(f'账户默认 · {"已登记" if active else "未启用"}\n配置文件 · {self.profile.xml_path}')
        self.disable_button.setEnabled(active)
        self.copy_button.setEnabled(self.profile.xml_path.is_file())

    def enable_profile(self):
        if not self.host.admin_unlocked:
            return
        try:
            self.profile.enable(self.form.settings())
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
        QApplication.clipboard().setText(self.profile.shell_command())
        self.message.setText('已复制加载命令。在启动驱动或感知程序的终端执行后，再启动该程序。')
