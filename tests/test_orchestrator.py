"""Tests for orchestrator — sequential execution, input immutability."""

import pytest

from neoagent.core.orchestrator import AgentOrchestrator, SubAgentTask, SubAgentRole


class TestOrchestratorSequential:
    @pytest.mark.asyncio
    async def test_sequential_does_not_mutate_input_tasks(self):
        """run_sequential must not modify the original task objects."""
        orch = AgentOrchestrator()
        original_prompt = "What is 2+2?"
        task = SubAgentTask(id="t1", prompt=original_prompt, role=SubAgentRole.CUSTOM)

        # Force a failure so we don't need real LLM calls
        async def fake_run_task(task, provider):
            from neoagent.core.orchestrator import SubAgentResult
            return SubAgentResult(
                task_id=task.id, role=task.role, output="4", success=True,
            )

        orch._run_task = fake_run_task  # type: ignore[method-assign]

        await orch.run_sequential([task])
        assert task.prompt == original_prompt

    @pytest.mark.asyncio
    async def test_sequential_passes_context(self):
        orch = AgentOrchestrator()
        calls = []

        async def fake_run_task(task, provider):
            from neoagent.core.orchestrator import SubAgentResult
            calls.append(task.prompt)
            return SubAgentResult(
                task_id=task.id, role=task.role, output=task.prompt, success=True,
            )

        orch._run_task = fake_run_task  # type: ignore[method-assign]

        t1 = SubAgentTask(id="t1", prompt="first", role=SubAgentRole.CUSTOM)
        t2 = SubAgentTask(id="t2", prompt="second", role=SubAgentRole.CUSTOM)
        results = await orch.run_sequential([t1, t2])

        assert len(results) == 2
        assert results[0].output == "first"
        assert "first" in calls[1]  # second task received context from first
