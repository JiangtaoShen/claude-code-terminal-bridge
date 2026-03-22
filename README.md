# 🌉 Claude Code Terminal Bridge

**A file-based IPC bridge that lets Claude Code control remote SSH sessions through a Windows CMD terminal, using pywinpty + pyte for real-time screen capture and command injection.**

```
┌─────────────┐     command.txt      ┌──────────────────┐      PTY       ┌─────────────┐
│ Claude Code │  ──────────────────> │ terminal_bridge.py│ ────────────> │  SSH Session │
│ (any project)│ <────────────────── │   (CMD window)   │ <──────────── │   (remote)   │
└─────────────┘     output.txt       └──────────────────┘               └─────────────┘
```

## 📋 Requirements

- **Windows 10+** (uses ConPTY)
- **Python 3.8+**

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/JiangtaoShen/claude-code-terminal-bridge.git
cd claude-code-terminal-bridge
pip install -r requirements.txt
```

### 2. Start the Bridge

Open a **CMD window** and run:

```bash
py terminal_bridge.py
```

You'll see a status bar at the top and a CMD prompt. Manually SSH into your server:

```bash
ssh user@your-server.com
# Enter password manually
```

### 3. Use from Any Claude Code Project

In **any** Claude Code project, tell Claude:

> "I've started terminal bridge and logged into SSH. The IPC files are at `~/.terminal-bridge/`."

Claude Code will:
- **Read** `~/.terminal-bridge/output.txt` to see the terminal screen
- **Write** `~/.terminal-bridge/command.txt` to send commands

## 📡 Command Protocol

Write commands to `command.txt`. Four formats are supported:

| Prefix | Description | Example |
|--------|-------------|---------|
| *(none)* | Send text + Enter | `ls -la` |
| `EXEC:` | Capture full output to `result.txt` | `EXEC:find / -name "*.log"` |
| `RAW:` | Send raw bytes | `RAW:\x03` (Ctrl+C) |
| `KEY:` | Send special key | `KEY:CTRL+C`, `KEY:UP`, `KEY:TAB` |

### Supported Keys

`CTRL+C`, `CTRL+D`, `CTRL+Z`, `CTRL+L`, `CTRL+A`, `CTRL+E`, `CTRL+K`, `CTRL+U`, `CTRL+W`, `CTRL+R`, `ENTER`, `TAB`, `BACKSPACE`, `ESCAPE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `HOME`, `END`, `DELETE`, `PAGEUP`, `PAGEDOWN`

## 📁 Files

All IPC files are stored in `~/.terminal-bridge/` by default:

| File | Direction | Description |
|------|-----------|-------------|
| `output.txt` | Bridge → Claude | Current terminal screen content |
| `command.txt` | Claude → Bridge | Commands to execute |
| `result.txt` | Bridge → Claude | Full output from `EXEC:` commands |
| `status.json` | Bridge → Claude | Bridge status (PID, alive, timestamps) |

## ⚙️ CLI Arguments

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

### Examples

```bash
# Default: IPC files in ~/.terminal-bridge/
py terminal_bridge.py

# Custom directory
py terminal_bridge.py --dir D:\my_project\bridge

# Use PowerShell instead of CMD
py terminal_bridge.py --shell powershell.exe
```

## ❓ FAQ

**Q: Can I use this from any Claude Code project?**

Yes! IPC files are stored in a fixed global path (`~/.terminal-bridge/`), so any Claude Code project can access them.

**Q: How do I enter passwords?**

Type passwords directly in the Bridge CMD window. Never send passwords through `command.txt`.

**Q: What if command output is too long?**

Use the `EXEC:` prefix — it redirects output to `result.txt` with no line limit. Or redirect manually: `some_command > /tmp/result.log`.

**Q: Can I still type in the Bridge window while Claude is using it?**

Yes, both manual input and Claude's commands work simultaneously.

## 🔧 How It Works

1. **pywinpty** creates a Windows pseudo-terminal (ConPTY) running `cmd.exe`
2. **pyte** renders the raw ANSI escape sequences into readable plain text
3. A background thread writes the rendered screen to `output.txt` every 0.5s
4. Another thread polls `command.txt` every 0.2s and forwards commands to the PTY
5. The Bridge window itself acts as a transparent terminal — you can type directly in it
