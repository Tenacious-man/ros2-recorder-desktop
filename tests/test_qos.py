from dataclasses import replace

import pytest
import yaml

from ros2_recorder.config import RecordConfig
from ros2_recorder.recording import prepare_recording_qos, validate_recording_qos


@pytest.mark.parametrize('policy', ['reliable', 'best_effort'])
def test_forced_recording_qos_generates_explicit_topic_overrides(tmp_path, policy):
    config = RecordConfig(all_topics=False, topics=['/camera', '/lidar'], recording_reliability=policy)
    effective = prepare_recording_qos(config, tmp_path)
    assert yaml.safe_load((tmp_path / 'recording_qos.yaml').read_text()) == {
        '/camera': {'reliability': policy, 'history': 'keep_last', 'depth': 10},
        '/lidar': {'reliability': policy, 'history': 'keep_last', 'depth': 10}}
    args = effective.argv(tmp_path / 'bag')
    assert args[args.index('--qos-profile-overrides-path') + 1] == str(tmp_path / 'recording_qos.yaml')
    assert RecordConfig.from_dict(config.to_dict()).recording_reliability == policy
    assert config.qos_path == ''
    with pytest.raises(ValueError, match='生成'):
        config.argv(tmp_path / 'bag')


def test_automatic_and_custom_yaml_keep_existing_behavior(tmp_path):
    config = RecordConfig(qos_path='/some/custom.yaml')
    assert prepare_recording_qos(config, tmp_path) is config
    assert not (tmp_path / 'recording_qos.yaml').exists()
    with pytest.raises(ValueError, match='YAML'):
        replace(config, all_topics=False, topics=['/lidar'], recording_reliability='reliable').validate()


def test_reliable_recording_rejects_mixed_publishers_only_for_selected_topics():
    config = RecordConfig(all_topics=False, topics=['/lidar'], recording_reliability='reliable')
    validate_recording_qos(config, [{'name': '/other', 'qos': 'BEST_EFFORT'}])
    validate_recording_qos(config, [{'name': '/lidar', 'qos': 'RELIABLE'}])
    with pytest.raises(ValueError, match='/lidar'):
        validate_recording_qos(config, [{'name': '/lidar', 'qos': 'BEST_EFFORT, RELIABLE'}])
    validate_recording_qos(replace(config, recording_reliability='auto'),
                           [{'name': '/lidar', 'qos': 'BEST_EFFORT'}])
