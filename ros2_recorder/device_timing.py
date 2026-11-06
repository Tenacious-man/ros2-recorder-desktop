"""LinuxPTP grandmaster instances and per-interface verification."""

from dataclasses import dataclass
from ipaddress import IPv4Address
import os
from pathlib import Path
import re
import subprocess
from typing import Optional

from .host_diagnostics import read_text


NET = Path('/sys/class/net')
TOOLS = {
    'ptp4l': '/usr/sbin/ptp4l', 'phc2sys': '/usr/sbin/phc2sys', 'pmc': '/usr/sbin/pmc',
    'ip': '/usr/sbin/ip', 'flock': '/usr/bin/flock', 'ethtool': '/usr/sbin/ethtool',
    'pkexec': '/usr/bin/pkexec',
}
HARDWARE_FLAGS = {'hardware-transmit', 'hardware-receive', 'hardware-raw-clock'}
INTERFACE_PATTERN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}')

# systemd supervises the whole process group. Either timing process exiting fails the unit.
# The script is fixed and parameters are passed as positional arguments, never as shell code.
SUPERVISE = r'''
nic=$1
socket=$2/ptp4l
domain=$3
transport=$4
clock_mode=$5
clock=$(/usr/sbin/ethtool -T "$nic" | /usr/bin/awk '/^PTP Hardware Clock: [0-9]+$/ { print $4 }')
[[ $clock =~ ^[0-9]+$ ]] || { printf 'No hardware clock for %s\n' "$nic"; exit 1; }
device=/dev/ptp$clock
[[ -c $device ]] || { printf 'PHC device missing: %s\n' "$device"; exit 1; }
exec 9<"$device" || exit 1
/usr/bin/flock -n 9 || { printf 'PHC already in use: %s\n' "$device"; exit 1; }
/usr/sbin/ip link set dev "$nic" up || exit 1
if [[ $transport == UDPv4 && -z $(/usr/sbin/ip -4 -o addr show dev "$nic") ]]; then
    printf 'No IPv4 address on %s\n' "$nic"
    exit 1
fi
config_args=()
if [[ -f /etc/ros2-recorder/ptp/$nic.conf ]]; then
    config_args=(-f "/etc/ros2-recorder/ptp/$nic.conf")
fi
/usr/sbin/ptp4l "${config_args[@]}" -i "$nic" -H -E -m -q --masterOnly=1 --domainNumber="$domain" \
    --network_transport="$transport" --uds_address="$socket" --tx_timestamp_timeout=50 &
ptp=$!
if [[ $clock_mode == utc ]]; then
    clock_args=(-O 0)
else
    clock_args=(-w)
fi
/usr/sbin/phc2sys -s CLOCK_REALTIME -c "$nic" -n "$domain" -z "$socket" "${clock_args[@]}" -m -q &
phc=$!
(
    while /usr/bin/sleep 3; do
        [[ -S $socket ]] || continue
        stats=$(/usr/bin/timeout 2 /usr/sbin/pmc -u -s "$socket" -i "$2/pmc" -b 0 -d "$domain" \
            'GET PORT_PROPERTIES_NP' 'GET PORT_STATS_NP' 2>/dev/null |
            /usr/bin/awk '$1 == "portState" || $1 == "tx_Sync" || $1 == "rx_Delay_Req" {printf "%s=%s ", $1, $2}')
        [[ -z $stats ]] || printf 'recorder_ptp %s\n' "$stats"
    done
) &
wait -n "$ptp" "$phc"
exit 1
'''

# pkexec runs only this fixed installer, with validated data as separate arguments.
# Neither systemd nor the installer executes files from the user's workspace.
INSTALL_SERVICE = 'SUPERVISE = ' + repr(SUPERVISE) + '\n' + r'''
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

nic, domain, transport, mode = sys.argv[1:]
if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}', nic)
        or not re.fullmatch(r'[0-9]{1,3}', domain) or int(domain) > 127
        or transport not in ('UDPv4', 'L2') or mode not in ('ptp', 'utc')):
    sys.exit('Invalid PTP service configuration')

def systemctl(*args, check=True):
    return subprocess.run(('/usr/bin/systemctl', '--no-pager', *args), check=check)

def write_if_changed(path, content, mode):
    if path.is_file() and path.read_text() == content:
        return False
    fd, name = tempfile.mkstemp(prefix='.ros-recorder-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return True

lock = os.open('/run/ros-recorder-timing-admin.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX)
directory = Path('/etc/ros2-recorder/ptp')
directory.mkdir(parents=True, exist_ok=True)
runner = directory / 'runner-v1'
runner_changed = write_if_changed(runner, '#!/bin/bash\n' + SUPERVISE, 0o755)
runtime = 'ros-recorder-timing-' + nic
unit = runtime + '.service'
content = f"""[Unit]
Description=ROS Recorder PTP grandmaster on {nic}
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=exec
RuntimeDirectory={runtime}
Environment=LC_ALL=C
ExecStart={runner} {nic} /run/{runtime} {domain} {transport} {mode}
Restart=on-failure
RestartSec=10s
TimeoutStopSec=5s
KillMode=control-group

[Install]
WantedBy=multi-user.target
"""
changed = write_if_changed(Path('/etc/systemd/system') / unit, content, 0o644)
systemctl('enable', '--', unit)
previous = f'ros-recorder-ptp-{nic}.service'
if systemctl('is-active', '--quiet', '--', previous, check=False).returncode == 0:
    systemctl('stop', '--', previous)
systemctl('restart' if changed or runner_changed else 'start', '--', unit)
'''


@dataclass(frozen=True)
class TimingProfile:
    domain: int = 0
    transport: str = 'UDPv4'
    clock_mode: str = 'ptp'
    falcon_ip: str = ''

    def __post_init__(self):
        if not 0 <= self.domain <= 127 or self.transport not in ('UDPv4', 'L2'):
            raise ValueError('PTP 域需为 0–127，传输方式需为 UDPv4 或 L2。')
        if self.clock_mode not in ('ptp', 'utc'):
            raise ValueError('未知的 PTP 时标。')
        if self.falcon_ip:
            IPv4Address(self.falcon_ip)


@dataclass(frozen=True)
class ServiceState:
    loaded: bool = False
    state: str = ''
    invocation: str = ''
    enabled: bool = False
    substate: str = ''


@dataclass(frozen=True)
class PortState:
    name: str
    phc: Optional[int]
    carrier: str
    managed: ServiceState
    legacy: ServiceState
    legacy_phc: ServiceState
    occupied: bool = False
    role: str = ''
    offset: Optional[int] = None
    servo: str = ''
    counters: Optional[tuple] = None
    device: str = ''
    error: str = ''
    saved_profile: Optional[TimingProfile] = None

    @property
    def active(self):
        return self.managed.state == 'active' or self.legacy.state == 'active'

    @property
    def stoppable(self):
        states = (self.managed.state, self.legacy.state, self.legacy_phc.state)
        return self.autostart or any(state in ('active', 'activating', 'failed') for state in states)

    @property
    def autostart(self):
        return self.managed.enabled or self.legacy.enabled or self.legacy_phc.enabled


def _command(*args, timeout=3):
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
        timeout=timeout, env={**os.environ, 'LC_ALL': 'C'},
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'{args[0]} 返回 {result.returncode}')
    return result.stdout.strip()


def _read_command(*args):
    try:
        return _command(*args)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return ''


def _units(name):
    return (f'ros-recorder-timing-{name}.service', f'ptp4l-{name}.service', f'phc2sys-{name}.service')


def _temporary_unit(name):
    return f'ros-recorder-ptp-{name}.service'


def _saved_profile(name):
    unit = _units(name)[0]
    content = read_text(Path('/etc/systemd/system') / unit) or ''
    args = re.search(r'^ExecStart=/etc/ros2-recorder/ptp/runner-v1 \S+ /run/\S+ (\d+) (UDPv4|L2) (ptp|utc)$',
                     content, re.M)
    if args and int(args[1]) <= 127:
        return TimingProfile(int(args[1]), args[2], args[3])
    return None


def _service_states(units):
    if not units:
        return {}
    output = _command(
        'systemctl', 'show', '--no-pager', '-p', 'Id', '-p', 'LoadState',
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'InvocationID', '-p', 'UnitFileState', '--', *units,
    )
    states = {}
    for block in output.split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        states[fields.get('Id')] = ServiceState(
            fields.get('LoadState') == 'loaded', fields.get('ActiveState', ''),
            fields.get('InvocationID', ''),
            fields.get('UnitFileState') == 'enabled',
            fields.get('SubState', ''),
        )
    return states


def _journal(unit, state, *options):
    if state.state != 'active' or not re.fullmatch(r'[a-f0-9]{32}', state.invocation):
        return ''
    return _read_command(
        'journalctl', '-b', '-u', unit, '--no-pager', '-o', 'cat', '-n', '100',
        *options, f'_SYSTEMD_INVOCATION_ID={state.invocation}',
    )


def _phc(nic):
    info = _read_command(TOOLS['ethtool'], '-T', nic)
    clock = re.search(r'^PTP Hardware Clock: (\d+)$', info, re.M)
    if clock and HARDWARE_FLAGS <= set(info.split()):
        return int(clock[1])
    return None


def _interfaces():
    return {nic.name: _phc(nic.name) for nic in sorted(NET.iterdir())
            if INTERFACE_PATTERN.fullmatch(nic.name) and read_text(nic / 'type') == '1'}


def _managed_ports():
    output = _read_command('systemctl', 'list-unit-files', '--no-legend', '--no-pager',
                           'ros-recorder-timing-*.service', 'ros-recorder-ptp-*.service')
    return set(re.findall(r'^ros-recorder-(?:timing|ptp)-([A-Za-z0-9_][A-Za-z0-9_.-]{0,14})\.service\s', output, re.M))


def _known_units(names):
    return [unit for name in names for unit in (*_units(name), _temporary_unit(name))]


def _clock_users(interfaces):
    """Find live LinuxPTP processes and the PHCs or NICs they may be using."""
    users = []
    for path in Path('/proc').glob('[0-9]*/comm'):
        program = read_text(path)
        if program not in ('ptp4l', 'phc2sys', 'ts2phc'):
            continue
        raw = read_text(path.with_name('cmdline'))
        if not raw:
            if path.exists():
                users.append((set(), set(), set()))
            continue
        args = raw.split('\0')[1:]
        clocks = set()
        nics = set()
        for value in args:
            if value in interfaces:
                nics.add(value)
                clocks.add(interfaces[value])
            match = re.fullmatch(r'(?:/dev/)?ptp(\d+)', value)
            if match:
                clocks.add(int(match[1]))
        # A process with no visible binding may be configured through a file or auto mode.
        groups = read_text(path.with_name('cgroup')) or ''
        services = {line.rsplit('/', 1)[-1] for line in groups.splitlines()}
        users.append((nics, clocks - {None}, services))
    return users


def _occupied(name, phc, users, allowed_unit=''):
    return any(allowed_unit not in services and (not (nics or clocks) or name in nics or phc in clocks)
               for nics, clocks, services in users)


def _device_status(name, profile):
    if not profile.falcon_ip:
        return '未设置设备查询'
    status = _read_command(
        '/usr/bin/curl', '-q', '--noproxy', '*', '--interface', f'if!{name}',
        '--fail', '--silent', '--max-time', '2', '--max-filesize', '256',
        f'http://{profile.falcon_ip}:8010/command/?get_time_sync_type',
    )
    if status == '7':
        return '设备报告 PTP 已锁定'
    if status == '8':
        return '设备报告 PTP 未锁定'
    return '设备无响应或返回未知状态'


def _measure(name, units, managed, legacy, legacy_phc):
    broadcast = _journal(units[0], managed) if managed.state == 'active' else _journal(units[1], legacy)
    clock = _journal(units[0], managed, '--since=-15s') if managed.state == 'active' else _journal(
        units[2], legacy_phc, '--since=-15s')
    transitions = re.findall(r'port 1(?: \([^)]+\))?: [A-Z_]+ to ([A-Z_]+) on ', broadcast)
    samples = re.findall(r'recorder_ptp ([^\n]+)', clock)
    stats = dict(re.findall(r'(\w+)=(\w+)', samples[-1])) if samples else {}
    counters = None
    if stats.get('tx_Sync', '').isdigit() and stats.get('rx_Delay_Req', '').isdigit():
        counters = int(stats['tx_Sync']), int(stats['rx_Delay_Req'])
    offsets = re.findall(rf'(?<!\S){re.escape(name)}\s+sys offset\s+([+-]?\d+)\s+s(\d+)\b', clock)
    offset, servo = (int(offsets[-1][0]), offsets[-1][1]) if offsets else (None, '')
    return stats.get('portState', transitions[-1] if transitions else ''), offset, servo, counters


def timing_snapshot(profiles):
    if not os.access(TOOLS['ethtool'], os.X_OK):
        raise RuntimeError('请先安装 ethtool，以识别网卡硬件时间戳与 PHC。')
    interfaces = _interfaces()
    names = set(interfaces) | _managed_ports()
    states = _service_states(_known_units(sorted(names)))
    users = _clock_users(interfaces)
    ports = []
    for name in sorted(names):
        phc = interfaces.get(name)
        units = _units(name)
        managed, legacy, legacy_phc = (states.get(unit, ServiceState()) for unit in units)
        temporary = states.get(_temporary_unit(name), ServiceState())
        if not managed.loaded and temporary.loaded:
            managed = temporary
            units = (_temporary_unit(name), *units[1:])
        if name not in interfaces and not any(state.loaded for state in (managed, legacy, legacy_phc)):
            continue
        carrier = read_text(NET / name / 'carrier') or ''
        role, offset, servo, counters = _measure(name, units, managed, legacy, legacy_phc)
        device = _device_status(name, profiles.get(name, TimingProfile())) if carrier == '1' else '链路未连通'
        errors = ''
        failed_unit = next((unit for unit, state in zip(units, (managed, legacy, legacy_phc))
                            if state.state == 'failed' or state.substate == 'auto-restart'), '')
        if failed_unit:
            errors = _read_command('journalctl', '-b', '-u', failed_unit, '-n', '8', '--no-pager', '-o', 'cat')
        ports.append(PortState(name, phc, carrier, managed, legacy, legacy_phc,
                               _occupied(name, phc, users, units[2]), role, offset, servo, counters, device, errors,
                               _saved_profile(name)))
    synced = _read_command('timedatectl', 'show', '-p', 'NTPSynchronized', '--value')
    return synced, ports


def _start(name, profile):
    missing = [tool for tool, path in TOOLS.items() if not os.access(path, os.X_OK)]
    if missing:
        raise RuntimeError('缺少授时工具：' + '、'.join(missing) + '。请安装 linuxptp 和 ethtool。')
    if profile.transport == 'UDPv4' and not _command(TOOLS['ip'], '-4', '-o', 'addr', 'show', 'dev', name):
        raise ValueError('网口没有 IPv4 地址。请先配置 IPv4，或选择设备支持的 L2 传输。')
    _command(
        TOOLS['pkexec'], '/usr/bin/python3', '-I', '-c', INSTALL_SERVICE,
        name, str(profile.domain), profile.transport, profile.clock_mode,
        timeout=90,
    )


def change_ports(enabled, profiles):
    """Apply each requested port independently and return per-port results."""
    if any(not INTERFACE_PATTERN.fullmatch(name) for name in profiles):
        raise ValueError('无效的网口名称。')
    interfaces = _interfaces()
    states = _service_states(_known_units(sorted(interfaces.keys() | profiles.keys())))
    users = _clock_users(interfaces)
    reserved = {}
    for name, phc in interfaces.items():
        if phc is not None and any(states.get(unit, ServiceState()).enabled for unit in _units(name)):
            reserved.setdefault(phc, set()).add(name)
    claimed = set()
    results = []
    for name, profile in profiles.items():
        units = _units(name)
        managed, legacy, legacy_phc = (states.get(unit, ServiceState()) for unit in units)
        previous = _temporary_unit(name)
        phc = interfaces.get(name)
        try:
            if not enabled:
                installed = [unit for unit, state in zip(units, (managed, legacy, legacy_phc)) if state.loaded]
                if installed:
                    _command('systemctl', '--no-pager', 'disable', '--now', '--', *installed, timeout=90)
                if states.get(previous, ServiceState()).loaded:
                    _command('systemctl', '--no-pager', 'stop', '--', previous, timeout=90)
                results.append(f'{name}：已停止授时并关闭开机启动。')
                continue
            if phc is None:
                raise ValueError('网卡缺少硬件收发时间戳或 PHC。')
            if phc in claimed:
                raise ValueError('已选的另一个网口使用同一硬件时钟。')
            if reserved.get(phc, set()) - {name}:
                raise ValueError('另一个使用同一硬件时钟的网口已设为开机启动。')
            reuse_legacy = not managed.loaded and legacy.loaded and legacy_phc.loaded
            owned = set(units[1:] if reuse_legacy else (units[0], previous))
            if _occupied(name, phc, [user for user in users if not user[2] & owned]):
                raise ValueError('检测到已有 LinuxPTP 进程可能占用该网口或时钟。')
            if reuse_legacy:
                _command('systemctl', '--no-pager', 'enable', '--now', '--', units[1], units[2], timeout=90)
            else:
                _start(name, profile)
            claimed.add(phc)
            results.append(f'{name}：开机配置已保存，等待授时状态验证。')
        except subprocess.TimeoutExpired:
            results.append(f'{name}：系统操作超时，请刷新核对。')
        except (OSError, RuntimeError, ValueError) as exc:
            results.append(f'{name}：操作失败 · {exc}')
    return '\n'.join(results)
