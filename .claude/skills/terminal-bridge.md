---
name: terminal-bridge
description: Control remote SSH sessions via the Terminal Bridge file-based IPC. Use when the user mentions "terminal bridge", "bridge", "SSH", "remote server", or asks to run commands on a remote machine.
---

# Terminal Bridge Skill

Control a remote SSH session through file-based IPC at `~/.terminal-bridge/`.

## Prerequisites

The user must have **manually**:
1. Opened a CMD window and run `py D:\remote_control\terminal_bridge.py`
2. SSH'd into the remote server inside that bridge window
3. Told you the bridge is connected

**You cannot start the bridge or enter SSH passwords.** If the bridge is not running, ask the user to start it.

The bridge has **single-instance protection** -- if an old bridge is still running, the new one will show an error with the old PID. The user needs to close the old bridge first.

## Executing Remote Commands

### Primary method: `bridge_run.py` (recommended)

One command does everything: write, poll, read.

```bash
py D:/remote_control/bridge_run.py "your_command_here"
```

Examples:
```bash
# Single command
py D:/remote_control/bridge_run.py "ls ~/data/"

# Output only (skip metadata header) -- saves tokens
py D:/remote_control/bridge_run.py --raw "nvidia-smi"

# Strip ANSI escape codes (for logs with color/progress bars)
py D:/remote_control/bridge_run.py --clean "tail -50 ~/train.log"

# Custom timeout (default: 130s)
py D:/remote_control/bridge_run.py --timeout 30 "pwd"

# Check SSH connection
py D:/remote_control/bridge_run.py --last 3

# Read scrollback history
py D:/remote_control/bridge_run.py --scroll 200

# Show bridge status
py D:/remote_control/bridge_run.py --status
```

The exit code matches the remote command's exit code. TIMEOUT returns 124.

### Script execution: `--script` (for multi-line logic)

**Use `--script` whenever you need multi-line Python/bash logic on the remote.** The file is base64-encoded, uploaded to `/tmp/`, and executed. Zero quoting issues.

```bash
# Run a local Python script on remote
py D:/remote_control/bridge_run.py --script D:/path/to/analysis.py

# Run a local bash script on remote
py D:/remote_control/bridge_run.py --script D:/path/to/setup.sh --interpreter bash

# With --raw to skip metadata
py D:/remote_control/bridge_run.py --script D:/path/to/check.py --raw
```

**When to use `--script` vs inline command:**
- Inline: simple one-liners like `ls`, `wc -l`, `grep`, `nvidia-smi`
- `--script`: anything with Python imports, loops, f-strings, nested quotes, or multi-line logic

### File upload: `--upload`

Upload a local file to a remote path (via base64 encoding):

```bash
py D:/remote_control/bridge_run.py --upload D:/local/script.py /tmp/remote_script.py
```

### Recovery: `--cancel`

Kill a stuck command and recover the terminal:

```bash
py D:/remote_control/bridge_run.py --cancel
```

Sends Ctrl+C three times and waits for the terminal to become idle.

### Fallback method: manual IPC

Use only when `bridge_run.py` is unavailable.

```bash
echo "EXEC:req_001:your_command" > ~/.terminal-bridge/command.txt
# Poll status.json for completion
cat ~/.terminal-bridge/status.json
# Then read result
cat ~/.terminal-bridge/result.txt
```

## Command Types

### EXEC -- Execute and capture output (default)

```bash
py D:/remote_control/bridge_run.py "nvidia-smi"
```

- Sends command through the PTY with start/end markers
- Output captured from pyte screen between markers
- Works transparently through SSH sessions
- Default timeout: 120s (bridge-side), 130s (poll-side)

### BATCH -- Multiple commands in one request

BATCH requires manual IPC (not yet in bridge_run.py):

```bash
printf "BATCH:batch_001\nEXEC:nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader\nEXEC:ps aux | grep python | grep -v grep\nEXEC:ls -lt ~/output/ | head -5\nEND_BATCH" > ~/.terminal-bridge/command.txt
```

Then poll `status.json` for `batch_001` completion and read `result.txt`.

### LAST -- Quick screen check (no command sent)

```bash
py D:/remote_control/bridge_run.py --last 5
```

## Result Format

```
[req_id: r42871]
[status: done]
[exit_code: 0]
[timestamp: 2026-04-03T16:46:25]
[duration: 1.6s]
[cmd: ls ~/data/]
table1  table2  table3
```

Use `--raw` flag to skip the metadata header and get only the command output.

## Critical Rules

1. **NEVER use heredoc (`<<`) in commands.** It deadlocks the bridge. `bridge_run.py` blocks this automatically. Use `--script` for multi-line logic.

2. **For multi-line scripts, ALWAYS use `--script`.** Write a local .py/.sh file, then `--script` it. Never try to pass complex Python one-liners with nested quotes.

3. **Commands are serialized.** The bridge holds a lock so only one EXEC/QUERY/BATCH runs at a time. `bridge_run.py` auto-waits if the bridge is busy.

4. **Never send passwords via command.txt.** SSH passwords must be typed manually in the bridge window.

5. **Use nohup for long-running tasks.** Don't EXEC a command that runs for hours -- it will timeout. Instead:
   ```bash
   py D:/remote_control/bridge_run.py "nohup python -u script.py > log.txt 2>&1 & echo PID=\$!"
   ```
   Then monitor with `py D:/remote_control/bridge_run.py "tail -5 log.txt"`.

6. **Verify SSH before first command.** Use `--last 3` to check for a Linux prompt (`[user@host ~]$`). If you see a Windows prompt, ask the user to SSH in.

7. **If the bridge is stuck, use `--cancel`.** Don't try to send more commands -- they will queue behind the stuck one.

8. **Never tail tqdm logs.** Progress bars with `\r` characters block the bridge. Use `wc -l`, `grep Done`, or `--last` instead.

## Common Workflows

### Check experiment progress
```bash
py D:/remote_control/bridge_run.py "ls ~/output/ | wc -l"
```

### Run analysis script on remote data
```bash
# Write analysis locally, run remotely
py D:/remote_control/bridge_run.py --script D:/project/analyze_results.py --raw
```

### GPU + processes check
```bash
printf "BATCH:chk\nEXEC:nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader\nEXEC:ps aux | grep python | grep -v grep\nEXEC:ls -lt ~/output/ | head -5\nEND_BATCH" > ~/.terminal-bridge/command.txt && sleep 15 && cat ~/.terminal-bridge/result.txt
```

### Deploy a script to remote and run it
```bash
py D:/remote_control/bridge_run.py --upload D:/project/run_experiment.py ~/experiment/run.py
py D:/remote_control/bridge_run.py "cd ~/experiment && nohup python -u run.py > run.log 2>&1 & echo PID=\$!"
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Bridge not running" error | Ask user to start: `py D:\remote_control\terminal_bridge.py` |
| "Another bridge is already running" | User must close old bridge window first, or `taskkill /F /PID <pid>` |
| Bridge stuck / not responding | Run `py D:/remote_control/bridge_run.py --cancel` |
| "bridge is busy, waiting..." | A previous command is still running. Wait or `--cancel` |
| "heredoc will deadlock" error | Use `--script` instead of `<<` in commands |
| TIMEOUT on commands | Check if SSH is connected (`--last 3`). Increase `--timeout` if command is slow. |
| Output garbled with escape codes | Use `--clean` flag |
| SSH disconnected | Use `--last 3` to verify. Ask user to SSH again in bridge window |