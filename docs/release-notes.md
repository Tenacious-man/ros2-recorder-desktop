# ROS Recorder v0.6.0

本版新增多网口设备授时和通信环境管理，并扩展实时点云、图像等话题的时间戳对照。

## 改动

- **设备授时**。管理员可勾选多个具备硬件 PTP 时间戳和独立 PHC 的网口，逐口配置 PTP 域、UDPv4 或 L2、标准 PTP 时标或 UTC 兼容模式。延时机制为 E2E。
- **系统服务与开机自启**。保存后由 Linux 授权创建各网口的 systemd 服务。可以独立启停和关闭自启，服务异常退出后会自动重试。录制准备和录制期间禁止切换授时。
- **授时检查**。显示网口链路、PTP 主端口状态、网卡时钟偏差、Sync 发送和 Delay_Req 接收情况。填入 Seyond Falcon 地址后可查询设备报告的 PTP 锁定状态。
- **实时消息时间戳**。新增本机当前时间和每条消息的本机接收时间，可用本机接收时刻或另一话题的最新 header 时间作为比较基准。
- **通信对照与诊断**。支持调整监听 QoS、队列深度和监听时长，保存本轮作为基线并比较帧龄及接收间隔。管理员可配置 Fast DDS 共享内存和 UDP、导出配置，查看本机 ROS 进程、中间件和话题 QoS。

## 安装

```bash
sudo apt install ./ros2-recorder-desktop_0.6.0-1_all.deb
```

授时功能需要 LinuxPTP、ethtool 和支持 pkexec 的 Linux 桌面授权环境。Falcon 设备状态查询另需 curl。按 [安装说明](https://github.com/Tenacious-man/ros2-recorder-desktop/blob/v0.6.0/INSTALL.md) 补齐目标机所需依赖。

## 实机验证与范围

在 Ubuntu 22.04、ROS 2 Humble 上验证了两路硬件 PTP 并行运行、逐口开机自启配置、报文计数和雷达端状态。PTP 端口处于 MASTER、报文持续发送与雷达端锁定是分别验证的状态。实时帧龄反映采样时间与应用接收时间之差，包含扫描、处理及传输耗时。

安装包包含程序和说明。ROS 2、设备驱动、私有消息包和现场系统配置由目标机器提供。
