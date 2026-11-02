#!/usr/bin/env python3
"""Install a per-user application menu entry for this checkout."""
from pathlib import Path
import hashlib
import os

root = Path(__file__).resolve().parent
data_dir = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
application_dir = data_dir / 'applications'
application_dir.mkdir(parents=True, exist_ok=True)
icon_data = (root / 'ros2_recorder/assets/icon.svg').read_bytes()
icon_dir = data_dir / 'icons' / 'hicolor' / 'scalable' / 'apps'
icon_dir.mkdir(parents=True, exist_ok=True)
# A new path for changed artwork avoids stale application-menu icon caches.
icon_path = icon_dir / ('ros2-recorder-' + hashlib.sha256(icon_data).hexdigest()[:12] + '.svg')
icon_path.write_bytes(icon_data)
target = application_dir / 'ros2-recorder.desktop'
def desktop_quote(path):
    return '"' + str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
target.write_text('\n'.join([
    '[Desktop Entry]', 'Version=1.0', 'Type=Application', 'Name=ROS Recorder',
    'Name[zh_CN]=ROS 2 录制工作台', 'Comment=ROS 2 recording, custom messages and frequency monitoring',
    'Exec=' + desktop_quote(root / 'launch.sh'),
    'Icon=' + str(icon_path),
    'Terminal=false', 'Categories=Development;', 'StartupNotify=true', '',
]), encoding='utf-8')
target.chmod(0o755)
print(f'已添加应用菜单入口：{target}')
