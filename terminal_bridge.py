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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".terminal-bridge")
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
DEFAULT_HISTORY = 1000
SCREEN_CAPTURE_INTERVAL = 0.5   # seconds
COMMAND_POLL_INTERVAL = 0.2     # seconds
PTY_READ_INTERVAL = 0.05        # seconds
DISPLAY_REFRESH_INTERVAL = 0.3  # seconds - console refresh interval
EXEC_RESULT_FILE = "/tmp/bridge_result.txt"  # temp file on remote server
EXEC_SIGNAL_FILE = "/tmp/bridge_signal.txt"  # completion signal file on remote
EXEC_DONE_MARKER = "__BRIDGE_DONE_8f3a__"    # command completion marker

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
        print(f"[Bridge] Window: {self.win_cols}x{self.win_rows}, PTY: {self.cols}x{self.rows}")
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
                        target=self._exec_silent,
                        args=(cmd, req_id, True),
                        daemon=True,
                    ).start()
            elif line.startswith("EXEC:"):
                # EXEC with request ID: EXEC:req_id:command
                rest = line[5:]
                req_id, cmd = self._parse_req_id_cmd(rest)
                if req_id and cmd:
                    threading.Thread(
                        target=self._exec_silent,
                        args=(cmd, req_id, False),
                        daemon=True,
                    ).start()
            elif line.startswith("QUERY:"):
                # Fast query: QUERY:req_id:command
                rest = line[6:]
                req_id, cmd = self._parse_req_id_cmd(rest)
                if req_id and cmd:
                    threading.Thread(
                        target=self._exec_query,
                        args=(cmd, req_id),
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
                        target=self._exec_batch,
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

    # ─── EXEC: Silent execution via background shell ─────────────────────────

    def _exec_silent(self, cmd, req_id, clean=False, timeout=120):
        """Execute command in background shell, write result to file. Does not mix with terminal stream."""
        self._set_exec_state(req_id, cmd, "running")
        start = time.time()

        # Wrap command to execute in background, capture output, and write signal file
        if clean:
            # Pipe through sed to strip ANSI escapes, then tr to remove \r
            wrapped = (
                f"{{ {{ {cmd}; }} 2>&1 | sed 's/\\x1b\\[[0-9;]*[a-zA-Z]//g' | tr -d '\\r'; }} "
                f"> {EXEC_RESULT_FILE}; "
                f"echo \"$?\" > {EXEC_SIGNAL_FILE}; "
                f"echo {req_id} >> {EXEC_SIGNAL_FILE}"
            )
        else:
            wrapped = (
                f"{{ {cmd}; }} > {EXEC_RESULT_FILE} 2>&1; "
                f"echo \"$?\" > {EXEC_SIGNAL_FILE}; "
                f"echo {req_id} >> {EXEC_SIGNAL_FILE}"
            )

        self.pty.write(f"({wrapped}) &\r")

        # Poll for signal file containing our req_id
        poll_cmd_sent = False
        exit_code = None
        while time.time() - start < timeout:
            time.sleep(0.5)
            # Send a cat command to check signal file
            if not poll_cmd_sent:
                time.sleep(0.3)
                poll_cmd_sent = True

            # Check screen for signal
            with self.lock:
                all_text = "\n".join(self.screen.display)

            # Also try reading signal via a quick check
            check_marker = f"__CHK_{req_id}__"
            self.pty.write(f"cat {EXEC_SIGNAL_FILE} 2>/dev/null && echo {check_marker}\r")
            time.sleep(0.5)

            with self.lock:
                screen_text = "\n".join(self._collect_all_lines())

            if check_marker in screen_text and req_id in screen_text:
                # Extract exit code from signal file content shown on screen
                for sline in screen_text.split("\n"):
                    sline = sline.strip()
                    if sline.isdigit() or (sline.startswith("-") and sline[1:].isdigit()):
                        try:
                            exit_code = int(sline)
                        except ValueError:
                            pass
                break
        else:
            # Timeout
            self._set_exec_state(req_id, cmd, "done", exit_code=-1)
            self._write_result_file(req_id, cmd, "[TIMEOUT]", exit_code=-1)
            return

        # Read the result file content
        time.sleep(0.2)
        begin_marker = f"__BR_{req_id}__"
        end_marker = f"__ER_{req_id}__"

        with self.lock:
            if hasattr(self.screen, "history"):
                self.screen.history.top.clear()

        self.pty.write(f"echo {begin_marker}; cat {EXEC_RESULT_FILE}; echo {end_marker}\r")

        # Wait for end marker
        deadline = time.time() + 30
        found = False
        while time.time() < deadline:
            time.sleep(0.3)
            with self.lock:
                all_lines = self._collect_all_lines()
            if end_marker in "\n".join(all_lines):
                found = True
                break

        if not found:
            self._set_exec_state(req_id, cmd, "done", exit_code=exit_code)
            self._write_result_file(req_id, cmd, "[ERROR: timeout reading result]", exit_code=exit_code)
            return

        # Extract result between markers
        with self.lock:
            all_lines = self._collect_all_lines()

        result_lines = []
        capturing = False
        for rline in all_lines:
            if begin_marker in rline:
                capturing = True
                continue
            if capturing:
                if end_marker in rline:
                    break
                result_lines.append(rline)

        result_text = "\n".join(result_lines)
        if clean:
            result_text = self._clean_ansi(result_text)

        self._set_exec_state(req_id, cmd, "done", exit_code=exit_code)
        self._write_result_file(req_id, cmd, result_text, exit_code=exit_code)

    # ─── QUERY: Fast lightweight query ───────────────────────────────────────

    def _exec_query(self, cmd, req_id, timeout=15):
        """Fast query: send command, wait for prompt to return, capture output from screen."""
        self._set_exec_state(req_id, cmd, "running")

        # Record pre-execution screen state
        with self.lock:
            pre_cursor_y = self.screen.cursor.y

        # Use a unique end marker to detect completion
        end_marker = f"__Q_{req_id}__"
        self.pty.write(f"{cmd}; echo {end_marker}\r")

        # Wait for marker to appear on screen
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(0.2)
            with self.lock:
                all_lines = self._collect_all_lines()
            full_text = "\n".join(all_lines)
            if end_marker in full_text:
                break
        else:
            self._set_exec_state(req_id, cmd, "done", exit_code=-1)
            self._write_result_file(req_id, cmd, "[TIMEOUT]", exit_code=-1)
            return

        # Extract output between the command echo and the marker
        with self.lock:
            all_lines = self._collect_all_lines()

        result_lines = []
        found_cmd = False
        for qline in all_lines:
            if end_marker in qline:
                break
            if found_cmd:
                result_lines.append(qline)
            elif cmd in qline or end_marker.split("__")[0] in qline:
                found_cmd = True

        # If we didn't find the command echo, just take lines before marker
        if not result_lines:
            capturing = False
            for qline in reversed(all_lines):
                if end_marker in qline:
                    capturing = True
                    continue
                if capturing:
                    if qline.strip() and (qline.strip().endswith("$") or qline.strip().endswith("#")):
                        break
                    result_lines.insert(0, qline)

        result_text = "\n".join(result_lines)
        self._set_exec_state(req_id, cmd, "done", exit_code=0)
        self._write_result_file(req_id, cmd, result_text, exit_code=0)

    # ─── BATCH: Multi-command sequential execution ───────────────────────────

    def _exec_batch(self, batch_id, cmds, timeout_per_cmd=120):
        """Execute multiple commands sequentially, write all results to result.txt."""
        self._set_exec_state(batch_id, f"BATCH({len(cmds)} cmds)", "running")

        total = len(cmds)
        all_results = []
        all_results.append(f"[batch_id: {batch_id}]")
        all_results.append(f"[total: {total}]")
        all_results.append("")

        for idx, cmd in enumerate(cmds, 1):
            sub_req_id = f"{batch_id}_{idx}"
            start = time.time()

            # Execute each command using signal file approach
            wrapped = (
                f"{{ {cmd}; }} > {EXEC_RESULT_FILE} 2>&1; "
                f"echo \"$?\" > {EXEC_SIGNAL_FILE}; "
                f"echo {sub_req_id} >> {EXEC_SIGNAL_FILE}"
            )
            self.pty.write(f"({wrapped})\r")

            # Wait for signal
            exit_code = None
            while time.time() - start < timeout_per_cmd:
                time.sleep(0.5)
                check_marker = f"__BC_{sub_req_id}__"
                self.pty.write(f"cat {EXEC_SIGNAL_FILE} 2>/dev/null && echo {check_marker}\r")
                time.sleep(0.5)

                with self.lock:
                    screen_text = "\n".join(self._collect_all_lines())

                if check_marker in screen_text and sub_req_id in screen_text:
                    for sline in screen_text.split("\n"):
                        sline = sline.strip()
                        if sline.isdigit():
                            try:
                                exit_code = int(sline)
                            except ValueError:
                                pass
                    break

            # Read result
            time.sleep(0.2)
            begin_m = f"__BB_{sub_req_id}__"
            end_m = f"__BE_{sub_req_id}__"

            with self.lock:
                if hasattr(self.screen, "history"):
                    self.screen.history.top.clear()

            self.pty.write(f"echo {begin_m}; cat {EXEC_RESULT_FILE}; echo {end_m}\r")

            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(0.3)
                with self.lock:
                    alines = self._collect_all_lines()
                if end_m in "\n".join(alines):
                    break

            with self.lock:
                alines = self._collect_all_lines()

            result_lines = []
            capturing = False
            for rline in alines:
                if begin_m in rline:
                    capturing = True
                    continue
                if capturing:
                    if end_m in rline:
                        break
                    result_lines.append(rline)

            duration = round(time.time() - start, 1)
            all_results.append(f"--- [{idx}/{total}] {cmd} ---")
            all_results.append(f"[exit_code: {exit_code}]")
            all_results.extend(result_lines)
            all_results.append("")

        all_results.append(f"[batch_status: done]")

        self._set_exec_state(batch_id, f"BATCH({total} cmds)", "done", exit_code=0)
        result_content = "\n".join(all_results)

        now = datetime.now().isoformat(timespec="seconds")
        content = (
            f"[batch_id: {batch_id}]\n"
            f"[timestamp: {now}]\n"
            f"{result_content}\n"
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
