"""Ubuntu dependency installer. Inspection modes never mutate the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from .environment import installed_environments, load_environment, saved_underlay

DEFAULT_ROS = {'focal': 'foxy', 'jammy': 'humble', 'noble': 'jazzy'}
COMMON_PACKAGES = (
    'python3', 'python3-pyqt5', 'python3-pyqt5.qtsvg', 'python3-yaml',
    'python3-colcon-common-extensions', 'build-essential', 'cmake',
    'desktop-file-utils', 'libxcb-xinerama0', 'libxkbcommon-x11-0',
    'fonts-noto-cjk',
)
ROS_COMPONENTS = (
    'ros2bag', 'rosbag2-py', 'rosbag2-transport', 'rosbag2-storage-default-plugins',
    'rclpy', 'rosidl-runtime-py', 'rosidl-default-generators',
    'ament-cmake', 'ament-index-python', 'std-msgs', 'sensor-msgs',
)
RELEASE_API = 'https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest'
RELEASE_PREFIX = 'https://github.com/ros-infrastructure/ros-apt-source/releases/download/'


class InstallError(RuntimeError):
    pass


def say(message):
    print(message, flush=True)


def os_release(path='/etc/os-release'):
    values = {}
    for line in Path(path).read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            words = shlex.split(value)
            values[key] = words[0] if words else ''
    if values.get('ID') != 'ubuntu':
        raise InstallError('此脚本目前仅自动安装 Ubuntu 依赖；其他系统可按 README 手动安装。')
    code = values.get('UBUNTU_CODENAME') or values.get('VERSION_CODENAME')
    if not code or not re.fullmatch(r'[a-z]+', code):
        raise InstallError('无法确认 Ubuntu 版本代号，已停止，避免配置错误的软件源。')
    values['codename'] = code
    return values


def validate_distro(value):
    if value and not re.fullmatch(r'[a-z][a-z0-9]*', value):
        raise InstallError('ROS 2 发行版名称格式无效')
    return value


def select_ros(args, system, env=None, installs=None):
    env = os.environ if env is None else env
    installs = installed_environments() if installs is None else list(installs)
    distro = validate_distro(args.ros_distro)
    if args.ros_setup:
        setup = Path(args.ros_setup).expanduser().resolve()
        if not setup.is_file():
            raise InstallError(f'环境文件不存在：{setup}')
        return distro, setup, False
    if distro:
        setup = next((p for p in installs if p.parent.name == distro), None)
        if setup:
            return distro, setup, False
        if args.install_ros:
            return distro, Path('/opt/ros') / distro / 'setup.bash', True
        raise InstallError(f'没有找到指定的 ROS 2：{distro}。如需安装，追加 --install-ros。')
    explicit = env.get('ROS2_RECORDER_SETUP')
    if explicit:
        setup = Path(explicit).expanduser().resolve()
        if not setup.is_file():
            raise InstallError(f'指定的 ROS 2 环境文件不存在：{setup}')
        return None, setup, False
    if env.get('ROS_VERSION') != '1' and env.get('ROS_DISTRO') and env.get('AMENT_PREFIX_PATH'):
        distro = validate_distro(env['ROS_DISTRO'])
        setup = next((p for p in installs if p.parent.name == distro), None)
        if setup is None:
            for key in ('COLCON_PREFIX_PATH', 'AMENT_PREFIX_PATH'):
                for prefix in env.get(key, '').split(os.pathsep):
                    if prefix:
                        candidates = [Path(prefix) / 'setup.bash', Path(prefix).parent / 'setup.bash']
                        setup = next((p.resolve() for p in candidates if p.is_file()), None)
                        if setup:
                            break
                if setup:
                    break
        return distro, setup, False
    configured = saved_underlay(env)
    if configured is not None:
        return None, configured, False
    if len(installs) == 1:
        return validate_distro(installs[0].parent.name), installs[0], False
    if len(installs) > 1:
        raise InstallError('找到多个 ROS 2 安装。请用 --ros-distro 名称 或 --ros-setup /路径/setup.bash 选择，不会自动覆盖其中任何一个。')
    if not args.install_ros:
        raise InstallError('未找到 ROS 2。全新机器请运行 ./install.sh --install-ros；已有自定义安装请传入 --ros-setup /路径/setup.bash。')
    distro = DEFAULT_ROS.get(system['codename'])
    if not distro:
        raise InstallError('此 Ubuntu 版本没有预设的 ROS 2 安装组合，请通过 --ros-distro 明确指定。')
    return distro, Path('/opt/ros') / distro / 'setup.bash', True


def package_installed(name):
    result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', name], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == 'install ok installed'


def package_available(name):
    env = dict(os.environ, LC_ALL='C')
    result = subprocess.run(['apt-cache', 'policy', name], env=env, capture_output=True, text=True)
    match = re.search(r'^\s*Candidate:\s*(\S+)', result.stdout, re.M)
    return bool(match and match.group(1) != '(none)')


def dependency_packages(distro, *, fresh=False, source_build=False):
    packages = list(COMMON_PACKAGES)
    if not source_build:
        if fresh:
            packages.append(f'ros-{distro}-ros-base')
        packages.extend(f'ros-{distro}-{component}' for component in ROS_COMPONENTS)
    return packages


def rosdep_package():
    for name in ('python3-rosdep', 'python3-rosdep2'):
        if package_installed(name):
            return name
    return 'python3-rosdep' if package_available('python3-rosdep') else 'python3-rosdep2'


def release_asset(release, codename):
    tag = release.get('tag_name', '')
    if not re.fullmatch(r'[0-9][A-Za-z0-9._+-]*', tag):
        raise InstallError('官方源配置包版本号无效')
    expected = f'ros2-apt-source_{tag}.{codename}_all.deb'
    asset = next((a for a in release.get('assets', []) if a.get('name') == expected), None)
    if not asset or not asset.get('browser_download_url', '').startswith(RELEASE_PREFIX + tag + '/'):
        raise InstallError(f'官方 ROS 2 源配置包没有提供 {codename} 安装文件，请先手动配置该系统的 ROS 2 软件源。')
    return asset


def download(url, destination, *, limit=8 * 1024**2):
    request = urllib.request.Request(url, headers={'User-Agent': 'ROS-Recorder-Installer', 'Accept': 'application/octet-stream'})
    total = 0
    with urllib.request.urlopen(request, timeout=45) as response, Path(destination).open('wb') as output:
        while True:
            block = response.read(65536)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise InstallError('下载文件超出预期大小，已停止')
            output.write(block)


def verify_asset(path, asset):
    digest = asset.get('digest')
    if digest:
        actual = 'sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if digest != actual:
            raise InstallError('源配置包校验失败，未安装该文件')
    identity = subprocess.run(['dpkg-deb', '-f', str(path), 'Package'], capture_output=True, text=True)
    if identity.returncode or identity.stdout.strip() != 'ros2-apt-source':
        raise InstallError('下载的文件不是预期的 ROS 2 源配置包')


class Installer:
    def __init__(self, args):
        self.args = args
        self.project = Path(__file__).resolve().parents[1]
        self.system = os_release()
        self.user = pwd.getpwnam(os.environ.get('SUDO_USER') or pwd.getpwuid(os.getuid()).pw_name)
        self.user_home = self.user.pw_dir
        self.env = dict(os.environ, LC_ALL='C.UTF-8')
        self.updated = False

    def run(self, argv, *, admin=False, as_user=False, capture=False, env=None, label=None):
        command = list(map(str, argv))
        child_env = dict(self.env if env is None else env)
        if as_user:
            child_env['HOME'] = self.user_home
            child_env['USER'] = self.user.pw_name
            child_env['LOGNAME'] = self.user.pw_name
            if os.geteuid() == 0 and self.user.pw_uid != 0:
                child_env.pop('XDG_CONFIG_HOME', None)
                child_env.pop('XDG_DATA_HOME', None)
                child_env.pop('XDG_CACHE_HOME', None)
                # Pass only path/configuration data, never shell command text.
                command = ['runuser', '-u', self.user.pw_name, '--', 'env',
                           'HOME=' + self.user_home, 'USER=' + self.user.pw_name,
                           'LOGNAME=' + self.user.pw_name, *command]
        if admin and os.geteuid() != 0:
            if not shutil.which('sudo'):
                raise InstallError('安装系统依赖需要管理员权限，但没有找到 sudo。请由管理员运行此安装脚本。')
            command = ['sudo', *command]
        say(label if label else '执行：' + shlex.join(command))
        result = subprocess.run(command, cwd=str(self.project), env=child_env,
                                capture_output=capture, text=capture)
        if result.returncode:
            detail = (result.stderr or result.stdout or '')[-3000:] if capture else ''
            raise InstallError(f'命令未完成（{result.returncode}）：{shlex.join(argv)}\n{detail}')
        return result

    def apt_update(self):
        if not self.updated:
            self.run(['apt-get', 'update'], admin=True)
            self.updated = True

    def install_missing(self, packages):
        missing = [p for p in packages if not package_installed(p)]
        if not missing:
            return
        self.apt_update()
        unavailable = [p for p in missing if not package_available(p)]
        if unavailable:
            raise InstallError('当前软件源没有这些依赖：' + ', '.join(unavailable) +
                               '\n请检查网络和软件源；ROS 2 源缺失时可追加 --install-ros 后重试。')
        self.run(['apt-get', 'install', '-y', '--no-install-recommends', *missing], admin=True)

    def ensure_universe(self):
        files = [Path('/etc/apt/sources.list'), *Path('/etc/apt/sources.list.d').glob('*.list'), *Path('/etc/apt/sources.list.d').glob('*.sources')]
        for path in files:
            if path.is_file():
                if any('universe' in line.split() for line in path.read_text(errors='replace').splitlines() if not line.lstrip().startswith('#')):
                    return
        self.install_missing(['software-properties-common'])
        self.run(['add-apt-repository', '-y', 'universe'], admin=True)
        self.updated = False

    def ensure_ros_repository(self, distro):
        self.apt_update()
        if package_available(f'ros-{distro}-rclpy'):
            return
        self.install_missing(['ca-certificates'])
        say('正在获取官方 ROS 2 软件源配置包…')
        request = urllib.request.Request(RELEASE_API, headers={'User-Agent': 'ROS-Recorder-Installer'})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise InstallError('源配置版本信息超出预期大小')
        asset = release_asset(json.loads(raw), self.system['codename'])
        with tempfile.TemporaryDirectory(prefix='ros-recorder-apt-') as directory:
            path = Path(directory) / asset['name']
            download(asset['browser_download_url'], path)
            verify_asset(path, asset)
            self.run(['dpkg', '-i', str(path)], admin=True)
        self.updated = False
        self.apt_update()
        if not package_available(f'ros-{distro}-rclpy'):
            raise InstallError(f'官方源中没有当前系统对应的 {distro} 包。请确认 Ubuntu 与 ROS 2 版本组合。')

    def rosdep_workspace(self, workspace, distro, env):
        root = Path(workspace).expanduser().resolve()
        source = root / 'src' if (root / 'src').is_dir() else root
        if not source.is_dir() or not any(source.rglob('package.xml')):
            raise InstallError('指定的工作空间没有找到 package.xml')
        if not Path('/etc/ros/rosdep/sources.list.d/20-default.list').is_file():
            self.run(['rosdep', 'init'], admin=True)
        update = ['rosdep', 'update', '--rosdistro', distro]
        if distro in {'foxy', 'galactic'}:
            update.append('--include-eol-distros')
        self.run(update, as_user=True, env=env)
        self.run(['rosdep', 'install', '--from-paths', str(source), '--ignore-src', '--rosdistro', distro, '-y'],
                 as_user=True, env=env)

    def configure_user(self, setup):
        script = '''
import json, os, runpy, sys
from pathlib import Path
root=Path(sys.argv[1])
sys.path.insert(0,str(root))
from ros2_recorder.config import atomic_json
config=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'ros2-recorder'
cache=Path(os.environ.get('XDG_CACHE_HOME',str(Path.home()/'.cache')))/'ros2-recorder'
cache.mkdir(parents=True,exist_ok=True)
atomic_json(config/'installation.json',{'source_directory':str(root),'base_environment_setup':sys.argv[2]})
runpy.run_path(str(root/'install_launcher.py'),run_name='__main__')
'''
        self.run([sys.executable, '-c', script, str(self.project), str(setup or '')], as_user=True,
                 label=f'正在为 {self.user.pw_name} 配置菜单入口和安装信息…')
        if shutil.which('update-desktop-database'):
            data = Path(os.environ.get('XDG_DATA_HOME', str(Path(self.user_home) / '.local/share')))
            if os.geteuid() == 0 and self.user.pw_uid != 0:
                data = Path(self.user_home) / '.local/share'
            self.run(['update-desktop-database', str(data / 'applications')], as_user=True)

    def verify(self, env):
        from .compatibility import detect_capabilities
        from .environment import validate_message_types
        self.run([sys.executable, '-c', 'from PyQt5 import QtWidgets, QtSvg; import yaml; print("Qt / SVG / YAML 正常")'], capture=True,
                 label='正在验证界面、图标和配置依赖…')
        validate_message_types(env, [])
        caps = detect_capabilities(env)
        if 'sqlite3' not in caps.storage_ids:
            raise InstallError('SQLite3 存储插件没有加载成功')
        say('录制组件正常；可用存储格式：' + ', '.join(sorted(caps.storage_ids)))

    def execute(self):
        if sys.version_info < (3, 8):
            raise InstallError('需要 Python 3.8 及以上，请使用匹配当前 Ubuntu / ROS 2 的系统 Python。')
        say(f"系统：Ubuntu {self.system.get('VERSION_ID', '?')} ({self.system['codename']})")
        distro, setup, fresh = select_ros(self.args, self.system)
        ros_env = None
        if not fresh:
            ros_env = load_environment(base_setup=str(setup)) if setup else load_environment()
            actual = validate_distro(ros_env.get('ROS_DISTRO', ''))
            if distro and actual != distro:
                raise InstallError('指定发行版与环境文件实际版本不一致')
            distro = actual
        if not distro:
            raise InstallError('无法识别当前 ROS 2 发行版，请通过 --ros-distro 指定')
        say('ROS 2 环境：' + distro + ('（准备安装）' if fresh else '（复用已有）'))
        if self.system['codename'] == 'focal' and distro == 'foxy':
            say('提示：此组合为早期 ROS 2，软件源和功能以现有仓库实际可用内容为准。')
        source_build = not fresh and not package_installed(f'ros-{distro}-ros-workspace')
        packages = dependency_packages(distro, fresh=fresh, source_build=source_build)
        rosdep = rosdep_package()
        packages.append(rosdep)
        missing = [p for p in packages if not package_installed(p)]
        optional_mcap = f'ros-{distro}-rosbag2-storage-mcap'
        say('缺少的依赖：' + (', '.join(missing) if missing else '无'))
        if self.args.check:
            if fresh or missing:
                return 1
            self.verify(ros_env)
            say('检查通过；未安装软件包，未修改配置。')
            return 0
        if self.args.dry_run:
            if self.args.install_ros:
                say('计划：需要时配置官方 ROS 2 软件源。')
            if missing:
                say('计划：sudo apt-get update')
                say('计划：' + shlex.join(['sudo', 'apt-get', 'install', '-y', *missing]))
            if not self.args.no_mcap and not source_build and not package_installed(optional_mcap):
                say('计划：如果仓库提供则安装 ' + optional_mcap + '，否则保留 SQLite3。')
            if self.args.workspace:
                say('计划：使用 rosdep 安装工作空间声明的依赖：' + self.args.workspace)
            say(f'计划：为 {self.user.pw_name} 保存基础环境配置并创建应用菜单入口。')
            say('预览结束；未下载、未安装、未修改配置。')
            return 0
        if missing or fresh:
            self.ensure_universe()
        if self.args.install_ros and not source_build:
            self.ensure_ros_repository(distro)
            # rosdep availability may change after enabling the ROS repository.
            replacement = rosdep_package()
            packages = [replacement if p == rosdep else p for p in packages]
        self.install_missing(packages)
        if not self.args.no_mcap and not source_build and not package_installed(optional_mcap):
            self.apt_update()
            if package_available(optional_mcap):
                self.install_missing([optional_mcap])
            else:
                say('当前仓库没有 MCAP 插件，继续配置 SQLite3 录制。')
        ros_env = load_environment(base_setup=str(setup)) if setup else load_environment()
        if self.args.workspace:
            self.rosdep_workspace(self.args.workspace, distro, ros_env)
        self.verify(ros_env)
        self.configure_user(setup)
        say('安装配置完成。在应用菜单搜索“ROS 2 录制工作台”，或运行 ./launch.sh。')
        say('菜单入口引用当前软件目录，请保留此目录；移动后重新运行安装脚本。')
        return 0


def parser():
    result = argparse.ArgumentParser(description='ROS 2 录制工作台一键依赖安装与配置')
    mode = result.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='仅检查，不修改系统或配置；缺依赖时返回 1')
    mode.add_argument('--dry-run', action='store_true', help='预览安装计划，不下载、不安装、不修改配置')
    result.add_argument('--install-ros', action='store_true', help='必要时配置官方源并安装匹配系统的 ROS 2 基础环境')
    result.add_argument('--ros-distro', help='选择 ROS 2 发行版；多个版本并存时使用')
    result.add_argument('--ros-setup', help='选择已有 ROS 2 基础环境 setup.bash 的完整路径')
    result.add_argument('--no-mcap', action='store_true', help='不额外安装可选 MCAP 存储插件')
    result.add_argument('--workspace', help='另用 rosdep 安装此工作空间/消息包声明的依赖')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return Installer(args).execute()
    except (InstallError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        say('安装未完成：' + str(exc))
        say('已完成的安装会保留。修复网络、软件源或权限后，可重新运行此脚本。')
        return 2
    except KeyboardInterrupt:
        say('安装已中止，可稍后重新运行。')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
