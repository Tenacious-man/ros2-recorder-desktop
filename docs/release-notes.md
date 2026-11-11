# ROS Recorder v0.7.0

本版完善 ROS 2 通信配置与诊断，并支持加载已有的自定义消息编译环境。

## 改动

- **ROS 启动环境**。可配置 RMW、ROS Domain、发现范围和 DDS XML，保存为账户默认或复制终端加载命令。
- **Fast DDS 进程诊断**。按话题查看发送方、接收方和 QoS，按进程核对启动环境、XML 配置与共享内存映射。共享内存映射按段数和每段大小显示。
- **雷达断帧排查**。命令行工具按 DDS 发布时间对比 Best Effort 和 Reliable，记录收帧空档、网口计数、UDP 接收丢包和 PTP 日志。
- **自定义消息环境**。支持直接加载已有的 `install/setup.bash`，也可清除自定义环境并切回基础 ROS 2。录制会尝试归档已安装的自定义消息接口定义。
- **界面与容量显示**。录制缓存和通信容量统一采用十进制 KB、MB 显示，已有方案保留原字节数。改进话题与网口表格的勾选和刷新行为。

## 安装

```bash
sudo apt install ./ros2-recorder-desktop_0.7.0-1_all.deb
```

安装包包含程序和说明。ROS 2、设备驱动、自定义消息包和现场配置由目标机提供。详见 [安装说明](https://github.com/Tenacious-man/ros2-recorder-desktop/blob/v0.7.0/INSTALL.md)。
