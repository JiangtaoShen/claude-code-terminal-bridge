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

## IPC Files

| File | Direction | Purpose |
|------|-----------|---------|
| `~/.terminal-bridge/command.txt` | You → Bridge | Write commands here |
| `~/.terminal-bridge/result.txt` | Bridge → You | Clean output from EXEC/LAST |
| `~/.terminal-bridge/output.txt` | Bridge → You | Raw terminal screen (fallback only) |
| `~/.terminal-bridge/status.json` | Bridge → You | Bridge status & timestamps |

## How to Execute a Remote Command

**Standard pattern — always use this:**

```bash
echo "EXEC:your_command_here" > ~/.terminal-bridge/command.txt && sleep N && cat ~/.terminal-bridge/result.txt
```

- `sleep N`: Wait time depends on command complexity:
  - Simple commands (ls, cat, wc): `sleep 5`
  - Medium commands (grep, find, pip install): `sleep 10`
  - Heavy commands (conda, python -c): `sleep 8`
- Always read `result.txt` (NOT `output.txt`) for clean output

**If result.txt shows stale/old output** (same timestamp as before), the bridge is still processing. Options:
1. Wait longer: `sleep 10 && cat ~/.terminal-bridge/result.txt`
2. Read raw screen as fallback: `cat ~/.terminal-bridge/output.txt`

## How to Check Progress (Lightweight)

For quick status checks (progress bars, prompts), use LAST — it reads the screen without sending any command:

```bash
echo "LAST:5" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt
```

## How to Send Special Keys

```bash
echo "KEY:CTRL+C" > ~/.terminal-bridge/command.txt    # Cancel running command
echo "KEY:CTRL+D" > ~/.terminal-bridge/command.txt    # EOF / logout
```

## Critical Rules

1. **ONE command at a time.** Never send a new EXEC before the previous one completes. Wait for result.txt to update before sending the next command.

2. **Never send passwords via command.txt.** SSH passwords must be typed manually in the bridge window.

3. **Use nohup for long-running tasks.** Don't EXEC a command that runs for hours — it will timeout (120s). Instead:
   ```
   EXEC:nohup python -u script.py > log.txt 2>&1 & echo PID=$!
   ```
   Then monitor with `EXEC:tail -5 log.txt` or `EXEC:ls output/ | wc -l`.

4. **Chain commands with &&** when they depend on each other:
   ```
   EXEC:conda activate myenv && cd /path && python script.py
   ```

5. **result.txt staleness check.** Compare the `[timestamp:]` line in result.txt with current time. If it's old, the bridge hasn't processed your command yet — wait longer or check output.txt.

6. **After bridge reconnect**, always verify the session first:
   ```
   echo "LAST:3" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt
   ```
   Look for the remote prompt (e.g., `[user@host ~]$`). If you see local CMD prompt (`C:\Users\...>`), SSH is not connected.

7. **output.txt is a fallback.** It contains the full terminal screen with noise (banners, old commands, progress bars). Only read it when result.txt is stuck and you need to see what's happening.

## Common Workflows

### Deploy and run a script
```bash
# 1. Check connection
echo "LAST:3" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt

# 2. Run script in background
echo "EXEC:cd ~/project && nohup python -u main.py > run.log 2>&1 & echo PID=\$!" > ~/.terminal-bridge/command.txt && sleep 8 && cat ~/.terminal-bridge/result.txt

# 3. Check progress
echo "EXEC:ls ~/project/output/ | wc -l" > ~/.terminal-bridge/command.txt && sleep 5 && cat ~/.terminal-bridge/result.txt

# 4. Check log
echo "EXEC:tail -10 ~/project/run.log" > ~/.terminal-bridge/command.txt && sleep 5 && cat ~/.terminal-bridge/result.txt
```

### Check GPU status
```bash
echo "EXEC:nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total --format=csv,noheader" > ~/.terminal-bridge/command.txt && sleep 5 && cat ~/.terminal-bridge/result.txt
```

### Kill a process
```bash
echo "EXEC:kill PID_NUMBER" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| result.txt not updating | Read output.txt as fallback; bridge may be processing a slow command |
| Bridge exited | Ask user to restart: `py D:\remote_control\terminal_bridge.py` then SSH again |
| SSH disconnected | Check output.txt for `Connection reset`. Ask user to SSH again in bridge window |
| Command timeout (120s) | Use nohup for long commands; EXEC is for commands that finish quickly |
