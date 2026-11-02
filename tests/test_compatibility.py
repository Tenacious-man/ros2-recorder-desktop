import ast
from dataclasses import replace
from pathlib import Path
import os

import pytest

from ros2_recorder.compatibility import RecorderCapabilities
from ros2_recorder.config import RecordConfig, import_command
from ros2_recorder.environment import choose_underlay, clean_ros_environment, installed_environments, inspect_workspace, load_environment


LEGACY_HELP = '''
options:
  --all
  --output OUTPUT
  --storage STORAGE
  --max-bag-size SIZE
  --max-cache-size LIMIT
                        maximum amount of messages to hold in cache
  --compression-mode {none,file}
  --compression-format {zstd}
  --include-hidden-topics
  --qos-profile-overrides-path PATH
'''
MODERN_HELP = '''
options:
  --all
  --output OUTPUT
  --storage {sqlite3,mcap}
  --topics TOPICS
  --max-bag-size SIZE
  --max-bag-duration SECONDS
  --max-cache-size BYTES
                        bytes in each buffer; double buffering
  --regex PATTERN
  --exclude PATTERN
  --compression-mode {none,file,message}
  --compression-format {zstd}
  --compression-queue-size COUNT
  --compression-threads COUNT
  --storage-preset-profile NAME
  --storage-config-file PATH
  --qos-profile-overrides-path PATH
  --include-hidden-topics
  --use-sim-time
'''


def test_legacy_recorder_uses_message_count_without_modern_flags():
    caps = RecorderCapabilities.from_help(LEGACY_HELP, {'sqlite3'})
    assert caps.cache_unit == 'messages'
    args = caps.command(RecordConfig(cache_messages=150), '/data/bag')
    assert args[args.index('--max-cache-size') + 1] == '150'
    assert '--max-bag-duration' not in args
    assert '--storage-preset-profile' not in args
    args = caps.command(RecordConfig(compression='file'), '/data/bag')
    assert '--compression-threads' not in args and '--compression-queue-size' not in args


@pytest.mark.parametrize('update', [{'split_seconds': 60}, {'use_sim_time': True}, {'preset': 'resilient'}, {'compression': 'message'}, {'storage': 'mcap'}, {'regex': 'camera', 'all_topics': False}])
def test_unsupported_active_features_are_not_silently_discarded(update):
    caps = RecorderCapabilities.from_help(LEGACY_HELP, {'sqlite3'})
    with pytest.raises(ValueError):
        caps.command(RecordConfig(**update), '/data/bag')


def test_newer_topic_option_and_cache_bytes():
    caps = RecorderCapabilities.from_help(MODERN_HELP)
    assert caps.cache_unit == 'bytes' and caps.double_buffered
    config = RecordConfig(all_topics=False, topics=['/camera'], cache_bytes=1000000000)
    args = caps.command(config, '/data/bag')
    assert args[args.index('--topics') + 1] == '/camera'
    assert args[args.index('--max-cache-size') + 1] == '1000000000'


def test_legacy_command_import_does_not_interpret_message_count_as_bytes():
    config = import_command('ros2 bag record -a --max-cache-size 200', cache_unit='messages')
    assert config.cache_messages == 200 and config.cache_bytes == -1
    assert import_command('ros2 bag record -a', cache_unit='messages').cache_messages == 0


def test_loaded_ros_environment_takes_precedence_over_other_installs(tmp_path):
    loaded = {'ROS_VERSION': '2', 'ROS_DISTRO': 'selected', 'AMENT_PREFIX_PATH': '/custom/selected'}
    assert choose_underlay(loaded, [tmp_path / 'one', tmp_path / 'two']) is None
    assert choose_underlay({'ROS_VERSION': '2', 'AMENT_PREFIX_PATH': '/custom/install'}, []) is None
    with pytest.raises(ValueError, match='多个'):
        choose_underlay({}, [tmp_path / 'one', tmp_path / 'two'])
    assert choose_underlay({}, [tmp_path / 'only']) == tmp_path / 'only'


def test_installed_environment_scan_excludes_ros1(tmp_path):
    for name, ros2 in [('ros1', False), ('ros2', True)]:
        prefix = tmp_path / name
        prefix.mkdir()
        (prefix / 'setup.bash').touch()
        if ros2:
            (prefix / 'bin').mkdir()
            (prefix / 'bin' / 'ros2').touch()
    assert installed_environments(tmp_path) == [tmp_path / 'ros2' / 'setup.bash']


def test_explicit_base_switch_removes_previous_ros_paths():
    env = {'ROS_DISTRO': 'old', 'ROS_VERSION': '2', 'ROS_DOMAIN_ID': '18',
           'AMENT_PREFIX_PATH': '/opt/ros/old:/data/old_ws/install',
           'PATH': '/opt/ros/old/bin:/usr/bin:/data/old_ws/install/bin',
           'PYTHONPATH': '/opt/ros/old/lib/python3.8/site-packages:/other/python',
           'LD_LIBRARY_PATH': '/opt/ros/old/lib:/usr/local/cuda/lib64'}
    clean = clean_ros_environment(env)
    assert clean['PATH'] == '/usr/bin'
    assert clean['PYTHONPATH'] == '/other/python'
    assert clean['LD_LIBRARY_PATH'] == '/usr/local/cuda/lib64'
    assert clean['ROS_DOMAIN_ID'] == '18' and 'ROS_DISTRO' not in clean


def test_overlay_cannot_silently_switch_ros_distribution(tmp_path):
    setup = tmp_path / 'setup.bash'
    setup.write_text('export ROS_DISTRO=other\n')
    base = load_environment()
    with pytest.raises(ValueError, match='版本不同'):
        load_environment(str(setup), base)


def test_build_cache_is_separate_for_each_ros_distribution(tmp_path):
    package = tmp_path / 'src' / 'msgs'
    package.mkdir(parents=True)
    (package / 'package.xml').write_text('<package><name>msgs</name></package>')
    assert inspect_workspace(str(tmp_path), {'ROS_DISTRO': 'one'}).build_root != inspect_workspace(str(tmp_path), {'ROS_DISTRO': 'two'}).build_root


def test_application_uses_python38_compatible_syntax():
    root = Path(__file__).resolve().parents[1]
    for path in [*root.joinpath('ros2_recorder').glob('*.py'), root / 'install_launcher.py']:
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 8))
