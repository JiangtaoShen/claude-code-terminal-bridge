# Terminal Bridge

**Let Claude Code control your SSH sessions through file-based IPC.**

让 Claude Code 通过文件 IPC 操控你的 SSH 终端会话。

```
┌─────────────┐     command.txt      ┌──────────────────┐      PTY       ┌─────────────┐
│ Claude Code │  ──────────────────> │ terminal_bridge.py│ ────────────> │  SSH Session │
│ (any project)│ <────────────────── │   (CMD window)   │ <──────────── │   (remote)   │
└─────────────┘     output.txt       └──────────────────┘               └─────────────┘
```

## Requirements / 环境要求

- **Windows 10+** (uses ConPTY)
- **Python 3.8+**

## Quick Start / 快速开始

### 1. Clone & Install / 克隆安装

```bash
git clone https://github.com/<your-username>/terminal-bridge.git
cd terminal-bridge
pip install -r requirements.txt
```

### 2. Start Bridge / 启动桥接

Open a **CMD window** and run:

打开一个 **CMD 窗口**，运行：

```bash
py terminal_bridge.py
```

You'll see a status bar at the top and a CMD prompt. Manually SSH into your server:

你会看到顶部状态栏和 CMD 提示符。手动 SSH 登录你的服务器：

```bash
ssh user@your-server.com
# Enter password manually / 手动输入密码
```

### 3. Use from Claude Code / 在 Claude Code 中使用

In **any** Claude Code project, tell Claude:

在**任意** Claude Code 项目中，告诉 Claude：

> "I've started terminal bridge and logged into SSH. The IPC files are at `~/.terminal-bridge/`."
>
> "我已经启动了 terminal bridge 并登录了 SSH。"

Claude Code will:
- **Read** `~/.terminal-bridge/output.txt` to see the terminal screen
- **Write** `~/.terminal-bridge/command.txt` to send commands

Claude Code 会：
- **读取** `~/.terminal-bridge/output.txt` 查看终端屏幕
- **写入** `~/.terminal-bridge/command.txt` 发送命令

## Command Protocol / 命令协议

Write commands to `command.txt`. Four formats are supported:

向 `command.txt` 写入命令。支持四种格式：

| Prefix | Description | Example |
|--------|-------------|---------|
| *(none)* | Send text + Enter / 发送文本+回车 | `ls -la` |
| `EXEC:` | Capture full output to `result.txt` / 完整输出捕获到 result.txt | `EXEC:find / -name "*.log"` |
| `RAW:` | Send raw bytes / 发送原始字节 | `RAW:\x03` (Ctrl+C) |
| `KEY:` | Send special key / 发送特殊按键 | `KEY:CTRL+C`, `KEY:UP`, `KEY:TAB` |

### Supported Keys / 支持的按键

`CTRL+C`, `CTRL+D`, `CTRL+Z`, `CTRL+L`, `CTRL+A`, `CTRL+E`, `CTRL+K`, `CTRL+U`, `CTRL+W`, `CTRL+R`, `ENTER`, `TAB`, `BACKSPACE`, `ESCAPE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `HOME`, `END`, `DELETE`, `PAGEUP`, `PAGEDOWN`

## Files / 文件说明

All IPC files are stored in `~/.terminal-bridge/` by default:

所有 IPC 文件默认存放在 `~/.terminal-bridge/` 下：

| File | Direction | Description |
|------|-----------|-------------|
| `output.txt` | Bridge → Claude | Current terminal screen content / 当前终端屏幕内容 |
| `command.txt` | Claude → Bridge | Commands to execute / 待执行的命令 |
| `result.txt` | Bridge → Claude | Full output from `EXEC:` commands / EXEC 命令的完整输出 |
| `status.json` | Bridge → Claude | Bridge status (PID, alive, timestamps) / 桥接状态信息 |

## CLI Arguments / 命令行参数

```
py terminal_bridge.py [OPTIONS]

Options:
  --dir DIR        IPC files directory (default: ~/.terminal-bridge/)
  --shell SHELL    Shell to spawn (default: cmd.exe)
  --cols N         Terminal columns (default: auto-detect)
  --rows N         Terminal rows (default: auto-detect)
  --output PATH    Override output.txt path
  --command PATH   Override command.txt path
  --status PATH    Override status.json path
```

### Examples / 示例

```bash
# Default: IPC files in ~/.terminal-bridge/
py terminal_bridge.py

# Custom directory
py terminal_bridge.py --dir D:\my_project\bridge

# Use PowerShell instead of CMD
py terminal_bridge.py --shell powershell.exe
```

## FAQ / 常见问题

**Q: Can I use this from any Claude Code project?**
**Q: 可以在任意 Claude Code 项目中使用吗？**

Yes! IPC files are stored in a fixed global path (`~/.terminal-bridge/`), so any Claude Code project can access them.

可以！IPC 文件存放在固定全局路径（`~/.terminal-bridge/`），任何项目的 Claude Code 都能访问。

**Q: How do I enter passwords?**
**Q: 怎么输入密码？**

Type passwords directly in the Bridge CMD window. Never send passwords through `command.txt`.

直接在 Bridge CMD 窗口中手动输入密码。不要通过 `command.txt` 发送密码。

**Q: What if command output is too long?**
**Q: 命令输出太长怎么办？**

Use the `EXEC:` prefix — it redirects output to `result.txt` with no line limit. Or redirect manually: `some_command > /tmp/result.log`.

使用 `EXEC:` 前缀，输出会完整保存到 `result.txt`，无行数限制。或者手动重定向：`some_command > /tmp/result.log`。

**Q: Can I still type in the Bridge window while Claude is using it?**
**Q: Claude 使用时我还能在 Bridge 窗口打字吗？**

Yes, both manual input and Claude's commands work simultaneously.

可以，手动输入和 Claude 的命令可以同时使用。

## License

MIT
