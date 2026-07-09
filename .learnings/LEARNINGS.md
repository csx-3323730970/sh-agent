# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260623-001] best_practice

**Logged**: 2026-06-23T18:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: tests

### Summary
Agent 路由决策函数必须抽取为纯函数才能做确定性单测

### Details
`_parse_decision` 原本接收整个 `CodingState` dict 作为参数，测试时必须构造完整 state。重构为 `parse_decision(text, exploration_result, review_feedback, review_approved, test_result, retry_count, max_retries)` 纯函数后，每个参数独立传入，测试可直接用参数化覆盖所有状态组合，覆盖率达到 100%。

### Suggested Action
所有 Agent 节点的核心决策逻辑都应遵循此模式：纯函数接收标量参数 → 单测验证 → 节点函数仅做适配层。

### Metadata
- Source: conversation
- Related Files: code_agent/agents/supervisor.py, tests/unit/test_routing.py
- Tags: agent-testing, pure-functions, refactoring

---

## [LRN-20260623-002] insight

**Logged**: 2026-06-23T18:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: tests

### Summary
Golden Dataset 评测框架应该用 mock 模式验证框架自身，用 live 模式验证 Agent 质量

### Details
评测框架本身也是代码，需要测试。通过 mock 模式（数据集自带 mock_output）可以在不调用 LLM 的情况下验证 Judge 评判引擎和 EvalReport 统计的正确性。这形成了「测试评测框架的测试」— 对 Agent 评测体系的信心来自于框架自身的测试覆盖。

### Suggested Action
评测框架的 Judge/Report/Runner 模块应有独立的 pytest 用例，验证结构断言、评分计算、报告聚合的正确性。

### Metadata
- Source: conversation
- Related Files: tests/eval/judge.py, tests/eval/report.py, tests/unit/test_eval_framework.py
- Tags: eval-framework, meta-testing, mock-mode

---
