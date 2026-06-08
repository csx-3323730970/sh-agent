"""Shell 工具 — Bash 执行"""
import subprocess
from langchain_core.tools import tool
from code_agent.config import get_setting


def _check_safety(command: str) -> tuple[bool, str]:
    """安全检查：返回 (是否安全, 原因)"""
    blocked = get_setting("safety", "bash_blocked_keywords")
    for keyword in blocked:
        if keyword in command:
            return False, f"命中禁止关键词: {keyword}"

    allowed = get_setting("safety", "bash_allowed_prefixes")
    # 空命令拒绝
    if not command.strip():
        return False, "空命令"

    for prefix in allowed:
        if command.startswith(prefix):
            return True, ""
    return False, f"命令前缀不在白名单中: {command[:50]}"


@tool(description="执行 Shell 命令并返回输出。入参: command(要执行的命令)。仅在 Coder/Executor Agent 中可用。")
def bash(command: str, workspace_dir: str = ".", timeout: int = 30) -> str:
    safe, reason = _check_safety(command)
    if not safe:
        return f"[安全拦截] {reason}"

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=workspace_dir, timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = result.stdout.strip()
        err = result.stderr.strip()

        parts = []
        if out:
            parts.append(out[:2000])
        if err:
            parts.append(f"[stderr]\n{err[:1000]}")
        if result.returncode != 0:
            parts.append(f"[退出码: {result.returncode}]")
        return "\n".join(parts) if parts else f"[执行完毕，无输出] 退出码: {result.returncode}"
    except subprocess.TimeoutExpired:
        return f"[超时] 命令执行超过 {timeout}s，已终止"
    except Exception as e:
        return f"[执行失败] {str(e)}"
