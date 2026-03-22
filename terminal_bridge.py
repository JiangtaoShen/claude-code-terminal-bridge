"""
Terminal Bridge - 让 Claude Code 通过文件 IPC 操控终端 (SSH 会话等)

使用方法:
  1. 启动: py terminal_bridge.py
  2. 在窗口中手动操作 (如 SSH 登录)
  3. Claude Code 通过 command.txt 发送命令, 通过 output.txt 读取屏幕
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


# ─── Win32 API 辅助 ──────────────────────────────────────────────────────────

def _get_clipboard_text():
    """通过 Win32 API 读取剪贴板文本"""
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

# ─── 配置 ───────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".terminal-bridge")
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
DEFAULT_HISTORY = 1000
SCREEN_CAPTURE_INTERVAL = 0.5   # 秒
COMMAND_POLL_INTERVAL = 0.2     # 秒
PTY_READ_INTERVAL = 0.05        # 秒
DISPLAY_REFRESH_INTERVAL = 0.3  # 秒 - 控制台刷新间隔
EXEC_RESULT_FILE = "/tmp/bridge_result.txt"  # 远程服务器上的临时结果文件
EXEC_DONE_MARKER = "__BRIDGE_DONE_8f3a__"    # 命令完成标记


# ─── 特殊按键映射 ───────────────────────────────────────────────────────────

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

        # 自动检测 CMD 窗口实际尺寸
        win_cols, win_rows = self._detect_console_size()
        # 如果命令行指定了尺寸, 用指定的; 否则用窗口实际尺寸
        self.cols = min(cols, win_cols) if cols else win_cols
        self.rows = min(rows, win_rows - 1) if rows else (win_rows - 1)  # -1 给状态栏
        self.win_cols = win_cols
        self.win_rows = win_rows

        # 初始化 PTY (匹配检测到的尺寸)
        self.pty = PTY(self.cols, self.rows)
        self.pty.spawn(shell)

        # 初始化 pyte 虚拟终端
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=DEFAULT_HISTORY)
        self.screen.set_mode(pyte.modes.LNM)
        self.stream = pyte.ByteStream(self.screen)

        # 创建空的 command.txt
        with open(self.command_path, "w", encoding="utf-8") as f:
            pass

    @staticmethod
    def _detect_console_size():
        """检测 CMD 窗口实际可见尺寸"""
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except Exception:
            return DEFAULT_COLS, DEFAULT_ROWS

    def start(self):
        """启动所有后台线程, 主线程处理用户键盘输入"""
        # 启动诊断
        print(f"[Bridge] 检测到窗口: {self.win_cols}x{self.win_rows}, PTY: {self.cols}x{self.rows}")
        print(f"[Bridge] 3秒后启动...")
        time.sleep(3)

        # 启用 Windows 控制台虚拟终端序列
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

        # 主线程: 读取用户键盘输入并转发到 PTY
        self._keyboard_input_loop()

    def _enable_quick_edit(self):
        """启用 QuickEdit 模式, 让右键粘贴生效"""
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode))
        # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
        kernel32.SetConsoleMode(h_stdin, mode.value | 0x0040 | 0x0080)

    def _keyboard_input_loop(self):
        """主线程: 读取用户键盘输入, 转发到 PTY"""
        self._enable_quick_edit()
        try:
            while self.running and self.pty.isalive():
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        # 功能键/方向键前缀, 读取第二个字节
                        ch2 = msvcrt.getwch()
                        key = self._translate_windows_key(ch2)
                        if key:
                            self.pty.write(key)
                    elif ch == "\x16":
                        # Ctrl+V: 粘贴剪贴板内容
                        text = _get_clipboard_text()
                        if text:
                            # 将 \r\n 和 \n 统一替换为 \r
                            text = text.replace("\r\n", "\r").replace("\n", "\r")
                            self.pty.write(text)
                    else:
                        # 普通字符, 直接发送 (回车转为 \r)
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
            sys.stdout.write("[Bridge] 已退出\n")
            sys.stdout.flush()

    def _translate_windows_key(self, ch2):
        """将 Windows 功能键码转为 ANSI 转义序列"""
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
        """后台线程: 持续读取 PTY 输出, 喂给 pyte (不直接输出到控制台)"""
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
        """获取 stdout 句柄 (缓存)"""
        if not hasattr(self, "_h_stdout"):
            self._h_stdout = ctypes.windll.kernel32.GetStdHandle(-11)
        return self._h_stdout

    def _set_cursor_pos(self, x, y):
        """使用 Win32 API 直接设置控制台光标位置"""
        handle = self._get_stdout_handle()
        coord = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
        ctypes.windll.kernel32.SetConsoleCursorPosition(handle, coord)

    def _set_console_buffer_size(self, cols, rows):
        """设置控制台缓冲区大小, 使其刚好等于窗口大小, 消除滚动条"""
        handle = self._get_stdout_handle()
        coord = (cols & 0xFFFF) | ((rows & 0xFFFF) << 16)
        ctypes.windll.kernel32.SetConsoleScreenBufferSize(handle, coord)

    def _clear_console(self):
        """清屏"""
        os.system("cls")

    def _display_refresh_loop(self):
        """后台线程: 用 _set_cursor_pos 回到原点, ANSI \\x1b[K 清行尾, 避免滚屏"""
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

                    # 回到 (0,0)
                    self._set_cursor_pos(0, 0)

                    buf = []
                    # 状态栏 (反色 + 清除行尾)
                    buf.append(f"\x1b[7m{status}\x1b[0m\x1b[K")

                    # PTY 行: 只输出 rows 行, 每行末尾 \x1b[K 清除残留
                    for i in range(self.rows):
                        line = lines[i] if i < len(lines) else ""
                        text = line.rstrip()
                        if i == cursor_y:
                            # 光标位置反色标记
                            padded = line  # 保留空格以正确定位光标
                            chars = list(padded)
                            if cursor_x < len(chars):
                                chars[cursor_x] = "\x1b[7m" + chars[cursor_x] + "\x1b[0m"
                            text = "".join(chars).rstrip()
                        buf.append(text + "\x1b[K")

                    # 用 \n 连接, 但最后不加 \n (防止滚屏)
                    # 再加 \x1b[J 清除下方所有残留
                    sys.stdout.write("\n".join(buf) + "\x1b[J")
                    sys.stdout.flush()
            except Exception:
                pass
            time.sleep(DISPLAY_REFRESH_INTERVAL)

    def _screen_capture_loop(self):
        """后台线程: 定期将 pyte 屏幕内容写入 output.txt"""
        while self.running:
            try:
                self._render_output()
            except Exception:
                pass
            time.sleep(SCREEN_CAPTURE_INTERVAL)

        # 最终写入一次
        try:
            self._render_output()
        except Exception:
            pass

    def _render_output(self):
        """将 pyte 当前可见屏幕写入 output.txt (不含滚动历史)"""
        now = datetime.now().isoformat(timespec="seconds")
        alive = self.pty.isalive()

        with self.lock:
            display_lines = [row.rstrip() for row in self.screen.display]

        # 只保留当前屏幕 (历史已在 Claude Code 上下文中)
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
        """后台线程: 监听 command.txt, 读取命令并发送到 PTY"""
        while self.running and self.pty.isalive():
            try:
                if os.path.exists(self.command_path):
                    with open(self.command_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    if content.strip():
                        self._process_command(content)
                        # 清空 command.txt
                        with open(self.command_path, "w", encoding="utf-8") as f:
                            pass
                        self.last_command_processed = datetime.now().isoformat(timespec="seconds")
                        self._write_status()
            except Exception:
                pass
            time.sleep(COMMAND_POLL_INTERVAL)

    def _process_command(self, content):
        """解析并执行命令

        支持的前缀:
          (无前缀)  - 普通命令, 发送文本 + 回车
          EXEC:     - 执行命令并将完整输出保存到 result.txt (无行数限制)
          RAW:      - 发送原始字节 (支持 \\x03 等转义)
          KEY:      - 发送特殊按键 (ENTER, CTRL+C, UP 等)
        """
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("EXEC:"):
                # 完整输出捕获: 重定向到文件, 完成后写标记
                cmd = line[5:].strip()
                wrapped = f"{cmd} > {EXEC_RESULT_FILE} 2>&1; echo {EXEC_DONE_MARKER}"
                self.pty.write(wrapped + "\r")
                # 启动后台线程等待完成并写入 result.txt
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
                # 普通命令, 发送文本 + 回车
                self.pty.write(line + "\r")

    def _wait_exec_result(self, original_cmd, timeout=60):
        """等待 EXEC 命令完成, 然后通过 cat 读取结果文件并写入本地 result.txt"""
        result_path = os.path.join(os.path.dirname(self.output_path), "result.txt")
        start = time.time()

        # 等待 DONE 标记出现在屏幕上
        while time.time() - start < timeout:
            with self.lock:
                screen_text = "\n".join(self.screen.display)
            if EXEC_DONE_MARKER in screen_text:
                break
            time.sleep(0.3)
        else:
            # 超时
            with open(result_path, "w", encoding="utf-8") as f:
                f.write(f"[TIMEOUT after {timeout}s] cmd: {original_cmd}\n")
            return

        # 用 cat 读取远程结果文件
        time.sleep(0.3)
        self.pty.write(f"cat {EXEC_RESULT_FILE}\r")
        time.sleep(1)  # 等待 cat 输出

        # 从 pyte 历史 + 屏幕中提取 cat 的输出
        with self.lock:
            all_lines = []
            if hasattr(self.screen, "history") and self.screen.history.top:
                for hline in self.screen.history.top:
                    row_text = ""
                    for col in range(self.cols):
                        row_text += hline[col].data if col in hline else " "
                    all_lines.append(row_text.rstrip())
            all_lines.extend([r.rstrip() for r in self.screen.display])

        # 找到 cat 命令之后、下一个 prompt 之前的内容
        result_lines = []
        capturing = False
        cat_cmd = f"cat {EXEC_RESULT_FILE}"
        for line in all_lines:
            if cat_cmd in line:
                capturing = True
                continue
            if capturing:
                # 检测到 prompt ($ 结尾) 就停止
                if line.rstrip().endswith("$") and not line.startswith(" "):
                    break
                result_lines.append(line)

        # 写入本地 result.txt
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(f"[cmd: {original_cmd}]\n")
            f.write(f"[timestamp: {datetime.now().isoformat(timespec='seconds')}]\n")
            f.write("\n".join(result_lines) + "\n")

    def _write_status(self):
        """写入 status.json"""
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

    # 确保工作目录存在
    bridge_dir = os.path.abspath(args.dir)
    os.makedirs(bridge_dir, exist_ok=True)

    # 解析 IPC 文件路径 (默认放在 --dir 下)
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
