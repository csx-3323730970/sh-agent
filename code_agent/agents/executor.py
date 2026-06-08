"""Executor Agent — 运行测试、验证结果"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_chat_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState

EXECUTOR_PROMPT = """你是 Code Executor，负责运行代码和测试来验证改动是否正确。

## 你的职责
1. 运行相关测试验证改动没有破坏现有功能
2. 如果没有现成测试，运行代码验证语法和基本逻辑
3. 清晰汇报执行结果

## 规则
- 优先运行项目已有的测试套件（pytest、npm test 等）
- 先确认项目结构再决定运行什么命令
- 如果测试失败，清楚说明哪个测试失败了、可能的原因
- 执行完成在末尾写上 [执行完成]
"""


def executor_node(state: CodingState) -> dict:
    agent = create_agent(
        model=get_chat_model(),
        system_prompt=EXECUTOR_PROMPT,
        tools=AGENT_TOOLS["executor"],
    )

    workspace = state.get("workspace_dir", ".")
    relevant_files = state.get("relevant_files", [])

    prompt_parts = [
        f"工作目录: {workspace}",
        "请验证代码改动的正确性。",
        "用 list_dir 查看项目结构，判断项目类型（Python/Node/其他），然后运行相应的测试或语法检查命令。",
    ]

    if relevant_files:
        prompt_parts.append(f"改动涉及的文件: {', '.join(relevant_files)}")

    prompt = "\n".join(prompt_parts)

    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    last_msg = result["messages"][-1].content

    return {
        "test_result": last_msg,
        "test_passed": "failed" not in last_msg.lower() and "error" not in last_msg.lower(),
    }
