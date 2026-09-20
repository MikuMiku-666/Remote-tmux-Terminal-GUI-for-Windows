# Remote tmux Terminal GUI for Windows v31

![Remote tmux Terminal GUI](UI.jpg)

Remote tmux Terminal GUI 是一个轻量级 Windows 图形终端。Windows 客户端通过
OpenSSH 端口转发连接 Linux 服务端，Linux 端使用 tmux 保存终端和正在运行的任务。

关闭 Windows 客户端只会断开显示，不会停止训练、服务或其他远程命令。重新连接后，
可以继续查看并操作已有 tmux 终端。

## 主要功能

- 使用 Windows 自带的 `ssh.exe` 建立安全的本地端口转发；
- Linux 端通过 tmux 保存持久终端；
- WebSocket 实时推送终端输出；
- 断线重连后自动恢复已有终端；
- 支持多个终端标签页和批量关闭远程终端；
- 支持直接在黑色终端区域输入，也支持底部命令输入框；
- 支持 Enter、Backspace、Tab、方向键、Home、End、Delete、Esc；
- 支持 Ctrl-C、Ctrl-D、Ctrl-Z、Ctrl-L、Ctrl-V 等常用组合键；
- 支持 Windows 剪贴板复制和粘贴；
- 支持设置新终端的默认 Linux 工作目录；
- 支持调整终端字体大小并自动保存；
- 支持保存 SSH 主机、端口、用户名、密钥路径等连接信息；
- 密码可使用 Windows DPAPI 加密保存；
- 可选择隐藏 `ssh.exe` 控制台窗口；
- 可打包成不依赖目标机器 Python 环境的 Windows EXE。

## v31 光标改进

v31 不再完全依赖 Windows 客户端猜测光标位置，而是以 tmux 的屏幕状态为准：

- 保留 tmux 的物理行，不再用 `capture-pane -J` 合并自动换行；
- 快照携带光标所在的快照行、终端显示列和 Tk 字符索引；
- 支持中文、全角字符、组合字符和 Emoji ZWJ 字素簇的列宽换算；
- 本地按键预测只用于降低网络往返期间的视觉延迟；
- 收到服务端快照后，立即以远端真实坐标校正光标；
- Windows 窗口或字体变化时，自动同步 tmux 的行数和列数。

这能显著改善普通 Shell、中文命令、长命令换行、历史命令和方向键编辑中的光标错位。

## 工作原理

```text
Windows GUI
    |
    | Windows ssh.exe 本地端口转发
    v
Linux FastAPI / WebSocket 服务
    |
    | tmux new-session / send-keys / capture-pane
    v
持久化 tmux 终端和远程任务
```

```text
关闭 Windows 客户端    -> 仅断开连接，远程任务继续运行
重新打开客户端          -> 重新连接已有 tmux 终端
点击 Close Remote      -> 真正关闭选中的远程 tmux 会话
```

## 项目结构

```text
Remote-tmux-Terminal-GUI-for-Windows/
├── server/
│   ├── server.py           # v31 服务端入口
│   ├── base_server.py      # 完整服务端功能
│   ├── protocol.py         # Unicode 列宽和光标协议
│   ├── requirements.txt
│   └── start_server.sh     # 一键创建环境并启动
├── windows_client/
│   ├── client.py           # v31 Windows 入口
│   ├── base_client.py      # 完整 GUI、SSH 和终端功能
│   ├── run_client.bat
│   ├── build_windows_exe.bat
│   └── RemoteTmuxTerminal-v31.spec
├── tests/
│   └── test_protocol.py
├── UI.jpg
└── README.md
```

## Linux 服务端

### 系统要求

Debian 或 Ubuntu：

```bash
sudo apt update
sudo apt install tmux python3 python3-venv
```

### 一键启动

进入项目根目录后执行：

```bash
bash server/start_server.sh
```

首次运行时，脚本会在 `server/.venv` 创建独立 Python 环境，安装 FastAPI、Uvicorn、
Pydantic 和 `wcwidth`，然后启动服务。后续会复用该环境，只有依赖清单变化时才重新安装，
不会污染系统 Python、Conda 环境或训练环境。

默认监听 `127.0.0.1:8765`。建议只通过 SSH 端口转发访问，不要直接暴露到公网。

使用其他端口：

```bash
bash server/start_server.sh --port 8766
```

检查服务：

```bash
curl http://127.0.0.1:8765/health
```

### 长期运行

```bash
tmux new -s rterm_server_v31
cd /你的项目目录
bash server/start_server.sh
```

按 `Ctrl+B`，松开后按 `D`，即可退出界面但保持服务运行。重新进入：

```bash
tmux attach -t rterm_server_v31
```

训练任务使用的 `rterm_xxxxxxxxxxxx` 会话和服务端会话相互独立。重启 FastAPI 服务不会
终止这些训练任务。

## Windows 客户端

### 从源码运行

Windows 需要安装 Python 和 Windows OpenSSH Client：

```bat
ssh -V
windows_client\run_client.bat
```

客户端源码只使用 Python 标准库，不需要 Paramiko、Cryptography 等第三方库。

### 构建独立 EXE

```bat
python -m pip install pyinstaller
windows_client\build_windows_exe.bat
```

输出文件：

```text
windows_client\dist\RemoteTmuxTerminal-v31.exe
```

目标机器不需要安装 Python，但仍需要 Windows OpenSSH Client。

## 连接设置

```text
SSH Host                 Linux 主机地址
SSH Port                 SSH 端口，通常为 22
Username                 Linux 用户名
Private key path         SSH 私钥路径
Password                 可选，可用 DPAPI 加密保存
Remote service port      服务端口，默认 8765
Default terminal dir     新建终端的默认目录
Hide ssh.exe window      隐藏 SSH 控制台窗口
Font size                终端字体大小
```

配置保存在：

```text
%APPDATA%\RemoteTmuxTerminal\client_config.json
```

密码由 Windows DPAPI 绑定到当前 Windows 用户。推荐优先使用 SSH 密钥或 ssh-agent。
使用需要交互输入的密码时不建议隐藏 SSH 窗口；保存密码后可通过 `SSH_ASKPASS` 认证。

## 默认远程目录

设置默认目录后，新建终端会先启动正常的登录式 Shell，再发送：

```bash
cd -- /your/default/path
```

Shell 初始化、环境变量和 Conda 初始化会先正常完成。该设置只影响新建终端，不会移动
已经存在的 tmux 会话。

## 常用操作

```text
Ctrl + Plus / Ctrl + Minus    调整字体
Ctrl + C                      有选区时复制，否则发送中断
Ctrl + V                      粘贴 Windows 剪贴板
Ctrl + Alt + V                向远端发送原始 Ctrl-V
Ctrl + Click                  多选远程终端
Shift + Click                 连续选择终端
```

长期任务示例：

```bash
conda activate myenv
cd /data/my_project
python train.py
```

关闭 Windows 客户端不会停止该命令，之后重新启动即可继续查看。

## 测试

```bash
python -m unittest discover -s tests -p "test_protocol.py" -v
```

建议手工检查 ASCII、中文、Emoji、组合字符、左右方向键、Home/End、命令历史、长命令
换行、空提示符、字体变化和窗口缩放。

## 限制与安全说明

- 这是面向持久任务和常规 Shell 操作的轻量终端，不是完整的 xterm 实现；
- `vim`、`htop`、`less` 等复杂全屏 TUI 可能仍有样式或局部刷新差异；
- v31 解决了主要光标坐标问题，但客户端不会完整还原全部 ANSI 样式；
- 服务端应监听 `127.0.0.1` 并通过 SSH 转发访问；
- `Close Remote` 会真正终止远程 tmux 会话，使用前请确认没有重要任务；
- 日常关闭窗口只会分离连接，不会终止远程任务。
