"""Reviewer Agent — 审查代码改动，检查质量"""
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from code_agent.model_factory import get_chat_model
from code_agent.tools.registry import AGENT_TOOLS
from code_agent.state import CodingState

REVIEWER_PROMPT = """你是 Code Reviewer，负责审查代码改动质量。

## 你的职责
1. 读取改动后的文件，检查代码质量和正确性
2. 检查潜在问题：逻辑错误、安全漏洞、边界情况、代码风格
3. 给出明确的审查结论

## 审查清单
- [ ] 逻辑正确：改动是否实现了需求
- [ ] 安全：有无注入风险、密钥泄露、权限问题
- [ ] 边界：空输入、大文件、异常情况是否处理
- [ ] 风格：是否和项目现有代码风格一致
- [ ] 副作用：改动是否会影响其他模块

## 输出格式
审查完毕后，给出明确结论：
- 通过：回复以 **审查通过** 开头
- 需修改：回复以 **审查不通过** 开头，然后列出具体需要修改的内容

示例：
```
审查通过。改动逻辑正确，无安全风险，代码风格一致。

小建议（可选）：第 23 行的变量名可改得更语义化。
```
"""


def reviewer_node(state: CodingState) -> dict:
    agent = create_agent(
        model=get_chat_model(),
        system_prompt=REVIEWER_PROMPT,
        tools=AGENT_TOOLS["reviewer"],
    )

    task = state.get("user_request", "")
    workspace = state.get("workspace_dir", ".")
    exploration = state.get("exploration_result", "")
    relevant_files = state.get("relevant_files", [])

    prompt_parts = [
        f"工作目录: {workspace}",
        f"用户原需求: {task}",
        f"\nExplorer 的分析: {exploration}",
    ]

    if relevant_files:
        prompt_parts.append(f"\n请审查以下文件的当前状态: {', '.join(relevant_files)}")
        prompt_parts.append("先用 read_file 读取每个文件的最新内容，再进行审查。")

    prompt = "\n".join(prompt_parts)

    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    last_msg = result["messages"][-1].content

    approved = "审查通过" in last_msg

    return {
        "review_feedback": last_msg,
        "review_approved": approved,
    }
