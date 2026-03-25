# Claude Code Terminal Bridge

**A file-based IPC bridge that lets Claude Code control remote SSH sessions through a Windows CMD terminal, using pywinpty + pyte for real-time screen capture and command injection.**

```
┌─────────────┐     command.txt      ┌──────────────────┐      PTY       ┌─────────────┐
│ Claude Code │  ──────────────────> │terminal_bridge.py│ ────────────>  │ SSH Session │
│(any project)│ <──────────────────  │   (CMD window)   │ <────────────  │  (remote)   │
└─────────────┘     output.txt       └──────────────────┘                └─────────────┘
```

## Requirements

- **Windows 10+** (uses ConPTY)
- **Python 3.8+**

## Quick Start

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
- **Read** `~/.terminal-bridge/result.txt` to get full command output

## Command Protocol

Write commands to `command.txt`. Five formats are supported:

| Prefix | Description | Example |
|--------|-------------|---------|
| *(none)* | Send text + Enter | `ls -la` |
| `EXEC:` | Capture full output to `result.txt` | `EXEC:find / -name "*.log"` |
| `LAST:N` | Write last N non-empty screen lines to `result.txt` (no command sent) | `LAST:5` |
| `RAW:` | Send raw bytes | `RAW:\x03` (Ctrl+C) |
| `KEY:` | Send special key | `KEY:CTRL+C`, `KEY:UP`, `KEY:TAB` |

### When to Use Each

- **Plain command** (`ls -la`): Quick commands where you'll read the screen via `output.txt` afterwards. Output is limited to the visible terminal area.
- **`EXEC:`**: Long-running commands or commands that produce lots of output. Result is captured to `result.txt` regardless of screen size. Uses unique delimiters for reliable extraction.
- **`LAST:N`**: Lightweight status check — reads the current screen without sending any command to the terminal. Perfect for checking progress bars, tailing logs, or reading prompts. Costs minimal tokens since only N lines are returned.

### Supported Keys

`CTRL+C`, `CTRL+D`, `CTRL+Z`, `CTRL+L`, `CTRL+A`, `CTRL+E`, `CTRL+K`, `CTRL+U`, `CTRL+W`, `CTRL+R`, `ENTER`, `TAB`, `BACKSPACE`, `ESCAPE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `HOME`, `END`, `DELETE`, `PAGEUP`, `PAGEDOWN`

## Files

All IPC files are stored in `~/.terminal-bridge/` by default:

| File | Direction | Description |
|------|-----------|-------------|
| `output.txt` | Bridge -> Claude | Current terminal screen (trailing blank lines stripped) |
| `command.txt` | Claude -> Bridge | Commands to execute |
| `result.txt` | Bridge -> Claude | Output from `EXEC:` or `LAST:N` commands |
| `status.json` | Bridge -> Claude | Bridge status (PID, alive, timestamps) |

### Token-Saving Design

- **`output.txt`** automatically strips trailing blank lines, so you only pay for lines with actual content instead of a full 40-row screen.
- **`LAST:N`** lets you check terminal state with just N lines (e.g., `LAST:3` for a quick progress check) — far cheaper than reading the full screen.
- **`EXEC:`** uses unique delimiters (`__BRIDGE_BEGIN_RESULT__` / `__BRIDGE_END_RESULT__`) for reliable output extraction, avoiding the old approach of screen-history parsing that mixed terminal noise into results.

## CLI Arguments

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

## FAQ

**Q: Can I use this from any Claude Code project?**

Yes! IPC files are stored in a fixed global path (`~/.terminal-bridge/`), so any Claude Code project can access them.

**Q: How do I enter passwords?**

Type passwords directly in the Bridge CMD window. Never send passwords through `command.txt`.

**Q: What if command output is too long?**

Use the `EXEC:` prefix — it redirects output to a temp file on the remote server, then reads it back with unique delimiters for reliable capture. For very large outputs (>50KB), redirect manually: `some_command > /tmp/result.log`.

**Q: Can I still type in the Bridge window while Claude is using it?**

Yes, both manual input and Claude's commands work simultaneously.

**Q: How do I check a running task's progress without wasting tokens?**

Use `LAST:3` or `LAST:5` — it reads only the last few non-empty lines from the current screen without sending any command. Perfect for progress bars and log tails.

## Common Pitfalls

**1. Bridge must be started by the user in a visible CMD window**

Claude Code cannot spawn a visible CMD window via `start cmd /k ...` or background Bash — the Bridge runs inside Claude's invisible subprocess. **The user must manually open CMD and run `py terminal_bridge.py`.**

**2. SSH must be done inside the Bridge window**

The Bridge only captures the PTY it owns. If the user SSHs in a different terminal, the Bridge still shows the local CMD prompt. Always SSH in the window with the `BRIDGE | ...` status bar at the top.

**3. Each Bridge start is a fresh session**

If the Bridge is restarted, the old SSH session is gone. You must SSH again in the new Bridge window. Old `result.txt` / `output.txt` may contain stale content from the previous session — always verify with `LAST:3` after reconnecting.

**4. Don't read `output.txt` to verify command results**

`output.txt` captures the full visible screen and may contain old command history, SSH banners, etc. For command results, always use `EXEC:` (writes clean result to `result.txt`) or `LAST:N` (last N lines only). Only read `output.txt` for debugging connection state.

**5. Prefer `LAST:N` over `output.txt` for status checks**

Reading `output.txt` returns the entire screen (~20-40 lines). `LAST:3` returns only 3 lines to `result.txt`. For progress checks, `LAST:N` saves significant tokens.

## Claude Code Skill

This project includes a Claude Code skill file (`.claude/skills/terminal-bridge.md`) that teaches Claude how to use the bridge correctly. When you use this project as a Claude Code workspace, the skill is automatically available.

To use the skill in **other projects**, copy it:

```bash
# Copy to your project
mkdir -p /path/to/your/project/.claude/skills
cp .claude/skills/terminal-bridge.md /path/to/your/project/.claude/skills/
```

The skill encodes best practices learned from real usage:
- Always use `EXEC:` prefix and read `result.txt` (not `output.txt`)
- One command at a time — wait for completion before sending the next
- Use `nohup` for long-running tasks, then monitor with `tail` / `wc -l`
- Use `LAST:N` for lightweight progress checks
- Fallback to `output.txt` when `result.txt` is stuck

## How It Works

1. **pywinpty** creates a Windows pseudo-terminal (ConPTY) running `cmd.exe`
2. **pyte** renders the raw ANSI escape sequences into readable plain text
3. A background thread writes the rendered screen to `output.txt` every 0.5s (trailing blank lines stripped)
4. Another thread polls `command.txt` every 0.2s and forwards commands to the PTY
5. `EXEC:` commands redirect output to a temp file, then read it back using unique delimiters for reliable extraction
6. `LAST:N` directly reads the pyte screen buffer without sending any command — zero latency, minimal tokens
7. The Bridge window itself acts as a transparent terminal — you can type directly in it
