"""评测运行器 V2 — 沙箱隔离 + diff 验证 + 多维度评分"""
import yaml
import tempfile
import shutil
import subprocess
import os
from pathlib import Path
from typing import Optional

from tests.eval.judge_v2 import JudgeV2, EvalDimension, JudgeResult
from tests.eval.report import EvalReport, ReportRenderer


DATASET_DIR = Path(__file__).parent / "datasets" / "v2"


class SandboxRunner:
    """在隔离目录中执行评测"""

    def __init__(self, base_workspace: str):
        self.base_workspace = base_workspace
        self.sandbox_dir: Optional[str] = None

    def setup(self, base_commit: str = "") -> str:
        """创建沙箱环境 — 复制项目到临时目录"""
        self.sandbox_dir = tempfile.mkdtemp(prefix="sh_agent_eval_")
        if base_commit:
            self._checkout_commit(base_commit)
        else:
            # 复制当前工作目录（排除 .git 以节省空间）
            for item in os.listdir(self.base_workspace):
                if item == ".git":
                    continue
                src = os.path.join(self.base_workspace, item)
                dst = os.path.join(self.sandbox_dir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                        "__pycache__", ".pytest_cache", "*.egg-info", ".git"
                    ))
                else:
                    shutil.copy2(src, dst)
        return self.sandbox_dir

    def _checkout_commit(self, commit: str):
        """从 git 历史检出特定 commit"""
        subprocess.run(
            ["git", "clone", self.base_workspace, self.sandbox_dir],
            capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", self.sandbox_dir, "checkout", commit],
            capture_output=True, timeout=10,
        )

    def run_tests(self, test_files: list[str]) -> dict:
        """在沙箱中运行指定测试文件，返回结果映射"""
        results = {}
        for test_file in test_files:
            test_path = os.path.join(self.sandbox_dir, test_file)
            if not os.path.exists(test_path):
                results[test_file] = "not_found"
                continue
            proc = subprocess.run(
                ["python", "-m", "pytest", test_path, "-v", "--tb=short"],
                capture_output=True, text=True, timeout=30,
                cwd=self.sandbox_dir,
            )
            results[test_file] = "passed" if proc.returncode == 0 else "failed"
        return results

    def cleanup(self):
        if self.sandbox_dir and os.path.exists(self.sandbox_dir):
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)


class EvalRunnerV2:
    """V2 评测运行器 — 沙箱隔离 + 多维度评判"""

    def __init__(self, base_workspace: str = ".", mock: bool = True):
        self.base_workspace = os.path.abspath(base_workspace)
        self.mock = mock
        self.renderer = ReportRenderer()

    def run_all(self, agents: list[str] | None = None) -> ReportRenderer:
        if agents is None:
            agents = ["supervisor", "explorer", "coder", "reviewer", "executor"]

        for agent_name in agents:
            dataset_path = DATASET_DIR / f"{agent_name}.yaml"
            if not dataset_path.exists():
                continue
            report = self.run_agent(agent_name, dataset_path)
            self.renderer.add_report(report)

        return self.renderer

    def run_agent(self, agent_name: str, dataset_path: Path) -> EvalReport:
        dataset = self._load_dataset(dataset_path)
        report = EvalReport(agent=agent_name)

        for case in dataset:
            case_name = case["name"]
            expected = case.get("expected", {})

            if self.mock:
                # 离线模式
                mock_output = case.get("mock_output", "")
                mock_metadata = case.get("mock_metadata", {})
                judge = JudgeV2(case_name, expected, ".")
                result = self._judge_mock(agent_name, judge, mock_output, mock_metadata)
            else:
                # 真实模式 — 使用沙箱
                sandbox = SandboxRunner(self.base_workspace)
                sandbox_dir = sandbox.setup(case.get("setup", {}).get("base_commit", ""))
                try:
                    judge = JudgeV2(case_name, expected, sandbox_dir)
                    output, metadata = self._invoke_agent(agent_name, case, sandbox_dir)
                    result = self._judge_live(agent_name, judge, output, metadata, sandbox)
                finally:
                    sandbox.cleanup()

            report.add(case_name, result)

        return report

    def _judge_mock(self, agent_name: str, judge: JudgeV2,
                    output: str, metadata: dict) -> JudgeResult:
        """离线评判（不依赖 LLM）"""
        if agent_name == "explorer":
            return judge.evaluate_explorer(output, metadata.get("files", []))
        elif agent_name == "supervisor":
            # Supervisor 用原有评判逻辑（路由决策 + 关键词）
            return self._judge_supervisor_mock(judge, output, metadata)
        elif agent_name == "reviewer":
            return judge.evaluate_reviewer(output)
        elif agent_name == "executor":
            return judge.evaluate_executor(output)
        else:
            from tests.eval.judge import Judge  # fallback to v1
            old_judge = Judge(judge.case_name, self._convert_expected(judge.expected))
            return old_judge.evaluate(output, metadata)

    def _judge_live(self, agent_name: str, judge: JudgeV2,
                    output: str, metadata: dict, sandbox: SandboxRunner) -> JudgeResult:
        if agent_name == "coder":
            test_files = [t["test_file"] for t in judge.expected.get("expected_behavior", [])]
            test_files += [t["test_file"] for t in judge.expected.get("regression_tests", [])]
            sandbox_results = sandbox.run_tests(test_files)
            return judge.evaluate_coder(output, metadata.get("diff", ""), sandbox_results)
        elif agent_name == "explorer":
            return judge.evaluate_explorer(output, metadata.get("files", []))
        elif agent_name == "reviewer":
            return judge.evaluate_reviewer(output)
        elif agent_name == "executor":
            return judge.evaluate_executor(output)
        else:
            from tests.eval.judge import Judge
            old_judge = Judge(judge.case_name, self._convert_expected(judge.expected))
            return old_judge.evaluate(output, metadata)

    def _judge_supervisor_mock(self, judge: JudgeV2, output: str, metadata: dict) -> JudgeResult:
        """对 Supervisor 的专项评判"""
        from tests.eval.judge import Judge
        expected = self._convert_expected(judge.expected)
        old_judge = Judge(judge.case_name, expected)
        old_result = old_judge.evaluate(output, metadata)
        return JudgeResult(
            passed=old_result.passed,
            score=old_result.score,
            dimensions={"routing": old_result.score},
            checks=old_result.checks,
            reason=old_result.reason,
        )

    @staticmethod
    def _convert_expected(expected: dict) -> dict:
        """将 V2 expected 格式转换回 V1 格式"""
        v1 = {}
        if "must_contain" in expected:
            v1["must_contain"] = expected["must_contain"]
        if "must_not_contain" in expected:
            v1["must_not_contain"] = expected["must_not_contain"]
        if "route_decision" in expected:
            v1["route_decision"] = expected["route_decision"]
        return v1

    def _load_dataset(self, path: Path) -> list[dict]:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or []

    def _invoke_agent(self, agent_name: str, case: dict, sandbox_dir: str) -> tuple[str, dict]:
        """真实调用 Agent 节点"""
        from code_agent.state import CodingState
        from langchain_core.messages import HumanMessage

        state: CodingState = {
            "messages": [HumanMessage(content=case.get("input", ""))],
            "user_request": case.get("input", ""),
            "workspace_dir": sandbox_dir,
            "task_plan": "",
            "current_agent": agent_name,
            "exploration_result": case.get("context", {}).get("exploration_result"),
            "relevant_files": None,
            "code_changes": None,
            "review_feedback": case.get("context", {}).get("review_feedback"),
            "review_approved": case.get("context", {}).get("review_approved", False),
            "test_result": case.get("context", {}).get("test_result"),
            "test_passed": False,
            "agent_summaries": None,
            "retry_count": case.get("context", {}).get("retry_count", 0),
            "max_retries": 3,
            "final_response": None,
            "task_complete": False,
        }

        metadata = {}

        if agent_name == "supervisor":
            from code_agent.agents.supervisor import supervisor_node, parse_decision
            result = supervisor_node(state)
            msgs = result.get("messages", [])
            output = msgs[-1].content if msgs else ""
            metadata["decision"] = parse_decision(
                output,
                exploration_result=state.get("exploration_result"),
                review_feedback=state.get("review_feedback"),
                review_approved=state.get("review_approved", False),
                test_result=state.get("test_result"),
                retry_count=state.get("retry_count", 0),
                max_retries=state.get("max_retries", 3),
            )

        elif agent_name == "explorer":
            from code_agent.agents.explorer import explorer_node
            result = explorer_node(state)
            output = result.get("exploration_result", "")
            metadata["files"] = result.get("relevant_files", [])

        elif agent_name == "coder":
            from code_agent.agents.coder import coder_node
            result = coder_node(state)
            msgs = result.get("messages", [])
            output = msgs[-1].content if msgs else ""
            changes = result.get("code_changes", [])
            metadata["files"] = [c.get("file_path", "") for c in (changes or [])]
            metadata["changes"] = changes

        elif agent_name == "reviewer":
            from code_agent.agents.reviewer import reviewer_node
            result = reviewer_node(state)
            output = result.get("review_feedback", "")
            metadata["approved"] = result.get("review_approved", False)

        elif agent_name == "executor":
            from code_agent.agents.executor import executor_node
            result = executor_node(state)
            output = result.get("test_result", "")
            metadata["passed"] = result.get("test_passed", False)

        else:
            output = ""
            metadata = {}

        return output, metadata


def run_eval_v2(mock: bool = True, agents: list[str] | None = None) -> ReportRenderer:
    runner = EvalRunnerV2(mock=mock)
    return runner.run_all(agents)
