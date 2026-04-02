# Terminal Bridge 改进方案

> 基于 2026-04-03 实际 SSH 会话中遇到的问题，整理出以下改进建议。
> 当天任务：通过 bridge 监控远程 GPU 服务器上的多个训练进程，执行 `ps`、`nvidia-smi`、`ls`、`kill`、`tail`、`grep` 等命令。

---

## 1. EXEC 请求-响应缺乏关联（最高优先级）

### 问题

当前 EXEC 流程：Claude 写 command.txt -> bridge 异步执行 -> 结果写入 result.txt。但 result.txt 没有标识属于哪次请求。Claude 读 result.txt 时经常读到**上一次** EXEC 的结果，因为：

- EXEC 是异步的，Claude 只能靠 `sleep N` 猜测完成时间
- result.txt 没有请求 ID、没有版本号、没有时间戳与命令的对应关系
- 如果 sleep 不够长，读到旧结果；sleep 太长，浪费时间

实际表现：本次会话中，反复出现"result.txt 显示上次命令的输出"的情况，不得不多次重试和 sleep。

### 方案

为每次 EXEC 引入 **request ID** 机制：

```
# command.txt 写入格式
EXEC:req_001:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader

# result.txt 输出格式
[req_id: req_001]
[status: done]
[exit_code: 0]
[timestamp: 2026-04-03T02:05:11]
[duration: 0.3s]
0, 80 %, 5276 MiB
1, 0 %, 22 MiB
...
```

同时新增 **completion 信号文件**（或在 status.json 中追加字段）：

```json
{
  "last_exec": {
    "req_id": "req_001",
    "status": "done",
    "exit_code": 0,
    "timestamp": "2026-04-03T02:05:11",
    "duration_s": 0.3
  }
}
```

Claude 的使用流程变为：
1. 生成唯一 req_id，写 command.txt
2. 轮询 status.json 中 `last_exec.req_id == "req_001"` 且 `status == "done"`
3. 确认匹配后读 result.txt

这消除了所有时序猜测。

---

## 2. 进度条 / 大量 ANSI 输出污染终端

### 问题

远程有 tqdm 进度条不断刷新（如 `kan/pde5: 37%|████...`），导致：

- **output.txt 完全被进度条淹没**：整个可见屏幕都是进度条的重复行，看不到命令结果
- **EXEC 的 done marker 被冲走**：进度条快速刷新可能将 `__BRIDGE_DONE_8f3a__` 推出屏幕，导致 bridge 误判超时
- **result.txt 也受干扰**：`echo __BRIDGE_BEGIN_RESULT__; cat /tmp/bridge_result.txt; echo __BRIDGE_END_RESULT__` 的输出中混入了进度条内容

### 方案

**方案 A：EXEC 使用独立通道**

不再让 EXEC 命令与正常终端流混合。改用后台子 shell 执行 + 直接写文件：

```bash
# bridge 实际发送到 PTY 的内容：
bash -c 'CMD_OUTPUT=$(nvidia-smi 2>&1); echo "$CMD_OUTPUT" > /tmp/bridge_result.txt; echo REQ_001_DONE > /tmp/bridge_signal.txt' &
```

bridge 端只需轮询远程信号文件（通过一个单独的轻量级 polling 命令），不依赖屏幕上的 marker。

**方案 B：静默执行模式**

EXEC 命令执行前后临时抑制 PTY 输出到屏幕的捕获：

```python
def _exec_silent(self, cmd, req_id):
    """在后台 shell 中执行命令，完全不经过可见终端流。"""
    # 将命令包装为不产生终端输出的后台任务
    wrapped = (
        f"{{ {cmd}; }} > {EXEC_RESULT_FILE} 2>&1; "
        f"echo '{req_id}' > /tmp/bridge_signal.txt"
    )
    # 用 \r 发送但不期望屏幕上出现结果
    self.pty.write(f"({wrapped}) &\r")
```

---

## 3. output.txt 只有当前屏幕，信息量不足

### 问题

output.txt 捕获 pyte 的 visible display（40行），无法回滚。当进度条刷屏后，之前执行的命令结果已不可见。Claude 只能看到一屏进度条。

即使不用 EXEC，只想看看 `ls` 的结果，如果进度条在跑，结果会在 1 秒内被推出屏幕。

### 方案

新增 **scrollback buffer 导出**：

```
# command.txt
SCROLLBACK:200
```

将 pyte history 中最近 200 行写入 result.txt。这样即使屏幕被刷新，Claude 仍可追溯历史。

实现上 pyte.HistoryScreen 已经维护了 history.top，只需导出：

```python
def _write_scrollback(self, n):
    with self.lock:
        history_lines = []
        if hasattr(self.screen, "history") and self.screen.history.top:
            for hline in self.screen.history.top:
                row = "".join(hline[c].data if c in hline else " " for c in range(self.cols))
                history_lines.append(row.rstrip())
        display_lines = [r.rstrip() for r in self.screen.display]
        all_lines = history_lines + display_lines
    # 取最后 n 行
    output = all_lines[-n:] if len(all_lines) > n else all_lines
    # 写入 result.txt
```

---

## 4. 缺少命令执行状态反馈

### 问题

Claude 发送 EXEC 后，无法得知：
- 命令是否还在执行中
- 是否已完成
- 退出码是什么
- 执行了多长时间

只能盲目 sleep 然后读 result.txt，如果读到旧数据还不确定是 sleep 不够还是命令失败。

### 方案

扩展 status.json 加入执行队列状态：

```json
{
  "pty_alive": true,
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

Claude 只需轮询 status.json（小文件，低开销），确认 `exec_queue.current.req_id` 匹配且 `status == "done"` 后再读 result.txt。

---

## 5. 多命令串行执行支持

### 问题

当前每次只能发一条 EXEC 命令。如果 Claude 想一次性查询多项信息（GPU 状态 + 进程列表 + 文件列表），必须：
1. 把多条命令用 `&&` 拼成一条巨大的命令
2. 或者串行发送 3 次 EXEC，每次 sleep 等待

拼接的大命令难以阅读和调试，且中间任何一步失败会导致后续命令不执行。

### 方案

支持 **批量执行**，command.txt 可以包含多条 EXEC，按序执行，结果分段写入 result.txt：

```
BATCH:batch_001
EXEC:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader
EXEC:ps aux | grep "python.*train" | grep -v grep
EXEC:ls -lt ~/output/*pde5* 2>/dev/null
END_BATCH
```

result.txt 输出：

```
[batch_id: batch_001]
[total: 3]

--- [1/3] nvidia-smi ... ---
[exit_code: 0]
0, 80%, 5276 MiB
1, 0%, 22 MiB
...

--- [2/3] ps aux ... ---
[exit_code: 0]
131318 ... kan pde5 adam ...

--- [3/3] ls -lt ... ---
[exit_code: 0]
-rw-rw-r-- ... fls_pde5_seed42.pkl
...

[batch_status: done]
```

---

## 6. grep 处理二进制内容失败

### 问题

远程 log 文件中混有 tqdm 的 ANSI 控制字符（`\r`, `\x1b[...`），导致：
- `grep` 返回 `Binary file (standard input) matches`
- `cat file | strings | grep ...` 会打乱行顺序

这不是 bridge 本身的 bug，但 bridge 可以提供辅助。

### 方案

新增 **EXEC 选项**，自动对远程命令的输出做清洗：

```
EXEC:CLEAN:cat ~/log.log | grep "Finished"
```

bridge 在写入 result.txt 前自动清除 ANSI 转义序列：

```python
import re
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\r')

def _clean_output(self, text):
    return ANSI_ESCAPE.sub('', text)
```

或者，bridge 在构造远程命令时自动添加管道清洗：

```bash
# 原始命令
cat ~/log.log | grep "Finished"
# bridge 自动改写为
cat ~/log.log | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' | tr -d '\r' | grep "Finished"
```

---

## 7. result.txt 的原子性与大文件问题

### 问题

当前 EXEC 结果通过 `cat /tmp/bridge_result.txt` 回显到终端，再由 bridge 从 pyte history 中提取 BEGIN/END marker 之间的内容。这个链路有多个脆弱点：

- pyte history 有上限（默认 1000 行），长输出被截断
- 进度条输出会插入到 marker 之间
- result.txt 文件写入不是原子的，Claude 可能读到写了一半的文件

### 方案

**使用 `os.replace()` 原子写入**（当前 output.txt 已经这样做了，但 result.txt 没有）：

```python
def _write_result(self, content):
    tmp = self.result_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, self.result_path)
```

**大文件场景**：对于超过 pyte history 容量的输出，不走终端回显通道。改为 bridge 直接通过一条辅助命令获取文件大小，如果超过阈值则分 chunk 读取：

```python
# 先检查文件大小
self.pty.write(f"wc -c < {EXEC_RESULT_FILE}\r")
# 根据大小决定：< 40KB 走 cat 回显，>= 40KB 走 chunk 传输
```

---

## 8. 新增 QUERY 命令类型（轻量级快速查询）

### 问题

很多时候 Claude 只想执行一条简短命令并拿到输出（如 `nvidia-smi`, `ps aux | wc -l`）。当前流程：

1. 写 `EXEC:cmd` 到 command.txt
2. bridge 发送 `cmd > /tmp/bridge_result.txt 2>&1; echo __BRIDGE_DONE_8f3a__`
3. 等待 done marker 出现在屏幕上
4. bridge 发送 `echo __BRIDGE_BEGIN_RESULT__; cat /tmp/bridge_result.txt; echo __BRIDGE_END_RESULT__`
5. 等待 end marker 出现
6. 从 history 提取结果

这个流程经过了**两轮**命令注入 + 屏幕等待，延迟高且容易被进度条干扰。

### 方案

新增 `QUERY:` 前缀，用于预期输出 < 20 行的快速命令：

```
QUERY:req_005:nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader
```

实现：bridge 发送命令后直接从 pyte display 变化中捕获输出（检测 prompt 重新出现），无需文件中转。适用于短小输出的高频查询。

```python
def _exec_query(self, cmd, req_id):
    """快速查询：发送命令，等待 prompt 回归，捕获中间输出。"""
    # 记录当前 screen 内容
    with self.lock:
        pre_lines = list(self.screen.display)
        pre_cursor_y = self.screen.cursor.y

    self.pty.write(cmd + "\r")

    # 等待 prompt 重新出现（cursor 回到 prompt 行 + 有 $ 或 # ）
    # ...提取 pre_cursor_y+1 到 new_cursor_y-1 之间的行作为输出
```

---

## 实现优先级建议

| 优先级 | 改进项 | 预计工作量 | 收益 |
|--------|--------|-----------|------|
| P0 | 1. Request ID 关联 | 小 | 消除最大痛点：result.txt 新旧混淆 |
| P0 | 4. status.json 执行状态 | 小 | 配合 req_id，Claude 可确定性等待 |
| P1 | 2. EXEC 独立通道 | 中 | 彻底解决进度条干扰问题 |
| P1 | 7. result.txt 原子写入 | 小 | 防止读到半写文件 |
| P2 | 3. Scrollback 导出 | 小 | 弥补 output.txt 只有一屏的局限 |
| P2 | 5. 批量执行 | 中 | 减少往返次数 |
| P2 | 6. ANSI 清洗 | 小 | 便利功能 |
| P3 | 8. QUERY 快速查询 | 中 | 优化高频短命令场景 |
