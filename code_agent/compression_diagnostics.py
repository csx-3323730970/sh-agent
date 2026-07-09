"""压缩诊断系统 — 可观测性 + 信息损失检测 + 自适应调优

三个核心问题及其解决方案:

1. 怎么发现压缩过度？
   → CompressionAuditor 记录每次压缩的 before/after，计算压缩率和信息损失分

2. 怎么定位问题出在哪个 Agent / 哪个工具？
   → 分层指标: 按 Agent → 按 Tool → 按压缩规则 三级 drill-down

3. 发现后怎么调整？
   → AdaptivePolicy 根据反馈自动调整压缩策略: 放松/收紧/禁用特定规则
"""

import re
import json
import time
import hashlib
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict
from enum import Enum


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

class CompressionAction(Enum):
    KEPT_AS_IS = "kept"           # 内容短，未压缩
    SKELETON = "skeleton"         # AST 骨架提取
    TRUNCATED = "truncated"       # 简单截断
    DIFF_RETAINED = "diff"        # diff 格式保留
    DROPPED = "dropped"           # 超出滑动窗口，整条丢弃
    SUMMARIZED = "summarized"     # 渐进式摘要


@dataclass
class CompressionRecord:
    """单次压缩操作的完整审计记录"""
    timestamp: float
    agent_name: str
    tool_name: str
    action: CompressionAction

    # 尺寸指标
    original_chars: int
    compressed_chars: int
    compression_ratio: float  # 0 = 完全丢弃, 1 = 未压缩

    # 内容指纹（用于跨轮次匹配）
    content_hash: str

    # 语义特征（用于判断是否丢了关键信息）
    had_function_signatures: bool = False
    had_error_messages: bool = False
    had_file_paths: bool = False
    had_diff_content: bool = False
    had_imports: bool = False

    # 原始内容的前 100 字符（用于问题定位）
    original_preview: str = ""

    # 压缩后内容的关键词集合（用于后续匹配 Agent 是否引用了被丢弃的信息）
    kept_keywords: set = field(default_factory=set)
    dropped_keywords: set = field(default_factory=set)


@dataclass
class AgentConfusionSignal:
    """Agent 可能因上下文缺失而产生困惑的信号"""
    agent_name: str
    turn: int
    signal_type: str  # "repeated_question" | "file_not_found_ask" | "uncertainty_marker" | "contradiction"
    message_snippet: str
    confidence: float  # 0~1, 信号可信度
    suspected_missing: list[str] = field(default_factory=list)  # 可能被错误丢弃的信息


@dataclass
class CompressionHealth:
    """压缩健康度快照"""
    overall_ratio: float           # 整体压缩率
    info_loss_risk: float          # 信息损失风险 (0~1)
    agent_confusion_count: int     # Agent 困惑信号数
    dropped_critical_count: int    # 丢弃的关键信息数
    per_tool_stats: dict           # 按工具类型的统计
    per_agent_stats: dict          # 按 Agent 的统计
    recent_warnings: list[str]     # 最近警告


# ═══════════════════════════════════════════════════════════════
# 信息损失检测
# ═══════════════════════════════════════════════════════════════

class InformationLossDetector:
    """检测压缩后的上下文是否丢失了关键信息"""

    # Agent 困惑信号模式 — 当 Agent 说出这些话时，可能意味着上下文被过度压缩
    CONFUSION_PATTERNS = [
        # 重复询问已给过的信息
        (r"(?:请|麻烦|能否|可以).*(?:提供|给出|告诉|说明).*(?:文件|路径|代码|内容)", 0.7,
         "repeated_question", "Agent 在询问上下文中应该已有但可能被压缩掉的信息"),

        # 不确定某文件是否存在
        (r"(?:我不确定|我不清楚|我找不到|似乎没有).*(?:文件|目录|模块|函数|类)", 0.6,
         "file_not_found_ask", "Agent 找不到被 Explorer 发现但被压缩掉的文件"),

        # 明确表达不确定性
        (r"(?:可能是|也许是|推测|猜测|根据现有信息无法|需要更多上下文)", 0.5,
         "uncertainty_marker", "Agent 表达了不确定，可能因为关键上下文被截断"),

        # 前后矛盾 — 先说了 A 又说了 not A
        (r"(?:实际上|等等|不对|我错了|重新.*看)", 0.4,
         "contradiction", "Agent 自我纠正，可能是压缩导致前后信息不一致"),
    ]

    @classmethod
    def detect_confusion(cls, agent_name: str, turn: int, message: str) -> list[AgentConfusionSignal]:
        """扫描 Agent 输出，检测困惑信号"""
        signals = []
        for pattern, confidence, signal_type, _desc in cls.CONFUSION_PATTERNS:
            matches = re.findall(pattern, message, re.IGNORECASE)
            for match in matches:
                snippet = match if isinstance(match, str) else match[0]
                signals.append(AgentConfusionSignal(
                    agent_name=agent_name,
                    turn=turn,
                    signal_type=signal_type,
                    message_snippet=snippet[:150],
                    confidence=confidence,
                ))
        return signals

    @classmethod
    def estimate_info_loss(cls, record: CompressionRecord) -> float:
        """估算单次压缩的信息损失 (0=无损, 1=全损)

        不同 Action 的信息损失权重不同:
        - kept: 0.0 (无损)
        - diff: 0.05 (diff 保留几乎全部有用信息)
        - skeleton: 0.3 (丢了实现细节但保留了结构)
        - truncated: 0.5 (粗暴截断，可能丢中间的关键内容)
        - summarized: 0.4 (摘要有一定语义损失)
        - dropped: 0.9 (几乎全丢)
        """
        base_loss = {
            CompressionAction.KEPT_AS_IS: 0.0,
            CompressionAction.DIFF_RETAINED: 0.05,
            CompressionAction.SKELETON: 0.3,
            CompressionAction.SUMMARIZED: 0.4,
            CompressionAction.TRUNCATED: 0.5,
            CompressionAction.DROPPED: 0.9,
        }.get(record.action, 0.5)

        # 加分项: 压缩后保留了关键语义特征 → 降低损失
        preserved_score = sum([
            record.had_function_signatures,
            record.had_error_messages,
            record.had_file_paths,
            record.had_diff_content,
            record.had_imports,
        ])
        # 每保留一个关键特征，减少 0.05 损失
        bonus = preserved_score * 0.05

        # 加分项: 丢弃了关键特征 → 增加损失
        if record.had_error_messages and record.action == CompressionAction.DROPPED:
            base_loss = min(1.0, base_loss + 0.3)  # 丢弃错误信息是最糟糕的
        if record.had_diff_content and record.action == CompressionAction.DROPPED:
            base_loss = min(1.0, base_loss + 0.2)

        return max(0.0, min(1.0, base_loss - bonus))


# ═══════════════════════════════════════════════════════════════
# 压缩审计器
# ═══════════════════════════════════════════════════════════════

class CompressionAuditor:
    """包装压缩器，记录每次压缩操作的全量指标"""

    def __init__(self, max_records: int = 500):
        self.records: list[CompressionRecord] = []
        self.confusion_signals: list[AgentConfusionSignal] = []
        self.max_records = max_records

        # 按维度的聚合计数器
        self._action_counts: dict[str, int] = defaultdict(int)
        self._tool_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ratio": 0.0, "total_loss": 0.0})
        self._agent_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_ratio": 0.0, "total_loss": 0.0})

        # 关键信息丢失计数器
        self.critical_drops: list[CompressionRecord] = []

    def record(self, agent_name: str, tool_name: str, action: CompressionAction,
               original: str, compressed: str):
        """记录一次压缩操作"""
        if len(self.records) >= self.max_records:
            self.records.pop(0)

        # 语义特征检测
        had_funcs = bool(re.search(r'\bdef\s+\w+\s*\(', original))
        had_errors = bool(re.search(r'(error|Error|ERROR|exception|Exception|Traceback|traceback|失败|错误)', original))
        had_paths = bool(re.search(r'[/\\]?\w+[/\\]\w+\.\w+', original))
        had_diff = original.startswith("[DIFF:") or '@@' in original
        had_imports = bool(re.search(r'^\s*(import|from)\s+\w+', original, re.MULTILINE))

        # 关键词差异分析
        original_keywords = set(re.findall(r'\b[a-zA-Z_]\w{3,}\b', original.lower()))
        compressed_keywords = set(re.findall(r'\b[a-zA-Z_]\w{3,}\b', compressed.lower()))
        kept = original_keywords & compressed_keywords
        dropped = original_keywords - compressed_keywords

        record = CompressionRecord(
            timestamp=time.time(),
            agent_name=agent_name,
            tool_name=tool_name,
            action=action,
            original_chars=len(original),
            compressed_chars=len(compressed),
            compression_ratio=len(compressed) / max(len(original), 1),
            content_hash=hashlib.md5(original.encode()).hexdigest()[:8],
            had_function_signatures=had_funcs,
            had_error_messages=had_errors,
            had_file_paths=had_paths,
            had_diff_content=had_diff,
            had_imports=had_imports,
            original_preview=original[:100],
            kept_keywords=kept,
            dropped_keywords=dropped,
        )

        self.records.append(record)

        # 更新聚合统计
        self._action_counts[action.value] += 1
        ts = self._tool_stats[tool_name]
        ts["count"] += 1
        ts["total_ratio"] += record.compression_ratio
        info_loss = InformationLossDetector.estimate_info_loss(record)
        ts["total_loss"] += info_loss

        ags = self._agent_stats[agent_name]
        ags["count"] += 1
        ags["total_ratio"] += record.compression_ratio
        ags["total_loss"] += info_loss

        # 标记关键信息被丢弃的情况
        if record.had_error_messages and action in (CompressionAction.DROPPED, CompressionAction.TRUNCATED):
            self.critical_drops.append(record)

    def scan_agent_output(self, agent_name: str, turn: int, output: str):
        """扫描 Agent 输出，检测困惑信号"""
        signals = InformationLossDetector.detect_confusion(agent_name, turn, output)
        self.confusion_signals.extend(signals)

    def get_health(self) -> CompressionHealth:
        """计算当前压缩健康度"""
        if not self.records:
            return CompressionHealth(
                overall_ratio=1.0, info_loss_risk=0.0,
                agent_confusion_count=len(self.confusion_signals),
                dropped_critical_count=len(self.critical_drops),
                per_tool_stats={}, per_agent_stats={}, recent_warnings=[]
            )

        # 整体压缩率
        recent = self.records[-100:]
        overall_ratio = sum(r.compression_ratio for r in recent) / len(recent)

        # 信息损失风险 — 越接近 0 越安全
        total_loss = sum(
            InformationLossDetector.estimate_info_loss(r) for r in recent
        )
        info_loss_risk = total_loss / len(recent)

        # 工具维度
        per_tool = {}
        for tool, stats in self._tool_stats.items():
            if stats["count"] > 0:
                per_tool[tool] = {
                    "count": stats["count"],
                    "avg_ratio": round(stats["total_ratio"] / stats["count"], 2),
                    "avg_loss": round(stats["total_loss"] / stats["count"], 2),
                }

        # Agent 维度
        per_agent = {}
        for agent, stats in self._agent_stats.items():
            if stats["count"] > 0:
                per_agent[agent] = {
                    "count": stats["count"],
                    "avg_ratio": round(stats["total_ratio"] / stats["count"], 2),
                    "avg_loss": round(stats["total_loss"] / stats["count"], 2),
                }

        # 生成警告
        warnings = self._generate_warnings(info_loss_risk, overall_ratio, per_tool)

        return CompressionHealth(
            overall_ratio=round(overall_ratio, 2),
            info_loss_risk=round(info_loss_risk, 2),
            agent_confusion_count=len(self.confusion_signals),
            dropped_critical_count=len(self.critical_drops),
            per_tool_stats=per_tool,
            per_agent_stats=per_agent,
            recent_warnings=warnings[-5:],
        )

    def _generate_warnings(self, info_loss_risk: float, overall_ratio: float,
                           per_tool: dict) -> list[str]:
        """生成健康警告"""
        warnings = []

        if info_loss_risk > 0.5:
            warnings.append(f"⚠ 整体信息损失风险偏高 ({info_loss_risk:.2f})，建议检查压缩策略")
        if overall_ratio < 0.1:
            warnings.append(f"⚠ 压缩过于激进 (整体保留率 {overall_ratio:.1%})，Agent 可能缺乏上下文")
        if self.critical_drops:
            recent_criticals = [r for r in self.critical_drops[-5:]]
            for cr in recent_criticals:
                warnings.append(f"⚠ 关键信息被丢弃: [{cr.agent_name}] {cr.tool_name} — {cr.original_preview[:80]}")

        # 工具级别异常
        for tool, stats in per_tool.items():
            if stats["count"] >= 3 and stats["avg_loss"] > 0.6:
                warnings.append(f"⚠ [{tool}] 平均信息损失 {stats['avg_loss']:.2f}，建议降低该工具的压缩强度")

        return warnings

    def reset(self):
        """重置所有统计（新会话时调用）"""
        self.records.clear()
        self.confusion_signals.clear()
        self.critical_drops.clear()
        self._action_counts.clear()
        self._tool_stats.clear()
        self._agent_stats.clear()


# ═══════════════════════════════════════════════════════════════
# 自适应压缩策略
# ═══════════════════════════════════════════════════════════════

class AdaptivePolicy:
    """根据健康反馈自动调整压缩策略

    核心逻辑:
    - 如果某工具的 avg_loss > 阈值 → 降低该工具的压缩强度（改为 KEPT_AS_IS）
    - 如果 Agent 困惑信号增多 → 扩大滑动窗口
    - 如果整体压缩率健康 → 可以逐步恢复激进策略
    """

    # 各工具的压缩强度: 0 = 不压缩, 1 = 正常, 2 = 激进
    _tool_aggressiveness: dict[str, int] = defaultdict(lambda: 1)

    @classmethod
    def get_max_chars(cls, tool_name: str) -> int:
        """根据当前策略返回该工具的 max_chars 阈值"""
        level = cls._tool_aggressiveness.get(tool_name, 1)
        if level == 0:
            return 999999  # 不压缩
        elif level == 1:
            return 1000    # 正常
        else:
            return 300     # 激进

    @classmethod
    def should_skip_compression(cls, tool_name: str) -> bool:
        """是否跳过该工具的压缩"""
        return cls._tool_aggressiveness.get(tool_name, 1) == 0

    @classmethod
    def adjust(cls, health: CompressionHealth):
        """根据健康报告调整策略"""
        # 规则 1: 单工具损失 > 0.6 → 降级（不压缩该工具）
        for tool, stats in health.per_tool_stats.items():
            if stats.get("avg_loss", 0) > 0.6 and stats.get("count", 0) >= 3:
                current = cls._tool_aggressiveness[tool]
                if current > 0:
                    cls._tool_aggressiveness[tool] = current - 1

        # 规则 2: Agent 困惑信号 > 3 → 整体降级（扩大窗口）
        if health.agent_confusion_count > 3:
            from code_agent.context_manager import AGENT_CONTEXT_CONFIG
            for agent_name in AGENT_CONTEXT_CONFIG:
                AGENT_CONTEXT_CONFIG[agent_name]["max_history"] = min(
                    20, AGENT_CONTEXT_CONFIG[agent_name]["max_history"] + 2
                )

        # 规则 3: 连续健康 → 逐步恢复
        if health.info_loss_risk < 0.2 and health.agent_confusion_count == 0:
            for tool in cls._tool_aggressiveness:
                if cls._tool_aggressiveness[tool] < 1:
                    cls._tool_aggressiveness[tool] += 1

    @classmethod
    def get_policy_report(cls) -> dict:
        """获取当前策略状态"""
        return {
            "tool_aggressiveness": dict(cls._tool_aggressiveness),
            "description": {
                0: "不压缩 (保护模式)",
                1: "正常压缩",
                2: "激进压缩",
            },
        }


# ═══════════════════════════════════════════════════════════════
# 诊断报告渲染
# ═══════════════════════════════════════════════════════════════

def render_compression_report(health: CompressionHealth, policy: dict) -> str:
    """渲染可读的压缩诊断报告"""
    lines = [
        "=" * 60,
        "  上下文压缩诊断报告",
        "=" * 60,
        "",
        f"整体压缩率:     {health.overall_ratio:.1%}  (保留 {health.overall_ratio:.0%} 的原始内容)",
        f"信息损失风险:   {health.info_loss_risk:.2f}  (0=无损, 1=严重损失)",
        f"Agent 困惑信号: {health.agent_confusion_count}",
        f"关键信息丢弃:   {health.dropped_critical_count}",
        "",
    ]

    if health.recent_warnings:
        lines.append("─" * 40)
        lines.append("  ⚠ 警告")
        lines.append("─" * 40)
        for w in health.recent_warnings:
            lines.append(f"  {w}")
        lines.append("")

    if health.per_tool_stats:
        lines.append("─" * 40)
        lines.append("  按工具统计")
        lines.append("─" * 40)
        lines.append(f"  {'工具':<20} {'次数':<6} {'保留率':<8} {'损失分':<8}")
        for tool, stats in sorted(health.per_tool_stats.items()):
            lines.append(f"  {tool:<20} {stats['count']:<6} {stats['avg_ratio']:<8.0%} {stats['avg_loss']:<8.2f}")

    if health.per_agent_stats:
        lines.append("")
        lines.append("─" * 40)
        lines.append("  按 Agent 统计")
        lines.append("─" * 40)
        lines.append(f"  {'Agent':<15} {'次数':<6} {'保留率':<8} {'损失分':<8}")
        for agent, stats in sorted(health.per_agent_stats.items()):
            lines.append(f"  {agent:<15} {stats['count']:<6} {stats['avg_ratio']:<8.0%} {stats['avg_loss']:<8.2f}")

    lines.append("")
    lines.append("─" * 40)
    lines.append("  自适应策略状态")
    lines.append("─" * 40)
    for tool, level in policy.get("tool_aggressiveness", {}).items():
        desc = policy.get("description", {}).get(level, "未知")
        lines.append(f"  {tool}: {desc} (level={level})")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

_auditor: Optional[CompressionAuditor] = None


def get_auditor() -> CompressionAuditor:
    global _auditor
    if _auditor is None:
        _auditor = CompressionAuditor()
    return _auditor
