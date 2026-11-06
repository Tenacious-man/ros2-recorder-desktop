"""Persistent Fast DDS defaults for the current Linux login account."""
from pathlib import Path
import shlex
import tempfile
from xml.etree import ElementTree as ET

from .communication import CommunicationSettings, PROFILE_VARIABLE


PROFILE_VARIABLES = (PROFILE_VARIABLE, 'FASTDDS_DEFAULT_PROFILES_FILE')
XML_MARKER = '<!-- Managed by ROS Recorder account DDS settings -->\n'


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

    def environment_text(self):
        path = str(self.xml_path)
        if any(char in path for char in '\r\n'):
            raise ValueError('配置路径不能包含换行。')
        quoted = path.replace('\\', '\\\\').replace('"', '\\"').replace('$', '$$')
        return '# Managed by ROS Recorder\n' + ''.join(f'{key}="{quoted}"\n' for key in PROFILE_VARIABLES)

    def active(self):
        try:
            return not self.env_path.is_symlink() and self.env_path.read_text(encoding='utf-8') == self.environment_text()
        except (OSError, ValueError):
            return False

    def settings(self):
        try:
            xml = self.xml_path.read_text(encoding='utf-8')
            if xml.startswith(XML_MARKER):
                root = ET.fromstring(xml)
                descriptors = root.findall('./{*}transport_descriptors/{*}transport_descriptor')
                types = {item.findtext('{*}type'): item for item in descriptors}
                shm = types['SHM'].findtext('{*}segment_size') if 'SHM' in types else str(8 * 1024**2)
                udp = types['UDPv4'].findtext('{*}receiveBufferSize', '0')
                return CommunicationSettings(transport='shm' if 'SHM' in types else 'udp',
                                             shm_mib=int(shm) // 1024**2, udp_kib=int(udp) // 1024)
        except (OSError, ET.ParseError, KeyError, TypeError, ValueError):
            pass
        return CommunicationSettings(transport='shm')

    def enable(self, settings):
        content = self.environment_text()
        if (self.env_path.exists() or self.env_path.is_symlink()) and not self.active():
            raise ValueError(f'已有其他配置占用 {self.env_path}，请先检查该文件。')
        if self.xml_path.is_symlink() or (self.xml_path.exists() and
                not self.xml_path.read_text(encoding='utf-8').startswith(XML_MARKER)):
            raise ValueError(f'已有其他配置占用 {self.xml_path}，请先检查该文件。')
        write_text_atomic(self.xml_path, XML_MARKER + settings.fastdds_xml())
        write_text_atomic(self.env_path, content)

    def disable(self):
        if not self.active():
            raise ValueError('未找到由本工具管理的账户默认项。')
        self.env_path.unlink()

    def shell_command(self):
        return 'export ' + ' '.join(f'{key}={shlex.quote(str(self.xml_path))}' for key in PROFILE_VARIABLES)
