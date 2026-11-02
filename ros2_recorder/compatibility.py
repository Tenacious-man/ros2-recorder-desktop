"""Inspect the installed recorder instead of assuming a particular ROS distribution."""
from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess


@dataclass
class RecorderCapabilities:
    options: set[str]
    storage_ids: set[str]
    compression_modes: set[str]
    cache_unit: str
    double_buffered: bool = False

    @classmethod
    def from_help(cls, help_text, storage_ids=()):
        options = set(re.findall(r'--[a-z][a-z0-9_-]*', help_text))
        cache = re.search(r'^\s*--max-cache-size\b.*?(?=\n\s*(?:--[a-z]|-[a-z],)|\Z)', help_text, re.M | re.S)
        cache_text = cache.group(0).lower() if cache else ''
        unit = 'bytes' if any(s in cache_text for s in ('byte', 'mebibyte', 'mib')) else 'messages' if 'messages' in cache_text else 'unknown'
        modes = re.search(r'--compression-mode\s+\{([^}]+)\}', help_text)
        choices = re.search(r'--storage\s+\{([^}]+)\}|-s\s+\{([^}]+)\}', help_text)
        advertised = set((choices.group(1) or choices.group(2)).split(',')) if choices else set()
        return cls(options, set(storage_ids) | advertised,
                   set(modes.group(1).split(',')) if modes else {'none'}, unit,
                   'double buffer' in cache_text)

    def command(self, config, output):
        config.validate()
        if self.storage_ids and config.storage not in self.storage_ids:
            raise ValueError(f"当前 ROS 2 未安装 {config.storage} 存储插件，请选择可用格式或安装对应插件")
        args = ['ros2', 'bag', 'record', '--output', str(output), '--storage', config.storage]
        def add(flag, value, active=True):
            if flag not in self.options:
                if active:
                    raise ValueError(f"当前 ROS 2 的录制器不支持 {flag}，请调整对应配置")
                return
            args.append(flag)
            if value is not None:
                args.append(str(value))
        if '--max-cache-size' in self.options:
            if self.cache_unit == 'messages':
                cache = config.cache_messages
            elif self.cache_unit == 'bytes':
                cache = config.cache_bytes if config.cache_bytes >= 0 else config.cache_mib * 1024**2
            else:
                raise ValueError('无法确认当前录制器的缓存单位，已阻止传入可能错误的缓存值')
            add('--max-cache-size', cache)
        size = config.split_bytes if config.split_bytes >= 0 else config.split_mib * 1024**2
        add('--max-bag-size', size, bool(size))
        add('--max-bag-duration', config.split_seconds, bool(config.split_seconds))
        if config.all_topics:
            add('--all', None)
        elif config.regex:
            add('--regex', config.regex)
        else:
            if '--topics' in self.options:
                args.append('--topics')
            args.extend(config.topics)
        for flag, value in (('--exclude', config.exclude), ('--storage-preset-profile', config.preset),
                            ('--qos-profile-overrides-path', config.qos_path), ('--storage-config-file', config.storage_config)):
            if value:
                add(flag, value)
        if config.compression != 'none':
            if config.compression not in self.compression_modes:
                raise ValueError(f'当前 ROS 2 不支持 {config.compression} 压缩模式')
            add('--compression-mode', config.compression)
            add('--compression-format', 'zstd')
            for flag, value in (('--compression-queue-size', config.compression_queue), ('--compression-threads', config.compression_threads)):
                if flag in self.options:
                    add(flag, value)
                elif value != (4 if flag == '--compression-queue-size' else 0):
                    raise ValueError(f'当前 ROS 2 不支持 {flag}，无法采用这个压缩配置')
        if config.include_hidden:
            add('--include-hidden-topics', None)
        if config.use_sim_time:
            add('--use-sim-time', None)
        return args


def detect_capabilities(env):
    result = subprocess.run(['ros2', 'bag', 'record', '--help'], env=env, capture_output=True,
                            text=True, timeout=20)
    if result.returncode:
        raise ValueError('ROS 2 录制组件不可用：' + (result.stderr or result.stdout)[-3000:])
    storage = subprocess.run(['ros2', 'bag', 'list', 'storage'], env=env, capture_output=True,
                             text=True, timeout=15)
    ids = set(storage.stdout.split()) if storage.returncode == 0 else set()
    if not ids:
        script = '''
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from ament_index_python.resources import get_resources, get_resource
ids = []
resource = 'rosbag2_storage__pluginlib__plugin'
for package, prefix in get_resources(resource).items():
    content, prefix = get_resource(resource, package)
    for relative in content.splitlines():
        path = Path(prefix) / relative
        if path.is_file():
            for cls in ET.parse(path).getroot().iter('class'):
                if cls.get('name'):
                    ids.append(cls.get('name'))
print(json.dumps(ids))
'''
        # Older recorder CLIs do not provide `bag list`; the ament plugin index is available.
        from .environment import python_executable
        import json
        plugins = subprocess.run([python_executable(env), '-c', script], env=env, capture_output=True, text=True, timeout=10)
        if plugins.returncode == 0:
            try:
                ids.update(json.loads(plugins.stdout.splitlines()[-1]))
            except (ValueError, IndexError):
                pass
    caps = RecorderCapabilities.from_help(result.stdout, ids)
    if not caps.storage_ids:
        caps.storage_ids = {'sqlite3'}
    return caps
