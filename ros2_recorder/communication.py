"""Settings for the analysis subscriber and exportable Fast DDS profiles."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
from xml.etree import ElementTree as ET


TRANSPORTS = {
    'environment': '沿用消息环境',
    'shm': '同机大点云（共享内存 + UDP）',
    'udp': '仅 UDP（排查用）',
}
RELIABILITY = {'best_effort': '优先最新（Best Effort）', 'reliable': '允许重传（Reliable）'}
PROFILE_VARIABLE = 'FASTRTPS_DEFAULT_PROFILES_FILE'
PROFILE_VARIABLES = (PROFILE_VARIABLE, 'FASTDDS_DEFAULT_PROFILES_FILE')
DEFAULT_SHM_MIB = 32


def profile_shell_command(path):
    value = shlex.quote(str(path))
    return 'export ' + ' '.join(f'{key}={value}' for key in PROFILE_VARIABLES)


@dataclass(frozen=True)
class CommunicationSettings:
    reliability: str = 'best_effort'
    depth: int = 5
    transport: str = 'environment'
    shm_mib: int = DEFAULT_SHM_MIB
    udp_kib: int = 0
    duration_s: int = 0
    note: str = ''

    def __post_init__(self):
        if self.reliability not in RELIABILITY or self.transport not in TRANSPORTS:
            raise ValueError('监听方式或传输方式无效。')
        for value, minimum, maximum in ((self.depth, 1, 100), (self.shm_mib, 1, 256),
                                        (self.udp_kib, 0, 65536), (self.duration_s, 0, 3600)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError('通信设置数值超出范围。')
        if not isinstance(self.note, str) or len(self.note) > 120:
            raise ValueError('对照备注最多 120 个字符。')

    @classmethod
    def load(cls, path):
        try:
            return cls(**json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError):
            return cls()

    def fastdds_xml(self):
        if self.transport == 'environment':
            raise ValueError('请先选择需要导出的传输方式。')
        root = ET.Element('profiles', xmlns='http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles')
        descriptors = ET.SubElement(root, 'transport_descriptors')
        participant = ET.SubElement(root, 'participant', profile_name='recorder_transport', is_default_profile='true')
        rtps = ET.SubElement(participant, 'rtps')
        transports = ET.SubElement(rtps, 'userTransports')
        ET.SubElement(rtps, 'useBuiltinTransports').text = 'false'
        udp = {'receiveBufferSize': self.udp_kib * 1024} if self.udp_kib else {}
        profiles = [('recorder_udp', 'UDPv4', udp)]
        if self.transport == 'shm':
            profiles.insert(0, ('recorder_shm', 'SHM', {'segment_size': self.shm_mib * 1024**2}))
        for name, kind, options in profiles:
            descriptor = ET.SubElement(descriptors, 'transport_descriptor')
            ET.SubElement(descriptor, 'transport_id').text = name
            ET.SubElement(descriptor, 'type').text = kind
            for key, value in options.items():
                ET.SubElement(descriptor, key).text = str(value)
            ET.SubElement(transports, 'transport_id').text = name
        return ET.tostring(root, encoding='unicode')

    def monitor_environment(self, env, directory):
        if self.transport == 'environment':
            return env
        if env.get('RMW_IMPLEMENTATION') not in (None, '', 'rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp'):
            raise ValueError('自定义传输需要 Fast DDS，请在消息环境中选择对应的 ROS 2 中间件。')
        if env.get('ROS_LOCALHOST_ONLY') == '1':
            raise ValueError('当前启用了仅本机 ROS 通信。Fast DDS 可能额外启用默认共享内存，请选择“沿用消息环境”。')
        path = Path(directory) / 'fastdds.xml'
        path.write_text(self.fastdds_xml(), encoding='utf-8')
        return dict(env, RMW_IMPLEMENTATION=env.get('RMW_IMPLEMENTATION') or 'rmw_fastrtps_cpp',
                    **dict.fromkeys(PROFILE_VARIABLES, str(path)))


def host_limits():
    try:
        shm = os.statvfs('/dev/shm')
        shm_bytes = shm.f_bavail * shm.f_frsize
    except OSError:
        shm_bytes = None
    try:
        udp_bytes = int(Path('/proc/sys/net/core/rmem_max').read_text())
    except (OSError, ValueError):
        udp_bytes = None
    return shm_bytes, udp_bytes
