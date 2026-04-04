# Claude Code Terminal Bridge

**A file-based IPC bridge that lets Claude Code control remote SSH sessions through a Windows CMD terminal, using pywinpty + pyte for real-time screen capture and command injection.**

```
┌─────────────┐                        ┌──────────────────┐      PTY       ┌─────────────┐
│ Claude Code │  ── bridge_run.py ──>  │terminal_bridge.py│ ────────────>  │ SSH Session │
│(any project)│ <── result.txt ──────  │   (CMD window)   │ <────────────  │  (remote)   │
└─────────────┘                        └──────────────────┘                └─────────────┘
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

You'll see `[Bridge v2.1.0]` in the startup message and a status bar at the top. Manually SSH into your server:

```bash
ssh user@your-server.com
# Enter password manually
```

### 3. Execute Remote Commands

#### Using `bridge_run.py` (recommended)

One command does everything -- write, poll, read:

```bash
# Single command
py bridge_run.py "ls ~/data"

# Output only (skip metadata header)
py bridge_run.py --raw "nvidia-smi"

# Strip ANSI escape codes (for logs with color/progress bars)
py bridge_run.py --clean "tail -50 ~/train.log"

# Custom timeout
py bridge_run.py --timeout 30 "quick_check"

# Upload & run a local script (avoids quoting issues)
py bridge_run.py --script local_analysis.py
py bridge_run.py --script setup.sh --interpreter bash

# Upload a file to remote
py bridge_run.py --upload local_data.py /tmp/remote_data.py

# Kill a stuck command
py bridge_run.py --cancel

# Check SSH connection
py bridge_run.py --last 3

# Read scrollback history
py bridge_run.py --scroll 200

# Show bridge status
py bridge_run.py --status
```

The exit code matches the remote command's exit code. Timeout returns 124.

#### Using raw IPC (fallback)

Write commands directly to `~/.terminal-bridge/command.txt`:

```bash
echo "EXEC:req_001:your_command" > ~/.terminal-bridge/command.txt
# Poll status.json for completion, then read result.txt
```

### 4. Use from Any Claude Code Project

In **any** Claude Code project, tell Claude:

> "I've started terminal bridge and logged into SSH."

Claude Code will use `bridge_run.py` or the IPC files at `~/.terminal-bridge/` to execute remote commands.

## Architecture

### v2.1.0 Changes

- **`--script` flag**: Upload and execute local script files via base64 encoding. Completely eliminates quoting and escaping issues when running multi-line Python/bash scripts on remote.
- **`--cancel` flag**: Send Ctrl+C (x3) to kill stuck commands and recover the terminal. No more manual `KEY:CTRL+C` to command.txt.
- **`--upload` flag**: Upload local files to remote paths via base64 encoding.
- **Heredoc detection**: Commands containing `<<` are automatically blocked with a helpful error message, preventing the most common cause of bridge deadlocks.
- **Busy detection**: Before sending a command, `bridge_run.py` checks if the bridge is already executing. If busy, it waits up to 30s or suggests `--cancel`.
- **Improved timeout recovery**: Bridge now sends Ctrl+C 3 times (instead of 1) on timeout, with a 1s settling delay, ensuring reliable terminal recovery.

### v2.0.0 Changes

- **PTY-through execution**: All commands (EXEC, QUERY, BATCH) are sent through the PTY with start/end markers. Works transparently through SSH sessions.
- **`bridge_run.py` CLI helper**: One-call command execution with auto-polling. Replaces the manual `echo > command.txt && sleep N && cat result.txt` pattern.
- **Single-instance lock**: Prevents multiple bridge processes from competing for `command.txt`.
- **Version field in `status.json`**: Easy verification of which code version is running.

### How It Works

1. **pywinpty** creates a Windows pseudo-terminal (ConPTY) running `cmd.exe`
2. **pyte** renders the raw ANSI escape sequences into readable plain text
3. A background thread writes the rendered screen to `output.txt` every 0.5s
4. Another thread polls `command.txt` every 0.2s and forwards commands to the PTY
5. `EXEC:` sends `echo <START_MARKER>; cmd; echo <END_MARKER> $?` through the PTY, then extracts output between markers from the pyte screen buffer
6. `BATCH:` executes multiple commands sequentially with the same marker-based capture
7. All file writes use `os.replace()` for atomicity
8. `bridge_run.py` writes to `command.txt`, polls `status.json` for completion, then reads `result.txt`

## Command Protocol

Write commands to `command.txt`. The following formats are supported:

| Prefix | Description | Example |
|--------|-------------|---------|
| *(none)* | Send text + Enter | `ls -la` |
| `EXEC:id:cmd` | Capture full output with request ID | `EXEC:req_001:find / -name "*.log"` |
| `EXEC:CLEAN:id:cmd` | EXEC with ANSI escape cleaning | `EXEC:CLEAN:req_002:cat log.txt` |
| `QUERY:id:cmd` | Same as EXEC with 30s timeout | `QUERY:q_001:pwd` |
| `BATCH:id` | Execute multiple commands sequentially | See below |
| `SCROLLBACK:N` | Export last N lines from scrollback history | `SCROLLBACK:200` |
| `LAST:N` | Write last N non-empty screen lines to `result.txt` | `LAST:5` |
| `RAW:` | Send raw bytes | `RAW:\x03` (Ctrl+C) |
| `KEY:` | Send special key | `KEY:CTRL+C`, `KEY:UP`, `KEY:TAB` |

### Request ID Correlation

Every `EXEC:` and `QUERY:` command includes a unique request ID. The result in `result.txt` includes this ID for verification:

```
[req_id: req_001]
[status: done]
[exit_code: 0]
[timestamp: 2026-04-03T02:05:11]
[duration: 0.3s]
[cmd: nvidia-smi]
...output...
```

### Batch Execution

Execute multiple commands in a single request:

```
BATCH:batch_001
EXEC:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader
EXEC:ps aux | grep "python.*train" | grep -v grep
EXEC:ls -lt ~/output/ | head -5
END_BATCH
```

Results are separated in `result.txt` with per-command exit codes.

### Supported Keys

`CTRL+C`, `CTRL+D`, `CTRL+Z`, `CTRL+L`, `CTRL+A`, `CTRL+E`, `CTRL+K`, `CTRL+U`, `CTRL+W`, `CTRL+R`, `ENTER`, `TAB`, `BACKSPACE`, `ESCAPE`, `UP`, `DOWN`, `LEFT`, `RIGHT`, `HOME`, `END`, `DELETE`, `PAGEUP`, `PAGEDOWN`

## Files

| File | Description |
|------|-------------|
| `terminal_bridge.py` | The bridge server -- runs in a CMD window |
| `bridge_run.py` | CLI helper for Claude Code -- one-call command execution |
| `~/.terminal-bridge/command.txt` | Claude -> Bridge: commands to execute |
| `~/.terminal-bridge/result.txt` | Bridge -> Claude: command output |
| `~/.terminal-bridge/status.json` | Bridge -> Claude: status, exec queue, version |
| `~/.terminal-bridge/output.txt` | Bridge -> Claude: raw terminal screen |
| `~/.terminal-bridge/bridge.lock` | Single-instance lock file |

## CLI Arguments

### terminal_bridge.py

```
py terminal_bridge.py [OPTIONS]

Options:
  --dir DIR        IPC files directory (default: ~/.terminal-bridge/)
  --shell SHELL    Shell to spawn (default: cmd.exe)
  --cols N         Terminal columns (default: auto-detect)
  --rows N         Terminal rows (default: auto-detect)
```

### bridge_run.py

```
py bridge_run.py [OPTIONS] "command"

Options:
  --raw                 Output only command result (skip metadata)
  --clean               Strip ANSI escapes from output
  --timeout N           Timeout in seconds (default: 130)
  --last N              Read last N screen lines (no command sent)
  --scroll N            Read N scrollback lines
  --status              Show bridge status as JSON
  --id ID               Custom request ID (auto-generated if omitted)
  --script FILE         Upload & execute a local script file
  --interpreter NAME    Interpreter for --script (default: python3)
  --upload LOCAL REMOTE Upload local file to remote path
  --cancel              Send Ctrl+C to kill stuck command
```

## FAQ

**Q: Can I use this from any Claude Code project?**
Yes! IPC files are stored in a fixed global path (`~/.terminal-bridge/`), so any Claude Code project can access them.

**Q: How do I enter passwords?**
Type passwords directly in the Bridge CMD window. Never send passwords through `command.txt`.

**Q: What if another bridge is already running?**
v2.0.0+ has single-instance protection. The new bridge will show an error with the old PID. Close the old bridge window or `taskkill /F /PID <pid>`.

**Q: What about progress bars / ANSI codes in output?**
Use `bridge_run.py --clean` or `EXEC:CLEAN:` to automatically strip ANSI escape sequences.

**Q: How do I check running task progress cheaply?**
Use `bridge_run.py --last 3` -- reads only the last few screen lines without sending any command.

**Q: How do I run multi-line scripts without quoting issues?**
Use `bridge_run.py --script local_file.py`. The file is base64-encoded, uploaded, and executed. Zero quoting issues.

**Q: The bridge is stuck / not responding?**
Run `bridge_run.py --cancel` to send Ctrl+C and recover. If that doesn't help, restart the bridge window.

## Common Pitfalls

1. **Bridge must be started by the user in a visible CMD window.** Claude Code cannot spawn visible windows.

2. **SSH must be done inside the Bridge window.** The Bridge only captures its own PTY.

3. **Each Bridge start is a fresh session.** Restarting the Bridge drops the old SSH connection.

4. **Never use heredoc (`<<`) in commands.** It causes the bridge to deadlock. `bridge_run.py` blocks this automatically. Use `--script` instead.

5. **Always verify request IDs.** `bridge_run.py` does this automatically. With raw IPC, check `[req_id:]` in result.txt.

6. **Use BATCH for multiple queries.** One BATCH command is faster than 3 separate EXECs.

## Claude Code Skill

This project includes a Claude Code skill file (`.claude/skills/terminal-bridge.md`) that teaches Claude how to use the bridge. To use in other projects:

```bash
mkdir -p /path/to/your/project/.claude/skills
cp .claude/skills/terminal-bridge.md /path/to/your/project/.claude/skills/
```
