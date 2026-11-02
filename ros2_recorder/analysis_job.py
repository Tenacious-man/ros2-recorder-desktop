"""Qt lifecycle for an isolated, cancellable bag worker."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .environment import python_executable
from .processes import ProcessRunner


class AnalysisJob(QObject):
    progress = pyqtSignal(object)
    finished = pyqtSignal()
    _exited = pyqtSignal(int)

    def __init__(self, action, request, env, parent=None):
        super().__init__(parent)
        self.temporary = tempfile.TemporaryDirectory(prefix='ros2-analysis-')
        self.directory = Path(self.temporary.name)
        request_file = self.directory / 'request.json'
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        self.action = action
        self.result = None
        self.error = ''
        self.running = False
        self.cancelled = False
        self.logs = []
        self._exited.connect(self._done)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(lambda: self.runner.stop(force=True))
        self.runner = ProcessRunner([python_executable(env), str(Path(__file__).with_name('analysis.py')),
                                     action, str(request_file), str(self.directory)], env,
                                    line=self._line, done=self._exited.emit)

    def start(self):
        self.running = True
        self.runner.start()

    def _line(self, line):
        if line.startswith('@@ANALYSIS@@'):
            try:
                event = json.loads(line[len('@@ANALYSIS@@'):])
                if 'progress' in event:
                    self.progress.emit(event['progress'])
                if 'error' in event:
                    self.error = event['error']
            except (ValueError, TypeError):
                self.error = '分析进程返回了无效数据。'
        else:
            self.logs.append(line[-2000:])
            self.logs = self.logs[-12:]

    def _done(self, code):
        self.kill_timer.stop()
        self.running = False
        if not self.cancelled and code == 0 and not self.error:
            try:
                self.result = json.loads((self.directory / 'result.json').read_text(encoding='utf-8'))
            except Exception as exc:
                self.error = f'读取分析结果失败：{exc}'
        elif not self.cancelled and not self.error:
            self.error = f'分析进程退出（{code}）：' + '\n'.join(self.logs)
        self.finished.emit()

    def cancel(self):
        if self.running:
            self.cancelled = True
            self.runner.stop()
            self.kill_timer.start(3000)

    def cleanup(self):
        if self.running:
            raise RuntimeError('分析进程未退出，不能删除工作目录。')
        self.temporary.cleanup()
        self.deleteLater()
