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
- **Read** `~/.terminal-bridge/status.json` to check execution status

## Command Protocol

Write commands to `command.txt`. The following formats are supported:

| Prefix | Description | Example |
|--------|-------------|---------|
| *(none)* | Send text + Enter | `ls -la` |
| `EXEC:id:cmd` | Capture full output with request ID | `EXEC:req_001:find / -name "*.log"` |
| `EXEC:CLEAN:id:cmd` | EXEC with ANSI escape cleaning | `EXEC:CLEAN:req_002:cat log.txt` |
| `QUERY:id:cmd` | Fast query for short output | `QUERY:q_001:nvidia-smi` |
| `BATCH:id` | Execute multiple commands sequentially | See below |
| `SCROLLBACK:N` | Export last N lines from scrollback history | `SCROLLBACK:200` |
| `LAST:N` | Write last N non-empty screen lines to `result.txt` | `LAST:5` |
| `RAW:` | Send raw bytes | `RAW:\x03` (Ctrl+C) |
| `KEY:` | Send special key | `KEY:CTRL+C`, `KEY:UP`, `KEY:TAB` |

### Request ID Correlation

Every `EXEC:` and `QUERY:` command includes a unique request ID. The result in `result.txt` includes this ID, so Claude can verify it's reading the correct result:

```
[req_id: req_001]
[status: done]
[exit_code: 0]
[timestamp: 2026-04-03T02:05:11]
[duration: 0.3s]
[cmd: nvidia-smi]
...output...
```

### Execution Status in status.json

`status.json` includes an `exec_queue` section showing the current and last completed command:

```json
{
  "pty_alive": true,
  "exec_queue": {
    "current": {
      "req_id": "req_003",
      "cmd": "ps aux | grep train",
      "status": "running",
      "started_at": "2026-04-03T02:05:10"
    },
    "last_completed": {
      "req_id": "req_002",
      "status": "done",
      "exit_code": 0,
      "duration_s": 1.2,
      "completed_at": "2026-04-03T02:05:09"
    }
  }
}
```

### Batch Execution

Execute multiple commands in a single request:

```
BATCH:batch_001
EXEC:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader
EXEC:ps aux | grep "python.*train" | grep -v grep
EXEC:ls -lt ~/output/*pde5* 2>/dev/null
END_BATCH
```

Results are separated in `result.txt` with per-command exit codes.

### When to Use Each

- **`EXEC:id:cmd`**: Standard command execution with full output capture. Uses background shell to avoid terminal noise interference.
- **`EXEC:CLEAN:id:cmd`**: Same as EXEC but strips ANSI escape sequences. Use when output contains tqdm progress bars, colored text, or other control characters.
- **`QUERY:id:cmd`**: Fast query for commands with short output (<20 lines). Captures directly from screen, no file redirect needed. 15s timeout.
- **`BATCH:id`**: Multiple commands executed sequentially in one request. Reduces round-trip overhead.
- **`SCROLLBACK:N`**: Retrieve scrollback history when the screen has been overwritten by progress bars.
- **`LAST:N`**: Lightweight status check — reads current screen without sending any command. Minimal tokens.

### Supported Keys

`CTRL+C`, `CTRL+D`, `CTRL+Z`, `CTRL+L`, `CTRL+A`, `CTRL+E`, `CTRL+K`, `CTRL+U`, `CTRL+W`, `CTRL+R`, `ENTER`, `TAB`, `BACKSPACE`, `ESCAPE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `HOME`, `END`, `DELETE`, `PAGEUP`, `PAGEDOWN`

## Files

All IPC files are stored in `~/.terminal-bridge/` by default:

| File | Direction | Description |
|------|-----------|-------------|
| `output.txt` | Bridge -> Claude | Current terminal screen (trailing blank lines stripped) |
| `command.txt` | Claude -> Bridge | Commands to execute |
| `result.txt` | Bridge -> Claude | Output from `EXEC:` / `QUERY:` / `BATCH:` / `LAST:N` / `SCROLLBACK:N` |
| `status.json` | Bridge -> Claude | Bridge status (PID, alive, timestamps, exec queue) |

### Token-Saving Design

- **`output.txt`** automatically strips trailing blank lines, so you only pay for lines with actual content instead of a full 40-row screen.
- **`LAST:N`** lets you check terminal state with just N lines (e.g., `LAST:3` for a quick progress check) — far cheaper than reading the full screen.
- **`QUERY:`** captures short outputs directly from screen changes — faster than the full EXEC pipeline.
- **`BATCH:`** reduces round-trip overhead by executing multiple commands in one request.
- **Atomic writes**: All result files use `os.replace()` for atomic writes, preventing Claude from reading half-written files.

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

Use the `EXEC:` prefix — it redirects output to a temp file on the remote server, then reads it back. For very large outputs (>50KB), redirect manually: `some_command > /tmp/result.log`.

**Q: Can I still type in the Bridge window while Claude is using it?**

Yes, both manual input and Claude's commands work simultaneously.

**Q: How do I check a running task's progress without wasting tokens?**

Use `LAST:3` or `LAST:5` — it reads only the last few non-empty lines from the current screen without sending any command. Or read `status.json` to check `exec_queue.current.status`.

**Q: What about progress bars / ANSI codes in output?**

Use `EXEC:CLEAN:` to automatically strip ANSI escape sequences and carriage returns from output. This solves issues with tqdm progress bars, colored text, and grep returning "Binary file matches".

## Common Pitfalls

**1. Bridge must be started by the user in a visible CMD window**

Claude Code cannot spawn a visible CMD window — the Bridge runs inside Claude's invisible subprocess. **The user must manually open CMD and run `py terminal_bridge.py`.**

**2. SSH must be done inside the Bridge window**

The Bridge only captures the PTY it owns. If the user SSHs in a different terminal, the Bridge still shows the local CMD prompt. Always SSH in the window with the `BRIDGE | ...` status bar at the top.

**3. Each Bridge start is a fresh session**

If the Bridge is restarted, the old SSH session is gone. You must SSH again in the new Bridge window.

**4. Always verify request IDs in result.txt**

With the request ID system, always check that `[req_id: ...]` in result.txt matches your request before trusting the output. This prevents reading stale results from previous commands.

**5. Use EXEC:CLEAN for log files with ANSI codes**

Remote log files with tqdm progress bars contain ANSI escape sequences that can confuse grep and produce garbled output. The CLEAN variant strips these automatically.

**6. Prefer BATCH for multiple queries**

Instead of sending 3 separate EXEC commands with sleep between each, use a single BATCH command. This reduces total latency and gives structured per-command results.

## Claude Code Skill

This project includes a Claude Code skill file (`.claude/skills/terminal-bridge.md`) that teaches Claude how to use the bridge correctly. When you use this project as a Claude Code workspace, the skill is automatically available.

To use the skill in **other projects**, copy it:

```bash
# Copy to your project
mkdir -p /path/to/your/project/.claude/skills
cp .claude/skills/terminal-bridge.md /path/to/your/project/.claude/skills/
```

## How It Works

1. **pywinpty** creates a Windows pseudo-terminal (ConPTY) running `cmd.exe`
2. **pyte** renders the raw ANSI escape sequences into readable plain text
3. A background thread writes the rendered screen to `output.txt` every 0.5s (trailing blank lines stripped)
4. Another thread polls `command.txt` every 0.2s and forwards commands to the PTY
5. `EXEC:` commands execute in a background shell (independent from terminal stream) and write results to a temp file, then read it back using unique per-request delimiters
6. `QUERY:` captures short outputs directly from screen changes for low-latency results
7. `BATCH:` executes multiple commands sequentially with structured per-command results
8. `SCROLLBACK:` exports pyte history buffer for retrieving output pushed off screen
9. All file writes use `os.replace()` for atomicity — no half-written files
10. `status.json` includes an `exec_queue` with current/last_completed states for deterministic polling
11. The Bridge window itself acts as a transparent terminal — you can type directly in it
