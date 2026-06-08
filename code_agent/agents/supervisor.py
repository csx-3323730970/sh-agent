"""Supervisor Agent — 任务拆解 + 动态调度"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_chat_model
from code_agent.state import CodingState

SUPERVISOR_PROMPT = """你是 Supervisor，负责任务分析和多 Agent 调度决策。

## 你的角色
你本身不持有任何工具，只负责：
1. 分析用户请求
2. 拆解为执行计划
3. 决定下一步需要哪个 Agent

## Agent 团队
| Agent | 职责 | 持有工具 |
|-------|------|---------|
| Explorer | 搜索、阅读、理解代码 | read_file, grep, glob_files, list_dir |
| Coder | 编写和修改代码 | read_file, write_file, edit_file, grep, glob_files, list_dir |
| Reviewer | 审查代码质量 | read_file, grep, list_dir, bash |
| Executor | 运行测试和验证 | bash, read_file |

## 典型流程
1. 用户要"修复一个 bug"或"加一个功能" → Explorer(定位代码) → Coder(修改) → Reviewer(审查) → Executor(测试)
2. 用户要"读某段代码" → Explorer(搜索+读取) → 直接回复
3. 用户要"运行测试" → Executor(执行) → 直接回复
4. Reviewer 发现问题 → Coder(修复) → Reviewer(再审)，最多重试 3 轮

## 输出格式
每次决策输出以下格式：

```
任务分析: <一句话分析用户需求>
执行计划: <分步骤描述>
决策: <explore/code/review/execute/finish>
```

决策关键词说明：
- explore: 需要先理解代码
- code: 需要编写/修改代码
- review: 需要审查 Coder 的产出
- execute: 需要运行测试验证
- finish: 所有步骤完成
"""


def supervisor_node(state: CodingState) -> dict:
    agent = create_agent(
        model=get_chat_model(),
        system_prompt=SUPERVISOR_PROMPT,
        tools=[],  # Supervisor 无工具，纯推理
    )

    task = state.get("user_request", "")
    exploration = state.get("exploration_result", "")
    review_feedback = state.get("review_feedback", "")
    review_approved = state.get("review_approved", False)
    test_result = state.get("test_result", "")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    prompt_parts = [f"用户需求: {task}"]

    # 反馈当前状态
    if exploration:
        prompt_parts.append(f"\n[状态] Explorer 已完成代码分析")
    else:
        prompt_parts.append("\n[状态] 尚未探索代码")

    if review_feedback:
        if review_approved:
            prompt_parts.append(f"[状态] Reviewer 审查通过")
        else:
            prompt_parts.append(f"[状态] Reviewer 要求修改 (第{retry_count}/{max_retries}轮)")
    else:
        if exploration and not review_feedback:
            prompt_parts.append("[状态] 代码已修改，等待审查")
        prompt_parts.append("[状态] 尚未审查")

    if test_result:
        prompt_parts.append(f"[状态] Executor 已执行测试")

    prompt_parts.append(f"\n当前审修轮次: {retry_count}/{max_retries}")
    prompt_parts.append("\n请输出你的任务分析和决策。")

    prompt = "\n".join(prompt_parts)

    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    last_msg = result["messages"][-1].content

    # 解析决策关键词
    decision = _parse_decision(last_msg, state)
    task_plan = _extract_plan(last_msg)

    return {
        "task_plan": task_plan,
        "current_agent": decision,
    }


def _parse_decision(text: str, state: CodingState) -> str:
    """从 Supervisor 输出中解析路由决策"""
    text_lower = text.lower()

    # LLM 明确说 finish 时直接结束
    if "finish" in text_lower:
        return "finish"

    # 没有探索过 → 先探索
    if not state.get("exploration_result"):
        return "explore"

    # 已有探索结果但没有审查 → 根据 LLM 决策判断
    # 如果 LLM 决定 code → 去写代码
    # 如果 LLM 决定 review → 去审查
    # 默认：探索后直接结束（读代码等只读任务）
    if not state.get("review_feedback"):
        if "code" in text_lower:
            return "code"
        if "review" in text_lower:
            return "review"
        return "finish"

    # Reviewer 不通过且未超限 → Coder 修复
    if not state.get("review_approved"):
        if state.get("retry_count", 0) < state.get("max_retries", 3):
            return "code"
        return "finish"

    # Reviewer 通过但还没测试 → 执行
    if not state.get("test_result"):
        if "execute" in text_lower or "review" in text_lower:
            return "execute"
        return "execute"

    # 所有步骤完成
    return "finish"


def _extract_plan(text: str) -> str:
    """提取任务计划文本"""
    for line in text.split("\n"):
        if "任务分析" in line or "执行计划" in line:
            continue
    return text[:300]
