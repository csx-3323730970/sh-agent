"""测试压缩诊断系统 — 审计记录 + 信息损失检测 + 困惑信号 + 自适应策略"""
import pytest
from code_agent.compression_diagnostics import (
    CompressionAuditor, CompressionAction, CompressionRecord,
    InformationLossDetector, AdaptivePolicy, CompressionHealth,
    AgentConfusionSignal, get_auditor,
)


class TestCompressionAuditor:
    """审计记录测试"""

    def test_record_single_compression(self):
        auditor = CompressionAuditor()
        auditor.record(
            agent_name="explorer",
            tool_name="read_file",
            action=CompressionAction.SKELETON,
            original="def foo():\n    return 1\ndef bar():\n    return 2\n",
            compressed="[骨架] def foo(): ...\ndef bar(): ...",
        )
        assert len(auditor.records) == 1
        r = auditor.records[0]
        assert r.agent_name == "explorer"
        assert r.tool_name == "read_file"
        assert r.action == CompressionAction.SKELETON
        assert r.compression_ratio < 1.0
        assert r.had_function_signatures is True

    def test_record_detects_semantic_features(self):
        auditor = CompressionAuditor()
        auditor.record(
            agent_name="coder",
            tool_name="write_file",
            action=CompressionAction.DIFF_RETAINED,
            original="[DIFF:edit]\n@@ -1,3 +1,3 @@\n-old\n+new\n\nimport os\nfrom pathlib import Path",
            compressed="[DIFF:edit]\n@@ -1,3 +1,3 @@\n-old\n+new",
        )
        r = auditor.records[0]
        assert r.had_diff_content is True
        assert r.had_imports is True

    def test_record_detects_errors(self):
        auditor = CompressionAuditor()
        auditor.record(
            agent_name="executor",
            tool_name="bash",
            action=CompressionAction.TRUNCATED,
            original="pytest output...\nFAILED test_user.py::test_login - AssertionError: expected True got False\nTraceback (most recent call last):\n  ...",
            compressed="pytest output...\nFAILED",
        )
        r = auditor.records[0]
        assert r.had_error_messages is True

    def test_record_detects_file_paths(self):
        auditor = CompressionAuditor()
        auditor.record(
            agent_name="explorer",
            tool_name="list_dir",
            action=CompressionAction.KEPT_AS_IS,
            original="src/models/user.py\nsrc/services/user_service.py\ntests/test_user.py",
            compressed="src/models/user.py\nsrc/services/user_service.py\ntests/test_user.py",
        )
        r = auditor.records[0]
        assert r.had_file_paths is True

    def test_max_records_enforced(self):
        auditor = CompressionAuditor(max_records=3)
        for i in range(5):
            auditor.record("test", "tool", CompressionAction.KEPT_AS_IS, f"content{i}", f"compressed{i}")
        assert len(auditor.records) == 3
        assert auditor.records[-1].original_preview == "content4"

    def test_keyword_difference_tracking(self):
        auditor = CompressionAuditor()
        auditor.record(
            agent_name="explorer",
            tool_name="read_file",
            action=CompressionAction.SKELETON,
            original="def authenticate_user(username: str, password: str) -> bool: pass",
            compressed="def authenticate_user(username, password): ...",
        )
        r = auditor.records[0]
        # "str" and "bool" should be in dropped (lost from original)
        assert "bool" in r.dropped_keywords or "str" in r.dropped_keywords


class TestInformationLossDetector:
    """信息损失检测测试"""

    def test_kept_has_zero_loss(self):
        record = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.KEPT_AS_IS,
            original_chars=100, compressed_chars=100, compression_ratio=1.0,
            content_hash="abc",
        )
        loss = InformationLossDetector.estimate_info_loss(record)
        assert loss == 0.0

    def test_dropped_has_high_loss(self):
        record = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.DROPPED,
            original_chars=1000, compressed_chars=10, compression_ratio=0.01,
            content_hash="abc",
        )
        loss = InformationLossDetector.estimate_info_loss(record)
        assert loss >= 0.8

    def test_skeleton_has_moderate_loss(self):
        record = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.SKELETON,
            original_chars=2000, compressed_chars=200, compression_ratio=0.1,
            content_hash="abc",
        )
        loss = InformationLossDetector.estimate_info_loss(record)
        assert 0.1 <= loss <= 0.5

    def test_dropped_error_increases_loss(self):
        record = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.DROPPED,
            original_chars=1000, compressed_chars=10, compression_ratio=0.01,
            content_hash="abc", had_error_messages=True,
        )
        loss = InformationLossDetector.estimate_info_loss(record)
        record_no_err = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.DROPPED,
            original_chars=1000, compressed_chars=10, compression_ratio=0.01,
            content_hash="abc", had_error_messages=False,
        )
        loss_no_err = InformationLossDetector.estimate_info_loss(record_no_err)
        assert loss > loss_no_err, "丢弃含错误信息的消息损失应该更大"

    def test_semantic_features_reduce_loss(self):
        record_rich = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.SKELETON,
            original_chars=2000, compressed_chars=200, compression_ratio=0.1,
            content_hash="abc",
            had_function_signatures=True, had_file_paths=True, had_imports=True,
        )
        record_poor = CompressionRecord(
            timestamp=0, agent_name="t", tool_name="t", action=CompressionAction.SKELETON,
            original_chars=2000, compressed_chars=200, compression_ratio=0.1,
            content_hash="abc",
        )
        loss_rich = InformationLossDetector.estimate_info_loss(record_rich)
        loss_poor = InformationLossDetector.estimate_info_loss(record_poor)
        assert loss_rich < loss_poor, "保留更多语义特征的压缩损失应该更小"

    def test_detect_repeated_question(self):
        signals = InformationLossDetector.detect_confusion(
            "coder", 3,
            "请再提供一次 user_service.py 的完整内容，我需要重新查看"
        )
        assert len(signals) >= 1
        assert signals[0].signal_type == "repeated_question"

    def test_detect_file_not_found(self):
        signals = InformationLossDetector.detect_confusion(
            "explorer", 2,
            "我不确定 models/user.py 这个文件是否存在，似乎没有在之前的探索中找到"
        )
        assert len(signals) >= 1
        assert signals[0].signal_type == "file_not_found_ask"

    def test_detect_uncertainty(self):
        signals = InformationLossDetector.detect_confusion(
            "reviewer", 5,
            "根据现有信息无法判断这个改动是否安全，推测可能是参数化查询没有完全覆盖"
        )
        assert len(signals) >= 1
        # 可能匹配到多个模式
        types = [s.signal_type for s in signals]
        assert "uncertainty_marker" in types

    def test_no_false_positive_on_normal_output(self):
        signals = InformationLossDetector.detect_confusion(
            "coder", 1,
            "修改完成。在 user_service.py 中添加了 None 检查。[编码完成]"
        )
        assert len(signals) == 0


class TestCompressionHealth:
    """健康报告测试"""

    def test_empty_health(self):
        auditor = CompressionAuditor()
        health = auditor.get_health()
        assert health.overall_ratio == 1.0
        assert health.info_loss_risk == 0.0
        assert health.agent_confusion_count == 0

    def test_health_with_records(self):
        auditor = CompressionAuditor()
        for _ in range(5):
            auditor.record("explorer", "read_file", CompressionAction.SKELETON,
                          "def foo():\n    return 1\n" * 100, "def foo(): ...")
        for _ in range(3):
            auditor.record("coder", "write_file", CompressionAction.KEPT_AS_IS,
                          "short content", "short content")
        health = auditor.get_health()
        assert 0 < health.overall_ratio < 1.0
        assert "read_file" in health.per_tool_stats
        assert "write_file" in health.per_tool_stats
        # KEPT 的损失应该小于 SKELETON
        assert health.per_tool_stats["write_file"]["avg_loss"] < health.per_tool_stats["read_file"]["avg_loss"]

    def test_health_with_confusion(self):
        auditor = CompressionAuditor()
        auditor.confusion_signals.append(
            AgentConfusionSignal(
                agent_name="coder", turn=1, signal_type="repeated_question",
                message_snippet="请再提供文件内容", confidence=0.7,
            )
        )
        health = auditor.get_health()
        assert health.agent_confusion_count == 1

    def test_warnings_on_high_loss(self):
        auditor = CompressionAuditor()
        for _ in range(10):
            auditor.record("test", "grep", CompressionAction.DROPPED,
                          "ERROR: critical failure in the pipeline\n" * 10, "")
        health = auditor.get_health()
        assert len(health.recent_warnings) > 0

    def test_critical_drop_tracking(self):
        auditor = CompressionAuditor()
        auditor.record("executor", "bash", CompressionAction.DROPPED,
                      "pytest output... FAILED with AssertionError...", "[已丢弃]")
        health = auditor.get_health()
        assert health.dropped_critical_count >= 1


class TestAdaptivePolicy:
    """自适应策略测试"""

    def test_default_aggressiveness(self):
        assert AdaptivePolicy.get_max_chars("unknown_tool") == 1000

    def test_skip_compression_when_protected(self):
        AdaptivePolicy._tool_aggressiveness["read_file"] = 0
        assert AdaptivePolicy.should_skip_compression("read_file") is True
        assert AdaptivePolicy.get_max_chars("read_file") > 10000

    def test_aggressive_mode_tight(self):
        AdaptivePolicy._tool_aggressiveness["grep"] = 2
        assert AdaptivePolicy.get_max_chars("grep") == 300

    def test_adjust_downgrades_high_loss_tool(self):
        AdaptivePolicy._tool_aggressiveness.clear()
        AdaptivePolicy._tool_aggressiveness["read_file"] = 1

        health = CompressionHealth(
            overall_ratio=0.3, info_loss_risk=0.7, agent_confusion_count=0,
            dropped_critical_count=0,
            per_tool_stats={
                "read_file": {"count": 5, "avg_ratio": 0.1, "avg_loss": 0.8},
            },
            per_agent_stats={},
            recent_warnings=[],
        )
        AdaptivePolicy.adjust(health)
        assert AdaptivePolicy._tool_aggressiveness["read_file"] == 0, "高损失工具应降为保护模式"

    def test_adjust_expands_window_on_confusion(self):
        from code_agent.context_manager import AGENT_CONTEXT_CONFIG
        original = AGENT_CONTEXT_CONFIG["supervisor"]["max_history"]

        health = CompressionHealth(
            overall_ratio=0.3, info_loss_risk=0.3, agent_confusion_count=5,
            dropped_critical_count=0, per_tool_stats={}, per_agent_stats={},
            recent_warnings=[],
        )
        AdaptivePolicy.adjust(health)
        assert AGENT_CONTEXT_CONFIG["supervisor"]["max_history"] > original

        # 恢复
        AGENT_CONTEXT_CONFIG["supervisor"]["max_history"] = original

    def test_adjust_recovers_when_healthy(self):
        AdaptivePolicy._tool_aggressiveness["read_file"] = 0  # 保护模式

        health = CompressionHealth(
            overall_ratio=0.9, info_loss_risk=0.1, agent_confusion_count=0,
            dropped_critical_count=0,
            per_tool_stats={
                "read_file": {"count": 10, "avg_ratio": 0.9, "avg_loss": 0.1},
            },
            per_agent_stats={},
            recent_warnings=[],
        )
        AdaptivePolicy.adjust(health)
        assert AdaptivePolicy._tool_aggressiveness["read_file"] == 1, "健康时应恢复正常压缩"


class TestAuditorIntegration:
    """集成测试 — auditor 全局单例"""

    def test_singleton(self):
        a1 = get_auditor()
        a2 = get_auditor()
        assert a1 is a2

    def test_reset_clears_all(self):
        auditor = get_auditor()
        auditor.record("t", "t", CompressionAction.DROPPED, "content", "")
        auditor.record("t", "t", CompressionAction.KEPT_AS_IS, "c2", "c2")
        assert len(auditor.records) > 0
        auditor.reset()
        assert len(auditor.records) == 0
        assert len(auditor.confusion_signals) == 0
        assert len(auditor.critical_drops) == 0
