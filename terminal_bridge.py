"""
Terminal Bridge - Let Claude Code control terminal sessions (SSH, etc.) via file-based IPC.

Usage:
  1. Start: py terminal_bridge.py
  2. Manually operate in the window (e.g. SSH login)
  3. Claude Code sends commands via command.txt and reads screen via output.txt
"""

import os
import sys
import json
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
EXEC_DONE_MARKER = "__BRIDGE_DONE_8f3a__"    # command completion marker


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
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.last_output_update = ""
        self.last_command_processed = ""
        self.running = True
        self.lock = threading.Lock()

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
        """Write current pyte visible screen to output.txt (no scroll history)."""
        now = datetime.now().isoformat(timespec="seconds")
        alive = self.pty.isalive()

        with self.lock:
            display_lines = [row.rstrip() for row in self.screen.display]

        # Only keep current screen (history is already in Claude Code context)
        parts = [f"[alive: {str(alive).lower()}] [timestamp: {now}]"]
        parts.extend(display_lines)

        content = "\n".join(parts) + "\n"

        tmp_path = self.output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, self.output_path)

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
          (none)    - Plain command, send text + Enter
          EXEC:     - Execute and capture full output to result.txt (no line limit)
          RAW:      - Send raw bytes (supports \\x03 etc.)
          KEY:      - Send special key (ENTER, CTRL+C, UP, etc.)
        """
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("EXEC:"):
                # Full output capture: redirect to file, write done marker
                cmd = line[5:].strip()
                wrapped = f"{cmd} > {EXEC_RESULT_FILE} 2>&1; echo {EXEC_DONE_MARKER}"
                self.pty.write(wrapped + "\r")
                # Start background thread to wait for completion and write result.txt
                threading.Thread(
                    target=self._wait_exec_result,
                    args=(cmd,),
                    daemon=True,
                ).start()
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

    def _wait_exec_result(self, original_cmd, timeout=60):
        """Wait for EXEC command to complete, then read result file and write to local result.txt."""
        result_path = os.path.join(os.path.dirname(self.output_path), "result.txt")
        start = time.time()

        # Wait for DONE marker to appear on screen
        while time.time() - start < timeout:
            with self.lock:
                screen_text = "\n".join(self.screen.display)
            if EXEC_DONE_MARKER in screen_text:
                break
            time.sleep(0.3)
        else:
            # Timeout
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(f"[TIMEOUT after {timeout}s] cmd: {original_cmd}\n")
            return

        # Read remote result file via cat
        time.sleep(0.3)
        self.pty.write(f"cat {EXEC_RESULT_FILE}\r")
        time.sleep(1)  # Wait for cat output

        # Extract cat output from pyte history + screen
        with self.lock:
            all_lines = []
            if hasattr(self.screen, "history") and self.screen.history.top:
                for hline in self.screen.history.top:
                    row_text = ""
                    for col in range(self.cols):
                        row_text += hline[col].data if col in hline else " "
                    all_lines.append(row_text.rstrip())
            all_lines.extend([r.rstrip() for r in self.screen.display])

        # Find content between cat command and next prompt
        result_lines = []
        capturing = False
        cat_cmd = f"cat {EXEC_RESULT_FILE}"
        for line in all_lines:
            if cat_cmd in line:
                capturing = True
                continue
            if capturing:
                # Stop at prompt (line ending with $)
                if line.rstrip().endswith("$") and not line.startswith(" "):
                    break
                result_lines.append(line)

        # Write to local result.txt
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(f"[cmd: {original_cmd}]\n")
            f.write(f"[timestamp: {datetime.now().isoformat(timespec='seconds')}]\n")
            f.write("\n".join(result_lines) + "\n")

    def _write_status(self):
        """Write status.json with current bridge state."""
        try:
            status = {
                "pid": self.pty.pid if self.pty.pid else None,
                "pty_alive": self.pty.isalive(),
                "shell": self.shell,
                "cols": self.cols,
                "rows": self.rows,
                "started_at": self.started_at,
                "last_output_update": self.last_output_update,
                "last_command_processed": self.last_command_processed,
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
