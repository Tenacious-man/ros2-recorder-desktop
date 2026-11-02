#!/usr/bin/env python3
"""Build a distro-neutral Python application package without installing it."""
import argparse
import ast
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
PACKAGE = 'ros2-recorder-desktop'


def application_version():
    tree = ast.parse((ROOT / 'ros2_recorder' / '__init__.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Application version is missing')


def build(version, output_dir):
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+:~\-]*', version):
        raise ValueError('Invalid Debian package version')
    if not shutil.which('dpkg-deb'):
        raise RuntimeError('需要 dpkg-deb，请在 Ubuntu / Debian 系统构建。')
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f'{PACKAGE}_{version}_all.deb'
    with tempfile.TemporaryDirectory(prefix='ros2-recorder-deb-') as temporary:
        stage = Path(temporary)
        app_root = stage / 'usr/lib/ros2-recorder'
        def copy(source, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o644)

        def write(relative, content, mode=0o644):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            target.chmod(mode)

        for source in sorted((ROOT / 'ros2_recorder').glob('*.py')):
            copy(source, app_root / 'ros2_recorder' / source.name)
        copy(ROOT / 'ros2_recorder/assets/icon.svg', app_root / 'ros2_recorder/assets/icon.svg')
        for name in ('launch.sh', 'install.sh', 'install_launcher.py'):
            copy(ROOT / name, app_root / name)
        for name in ('launch.sh', 'install.sh'):
            (app_root / name).chmod(0o755)
        write('usr/bin/ros2-recorder', '#!/bin/sh\nexec /bin/bash /usr/lib/ros2-recorder/launch.sh "$@"\n', 0o755)
        write('usr/bin/ros2-recorder-setup', '#!/bin/sh\nexec /bin/bash /usr/lib/ros2-recorder/install.sh "$@"\n', 0o755)
        write('usr/share/applications/ros2-recorder.desktop', '\n'.join([
            '[Desktop Entry]', 'Version=1.0', 'Type=Application', 'Name=ROS Recorder',
            'Name[zh_CN]=ROS 2 录制工作台',
            'Comment=ROS 2 recording, custom messages and frequency monitoring',
            'Exec=ros2-recorder', 'TryExec=ros2-recorder', 'Icon=ros2-recorder',
            'Terminal=false', 'Categories=Development;', 'StartupNotify=true', '',
        ]))
        copy(ROOT / 'ros2_recorder/assets/icon.svg', stage / 'usr/share/icons/hicolor/scalable/apps/ros2-recorder.svg')
        for name in ('README.md', 'INSTALL.md'):
            copy(ROOT / name, stage / 'usr/share/doc' / PACKAGE / name)
        payload = sorted(p for p in stage.rglob('*') if p.is_file())
        installed_kib = (sum(p.stat().st_size for p in payload) + 1023) // 1024
        write('DEBIAN/control', '\n'.join([
            f'Package: {PACKAGE}', f'Version: {version}', 'Section: utils', 'Priority: optional',
            'Architecture: all', 'Maintainer: ROS Recorder contributors <ros2-recorder@localhost>',
            f'Installed-Size: {installed_kib}',
            'Depends: bash, python3 (>= 3.8), python3-pyqt5 (>= 5.14), python3-pyqt5.qtsvg, python3-yaml (>= 5.3)',
            'Recommends: fonts-noto-cjk, python3-colcon-common-extensions, build-essential, cmake',
            'Description: ROS 2 recording workbench',
            ' Chinese desktop interface for rosbag2 recording, topic frequency monitoring,',
            ' custom message builds and recording reports.',
            ' Uses an existing ROS 2 installation selected at runtime. ROS 2 and sensor',
            ' drivers are not bundled. Run ros2-recorder-setup to configure dependencies.', '',
        ]))
        write('DEBIAN/md5sums', ''.join(
            f'{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(stage)}\n' for p in payload))
        # xz archives can be read by the older dpkg shipped with Ubuntu 20.04.
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(stage), str(destination)], check=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.deb.sha256').write_text(f'{digest}  {destination.name}\n')
    return destination


def main():
    parser = argparse.ArgumentParser(description='构建 ROS 2 录制工作台 deb 安装包')
    parser.add_argument('--version', default=application_version() + '-1', help='Debian 软件包版本')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts')
    args = parser.parse_args()
    print(build(args.version, args.output_dir))


if __name__ == '__main__':
    main()
