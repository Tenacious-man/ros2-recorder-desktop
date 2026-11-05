"""Session lifecycle, immutable inputs, monitoring and metadata reports."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import threading
import time

import yaml

from .config import MIB, RecordConfig, atomic_json
from .environment import environment_summary, inspect_workspace
from .processes import ProcessRunner
from .compatibility import detect_capabilities


def folder_size(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.iterdir() if item.is_file())
    except OSError:
        return 0


def validate_files(config: RecordConfig):
    for label, raw in (("QoS", config.qos_path if config.qos_enabled else ""),
                       ("存储", config.storage_config if config.storage_config_enabled else "")):
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 2 * MIB:
            raise ValueError(f"{label} 配置文件不存在，或超过 2 MiB")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{label} 配置必须是 YAML 对象")


def prepare_recording_qos(config, directory):
    """Materialize explicit-topic overrides; leave automatic/custom YAML behavior intact."""
    config.validate()
    if config.recording_reliability == 'auto':
        return config
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / 'recording_qos.yaml'
    payload = {name: {'reliability': config.recording_reliability,
                      'history': 'keep_last', 'depth': 10}
               for name in config.topics}
    destination.write_text(yaml.safe_dump(payload, sort_keys=True), encoding='utf-8')
    return replace(config, recording_reliability='auto', qos_path=str(destination), qos_enabled=True)


def validate_recording_qos(config, topics):
    if config.recording_reliability != 'reliable':
        return
    incompatible = [t['name'] for t in topics if t['name'] in config.topics
                    and 'BEST_EFFORT' in t.get('qos', '')]
    if incompatible:
        raise ValueError('以下话题存在 Best Effort 发布端，不能强制 Reliable 录制：\n'
                         + '\n'.join(incompatible) + '\n请选择自动适配或 Best Effort。')


def make_report(output: Path, code: int, stopped: bool, warnings: list[str], *, forced=False) -> dict:
    metadata_file = output / "metadata.yaml"
    result = {"bag_path": str(output), "returncode": code, "stop_requested": stopped,
              "forced_stop": forced, "integrity": "未验证：需与发送端计数、序号或时间戳核对",
              "warnings": list(warnings), "topics": [], "size_bytes": folder_size(output),
              "metadata_present": metadata_file.exists(), "message_count": 0,
              "duration_seconds": 0, "status": "failed"}
    if metadata_file.exists():
        data = yaml.safe_load(metadata_file.read_text(encoding="utf-8"))
        info = data["rosbag2_bagfile_information"]
        result["message_count"] = int(info.get("message_count", 0))
        duration = int(info.get("duration", {}).get("nanoseconds", 0)) / 1e9
        result["duration_seconds"] = duration
        result["storage_identifier"] = info.get("storage_identifier", "")
        for item in info.get("topics_with_message_count", []):
            topic = item["topic_metadata"]
            count = int(item.get("message_count", 0))
            result["topics"].append({"name": topic["name"], "type": topic["type"], "count": count,
                                      "bag_average_hz": count / duration if duration > 0 else None})
        if not forced and code == 0:
            result["status"] = "completed" if result["message_count"] else "empty"
    return result


class RecordingSession:
    def __init__(self, config: RecordConfig, env: dict, *, on_line=lambda _: None, on_done=lambda _: None, capabilities=None):
        # Profiles/widgets may change later; retain the exact recording inputs.
        self.config = RecordConfig.from_dict(config.to_dict())
        self.env = dict(env)
        self.capabilities = capabilities
        self.on_line, self.on_done = on_line, on_done
        self.runner: ProcessRunner | None = None
        self.output: Path | None = None
        self.session_dir: Path | None = None
        self.started_at: float | None = None
        self.stopped = False
        self.forced = False
        self.finished = False
        self.warnings: list[str] = []
        self.log_file = None
        self._log_failed = False
        self._previous = (time.monotonic(), 0)

    def start(self):
        self.config.validate()
        validate_files(self.config)
        if self.capabilities is None:
            self.capabilities = detect_capabilities(self.env)
        root = Path(self.config.output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.root_device = root.stat().st_dev
        free = shutil.disk_usage(root).free
        if free < self.config.min_free_gib * 1024**3:
            raise ValueError("保存目录可用空间低于设定的预留空间，请清理磁盘或修改阈值")
        base_name = f"{self.config.prefix}_{datetime.now():%Y%m%d_%H%M%S}"
        sequence = 0
        while True:
            name = base_name if sequence == 0 else f"{base_name}_{sequence}"
            output = root / name
            session_dir = root / ".recorder_sessions" / name
            sequence += 1
            if output.exists() or output.is_symlink():
                continue
            try:
                # Reserving the report directory also prevents two instances
                # choosing the same name before rosbag creates its output.
                session_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            self.output, self.session_dir = output, session_dir
            break
        effective = replace(self.config)
        for field in ("qos_path", "storage_config"):
            raw = getattr(effective, field) if getattr(effective, "qos_enabled" if field == "qos_path" else "storage_config_enabled") else ""
            if raw:
                destination = self.session_dir / f"{field}.yaml"
                shutil.copyfile(Path(raw).expanduser().resolve(), destination)
                setattr(effective, field, str(destination))
        effective = prepare_recording_qos(effective, self.session_dir)
        argv = self.capabilities.command(effective, self.output)
        # Save source interface definitions for future decoding on another machine.
        if self.config.workspace:
            workspace = inspect_workspace(self.config.workspace)
            for package in workspace.packages:
                if not package['interfaces']:
                    continue
                for relative in ["package.xml", *package['interfaces']]:
                    src = Path(package['path']) / relative
                    if src.stat().st_size > 2 * MIB:
                        continue
                    target = self.session_dir / "interfaces" / package['name'] / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, target)
        atomic_json(self.session_dir / "session.json", {
            "version": 1, "started_at": datetime.now().astimezone().isoformat(),
            "config": self.config.to_dict(), "argv": argv, "command": shlex.join(argv),
            "environment": environment_summary(self.env, self.config.environment_setup),
            "bag_path": str(self.output), "session_dir": str(self.session_dir), "state": "starting",
        })
        self.log_file = (self.session_dir / "recorder.log").open("w", encoding="utf-8", buffering=1)
        self.started_at = time.monotonic()
        self._previous = (self.started_at, 0)
        self.runner = ProcessRunner(argv, self.env, line=self._line, done=self._done)
        self.runner.start()

    def _line(self, line):
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)
        if re.search(r'\b(warn(?:ing)?|error|fatal)\b|\blost\b|\bdropped\b|cache.*full', clean, re.I):
            # Preserve a bounded warning summary; the full log remains on disk.
            if len(self.warnings) < 1000:
                self.warnings.append(clean)
        try:
            self.log_file.write(clean + "\n")
        except OSError:
            if not self._log_failed:
                self._log_failed = True
                self.warnings.append("录制日志写入失败，已请求停止录制")
                self.stop()
        self.on_line(clean)

    def _done(self, code):
        try:
            self.log_file.close()
            report = make_report(self.output, code, self.stopped, self.warnings, forced=self.forced)
            atomic_json(self.session_dir / "report.json", report)
            manifest = json.loads((self.session_dir / "session.json").read_text(encoding="utf-8"))
            manifest.update(state=report["status"], finished_at=datetime.now().astimezone().isoformat())
            atomic_json(self.session_dir / "session.json", manifest)
        except Exception as exc:
            report = {"status": "failed", "error": f"结果保存/读取失败：{exc}",
                      "bag_path": str(self.output), "topics": [], "returncode": code}
        self.finished = True
        self.on_done(report)

    def stop(self, *, force=False):
        self.stopped = True
        self.forced = self.forced or force
        if self.runner:
            self.runner.stop(force=force)

    def metrics(self) -> dict:
        if self.output and self.output.parent.stat().st_dev != self.root_device:
            raise OSError("保存目录的挂载设备已变化")
        now = time.monotonic()
        size = folder_size(self.output) if self.output else 0
        last_time, last_size = self._previous
        rate = max(0, size - last_size) / max(now - last_time, 0.001)
        self._previous = now, size
        free = shutil.disk_usage(self.output.parent).free if self.output else 0
        if (not self.finished and not self.stopped and self.config.auto_stop_low_disk
                and free < self.config.min_free_gib * 1024**3):
            reason = "磁盘余量低于预留阈值，已请求停止并等待文件写完"
            self.warnings.append(reason)
            self.on_line(reason)
            self.stop()
        return {"elapsed": now - self.started_at if self.started_at else 0, "size": size,
                "growth_bytes_per_sec": rate, "free": free, "warning_count": len(self.warnings)}


def list_sessions(output_root: str) -> list[dict]:
    root = Path(output_root).expanduser() / ".recorder_sessions"
    sessions = []
    if not root.exists():
        return []
    for path in sorted(root.glob("*/session.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get('bag_path'), str) or not isinstance(data.get('config'), dict):
                continue
            report_file = path.with_name("report.json")
            data["report"] = json.loads(report_file.read_text(encoding="utf-8")) if report_file.exists() else None
            report = data['report']
            if report is not None:
                if not isinstance(report, dict) or not isinstance(report.get('topics', []), list):
                    data['report'] = None
                elif any(not isinstance(t, dict) or not all(key in t for key in ('name', 'type', 'count')) for t in report.get('topics', [])):
                    data['report'] = None
            data['session_dir'] = str(path.parent)
            sessions.append(data)
        except (ValueError, OSError):
            continue
    return sessions


def benchmark_directory(directory: str, log=lambda _: None) -> dict:
    """Explicit bounded sequential-write probe; not an assurance of sustained throughput."""
    import tempfile
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    total = 128 * MIB
    if shutil.disk_usage(root).free < total + 1024**3:
        raise ValueError("测速需要至少 1.125 GiB 可用空间")
    block = os.urandom(4 * MIB)
    log("写入 128 MiB 临时数据并同步，结束后自动删除……")
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(prefix=".ros2-recorder-speed-", dir=root) as f:
        for _ in range(total // len(block)):
            f.write(block)
        f.flush()
        os.fsync(f.fileno())
    elapsed = time.monotonic() - started
    return {"mib_per_second": total / MIB / max(elapsed, 0.001), "seconds": elapsed}
