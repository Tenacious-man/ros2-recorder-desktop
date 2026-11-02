"""Verify the distributable, rather than installing into the developer's OS."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

from ros2_recorder import __version__


@pytest.fixture(scope='module')
def deb_bundle(tmp_path_factory):
    if not shutil.which('dpkg-deb'):
        pytest.skip('dpkg-deb is not installed')
    target = tmp_path_factory.mktemp('deb-package')
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / 'make_deb.py'), '--output-dir', str(target)], check=True, capture_output=True)
    package = target / f'ros2-recorder-desktop_{__version__}-1_all.deb'
    extracted = target / 'extracted'
    subprocess.run(['dpkg-deb', '-x', str(package), str(extracted)], check=True)
    return package, extracted


def test_deb_metadata_payload_permissions_and_checksums(deb_bundle):
    package, extracted = deb_bundle
    field = lambda name: subprocess.check_output(['dpkg-deb', '-f', str(package), name], text=True).strip()
    assert field('Package') == 'ros2-recorder-desktop'
    assert field('Version') == f'{__version__}-1'
    assert field('Architecture') == 'all'
    assert 'python3-pyqt5' in field('Depends') and 'python3-yaml' in field('Depends')
    assert 'humble' not in field('Depends') and 'jazzy' not in field('Depends')
    for name in ('ros2-recorder', 'ros2-recorder-setup'):
        command = extracted / 'usr/bin' / name
        assert os.access(command, os.X_OK)
        subprocess.run(['/bin/sh', '-n', str(command)], check=True)
    assert (extracted / 'usr/lib/ros2-recorder/ros2_recorder/probe.py').is_file()
    assert (extracted / 'usr/lib/ros2-recorder/install.sh').is_file()
    assert (extracted / 'usr/share/icons/hicolor/scalable/apps/ros2-recorder.svg').is_file()
    forbidden = {'__pycache__', 'profiles.json', 'installation.json', '.recorder_sessions', 'sites', 'tests', 'bags'}
    assert not any(forbidden.intersection(p.relative_to(extracted).parts) for p in extracted.rglob('*'))
    raw = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(package)])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        assert all(entry.uid == 0 and entry.gid == 0 for entry in archive.getmembers())
    checksums = subprocess.check_output(['dpkg-deb', '-I', str(package), 'md5sums'], text=True)
    for line in checksums.splitlines():
        digest, relative = line.split('  ', 1)
        assert hashlib.md5((extracted / relative).read_bytes()).hexdigest() == digest
    assert package.with_suffix('.deb.sha256').read_text().split()[0] == hashlib.sha256(package.read_bytes()).hexdigest()
    desktop = extracted / 'usr/share/applications/ros2-recorder.desktop'
    assert 'Exec=ros2-recorder\n' in desktop.read_text()
    if shutil.which('desktop-file-validate'):
        subprocess.run(['desktop-file-validate', str(desktop)], check=True)


@pytest.mark.integration
def test_unpacked_application_and_dependency_checker_run(deb_bundle, tmp_path):
    _, extracted = deb_bundle
    root = extracted / 'usr/lib/ros2-recorder'
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), QT_QPA_PLATFORM='offscreen')
    # Launch the exact payload scripts. Absolute /usr/bin wrappers are checked
    # above; executing them here would target a real installed system directory.
    screenshot = tmp_path / 'window.png'
    result = subprocess.run(['/bin/bash', str(root / 'launch.sh'), '--no-discovery', '--screenshot', str(screenshot)],
                            cwd='/tmp', env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert screenshot.read_bytes().startswith(b'\x89PNG')
    check = subprocess.run(['/bin/bash', str(root / 'install.sh'), '--dry-run'], cwd='/tmp', env=env,
                           capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stdout + check.stderr
    assert not (tmp_path / 'config/ros2-recorder/installation.json').exists()
