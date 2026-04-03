"""
Terminal Bridge - Let Claude Code control terminal sessions (SSH, etc.) via file-based IPC.

Usage:
  1. Start: py terminal_bridge.py
  2. Manually operate in the window (e.g. SSH login)
  3. Claude Code sends commands via command.txt and reads screen via output.txt

Command format:
  (plain text)                        - Send text + Enter
  EXEC:req_id:command                 - Execute and capture output with request ID
  EXEC:CLEAN:req_id:command           - Execute with ANSI escape cleaning
  QUERY:req_id:command                - Fast query for short output (<20 lines)
  BATCH:batch_id\\nEXEC:cmd\\n...\\nEND_BATCH  - Batch execute multiple commands
  SCROLLBACK:N                        - Export last N lines from scrollback history
  LAST:N                              - Write last N non-empty screen lines to result.txt
  RAW:bytes                           - Send raw bytes (supports \\x03 etc.)
  KEY:name                            - Send special key (ENTER, CTRL+C, UP, etc.)
"""

import os
import sys
import json
import re
import time
import threading
import argparse
import msvcrt
import ctypes
from datetime import datetime

from winpty import PTY
import pyte


# ─── Win32 API Helpers ───────────────────────────────────────────────────────

def _get_clipboard_text():
    """Read clipboard text via Win32 API."""
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(0):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        kernel32.GlobalLock.restype = ctypes.c_wchar_p
        text = kernel32.GlobalLock(handle)
        result = str(text) if text else ""
        kernel32.GlobalUnlock(handle)
        return result
    finally:
        user32.CloseClipboard()

# ─── Configuration ───────────────────────────────────────────────────────────

BRIDGE_VERSION = "2.0.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".terminal-bridge")
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
DEFAULT_HISTORY = 1000
SCREEN_CAPTURE_INTERVAL = 0.5   # seconds
COMMAND_POLL_INTERVAL = 0.2     # seconds
PTY_READ_INTERVAL = 0.05        # seconds
DISPLAY_REFRESH_INTERVAL = 0.3  # seconds - console refresh interval

# ANSI escape sequence pattern for cleaning
ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\r')


# ─── Special Key Mapping ─────────────────────────────────────────────────────

KEY_MAP = {
    "CTRL+C": "\x03",
    "CTRL+D": "\x04",
    "CTRL+Z": "\x1a",
    "CTRL+L": "\x0c",
    "CTRL+A": "\x01",
    "CTRL+E": "\x05",
    "CTRL+K": "\x0b",
    "CTRL+U": "\x15",
    "CTRL+W": "\x17",
    "CTRL+R": "\x12",
    "ENTER": "\r",
    "TAB": "\t",
    "BACKSPACE": "\x7f",
    "ESCAPE": "\x1b",
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "RIGHT": "\x1b[C",
    "LEFT": "\x1b[D",
    "HOME": "\x1b[H",
    "END": "\x1b[F",
    "DELETE": "\x1b[3~",
    "PAGEUP": "\x1b[5~",
    "PAGEDOWN": "\x1b[6~",
}


class TerminalBridge:
    def __init__(self, cols, rows, shell, output_path, command_path, status_path):
        self.shell = shell
        self.output_path = output_path
        self.command_path = command_path
        self.status_path = status_path
        self.result_path = os.path.join(os.path.dirname(output_path), "result.txt")
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.last_output_update = ""
        self.last_command_processed = ""
        self.running = True
        self.lock = threading.Lock()

        # Exec queue state (for status.json)
        self._cmd_lock = threading.Lock()   # Serialize PTY command execution
        self._exec_lock = threading.Lock()
        self._exec_current = None      # dict: req_id, cmd, status, started_at
        self._exec_last_completed = None  # dict: req_id, status, exit_code, duration_s, completed_at

        # Auto-detect CMD window size
        win_cols, win_rows = self._detect_console_size()
        # Use specified size or detected window size
        self.cols = min(cols, win_cols) if cols else win_cols
        self.rows = min(rows, win_rows - 1) if rows else (win_rows - 1)  # -1 for status bar
        self.win_cols = win_cols
        self.win_rows = win_rows

        # Initialize PTY (matching detected size)
        self.pty = PTY(self.cols, self.rows)
        self.pty.spawn(shell)

        # Initialize pyte virtual terminal
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=DEFAULT_HISTORY)
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.ByteStream(self.screen)

        # Create empty command.txt
        with open(self.command_path, "w", encoding="utf-8") as f:
            pass

    @staticmethod
    def _detect_console_size():
        """Detect actual visible console window size."""
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except Exception:
            return DEFAULT_COLS, DEFAULT_ROWS

    def start(self):
        """Start all background threads; main thread handles keyboard input."""
        # Startup diagnostics
        print(f"[Bridge v{BRIDGE_VERSION}] Window: {self.win_cols}x{self.win_rows}, PTY: {self.cols}x{self.rows}")
        print(f"[Bridge] Starting in 3 seconds...")
        time.sleep(3)

        # Enable Windows console virtual terminal sequences
        if sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

        threads = [
            threading.Thread(target=self._pty_reader_loop, name="PTYReader", daemon=True),
            threading.Thread(target=self._screen_capture_loop, name="ScreenCapture", daemon=True),
            threading.Thread(target=self._command_watcher_loop, name="CommandWatcher", daemon=True),
            threading.Thread(target=self._display_refresh_loop, name="DisplayRefresh", daemon=True),
        ]
        for t in threads:
            t.start()

        self._write_status()

        # Main thread: read keyboard input and forward to PTY
        self._keyboard_input_loop()

    def _enable_quick_edit(self):
        """Enable QuickEdit mode for right-click paste support."""
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode))
        # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
        kernel32.SetConsoleMode(h_stdin, mode.value | 0x0040 | 0x0080)

    def _keyboard_input_loop(self):
        """Main thread: read keyboard input and forward to PTY."""
        self._enable_quick_edit()
        try:
            while self.running and self.pty.isalive():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        # Function/arrow key prefix, read second byte
                        ch2 = msvcrt.getwch()
                        key = self._translate_windows_key(ch2)
                        if key:
                            self.pty.write(key)
                    elif ch == "\x16":
                        # Ctrl+V: paste clipboard content
                        text = _get_clipboard_text()
                        if text:
                            # Normalize line endings to \r
                            text = text.replace("\r\n", "\r").replace("\n", "\r")
                            self.pty.write(text)
                    else:
                        # Regular character, send directly (Enter becomes \r)
                        if ch == "\r":
                            self.pty.write("\r")
                        else:
                            self.pty.write(ch)
                else:
                    time.sleep(0.01)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._write_status()
            self._clear_console()
            sys.stdout.write("[Bridge] Exited\n")
            sys.stdout.flush()

    def _translate_windows_key(self, ch2):
        """Translate Windows function key codes to ANSI escape sequences."""
        mapping = {
            "H": "\x1b[A",   # Up
            "P": "\x1b[B",   # Down
            "M": "\x1b[C",   # Right
            "K": "\x1b[D",   # Left
            "G": "\x1b[H",   # Home
            "O": "\x1b[F",   # End
            "S": "\x1b[3~",  # Delete
            "I": "\x1b[5~",  # Page Up
            "Q": "\x1b[6~",  # Page Down
        }
        return mapping.get(ch2)

    def _pty_reader_loop(self):
        """Background thread: read PTY output and feed to pyte (no direct console output)."""
        while self.running and self.pty.isalive():
            try:
                data = self.pty.read(blocking=False)
                if data:
                    with self.lock:
                        self.stream.feed(data.encode("utf-8", errors="replace"))
            except Exception:
                pass
            time.sleep(PTY_READ_INTERVAL)

        self.running = False

    def _get_stdout_handle(self):
        """Get stdout handle (cached)."""
        if not hasattr(self, "_h_stdout"):
            self._h_stdout = ctypes.windll.kernel32.GetStdHandle(-11)
        return self._h_stdout

    def _set_cursor_pos(self, x, y):
        """Set console cursor position via Win32 API."""
        handle = self._get_stdout_handle()
        coord = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        ctypes.windll.kernel32.SetConsoleCursorPosition(handle, coord)

    def _set_console_buffer_size(self, cols, rows):
        """Set console buffer size to match window size, eliminating scrollbar."""
        handle = self._get_stdout_handle()
        coord = (cols & 0xFFFF) | ((rows & 0xFFFF) << 16)
        ctypes.windll.kernel32.SetConsoleScreenBufferSize(handle, coord)

    def _clear_console(self):
        """Clear the console screen."""
        os.system("cls")

    def _display_refresh_loop(self):
        """Background thread: overwrite display in-place using cursor repositioning and ANSI clear-to-EOL."""
        last_snapshot = ""
        self._clear_console()

        while self.running:
            try:
                with self.lock:
                    lines = list(self.screen.display)
                    cursor_y = self.screen.cursor.y
                    cursor_x = self.screen.cursor.x

                snapshot = f"{cursor_y},{cursor_x}:" + "\n".join(lines)

                if snapshot != last_snapshot:
                    last_snapshot = snapshot

                    now = datetime.now().strftime("%H:%M:%S")
                    status = f" BRIDGE | {self.cols}x{self.rows} | ({cursor_y},{cursor_x}) | {now} "

                    # Move cursor to (0,0)
                    self._set_cursor_pos(0, 0)

                    buf = []
                    # Status bar (reverse video + clear to end of line)
                    buf.append(f"\x1b[7m{status}\x1b[0m\x1b[K")

                    # PTY lines: output exactly 'rows' lines, clear trailing chars with \x1b[K
                    for i in range(self.rows):
                        line = lines[i] if i < len(lines) else ""
                        text = line.rstrip()
                        if i == cursor_y:
                            # Highlight cursor position with reverse video
                            padded = line  # preserve spaces for correct cursor positioning
                            chars = list(padded)
                            if cursor_x < len(chars):
                                chars[cursor_x] = "\x1b[7m" + chars[cursor_x] + "\x1b[0m"
                            text = "".join(chars).rstrip()
                        buf.append(text + "\x1b[K")

                    # Join with \n but no trailing \n (prevents scrolling)
                    # Append \x1b[J to clear everything below
                    sys.stdout.write("\n".join(buf) + "\x1b[J")
                    sys.stdout.flush()
            except Exception:
                pass
            time.sleep(DISPLAY_REFRESH_INTERVAL)

    def _screen_capture_loop(self):
        """Background thread: periodically write pyte screen content to output.txt."""
        while self.running:
            try:
                self._render_output()
            except Exception:
                pass
            time.sleep(SCREEN_CAPTURE_INTERVAL)

        # Final write on exit
        try:
            self._render_output()
        except Exception:
            pass

    def _render_output(self):
        """Write current pyte visible screen to output.txt, stripping trailing blank lines."""
        now = datetime.now().isoformat(timespec="seconds")
        alive = self.pty.isalive()

        with self.lock:
            display_lines = [row.rstrip() for row in self.screen.display]

        # Strip trailing empty lines to save tokens
        while display_lines and not display_lines[-1]:
            display_lines.pop()

        parts = [f"[alive: {str(alive).lower()}] [timestamp: {now}]"]
        parts.extend(display_lines)

        content = "\n".join(parts) + "\n"

        self._atomic_write(self.output_path, content)

        self.last_output_update = now
        self._write_status()

    def _command_watcher_loop(self):
        """Background thread: watch command.txt and send commands to PTY."""
        while self.running and self.pty.isalive():
            try:
                if os.path.exists(self.command_path):
                    with open(self.command_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():
                        self._process_command(content)
                        # Clear command.txt
                        with open(self.command_path, "w", encoding="utf-8") as f:
                            pass
                        self.last_command_processed = datetime.now().isoformat(timespec="seconds")
                        self._write_status()
            except Exception:
                pass
            time.sleep(COMMAND_POLL_INTERVAL)

    def _process_command(self, content):
        """Parse and execute commands.

        Supported prefixes:
          (none)            - Plain command, send text + Enter
          EXEC:id:cmd       - Execute and capture full output with request ID
          EXEC:CLEAN:id:cmd - Execute with ANSI cleaning
          QUERY:id:cmd      - Fast query for short output
          BATCH:id          - Start batch execution block
          SCROLLBACK:N      - Export last N lines from scrollback history
          LAST:N            - Write last N non-empty screen lines to result.txt
          RAW:              - Send raw bytes (supports \\x03 etc.)
          KEY:              - Send special key (ENTER, CTRL+C, UP, etc.)
        """
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            if not line:
                continue

            if line.startswith("EXEC:CLEAN:"):
                # EXEC with ANSI cleaning: EXEC:CLEAN:req_id:command
                rest = line[len("EXEC:CLEAN:"):]
                req_id, cmd = self._parse_req_id_cmd(rest)
                if req_id and cmd:
                    threading.Thread(
                        target=self._exec_pty,
                        args=(cmd, req_id, True),
                        daemon=True,
                    ).start()
            elif line.startswith("EXEC:"):
                # EXEC with request ID: EXEC:req_id:command
                rest = line[5:]
                req_id, cmd = self._parse_req_id_cmd(rest)
                if req_id and cmd:
                    threading.Thread(
                        target=self._exec_pty,
                        args=(cmd, req_id, False),
                        daemon=True,
                    ).start()
            elif line.startswith("QUERY:"):
                # Fast query: QUERY:req_id:command (shorter timeout)
                rest = line[6:]
                req_id, cmd = self._parse_req_id_cmd(rest)
                if req_id and cmd:
                    threading.Thread(
                        target=self._exec_pty,
                        args=(cmd, req_id, False, 30),
                        daemon=True,
                    ).start()
            elif line.startswith("BATCH:"):
                # Batch execution: collect lines until END_BATCH
                batch_id = line[6:].strip()
                batch_cmds = []
                while i < len(lines):
                    bline = lines[i].strip()
                    i += 1
                    if bline == "END_BATCH":
                        break
                    if bline.startswith("EXEC:"):
                        batch_cmds.append(bline[5:].strip())
                if batch_id and batch_cmds:
                    threading.Thread(
                        target=self._exec_batch_pty,
                        args=(batch_id, batch_cmds),
                        daemon=True,
                    ).start()
            elif line.startswith("SCROLLBACK:"):
                try:
                    n = int(line[11:].strip())
                except ValueError:
                    n = 200
                self._write_scrollback(n)
            elif line.startswith("LAST:"):
                try:
                    n = int(line[5:].strip())
                except ValueError:
                    n = 10
                self._write_last_lines(n)
            elif line.startswith("RAW:"):
                raw = line[4:]
                raw = raw.encode("utf-8").decode("unicode_escape")
                self.pty.write(raw)
            elif line.startswith("KEY:"):
                key_name = line[4:].strip().upper()
                if key_name in KEY_MAP:
                    self.pty.write(KEY_MAP[key_name])
            else:
                # Plain command: send text + Enter
                self.pty.write(line + "\r")

    @staticmethod
    def _parse_req_id_cmd(text):
        """Parse 'req_id:command' from text. Returns (req_id, cmd) or (None, None)."""
        idx = text.find(":")
        if idx == -1:
            return None, None
        return text[:idx].strip(), text[idx + 1:].strip()

    @staticmethod
    def _clean_ansi(text):
        """Remove ANSI escape sequences and carriage returns from text."""
        return ANSI_ESCAPE_RE.sub('', text)

    def _atomic_write(self, path, content):
        """Write content to file atomically using os.replace()."""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

    def _set_exec_state(self, req_id, cmd, status, exit_code=None, started_at=None):
        """Update exec queue state for status.json."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._exec_lock:
            if status == "running":
                self._exec_current = {
                    "req_id": req_id,
                    "cmd": cmd[:100],  # truncate long commands
                    "status": "running",
                    "started_at": started_at or now,
                }
            elif status == "done":
                duration_s = None
                if self._exec_current and self._exec_current.get("started_at"):
                    try:
                        t0 = datetime.fromisoformat(self._exec_current["started_at"])
                        duration_s = round((datetime.now() - t0).total_seconds(), 1)
                    except Exception:
                        pass
                self._exec_last_completed = {
                    "req_id": req_id,
                    "status": "done",
                    "exit_code": exit_code,
                    "duration_s": duration_s,
                    "completed_at": now,
                }
                self._exec_current = None
        self._write_status()

    # ─── PTY-through execution (works through SSH) ────────────────────────

    def _send_and_capture(self, cmd, req_id, timeout=120):
        """Send command through PTY with start/end markers and capture output.

        Caller must hold _cmd_lock. Returns (result_lines, exit_code).
        The command is sent as: echo <START>; <cmd>; echo <END> $?
        Output between markers is captured from pyte history + screen.
        """
        start_marker = f"__S_{req_id}__"
        end_marker = f"__E_{req_id}__"

        # Clear scrollback history for clean capture
        with self.lock:
            if hasattr(self.screen, "history"):
                self.screen.history.top.clear()

        # Send command with markers through PTY
        self.pty.write(f"echo {start_marker}; {cmd}; echo {end_marker} $?\r")

        # Wait for end marker output to appear on screen
        start_time = time.time()
        found = False
        while time.time() - start_time < timeout:
            time.sleep(0.3)
            with self.lock:
                all_lines = self._collect_all_lines()
            for line in all_lines:
                if line.strip().startswith(end_marker):
                    found = True
                    break
            if found:
                break

        if not found:
            # Timeout - send Ctrl+C to cancel hung command
            self.pty.write("\x03")
            time.sleep(0.5)
            return ["[TIMEOUT]"], -1

        # Small delay for screen to stabilize
        time.sleep(0.2)

        # Extract output between markers
        with self.lock:
            all_lines = self._collect_all_lines()

        result_lines = []
        exit_code = 0
        capturing = False
        for line in all_lines:
            stripped = line.strip()
            if not capturing:
                # Match the echo output line (no shell prompt prefix)
                if stripped == start_marker:
                    capturing = True
                continue
            if stripped.startswith(end_marker):
                # Parse exit code from: __E_req_id__ <exit_code>
                remainder = stripped[len(end_marker):].strip()
                try:
                    exit_code = int(remainder)
                except ValueError:
                    pass
                break
            result_lines.append(line)

        return result_lines, exit_code

    def _exec_pty(self, cmd, req_id, clean=False, timeout=120):
        """Execute command through PTY. Thread-safe entry point for EXEC/QUERY."""
        with self._cmd_lock:
            self._set_exec_state(req_id, cmd, "running")
            result_lines, exit_code = self._send_and_capture(cmd, req_id, timeout)
            while result_lines and not result_lines[-1].strip():
                result_lines.pop()
            result_text = "\n".join(result_lines)
            if clean:
                result_text = self._clean_ansi(result_text)
            self._set_exec_state(req_id, cmd, "done", exit_code=exit_code)
            self._write_result_file(req_id, cmd, result_text, exit_code=exit_code)

    def _exec_batch_pty(self, batch_id, cmds, timeout_per_cmd=120):
        """Execute multiple commands sequentially through PTY."""
        with self._cmd_lock:
            self._set_exec_state(batch_id, f"BATCH({len(cmds)} cmds)", "running")

            total = len(cmds)
            all_results = [f"[batch_id: {batch_id}]", f"[total: {total}]", ""]

            for idx, cmd in enumerate(cmds, 1):
                sub_req_id = f"{batch_id}_{idx}"
                result_lines, exit_code = self._send_and_capture(
                    cmd, sub_req_id, timeout_per_cmd
                )
                while result_lines and not result_lines[-1].strip():
                    result_lines.pop()
                all_results.append(f"--- [{idx}/{total}] {cmd} ---")
                all_results.append(f"[exit_code: {exit_code}]")
                all_results.extend(result_lines)
                all_results.append("")

            all_results.append("[batch_status: done]")

            self._set_exec_state(batch_id, f"BATCH({total} cmds)", "done", exit_code=0)

            now = datetime.now().isoformat(timespec="seconds")
            content = (
                f"[batch_id: {batch_id}]\n"
                f"[timestamp: {now}]\n"
                + "\n".join(all_results) + "\n"
            )
            self._atomic_write(self.result_path, content)

    # ─── SCROLLBACK: History export ──────────────────────────────────────────

    def _write_scrollback(self, n):
        """Export last N lines from scrollback history + current display to result.txt."""
        with self.lock:
            history_lines = []
            if hasattr(self.screen, "history") and self.screen.history.top:
                for hline in self.screen.history.top:
                    row = "".join(hline[c].data if c in hline else " " for c in range(self.cols))
                    history_lines.append(row.rstrip())
            display_lines = [r.rstrip() for r in self.screen.display]
            all_lines = history_lines + display_lines

        # Take last n lines
        output = all_lines[-n:] if len(all_lines) > n else all_lines

        now = datetime.now().isoformat(timespec="seconds")
        content = (
            f"[scrollback: last {n} lines (total available: {len(all_lines)})]\n"
            f"[timestamp: {now}]\n"
            + "\n".join(output) + "\n"
        )
        self._atomic_write(self.result_path, content)

    def _write_last_lines(self, n):
        """Write last N non-empty lines from screen to result.txt (no command sent to PTY)."""
        with self.lock:
            display_lines = [row.rstrip() for row in self.screen.display]

        # Filter out empty lines
        non_empty = [l for l in display_lines if l]
        # Take last N
        last_n = non_empty[-n:] if len(non_empty) > n else non_empty

        now = datetime.now().isoformat(timespec="seconds")
        content = (
            f"[screen: last {n} lines]\n"
            f"[timestamp: {now}]\n"
            + "\n".join(last_n) + "\n"
        )
        self._atomic_write(self.result_path, content)

    def _write_result_file(self, req_id, cmd, result_text, exit_code=None):
        """Write structured result to result.txt with request ID and metadata."""
        now = datetime.now().isoformat(timespec="seconds")

        # Calculate duration from exec state
        duration_s = None
        with self._exec_lock:
            if self._exec_last_completed and self._exec_last_completed.get("req_id") == req_id:
                duration_s = self._exec_last_completed.get("duration_s")

        lines = [
            f"[req_id: {req_id}]",
            f"[status: done]",
            f"[exit_code: {exit_code}]",
            f"[timestamp: {now}]",
        ]
        if duration_s is not None:
            lines.append(f"[duration: {duration_s}s]")
        lines.append(f"[cmd: {cmd}]")
        lines.append(result_text)

        self._atomic_write(self.result_path, "\n".join(lines) + "\n")

    def _collect_all_lines(self):
        """Collect all lines from pyte history + current display (must hold self.lock)."""
        all_lines = []
        if hasattr(self.screen, "history") and self.screen.history.top:
            for hline in self.screen.history.top:
                row_text = ""
                for col in range(self.cols):
                    row_text += hline[col].data if col in hline else " "
                all_lines.append(row_text.rstrip())
        all_lines.extend([r.rstrip() for r in self.screen.display])
        return all_lines

    def _write_status(self):
        """Write status.json with current bridge state including exec queue."""
        try:
            with self._exec_lock:
                exec_queue = {}
                if self._exec_current:
                    exec_queue["current"] = dict(self._exec_current)
                if self._exec_last_completed:
                    exec_queue["last_completed"] = dict(self._exec_last_completed)

            status = {
                "version": BRIDGE_VERSION,
                "pid": self.pty.pid if self.pty.pid else None,
                "pty_alive": self.pty.isalive(),
                "shell": self.shell,
                "cols": self.cols,
                "rows": self.rows,
                "started_at": self.started_at,
                "last_output_update": self.last_output_update,
                "last_command_processed": self.last_command_processed,
                "exec_queue": exec_queue,
            }
            tmp_path = self.status_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
            os.replace(tmp_path, self.status_path)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Terminal Bridge for Claude Code")
    parser.add_argument("--shell", default="cmd.exe", help="Shell to spawn (default: cmd.exe)")
    parser.add_argument("--cols", type=int, default=DEFAULT_COLS, help=f"Terminal columns (default: {DEFAULT_COLS})")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS, help=f"Terminal rows (default: {DEFAULT_ROWS})")
    parser.add_argument("--dir", default=DEFAULT_BRIDGE_DIR, help=f"Working directory for IPC files (default: {DEFAULT_BRIDGE_DIR})")
    parser.add_argument("--output", default=None, help="Output file path (default: <dir>/output.txt)")
    parser.add_argument("--command", default=None, help="Command file path (default: <dir>/command.txt)")
    parser.add_argument("--status", default=None, help="Status file path (default: <dir>/status.json)")
    args = parser.parse_args()

    # Ensure working directory exists
    bridge_dir = os.path.abspath(args.dir)
    os.makedirs(bridge_dir, exist_ok=True)

    # Single-instance lock
    lock_path = os.path.join(bridge_dir, "bridge.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.write(lock_fd, str(os.getpid()).encode())
        os.ftruncate(lock_fd, len(str(os.getpid())))
    except OSError:
        try:
            with open(lock_path, "r") as f:
                old_pid = f.read().strip()
        except Exception:
            old_pid = "unknown"
        print(f"[Bridge] ERROR: Another bridge is already running (PID: {old_pid})")
        print(f"[Bridge] Close the other bridge window first, or: taskkill /F /PID {old_pid}")
        sys.exit(1)

    # Resolve IPC file paths (default to --dir)
    output_path = args.output or os.path.join(bridge_dir, "output.txt")
    command_path = args.command or os.path.join(bridge_dir, "command.txt")
    status_path = args.status or os.path.join(bridge_dir, "status.json")

    bridge = TerminalBridge(
        cols=args.cols,
        rows=args.rows,
        shell=args.shell,
        output_path=output_path,
        command_path=command_path,
        status_path=status_path,
    )
    bridge.start()


if __name__ == "__main__":
    main()
