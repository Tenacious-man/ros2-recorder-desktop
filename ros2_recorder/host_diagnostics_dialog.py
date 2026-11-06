"""Native UI for inspecting local ROS 2 deployment settings."""
import os
import subprocess

from PyQt5.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .environment import graph_probe
from .host_diagnostics import host_report


SCENARIOS = """
<h2>按用途选起点</h2>
<p>先定义应用能容忍的最大帧龄、连续缺帧数和处理时限。以下参数适合开始测量，部署值用实际负载和故障场景验证。</p>
<h3>同机实时感知 · 点云、图像</h3>
<p>目标是优先处理新帧。订阅先用 Best Effort、Keep Last 1～2、Volatile。Fast DDS 同机可用 SHM。按最大序列化帧和并发发送留出段容量，实际收到的帧龄和连续缺帧都要记录。</p>
<p>本机 Seyond 的约 2 MiB 点云曾在默认 512 KiB 段上复现缺帧；8 MiB 在隔离对照中收齐 70 帧。用相同话题和负载给真实发布进程复测。</p>
<h3>多传感器融合</h3>
<p>先核对设备时钟基准、header 时间代表的采样时刻和各源频率。队列只容纳配对窗口内需要的帧；按允许时差配对并丢弃过期帧。用配对成功率、时差、等待时间和超时降级验收。</p>
<h3>控制周期与关键事件</h3>
<p>周期命令按时限选 QoS，接收端检查序号和有效期。监测心跳与超时，进入系统定义的安全状态。需要确认的一次性事件可用 Reliable 配合应用层确认。验证最坏周期、尾延迟和故障响应。</p>
<h3>录包与离线诊断</h3>
<p>优先完整时，发布端兼容的情况下试用 Reliable，并实测磁盘写入、消息计数、缓存积压和同机感知延迟。只做实时预览时可接受跳过旧帧，队列宜浅。</p>
<h3>跨机传输</h3>
<p>先核对网卡速率、ROS Domain、发现配置和可用带宽。用 UDP 时测接收机 socket 丢包、丢帧与端到端延迟；按实测突发量调整 DDS socket 请求和 Linux 上限。共享内存适用于同机进程。</p>
<p><a href="https://github.com/ros2/ros2_documentation/blob/humble/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst">ROS 2 Humble QoS</a> · <a href="https://design.ros2.org/articles/realtime_background.html">ROS 2 实时系统设计</a></p>
"""

PARAMETERS = """
<h2>各项配置的作用</h2>
<h3>ROS 2 节点和话题</h3>
<ul>
<li><b>Best Effort / Reliable</b>决定缺失数据是否尝试重传。重传有助于补帧，也可能使新帧排队。Reliable 订阅要求发布端支持。</li>
<li><b>Keep Last / Depth</b>保留最近 N 条消息。加深队列能缓冲短时积压，也可能让应用处理更旧的数据。Keep All 仍受中间件资源上限约束。</li>
<li><b>Volatile / Transient Local</b>影响新加入的订阅者能否收到发布端保留的历史数据。动态传感器通常用 Volatile，静态状态或配置可考虑 Transient Local。</li>
<li><b>Deadline / Lifespan</b>分别用于监测更新是否及时和限制消息有效期。Deadline 需要端点兼容；控制应用仍需按业务时间检查过期和超时。</li>
<li><b>Liveliness / Lease</b>用于监测发布端是否还活着。租约到期表示失去活性，应用应定义收到告警后的处理。</li>
<li><b>Domain / 仅本机</b>影响 ROS 节点发现范围。需要互通的节点使用兼容的发现配置；仅本机限制用于隔离测试或单机部署。</li>
</ul>
<h3>DDS 中间件和 Linux</h3>
<ul>
<li><b>RMW 与配置文件</b>决定 ROS 2 使用的中间件和传输。Fast DDS 与 Cyclone DDS 使用不同配置格式。部署时确认真正的驱动、感知节点、服务或容器都从自己的启动环境加载了预期配置。</li>
<li><b>SHM 段大小 / /dev/shm 容量</b>分别表示 Fast DDS 参与者的段容量和系统共享内存文件系统的可用空间。段太小可能在发送大消息时丢分片；系统可用空间还要覆盖所有参与者的分配。</li>
<li><b>UDP 缓冲</b>由 DDS 向内核申请。rmem_default 和 wmem_default 是默认值，rmem_max 和 wmem_max 限制常规 socket 请求。网口与 UDP 累计计数要比较测量前后的增量。</li>
<li><b>线程调度 / CPU 亲和性</b>决定线程怎样争用 CPU。RT 内核、FIFO/RR、CPU 隔离可作为控制链路的调优手段，先用满负载测试确定需要和优先级。</li>
<li><b>授时</b>决定不同设备的时间戳能否比较。帧龄用本机接收时间减消息时间，前提是两者时钟基准一致。Linux 系统时钟已同步和 PTP 进程存在都需要与设备端锁定状态分开核对。</li>
</ul>
<p><a href="https://fast-dds.docs.eprosima.com/en/2.6.x/fastdds/transport/shared_memory/shared_memory.html">Fast DDS 共享内存</a> · <a href="https://docs.kernel.org/admin-guide/sysctl/net.html">Linux 网络参数</a> · <a href="https://design.ros2.org/articles/qos_deadline_liveliness_lifespan.html">ROS 2 时限与活性</a></p>
"""


def duration_text(ns):
    if ns >= 2147483647 * 10**9:
        return '无限'
    return f'{ns / 1e6:g} ms' if ns > 0 else '默认或未报告'


def format_graph(data):
    lines = [f'探测进程实际中间件 · {data["rmw"]} · ROS Domain {data["domain"]}',
             '此处是 ROS 图发现的发布端和订阅端 QoS。部分中间件不会公布历史深度等参数。']
    if not data['endpoints']:
        lines.append('当前话题未发现端点。')
    for item in data['endpoints']:
        depth = item['depth'] if item['depth'] and item['history'] == 'KEEP_LAST' else '未报告或不适用'
        lines.extend((
            f'\n{item["direction"]} · {item["node"]} · {item["type"]}',
            f'  可靠性 {item["reliability"]} · 历史 {item["history"]} · 深度 {depth} · 持久性 {item["durability"]}',
            f'  更新时限 {duration_text(item["deadline"])} · 有效期 {duration_text(item["lifespan"])}'
            f' · 活性 {item["liveliness"]} · 租约 {duration_text(item["lease"])}',
        ))
    return '\n'.join(lines)


class HostDiagnosticsPanel(QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        layout = QVBoxLayout(self)
        label = QLabel('检查当前主机、运行进程和 ROS 图。按用途建议和参数说明可作为部署参考。')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.overview = self.report_view()
        self.processes = self.report_view()
        self.tabs.addTab(self.overview, '当前环境')
        self.tabs.addTab(self.processes, '运行进程')
        qos = QWidget()
        qos_layout = QVBoxLayout(qos)
        chooser = QHBoxLayout()
        chooser.addWidget(QLabel('话题'))
        self.topic = QComboBox()
        self.topic.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.topic.setMinimumContentsLength(20)
        self.topic.currentIndexChanged.connect(self.refresh_qos)
        chooser.addWidget(self.topic, 1)
        qos_layout.addLayout(chooser)
        self.qos = self.report_view()
        qos_layout.addWidget(self.qos)
        self.tabs.addTab(qos, '话题 QoS')
        self.guides = []
        for title, content in (('按用途建议', SCENARIOS), ('参数说明', PARAMETERS)):
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setHtml(content)
            self.tabs.addTab(view, title)
            self.guides.append(view)
        actions = QHBoxLayout()
        self.refresh_button = QPushButton('重新检查')
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        copy = QPushButton('复制检查结果')
        copy.clicked.connect(self.copy_report)
        actions.addWidget(copy)
        actions.addStretch()
        layout.addLayout(actions)

    @staticmethod
    def report_view():
        view = QPlainTextEdit()
        view.setReadOnly(True)
        return view

    def refresh(self):
        self.check(include_host=True)

    def refresh_qos(self):
        self.check(include_host=False)

    def check(self, *, include_host):
        host = self.host
        if not self.refresh_button.isEnabled() or host.pending_close:
            return
        self.refresh_button.setEnabled(False)
        self.topic.setEnabled(False)
        env = dict(host.active_env or os.environ)
        topic = self.topic.currentText() or next(iter(host.analysis_widget.selected), '')
        self.qos.setPlainText('正在发现话题端点…')

        def read(_log):
            reports = host_report(env) if include_host else None
            try:
                graph = graph_probe(env, 'qos', '--topic', topic)
                graph['domain'] = env.get('ROS_DOMAIN_ID') or '0'
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                graph = {'error': f'无法查询 ROS 图 · {exc}'}
            return reports, graph

        host.run_task(read, self.show_result, lambda error: self.show_result((None, {'error': error})))

    def show_result(self, result):
        reports, graph = result
        if reports:
            self.overview.setPlainText(reports[0])
            self.processes.setPlainText(reports[1])
        if 'error' in graph:
            self.qos.setPlainText(graph['error'])
        else:
            self.topic.blockSignals(True)
            self.topic.clear()
            self.topic.addItems(graph['names'])
            self.topic.setCurrentIndex(self.topic.findText(graph['topic']))
            self.topic.blockSignals(False)
            self.qos.setPlainText(format_graph(graph))
        self.refresh_button.setEnabled(True)
        self.topic.setEnabled(True)

    def copy_report(self):
        sections = ('当前环境\n' + self.overview.toPlainText(), '运行进程\n' + self.processes.toPlainText(),
                    f'话题 QoS · {self.topic.currentText()}\n' + self.qos.toPlainText())
        QApplication.clipboard().setText('\n\n'.join(sections))
