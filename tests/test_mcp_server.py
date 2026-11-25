"""
Tests for JSON-RPC 2.0 MCP Server (`neoagent/mcp/server.py`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from neoagent.mcp.server import MCPDevOpsServer


@pytest.fixture
def mcp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "mcp_proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True, capture_output=True)
    (repo / "main.py").write_text("def greet() -> str:\n    return 'hi'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


class TestMCPDevOpsServer:
    def test_initialize_list_tools_call_gates_and_read_resources(self, mcp_repo: Path):
        srv = MCPDevOpsServer(workspace_root=mcp_repo)

        # 1. initialize
        init_res = srv.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert init_res is not None
        assert init_res["result"]["serverInfo"]["name"] == "neoagent-devops-mcp"

        # 2. tools/list
        list_res = srv.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        assert list_res is not None
        tool_names = {t["name"] for t in list_res["result"]["tools"]}
        assert {
            "devops_new_loop",
            "devops_review",
            "devops_pipeline",
            "devops_release",
            "devops_metrics",
            "skills_recommend",
        }.issubset(tool_names)

        # 3. Create a clean change and run devops_new_loop -> devops_build -> devops_review via MCP
        (mcp_repo / "main.py").write_text(
            "def greet(name: str = 'world') -> str:\n    return f'hi {name}'\n",
            encoding="utf-8",
        )
        new_res = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "devops_new_loop",
                    "arguments": {
                        "title": "Parameterize greet",
                        "files": ["main.py"],
                        "mode": "unattended",
                        "tier": "low",
                    },
                },
            }
        )
        assert new_res is not None
        loop_payload = json.loads(new_res["result"]["content"][0]["text"])
        loop_id = loop_payload["id"]

        srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "devops_build", "arguments": {"loop_id": loop_id}},
            }
        )
        rev_res = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "devops_review",
                    "arguments": {"loop_id": loop_id, "reviewers": "static"},
                },
            }
        )
        assert rev_res is not None
        rev_payload = json.loads(rev_res["result"]["content"][0]["text"])
        assert rev_payload["code"] == 0
        assert rev_payload["gate"]["passed"] is True

        # 4. Read MCP resource `neoagent://devops/metrics`
        res_read = srv.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": "neoagent://devops/metrics"},
            }
        )
        assert res_read is not None
        metrics_md = res_read["result"]["contents"][0]["text"]
        assert "VERIFIED (SHA-256 intact)" in metrics_md
