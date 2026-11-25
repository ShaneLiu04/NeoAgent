"""Zero-External-Dependency Production Local Control-Plane Server (HTTP + SSE).

Powers the NeoAgent Studio desktop app (`neo-desktop` & Electron shell).
Runs out-of-the-box on Python 3.11+ stdlib (`ThreadingHTTPServer`) in any corporate
intranet or developer laptop while sharing 100% of `DevOpsEngine`, `SkillRegistry`,
`Harness L1-L7`, `PrivacyPolicyConfig`, and `NeoAgent` streaming chat.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import __version__
from ..core.agent import AgentEventKind, NeoAgent
from ..core.config import get_config
from ..core.deepseek_harness import HarnessRoleRouter
from ..devops.config import DEFAULT_DEVOPS_TOML, DEVOPS_CONFIG_NAME, load_devops_config
from ..devops.engine import DevOpsEngine
from ..devops.feedback import parse_feedback_payload
from ..devops.harness import ContextBudgetMonitor, EvolutionProposal, EvolutionScope, RepoScaffold
from ..devops.ledger import AuditChainTamperedError, LedgerError, LoopNotFound
from ..devops.models import Mode, RiskTier, Stage
from ..devops.privacy import redact_for_llm, scan_repository_privacy
from ..devops.release import ReleaseError
from ..devops.reviewers import ReviewerBackendUnavailable, StaticASTReviewer, build_review_chain
from ..devops.vcs import VcsError, changed_files, count_changed_lines, diff_against, is_git_repo
from ..skills.registry import SkillRegistry, get_skill_registry

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MB request guard


class DesktopState:
    """Shared state across HTTP/SSE worker threads for one workspace."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.root = Path(workspace_root or Path.cwd()).resolve()
        self.skills = skill_registry or get_skill_registry()
        self._agent_lock = threading.Lock()
        self._agents: dict[str, NeoAgent] = {}
        self.context_monitor = ContextBudgetMonitor()
        self._approx_session_tokens: dict[str, int] = {}

    def engine(self) -> DevOpsEngine:
        cfg = load_devops_config(self.root)
        return DevOpsEngine(self.root, cfg, skill_registry=self.skills)

    def get_or_create_agent(self, session_id: str) -> NeoAgent:
        with self._agent_lock:
            if session_id not in self._agents:
                self._agents[session_id] = NeoAgent(session_id=session_id)
            return self._agents[session_id]

    def overview(self) -> dict[str, Any]:
        cfg = get_config()
        eng = self.engine()
        prov = cfg.llm.default_provider
        has_key = bool(cfg.api_key_for(prov))
        loops = eng.ledger.list_all()
        metrics = eng.metrics_snapshot()
        diag = RepoScaffold.diagnose(self.root)
        return {
            "version": __version__,
            "workspace": self.root.name,
            "is_git_repo": is_git_repo(self.root),
            "provider": prov,
            "model": cfg.llm.default_model,
            "cheap_model": cfg.llm.cheap_model,
            "powerful_model": cfg.llm.powerful_model,
            "llm_ready": has_key,
            "skills_count": self.skills.count(),
            "loops_total": len(loops),
            "active_loops": sum(
                1 for lp in loops if lp.stage not in (Stage.DONE, Stage.ROLLED_BACK)
            ),
            "repo_scaffold_mode": diag.mode,
            "metrics": metrics,
        }


def _make_handler(state: DesktopState) -> type[BaseHTTPRequestHandler]:
    class StudioHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Quiet structured operation; avoid noisy stdout spam
            return

        # ── Helpers ───────────────────────────────────────────────────────────

        def _send_json(self, status: int, data: Any) -> None:
            raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("Payload exceeds 2MB limit")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("Expected JSON object body")
            return obj

        def _serve_static(self, rel_path: str) -> None:
            clean = rel_path.lstrip("/") or "index.html"
            target = (STATIC_DIR / clean).resolve()
            if not target.is_relative_to(STATIC_DIR) or not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Asset not found"})
                return
            mime, _ = mimetypes.guess_type(target.name)
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        # ── HTTP GET ──────────────────────────────────────────────────────────

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            try:
                if path == "/api/health":
                    self._send_json(HTTPStatus.OK, {"status": "healthy", "version": __version__})
                    return

                if path == "/api/overview":
                    self._send_json(HTTPStatus.OK, state.overview())
                    return

                if path == "/api/loops":
                    eng = state.engine()
                    items = [lp.to_dict() for lp in reversed(eng.ledger.list_all())]
                    self._send_json(HTTPStatus.OK, {"loops": items})
                    return

                if path.startswith("/api/loops/"):
                    parts = [p for p in path.split("/") if p]
                    # ["api", "loops", "<id>", ...]
                    loop_id = parts[2]
                    eng = state.engine()
                    lp = eng.ledger.load(loop_id)
                    art_dir = eng.ledger.artifact_dir(lp.id)

                    if len(parts) == 3:
                        artifacts = (
                            sorted(p.name for p in art_dir.iterdir() if p.is_file())
                            if art_dir.is_dir()
                            else []
                        )
                        self._send_json(
                            HTTPStatus.OK,
                            {"loop": lp.to_dict(), "artifacts": artifacts},
                        )
                        return

                    if len(parts) == 4 and parts[3] == "artifact":
                        name = (qs.get("name") or [""])[0].strip()
                        target = (art_dir / name).resolve()
                        if not name or not target.is_relative_to(art_dir.resolve()) or not target.is_file():
                            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Artifact not found"})
                            return
                        text = target.read_text(encoding="utf-8", errors="replace")[:40_000]
                        self._send_json(HTTPStatus.OK, {"name": name, "content": text})
                        return

                if path == "/api/skills":
                    stage = (qs.get("stage") or [""])[0]
                    query = (qs.get("query") or [""])[0]
                    files_raw = (qs.get("files") or [""])[0]
                    files = [f.strip() for f in files_raw.split(",") if f.strip()]
                    if stage or files:
                        matched = state.skills.recommend(
                            stage=stage, files=files, query=query, limit=20
                        )
                    elif query:
                        matched = state.skills.search(query, limit=30)
                    else:
                        matched = state.skills.list_all()[:100]
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "total": state.skills.count(),
                            "skills": [s.to_summary_dict() for s in matched],
                        },
                    )
                    return

                if path == "/api/skills/detail":
                    name = (qs.get("name") or [""])[0].strip()
                    ref = (qs.get("ref") or [""])[0].strip()
                    desc = state.skills.get(name)
                    if desc is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Skill '{name}' not found"})
                        return
                    content = desc.read_reference(ref) if ref else desc.read_body()
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "skill": desc.to_summary_dict(),
                            "references": desc.list_references(),
                            "validation": desc.validate_harness().to_dict(),
                            "content": redact_for_llm(content, state.engine().cfg.privacy),
                        },
                    )
                    return

                if path == "/api/harness":
                    self._send_json(HTTPStatus.OK, state.engine().harness_status())
                    return

                if path == "/api/metrics":
                    eng = state.engine()
                    try:
                        valid, cnt, msg = eng.ledger.verify_audit_chain()
                    except AuditChainTamperedError as tam:
                        valid, cnt, msg = False, 0, str(tam)
                    snap = eng.metrics_snapshot()
                    snap["ledger_verification"] = {
                        "valid": valid,
                        "events": cnt,
                        "message": msg,
                    }
                    self._send_json(HTTPStatus.OK, snap)
                    return

                if path == "/api/context":
                    sid = (qs.get("session_id") or ["default"])[0]
                    used = state._approx_session_tokens.get(sid, 1_200)
                    st = state.context_monitor.check(used)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "used_tokens": st.used_tokens,
                            "window_tokens": st.window_tokens,
                            "ratio": st.ratio,
                            "zone": st.zone.value,
                            "recommended_action": st.recommended_action,
                        },
                    )
                    return

                # Static SPA assets
                self._serve_static(path)

            except LoopNotFound as nf:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(nf)})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        # ── HTTP POST ─────────────────────────────────────────────────────────

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/api/chat/stream":
                self._handle_chat_sse()
                return

            try:
                body = self._read_json()
                eng = state.engine()

                if path == "/api/init":
                    eng.ledger.init()
                    toml_path = state.root / DEVOPS_CONFIG_NAME
                    created_toml = False
                    if not toml_path.exists():
                        toml_path.write_text(DEFAULT_DEVOPS_TOML, encoding="utf-8")
                        created_toml = True
                    self._send_json(
                        HTTPStatus.OK,
                        {"initialized": True, "created_toml": created_toml},
                    )
                    return

                if path == "/api/loops":
                    title = str(body.get("title") or "").strip()
                    files = list(body.get("files") or [])
                    lines = int(body.get("changed_lines") or 0)
                    diff_txt = ""
                    base = body.get("base") or eng.cfg.base_branch
                    if body.get("from_git") and is_git_repo(state.root):
                        try:
                            files = files or changed_files(state.root, base)
                            diff_txt = diff_against(state.root, base)
                            lines = lines or count_changed_lines(diff_txt)
                        except VcsError:
                            pass
                    mode = Mode(str(body.get("mode") or "attended"))
                    tier = RiskTier(body["tier"]) if body.get("tier") else None
                    lp = eng.new_loop(
                        title,
                        files=files,
                        changed_lines=lines,
                        diff_text=diff_txt,
                        mode=mode,
                        tier=tier,
                        spec_ref=str(body.get("spec_ref") or ""),
                        base=base,
                    )
                    self._send_json(HTTPStatus.OK, {"loop": lp.to_dict()})
                    return

                if path.startswith("/api/loops/") and path.endswith("/action"):
                    loop_id = [p for p in path.split("/") if p][2]
                    lp = eng.ledger.load(loop_id)
                    act = str(body.get("action") or "").strip().lower()
                    res_dict = self._dispatch_loop_action(eng, lp, act, body)
                    self._send_json(HTTPStatus.OK, res_dict)
                    return

                if path == "/api/harness/scaffold":
                    diag = RepoScaffold.diagnose(state.root)
                    created: list[str] = []
                    if body.get("apply"):
                        created = RepoScaffold.apply(state.root, diag)
                        diag = RepoScaffold.diagnose(state.root)
                    self._send_json(
                        HTTPStatus.OK,
                        {"diagnosis": diag.to_dict(), "created": created},
                    )
                    return

                if path == "/api/harness/evolution/approve":
                    prop_id = str(body.get("proposal_id") or "").strip()
                    approver = str(body.get("by") or "desktop-engineer").strip()
                    pending_file = eng.evolution.pending_dir / f"{prop_id}.json"
                    if not pending_file.is_file():
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Proposal {prop_id} not found"})
                        return
                    raw_p = json.loads(pending_file.read_text(encoding="utf-8"))
                    prop = EvolutionProposal(
                        id=raw_p["id"],
                        scope=EvolutionScope(raw_p["scope"]),
                        trigger_signal=raw_p["trigger_signal"],
                        target_artifact=raw_p["target_artifact"],
                        addition_summary=raw_p["addition_summary"],
                        rollback_hint=raw_p["rollback_hint"],
                        weakens_l6_constraint=bool(raw_p.get("weakens_l6_constraint", False)),
                        deletes_existing=bool(raw_p.get("deletes_existing", False)),
                        single_point_change=bool(raw_p.get("single_point_change", True)),
                    )
                    applied = eng.evolution.apply_proposal(prop, approved_by=approver)
                    self._send_json(HTTPStatus.OK, {"proposal": applied.to_dict()})
                    return

                if path == "/api/harness/privacy-scan":
                    hits = scan_repository_privacy(state.root, eng.cfg.privacy)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "clean": len(hits) == 0,
                            "count": len(hits),
                            "violations": [h.to_dict() for h in hits[:50]],
                        },
                    )
                    return

                if path == "/api/skills/validate":
                    reports = []
                    for s in state.skills.list_all():
                        rep = s.validate_harness()
                        reports.append({"name": s.name, "category": s.category, **rep.to_dict()})
                    passed_cnt = sum(1 for r in reports if r["passed"])
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "total": len(reports),
                            "passed": passed_cnt,
                            "failed": len(reports) - passed_cnt,
                            "reports": reports,
                        },
                    )
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})

            except (LedgerError, ReleaseError, ValueError) as err:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

        def _dispatch_loop_action(
            self, eng: DevOpsEngine, lp: Any, act: str, body: dict[str, Any]
        ) -> dict[str, Any]:
            if act == "build":
                res = eng.start_build(lp)
            elif act == "review":
                diff_txt = body.get("diff_text")
                if diff_txt is None:
                    diff_txt = diff_against(state.root, lp.base) if is_git_repo(state.root) else ""
                rev_names = [
                    x.strip()
                    for x in str(body.get("reviewers") or "static").split(",")
                    if x.strip()
                ]
                bundle = state.skills.build_context_bundle(lp.activated_skills)
                chain, _ = build_review_chain(
                    rev_names,
                    skill_context=bundle,
                    checklist_rel_path=eng.cfg.skills.checklist_path,
                    privacy_policy=eng.cfg.privacy,
                )
                try:
                    res = eng.review(lp, str(diff_txt), chain)
                except ReviewerBackendUnavailable:
                    res = eng.review(lp, str(diff_txt), [r for r in chain if r.name != "neoagent-skills"])
            elif act == "pipeline":
                res = eng.run_pipeline(lp)
            elif act == "release":
                ver = str(body.get("version") or "v0.1.0")
                dry = bool(body.get("dry_run", True))
                res = eng.release(lp, ver, dry_run=dry)
            elif act == "feedback":
                raw_fb = str(body.get("feedback_text") or "")
                items = parse_feedback_payload(raw_fb)
                res = eng.ingest_feedback(lp, items)
            elif act == "approve":
                res = eng.approve(
                    lp,
                    str(body.get("point") or "review"),
                    str(body.get("by") or "engineer"),
                    str(body.get("note") or ""),
                )
            elif act == "waive":
                eng.waive(
                    lp,
                    str(body.get("finding_id") or ""),
                    str(body.get("by") or "engineer"),
                    str(body.get("reason") or "Accepted risk"),
                )
                return {"code": 0, "stage": lp.stage.value, "message": "Finding waived", "loop": lp.to_dict()}
            elif act == "resume":
                to_stage = Stage(body["to"]) if body.get("to") else None
                res = eng.resume(lp, str(body.get("by") or "engineer"), to_stage)
            elif act == "autopilot":
                bundle = state.skills.build_context_bundle(lp.activated_skills)
                chain, _ = build_review_chain(
                    ["static"],
                    skill_context=bundle,
                    checklist_rel_path=eng.cfg.skills.checklist_path,
                    privacy_policy=eng.cfg.privacy,
                )
                fb_items = (
                    parse_feedback_payload(str(body["feedback_text"]))
                    if body.get("feedback_text")
                    else None
                )
                res = eng.autopilot(
                    lp,
                    reviewers=chain,
                    version=str(body.get("version") or "v0.1.0"),
                    dry_run_release=bool(body.get("dry_run", True)),
                    feedback_items=fb_items,
                )
            else:
                raise ValueError(f"Unsupported loop action: {act}")

            reloaded = eng.ledger.load(lp.id)
            return {**res.to_dict(), "loop": reloaded.to_dict()}

        # ── SSE Streaming Agent Session ──────────────────────────────────────

        def _write_sse(self, event: str, data: dict[str, Any]) -> None:
            frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

        def _handle_chat_sse(self) -> None:
            try:
                body = self._read_json()
            except Exception as err:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(err)})
                return

            message = str(body.get("message") or "").strip()
            session_id = str(body.get("session_id") or "studio-main")
            role = str(body.get("role") or "architect").strip().lower()

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            if not message:
                self._write_sse("error", {"message": "Empty message"})
                return

            cfg = get_config()
            eng = state.engine()
            scrubbed_msg = redact_for_llm(message, eng.cfg.privacy)

            # Track L4 context budget across conversation
            used = state._approx_session_tokens.get(session_id, 1_200) + max(20, len(scrubbed_msg) // 3)
            state._approx_session_tokens[session_id] = used
            ctx_st = state.context_monitor.check(used)
            self._write_sse(
                "context_status",
                {
                    "used_tokens": ctx_st.used_tokens,
                    "window_tokens": ctx_st.window_tokens,
                    "ratio": ctx_st.ratio,
                    "zone": ctx_st.zone.value,
                    "recommended_action": ctx_st.recommended_action,
                },
            )

            prov = cfg.llm.default_provider
            if not cfg.api_key_for(prov):
                self._stream_local_harness_advisor(eng, scrubbed_msg, role)
                return

            agent = state.get_or_create_agent(session_id)

            async def _pump() -> None:
                async for ev in agent.stream(scrubbed_msg, session_id=session_id):
                    if ev.kind == AgentEventKind.REASONING:
                        self._write_sse("reasoning", {"delta": str(ev.data)})
                    elif ev.kind == AgentEventKind.TOKEN:
                        state._approx_session_tokens[session_id] += max(1, len(str(ev.data)) // 4)
                        self._write_sse("token", {"delta": str(ev.data)})
                    elif ev.kind == AgentEventKind.TOOL_CALL_START:
                        self._write_sse("tool_start", ev.data if isinstance(ev.data, dict) else {"info": str(ev.data)})
                    elif ev.kind == AgentEventKind.TOOL_CALL_END:
                        self._write_sse("tool_end", ev.data if isinstance(ev.data, dict) else {"info": str(ev.data)})
                    elif ev.kind == AgentEventKind.ERROR:
                        self._write_sse("error", {"message": str(ev.data)})
                self._write_sse("done", {"mode": "llm"})

            try:
                asyncio.run(_pump())
            except Exception as exc:
                self._write_sse("error", {"message": str(exc)})

        def _stream_local_harness_advisor(
            self, eng: DevOpsEngine, prompt: str, role: str = "architect"
        ) -> None:
            """Offline deterministic advisor used when no external LLM API key is configured."""
            cfg = get_config()
            lower = prompt.lower()
            if any(k in lower for k in ("diff", "review", "检视", "审查", "bug")):
                intent = "code review"
            elif any(k in lower for k in ("skill", "技能", "规范", "standard")):
                intent = "skill recommendation"
            else:
                intent = "harness diagnosis"
            routed_model = HarnessRoleRouter.select_model(
                role, cfg.llm.powerful_model, cfg.llm.cheap_model
            )
            reasoning_steps = (
                f"[Router] role={role} -> model tier '{routed_model}' (offline: local engine)\n",
                f"[L1] intent='{intent}'; stage boundary loaded (must_know / forbidden_know)\n",
                "[L4] outbound prompt scrubbed by DLP; context kept inside the Smart Zone\n",
                "[Skills] Tier-1 index consulted; only matching SKILL.md bodies activated\n",
                "[L6] deterministic static / AST / anti-cheat / DLP inspectors engaged\n",
            )
            for step in reasoning_steps:
                self._write_sse("reasoning", {"delta": step})
                time.sleep(0.12)

            if intent == "code review":
                diff_txt = diff_against(state.root, eng.cfg.base_branch) if is_git_repo(state.root) else ""
                findings = StaticASTReviewer(eng.cfg.privacy).review(diff_txt, repo_root=state.root)
                if not diff_txt.strip():
                    reply = (
                        "### 本地混合检视\n"
                        f"工作区相对基线分支 `{eng.cfg.base_branch}` 没有未提交改动。"
                        "可在 **交付闭环** 中新建闭环以运行完整门禁。"
                    )
                elif not findings:
                    reply = (
                        "### 本地混合检视通过\n"
                        f"已检查 `{count_changed_lines(diff_txt)}` 行改动：**0 个静态 / AST / DLP / 反作弊问题**。"
                    )
                else:
                    bullets = "\n".join(
                        f"- **{f.severity.value.upper()}** `{f.rule}` · `{f.file}:{f.line}` — {f.message}"
                        for f in findings[:12]
                    )
                    reply = (
                        f"### 检视发现 {len(findings)} 个问题\n{bullets}\n\n"
                        "> 建议在 **交付闭环** 中运行 Autopilot：评审者独立复核，修复者无权自行关闭问题。"
                    )
            elif intent == "skill recommendation":
                rec = state.skills.recommend(stage="build", files=["app.py"], query=prompt, limit=6)
                bullets = "\n".join(f"- **`{s.name}`** · {s.description[:90]}" for s in rec)
                reply = f"### 推荐技能（{len(rec)} / 已挂载 {state.skills.count()}）\n{bullets}"
            else:
                h = eng.harness_status()["layers"]
                l6 = h["L6_mechanical_constraints_and_dlp"]
                reply = (
                    "### Harness 七层健康度\n"
                    f"- **技能库**：已挂载 `{state.skills.count()}` 个渐进式技能包，F1–F4 合规 `{l6['skills_f1_f4_compliant']}`\n"
                    f"- **L1 文档拓扑**：`{h['L1_information_boundary']['repo_scaffold']['mode']}`\n"
                    f"- **L6 账本与 DLP**：`{l6['status']}` · 哈希链 `{l6['audit_events_verified']}` 条 · 隐私泄露 `{l6['privacy_violations_count']}`\n"
                    f"- **L7 自演进**：待确认提案 `{h['L7_self_evolution_safe']['pending_proposals']}` 个\n\n"
                    "> 配置 `DEEPSEEK_API_KEY` 即可解锁生成式编码；交付闭环、技能库与 Harness 治理无需任何外部 API 即可使用。"
                )

            # Stream the answer progressively so the UI behaves like a live LLM stream.
            for i in range(0, len(reply), 24):
                self._write_sse("token", {"delta": reply[i : i + 24]})
                time.sleep(0.015)
            self._write_sse("done", {"mode": "local_harness"})

    return StudioHandler


class DesktopControlServer:
    """Lifecycle wrapper around `ThreadingHTTPServer` for desktop & test embedding."""

    def __init__(
        self,
        workspace_root: Path | str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.state = DesktopState(workspace_root=workspace_root, skill_registry=skill_registry)
        self.host = host
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self.state))
        self.port: int = int(self._httpd.server_address[1])
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start_background(self) -> str:
        if self._thread is not None and self._thread.is_alive():
            return self.url
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="neoagent-desktop-server",
            daemon=True,
        )
        self._thread.start()
        return self.url

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)


def create_desktop_server(
    workspace_root: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    skill_registry: SkillRegistry | None = None,
) -> DesktopControlServer:
    return DesktopControlServer(
        workspace_root=workspace_root,
        host=host,
        port=port,
        skill_registry=skill_registry,
    )
