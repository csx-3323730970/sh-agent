"""CLI 入口 — 终端 REPL"""
import os
import sys
import uuid
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text
from rich.console import Console
from langchain_core.messages import HumanMessage

from code_agent.state import CodingState
from code_agent.graph import compile_graph
from code_agent.config import get_setting
from rich.panel import Panel
from code_agent.ui.terminal import (
    console, render_banner, render_help, render_agent_header, create_prompt_session,
)


console = Console()


def main():
    render_banner()

    # 没有 .env 则自动启动配置向导
    if not os.path.exists(".env"):
        console.print("[yellow]未检测到 .env 配置文件，启动配置向导...[/yellow]")
        _setup_wizard()

    # 编译 graph
    console.print("[dim]正在初始化 Multi-Agent 系统...[/dim]")
    try:
        graph = compile_graph(with_checkpoint=True)
        console.print("[green]Redis checkpoint 已连接[/green]")
    except Exception:
        console.print("[yellow]Redis 不可用，使用无记忆模式[/yellow]")
        graph = compile_graph(with_checkpoint=False)

    console.print(
        "\n[dim]💡 输入数字 1-4 快速开始，或直接输入你的编程问题[/dim]"
    )

    # 终端 REPL
    session = create_prompt_session()
    workspace_dir = os.getcwd()

    # 快捷问题映射
    shortcuts = {
        "1": "列出当前项目的文件结构",
        "2": "帮我分析 code_agent/graph.py 的代码逻辑",
        "3": "审查 code_agent/agents/supervisor.py 的代码质量",
        "4": "在 code_agent/ 下新增一个单元测试模块",
    }

    while True:
        try:
            user_input = session.prompt([("class:prompt", "\n> ")]).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见！[/dim]")
            break

        if not user_input:
            continue

        # 快捷数字映射
        if user_input in shortcuts:
            user_input = shortcuts[user_input]
            console.print(f"[dim]→ {user_input}[/dim]")

        # 内置命令
        if user_input.startswith("/"):
            _handle_command(user_input, console)
            continue

        # 构建初始状态
        session_id = uuid.uuid4().hex[:12]
        config = {"configurable": {"thread_id": session_id}}

        initial_state: CodingState = {
            "messages": [HumanMessage(content=user_input)],
            "user_request": user_input,
            "workspace_dir": workspace_dir,
            "task_plan": "",
            "current_agent": "supervisor",
            "exploration_result": None,
            "relevant_files": None,
            "code_changes": None,
            "review_feedback": None,
            "review_approved": False,
            "test_result": None,
            "test_passed": False,
            "retry_count": 0,
            "max_retries": get_setting("agent", "max_review_retries"),
            "final_response": None,
            "task_complete": False,
        }

        # 流式执行并渲染
        console.print()
        last_agent = None

        try:
            for chunk in graph.stream(initial_state, config, stream_mode="values"):
                current_agent = chunk.get("current_agent", "")
                messages = chunk.get("messages", [])

                # 渲染 Agent 切换
                if current_agent and current_agent != last_agent:
                    console.print(render_agent_header(current_agent))
                    last_agent = current_agent

                # 渲染最新消息
                if messages:
                    latest = messages[-1]
                    if hasattr(latest, "content") and latest.content:
                        _render_message(latest, current_agent)

            # 最终输出
            final = chunk.get("final_response", "")
            if final:
                console.print()
                console.print(Markdown(final))

        except Exception as e:
            console.print(f"\n[red]执行出错: {e}[/red]")

        console.print("[dim]─" * 60 + "[/dim]")


def _render_message(message, agent_name: str):
    """渲染一条消息"""
    content = message.content if hasattr(message, "content") else str(message)
    if not content:
        return

    # 工具调用消息特殊处理
    if hasattr(message, "tool_calls") and message.tool_calls:
        for tc in message.tool_calls:
            name = tc.get("name", "?")
            args = str(tc.get("args", {}))[:80]
            console.print(f"  [dim]🔧 {name}({args})[/dim]")
        return

    # 工具返回结果
    if hasattr(message, "name") and message.name:
        preview = content[:150].replace("\n", " ")
        console.print(f"  [dim]  → {preview}[/dim]")
        return

    # 普通文本
    if hasattr(message, "type") and message.type == "human":
        console.print(f"[bold white]{content}[/bold white]")
    else:
        # AI 回复截取前 200 字符在终端展示
        preview = content[:300]
        if len(content) > 300:
            preview += "..."
        console.print(f"  {preview}")


def _handle_command(cmd: str, console: Console):
    cmd = cmd.lower().strip()
    if cmd == "/quit" or cmd == "/q":
        console.print("[dim]再见！[/dim]")
        sys.exit(0)
    elif cmd == "/help" or cmd == "/h":
        render_help()
    elif cmd == "/clear":
        console.clear()
        render_banner()
    elif cmd == "/status":
        console.print(f"[dim]工作目录: {os.getcwd()}[/dim]")
    elif cmd == "/setup":
        _setup_wizard()
    else:
        console.print(f"[yellow]未知命令: {cmd}[/yellow] 输入 /help 查看帮助")


def _setup_wizard():
    """交互式配置向导"""
    from rich.prompt import Prompt, Confirm
    from pathlib import Path

    console.print()
    console.print(Panel(
        "[bold]🔧 API 配置向导[/bold]",
        border_style="cyan",
    ))
    console.print()

    # API Key
    current_key = os.getenv("OPENAI_API_KEY", "")
    masked = current_key[:8] + "****" if len(current_key) > 8 else "(未设置)"
    console.print(f"当前 API Key: [dim]{masked}[/dim]")
    api_key = Prompt.ask("请输入 API Key", default=current_key or "", password=True)

    # Base URL
    current_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    console.print(f"\n当前 Base URL: [dim]{current_url}[/dim]")
    console.print("[dim]常用: 1=DeepSeek  2=OpenAI  3=自定义[/dim]")
    choice = Prompt.ask("选择 (1/2/3)", default="1")
    if choice == "1":
        base_url = "https://api.deepseek.com"
    elif choice == "2":
        base_url = "https://api.openai.com/v1"
    else:
        base_url = Prompt.ask("输入自定义 URL", default=current_url)

    # 写入 .env
    env_path = Path(".env")
    content = (
        f"OPENAI_API_KEY={api_key}\n"
        f"OPENAI_BASE_URL={base_url}\n"
        f"PG_PASSWORD=\n"
    )
    env_path.write_text(content, encoding="utf-8")
    console.print(f"\n[green]✅ 配置已保存到 {env_path}[/green]")

    # 测试连接
    if Confirm.ask("\n是否测试 API 连接?", default=True):
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_BASE_URL"] = base_url
        try:
            from code_agent.model_factory import get_chat_model
            # 清除单例缓存，使用新配置
            import code_agent.model_factory as mf
            mf._chat_model = None
            model = get_chat_model()
            from langchain_core.messages import HumanMessage
            resp = model.invoke([HumanMessage(content="回复OK两个字母，不要其他内容")])
            console.print(f"[green]✅ 连接成功！模型: {model.model_name}[/green]")
        except Exception as e:
            console.print(f"[red]❌ 连接失败: {e}[/red]")
            console.print("[yellow]请检查 API Key 和 Base URL 是否正确[/yellow]")

    console.print()


if __name__ == "__main__":
    main()
