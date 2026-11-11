"""Persistent ROS and DDS defaults for the current Linux login account."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from xml.etree import ElementTree as ET

from .communication import CommunicationSettings, DEFAULT_SHM_MIB
from .launch_environment import LaunchEnvironment


XML_MARKER = '<!-- Managed by ROS Recorder account DDS settings -->\n'
ENV_MARKER = '# Managed by ROS Recorder\n'
STARTUP_MARKER = '# Startup profile '


def write_text_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class AccountDDSProfile:
    def __init__(self, config_dir):
        self.xml_path = config_dir.absolute() / 'account-fastdds.xml'
        self.env_path = config_dir.absolute().parent / 'environment.d' / '80-ros2-recorder-dds.conf'
        self.startup_path = config_dir.absolute() / 'account-launch.json'

    def environment_text(self, startup=None):
        startup = startup or LaunchEnvironment()
        lines = [ENV_MARKER]
        if startup != LaunchEnvironment():
            lines.append(STARTUP_MARKER + json.dumps(asdict(startup), ensure_ascii=True) + '\n')
        for key, value in startup.assignments(self.xml_path).items():
            if any(char in value for char in '\r\n\0'):
                raise ValueError('配置路径或参数不能包含换行。')
            quoted = value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$')
            lines.append(f'{key}="{quoted}"\n')
        return ''.join(lines)

    def _managed_startup(self):
        if self.env_path.is_symlink():
            return None
        try:
            content = self.env_path.read_text(encoding='utf-8')
            if not content.startswith(ENV_MARKER):
                return None
            metadata = content[len(ENV_MARKER):].split('\n', 1)[0]
            startup = (LaunchEnvironment(**json.loads(metadata[len(STARTUP_MARKER):]))
                       if metadata.startswith(STARTUP_MARKER) else LaunchEnvironment())
            return startup if content == self.environment_text(startup) else None
        except (OSError, ValueError, TypeError):
            return None

    def active(self):
        return self._managed_startup() is not None

    def startup_settings(self):
        active = self._managed_startup()
        if active is not None:
            return active
        try:
            return LaunchEnvironment(**json.loads(self.startup_path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            return LaunchEnvironment()

    def settings(self):
        try:
            xml = self.xml_path.read_text(encoding='utf-8')
            if xml.startswith(XML_MARKER):
                root = ET.fromstring(xml)
                descriptors = root.findall('./{*}transport_descriptors/{*}transport_descriptor')
                types = {item.findtext('{*}type'): item for item in descriptors}
                shm = types['SHM'].findtext('{*}segment_size') if 'SHM' in types else str(DEFAULT_SHM_MIB * 1024**2)
                udp = types['UDPv4'].findtext('{*}receiveBufferSize', '0')
                return CommunicationSettings(transport='shm' if 'SHM' in types else 'udp',
                                             shm_mib=int(shm) // 1024**2, udp_kib=int(udp) // 1024)
        except (OSError, ET.ParseError, KeyError, TypeError, ValueError):
            pass
        return CommunicationSettings(transport='shm')

    def enable(self, settings, startup=None):
        startup = startup or LaunchEnvironment()
        if not startup.assignments(self.xml_path):
            raise ValueError('请至少设置一个账户环境变量。ROS 基础环境脚本通过终端命令加载。')
        content = self.environment_text(startup)
        if (self.env_path.exists() or self.env_path.is_symlink()) and not self.active():
            raise ValueError(f'已有其他配置占用 {self.env_path}，请先检查该文件。')
        if not startup.generate_fastdds:
            write_text_atomic(self.env_path, content)
            return
        if self.xml_path.is_symlink():
            raise ValueError(f'已有其他配置占用 {self.xml_path}，请先检查该文件。')
        previous_xml = self.xml_path.read_text(encoding='utf-8') if self.xml_path.exists() else None
        if previous_xml is not None and not previous_xml.startswith(XML_MARKER):
            raise ValueError(f'已有其他配置占用 {self.xml_path}，请先检查该文件。')
        write_text_atomic(self.xml_path, XML_MARKER + settings.fastdds_xml())
        try:
            write_text_atomic(self.env_path, content)
        except OSError:
            if previous_xml is None:
                self.xml_path.unlink(missing_ok=True)
            else:
                write_text_atomic(self.xml_path, previous_xml)
            raise

    def disable(self):
        startup = self._managed_startup()
        if startup is None:
            raise ValueError('未找到由本工具管理的账户默认项。')
        write_text_atomic(self.startup_path, json.dumps(asdict(startup), ensure_ascii=False, indent=2) + '\n')
        self.env_path.unlink()

    def shell_command(self):
        return self.startup_settings().shell_command(self.xml_path)
