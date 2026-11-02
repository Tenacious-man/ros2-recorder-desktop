"""Qt bridges for off-main-thread operations."""
import threading
from PyQt5.QtCore import QObject, pyqtSignal


class Task(QObject):
    log = pyqtSignal(str)
    result = pyqtSignal(object)
    failed = pyqtSignal(str)
    ended = pyqtSignal()

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            self.result.emit(self.function(self.log.emit))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.ended.emit()


class Signals(QObject):
    line = pyqtSignal(str)
    done = pyqtSignal(object)
    frequency = pyqtSignal(str, object)
    monitor_done = pyqtSignal(str, int)
    build_done = pyqtSignal(int)
