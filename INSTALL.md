# 一键安装与依赖配置

## 使用 deb 安装包（移植到其他电脑）

把 `ros2-recorder-desktop_0.5.0-1_all.deb` 复制到目标 Ubuntu 电脑，在安装包所在目录运行：

```bash
sudo apt install ./ros2-recorder-desktop_0.5.0-1_all.deb
```

安装后在应用菜单搜索“ROS 2 录制工作台”，或运行 `ros2-recorder`。使用 `apt install` 会按声明补齐缺少的界面依赖，需要可用的软件源和网络；此 deb 不是包含全部依赖的离线安装包。

ROS 2、传感器驱动和私有消息包不包含在 deb 中。目标机器已有完整 ROS 2 录制环境时可直接启动；需要补齐录制或编译组件时运行：

```bash
ros2-recorder-setup
```

目标机器没有 ROS 2 时运行：

```bash
ros2-recorder-setup --install-ros
```

其他安装选项也可使用，例如 `ros2-recorder-setup --check`、`ros2-recorder-setup --ros-distro jazzy`、`ros2-recorder-setup --workspace "/路径/ros2_ws"`。自定义消息请另行复制消息包/工作空间源码，在目标电脑重新“编译并加载”。

程序文件安装到 `/usr/lib/ros2-recorder`，命令和应用菜单入口由系统管理。deb 不包含录制数据、个人方案或编译缓存。后续升级仍用 `sudo apt install ./新版安装包.deb`；卸载使用 `sudo apt remove ros2-recorder-desktop`，用户的录制数据与个人配置会保留。

如果这台电脑曾经用脚本安装过应用菜单入口，旧的个人入口可能优先于系统入口；安装 deb 后运行一次 `ros2-recorder-setup`，可将个人入口指向已安装的程序。

同一个 `all` 包提供 Python 源码，不绑定 CPU 架构或 ROS 2 发行版；运行依赖使用目标系统的软件包。兼容性目标为 Ubuntu 20.04 / 22.04 / 24.04，目前实际运行验证仍限于开发机 Ubuntu 22.04，其余系统需在目标电脑验证。

重新构建安装包：

```bash
python3 make_deb.py
```

生成目录为 `artifacts/`，同时生成 SHA-256 校验文件；重新发布修改版时可用 `python3 make_deb.py --version 0.5.0-2` 增加包版本。构建使用 `dpkg-deb`，无需管理员权限，不会在本机安装软件。仓库主分支更新应用版本号时，GitHub Actions 会自动构建并发布该版本的 deb 和校验文件。

## 使用源码压缩包

把软件目录复制或解压到目标 Ubuntu 机器，打开该目录的终端运行：

```bash
./install.sh
```

脚本会检测已有 ROS 2，补齐缺少的界面、录制和消息编译依赖，保存基础环境选择，并添加应用菜单入口。已安装的软件包会跳过，不执行系统整体升级，不修改 `.bashrc`，不覆盖录制方案或录制数据。需要安装系统依赖时会通过 `sudo` 请求管理员密码。

如果文件传输后执行权限丢失，可以运行 `bash install.sh`。

## 全新机器没有 ROS 2

```bash
./install.sh --install-ros
```

默认组合随 Ubuntu 版本选择：

| Ubuntu | 默认新装的 ROS 2 |
|---|---|
| 20.04 | Foxy |
| 22.04 | Humble |
| 24.04 | Jazzy |

这仅用于**没有 ROS 2 时的新装选择**；已有可用环境会优先复用。早期系统/发行版的软件源和功能取决于仓库实际可用情况。安装的是录制所需基础组件，不会安装整套桌面、RViz 或传感器驱动。

需要配置软件源时，脚本从 ROS 官方仓库 `ros-infrastructure/ros-apt-source` 获取当前系统对应的配置包，经 HTTPS 下载、核对包名并校验发布元数据提供的摘要，再安装。不会下载远程脚本直接执行。需要目标机器能访问 Ubuntu / ROS 软件源及 GitHub；网络失败时保留已完成步骤，修复后重跑即可。

## 只检查或预览

```bash
./install.sh --check
./install.sh --dry-run
```

- `--check`：不安装、不写配置。检查通过返回 `0`，缺依赖返回 `1`，环境错误返回 `2`。
- `--dry-run`：列出计划，不下载、不安装、不写配置。

## 多版本或自定义 ROS 2

安装多个版本时，明确选择要补依赖的版本：

```bash
./install.sh --ros-distro jazzy
```

选择自己的基础环境：

```bash
./install.sh --ros-setup "/路径/ros2/install/setup.bash"
```

源码安装的 ROS 2 不会被自动替换成另一套二进制安装。脚本补齐通用工具并验证现有录制组件；源码环境本身缺少的 ROS 包需要在该环境中补齐。已有录制配置的显式环境选择会保留，必要时可在软件“消息环境”页切换。

## 同时安装自定义消息包声明的依赖

```bash
./install.sh --workspace "/路径/ros2_ws"
```

也支持单个消息包目录。脚本按 `package.xml` 使用 `rosdep` 安装系统依赖，忽略工作空间中已有的源码包；需要时初始化并更新 rosdep。它不会替你下载不存在的私有消息包或设备驱动源码。依赖安装后，在软件中扫描并“编译并加载”。

首次安装 ROS 2 与工作空间依赖也可合并：

```bash
./install.sh --install-ros --workspace "/路径/ros2_ws"
```

## 可选项与文件位置

`--no-mcap` 表示不额外安装可选 MCAP 包；某些 ROS 2 的默认存储依赖本身可能包含它。当前仓库不提供 MCAP 时，脚本继续配置 SQLite3。

应用菜单入口引用当前软件目录，请保留此目录；移动目录后重跑脚本即可。配置与缓存写入当前用户目录；使用 `sudo` 启动整个脚本时，会优先为原始调用用户创建入口。

安装器记录在 `~/.config/ros2-recorder/installation.json`，录制方案仍保存在 `profiles.json`。无需修改整个系统的默认 ROS 版本。

脚本已在开发机验证依赖检查和配置流程，并通过模拟不同 Ubuntu / ROS 2 组合的安装逻辑测试；20.04、24.04 的全新整机安装仍需在目标机器验证。

## 打包到其他机器

运行 `python3 make_package.py` 生成 `artifacts/ros2-recorder-portable.tar.gz`。压缩包包含程序、安装脚本、说明和测试源码，不包含录制数据、个人配置、缓存或之前发布的网页项目。

在目标机器解压后，进入 `ros2-recorder` 目录，运行 `./install.sh`；没有 ROS 2 则运行 `./install.sh --install-ros`。
