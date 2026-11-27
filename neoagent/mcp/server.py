"""MCP (Model Context Protocol) JSON-RPC 2.0 Stdio Server.

Allows modern AI coding IDEs (Cursor, Claude Code, OpenCode, Windsurf, Cline)
across the company to invoke NeoAgent's DevOps Closed-Loop Engine and Enterprise
Skill Library natively via ``mcp.json`` without any custom plugin glue.

Supported MCP methods:
  - ``initialize`` / ``notifications/initialized`` / ``ping``
  - ``tools/list`` / ``tools/call``
  - ``resources/list`` / ``resources/read``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..devops.config import load_devops_config
from ..devops.engine import DevOpsEngine
from ..devops.feedback import load_feedback_file, parse_feedback_payload
from ..devops.metrics import render_devops_metrics_markdown
from ..devops.models import Mode, RiskTier
from ..devops.reviewers import ReviewerBackendUnavailable, build_review_chain
from ..devops.vcs import diff_against
from ..skills.registry import SkillRegistry, get_skill_registry

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPDevOpsServer:
    """Stateless-per-request JSON-RPC 2.0 dispatcher backed by ``.neoagent-devops`` ledger."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.root = Path(workspace_root or Path.cwd()).resolve()
        self.skills = skill_registry or get_skill_registry()

    def _engine(self) -> DevOpsEngine:
        cfg = load_devops_config(self.root)
        return DevOpsEngine(self.root, cfg, skill_registry=self.skills)

    # ── Tool Schemas ──────────────────────────────────────────────────────────

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        return [
            {
                "name": "devops_new_loop",
                "description": "Start a new AI DevOps feature/bugfix closed loop with automatic risk classification and enterprise skill routing.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Feature or fix title"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "changed_lines": {"type": "integer", "default": 0},
                        "mode": {"type": "string", "enum": ["attended", "unattended"], "default": "attended"},
                        "tier": {"type": "string", "enum": ["low", "medium", "high"]},
                        "spec_ref": {"type": "string", "default": ""},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "devops_status",
                "description": "Inspect status, findings, and approvals of a specific Loop ID, or list all active Loops.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"loop_id": {"type": "string"}},
                },
            },
            {
                "name": "devops_build",
                "description": "Transition a Loop from INTAKE to BUILD (TDD RED->GREEN->REFACTOR) and return matched coding skills.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"loop_id": {"type": "string"}},
                    "required": ["loop_id"],
                },
            },
            {
                "name": "devops_review",
                "description": "Run 3-layer code review (Static AST + BugPatterns + optional Parallel Skill-LLM) and evaluate review gate.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "diff_text": {"type": "string", "description": "Optional raw unified diff; defaults to git diff"},
                        "reviewers": {"type": "string", "default": "static"},
                    },
                    "required": ["loop_id"],
                },
            },
            {
                "name": "devops_pipeline",
                "description": "Execute configured CI build/lint/test/coverage steps with 3-way failure triage.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"loop_id": {"type": "string"}},
                    "required": ["loop_id"],
                },
            },
            {
                "name": "devops_release",
                "description": "Execute progressive canary release (5%->25%->100%) with SLO health evaluation and automatic rollback.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "version": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True},
                    },
                    "required": ["loop_id", "version"],
                },
            },
            {
                "name": "devops_feedback",
                "description": "Triage post-release user feedback or alerts into HOTFIX / NEXT_ITERATION / DOCS / BACKLOG and spawn child Loops.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "feedback_jsonl": {"type": "string", "description": "Inline JSONL/text feedback lines"},
                        "feedback_file": {"type": "string", "description": "Path to feedback file"},
                    },
                    "required": ["loop_id"],
                },
            },
            {
                "name": "devops_approve",
                "description": "Record human sign-off for a review, release, or full_rollout checkpoint.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "point": {"type": "string", "enum": ["review", "release", "full_rollout"]},
                        "by": {"type": "string"},
                        "note": {"type": "string", "default": ""},
                    },
                    "required": ["loop_id", "point", "by"],
                },
            },
            {
                "name": "devops_metrics",
                "description": "Compute DORA 4 metrics, AI self-heal/convergence metrics, and verify SHA-256 ledger integrity.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "skills_recommend",
                "description": "Recommend enterprise SKILL.md workflows for a given DevOps stage, file list, and task query.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "stage": {"type": "string", "default": ""},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "query": {"type": "string", "default": ""},
                    },
                },
            },
            {
                "name": "skills_activate",
                "description": "Load the full SKILL.md guide or a specific reference rubric from the mounted Skill library.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "reference_path": {"type": "string", "default": ""},
                    },
                    "required": ["skill_name"],
                },
            },
            {
                "name": "devops_harness_audit",
                "description": "Inspect full 7-Layer Harness (L1-L7) health, SHA-256 ledger integrity, F1-F4 skill compliance, and DLP privacy scan.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "devops_scaffold",
                "description": "Diagnose or materialize the 4-layer AI-native repository documentation layout (AGENTS.md -> docs/AI-Map.md -> specs/changes/architecture/knowledges).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "apply": {"type": "boolean", "default": False},
                    },
                },
            },
            {
                "name": "devops_blueprint",
                "description": "Synthesize Spec-to-Code Blueprint (`spec.md`, `design.md`, `tasks.md`, `traceability.md`) with EARS requirements, 6-part interface contracts, canonical `TC-*` test cases, and DAG task plan.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "requirements": {"type": "array", "items": {"type": "string"}},
                        "requested_lite": {"type": "boolean", "default": False},
                        "selected_option": {"type": "string", "default": "A"},
                    },
                    "required": ["loop_id"],
                },
            },
            {
                "name": "devops_phase_gate",
                "description": "Run independent read-only Blueprint Phase Gate (`spec`, `design`, `plan`, or `sdc` 3D compliance).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "loop_id": {"type": "string"},
                        "phase": {"type": "string", "enum": ["spec", "design", "plan", "sdc"]},
                        "force_by": {"type": "string", "default": ""},
                    },
                    "required": ["loop_id", "phase"],
                },
            },
            {
                "name": "standards_compile",
                "description": "Compile the 3-Layer Coding Standards Quality Stack (5D Clean-Code Floor + 7-Rung Minimal-Code Ladder + Language Guardrails) within the <40% context budget.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string"}},
                        "stage": {"type": "string", "default": "build"},
                    },
                },
            },
        ]

    @staticmethod
    def resource_definitions() -> list[dict[str, Any]]:
        return [
            {
                "uri": "neoagent://devops/metrics",
                "name": "AI DevOps DORA & Autonomy Metrics",
                "mimeType": "text/markdown",
            },
            {
                "uri": "neoagent://devops/loops",
                "name": "Active DevOps Closed Loops",
                "mimeType": "application/json",
            },
            {
                "uri": "neoagent://skills/catalog",
                "name": "Enterprise Skill Catalog",
                "mimeType": "text/markdown",
            },
        ]

    # ── JSON-RPC Request Dispatcher ───────────────────────────────────────────

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params") or {}

        # Notifications have no `id` and expect no response frame
        if req_id is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "neoagent-devops-mcp", "version": "3.0.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_definitions()}
            elif method == "tools/call":
                result = self._call_tool(str(params.get("name", "")), params.get("arguments") or {})
            elif method == "resources/list":
                result = {"resources": self.resource_definitions()}
            elif method == "resources/read":
                result = self._read_resource(str(params.get("uri", "")))
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        eng = self._engine()

        if name == "devops_new_loop":
            lp = eng.new_loop(
                str(args["title"]),
                files=list(args.get("files") or []),
                changed_lines=int(args.get("changed_lines") or 0),
                mode=Mode(args.get("mode", "attended")),
                tier=RiskTier(args["tier"]) if args.get("tier") else None,
                spec_ref=str(args.get("spec_ref", "")),
            )
            return self._ok_json(lp.to_dict())

        if name == "devops_status":
            lid = args.get("loop_id")
            if lid:
                return self._ok_json(eng.ledger.load(str(lid)).to_dict())
            return self._ok_json([lp.to_dict() for lp in eng.ledger.list_all()])

        if name == "devops_build":
            lp = eng.ledger.load(str(args["loop_id"]))
            return self._ok_json(eng.start_build(lp).to_dict())

        if name == "devops_review":
            lp = eng.ledger.load(str(args["loop_id"]))
            diff = args.get("diff_text")
            if diff is None:
                diff = diff_against(self.root, lp.base)
            rev_names = [x.strip() for x in str(args.get("reviewers", "static")).split(",") if x.strip()]
            bundle = self.skills.build_context_bundle(lp.activated_skills)
            chain, _ = build_review_chain(
                rev_names,
                skill_context=bundle,
                checklist_rel_path=eng.cfg.skills.checklist_path,
            )
            try:
                res = eng.review(lp, str(diff), chain)
            except ReviewerBackendUnavailable:
                res = eng.review(lp, str(diff), [r for r in chain if r.name != "neoagent-skills"])
            return self._ok_json(res.to_dict())

        if name == "devops_pipeline":
            lp = eng.ledger.load(str(args["loop_id"]))
            return self._ok_json(eng.run_pipeline(lp).to_dict())

        if name == "devops_release":
            lp = eng.ledger.load(str(args["loop_id"]))
            res = eng.release(
                lp,
                str(args["version"]),
                dry_run=bool(args.get("dry_run", True)),
            )
            return self._ok_json(res.to_dict())

        if name == "devops_feedback":
            lp = eng.ledger.load(str(args["loop_id"]))
            if args.get("feedback_file"):
                items = load_feedback_file(Path(str(args["feedback_file"])))
            else:
                items = parse_feedback_payload(str(args.get("feedback_jsonl", "")))
            return self._ok_json(eng.ingest_feedback(lp, items).to_dict())

        if name == "devops_approve":
            lp = eng.ledger.load(str(args["loop_id"]))
            res = eng.approve(
                lp,
                str(args["point"]),
                str(args["by"]),
                str(args.get("note", "")),
            )
            return self._ok_json(res.to_dict())

        if name == "devops_metrics":
            return self._ok_json(eng.metrics_snapshot())

        if name == "skills_recommend":
            matched = self.skills.recommend(
                stage=str(args.get("stage", "")),
                files=list(args.get("files") or []),
                query=str(args.get("query", "")),
                limit=8,
            )
            return self._ok_json([m.to_summary_dict() for m in matched])

        if name == "skills_activate":
            from ..devops.privacy import redact_for_llm

            desc = self.skills.get(str(args["skill_name"]))
            if desc is None:
                return self._err_text(f"Skill not found: {args['skill_name']}")
            ref = str(args.get("reference_path", "")).strip()
            text = desc.read_reference(ref) if ref else desc.read_body()
            return {
                "content": [{"type": "text", "text": redact_for_llm(text, eng.cfg.privacy)}],
                "isError": False,
            }

        if name == "devops_harness_audit":
            return self._ok_json(eng.harness_status())

        if name == "devops_scaffold":
            from ..devops.harness import RepoScaffold

            diag = RepoScaffold.diagnose(self.root)
            created = RepoScaffold.apply(self.root, diag) if bool(args.get("apply")) else []
            if created:
                diag = RepoScaffold.diagnose(self.root)
            return self._ok_json({"diagnosis": diag.to_dict(), "created": created})

        if name == "devops_blueprint":
            lp = eng.ledger.load(str(args["loop_id"]))
            res = eng.blueprint(
                lp,
                requirements=list(args.get("requirements") or []) or None,
                requested_lite=bool(args.get("requested_lite", False)),
                selected_option=str(args.get("selected_option", "A")),
            )
            return self._ok_json(res.to_dict())

        if name == "devops_phase_gate":
            lp = eng.ledger.load(str(args["loop_id"]))
            res = eng.run_phase_gate(
                lp,
                str(args["phase"]),
                force_by=str(args.get("force_by", "")),
            )
            return self._ok_json(res.to_dict())

        if name == "standards_compile":
            qstack = eng.compile_quality_stack(
                files=list(args.get("files") or []),
                stage=str(args.get("stage", "build")),
            )
            return self._ok_json(qstack.to_dict())

        return self._err_text(f"Unknown tool: {name}")

    def _read_resource(self, uri: str) -> dict[str, Any]:
        eng = self._engine()
        if uri == "neoagent://devops/metrics":
            md = render_devops_metrics_markdown(eng.metrics_snapshot())
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": md}]}
        if uri == "neoagent://devops/loops":
            payload = json.dumps(
                [lp.to_dict() for lp in eng.ledger.list_all()], ensure_ascii=False, indent=2
            )
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": payload}]}
        if uri == "neoagent://skills/catalog":
            md = self.skills.catalog_markdown(max_items=100)
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": md}]}
        raise ValueError(f"Unknown MCP resource URI: {uri}")

    @staticmethod
    def _ok_json(data: Any) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2, default=str)}
            ],
            "isError": False,
        }

    @staticmethod
    def _err_text(msg: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": msg}], "isError": True}


def main(argv: list[str] | None = None) -> None:
    """Stdio JSON-RPC 2.0 loop for ``neoagent-mcp``."""
    import argparse

    parser = argparse.ArgumentParser(prog="neoagent-mcp", description="NeoAgent DevOps MCP Server")
    parser.add_argument("--root", default=".", help="Target repository root")
    parsed = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    server = MCPDevOpsServer(workspace_root=Path(parsed.root))
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        resp = server.handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    main()
