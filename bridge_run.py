"""
Bridge Run - Execute commands on a remote server via Terminal Bridge in one call.

Replaces the manual echo+sleep+cat pattern with a single command that
writes to command.txt, polls status.json for completion, and prints result.txt.

Usage:
  py bridge_run.py "ls ~/data"                      Single command
  py bridge_run.py --clean "tail -50 ~/train.log"   Strip ANSI escapes
  py bridge_run.py --timeout 30 "quick_check"       Custom timeout
  py bridge_run.py --last 5                          Read last 5 screen lines
  py bridge_run.py --scroll 200                      Read scrollback history
  py bridge_run.py --status                          Show bridge status
  py bridge_run.py --raw "ls"                        Output only (no metadata)
"""

import os
import sys
import json
import time
import argparse

BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".terminal-bridge")
COMMAND_FILE = os.path.join(BRIDGE_DIR, "command.txt")
STATUS_FILE = os.path.join(BRIDGE_DIR, "status.json")
RESULT_FILE = os.path.join(BRIDGE_DIR, "result.txt")


def read_status():
    """Read and parse status.json. Returns dict or None."""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_command(content):
    """Write command to command.txt."""
    with open(COMMAND_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def read_result():
    """Read result.txt content."""
    try:
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def poll_completion(req_id, timeout=130, poll_interval=0.3):
    """Poll status.json until req_id appears in last_completed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = read_status()
        if status:
            last = status.get("exec_queue", {}).get("last_completed", {})
            if last.get("req_id") == req_id and last.get("status") == "done":
                return last
        time.sleep(poll_interval)
    return None


def strip_metadata(result_text):
    """Remove metadata header lines (those starting with [) from result."""
    lines = result_text.splitlines()
    # Skip leading metadata lines
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("["):
            start = i + 1
        else:
            break
    return "\n".join(lines[start:])


def gen_id():
    """Generate a short unique request ID."""
    return f"r{int(time.time() * 100) % 1000000}"


def check_bridge():
    """Verify bridge is running. Exit with error if not."""
    status = read_status()
    if not status:
        print("ERROR: Bridge not running. Start it with:", file=sys.stderr)
        print("  py D:\\remote_control\\terminal_bridge.py", file=sys.stderr)
        sys.exit(1)
    if not status.get("pty_alive"):
        print("ERROR: Bridge PTY is dead. Restart the bridge.", file=sys.stderr)
        sys.exit(1)
    return status


def main():
    parser = argparse.ArgumentParser(
        description="Execute commands via Terminal Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", help="Command to execute on remote")
    parser.add_argument("--clean", action="store_true", help="Strip ANSI escapes from output")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout in seconds (default: 130)")
    parser.add_argument("--last", type=int, metavar="N", help="Read last N screen lines (no exec)")
    parser.add_argument("--scroll", type=int, metavar="N", help="Read N scrollback lines (no exec)")
    parser.add_argument("--status", action="store_true", help="Show bridge status")
    parser.add_argument("--raw", action="store_true", help="Output only command result (skip metadata)")
    parser.add_argument("--id", default=None, help="Custom request ID")
    args = parser.parse_args()

    # --status: just print status.json
    if args.status:
        status = read_status()
        if status:
            print(json.dumps(status, indent=2))
        else:
            print("Bridge not running", file=sys.stderr)
            sys.exit(1)
        return

    # --last N: send LAST:N, sleep briefly, read result
    if args.last is not None:
        check_bridge()
        write_command(f"LAST:{args.last}")
        time.sleep(1.5)
        result = read_result()
        print(result or "", end="")
        return

    # --scroll N: send SCROLLBACK:N, sleep briefly, read result
    if args.scroll is not None:
        check_bridge()
        write_command(f"SCROLLBACK:{args.scroll}")
        time.sleep(2)
        result = read_result()
        print(result or "", end="")
        return

    # EXEC: require a command
    if not args.command:
        parser.print_help()
        sys.exit(1)

    check_bridge()

    req_id = args.id or gen_id()
    timeout = args.timeout or 130

    # Build command line
    if args.clean:
        cmd_line = f"EXEC:CLEAN:{req_id}:{args.command}"
    else:
        cmd_line = f"EXEC:{req_id}:{args.command}"

    # Send command
    write_command(cmd_line)

    # Poll for completion
    completed = poll_completion(req_id, timeout=timeout)

    if completed is None:
        print(f"TIMEOUT after {timeout}s (req_id={req_id})", file=sys.stderr)
        sys.exit(124)

    # Read result
    result = read_result()
    if not result:
        print("ERROR: result.txt empty after completion", file=sys.stderr)
        sys.exit(1)

    # Verify req_id matches
    if f"[req_id: {req_id}]" not in result:
        print(f"WARNING: result.txt req_id mismatch (expected {req_id})", file=sys.stderr)

    # Output
    if args.raw:
        print(strip_metadata(result), end="")
    else:
        print(result, end="")

    # Exit with remote command's exit code
    exit_code = completed.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
