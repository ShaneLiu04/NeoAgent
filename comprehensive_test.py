"""NeoAgent 本地部署综合功能测试"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone

# ── 配置测试 ──────────────────────────────────────

def test_config():
    print("=" * 60)
    print("[1/10] 配置系统测试")
    print("=" * 60)
    from neoagent.core.config import get_config, Config

    cfg = get_config()
    print(f"✓ 配置加载成功: provider={cfg.llm.default_provider}, model={cfg.llm.default_model}")
    print(f"✓ 工作区目录: {cfg.workspace_dir}")
    print(f"✓ office 目录: {cfg.office_dir}")
    print(f"✓ 内存目录: {cfg.memory_dir}")
    print(f"✓ 数据库路径: {cfg.db_path}")
    print(f"✓ Chroma 路径: {cfg.chroma_path}")

    key = cfg.api_key_for("openai")
    print(f"✓ API key 查询: openai={'已配置' if key else '未配置'}")

    cfg.ensure_dirs()
    assert cfg.workspace_dir.exists()
    assert cfg.office_dir.exists()
    print("✓ 工作区目录已创建")
    print()

# ── 安全模块测试 ──────────────────────────────────

def test_security():
    print("=" * 60)
    print("[2/10] 安全模块测试")
    print("=" * 60)
    from neoagent.core.security import (
        Capability, CapabilityToken, SafeEvaluator, get_safe_evaluator,
        DockerSandbox, SandboxResult
    )

    token = CapabilityToken(granted=Capability.FILESYSTEM_READ | Capability.NETWORK_OUTBOUND)
    assert token.has(Capability.FILESYSTEM_READ)
    assert not token.has(Capability.FILESYSTEM_WRITE)
    print("✓ Capability 系统: 读权限通过，写权限拒绝")

    evaler = SafeEvaluator()
    assert evaler.evaluate("2 + 2") == 4
    assert evaler.evaluate("sqrt(16) + 3 * 2") == 10.0
    assert evaler.evaluate("[1, 2, 3][0]") == 1
    assert evaler.evaluate('{"a": 5}["a"]') == 5
    assert evaler.evaluate("[1, 2, 3, 4][::2]") == [1, 3]
    print("✓ AST 安全求值: 算术、列表索引、字典查找、切片")

    for bad in ["__import__('os')", "lambda x: x", "x = 5"]:
        try:
            evaler.evaluate(bad)
            assert False, f"应当拒绝: {bad}"
        except (ValueError, SyntaxError):
            pass
    print("✓ AST 安全求值: 正确拒绝危险操作")

    available = DockerSandbox.is_available()
    print(f"✓ Docker 沙箱可用性: {'可用' if available else '不可用（降级为 process 模式）'}")
    print()

# ── 记忆系统测试 ──────────────────────────────────

def test_memory():
    print("=" * 60)
    print("[3/10] 记忆系统测试")
    print("=" * 60)
    from neoagent.core.memory import (
        TokenCounter, ShortTermMemory, UserProfile, UserPreference
    )
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

    tc = TokenCounter()
    assert tc.count("Hello, world!") > 0
    assert tc.count("") == 0
    print(f"✓ TokenCounter: 'Hello, world!' = {tc.count('Hello, world!')} tokens")

    mem = ShortTermMemory(max_turns=2, max_tokens=100_000)
    msgs = [
        SystemMessage(content="你是一个助手"),
        HumanMessage(content="问题1"),
        AIMessage(content="回答1"),
        HumanMessage(content="问题2"),
        AIMessage(content="回答2"),
        HumanMessage(content="问题3"),
        AIMessage(content="回答3"),
    ]
    kept, discarded = mem.prune(msgs)
    assert any(isinstance(m, SystemMessage) for m in kept)
    print(f"✓ 短期记忆剪枝: 保留 {len(kept)} 条, 丢弃 {len(discarded)} 条")

    profile = UserProfile(name="测试用户")
    profile.set_preference("language", "zh-CN")
    profile.add_fact("喜欢冰美式")
    profile.add_constraint("回答要简洁")
    prompt = profile.to_system_prompt()
    assert "测试用户" in prompt
    assert "zh-CN" in prompt
    assert "冰美式" in prompt
    print("✓ 用户画像: 偏好、事实、约束已结构化")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        path = f.name
    profile.save(path)
    loaded = UserProfile.load(path)
    assert loaded.name == "测试用户"
    os.unlink(path)
    print("✓ 用户画像序列化/反序列化")
    print()

# ── 提供商模块测试 ────────────────────────────────

def test_provider():
    print("=" * 60)
    print("[4/10] 提供商模块测试")
    print("=" * 60)
    from neoagent.core.provider import (
        SmartRouter, TaskComplexity, TokenBudget, ModelInfo, ProviderKind,
        get_model_info, create_budget
    )
    from langchain_core.messages import HumanMessage

    router = SmartRouter(enabled=True)
    simple = [HumanMessage(content="hello how are you")]
    complex_msg = [HumanMessage(content="implement a red-black tree in Python")]
    assert router.classify(simple) == TaskComplexity.SIMPLE
    assert router.classify(complex_msg) == TaskComplexity.COMPLEX
    print("✓ SmartRouter: 简单问候 → SIMPLE, 复杂代码 → COMPLEX")

    info = ModelInfo("test", ProviderKind.OPENAI, 4096, 0.001, 0.005)
    budget = TokenBudget(10000, 5000, 100.0, info)
    budget.check_and_reserve(500, 200)
    budget.record(1000, 500)
    assert budget.total_cost > 0
    print(f"✓ TokenBudget: 总成本 ${budget.total_cost:.4f}, 摘要: {budget.summary}")

    gpt4o = get_model_info("gpt-4o")
    assert gpt4o is not None
    assert gpt4o.supports_vision
    print(f"✓ 模型目录: gpt-4o 支持视觉, 输入成本 ${gpt4o.input_cost_per_1k}/1K")
    print()

# ── 工具系统测试 ──────────────────────────────────

def test_tools():
    print("=" * 60)
    print("[5/10] 工具系统测试")
    print("=" * 60)
    from neoagent.core.tools.builtins import (
        calculator, get_current_time, get_system_info,
    )
    from neoagent.core.tools.base import get_tool_metadata, list_tools

    result = calculator.invoke({"expression": "2 + 3"})
    assert "5" in result
    result = calculator.invoke({"expression": "sqrt(25) + 3 * 2"})
    assert "11" in result
    result = calculator.invoke({"expression": "__import__('os')"})
    assert "Error" in result or "error" in result
    print("✓ 计算器: 基础运算、复杂表达式、危险代码拒绝")

    result = get_current_time.invoke({})
    assert "Current time" in result
    print(f"✓ 时间工具: {result[:50]}...")

    result = get_system_info.invoke({})
    assert "Provider" in result
    print(f"✓ 系统信息: {result[:80]}...")

    meta = get_tool_metadata("calculator")
    assert meta is not None
    all_tools = list_tools()
    assert "calculator" in all_tools
    assert "web_search" in all_tools
    print(f"✓ 工具注册表: 共 {len(all_tools)} 个工具已注册")
    print()

# ── 可观测性测试 ──────────────────────────────────

def test_observability():
    print("=" * 60)
    print("[6/10] 可观测性测试")
    print("=" * 60)
    from neoagent.core.observability import get_observability, EventType

    obs = get_observability()
    obs.record_llm_call(
        model="gpt-4o-mini",
        input_tokens=1000,
        output_tokens=300,
        cost_usd=0.0005,
        latency_ms=800,
    )
    obs.record_tool_call("web_search", success=True, latency_ms=1200)

    metrics = obs.get_metrics()
    summary = obs.get_cost_summary()
    assert metrics.total_requests == 1
    assert metrics.total_input_tokens == 1000
    assert summary["total_cost_usd"] > 0
    print(f"✓ 指标: 请求数={metrics.total_requests}, 输入token={metrics.total_input_tokens}")
    print(f"✓ 成本: 总成本 ${summary['total_cost_usd']:.6f}, 总token={summary['total_tokens']}")

    from neoagent.core.observability import LogLevel
    obs.log(EventType.SESSION_START, session_id="test", level=LogLevel.INFO, message="测试会话开始")
    print("✓ 异步日志写入队列")
    obs.shutdown()
    print("✓ Observability 正常关闭")
    print()

# ── 调度器测试 ────────────────────────────────────

def test_scheduler():
    print("=" * 60)
    print("[7/10] 调度器测试")
    print("=" * 60)
    from neoagent.core.tools.scheduler import get_scheduler, TaskScheduler

    # 创建新的调度器实例（避免与全局单例冲突）
    scheduler = TaskScheduler(db_url="sqlite:///:memory:")
    print("✓ APScheduler 实例已创建（SQLite 内存模式）")

    events = []
    def on_trigger(task_id, description):
        events.append((task_id, description))

    scheduler.on_trigger(on_trigger)

    target = datetime.now(timezone.utc)
    scheduler.add_task("test-task-1", target, "测试任务描述")
    print("✓ 任务已注册到调度器")

    try:
        jobs = scheduler.list_tasks()
        print(f"✓ 当前调度任务数: {len(jobs)}")
    except Exception as e:
        print(f"⚠ 任务列表获取失败（调度器未启动）: {e}")

    scheduler.remove_task("test-task-1")
    print("✓ 任务已从调度器移除")
    print()

# ── 编排器测试 ────────────────────────────────────

def test_orchestrator():
    print("=" * 60)
    print("[8/10] 编排器测试")
    print("=" * 60)
    from neoagent.core.orchestrator import (
        AgentOrchestrator, OrchestratorConfig, SubAgentTask, SubAgentRole
    )

    orch = AgentOrchestrator(config=OrchestratorConfig(max_parallel=2, timeout_per_task=10))
    assert orch.config.max_parallel == 2
    print("✓ 编排器初始化: max_parallel=2")

    task = orch.create_task("测试提示", role=SubAgentRole.RESEARCHER)
    assert task.role == SubAgentRole.RESEARCHER
    assert task.prompt == "测试提示"
    print(f"✓ 任务创建: id={task.id}, role={task.role.value}")
    print()

# ── 文件操作测试 ──────────────────────────────────

def test_fileops():
    print("=" * 60)
    print("[9/10] 文件操作测试")
    print("=" * 60)
    import neoagent.core.tools.fileops as fileops_mod
    from neoagent.core.tools.fileops import _resolve_safe

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir).resolve()

        class FakeCfg:
            office_dir = base

        original = fileops_mod.get_config
        fileops_mod.get_config = lambda: FakeCfg()
        try:
            target = _resolve_safe("test.txt")
            assert target.resolve() == (base / "test.txt").resolve()
            print("✓ 路径解析: 工作区内文件通过")

            try:
                _resolve_safe("../../../etc/passwd")
                assert False, "应当拒绝"
            except PermissionError:
                print("✓ 路径穿越阻断: ../../../etc/passwd 被拒绝")

            try:
                _resolve_safe("C:/Windows/system32" if sys.platform == "win32" else "/etc/passwd")
                assert False, "应当拒绝"
            except PermissionError:
                print("✓ 绝对路径外部阻断")
        finally:
            fileops_mod.get_config = original
    print()

# ── Agent 初始化测试 ──────────────────────────────

def test_agent_init():
    print("=" * 60)
    print("[10/10] Agent 初始化测试")
    print("=" * 60)
    from unittest.mock import patch, MagicMock
    from neoagent.core.agent import NeoAgent, AgentEventKind, create_agent

    with patch("neoagent.core.agent.create_llm", return_value=MagicMock()):
        agent = NeoAgent(session_id="test-deploy", max_turns=30)
        assert agent.session_id == "test-deploy"
        assert agent.max_turns == 30
        assert agent._app is not None
        print("✓ NeoAgent 初始化: session_id=test-deploy, max_turns=30")

        assert AgentEventKind.TOKEN == "token"
        assert AgentEventKind.TOOL_CALL_START == "tool_call_start"
        assert AgentEventKind.ERROR == "error"
        print("✓ AgentEventKind 枚举: TOKEN, TOOL_CALL_START, ERROR")

        old_budget = agent._budget
        agent.reset_session()
        assert agent._budget is not old_budget
        print("✓ 预算重置: 创建了新预算实例")
    print()

# ── Server 启动测试 ───────────────────────────────

def test_server_startup():
    print("=" * 60)
    print("[附加] Server 启动测试")
    print("=" * 60)
    from neoagent.server.app import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app)

    # 健康检查
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    print(f"✓ 健康检查: {data}")

    # 会话列表
    response = client.get("/sessions")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    print("✓ 会话列表 API 正常")

    # 指标接口
    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "total_cost_usd" in data
    print(f"✓ 指标接口: 总成本=${data['total_cost_usd']}")
    print()

# ── 主入口 ────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  NeoAgent 本地部署综合功能测试")
    print("=" * 60 + "\n")

    test_config()
    test_security()
    test_memory()
    test_provider()
    test_tools()
    test_observability()
    test_scheduler()
    test_orchestrator()
    test_fileops()
    test_agent_init()
    test_server_startup()

    print("=" * 60)
    print("  🎉 全部功能测试通过！")
    print("=" * 60)
    print("\n系统已就绪，可通过以下方式使用:")
    print("  CLI  : neoagent")
    print("  Server: neoagent-server")
    print("  API  : 配置真实 API Key 后调用 agent.stream()")

if __name__ == "__main__":
    main()
