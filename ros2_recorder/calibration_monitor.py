"""Lifecycle and bounded preview transport; ROS never loads into the GUI."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import tempfile

import numpy as np

from .config import atomic_json
from .environment import python_executable
from .processes import ProcessRunner


class CalibrationMonitor:
    def __init__(self, env, settings, *, on_snapshot, on_done):
        self.directory = tempfile.TemporaryDirectory(prefix="recorder-calibration-")
        self.control = Path(self.directory.name) / "control.json"
        self.settings = {}
        self.generation = 0
        self.revision = 0
        self.stopping = False
        self.update(settings)
        diagnostics = deque(maxlen=8)

        def line(raw):
            if not raw.startswith("@@RESULT@@"):
                diagnostics.append(raw[:500])
                return
            try:
                value = json.loads(raw[10:])
                if value.get("generation", self.generation) != self.generation:
                    return
                if value.get("sample"):
                    role = value["role"]
                    if role not in ("input", "reference"):
                        return
                    with np.load(Path(self.directory.name) / (role + ".npz"), allow_pickle=False) as archive:
                        if int(archive["generation"]) != self.generation:
                            return
                        value["cloud"] = {key: archive[key] for key in ("xyz", "fields", "indexes")}
                        value["cloud"].update({key: int(archive[key]) for key in ("stamp_ns", "total")})
                        value["cloud"]["frame"] = str(archive["frame"])
                on_snapshot(value)
            except (ValueError, KeyError, OSError) as exc:
                on_snapshot({"generation": self.generation, "error": "预览数据读取失败：" + str(exc)[:500]})

        def done(code):
            self.directory.cleanup()
            if code and not self.stopping and diagnostics:
                on_snapshot({"error": "订阅进程退出：" + "\n".join(diagnostics)[-2000:]})
            on_done(code)

        argv = [python_executable(env), str(Path(__file__).with_name("calibration_probe.py")),
                "--control", str(self.control), "--directory", self.directory.name]
        self.runner = ProcessRunner(argv, env, line=line, done=done)

    def update(self, settings):
        if self.stopping:
            return
        if settings["topics"] != self.settings.get("topics"):
            self.generation += 1
        self.settings = dict(settings)
        self.revision += 1
        atomic_json(self.control, dict(settings, generation=self.generation, revision=self.revision))

    def start(self):
        self.runner.start()

    def stop(self, *, force=False):
        self.stopping = True
        self.runner.stop(force=force)

    def wait(self, timeout=None):
        return self.runner.wait(timeout)
