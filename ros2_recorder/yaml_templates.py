"""Editable user templates, created once without replacing user changes."""
import os
from pathlib import Path

TEMPLATES = {
    'qos': '''# ROS 2 QoS 覆盖：按完整话题名配置，不支持用通配符代表所有话题。
# 默认不覆盖任何话题，沿用自动适配。
# 使用示例时，删除最后一行 {}，并取消下方话题配置前面的 #。
# /iv_points:
#   reliability: reliable  # reliable 或 best_effort
#   history: keep_last    # keep_last 或 keep_all
#   depth: 10             # keep_last 队列深度，单位：消息条数
#   durability: volatile  # volatile 或 transient_local
{}
''',
    'sqlite3': '''# sqlite3 存储配置；空列表沿用插件/存储预设的参数。
write:
  pragmas: []
# 自定义示例（替换上方空列表，注意磁盘吞吐变化）：
# write:
#   pragmas:
#     - "journal_mode = WAL"
#     - "synchronous = NORMAL"
''',
    'mcap': '''# MCAP 存储配置，键名大小写敏感；显式参数会覆盖存储预设。
# 默认模板保留索引和块校验，不进行块压缩。
noChunkCRC: false
noChunking: false
noMessageIndex: false
noSummary: false
chunkSize: 786432       # 未压缩块的目标字节数
compression: "None"    # None、Lz4、Zstd
# compressionLevel: "Fast"  # 启用压缩后可选 Fastest/Fast/Default/Slow/Slowest
''',
}


def template_paths():
    root = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'ros2-recorder' / 'templates'
    return {kind: root / (kind + '.yaml') for kind in TEMPLATES}


def ensure_templates():
    paths = template_paths()
    for kind, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(TEMPLATES[kind])
        except FileExistsError:
            pass
    return paths
