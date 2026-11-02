"""One disposable ROS process measures all requested topics."""
import json
from pathlib import Path
import tempfile

from .config import atomic_json
from .environment import probe_argv
from .processes import ProcessRunner


class FrequencyMonitor:
    def __init__(self, env, topics, *, on_result, on_done):
        self.directory = tempfile.TemporaryDirectory(prefix="ros-recorder-frequency-")
        self.control = Path(self.directory.name) / "topics.json"
        self.stopping = False
        self.update(topics)
        def line(raw):
            if raw.startswith('@@RESULT@@'):
                try:
                    value = json.loads(raw[10:])
                except ValueError:
                    return
                on_result(value.get('topic', ''), value)
        def done(code):
            self.directory.cleanup()
            on_done(code)
        self.runner = ProcessRunner(probe_argv('watch', '--control', str(self.control), env=env), env,
                                    line=line, done=done)

    def update(self, topics):
        atomic_json(self.control, {"topics": dict(topics)})

    def start(self):
        self.runner.start()

    def stop(self, *, force=False):
        self.stopping = True
        self.runner.stop(force=force)

    def wait(self, timeout=None):
        return self.runner.wait(timeout)
