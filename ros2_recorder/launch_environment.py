"""ROS and DDS overrides for starting a driver or another ROS process."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex

from .communication import PROFILE_VARIABLES


BUILTIN_TRANSPORTS = ('DEFAULT', 'DEFAULTv6', 'UDPv4', 'UDPv6', 'SHM', 'LARGE_DATA', 'NONE')
DISCOVERY_RANGES = ('SUBNET', 'LOCALHOST', 'OFF', 'SYSTEM_DEFAULT')
STARTUP_KEYS = (
    'RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY', *PROFILE_VARIABLES,
    'CYCLONEDDS_URI', 'FASTDDS_BUILTIN_TRANSPORTS', 'ROS_DISCOVERY_SERVER',
    'ROS_AUTOMATIC_DISCOVERY_RANGE', 'ROS_STATIC_PEERS',
)


def absolute_path(value, label):
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise ValueError(f'{label}请填写绝对路径。') from exc
    if not path.is_absolute():
        raise ValueError(f'{label}请填写绝对路径。')
    return str(path)


@dataclass(frozen=True)
class LaunchEnvironment:
    setup: str = ''
    generate_fastdds: bool = True
    variables: dict = field(default_factory=dict)

    def __post_init__(self):
        if (not isinstance(self.setup, str) or type(self.generate_fastdds) is not bool
                or not isinstance(self.variables, dict) or self.variables.keys() - set(STARTUP_KEYS)):
            raise ValueError('启动环境配置格式无效。')
        if any(not isinstance(value, str) or any(ch in value for ch in '\r\n\0')
               for value in (self.setup, *self.variables.values())):
            raise ValueError('启动参数必须是单行文本。')
        values = {key: self.variables[key].strip() for key in STARTUP_KEYS if self.variables.get(key, '').strip()}
        setup = absolute_path(self.setup.strip(), 'ROS 基础环境') if self.setup.strip() else ''
        if 'ROS_DOMAIN_ID' in values:
            domain = values['ROS_DOMAIN_ID']
            if not re.fullmatch(r'[0-9]{1,3}', domain) or int(domain) > 232:
                raise ValueError('ROS Domain ID 必须是 0～232 的整数。')
            values['ROS_DOMAIN_ID'] = str(int(domain))
        for key, options in (('ROS_LOCALHOST_ONLY', ('0', '1')),
                             ('FASTDDS_BUILTIN_TRANSPORTS', BUILTIN_TRANSPORTS),
                             ('ROS_AUTOMATIC_DISCOVERY_RANGE', DISCOVERY_RANGES)):
            if key in values and values[key] not in options:
                raise ValueError(f'{key} 的选项无效。')
        rmw = values.get('RMW_IMPLEMENTATION', '')
        if rmw and not re.fullmatch(r'rmw_[a-zA-Z0-9_]+', rmw):
            raise ValueError('RMW_IMPLEMENTATION 请填写有效的中间件包名。')
        if self.generate_fastdds:
            if rmw and rmw not in ('rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp'):
                raise ValueError('生成的 XML 适用于 Fast DDS。请选择“手动指定或沿用”来使用其他中间件。')
            for key in (*PROFILE_VARIABLES, 'FASTDDS_BUILTIN_TRANSPORTS'):
                values.pop(key, None)
        else:
            for key in PROFILE_VARIABLES:
                if key in values:
                    values[key] = absolute_path(values[key], key)
        object.__setattr__(self, 'setup', setup)
        object.__setattr__(self, 'variables', values)

    def assignments(self, generated_path=None):
        values = dict(self.variables)
        if self.generate_fastdds:
            if generated_path is None:
                raise ValueError('请先保存生成的 Fast DDS XML。')
            values.update(dict.fromkeys(PROFILE_VARIABLES, str(generated_path)))
        return values

    def shell_command(self, generated_path=None):
        values = self.assignments(generated_path)
        command = ['source ' + shlex.quote(self.setup)] if self.setup else []
        if values:
            command.append('export ' + ' '.join(f'{key}={shlex.quote(value)}' for key, value in values.items()))
        if not command:
            raise ValueError('请至少设置一项启动参数。')
        return ' && '.join(command)
