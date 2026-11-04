"""Keep one QApplication alive across native widget tests.

Qt supports a single application per process. Function-local applications can
otherwise be destroyed before retained widget/signal wrappers during teardown.
"""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    application = QApplication.instance() or QApplication([])
    yield application
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
