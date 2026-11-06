import subprocess
import sys

from ros2_recorder import device_timing as timing


def test_start_unconfigured_ports_and_preserve_legacy_clock(monkeypatch):
    legacy_clock = timing._units('legacy')[2]
    monkeypatch.setattr(timing, '_interfaces', lambda: {'eth1': 1, 'eth2': 2, 'shared': 1, 'legacy': 0})
    monkeypatch.setattr(timing, '_service_states', lambda _units: {
        timing._units('legacy')[1]: timing.ServiceState(True, 'inactive'),
        legacy_clock: timing.ServiceState(True, 'active'),
    })
    monkeypatch.setattr(timing, '_clock_users', lambda _interfaces: [({'legacy'}, {0}, {legacy_clock})])
    started, commands = [], []
    monkeypatch.setattr(timing, '_start', lambda name, profile: started.append((name, profile)))
    monkeypatch.setattr(timing, '_command', lambda *args, **_kwargs: commands.append(args))
    standard = timing.TimingProfile()
    l2 = timing.TimingProfile(4, 'L2')
    result = timing.change_ports(True, {'eth1': standard, 'shared': standard, 'eth2': l2, 'legacy': standard})
    assert started == [('eth1', standard), ('eth2', l2)]
    assert commands[-1][-3:] == ('--', *timing._units('legacy')[1:])
    assert 'shared：操作失败' in result
    assert result.count('开机配置已保存') == 3


def test_persistent_unit_retains_per_port_config_and_reuses_existing_service(tmp_path):
    unit_dir = tmp_path / 'system'
    unit_dir.mkdir()
    config_dir = tmp_path / 'config'
    audit = tmp_path / 'commands'
    systemctl = tmp_path / 'systemctl'
    systemctl.write_text(f'#!/bin/sh\necho "$*" >> "{audit}"\n[ "$2" != is-active ]\n')
    systemctl.chmod(0o755)
    installer = timing.INSTALL_SERVICE.replace('/etc/systemd/system', str(unit_dir))
    installer = installer.replace('/etc/ros2-recorder/ptp', str(config_dir))
    installer = installer.replace('/run/ros-recorder-timing-admin.lock', str(tmp_path / 'lock'))
    installer = installer.replace('/usr/bin/systemctl', str(systemctl))

    def apply(name, domain, transport):
        subprocess.run([sys.executable, '-I', '-c', installer, name, domain, transport, 'ptp'], check=True)

    apply('eth2', '12', 'UDPv4')
    apply('eth3', '13', 'L2')
    apply('eth2', '12', 'UDPv4')
    first = unit_dir / 'ros-recorder-timing-eth2.service'
    second = unit_dir / 'ros-recorder-timing-eth3.service'
    assert 'eth2 /run/ros-recorder-timing-eth2 12 UDPv4 ptp' in first.read_text()
    assert 'eth3 /run/ros-recorder-timing-eth3 13 L2 ptp' in second.read_text()
    assert 'WantedBy=multi-user.target' in first.read_text()
    assert 'Restart=on-failure' in first.read_text()
    assert (config_dir / 'runner-v1').stat().st_mode & 0o111
    commands = audit.read_text()
    assert commands.count('enable -- ros-recorder-timing-eth2.service') == 2
    assert commands.count('restart -- ros-recorder-timing-eth2.service') == 1
    assert commands.splitlines()[-1].endswith('start -- ros-recorder-timing-eth2.service')
    apply('eth2', '14', 'L2')
    assert '14 L2 ptp' in first.read_text()
    assert audit.read_text().splitlines()[-1].endswith('restart -- ros-recorder-timing-eth2.service')


def test_stop_also_disables_autostart(monkeypatch):
    unit = timing._units('eth2')[0]
    calls = []
    monkeypatch.setattr(timing, '_interfaces', lambda: {'eth2': 2})
    monkeypatch.setattr(timing, '_clock_users', lambda _: [])
    monkeypatch.setattr(timing, '_service_states', lambda _: {unit: timing.ServiceState(True, 'inactive', enabled=True)})
    monkeypatch.setattr(timing, '_command', lambda *args, **_: calls.append(args))
    timing.change_ports(False, {'eth2': timing.TimingProfile()})
    assert calls == [('systemctl', '--no-pager', 'disable', '--now', '--', unit)]


def test_measurement_uses_recent_offsets_and_packet_counts(monkeypatch):
    journal = '''
ptp4l[412.220]: port 1 (eth2): LISTENING to MASTER on RS_MASTER
phc2sys[412.227]: eth2 sys offset -30 s2 freq +42 delay 3542
recorder_ptp portState=MASTER tx_Sync=19 rx_Delay_Req=7
'''
    monkeypatch.setattr(timing, '_journal', lambda *_: journal)
    active = timing.ServiceState(True, 'active')
    missing = timing.ServiceState()
    assert timing._measure('eth2', timing._units('eth2'), active, missing, missing) == (
        'MASTER', -30, '2', (19, 7))


def test_falcon_lock_is_queried_on_selected_interface(monkeypatch):
    calls = []
    monkeypatch.setattr(timing, '_read_command', lambda *args: calls.append(args) or '7')
    assert timing._device_status('eth2', timing.TimingProfile(falcon_ip='192.168.2.10')) == '设备报告 PTP 已锁定'
    assert 'if!eth2' in calls[0]
    assert calls[0][-1] == 'http://192.168.2.10:8010/command/?get_time_sync_type'
