"""Validated settings and reversible ros2 bag command import/export."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path
import re
import shlex
import tempfile

MIB = 1024 * 1024


@dataclass
class RecordConfig:
    output_root: str = str(Path.home() / "rosbags")
    prefix: str = "capture"
    all_topics: bool = True
    topics: list[str] = field(default_factory=list)
    expected_hz: dict[str, float] = field(default_factory=dict)
    regex: str = ""
    exclude: str = ""
    storage: str = "sqlite3"
    preset: str = ""
    cache_mib: int = 512
    cache_messages: int = 100
    split_mib: int = 4096
    cache_bytes: int = -1
    split_bytes: int = -1
    split_seconds: int = 0
    compression: str = "none"
    compression_queue: int = 4
    compression_threads: int = 0
    qos_path: str = ""
    storage_config: str = ""
    include_hidden: bool = False
    use_sim_time: bool = False
    min_free_gib: float = 5.0
    auto_stop_low_disk: bool = True
    workspace: str = ""
    packages: list[str] = field(default_factory=list)
    environment_setup: str = ""
    auto_detect_frequency: bool = True
    base_environment_setup: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RecordConfig":
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"不支持的配置字段：{', '.join(sorted(unknown))}")
        config = cls(**data)
        config.validate()
        return config

    def validate(self) -> None:
        # Reject malformed profile imports before they reach widgets or CLI arguments.
        defaults = RecordConfig()
        for f in fields(self):
            value, default = getattr(self, f.name), getattr(defaults, f.name)
            if isinstance(default, bool):
                valid = type(value) is bool
            elif isinstance(default, int):
                valid = type(value) is int
            elif isinstance(default, float):
                valid = type(value) in (int, float)
            else:
                valid = isinstance(value, type(default))
            if not valid:
                raise ValueError(f"配置字段 {f.name} 类型错误")
        if not self.output_root.strip():
            raise ValueError("请选择保存目录")
        if any(not isinstance(p, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", p) for p in self.packages):
            raise ValueError("消息包名称无效")
        if not re.fullmatch(r"[\w\-]+", self.prefix, flags=re.UNICODE) or len(self.prefix) > 80:
            raise ValueError("录制名称只能包含文字、数字、下划线和短横线（最多 80 字）")
        if self.storage not in {"sqlite3", "mcap"}:
            raise ValueError("存储格式必须是 sqlite3 或 mcap")
        allowed = {"", "resilient"} if self.storage == "sqlite3" else {"", "fastwrite", "zstd_fast", "zstd_small"}
        if self.preset not in allowed:
            raise ValueError("当前存储格式不支持这个预设")
        if self.all_topics and (self.topics or self.regex):
            raise ValueError("全部话题、指定话题和正则模式不能同时启用")
        if not self.all_topics and not self.topics and not self.regex:
            raise ValueError("请至少选择一个要录制的话题")
        if self.topics and (self.regex or self.exclude):
            raise ValueError("指定话题模式不能同时使用正则或排除规则")
        if any(not isinstance(t, str) or not t.startswith('/') or any(c.isspace() for c in t) for t in self.topics):
            raise ValueError("话题必须是以 / 开头且没有空格的完整名称")
        for expr in (self.regex, self.exclude):
            if expr:
                try:
                    re.compile(expr)
                except re.error as exc:
                    raise ValueError(f"正则表达式无效：{exc}") from exc
        if not 0 <= self.cache_mib <= 4095:
            raise ValueError("缓存大小必须在 0～4095 MiB 之间")
        if not 0 <= self.cache_messages <= 100000000:
            raise ValueError("缓存消息数超出范围（0～100000000）")
        if not -1 <= self.cache_bytes <= 4294967295:
            raise ValueError("精确缓存字节数超出范围（0～4294967295，空值表示使用 MiB）")
        if self.split_bytes != -1 and not (self.split_bytes == 0 or 86016 < self.split_bytes <= 2**63 - 1):
            raise ValueError("精确分包字节数必须为 0（不分包）或大于 86016")
        if not 0 <= self.split_mib <= 1048576 or not 0 <= self.split_seconds <= 2147483647:
            raise ValueError("分包大小或时长超出范围")
        if self.compression not in {"none", "file", "message"}:
            raise ValueError("压缩模式无效")
        if self.storage == "mcap" and self.compression == "message":
            raise ValueError("MCAP 不支持 rosbag 消息级压缩；请使用 MCAP 的 zstd_fast 预设")
        if not 1 <= self.compression_queue <= 100000 or not 0 <= self.compression_threads <= 1024:
            raise ValueError("压缩队列/线程数量超出范围")
        import math
        if not math.isfinite(self.min_free_gib) or not 0 <= self.min_free_gib <= 1000000:
            raise ValueError("磁盘预留空间无效")
        for topic, hz in self.expected_hz.items():
            if not isinstance(topic, str) or not topic.startswith('/') or type(hz) not in (int, float) or not math.isfinite(hz) or hz <= 0:
                raise ValueError("预期频率必须是大于 0 的有限数值")

    def argv(self, output: Path | str) -> list[str]:
        self.validate()
        args = ["ros2", "bag", "record", "-o", str(output), "-s", self.storage,
                "--max-cache-size", str(self.cache_bytes if self.cache_bytes >= 0 else self.cache_mib * MIB),
                "--max-bag-size", str(self.split_bytes if self.split_bytes >= 0 else self.split_mib * MIB),
                "--max-bag-duration", str(self.split_seconds)]
        if self.all_topics:
            args.append("--all")
        elif self.regex:
            args += ["--regex", self.regex]
        else:
            args += self.topics
        for flag, value in (("--exclude", self.exclude), ("--storage-preset-profile", self.preset),
                            ("--qos-profile-overrides-path", self.qos_path),
                            ("--storage-config-file", self.storage_config)):
            if value:
                args += [flag, value]
        if self.compression != "none":
            args += ["--compression-mode", self.compression, "--compression-format", "zstd",
                     "--compression-queue-size", str(self.compression_queue),
                     "--compression-threads", str(self.compression_threads)]
        if self.include_hidden:
            args.append("--include-hidden-topics")
        if self.use_sim_time:
            args.append("--use-sim-time")
        return args

    def to_dict(self) -> dict:
        return asdict(self)


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(f"命令无法导入：{message}")


def import_command(command: str, *, cache_unit='bytes') -> RecordConfig:
    if cache_unit not in {'bytes', 'messages'}:
        raise ValueError('尚未确认当前录制器的缓存单位，请先加载有效 ROS 2 环境')
    tokens = shlex.split(command)
    if tokens[:3] != ["ros2", "bag", "record"]:
        raise ValueError("请粘贴以 ros2 bag record 开头的录制命令；先去掉 source 等其他命令")
    if any(t in {";", "&&", "||", "|", ">", "&"} for t in tokens):
        raise ValueError("只支持单条录制命令")
    p = CommandParser(add_help=False, allow_abbrev=False)
    p.add_argument("-a", "--all", action="store_true", dest="all_topics")
    p.add_argument("-o", "--output")
    p.add_argument("-s", "--storage", default="sqlite3")
    p.add_argument("-e", "--regex", default="")
    p.add_argument("-x", "--exclude", default="")
    p.add_argument("--max-cache-size", type=int, default=100 * MIB if cache_unit == 'bytes' else 0)
    p.add_argument("-b", "--max-bag-size", type=int, default=0)
    p.add_argument("-d", "--max-bag-duration", type=int, default=0)
    p.add_argument("--storage-preset-profile", default="")
    p.add_argument("--storage-config-file", default="")
    p.add_argument("--qos-profile-overrides-path", default="")
    p.add_argument("--compression-mode", default="none")
    p.add_argument("--compression-format", default="")
    p.add_argument("--compression-queue-size", type=int, default=1)
    p.add_argument("--compression-threads", type=int, default=0)
    p.add_argument("--include-hidden-topics", action="store_true")
    p.add_argument("--use-sim-time", action="store_true")
    p.add_argument("topics", nargs="*")
    ns = p.parse_intermixed_args(tokens[3:])
    for value in (ns.max_cache_size, ns.max_bag_size):
        if value < 0:
            raise ValueError("缓存和分包字节数不能为负数")
    if ns.compression_format not in {"", "zstd"}:
        raise ValueError("本版仅支持 zstd 压缩")
    if ns.compression_format and ns.compression_mode == "none":
        raise ValueError("指定压缩格式时，必须选择压缩模式")
    config = RecordConfig(
        all_topics=ns.all_topics, topics=ns.topics, regex=ns.regex, exclude=ns.exclude,
        storage=ns.storage, preset=ns.storage_preset_profile,
        cache_mib=ns.max_cache_size // MIB if cache_unit == 'bytes' else 512,
        cache_messages=ns.max_cache_size if cache_unit == 'messages' else 100,
        cache_bytes=ns.max_cache_size if cache_unit == 'bytes' else -1, split_bytes=ns.max_bag_size,
        split_seconds=ns.max_bag_duration, compression=ns.compression_mode,
        compression_queue=ns.compression_queue_size, compression_threads=ns.compression_threads,
        qos_path=ns.qos_profile_overrides_path, storage_config=ns.storage_config_file,
        include_hidden=ns.include_hidden_topics, use_sim_time=ns.use_sim_time,
    )
    if ns.output:
        path = Path(ns.output).expanduser().absolute()
        config.output_root = str(path.parent)
        config.prefix = path.name
    config.validate()
    return config


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as f:
            tmp = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        if tmp and tmp.exists():
            tmp.unlink()


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ros2-recorder" / "profiles.json"
        self.profiles: dict[str, dict] = {}
        self.last: dict | None = None
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.profiles = data.get("profiles", {})
            self.last = data.get("last")
            if not isinstance(self.profiles, dict):
                raise ValueError("配置列表格式无效")

    def save(self, config: RecordConfig, name: str | None = None):
        config.validate()
        self.last = config.to_dict()
        if name:
            self.profiles[name] = self.last.copy()
        atomic_json(self.path, {"version": 1, "last": self.last, "profiles": self.profiles})
