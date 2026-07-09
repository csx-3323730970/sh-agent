"""模型工厂 — 支持按 Agent 角色分层选择模型

多 Agent 架构的核心经济优势:
- Supervisor → 廉价模型 (deepseek-chat)，只做路由决策
- Explorer   → 廉价模型，搜索代码不需要强推理
- Coder      → 强模型 (deepseek-reasoner / claude-sonnet)，写代码需要深度推理
- Reviewer   → 不同于 Coder 的模型，实现真正的独立审查
- Executor   → 廉价模型，运行测试不需要强推理

单 Agent 架构的软肋: 所有推理用同一个模型，成本 = 最贵模型 × 所有推理，
或者质量 = 最便宜模型 × 所有推理。无法按任务难度区分。
"""
from threading import Lock
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from code_agent.config import get_setting, get_env

load_dotenv()

# ── 默认模型（向后兼容） ──
_default_model = None
_default_lock = Lock()

# ── 按 Agent 的模型缓存 ──
_agent_models: dict[str, ChatOpenAI] = {}
_agent_lock = Lock()


def get_chat_model() -> ChatOpenAI:
    """获取默认模型（向后兼容）"""
    global _default_model
    if _default_model is None:
        with _default_lock:
            if _default_model is None:
                _default_model = _build_model("default")
    return _default_model


def get_agent_model(agent_name: str) -> ChatOpenAI:
    """按 Agent 角色获取专属模型

    agent_name: "supervisor" | "explorer" | "coder" | "reviewer" | "executor"

    优先级: agent_models.<agent> > model.* (全局默认)
    """
    if agent_name not in _agent_models:
        with _agent_lock:
            if agent_name not in _agent_models:
                _agent_models[agent_name] = _build_model(agent_name)
    return _agent_models[agent_name]


def _build_model(agent_name: str) -> ChatOpenAI:
    """构建模型实例 — 优先读取 agent 专属配置，fallback 到全局配置"""
    if agent_name != "default":
        agent_model = _get_agent_model_name(agent_name)
        agent_api_key_env = _get_agent_api_key_env(agent_name)
        agent_base_url_env = _get_agent_base_url_env(agent_name)
        agent_temp = _get_agent_temperature(agent_name)
    else:
        agent_model = None
        agent_api_key_env = None
        agent_base_url_env = None
        agent_temp = None

    return ChatOpenAI(
        api_key=get_env(agent_api_key_env or get_setting("model", "api_key_env")),
        base_url=get_env(agent_base_url_env or get_setting("model", "base_url_env")),
        model=agent_model or get_setting("model", "chat_model"),
        temperature=(
            agent_temp if agent_temp is not None
            else get_setting("agent", "model_temperature")
        ),
        streaming=True,
    )


def _get_agent_model_name(agent_name: str) -> str | None:
    """读取 agent 专属模型名"""
    try:
        return get_setting("agent_models", agent_name, "model")
    except (KeyError, TypeError):
        return None


def _get_agent_api_key_env(agent_name: str) -> str | None:
    """读取 agent 专属 API key 环境变量名"""
    try:
        return get_setting("agent_models", agent_name, "api_key_env")
    except (KeyError, TypeError):
        return None


def _get_agent_base_url_env(agent_name: str) -> str | None:
    """读取 agent 专属 base URL 环境变量名"""
    try:
        return get_setting("agent_models", agent_name, "base_url_env")
    except (KeyError, TypeError):
        return None


def _get_agent_temperature(agent_name: str) -> float | None:
    """读取 agent 专属 temperature"""
    try:
        return float(get_setting("agent_models", agent_name, "temperature"))
    except (KeyError, TypeError, ValueError):
        return None


def verify_cross_model_review() -> dict:
    """验证 Reviewer 和 Coder 是否使用不同模型 — 跨模型审查的核心保障

    同一模型审查自己的代码有盲区（模型倾向于认可自己的输出逻辑），
    只有不同模型做的审查才是真正的独立验证。
    """
    coder_model = _build_model("coder")
    reviewer_model = _build_model("reviewer")

    coder_id = f"{coder_model.model_name}@{coder_model.openai_api_base or 'default'}"
    reviewer_id = f"{reviewer_model.model_name}@{reviewer_model.openai_api_base or 'default'}"

    same = coder_id == reviewer_id
    return {
        "cross_model": not same,
        "coder_model": coder_id,
        "reviewer_model": reviewer_id,
        "warning": None if not same else (
            "⚠ Reviewer 和 Coder 使用相同模型。同一模型审查自己的代码存在盲区。"
            "建议在 settings.yml 中为 reviewer 配置不同的模型或供应商。"
        ),
    }


def clear_model_cache():

    """清除模型缓存 — 配置变更后调用"""
    global _default_model, _agent_models
    _default_model = None
    _agent_models.clear()
