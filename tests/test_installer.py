from pathlib import Path
import hashlib
import json
import subprocess

import pytest

from ros2_recorder import installer
from ros2_recorder.environment import choose_underlay, saved_underlay


def arguments(*args):
    return installer.parser().parse_args(list(args))


def empty_env(tmp_path):
    return {'HOME': str(tmp_path), 'XDG_CONFIG_HOME': str(tmp_path / 'config')}


@pytest.mark.parametrize('codename,distro', [('focal', 'foxy'), ('jammy', 'humble'), ('noble', 'jazzy')])
def test_fresh_ros_is_opt_in_and_matches_ubuntu(tmp_path, codename, distro):
    with pytest.raises(installer.InstallError, match='--install-ros'):
        installer.select_ros(arguments(), {'codename': codename}, empty_env(tmp_path), [])
    selected, setup, fresh = installer.select_ros(arguments('--install-ros'), {'codename': codename}, empty_env(tmp_path), [])
    assert selected == distro and fresh and str(setup) == f'/opt/ros/{distro}/setup.bash'


def test_existing_ros_not_replaced_by_os_default(tmp_path):
    env = {**empty_env(tmp_path), 'ROS_DISTRO': 'custom', 'ROS_VERSION': '2', 'AMENT_PREFIX_PATH': '/custom'}
    distro, setup, fresh = installer.select_ros(arguments('--install-ros'), {'codename': 'noble'}, env, [])
    assert distro == 'custom' and not fresh


def test_loaded_source_install_can_be_remembered_for_menu_launch(tmp_path):
    prefix = tmp_path / 'source install'
    prefix.mkdir(); (prefix / 'setup.bash').touch()
    env = {**empty_env(tmp_path), 'ROS_VERSION': '2', 'ROS_DISTRO': 'custom', 'AMENT_PREFIX_PATH': str(prefix)}
    distro, setup, fresh = installer.select_ros(arguments(), {'codename': 'jammy'}, env, [])
    assert distro == 'custom' and setup == prefix / 'setup.bash' and not fresh


def test_multiple_versions_require_selection(tmp_path):
    choices = [tmp_path / 'one/setup.bash', tmp_path / 'two/setup.bash']
    with pytest.raises(installer.InstallError, match='多个'):
        installer.select_ros(arguments(), {'codename': 'jammy'}, empty_env(tmp_path), choices)
    distro, setup, fresh = installer.select_ros(arguments('--ros-distro', 'two'), {'codename': 'jammy'}, empty_env(tmp_path), choices)
    assert distro == 'two' and setup == choices[1] and not fresh


def test_distro_names_do_not_become_command_arguments():
    with pytest.raises(installer.InstallError):
        installer.validate_distro('jazzy; touch /tmp/bad')


def test_source_build_does_not_pull_another_ros_binary_install():
    packages = installer.dependency_packages('custom', source_build=True)
    assert 'python3-pyqt5' in packages
    assert not any(p.startswith('ros-custom-') for p in packages)


def test_existing_rosdep_variant_is_reused(monkeypatch):
    monkeypatch.setattr(installer, 'package_installed', lambda name: name == 'python3-rosdep2')
    monkeypatch.setattr(installer, 'package_available', lambda _: True)
    assert installer.rosdep_package() == 'python3-rosdep2'


def test_official_release_asset_selection_and_url_validation():
    release = {'tag_name': '1.3.0', 'assets': [{'name': 'ros2-apt-source_1.3.0.noble_all.deb',
                'browser_download_url': installer.RELEASE_PREFIX + '1.3.0/ros2-apt-source_1.3.0.noble_all.deb'}]}
    assert installer.release_asset(release, 'noble') == release['assets'][0]
    with pytest.raises(installer.InstallError):
        installer.release_asset(release, 'focal')
    release['assets'][0]['browser_download_url'] = 'https://example.org/foreign.deb'
    with pytest.raises(installer.InstallError):
        installer.release_asset(release, 'noble')


def test_failed_checksum_cannot_reach_package_install(tmp_path, monkeypatch):
    path = tmp_path / 'source.deb'
    path.write_bytes(b'not a source package')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **k: pytest.fail('must reject before invoking dpkg'))
    with pytest.raises(installer.InstallError, match='校验失败'):
        installer.verify_asset(path, {'digest': 'sha256:' + '0'*64})


@pytest.mark.parametrize('mode,expected', [('--check', 1), ('--dry-run', 0)])
def test_inspection_modes_never_run_privileged_or_configuration_steps(tmp_path, monkeypatch, mode, expected):
    monkeypatch.setattr(installer, 'os_release', lambda: {'codename': 'jammy', 'VERSION_ID': '22.04'})
    monkeypatch.setattr(installer, 'select_ros', lambda *a: ('testros', tmp_path / 'setup.bash', False))
    monkeypatch.setattr(installer, 'load_environment', lambda **kw: {'ROS_DISTRO': 'testros'})
    monkeypatch.setattr(installer, 'package_installed', lambda _: False)
    monkeypatch.setattr(installer, 'package_available', lambda _: True)
    app = installer.Installer(arguments(mode))
    app.run = lambda *a, **kw: pytest.fail('inspection attempted to execute installation')
    app.configure_user = lambda *a: pytest.fail('inspection changed user configuration')
    assert app.execute() == expected


def test_already_installed_dependencies_are_not_reinstalled(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'os_release', lambda: {'codename': 'jammy', 'VERSION_ID': '22.04'})
    monkeypatch.setattr(installer, 'select_ros', lambda *a: ('testros', tmp_path / 'setup.bash', False))
    monkeypatch.setattr(installer, 'load_environment', lambda **kw: {'ROS_DISTRO': 'testros'})
    monkeypatch.setattr(installer, 'package_installed', lambda _: True)
    app = installer.Installer(arguments())
    app.run = lambda *a, **kw: pytest.fail('unnecessary package install')
    steps = []
    app.verify = lambda _: steps.append('verify')
    app.configure_user = lambda _: steps.append('configure')
    assert app.execute() == 0
    assert steps == ['verify', 'configure']


def test_installer_selection_is_used_only_without_loaded_ros(tmp_path):
    env = empty_env(tmp_path)
    setup = tmp_path / 'selected/setup.bash'
    setup.parent.mkdir(); setup.touch()
    config = Path(env['XDG_CONFIG_HOME']) / 'ros2-recorder'
    config.mkdir(parents=True)
    (config / 'installation.json').write_text(json.dumps({'base_environment_setup': str(setup)}))
    assert saved_underlay(env) == setup
    assert choose_underlay(env) == setup
    loaded = {**env, 'ROS_VERSION': '2', 'ROS_DISTRO': 'other', 'AMENT_PREFIX_PATH': '/other'}
    assert choose_underlay(loaded) is None


def test_corrupted_or_missing_installer_selection_is_ignored(tmp_path):
    env = empty_env(tmp_path)
    config = Path(env['XDG_CONFIG_HOME']) / 'ros2-recorder'
    config.mkdir(parents=True)
    state = config / 'installation.json'
    state.write_text('broken json')
    assert saved_underlay(env) is None
    state.write_text(json.dumps({'base_environment_setup': str(tmp_path / 'missing')}))
    assert saved_underlay(env) is None


def test_rosdep_resolves_selected_workspace_as_target_user(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, 'os_release', lambda: {'codename': 'focal', 'VERSION_ID': '20.04'})
    workspace = tmp_path / 'workspace with spaces'
    package = workspace / 'src' / 'messages'
    package.mkdir(parents=True); (package / 'package.xml').touch()
    app = installer.Installer(arguments())
    calls = []
    app.run = lambda command, **kwargs: calls.append((command, kwargs))
    app.rosdep_workspace(str(workspace), 'foxy', {})
    install = next(c for c in calls if c[0][:2] == ['rosdep', 'install'])
    assert install[0][install[0].index('--from-paths') + 1] == str(workspace / 'src')
    assert install[1]['as_user'] and '--ignore-src' in install[0]
    assert any('--include-eol-distros' in c[0] for c in calls)
