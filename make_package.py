#!/usr/bin/env python3
"""Package only application/installation sources; never include recordings or local configuration."""
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent
destination = root / 'artifacts' / 'ros2-recorder-portable.tar.gz'
destination.parent.mkdir(exist_ok=True)
def source_only(info):
    if '__pycache__' in Path(info.name).parts or info.name.endswith(('.pyc', '.pyo')):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ''
    return info

with tarfile.open(destination, 'w:gz') as bundle:
    files = [*root.joinpath('ros2_recorder').glob('*.py'), *root.joinpath('tests').glob('test_*.py')]
    files.append(root / 'tests/conftest.py')
    files += list(root.joinpath('docs').glob('*.md'))
    files += [root / name for name in ('ros2_recorder/assets/icon.svg', 'install.sh', 'launch.sh',
                                     'install_launcher.py', 'make_package.py', 'make_deb.py', 'pyproject.toml', 'README.md', 'INSTALL.md')]
    for path in sorted(files):
        bundle.add(path, arcname='ros2-recorder/' + str(path.relative_to(root)), filter=source_only)
print(destination)
