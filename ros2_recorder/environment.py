"""Compile existing interface packages and load overlays in fresh child processes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import xml.etree.ElementTree as ET


@dataclass
class Workspace:
    path: Path
    packages: list[dict]
    build_root: Path

    @property
    def setup(self):
        return self.build_root / "install" / "setup.bash"


def inspect_workspace(raw_path: str, env=None) -> Workspace:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_dir() or not ((path / "src").is_dir() or (path / "package.xml").is_file()):
        raise ValueError("请选择包含 src 的工作空间，或包含 package.xml 的 ROS 2 包目录")
    source = path / "src" if (path / "src").is_dir() else path
    packages = []
    for parent, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in {"build", "install", "log", ".git", "node_modules"} and not d.startswith('.')]
        if "COLCON_IGNORE" in files or "AMENT_IGNORE" in files:
            dirs[:] = []
            continue
        if "package.xml" not in files:
            continue
        package = Path(parent)
        try:
            root = ET.parse(package / "package.xml").getroot()
        except ET.ParseError as exc:
            raise ValueError(f"package.xml 无效：{package}：{exc}") from exc
        name = root.findtext("name", "").strip()
        import re
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
            raise ValueError(f"包名称无效：{package}")
        interfaces = sorted(str(p.relative_to(package)) for folder in ("msg", "srv", "action")
                            for p in (package / folder).glob('*') if p.is_file())
        packages.append({"name": name, "path": str(package), "interfaces": interfaces})
        dirs[:] = []
    if not packages:
        raise ValueError("没有找到可编译的 ROS 2 包")
    if len({p['name'] for p in packages}) != len(packages):
        raise ValueError("工作空间中存在重名包，请先处理后再编译")
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    selected_env = os.environ if env is None else env
    identity = hashlib.sha256((str(path) + '|' + selected_env.get('ROS_DISTRO', '') + '|' + sys.implementation.cache_tag).encode()).hexdigest()[:16]
    return Workspace(path, sorted(packages, key=lambda p: p['name']), cache / "ros2-recorder" / "workspaces" / identity)


def installed_environments(root=None):
    root = Path('/opt/ros') if root is None else Path(root)
    return [path for path in sorted(root.glob('*/setup.bash'))
            if (path.parent / 'bin' / 'ros2').is_file() or (path.parent / 'share' / 'ros2cli' / 'package.xml').is_file()]


def clean_ros_environment(env):
    result = dict(env)
    prefixes = [p for key in ('AMENT_PREFIX_PATH', 'COLCON_PREFIX_PATH', 'CMAKE_PREFIX_PATH')
                for p in env.get(key, '').split(os.pathsep) if p]
    for key in ('PATH', 'PYTHONPATH', 'LD_LIBRARY_PATH', 'AMENT_PREFIX_PATH', 'COLCON_PREFIX_PATH', 'CMAKE_PREFIX_PATH', 'PKG_CONFIG_PATH'):
        if key in result:
            result[key] = os.pathsep.join(p for p in result[key].split(os.pathsep) if p and
                                         not any(p == prefix or p.startswith(prefix.rstrip('/') + '/') for prefix in prefixes))
    for key in ('ROS_DISTRO', 'ROS_VERSION', 'ROS_PYTHON_VERSION', 'ROS_PACKAGE_PATH'):
        result.pop(key, None)
    return result


def choose_underlay(env, candidates=None):
    explicit = env.get('ROS2_RECORDER_SETUP', '')
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f'指定的 ROS 2 环境文件不存在：{path}')
        return path
    if env.get('ROS_VERSION') != '1' and (env.get('ROS_VERSION') == '2' or env.get('ROS_DISTRO')) and env.get('AMENT_PREFIX_PATH'):
        return None
    configured = saved_underlay(env) if candidates is None else None
    if configured is not None:
        return configured
    choices = installed_environments() if candidates is None else list(candidates)
    if len(choices) == 1:
        return choices[0]
    if not choices:
        raise ValueError('未找到 ROS 2 环境。请安装 ROS 2，或在“消息环境”中选择基础环境的 setup.bash。')
    raise ValueError('检测到多个 ROS 2 环境，请在“消息环境”中选择基础环境，或先加载需要的 ROS 2 环境后启动。')


def saved_underlay(env):
    config = Path(env.get('XDG_CONFIG_HOME', str(Path(env.get('HOME', str(Path.home()))) / '.config')))
    try:
        data = json.loads((config / 'ros2-recorder' / 'installation.json').read_text())
        setup = data.get('base_environment_setup') if isinstance(data, dict) else None
        path = Path(setup) if isinstance(setup, str) and setup else None
        return path if path is not None and path.is_file() else None
    except (OSError, ValueError):
        return None


def load_environment(setup: str = "", base: dict | None = None, *, base_setup='') -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    expected_distro = env.get('ROS_DISTRO') if base is not None else None
    if base_setup:
        env = clean_ros_environment(env)
        env['ROS2_RECORDER_SETUP'] = base_setup
        expected_distro = None
    setups = []
    underlay = choose_underlay(env)
    if underlay is not None:
        if env.get('ROS_VERSION') == '1':
            env = clean_ros_environment(env)
        setups.append(str(underlay))
    if setup:
        path = Path(setup).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"环境文件不存在：{path}")
        setups.append(str(path))
    script = 'set -e; for setup_path in "$@"; do source "$setup_path" >&2; done; env -0'
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script,
                             "ros2-recorder-env", *setups], env=env,
                            capture_output=True, timeout=25)
    if result.returncode:
        raise RuntimeError("加载环境失败：" + result.stderr.decode(errors="replace")[-4000:])
    values = {}
    for item in result.stdout.split(b'\0'):
        if b'=' in item:
            key, value = item.split(b'=', 1)
            values[os.fsdecode(key)] = os.fsdecode(value)
    if not values or not values.get("AMENT_PREFIX_PATH") or values.get('ROS_VERSION') == '1':
        raise RuntimeError("没有获得有效 ROS 2 环境，请检查 ROS 2 安装和环境文件")
    if expected_distro and values.get('ROS_DISTRO') != expected_distro:
        raise ValueError('消息工作空间与当前基础 ROS 2 环境的版本不同，请选择匹配的工作空间或切换基础环境')
    return values


def build_command(workspace: Workspace, selected: list[str]) -> list[str]:
    available = {p['name'] for p in workspace.packages}
    if not selected or not set(selected) <= available:
        raise ValueError("请勾选要编译的消息包")
    source = workspace.path / "src" if (workspace.path / "src").is_dir() else workspace.path
    return ["colcon", "--log-base", str(workspace.build_root / "log"), "build",
            "--base-paths", str(source), "--build-base", str(workspace.build_root / "build"),
            "--install-base", str(workspace.build_root / "install"),
            "--packages-up-to", *selected, "--executor", "sequential",
            "--event-handlers", "console_direct+", "--cmake-args", "-DBUILD_TESTING=OFF"]


def python_executable(env=None):
    selected = os.environ if env is None else env
    explicit = selected.get('ROS2_RECORDER_PYTHON')
    return explicit or sys.executable


def probe_argv(*args: str, env=None) -> list[str]:
    return [python_executable(env), str(Path(__file__).with_name("probe.py")), *args]


def discover_topics(env: dict, include_hidden=False) -> list[dict]:
    result = subprocess.run(probe_argv("topics", *( ["--hidden"] if include_hidden else []), env=env),
                            env=env, capture_output=True, text=True, timeout=15)
    events = [json.loads(line[10:]) for line in result.stdout.splitlines() if line.startswith("@@RESULT@@")]
    if result.returncode or not events or "error" in events[-1]:
        detail = events[-1].get("error", "") if events else result.stderr[-2000:]
        raise RuntimeError("发现话题失败：" + detail)
    return events[-1]["topics"]


def environment_summary(env: dict, setup: str) -> dict:
    return {**{k: env.get(k, "") for k in ("ROS_DISTRO", "ROS_DOMAIN_ID", "RMW_IMPLEMENTATION", "ROS_LOCALHOST_ONLY")},
            "setup": setup}


def validate_message_types(env: dict, packages: list[str]) -> list[str]:
    script = '''
import json, sys
import rclpy
from ament_index_python.packages import get_package_prefix
from ament_index_python.resources import get_resource
from rosidl_runtime_py.utilities import get_message
from rclpy.type_support import check_for_type_support
validated = []
for package in sys.argv[1:]:
    get_package_prefix(package)
    try:
        definitions, _ = get_resource('rosidl_interfaces', package)
    except LookupError:
        continue
    names = sorted({package + '/msg/' + __import__('pathlib').Path(p).stem for p in definitions.splitlines()
                    if p.startswith('msg/') and p.endswith(('.msg', '.idl'))})
    for name in names:
        check_for_type_support(get_message(name))
        validated.append(name)
print(json.dumps(validated))
'''
    result = subprocess.run([python_executable(env), "-c", script, *packages], env=env,
                            capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise ValueError("消息环境或类型支持不可用：" + result.stderr[-4000:])
    return json.loads(result.stdout.splitlines()[-1])
