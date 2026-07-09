"""Coder Agent — 写代码、改代码"""
from threading import Lock
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_agent_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState, FileChange
from code_agent.project_context import get_project_context, format_project_context
from code_agent.context_manager import get_context_manager, AgentSummary

CODER_PROMPT = """你是 Code Writer，负责编写和修改代码。

## 操作规范
1. 修改前务必用 read_file 读取文件最新内容
2. edit_file 的 old_string 必须与文件中实际内容一字不差（缩进、空行、标点）
3. 如果 old_string 匹配多处，加更多上下文使其唯一
4. 创建新文件用 write_file，修改现有文件用 edit_file

## 代码标准
- 不引入安全漏洞：无 SQL 注入、命令注入、路径穿越、XSS
- 不写废话注释，代码即文档
- 保持与项目现有风格完全一致（缩进、命名、引号、import 顺序）
- 改动最小化：只改需要的，不顺手重构无关代码

## Reviewer 反馈
收到 Review 反馈时，逐条对照修改，确认修完后在末尾写 [编码完成]。
"""

_agent = None
_lock = Lock()


def _get_agent():
    global _agent
    if _agent is None:
        with _lock:
            if _agent is None:
                _agent = create_react_agent(
                    model=get_agent_model("coder"),
                    tools=AGENT_TOOLS["coder"],
                    prompt=CODER_PROMPT,
                )
    return _agent


def coder_node(state: CodingState) -> dict:
    task = state.get("user_request", "")
    workspace = state.get("workspace_dir", ".")
    exploration = state.get("exploration_result", "")
    relevant_files = state.get("relevant_files", [])
    review_feedback = state.get("review_feedback", "")

    ctx = get_project_context(workspace)
    proj_info = format_project_context(ctx)

    prompt_parts = [
        f"## 项目信息\n{proj_info}",
        f"\n## 用户需求\n{task}",
    ]

    if exploration:
        prompt_parts.append(f"\n## Explorer 分析\n{exploration}")
    if relevant_files:
        prompt_parts.append(f"\n## 相关文件\n" + "\n".join(f"- {f}" for f in relevant_files))
    if review_feedback:
        prompt_parts.append(f"\n## Reviewer 修改要求\n{review_feedback}")
        prompt_parts.append("请逐条对照修改要求进行调整。")

    prompt = "\n".join(prompt_parts)

    ctx_mgr = get_context_manager()
    existing = list(state.get("messages", []))
    context_messages = ctx_mgr.build_context(
        "coder", existing, prompt,
        summaries=state.get("agent_summaries"),
    )
    result = _get_agent().invoke({"messages": context_messages})
    last_msg = result["messages"][-1].content

    code_changes = _extract_changes(result["messages"])

    ctx_mgr.record_summary(AgentSummary(
        agent="coder",
        summary=last_msg[:200],
        key_findings=[],
        files_touched=[c["file_path"] for c in code_changes if c.get("file_path")],
    ))

    return {
        "code_changes": code_changes,
        "messages": result["messages"],
    }


def _extract_changes(messages: list) -> list[FileChange]:
    """从 Coder 的 tool 消息中提取代码改动记录"""
    changes = []
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else ""
        if not isinstance(content, str):
            continue

        if not (content.startswith("[DIFF:") or content.startswith("[已写入]") or content.startswith("[已修改]")):
            continue

        name = getattr(msg, "name", "")
        file_path = ""
        reason = ""

        if name == "write_file":
            # 从 "[已写入] path (N 行, M 字符)" 中提取
            for line in content.split("\n"):
                if "[已写入]" in line:
                    parts = line.split("[已写入]")[-1].strip().split("(")[0].strip()
                    file_path = parts
            reason = "创建新文件"

        elif name == "edit_file":
            for line in content.split("\n"):
                if "[已修改]" in line:
                    parts = line.split("[已修改]")[-1].strip().split("(")[0].strip()
                    file_path = parts
            reason = "修改现有文件"

        if file_path:
            changes.append(FileChange(
                file_path=file_path,
                original="",
                replacement="",
                reason=reason,
            ))

    return changes
