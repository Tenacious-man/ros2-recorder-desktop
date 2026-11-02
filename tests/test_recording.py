import json
import os
from pathlib import Path
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
import yaml

from ros2_recorder.config import RecordConfig
from ros2_recorder.recording import RecordingSession, make_report


def metadata(path, count=4):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information': {
        'message_count': count, 'duration': {'nanoseconds': 2_000_000_000},
        'storage_identifier': 'sqlite3', 'topics_with_message_count': [{
            'topic_metadata': {'name': '/radar', 'type': 'radar_msgs/msg/Target'}, 'message_count': count,
        }],
    }}))


def test_report_does_not_claim_integrity_from_metadata(tmp_path):
    metadata(tmp_path)
    report = make_report(tmp_path, 0, True, [])
    assert report['status'] == 'completed'
    assert report['topics'][0]['count'] == 4
    assert report['topics'][0]['bag_average_hz'] == 2
    assert report['integrity'].startswith('未验证')


def test_nonzero_exit_and_forced_stop_never_report_success(tmp_path):
    metadata(tmp_path)
    assert make_report(tmp_path, 1, True, [])['status'] == 'failed'
    assert make_report(tmp_path, 0, True, [], forced=True)['status'] == 'failed'
    assert make_report(tmp_path / 'missing', 0, True, [])['status'] == 'failed'


def test_low_disk_requests_graceful_stop_without_deleting_data(tmp_path, monkeypatch):
    session = RecordingSession(RecordConfig(output_root=str(tmp_path), min_free_gib=2), {})
    session.output = tmp_path / 'bag'
    session.output.mkdir()
    (session.output / 'keep.db3').write_bytes(b'abc')
    session.root_device = tmp_path.stat().st_dev
    session.started_at = time.monotonic()
    monkeypatch.setattr('ros2_recorder.recording.shutil.disk_usage', lambda _: type('Usage', (), {'free': 1})())
    session.metrics()
    assert session.stopped
    assert not session.forced
    assert (session.output / 'keep.db3').read_bytes() == b'abc'


def test_configuration_is_snapshotted_before_later_edits(tmp_path):
    config = RecordConfig(output_root=str(tmp_path))
    session = RecordingSession(config, {})
    config.cache_mib = 1024
    assert session.config.cache_mib == 512


@pytest.mark.parametrize('existing', [None, 'bag', 'report'])
def test_recording_names_use_time_and_only_add_numbers_on_collision(tmp_path, monkeypatch, existing):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 10, 30, 17, 43, 6)

    class FakeRunner:
        def __init__(self, argv, env, **kwargs):
            self.argv = argv

        def start(self):
            pass

    monkeypatch.setattr('ros2_recorder.recording.datetime', FixedDatetime)
    monkeypatch.setattr('ros2_recorder.recording.ProcessRunner', FakeRunner)
    base = 'lidar_20261030_174306'
    marker = None
    if existing:
        path = tmp_path / base if existing == 'bag' else tmp_path / '.recorder_sessions' / base
        path.mkdir(parents=True)
        marker = path / 'keep.txt'
        marker.write_text('existing recording')
    caps = SimpleNamespace(command=lambda config, output: config.argv(output))
    sessions = []
    try:
        for index in range(2):
            session = RecordingSession(RecordConfig(output_root=str(tmp_path), prefix='lidar', min_free_gib=0),
                                       {}, capabilities=caps)
            sessions.append(session)
            session.start()
            number = index + int(existing is not None)
            expected = base if number == 0 else f'{base}_{number}'
            assert session.output == tmp_path / expected
            assert session.session_dir == tmp_path / '.recorder_sessions' / expected
            manifest = json.loads((session.session_dir / 'session.json').read_text())
            assert manifest['bag_path'] == str(session.output)
            assert str(session.output) in session.runner.argv
        assert sessions[0].output != sessions[1].output
        if marker:
            assert marker.read_text() == 'existing recording'
    finally:
        for session in sessions:
            if session.log_file:
                session.log_file.close()
