"""Executor Agent — 运行测试、验证结果"""
from threading import Lock
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_agent_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState
from code_agent.project_context import get_project_context, format_project_context
from code_agent.context_manager import get_context_manager, AgentSummary

EXECUTOR_PROMPT = """你是 Code Executor，负责验证代码改动的正确性。

## 执行策略（按优先级）
1. 项目有 pytest → `pytest <test_dir>/ -v --tb=short`
2. Python 项目无 pytest → `python -m py_compile <changed_file>` 检查语法
3. 有 Makefile/package.json → 运行其中定义的 test 命令
4. 其他 → 运行对应语言的语法/编译检查

## 输出要求
- 清晰说明：运行了什么命令、为什么选这个命令
- 成功：显示通过数量、覆盖率（如有）
- 失败：逐条列出失败用例 + 错误信息摘要
- 以 [执行完成] 结尾
"""

_agent = None
_lock = Lock()


def _get_agent():
    global _agent
    if _agent is None:
        with _lock:
            if _agent is None:
                _agent = create_react_agent(
                    model=get_agent_model("executor"),
                    tools=AGENT_TOOLS["executor"],
                    prompt=EXECUTOR_PROMPT,
                )
    return _agent


def executor_node(state: CodingState) -> dict:
    workspace = state.get("workspace_dir", ".")
    relevant_files = state.get("relevant_files", [])
    review_feedback = state.get("review_feedback", "")
    review_approved = state.get("review_approved", False)

    ctx = get_project_context(workspace)
    proj_info = format_project_context(ctx)

    prompt_parts = [
        f"## 项目信息\n{proj_info}",
    ]

    if not review_approved:
        prompt_parts.append("\n⚠️ Reviewer 未通过审查，但仍需验证当前代码状态。")

    prompt_parts.append("\n请用合适的命令验证代码正确性。")

    if relevant_files:
        prompt_parts.append(f"\n涉及文件:\n" + "\n".join(f"- {f}" for f in relevant_files))

    prompt = "\n".join(prompt_parts)

    ctx_mgr = get_context_manager()
    existing = list(state.get("messages", []))
    context_messages = ctx_mgr.build_context("executor", existing, prompt)
    result = _get_agent().invoke({"messages": context_messages})
    last_msg = result["messages"][-1].content

    test_passed = "failed" not in last_msg.lower() and "error" not in last_msg.lower()

    ctx_mgr.record_summary(AgentSummary(
        agent="executor",
        summary="测试通过" if test_passed else "测试失败",
        key_findings=[last_msg[:200]],
        files_touched=[],
    ))

    return {
        "test_result": last_msg,
        "test_passed": test_passed,
        "messages": result["messages"],
    }
