"""Reviewer Agent — 审查代码改动，检查质量"""
from threading import Lock
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_agent_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState
from code_agent.project_context import get_project_context, format_project_context
from code_agent.context_manager import get_context_manager, AgentSummary

REVIEWER_PROMPT = """你是 Code Reviewer，负责把关代码质量。

## 审查清单（逐条检查）
- 逻辑正确性：改动是否准确实现了需求，有无遗漏或曲解
- 安全性：有无注入风险、路径穿越、密钥泄露、权限绕过
- 边界处理：空输入、None、大文件、并发场景是否安全
- 代码风格：缩进、命名、引号、import 顺序是否与项目一致
- 副作用：改动是否影响其他模块、是否破坏公共接口

## 输出格式
- 通过：回复以 **审查通过** 开头，可选附小建议
- 不通过：回复以 **审查不通过** 开头，逐条列出问题 + 修复建议

示例：
```
**审查通过**
改动逻辑正确，无安全风险，与现有风格一致。
小建议：第 23 行变量名可更语义化（非阻塞项）。
```
"""

_agent = None
_lock = Lock()


def _get_agent():
    global _agent
    if _agent is None:
        with _lock:
            if _agent is None:
                _agent = create_react_agent(
                    model=get_agent_model("reviewer"),
                    tools=AGENT_TOOLS["reviewer"],
                    prompt=REVIEWER_PROMPT,
                )
    return _agent


def reviewer_node(state: CodingState) -> dict:
    task = state.get("user_request", "")
    workspace = state.get("workspace_dir", ".")
    exploration = state.get("exploration_result", "")
    relevant_files = state.get("relevant_files", [])
    code_changes = state.get("code_changes") or []

    ctx = get_project_context(workspace)
    proj_info = format_project_context(ctx)

    prompt_parts = [
        f"## 项目信息\n{proj_info}",
        f"\n## 用户原需求\n{task}",
        f"\n## Explorer 分析\n{exploration}",
    ]

    # 传递 Coder 的实际改动记录给 Reviewer
    if code_changes:
        prompt_parts.append(f"\n## Coder 改动记录 ({len(code_changes)} 处改动)")
        for i, change in enumerate(code_changes, 1):
            prompt_parts.append(f"{i}. [{change.get('reason', '修改')}] {change.get('file_path', '?')}")
        prompt_parts.append("\n请 read_file 读取上述文件的最新内容进行审查。")

    if relevant_files:
        prompt_parts.append(f"\n## Explorer 标记的相关文件\n" + "\n".join(f"- {f}" for f in relevant_files))

    prompt = "\n".join(prompt_parts)

    ctx_mgr = get_context_manager()
    existing = list(state.get("messages", []))
    context_messages = ctx_mgr.build_context(
        "reviewer", existing, prompt,
        summaries=state.get("agent_summaries"),
    )
    result = _get_agent().invoke({"messages": context_messages})
    last_msg = result["messages"][-1].content

    approved = "审查通过" in last_msg

    ctx_mgr.record_summary(AgentSummary(
        agent="reviewer",
        summary="审查通过" if approved else "审查不通过，需修改",
        key_findings=[last_msg[:200]],
        files_touched=relevant_files if relevant_files else [],
    ))

    return {
        "review_feedback": last_msg,
        "review_approved": approved,
        "messages": result["messages"],
    }
