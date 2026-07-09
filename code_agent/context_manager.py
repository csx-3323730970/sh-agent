"""上下文管理 — 分层压缩 + 滑动窗口 + 渐进式摘要"""
import ast
from typing import TypedDict, Optional
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage
from code_agent.compression_diagnostics import (
    CompressionAuditor, CompressionAction, AdaptivePolicy, get_auditor,
)


class AgentSummary(TypedDict):
    agent: str
    summary: str
    key_findings: list[str]
    files_touched: list[str]


# ── 各 Agent 上下文预算（tokens，基于 4 chars/token 估算） ──
AGENT_CONTEXT_CONFIG = {
    "supervisor": {"max_history": 8, "max_tool_results": 0, "include_summaries": True},
    "explorer":   {"max_history": 4, "max_tool_results": 10, "include_summaries": False},
    "coder":      {"max_history": 4, "max_tool_results": 6, "include_summaries": True},
    "reviewer":   {"max_history": 4, "max_tool_results": 6, "include_summaries": True},
    "executor":   {"max_history": 2, "max_tool_results": 4, "include_summaries": False},
}


class ToolResultCompressor:
    """工具返回内容压缩 — 不同工具不同策略"""

    @staticmethod
    def compress(tool_name: str, content: str, max_chars: int = 1000,
                 agent_name: str = "unknown") -> str:
        if not content or not isinstance(content, str):
            return str(content)[:max_chars]

        # 自适应策略: 该工具是否被标记为"不压缩"
        if AdaptivePolicy.should_skip_compression(tool_name):
            max_chars = 999999

        original = content
        action = CompressionAction.KEPT_AS_IS

        # 已经是压缩格式 (diff / grep 结果) — 直接保留
        if content.startswith("[DIFF:") or content.startswith("──"):
            lines = content.split("\n")
            if len(lines) <= 30:
                result = content
                action = CompressionAction.DIFF_RETAINED
            else:
                result = "\n".join(lines[:30]) + f"\n... (截断，共 {len(lines)} 行)"
                action = CompressionAction.DIFF_RETAINED
        # read_file 返回的带行号文件 — 提取骨架
        elif "│" in content and len(content) > 2000:
            result = ToolResultCompressor._extract_skeleton(content)
            action = CompressionAction.SKELETON
        # 通用截断
        elif len(content) > max_chars:
            half = max_chars // 2
            result = content[:half] + f"\n... [截断: {len(content)} 字符] ...\n" + content[-half:]
            action = CompressionAction.TRUNCATED
        else:
            result = content
            action = CompressionAction.KEPT_AS_IS

        # 记录审计日志
        auditor = get_auditor()
        auditor.record(
            agent_name=agent_name,
            tool_name=tool_name,
            action=action,
            original=original,
            compressed=result,
        )

        return result

    @staticmethod
    def _extract_skeleton(content: str) -> str:
        """从文件内容提取骨架 — 保留函数/类签名，丢弃实现体"""
        # 先尝试 AST 解析
        try:
            # 去掉行号前缀 "   1│ "
            clean_lines = []
            for line in content.split("\n"):
                if "│" in line:
                    code = line.split("│", 1)[-1]
                    clean_lines.append(code)
            clean = "\n".join(clean_lines)
            tree = ast.parse(clean)
            skeletons = []
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    args = []
                    for a in node.args.args:
                        arg_str = a.arg
                        if a.annotation:
                            arg_str += f": {ast.unparse(a.annotation)}"
                        args.append(arg_str)
                    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                    skeletons.append(
                        f"def {node.name}({', '.join(args)}){ret}: ...  (行{node.lineno})"
                    )
                elif isinstance(node, ast.ClassDef):
                    bases = ", ".join(ast.unparse(b) for b in node.bases) if node.bases else ""
                    base_str = f"({bases})" if bases else ""
                    skeletons.append(f"class {node.name}{base_str}: ...  (行{node.lineno})")
            if skeletons:
                return f"[代码骨架 — {len(skeletons)} 个符号]\n" + "\n".join(skeletons)
        except (SyntaxError, Exception):
            pass

        # AST 解析失败 → 截断
        lines = content.split("\n")
        if len(lines) > 50:
            return "\n".join(lines[:50]) + f"\n... [文件共 {len(lines)} 行]"
        return content


class ContextManager:
    """为每个 Agent 构建裁剪后的上下文"""

    def __init__(self):
        self.compressor = ToolResultCompressor()
        self._session_summaries: list[AgentSummary] = []

    def reset(self):
        """重置会话级摘要（新会话开始时调用）"""
        self._session_summaries.clear()

    def record_summary(self, summary: AgentSummary):
        """Agent 完成后记录结构化摘要"""
        self._session_summaries.append(summary)

    def build_context(
        self,
        agent_name: str,
        messages: list,
        current_prompt: str,
        summaries: Optional[list[AgentSummary]] = None,
    ) -> list:
        """构建适合当前 Agent 的上下文"""
        self._current_agent = agent_name  # 供 _compress_messages 使用
        config = AGENT_CONTEXT_CONFIG.get(agent_name, AGENT_CONTEXT_CONFIG["explorer"])
        context_messages = []

        # ── 第1层：前序 Agent 摘要（如需要） ──
        if config["include_summaries"] and (summaries or self._session_summaries):
            summaries_text = self._format_summaries(summaries or self._session_summaries)
            context_messages.append(SystemMessage(content=summaries_text))

        # ── 第2层：最近 N 轮历史（压缩后） ──
        max_hist = config["max_history"]
        if max_hist > 0 and messages:
            recent = messages[-max_hist:]
            context_messages.extend(self._compress_messages(recent, config["max_tool_results"]))

        # ── 第3层：当前任务 prompt ──
        context_messages.append(HumanMessage(content=current_prompt))

        # ── 第4层：更早历史的渐进式摘要（如果有大量旧消息） ──
        if len(messages) > max_hist + 10:
            older = messages[:-max_hist]
            summary_text = self._rolling_summary(older)
            if summary_text:
                context_messages.insert(0, SystemMessage(
                    content=f"<earlier_context>\n{summary_text}\n</earlier_context>"
                ))
                # 记录被摘要的消息
                auditor = get_auditor()
                for old_msg in older:
                    content = old_msg.content if hasattr(old_msg, "content") else ""
                    if isinstance(content, str) and len(content) > 100:
                        auditor.record(
                            agent_name=agent_name,
                            tool_name=getattr(old_msg, "name", "history"),
                            action=CompressionAction.SUMMARIZED,
                            original=content,
                            compressed=summary_text[:500],
                        )

        return context_messages

    def _compress_messages(self, messages: list, max_tool: int) -> list:
        """压缩消息列表 — 裁剪工具返回，限制数量"""
        compressed = []
        tool_count = 0
        agent_name = getattr(self, "_current_agent", "unknown")

        for msg in messages:
            msg_type = getattr(msg, "type", "unknown")

            if msg_type == "tool" or hasattr(msg, "tool_call_id"):
                if max_tool > 0 and tool_count >= max_tool:
                    # 滑动窗口丢弃 — 记录审计
                    content = msg.content if hasattr(msg, "content") else ""
                    if isinstance(content, str) and content:
                        get_auditor().record(
                            agent_name=agent_name,
                            tool_name=getattr(msg, "name", "unknown"),
                            action=CompressionAction.DROPPED,
                            original=content,
                            compressed="[已丢弃: 超出滑动窗口]",
                        )
                    continue
                tool_count += 1

                # 压缩工具返回内容
                content = msg.content if hasattr(msg, "content") else str(msg)
                if isinstance(content, str):
                    compressed_content = self.compressor.compress(
                        getattr(msg, "name", "unknown"), content,
                        max_chars=AdaptivePolicy.get_max_chars(getattr(msg, "name", "unknown")),
                        agent_name=agent_name,
                    )
                    new_msg = ToolMessage(
                        content=compressed_content,
                        tool_call_id=getattr(msg, "tool_call_id", ""),
                        name=getattr(msg, "name", ""),
                    )
                    compressed.append(new_msg)
                else:
                    compressed.append(msg)
            else:
                # AI / Human / System 消息原样保留
                compressed.append(msg)

        return compressed

    def _format_summaries(self, summaries: list[AgentSummary]) -> str:
        """格式化 Agent 摘要列表"""
        lines = ["<agent_summaries>"]
        for s in summaries:
            lines.append(f"\n[{s['agent'].upper()}] {s['summary']}")
            if s.get("key_findings"):
                for f in s["key_findings"][:5]:
                    lines.append(f"  - {f}")
            if s.get("files_touched"):
                lines.append(f"  涉及文件: {', '.join(s['files_touched'][:5])}")
        lines.append("\n</agent_summaries>")
        return "\n".join(lines)

    def _rolling_summary(self, messages: list) -> str:
        """对旧消息做渐进式摘要"""
        if not messages:
            return ""

        # 提取关键信息：用户请求、Agent 决策、文件改动
        summary_parts = []
        for msg in messages:
            content = msg.content if hasattr(msg, "content") else ""
            if not isinstance(content, str) or not content:
                continue

            msg_type = getattr(msg, "type", "")
            if msg_type == "human":
                # 用户消息 → 保留完整内容
                summary_parts.append(f"[用户] {content[:200]}")
            elif msg_type == "ai":
                # AI 消息 → 提取关键决策点
                for keyword in ["决策:", "任务分析:", "审查通过", "审查不通过", "[探索完成]", "[编码完成]"]:
                    if keyword in content:
                        idx = content.index(keyword)
                        snippet = content[idx:idx + 150].replace("\n", " ")
                        summary_parts.append(f"[AI] {snippet}")
                        break
            elif msg_type == "tool":
                # 工具消息 → 只记录操作类型
                tool_name = getattr(msg, "name", "?")
                content_preview = content[:100].replace("\n", " ")
                summary_parts.append(f"[工具] {tool_name}: {content_preview}")

            if len(summary_parts) >= 15:
                break

        return "\n".join(summary_parts) if summary_parts else ""


# ── 全局单例 ──
_context_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
