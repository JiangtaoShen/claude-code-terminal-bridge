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
| `~/.terminal-bridge/result.txt` | Bridge → You | Clean output from EXEC/QUERY/BATCH/LAST/SCROLLBACK |
| `~/.terminal-bridge/output.txt` | Bridge → You | Raw terminal screen (fallback only) |
| `~/.terminal-bridge/status.json` | Bridge → You | Bridge status, timestamps & exec queue state |

## How to Execute a Remote Command

**Standard pattern — always use request IDs:**

```bash
echo "EXEC:req_001:your_command_here" > ~/.terminal-bridge/command.txt
```

Then poll `status.json` to check completion:

```bash
cat ~/.terminal-bridge/status.json
```

Look for `exec_queue.last_completed.req_id == "req_001"` and `status == "done"`. Once confirmed, read the result:

```bash
cat ~/.terminal-bridge/result.txt
```

The result includes structured metadata:
```
[req_id: req_001]
[status: done]
[exit_code: 0]
[timestamp: 2026-04-03T02:05:11]
[duration: 0.3s]
[cmd: nvidia-smi]
...actual output...
```

**One-liner for simple cases:**

```bash
echo "EXEC:req_001:your_command" > ~/.terminal-bridge/command.txt && sleep 5 && cat ~/.terminal-bridge/result.txt
```

Verify `[req_id: req_001]` in the output matches your request before trusting the result.

## Command Types

### EXEC — Full output capture with request ID

```bash
echo "EXEC:req_001:nvidia-smi" > ~/.terminal-bridge/command.txt
```

- Executes in a background shell, does not mix with terminal stream
- Result written to result.txt with structured metadata (req_id, exit_code, timestamp, duration)
- Always include a unique req_id to correlate request with response

### EXEC:CLEAN — EXEC with ANSI escape cleaning

```bash
echo "EXEC:CLEAN:req_002:cat ~/log.log | grep Finished" > ~/.terminal-bridge/command.txt
```

- Same as EXEC but automatically strips ANSI escape sequences and `\r` from output
- Use when remote output contains tqdm progress bars, colored text, or other ANSI codes

### QUERY — Fast lightweight query

```bash
echo "QUERY:req_003:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader" > ~/.terminal-bridge/command.txt
```

- For short output commands (<20 lines)
- Captures output directly from screen changes (no file redirect on remote)
- Faster than EXEC for quick checks, 15s timeout

### BATCH — Multiple commands in one request

```bash
printf "BATCH:batch_001\nEXEC:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader\nEXEC:ps aux | grep train\nEXEC:ls -lt ~/output/ | head -5\nEND_BATCH" > ~/.terminal-bridge/command.txt
```

- Executes commands sequentially, results separated in result.txt
- Each command's exit code is tracked
- Reduces round-trip overhead vs sending 3 separate EXECs

### SCROLLBACK — Export scrollback history

```bash
echo "SCROLLBACK:200" > ~/.terminal-bridge/command.txt && sleep 2 && cat ~/.terminal-bridge/result.txt
```

- Exports last N lines from pyte scrollback history + current display
- Use when screen was overwritten by progress bars but you need earlier output

### LAST — Quick screen check (no command sent)

```bash
echo "LAST:5" > ~/.terminal-bridge/command.txt && sleep 2 && cat ~/.terminal-bridge/result.txt
```

- Reads last N non-empty lines from current screen without sending any command
- Perfect for checking progress bars, prompts, or connection state

### RAW / KEY — Send raw bytes or special keys

```bash
echo "KEY:CTRL+C" > ~/.terminal-bridge/command.txt    # Cancel running command
echo "KEY:CTRL+D" > ~/.terminal-bridge/command.txt    # EOF / logout
echo "RAW:\x03" > ~/.terminal-bridge/command.txt      # Same as CTRL+C
```

## Checking Execution Status

Read `status.json` to check if a command is still running:

```bash
cat ~/.terminal-bridge/status.json
```

The `exec_queue` field shows:
```json
{
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

**Polling pattern:**
1. Generate unique req_id, write EXEC command
2. Poll `status.json` until `exec_queue.last_completed.req_id` matches your req_id
3. Read `result.txt` — verify `[req_id:]` matches

## Critical Rules

1. **Always use request IDs.** Every EXEC/QUERY command must include a unique req_id. Verify req_id in result.txt matches before trusting the output.

2. **ONE command at a time.** Never send a new EXEC before the previous one completes. Check status.json before sending the next command.

3. **Never send passwords via command.txt.** SSH passwords must be typed manually in the bridge window.

4. **Use nohup for long-running tasks.** Don't EXEC a command that runs for hours — it will timeout (120s). Instead:
   ```
   EXEC:req_010:nohup python -u script.py > log.txt 2>&1 & echo PID=$!
   ```
   Then monitor with `EXEC:req_011:tail -5 log.txt`.

5. **Use EXEC:CLEAN for logs with ANSI codes.** If grep returns "Binary file matches" or output is garbled with escape sequences, use the CLEAN variant.

6. **Use BATCH for multiple queries.** Instead of sending 3 separate EXECs with sleep between each, send a single BATCH command.

7. **Use SCROLLBACK when screen is overwritten.** If progress bars have pushed useful output off screen, `SCROLLBACK:200` retrieves history.

8. **After bridge reconnect**, always verify the session first:
   ```
   echo "LAST:3" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt
   ```

## Common Workflows

### Check GPU + processes + files in one call
```bash
printf "BATCH:chk_001\nEXEC:nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader\nEXEC:ps aux | grep python.*train | grep -v grep\nEXEC:ls -lt ~/output/ | head -5\nEND_BATCH" > ~/.terminal-bridge/command.txt && sleep 15 && cat ~/.terminal-bridge/result.txt
```

### Read log with ANSI cleaning
```bash
echo "EXEC:CLEAN:log_001:tail -50 ~/train.log | grep -i 'epoch\|loss\|accuracy'" > ~/.terminal-bridge/command.txt && sleep 8 && cat ~/.terminal-bridge/result.txt
```

### Quick GPU check
```bash
echo "QUERY:gpu_001:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader" > ~/.terminal-bridge/command.txt && sleep 3 && cat ~/.terminal-bridge/result.txt
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| result.txt shows wrong req_id | Result is from a previous command — poll status.json until your req_id appears in last_completed |
| result.txt not updating | Check status.json exec_queue.current — command may still be running |
| Output garbled with escape codes | Use `EXEC:CLEAN:` variant to strip ANSI escapes |
| Screen overwritten by progress bars | Use `SCROLLBACK:200` to retrieve history |
| Bridge exited | Ask user to restart: `py D:\remote_control\terminal_bridge.py` then SSH again |
| SSH disconnected | Check output.txt for `Connection reset`. Ask user to SSH again in bridge window |
| Command timeout (120s) | Use nohup for long commands; EXEC is for commands that finish quickly |
