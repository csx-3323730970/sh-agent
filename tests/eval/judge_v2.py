"""评判引擎 V2 — 多维度 AST 级别评判 + 沙箱测试验证"""
from dataclasses import dataclass, field
import re
import ast
import subprocess
import tempfile
import os
from pathlib import Path


@dataclass
class JudgeResult:
    passed: bool
    score: float  # 0.0 ~ 1.0
    dimensions: dict = field(default_factory=dict)  # 各维度得分
    checks: list[dict] = field(default_factory=list)
    reason: str = ""


@dataclass
class EvalDimension:
    name: str
    score: float
    max_score: float
    details: list[str] = field(default_factory=list)


class CodeChangeGroundTruth:
    """用代码 diff 承载 ground truth"""

    def __init__(self, expected_changes: list[dict], sandbox_dir: str):
        self.expected_changes = expected_changes
        self.sandbox_dir = sandbox_dir

    def verify_diff(self, actual_diff_text: str) -> dict:
        """验证实际 diff 是否匹配 ground truth 的期望改动"""
        results = {}
        for change_spec in self.expected_changes:
            file_path = change_spec["file"]
            file_results = {}

            # 检查 must_contain_after
            if "must_contain_after" in change_spec:
                full_path = os.path.join(self.sandbox_dir, file_path)
                if os.path.exists(full_path):
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    ok = change_spec["must_contain_after"] in content
                    file_results["must_contain_after"] = {"pass": ok, "detail": change_spec["must_contain_after"]}

            # 检查 must_not_contain_after
            if "must_not_contain_after" in change_spec:
                full_path = os.path.join(self.sandbox_dir, file_path)
                if os.path.exists(full_path):
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    ok = change_spec["must_not_contain_after"] not in content
                    file_results["must_not_contain_after"] = {"pass": ok, "detail": change_spec["must_not_contain_after"]}

            # 检查 line_range
            if "line_range" in change_spec:
                in_range = self._changes_in_range(actual_diff_text, file_path, change_spec["line_range"])
                file_results["line_range"] = {"pass": in_range, "detail": str(change_spec["line_range"])}

            results[file_path] = file_results

        return results

    @staticmethod
    def _changes_in_range(diff_text: str, file_path: str, line_range: list) -> bool:
        """检查 diff 中的改动行是否在指定范围内"""
        lo, hi = line_range
        for line in diff_text.split("\n"):
            if line.startswith("@@") and file_path in diff_text:
                # 解析 @@ -old,count +new,count @@
                m = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if m:
                    start = int(m.group(1))
                    end = start + (int(m.group(2)) if m.group(2) else 1)
                    if lo <= start <= hi or lo <= end <= hi:
                        return True
        return False


class JudgeV2:
    """多维度评判引擎"""

    DIMENSIONS = {
        "explorer": [
            ("recall", "召回率", 0.4, "找到了多少需要的文件"),
            ("precision", "精确率", 0.3, "返回的文件中有多少是相关的"),
            ("dependency_awareness", "依赖感知", 0.3, "是否发现了受影响的依赖文件"),
        ],
        "coder": [
            ("functional_correctness", "功能正确性", 0.4, "改动的代码能否通过相关测试"),
            ("minimal_change", "最小改动", 0.2, "是否只改了必要的行"),
            ("style_consistency", "风格一致性", 0.2, "缩进、命名、import 风格是否与项目一致"),
            ("no_side_effects", "无副作用", 0.2, "是否破坏了无关功能"),
        ],
        "reviewer": [
            ("vuln_detection", "漏洞检出率", 0.4, "发现了多少注入的安全问题"),
            ("false_positive", "误报率", 0.3, "对正常代码的误判比例"),
            ("suggestion_quality", "修复建议质量", 0.3, "建议是否具体可执行"),
        ],
        "executor": [
            ("command_selection", "命令选择", 0.4, "是否选择了正确的测试命令"),
            ("failure_attribution", "失败归因", 0.6, "测试失败时能否正确解释原因"),
        ],
    }

    def __init__(self, case_name: str, expected: dict, sandbox_dir: str = "."):
        self.case_name = case_name
        self.expected = expected
        self.sandbox_dir = sandbox_dir
        self.ground_truth = CodeChangeGroundTruth(
            expected.get("expected_changes", []), sandbox_dir
        )

    def evaluate_explorer(self, output: str, actual_files: list[str]) -> JudgeResult:
        """评判 Explorer：召回率 + 精确率 + 依赖感知"""
        expected_files = set(self.expected.get("expected_files", []))
        actual_set = set(actual_files) if actual_files else set()
        dims = []

        # 召回率
        if expected_files:
            found = expected_files & actual_set
            recall = len(found) / len(expected_files)
        else:
            recall = 0.5
        dims.append(EvalDimension("recall", recall, 0.4,
                                  [f"期望文件: {expected_files}", f"实际找到: {actual_set}"]))

        # 精确率
        if actual_set:
            if expected_files:
                precision = len(expected_files & actual_set) / len(actual_set)
            else:
                precision = 0.5
        else:
            precision = 0.0
        dims.append(EvalDimension("precision", precision, 0.3))

        # 依赖感知
        expected_deps = set(self.expected.get("dependency_chain", []))
        if expected_deps:
            dep_aware = len(expected_deps & actual_set) / len(expected_deps)
        else:
            dep_aware = 0.5
        dims.append(EvalDimension("dependency_awareness", dep_aware, 0.3,
                                  [f"依赖链: {expected_deps}"]))

        score = sum(d.score * d.max_score for d in dims)
        all_pass = score >= 0.6

        return JudgeResult(
            passed=all_pass,
            score=score,
            dimensions={d.name: d.score for d in dims},
            reason=" | ".join(f"{d.name}={d.score:.2f}" for d in dims),
        )

    def evaluate_coder(self, output: str, actual_diff: str, sandbox_results: dict) -> JudgeResult:
        """评判 Coder：功能正确性 + 最小改动 + 风格 + 无副作用"""
        dims = []

        # 功能正确性 — 最硬指标
        behavior_tests = self.expected.get("expected_behavior", [])
        if behavior_tests:
            passed_tests = sum(
                1 for t in behavior_tests
                if sandbox_results.get(t["test_file"]) == t.get("expected_result", "passed")
            )
            func_score = passed_tests / len(behavior_tests)
        else:
            func_score = 0.5
        dims.append(EvalDimension("functional_correctness", func_score, 0.4))

        # 最小改动 — diff 越小越好（但不能漏）
        optimal = self.expected.get("optimal_diff_lines", 10)
        actual_lines = len([l for l in (actual_diff or "").split("\n") if l.startswith(("+", "-"))])
        if actual_lines > 0 and optimal > 0:
            minimal = max(0, 1.0 - abs(actual_lines - optimal) / max(optimal, 1) * 0.5)
        else:
            minimal = 0.5
        dims.append(EvalDimension("minimal_change", min(minimal, 1.0), 0.2,
                                  [f"期望改动行数: ~{optimal}, 实际: {actual_lines}"]))

        # 风格一致性 — 用 linter 跑分
        style_issues = self._count_lint_issues()
        style_score = max(0, 1.0 - style_issues * 0.1)
        dims.append(EvalDimension("style_consistency", style_score, 0.2,
                                  [f"Lint 问题数: {style_issues}"]))

        # 无副作用
        regression_tests = self.expected.get("regression_tests", [])
        if regression_tests:
            passed = sum(
                1 for t in regression_tests
                if sandbox_results.get(t["test_file"]) == t.get("expected_result", "passed")
            )
            side_effect_free = passed / len(regression_tests)
        else:
            side_effect_free = 0.5
        dims.append(EvalDimension("no_side_effects", side_effect_free, 0.2))

        score = sum(d.score * d.max_score for d in dims)
        return JudgeResult(
            passed=score >= 0.7,
            score=score,
            dimensions={d.name: d.score for d in dims},
            reason=" | ".join(f"{d.name}={d.score:.2f}" for d in dims),
        )

    def evaluate_reviewer(self, output: str) -> JudgeResult:
        """评判 Reviewer：漏洞检出率 + 误报率 + 建议质量"""
        dims = []

        # 漏洞检出
        injected_issues = self.expected.get("injected_issues", [])
        if injected_issues:
            detected = sum(1 for issue in injected_issues if issue.get("type", "") in output.lower())
            vuln_score = detected / len(injected_issues)
        else:
            vuln_score = 0.5
        dims.append(EvalDimension("vuln_detection", vuln_score, 0.4,
                                  [f"注入问题 {len(injected_issues)} 个，检出 {int(vuln_score * len(injected_issues))} 个"]))

        # 误报率
        false_positive_indicators = ["审查不通过", "不通过", "需要修改", "安全问题"]
        has_rejection = any(ind in output for ind in false_positive_indicators)
        has_real_issues = len(injected_issues) > 0
        if has_rejection and not has_real_issues:
            fp_score = 0.0
        else:
            fp_score = 1.0
        dims.append(EvalDimension("false_positive", fp_score, 0.3))

        # 建议质量 — 是否包含具体行号或代码片段
        has_line_refs = bool(re.search(r"(第\s*\d+\s*行|line\s*\d+|L\d+)", output, re.IGNORECASE))
        has_code_snippet = "`" in output or "```" in output
        sq_score = (0.5 if has_line_refs else 0.0) + (0.5 if has_code_snippet else 0.0)
        dims.append(EvalDimension("suggestion_quality", sq_score, 0.3,
                                  [f"行号引用: {has_line_refs}, 代码片段: {has_code_snippet}"]))

        score = sum(d.score * d.max_score for d in dims)
        return JudgeResult(
            passed=score >= 0.6,
            score=score,
            dimensions={d.name: d.score for d in dims},
            reason=" | ".join(f"{d.name}={d.score:.2f}" for d in dims),
        )

    def evaluate_executor(self, output: str) -> JudgeResult:
        """评判 Executor：命令选择 + 失败归因"""
        dims = []

        expected_cmd = self.expected.get("expected_command", "")
        cmd_ok = expected_cmd.lower() in output.lower() if expected_cmd else True
        dims.append(EvalDimension("command_selection", 1.0 if cmd_ok else 0.0, 0.4,
                                  [f"期望命令: {expected_cmd}"]))

        # 失败归因 — 输出是否包含诊断信息
        has_error_analysis = bool(re.search(
            r"(错误|失败|error|fail|assert|traceback|原因|因为)",
            output, re.IGNORECASE
        ))
        has_exit_code = "退出码" in output or "exit" in output.lower()
        attr_score = (0.5 if has_error_analysis else 0.0) + (0.5 if has_exit_code else 0.0)
        dims.append(EvalDimension("failure_attribution", attr_score, 0.6))

        score = sum(d.score * d.max_score for d in dims)
        return JudgeResult(
            passed=score >= 0.5,
            score=score,
            dimensions={d.name: d.score for d in dims},
            reason=" | ".join(f"{d.name}={d.score:.2f}" for d in dims),
        )

    def _count_lint_issues(self) -> int:
        """运行 flake8 统计风格问题数（无 flake8 则返回 0）"""
        try:
            result = subprocess.run(
                ["flake8", self.sandbox_dir, "--count", "--select=E,W"],
                capture_output=True, text=True, timeout=10,
                cwd=self.sandbox_dir,
            )
            return int(result.stdout.strip() or 0)
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            return 0
