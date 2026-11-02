"""Subprocesses own their ROS environment and process group; no shell command strings."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable


class ProcessRunner:
    def __init__(self, argv: list[str], env: dict[str, str], *, cwd=None,
                 line: Callable[[str], None] = lambda _: None,
                 done: Callable[[int], None] = lambda _: None):
        self.argv, self.env, self.cwd = argv, env, cwd
        self.on_line, self.on_done = line, done
        self.process: subprocess.Popen | None = None
        self.returncode: int | None = None
        self._lock = threading.Lock()
        self._stop = False
        self._signal = signal.SIGINT
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        code = 127
        try:
            env = dict(self.env, PYTHONUNBUFFERED="1", RCUTILS_COLORIZED_OUTPUT="0",
                       RCUTILS_LOGGING_BUFFERED_STREAM="0")
            with self._lock:
                self.process = subprocess.Popen(
                    self.argv, env=env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", start_new_session=True,
                )
                if self._stop:
                    self._send(self._signal)
            for line in self.process.stdout:
                self.on_line(line.rstrip("\n"))
            code = self.process.wait()
        except Exception as exc:
            self.on_line(f"进程异常：{exc}")
            if self.process and self.process.poll() is None:
                self._send(signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._send(signal.SIGKILL)
                    self.process.wait()
        finally:
            self.returncode = code
            if self.process and self.process.stdout:
                self.process.stdout.close()
            self.on_done(code)

    def _send(self, sig):
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass

    def stop(self, *, force=False):
        with self._lock:
            self._stop = True
            self._signal = signal.SIGKILL if force else signal.SIGINT
            self._send(self._signal)

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def pid(self):
        return self.process.pid if self.process else None
