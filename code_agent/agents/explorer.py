"""Explorer Agent — 搜索、阅读、理解代码"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_chat_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState

EXPLORER_PROMPT = """你是 Code Explorer，负责理解和分析现有代码。

## 你的职责
1. 使用 read_file / grep / glob_files / list_dir 探索项目结构
2. 找到与用户需求相关的文件和代码片段
3. 分析代码逻辑，返回结构化的分析结果

## 规则
- 每次探索后，用中文简洁总结你的发现（相关文件、关键函数、需要修改的位置）
- 如果有不确定的地方，明确标注"需确认"
- 探索完成后在回复末尾写上 [探索完成]
"""


def explorer_node(state: CodingState) -> dict:
    agent = create_agent(
        model=get_chat_model(),
        system_prompt=EXPLORER_PROMPT,
        tools=AGENT_TOOLS["explorer"],
    )

    task = state.get("user_request", "")
    workspace = state.get("workspace_dir", ".")
    plan = state.get("task_plan", "")

    prompt = f"工作目录: {workspace}\n任务计划: {plan}\n\n请探索以下需求的代码:\n{task}"

    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    last_msg = result["messages"][-1].content

    # 提取相关文件列表
    relevant_files = _extract_files(last_msg, workspace)

    return {
        "exploration_result": last_msg,
        "relevant_files": relevant_files,
    }


def _extract_files(text: str, workspace: str) -> list[str]:
    """从探索结果中提取文件路径"""
    import re
    import os
    files = set()
    patterns = [
        r'[\w/\-]+\.\w{1,6}',  # 通用文件路径
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text):
            abs_path = os.path.join(workspace, match)
            if os.path.isfile(abs_path):
                files.add(match)
    return list(files)[:10]
