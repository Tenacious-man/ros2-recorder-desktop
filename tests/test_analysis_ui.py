import csv
import json
import os
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt5.QtCore import Qt, QPoint, QPointF
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from ros2_recorder.app import MainWindow, configure_style
from ros2_recorder.analysis_page import AnalysisPage
from ros2_recorder.environment import load_environment
from test_analysis import T, NS, write_bag, write_header_bag


def pump(app, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        time.sleep(.01)
        assert time.monotonic() < deadline, 'UI operation timed out'
    app.processEvents()


def make_window(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setattr(MainWindow, '_initialize_environment', lambda *args: None)
    # Offline statistics remain a separate window; test that page explicitly.
    monkeypatch.setattr('ros2_recorder.app.LiveStampPage', AnalysisPage)
    app = QApplication.instance() or QApplication([])
    configure_style(app)
    window = MainWindow(auto_discover=False)
    window.timer.stop()
    window.active_env = load_environment()
    window.nav.setCurrentRow(4)
    window.show()
    return app, window


def test_focused_metrics_header_time_comparison_zoom_export_and_cancel(tmp_path, monkeypatch):
    app, window = make_window(tmp_path, monkeypatch)
    page = window.analysis_widget
    origin = 7 * NS + 123
    bag = write_header_bag(tmp_path / 'bag', 'mcap',
        [('/sensor', T + i * NS // 10, origin + value) for i, value in enumerate([0, NS // 10, NS // 10, NS])]
        + [('/sensor2', T + NS, origin), ('/sensor2', T + 2 * NS, origin + NS // 2)],
        window.active_env)
    try:
        page.open_bag(bag)
        assert page.busy and not page.scan_button.isEnabled()
        errors = []
        window._error = errors.append
        window.start_recording()
        assert window.session is None and errors and '分析' in errors[-1]
        pump(app, lambda: not page.busy)
        assert page.dataset and page.report is None, 'Opening a bag must not scan every topic'
        page.topics.blockSignals(True)
        for name, item in page.topic_items.items():
            item.setCheckState(0, Qt.Checked if name in ('/sensor', '/sensor2') else Qt.Unchecked)
        page.topics.blockSignals(False)
        page.show_topics()
        page.scan()
        pump(app, lambda: not page.busy)
        assert page.report and page.report['message_count'] == 6, page.status.value
        page.topic_items['/sensor2'].setCheckState(0, Qt.Unchecked)
        assert page.report['time_source'] == 'header_stamp'
        assert window.stack.count() == 5
        assert list(page.metric_buttons) == ['frequency', 'count', 'maximum', 'minimum', 'timestamps']
        assert page.checked_names() == ['/sensor']
        assert '2.00' in page.metric_summary.value  # Header interval frequency, not 10 Hz bag frequency.
        for key, value in [('count', '4'), ('maximum', '900.00'), ('minimum', '0.00')]:
            page.metric_buttons[key].click()
            assert page.chart.metric == key and value in page.metric_summary.value
            page.chart.grab()
        page.metric_buttons['timestamps'].click()
        pump(app, lambda: not page.query_running and page.chart.stamps.get('/sensor', {}).get('total') == 4)
        page.chart.view = (.05, .15)
        page.chart.rangeChanged.emit()
        pump(app, lambda: not page.query_running and page.chart.stamps.get('/sensor', {}).get('total') == 2)
        page.chart.cursor = .1
        page.chart.describe_cursor()
        assert '7.100000123' in page.cursor_text.value
        page.chart.grab()
        page.set_metric('frequency')
        page.chart.reset_view()
        low, high = page.chart.view
        pos = page.chart.area().center().toPoint()
        wheel = QWheelEvent(QPointF(pos), QPointF(page.chart.mapToGlobal(pos)), QPoint(), QPoint(0, 120),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        app.sendEvent(page.chart, wheel)
        assert page.chart.view[1] - page.chart.view[0] == pytest.approx((high - low) * .75)
        QTest.mouseDClick(page.chart, Qt.LeftButton, pos=pos)
        assert page.chart.view == page.chart.bounds()
        page.topic_items['/sensor2'].setCheckState(0, Qt.Checked)
        assert len(page.chart.topics) == 2
        # A real checkbox click can deselect a topic; text selection does not recheck it.
        item = page.topic_items['/sensor2']
        rect = page.topics.visualItemRect(item)
        QTest.mouseClick(page.topics.viewport(), Qt.LeftButton, pos=QPoint(rect.left() + 9, rect.center().y()))
        assert item.checkState(0) == Qt.Unchecked
        page.search.setText('sensor2')
        assert page.topic_items['/sensor'].isHidden()
        assert page.topic_items['/sensor'].checkState(0) == Qt.Checked
        page.search.clear()
        window.resize(1080, 740)
        app.processEvents()
        assert page.chart.geometry().bottom() <= page.chart.parentWidget().rect().bottom()
        assert page.chart.height() >= 230
        output = tmp_path / 'export.json'
        page.start_export(output)
        assert page.busy
        pump(app, lambda: not page.busy and not window.tasks)
        data = json.loads(output.read_text())
        assert data['message_count'] == 6
        with output.with_name('export_anomalies.csv').open(encoding='utf-8-sig') as stream:
            events = list(csv.DictReader(stream))
        assert len(events) == 1 and events[0]['time_source'] == 'header_stamp'
        previous = page.report
        page.scan()
        cancelled_job = page.job
        cancelled_dir = cancelled_job.directory
        page.cancel()
        pump(app, lambda: not page.busy)
        assert page.report is previous and page.export_button.isEnabled()
        assert not cancelled_dir.exists() and cancelled_job.runner.wait(1)
        assert cancelled_job.runner.process.poll() is not None
        assert '已取消' in page.status.value
        event = threading.Event()
        event.set()
        with pytest.raises(ValueError, match='取消'):
            page.export_report(output, event)
        assert json.loads(output.read_text()) == data
        assert not list(tmp_path.glob('.analysis-export-*'))
    finally:
        window.close()
        pump(app, lambda: not window.isVisible())


def test_failed_open_retains_previous_dataset_and_close_cleans_worker(tmp_path, monkeypatch):
    app, window = make_window(tmp_path, monkeypatch)
    page = window.analysis_widget
    bag = write_header_bag(tmp_path / 'bag', 'sqlite3', [('/sensor', T, 7 * NS)], window.active_env)
    page.open_bag(bag)
    pump(app, lambda: not page.busy)
    assert page.dataset, page.status.value
    previous = page.dataset
    page.open_bag(tmp_path / 'missing')
    pump(app, lambda: not page.busy)
    assert page.dataset is previous and '操作失败' in page.status.value
    page.scan()
    job = page.job
    directory = job.directory
    window.close()
    pump(app, lambda: not window.isVisible())
    assert not page.busy and job.runner.wait(1)
    assert not directory.exists()


def test_history_empty_bag_and_missing_header_do_not_use_bag_time(tmp_path, monkeypatch):
    app, window = make_window(tmp_path, monkeypatch)
    page = window.analysis_widget
    bag = write_bag(tmp_path / 'bag', 'sqlite3', [], window.active_env)
    window._selected_session = lambda: {'bag_path': str(bag)}
    try:
        window.nav.setCurrentRow(1)
        window.analyze_selected_session()
        pump(app, lambda: not page.busy)
        assert window.nav.currentRow() == 4
        assert page.dataset['message_count'] == 0
        page.scan()
        pump(app, lambda: not page.busy)
        assert page.report and page.report['message_count'] == 0
        errors = []
        window._error = errors.append
        class Session:
            finished = False
        window.session = Session()
        page.open_bag(bag)
        assert not page.busy and '录制' in errors[-1]
        window.session = None
        no_header = write_header_bag(tmp_path / 'no header', 'sqlite3', [('/no_header', T, 0), ('/no_header', T + NS, 0)], window.active_env)
        page.open_bag(no_header)
        pump(app, lambda: not page.busy)
        page.topics.blockSignals(True)
        for name, item in page.topic_items.items():
            item.setCheckState(0, Qt.Checked if name == '/no_header' else Qt.Unchecked)
        page.topics.blockSignals(False)
        page.show_topics()
        page.scan()
        pump(app, lambda: not page.busy)
        assert page.results['/no_header']['count'] == 2
        assert page.results['/no_header']['interval_hz'] is None
        assert '不含 header' in page.status.value
        page.set_metric('count')
        assert '2' in page.metric_summary.value
    finally:
        window.session = None
        window.close()
        pump(app, lambda: not window.isVisible())
