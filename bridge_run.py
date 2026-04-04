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
  py bridge_run.py --script local.py                 Upload & run a local script
  py bridge_run.py --script local.py --interpreter bash  Upload & run as bash
  py bridge_run.py --cancel                          Send Ctrl+C to kill stuck command
  py bridge_run.py --upload local.py /tmp/remote.py  Upload file to remote path
"""

import os
import sys
import json
import time
import base64
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


def is_bridge_busy():
    """Check if bridge has a currently running command."""
    status = read_status()
    if not status:
        return False
    current = status.get("exec_queue", {}).get("current")
    return current is not None and current.get("status") == "running"


def wait_bridge_idle(timeout=60):
    """Wait until bridge is idle (no running command). Returns True if idle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_bridge_busy():
            return True
        time.sleep(0.5)
    return False


def send_cancel():
    """Send Ctrl+C to kill the current command, with retry."""
    for _ in range(3):
        write_command("KEY:CTRL+C")
        time.sleep(1.0)
    # Wait for bridge to become idle
    if wait_bridge_idle(timeout=10):
        print("OK: command cancelled, bridge is idle", file=sys.stderr)
    else:
        print("WARNING: sent Ctrl+C x3 but bridge may still be busy", file=sys.stderr)


def check_dangerous_patterns(cmd):
    """Warn about command patterns known to cause bridge deadlocks."""
    # Heredoc: << causes the bridge marker-wrapped command to never terminate
    if "<<" in cmd:
        print("ERROR: heredoc (<<) will deadlock the bridge.", file=sys.stderr)
        print("  Use --script to upload and run a local file instead.", file=sys.stderr)
        sys.exit(1)


def upload_and_run_script(local_path, interpreter="python3", timeout=130, raw=False):
    """Upload a local script to remote via base64, then execute it.

    This avoids all quoting/escaping issues by encoding the file content
    as base64 and decoding on the remote side.
    """
    if not os.path.isfile(local_path):
        print(f"ERROR: file not found: {local_path}", file=sys.stderr)
        sys.exit(1)

    with open(local_path, "rb") as f:
        content = f.read()
    b64 = base64.b64encode(content).decode("ascii")

    # Determine remote temp path
    ext = os.path.splitext(local_path)[1] or ".py"
    remote_path = f"/tmp/_bridge_script{ext}"

    # Build command: decode base64 to file, then run it
    # Split base64 into chunks to avoid line-too-long issues
    cmd = f"echo '{b64}' | base64 -d > {remote_path} && {interpreter} {remote_path}"

    check_bridge()
    req_id = gen_id()

    # Check busy
    if is_bridge_busy():
        print("WARNING: bridge is busy, waiting...", file=sys.stderr)
        if not wait_bridge_idle(timeout=30):
            print("ERROR: bridge still busy. Use --cancel first.", file=sys.stderr)
            sys.exit(1)

    write_command(f"EXEC:{req_id}:{cmd}")

    completed = poll_completion(req_id, timeout=timeout)
    if completed is None:
        print(f"TIMEOUT after {timeout}s (req_id={req_id})", file=sys.stderr)
        sys.exit(124)

    result = read_result()
    if not result:
        print("ERROR: result.txt empty after completion", file=sys.stderr)
        sys.exit(1)

    if raw:
        print(strip_metadata(result), end="")
    else:
        print(result, end="")

    exit_code = completed.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        sys.exit(exit_code)


def upload_file(local_path, remote_path):
    """Upload a local file to a remote path via base64 encoding."""
    if not os.path.isfile(local_path):
        print(f"ERROR: file not found: {local_path}", file=sys.stderr)
        sys.exit(1)

    with open(local_path, "rb") as f:
        content = f.read()
    b64 = base64.b64encode(content).decode("ascii")

    cmd = f"echo '{b64}' | base64 -d > {remote_path}"

    check_bridge()
    req_id = gen_id()

    if is_bridge_busy():
        print("WARNING: bridge is busy, waiting...", file=sys.stderr)
        if not wait_bridge_idle(timeout=30):
            print("ERROR: bridge still busy. Use --cancel first.", file=sys.stderr)
            sys.exit(1)

    write_command(f"EXEC:{req_id}:{cmd}")

    completed = poll_completion(req_id, timeout=30)
    if completed is None:
        print("TIMEOUT uploading file", file=sys.stderr)
        sys.exit(124)

    result = read_result()
    print(f"Uploaded {local_path} -> {remote_path} ({len(content)} bytes)", file=sys.stderr)

    exit_code = completed.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        print(f"ERROR: remote write failed (exit {exit_code})", file=sys.stderr)
        sys.exit(exit_code)


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
    parser.add_argument("--cancel", action="store_true", help="Send Ctrl+C to kill stuck command")
    parser.add_argument("--script", metavar="FILE", help="Upload & execute a local script file")
    parser.add_argument("--interpreter", default="python3", help="Interpreter for --script (default: python3)")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"), help="Upload local file to remote path")
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

    # --cancel: send Ctrl+C to kill stuck command
    if args.cancel:
        check_bridge()
        send_cancel()
        return

    # --script: upload and execute a local file
    if args.script:
        timeout = args.timeout or 130
        upload_and_run_script(args.script, args.interpreter, timeout, args.raw)
        return

    # --upload: upload file to remote
    if args.upload:
        check_bridge()
        upload_file(args.upload[0], args.upload[1])
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

    # Safety checks
    check_dangerous_patterns(args.command)

    if is_bridge_busy():
        print("WARNING: bridge is busy, waiting up to 30s...", file=sys.stderr)
        if not wait_bridge_idle(timeout=30):
            print("ERROR: bridge still busy. Use --cancel first.", file=sys.stderr)
            sys.exit(1)

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
