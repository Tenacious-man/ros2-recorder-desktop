import shlex

import pytest

from ros2_recorder.config import MIB, ProfileStore, RecordConfig, import_command
from ros2_recorder.environment import build_command, inspect_workspace, load_environment


def test_import_keeps_non_mib_byte_values_and_quoted_paths(tmp_path):
    config = import_command('ros2 bag record /camera/image /radar/targets -s mcap --max-cache-size 1000000000 -b 4000000000 --storage-preset-profile fastwrite -o "/mnt/my disk/capture"')
    args = config.argv(tmp_path / 'bag')
    assert args[args.index('--max-cache-size') + 1] == '1000000000'
    assert args[args.index('--max-bag-size') + 1] == '4000000000'
    assert config.output_root == '/mnt/my disk'
    assert import_command(shlex.join(args)).to_dict() == RecordConfig.from_dict({**config.to_dict(), 'output_root': str(tmp_path), 'prefix': 'bag'}).to_dict()


@pytest.mark.parametrize('command', [
    'ros2 bag record -a --snapshot-mode',
    'ros2 bag record -a ; touch /tmp/should-not-be-created',
    'ros2 bag record -a /chatter',
    'ros2 bag record /chatter --exclude radar',
    'ros2 bag record -a -s mcap --compression-mode message',
])
def test_reject_unsupported_or_conflicting_imports(command):
    with pytest.raises(ValueError):
        import_command(command)


def test_profile_roundtrip_preserves_environment(tmp_path):
    store = ProfileStore(tmp_path / 'profiles.json')
    config = RecordConfig(workspace='/data/radar_ws', packages=['radar_msgs'], environment_setup='/data/radar_ws/install/setup.bash')
    store.save(config, '毫米波')
    loaded = ProfileStore(store.path)
    assert RecordConfig.from_dict(loaded.profiles['毫米波']) == config


@pytest.mark.parametrize('update', [{'cache_mib': True}, {'packages': ['bad;name']}, {'min_free_gib': float('nan')}, {'cache_bytes': 2**32}, {'split_bytes': 10}, {'all_topics': 'true'}])
def test_untrusted_profile_validation(update):
    with pytest.raises(ValueError):
        RecordConfig.from_dict({**RecordConfig().to_dict(), **update})


def test_environment_paths_are_arguments_not_shell_fragments(tmp_path):
    setup = tmp_path / "space ' and ; shell.bash"
    setup.write_text('export RECORDER_ENV_SENTINEL="literal value"\nprintf "a setup message\\n"\n')
    env = load_environment(str(setup))
    assert env['RECORDER_ENV_SENTINEL'] == 'literal value'


def test_workspace_discovers_interfaces_and_keeps_build_outputs_outside_source(tmp_path):
    package = tmp_path / 'src' / 'radar_msgs'
    (package / 'msg').mkdir(parents=True)
    (package / 'msg' / 'Target.msg').write_text('float32 range\n')
    (package / 'package.xml').write_text('<package><name>radar_msgs</name></package>')
    workspace = inspect_workspace(str(tmp_path))
    assert workspace.packages[0]['interfaces'] == ['msg/Target.msg']
    import os
    assert os.path.commonpath([str(workspace.build_root), str(tmp_path)]) != str(tmp_path)
    argv = build_command(workspace, ['radar_msgs'])
    assert '--packages-up-to' in argv
    assert argv[argv.index('--base-paths') + 1] == str(tmp_path / 'src')
