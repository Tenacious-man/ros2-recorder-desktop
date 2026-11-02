import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt5.QtWidgets import QApplication

from ros2_recorder.summary_dialog import RecordingSummaryDialog


@pytest.mark.parametrize('status, metadata, duration, count, title, frequency', [
    ('completed', True, 10.0, 300, '录制已保存', '30.00 Hz'),
    ('empty', True, 0, 0, '录制结束：未写入消息', '—'),
    ('failed', False, 0, 0, '录制结束：保存异常', '—'),
])
def test_saved_summary_uses_metadata_and_handles_empty_or_failed_reports(
        tmp_path, status, metadata, duration, count, title, frequency):
    app = QApplication.instance() or QApplication([])
    output = tmp_path / 'capture_20261030_102030_abc123'
    output.mkdir()
    report = dict(status=status, metadata_present=metadata, bag_path=str(output),
                  duration_seconds=duration, message_count=count,
                  topics=[dict(name='/camera/image', type='sensor_msgs/msg/Image', count=count,
                               bag_average_hz=count / duration if duration else None)],
                  error='结果保存失败：<metadata>' if status == 'failed' else '')
    dialog = RecordingSummaryDialog(report)
    try:
        assert dialog.windowTitle() == title
        assert dialog.path_editor.text() == str(output)
        assert dialog.path_editor.isReadOnly()
        assert output.name in dialog.summary_text
        assert dialog.topic_table.item(0, 3).text() == frequency
        if metadata:
            assert f'{duration:.3f} 秒' in dialog.summary_text
            assert f'{int(count > 0)} 个有消息 / 1 个已登记' in dialog.summary_text
        else:
            assert '元数据不可用' in dialog.summary_text
            assert '结果保存失败：<metadata>' in dialog.summary_text
        assert dialog.open_button.isEnabled()
        dialog.copy_button.click()
        assert app.clipboard().text() == dialog.summary_text
    finally:
        dialog.close()
