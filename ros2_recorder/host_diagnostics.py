"""Read-only host and local ROS process diagnostics for Linux."""
from collections import Counter
from datetime import datetime
import os
from pathlib import Path
import platform
import re
import subprocess
from xml.etree import ElementTree as ET

from .communication import PROFILE_VARIABLE, host_limits


UNKNOWN = '未读取到'
PROFILE_KEYS = ('FASTDDS_DEFAULT_PROFILES_FILE', PROFILE_VARIABLE, 'CYCLONEDDS_URI')
DISCOVERY_KEYS = ('FASTDDS_BUILTIN_TRANSPORTS', 'ROS_DISCOVERY_SERVER',
                  'ROS_AUTOMATIC_DISCOVERY_RANGE', 'ROS_STATIC_PEERS')
SCHEDULERS = {0: '普通', 1: 'FIFO', 2: 'RR', 3: 'Batch', 5: 'Idle', 6: 'Deadline'}
SHM_FILE = re.compile(r'(/dev/shm/fastrtps_[0-9a-fA-F]+)(?: \(deleted\))?$')
DDS_LIBRARY = re.compile(r'/(lib(?:fastrtps|fastdds|ddsc|nddsc|connextdds)[^ /]*\.so(?:\.[^ /]+)?)(?: \(deleted\))?$', re.M)


def read_text(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return None


def size_text(value):
    if value is None:
        return UNKNOWN
    if value >= 1024 ** 2:
        return f'{value / 1024 ** 2:g} MiB'
    return f'{value / 1024:g} KiB'


def dds_profile(path):
    """Describe transport descriptors in the file, separate from observed mappings."""
    if not path.is_file():
        return 'XML 文件不存在或不可读。'
    try:
        with path.open('rb') as stream:
            raw = stream.read(1024 ** 2 + 1)
        if len(raw) > 1024 ** 2:
            return 'XML 超过 1 MiB，未展开。'
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError, ValueError) as exc:
        return f'无法读取 XML · {exc}'
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    lines = []
    for descriptor in root.iter('transport_descriptor'):
        name = descriptor.findtext('transport_id', '未命名')
        kind = descriptor.findtext('type', '未指定')
        options = []
        for label, key in (('共享内存段', 'segment_size'), ('接收缓冲请求', 'receiveBufferSize'),
                           ('发送缓冲请求', 'sendBufferSize')):
            value = descriptor.findtext(key, '').strip()
            if value:
                options.append(f'{label} {size_text(int(value)) if value.isdecimal() and len(value) <= 12 else value}')
        lines.append(f'{name} · {kind}' + (' · ' + ' · '.join(options) if options else ''))
    for participant in root.iter('participant'):
        if participant.get('is_default_profile', '').lower() == 'true':
            ids = ', '.join(item.text or '' for item in participant.findall('./rtps/userTransports/transport_id'))
            builtin = participant.findtext('./rtps/useBuiltinTransports', '默认')
            lines.append(f'默认参与者 {participant.get("profile_name", "")} · 内建传输 {builtin}' +
                         (f' · 自定义传输 {ids}' if ids else ''))
    return '\n'.join(lines) or '未找到显式传输描述符。'


def udp_errors():
    raw = read_text('/proc/net/snmp') or ''
    udp = [line.split()[1:] for line in raw.splitlines() if line.startswith('Udp:')]
    return dict(zip(*udp)) if len(udp) == 2 else {}


def local_ros_processes():
    programs = []
    clocks = Counter()
    uid = os.getuid()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        name = read_text(proc / 'comm')
        if name in {'ptp4l', 'phc2sys', 'chronyd', 'systemd-timesyn'}:
            clocks[name] += 1
        try:
            if proc.stat().st_uid != uid:
                continue
        except OSError:
            continue
        maps = read_text(proc / 'maps') or ''
        libraries = sorted(set(DDS_LIBRARY.findall(maps)))
        if not libraries:
            continue
        try:
            values = (proc / 'environ').read_bytes().decode(errors='replace').split('\0')
        except OSError:
            values = []
        keys = {*PROFILE_KEYS, *DISCOVERY_KEYS, 'ROS_DISTRO', 'RMW_IMPLEMENTATION', 'ROS_DOMAIN_ID', 'ROS_LOCALHOST_ONLY'}
        env = {key: value for entry in values for key, _, value in [entry.partition('=')] if key in keys}
        sizes = Counter()
        for path in {match.group(1) for line in maps.splitlines() if (match := SHM_FILE.search(line))}:
            try:
                sizes[(proc / 'root' / path.lstrip('/')).stat().st_size] += 1
            except OSError:
                pass
        status = read_text(proc / 'status') or ''
        cpus = next((line.split(':', 1)[1].strip() for line in status.splitlines()
                     if line.startswith('Cpus_allowed_list:')), UNKNOWN)
        policies = Counter()
        try:
            for thread in (proc / 'task').iterdir():
                try:
                    policies[os.sched_getscheduler(int(thread.name))] += 1
                except OSError:
                    continue
        except OSError:
            pass
        programs.append((int(proc.name), name or UNKNOWN, libraries, env, sizes, cpus, policies))
    return sorted(programs), clocks


def process_report(programs):
    if not programs:
        return '未发现当前账户可读取且已加载已知 DDS 库的进程。'
    lines = ['范围为当前账户可读取的本机进程。共享内存映射可能含其他参与者，大小含文件管理开销。']
    for pid, name, libraries, env, sizes, cpus, policies in programs:
        scheduling = ', '.join(f'{SCHEDULERS.get(policy, policy)} × {count}'
                               for policy, count in sorted(policies.items()))
        mappings = ', '.join(f'{size_text(size)} × {count}' for size, count in sorted(sizes.items()))
        lines.extend((
            f'\n{name} · PID {pid}',
            '  已加载中间件库 · ' + ', '.join(libraries),
            f'  启动环境 · ROS {env.get("ROS_DISTRO") or UNKNOWN} · Domain {env.get("ROS_DOMAIN_ID") or "未显式设置"}'
            f' · 仅本机 {env.get("ROS_LOCALHOST_ONLY") or "未显式设置"}'
            f' · RMW {env.get("RMW_IMPLEMENTATION") or "未显式设置"}',
            f'  共享内存映射 · {mappings or "未发现可读的 Fast DDS 段"}',
            f'  调度 · {scheduling or UNKNOWN} · 主线程可用 CPU {cpus}',
        ))
        for key in (*PROFILE_KEYS, *DISCOVERY_KEYS):
            if env.get(key):
                lines.append(f'  {key} · {env[key]}')
    return '\n'.join(lines)


def host_report(env):
    programs, clocks = local_ros_processes()
    shm_mib, _ = host_limits()
    kernel = platform.release()
    realtime = read_text('/sys/kernel/realtime')
    try:
        synced = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                                capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        synced = ''
    lines = [
        f'检查时间 · {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}',
        '',
        'ROS 2 与中间件启动环境',
        f'  ROS 版本 · {env.get("ROS_DISTRO") or "未加载 ROS 环境"}',
        f'  RMW_IMPLEMENTATION · {env.get("RMW_IMPLEMENTATION") or "未显式指定，按 ROS 安装默认选择"}',
        f'  ROS_DOMAIN_ID · {env.get("ROS_DOMAIN_ID") or "0（默认）"}',
        f'  ROS_LOCALHOST_ONLY · {env.get("ROS_LOCALHOST_ONLY") or "0（默认）"}',
    ]
    files = set()
    for key in PROFILE_KEYS:
        value = env.get(key)
        lines.append(f'  {key} · {value or "未设置"}')
        if value and key != 'CYCLONEDDS_URI' and value not in files:
            files.add(value)
            lines.extend('    ' + line for line in dds_profile(Path(value).expanduser()).splitlines())
    for key in DISCOVERY_KEYS:
        lines.append(f'  {key} · {env.get(key) or "未设置"}')
    lines.extend(['', 'Linux 主机',
                  f'  内核 · {kernel} · 实时内核标志 {realtime if realtime is not None else UNKNOWN}',
                  f'  隔离 CPU · {read_text("/sys/devices/system/cpu/isolated") or "无或未读取到"}',
                  f'  /dev/shm 可用 · {shm_mib if shm_mib is not None else UNKNOWN} MiB'])
    for key in ('rmem_default', 'rmem_max', 'wmem_default', 'wmem_max'):
        value = read_text('/proc/sys/net/core/' + key) or ''
        lines.append(f'  net.core.{key} · {size_text(int(value) if value.isdecimal() else None)}')
    errors = udp_errors()
    lines.extend(['  UDP 接收缓冲丢包累计 · ' + errors.get('RcvbufErrors', UNKNOWN),
                  '  UDP 发送缓冲错误累计 · ' + errors.get('SndbufErrors', UNKNOWN),
                  '', '本机时间与网口',
                  '  系统时钟同步状态 · ' + {'yes': '已同步', 'no': '未同步'}.get(synced, UNKNOWN),
                  '  授时相关进程 · ' + (', '.join(f'{name} × {count}' for name, count in sorted(clocks.items())) or '未检测到'),
                  '  授时进程存在仅代表进程在运行，设备端锁定状态和偏差需单独验证。'])
    net = Path('/sys/class/net')
    if net.is_dir():
        for nic in sorted(net.iterdir()):
            state = read_text(nic / 'operstate')
            if nic.name == 'lo' or state == 'down':
                continue
            speed = read_text(nic / 'speed') or ''
            mtu = read_text(nic / 'mtu') or UNKNOWN
            drops = read_text(nic / 'statistics/rx_dropped') or UNKNOWN
            missed = read_text(nic / 'statistics/rx_missed_errors') or UNKNOWN
            rx_errors = read_text(nic / 'statistics/rx_errors') or UNKNOWN
            lines.append(f'  {nic.name} · {state or UNKNOWN} · {speed + " Mb/s" if speed.isdecimal() else "速率未知"}'
                         f' · MTU {mtu} · RX 丢弃 {drops} / 未及时接收 {missed} / 错误 {rx_errors}（累计）')
    return '\n'.join(lines), process_report(programs)
