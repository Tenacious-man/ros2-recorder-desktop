from dataclasses import replace

import yaml
import pytest

from ros2_recorder.config import RecordConfig, import_command
from ros2_recorder.recording import validate_files, prepare_recording_qos
from ros2_recorder.yaml_templates import ensure_templates


def test_templates_are_yaml_and_preserve_edits(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    paths = ensure_templates()
    assert set(paths) == {'qos', 'sqlite3', 'mcap'}
    assert all(isinstance(yaml.safe_load(p.read_text()), dict) for p in paths.values())
    paths['qos'].write_text('/custom:\n  history: keep_last\n  depth: 20\n')
    assert ensure_templates()['qos'].read_text().startswith('/custom:')


def test_disabled_yaml_is_not_read_or_passed_to_recorder(tmp_path):
    config = RecordConfig(qos_path='/missing/qos.yaml', storage_config='/missing/storage.yaml',
                          qos_enabled=False, storage_config_enabled=False)
    validate_files(config)
    args = config.argv(tmp_path / 'bag')
    assert '--qos-profile-overrides-path' not in args
    assert '--storage-config-file' not in args
    validate_files(RecordConfig.from_dict(config.to_dict()))
    forced = replace(config, all_topics=False, topics=['/lidar'], recording_reliability='reliable')
    assert prepare_recording_qos(forced, tmp_path).qos_enabled
    with pytest.raises(ValueError, match='不存在'):
        validate_files(replace(config, qos_enabled=True))


def test_legacy_and_command_import_keep_explicit_yaml_enabled():
    legacy = RecordConfig.from_dict({'qos_path': '/old/qos.yaml', 'storage_config': '/old/storage.yaml'})
    assert legacy.qos_enabled and legacy.storage_config_enabled
    imported = import_command('ros2 bag record -a --qos-profile-overrides-path /q.yaml --storage-config-file /s.yaml')
    assert imported.qos_enabled and imported.storage_config_enabled
