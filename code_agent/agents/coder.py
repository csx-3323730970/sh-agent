"""Coder Agent — 写代码、改代码"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_chat_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState

CODER_PROMPT = """你是 Code Writer，负责编写和修改代码。

## 你的职责
1. 根据 Explorer 的分析结果和用户需求，编写或修改代码
2. 使用 write_file 创建新文件，edit_file 修改现有文件
3. 遵循项目现有的代码风格

## 规则
- 修改前先用 read_file 确认文件当前内容
- edit_file 的 old_string 必须与文件内容精确匹配（包括缩进、空行）
- 每次修改后说明改了什么、为什么这样改
- 如果 Review 反馈要求修改，按要求调整
- 完成所有修改后在末尾写上 [编码完成]

## 代码规范
- 不引入安全漏洞（SQL注入、命令注入、XSS等）
- 不写多余注释
- 保持和现有代码风格一致
"""


def coder_node(state: CodingState) -> dict:
    agent = create_agent(
        model=get_chat_model(),
        system_prompt=CODER_PROMPT,
        tools=AGENT_TOOLS["coder"],
    )

    task = state.get("user_request", "")
    workspace = state.get("workspace_dir", ".")
    exploration = state.get("exploration_result", "")
    relevant_files = state.get("relevant_files", [])
    review_feedback = state.get("review_feedback", "")

    prompt_parts = [
        f"工作目录: {workspace}",
        f"用户需求: {task}",
    ]

    if exploration:
        prompt_parts.append(f"\nExplorer 的分析结果:\n{exploration}")
    if relevant_files:
        prompt_parts.append(f"\n相关文件: {', '.join(relevant_files)}")
    if review_feedback:
        prompt_parts.append(f"\nReviewer 的修改要求:\n{review_feedback}")

    prompt = "\n".join(prompt_parts)

    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    last_msg = result["messages"][-1].content

    return {"code_changes": [], "messages": result["messages"]}
