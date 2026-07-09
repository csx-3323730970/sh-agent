"""Parallel Explorer — 并发探索多个代码区域

这是多 Agent 架构对单 Agent 的核心优势之一:
单 Agent 必须串行搜索 — 先看前端，再看后端，再看数据库。
并行 Explorer 可以同时搜索 3 个区域，总耗时 = max(单次耗时) 而非 sum(单次耗时)。

使用场景:
- 全栈项目: 同时探索 frontend/ + backend/ + shared/
- 微服务: 同时探索多个独立的服务目录
- 跨模块依赖: 同时探索调用方和被调用方
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

from code_agent.model_factory import get_agent_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState
from code_agent.project_context import get_project_context, format_project_context
from code_agent.context_manager import get_context_manager, AgentSummary

PARALLEL_EXPLORER_PROMPT = """你是 Code Explorer 的一个并行实例，负责搜索代码库的特定区域。

## 你的搜索范围
你只负责分配给你的子任务，不需要理解整个项目。

## 工作流程
1. 用 list_dir 了解你负责的目录结构
2. 用 glob_files 找到相关文件
3. 用 grep 搜索关键符号
4. 用 read_file 精读关键文件

## 输出要求
- 以 [子任务完成] 结尾
- 列出你的发现：文件路径 + 关键代码 + 与子任务的关系
"""

_agent_cache: dict[str, object] = {}
_cache_lock = Lock()


def _get_parallel_agent(thread_id: str):
    """每个线程获取自己的 agent 实例（线程安全懒加载）"""
    if thread_id not in _agent_cache:
        with _cache_lock:
            if thread_id not in _agent_cache:
                _agent_cache[thread_id] = create_react_agent(
                    model=get_agent_model("explorer"),
                    tools=AGENT_TOOLS["explorer"],
                    prompt=PARALLEL_EXPLORER_PROMPT,
                )
    return _agent_cache[thread_id]


def _explore_single_target(target: str, workspace: str, thread_id: str) -> dict:
    """在单个线程中探索一个子目标"""
    agent = _get_parallel_agent(thread_id)

    ctx = get_project_context(workspace)
    proj_info = format_project_context(ctx)

    prompt = (
        f"## 项目信息\n{proj_info}\n\n"
        f"## 你的子任务\n{target}\n\n"
        f"请只关注这个子任务，搜索相关代码并报告你的发现。\n"
        f"完成后以 [子任务完成] 结尾。"
    )

    try:
        result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
        last_msg = result["messages"][-1].content if result.get("messages") else ""
        return {
            "target": target,
            "result": last_msg,
            "success": True,
        }
    except Exception as e:
        return {
            "target": target,
            "result": f"[错误] {str(e)}",
            "success": False,
        }


def parallel_explorer_node(state: CodingState) -> dict:
    """并发探索多个代码区域

    从 task_plan 中解析 Supervisor 拆解的并行子任务，
    每个子任务在独立线程中运行 Explorer agent。
    """
    workspace = state.get("workspace_dir", ".")
    task_plan = state.get("task_plan", "")
    user_request = state.get("user_request", "")

    # 从 task_plan 中解析并行子任务
    targets = _parse_parallel_targets(task_plan, user_request)

    if len(targets) <= 1:
        # 没有并行任务 → 回退到普通 explorer
        from code_agent.agents.explorer import explorer_node
        return explorer_node(state)

    # 并发执行
    all_results = []
    all_files = []

    with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as executor:
        futures = {}
        for i, target in enumerate(targets):
            future = executor.submit(_explore_single_target, target, workspace, f"parallel_{i}")
            futures[future] = target

        for future in as_completed(futures):
            target = futures[future]
            try:
                result = future.result(timeout=120)
                all_results.append(result)
                if result.get("success"):
                    # 提取文件路径
                    import re
                    files = re.findall(r'[\w/\-]+\.\w{1,6}', result.get("result", ""))
                    all_files.extend(files)
            except Exception as e:
                all_results.append({"target": target, "result": str(e), "success": False})

    # 聚合结果
    combined = _aggregate_results(all_results, targets)

    ctx_mgr = get_context_manager()
    ctx_mgr.record_summary(AgentSummary(
        agent="parallel_explorer",
        summary=f"并行探索完成: {len(targets)} 个子任务, {sum(1 for r in all_results if r['success'])} 个成功",
        key_findings=[r["target"][:100] for r in all_results if r["success"]],
        files_touched=list(set(all_files))[:10] if all_files else [],
    ))

    return {
        "exploration_result": combined,
        "relevant_files": list(set(all_files))[:15] if all_files else None,
        "messages": state.get("messages", []),
    }


def _parse_parallel_targets(task_plan: str, user_request: str) -> list[str]:
    """从 Supervisor 的任务计划中解析并行子任务

    Supervisor 输出格式:
    ```
    并行探索:
    - frontend/src/components/ (前端组件)
    - backend/api/ (后端API)
    - shared/types/ (共享类型)
    ```
    """
    targets = []

    # 找 "并行" 相关的行
    in_parallel_section = False
    for line in task_plan.split("\n"):
        stripped = line.strip()
        if "并行" in stripped:
            in_parallel_section = True
            continue
        if in_parallel_section and stripped.startswith(("-", "•", "*", "1.", "2.", "3.")):
            target = stripped.lstrip("-•* 0123456789.").strip()
            if target and len(target) > 3:
                targets.append(target)
        elif in_parallel_section and not stripped.startswith(("-", "•", "*")):
            # 离开了并行列表
            if targets:
                break

    # 如果 Supervisor 没有明确给并行计划，尝试从用户请求中生成
    if not targets:
        targets = _infer_parallel_targets(user_request)

    return targets[:4]  # 最多 4 个并行任务


def _infer_parallel_targets(user_request: str) -> list[str]:
    """从用户请求中推断可并行的搜索目标"""
    targets = []

    # 检测常见的并行搜索模式
    if "前后端" in user_request or "全栈" in user_request:
        targets = ["探索前端相关代码", "探索后端API代码"]
    elif "多个" in user_request and "模块" in user_request:
        targets = ["探索所有相关模块的代码结构"]
    elif "对比" in user_request or "比较" in user_request:
        parts = user_request.split("对比" if "对比" in user_request else "比较")
        if len(parts) >= 2:
            items = parts[1].strip().split()
            targets = [f"探索 {item} 相关代码" for item in items[:3]]

    return targets


def _aggregate_results(results: list[dict], targets: list[str]) -> str:
    """聚合并行探索结果"""
    parts = [f"## 并行探索结果 ({len(targets)} 个子任务)\n"]

    for r in results:
        status = "✅" if r["success"] else "❌"
        parts.append(f"### {status} {r['target']}")
        result_text = r["result"]
        if len(result_text) > 1500:
            result_text = result_text[:1500] + "\n... [截断]"
        parts.append(result_text)
        parts.append("")

    return "\n".join(parts)
