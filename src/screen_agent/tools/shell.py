"""受限命令执行。

对标 OpenAI Codex 的 `exec_command`（Apache-2.0，源码
`codex-rs/core/src/tools/handlers/shell_spec.rs`）：参数集、风险分级思路与
Windows 安全规则照搬，代码是按本项目风格用 Python 重写的，没抄 Rust 实现。

三条 Windows 安全规则直接来自它的工具说明，都是踩过坑的结论：

1. **别跨 shell 组合破坏性文件操作**——在 PowerShell 里枚举路径再交给 `cmd /c` 去删，
   路径一旦被重新解释就会删错地方
2. **递归删除或移动之前，必须验证解析后的绝对路径仍在目标范围内**
3. **起后台进程时窗口要隐藏**，除非用户明确要看

风险分级：

- `deny` 直接拒：格式化磁盘、删系统目录、改注册表、关机、fork 炸弹
- `safe` 只读放行：目录列举、文件读取、状态查询
- `ask`  其余一律要确认：删除、移动、装依赖、起进程、重定向写文件

`git` 单独做了细分——`git status` 放行，`git push` / `reset --hard` / `clean -fd`
这类会丢改动的子命令必须问。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from screen_agent.tools.base import ActionResult

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 300
DEFAULT_MAX_OUTPUT = 8000

# ---- 硬拒：没有商量余地 ----
_DENY: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bformat\s+[a-z]:", re.I), "格式化磁盘"),
    (re.compile(r"\bmkfs\b", re.I), "格式化磁盘"),
    (re.compile(r"\bdiskpart\b", re.I), "动分区表"),
    (re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+/(\s|$|\*)", re.I), "删根目录"),
    (re.compile(r"\b(del|erase)\s+/[fsq]\b[^\n]{0,24}[a-z]:\\(\s|$|\*)", re.I), "强删盘根"),
    (re.compile(r"\brd\s+/s\s+/q\s+[a-z]:\\?\s*$", re.I), "强删盘根"),
    (re.compile(r"\bshutdown\b", re.I), "关机或重启"),
    (re.compile(r"\breg\s+delete\b", re.I), "删注册表项"),
    (re.compile(r"\btakeown\b", re.I), "夺取文件所有权"),
    (re.compile(r"\bnet\s+user\b[^\n]*\s/add\b", re.I), "新建系统账户"),
    (re.compile(r":\(\)\s*\{.*\};\s*:", re.S), "把机器拖死"),
]

# ---- 要确认：可能造成不可逆后果 ----
_ASK: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--|branch\s+-D)\b", re.I),
     "覆盖远端或丢弃本地改动"),
    (re.compile(r"\b(rm|del|erase|rmdir|rd)\b", re.I), "删东西"),
    (re.compile(r"\b(Remove-Item|Move-Item|Copy-Item|move|mv|robocopy|xcopy)\b", re.I),
     "移动或覆盖文件"),
    (re.compile(r"\b(chmod|chown|attrib|icacls|cacls|Set-Acl)\b", re.I), "改文件权限"),
    (re.compile(r"\b(taskkill|Stop-Process|kill)\b", re.I), "结束进程"),
    (re.compile(r"\|\s*(bash|sh|zsh|iex|Invoke-Expression)\b", re.I), "把下载的内容直接跑起来"),
    (re.compile(r"\b(pip|pip3|npm|yarn|pnpm|winget|choco|scoop)\s+(install|add|upgrade)\b", re.I),
     "装东西"),
    (re.compile(r"\b(Start-Process|schtasks|reg\s+add)\b", re.I), "起进程、改计划任务或注册表"),
    (re.compile(r"(^|\s)>{1,2}\s*\S", re.I), "把输出重定向进文件"),
]

# ---- 只读命令：直接放行 ----
_SAFE_HEADS = {
    "dir", "ls", "type", "cat", "echo", "where", "which", "findstr", "find",
    "pwd", "cd", "tree", "head", "tail", "wc", "sort", "more",
    "git", "pytest", "whoami", "hostname", "date", "ver", "path",
    "tasklist", "netstat", "ipconfig", "ping", "tracert", "nslookup", "systeminfo",
}


def classify_command(cmd: str) -> tuple[str, str]:
    """给命令定级：deny / safe / ask，附一句给人看的理由。"""
    text = (cmd or "").strip()
    if not text:
        return "deny", "命令是空的"
    for pattern, why in _DENY:
        if pattern.search(text):
            return "deny", f"这条命令会{why}，我不能执行"
    for pattern, why in _ASK:
        if pattern.search(text):
            return "ask", f"这条命令可能{why}，要你点头"
    head = text.split()[0].lower().strip("\"'")
    if head in _SAFE_HEADS:
        return "safe", ""
    return "ask", "这条命令我不确定是不是只读，先问一声"


def _decode(raw: bytes) -> str:
    """cmd 在中文 Windows 上吐 GBK，PowerShell 吐 UTF-8，都要认。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_command(
    cmd: str,
    workdir: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
) -> ActionResult:
    """在受限工作目录里执行一条命令。

    确认环节不在这里——`deny` 直接拒，`ask` 交给 PolicyEngine 去要用户点头，
    这样"谁有权放行"只有一个地方说了算。
    """
    from screen_agent.tools.files import _guard_write, _user_dir  # 局部导入避免循环依赖

    level, why = classify_command(cmd)
    if level == "deny":
        return ActionResult(success=False, message=why, detail={"level": level})

    cwd = Path(workdir).expanduser() if workdir else _user_dir("Desktop")
    if not cwd.is_dir():
        return ActionResult(success=False, message=f"工作目录不存在：{cwd}")
    # 命令一律在白名单目录里跑，不在系统目录里乱来
    guard = _guard_write(cwd)
    if guard:
        return ActionResult(success=False, message=guard)

    try:
        limit = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT
    try:
        budget = max(200, int(max_output or DEFAULT_MAX_OUTPUT))
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_OUTPUT

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(
            success=False,
            message=f"这条命令跑了超过 {limit} 秒还没完，我把它停了",
            detail={"level": level, "timeout": True},
        )
    except OSError as exc:
        return ActionResult(success=False, message=f"没法执行这条命令：{exc}")

    output = (_decode(proc.stdout or b"") + _decode(proc.stderr or b"")).strip()
    total = len(output)
    if total > budget:
        output = output[:budget] + f"\n…输出还有 {total - budget} 字没显示（太长就不全灌了）"
    if not output:
        output = "（没有任何输出）"

    code = proc.returncode
    head = "执行完成" if code == 0 else f"退出码 {code}"
    return ActionResult(
        success=code == 0,
        message=f"{head}：{cmd}\n{output}",
        detail={"exit_code": code, "chars": total, "cwd": str(cwd), "level": level},
    )
