"""FastAPI Router for AI DevOps Closed-Loop Control Plane & Git/ChatOps Webhooks.

Enables platform-level automation:
  - ``POST /devops/webhooks/mr``       : GitLab / GitHub / Gitea MR/PR auto-review -> SARIF + inline comments
  - ``POST /devops/webhooks/feedback`` : Production user feedback / alert ingestion -> auto-triage & child loops
  - ``/devops/loops/*``                : Full REST lifecycle for internal developer portals
  - ``/skills/*``                      : Skill discovery & Harness validation API
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..devops.config import load_devops_config
from ..devops.engine import DevOpsEngine
from ..devops.feedback import FeedbackItem, feedback_item_from_dict
from ..devops.ledger import LedgerError, LoopNotFound
from ..devops.models import Mode, RiskTier, Stage, findings_to_mr_comments, findings_to_sarif
from ..devops.release import ReleaseError
from ..devops.reviewers import ReviewerBackendUnavailable, build_review_chain
from ..devops.vcs import count_changed_lines, diff_against
from ..skills.registry import get_skill_registry


class CreateLoopRequest(BaseModel):
    title: str
    files: list[str] = Field(default_factory=list)
    changed_lines: int = 0
    mode: str = "attended"
    tier: str | None = None
    spec_ref: str = ""
    base: str | None = None


class ReviewLoopRequest(BaseModel):
    diff_text: str | None = None
    reviewers: list[str] = Field(default_factory=lambda: ["static"])


class ReleaseLoopRequest(BaseModel):
    version: str
    dry_run: bool = True


class ApproveLoopRequest(BaseModel):
    point: str
    by: str
    note: str = ""


class MergeRequestWebhookPayload(BaseModel):
    project_id: str = ""
    mr_id: str = ""
    title: str
    diff_text: str
    files: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=lambda: ["static"])
    max_inline_comments: int = 10


class OnlineFeedbackWebhookPayload(BaseModel):
    loop_id: str
    items: list[dict[str, Any]]


def create_devops_router(workspace_root: Path | None = None) -> APIRouter:
    router = APIRouter(tags=["AI DevOps Closed Loop"])
    root = (workspace_root or Path.cwd()).resolve()

    def _eng() -> DevOpsEngine:
        return DevOpsEngine(root, load_devops_config(root))

    @router.get("/skills")
    async def list_skills(stage: str = "", query: str = "") -> dict[str, Any]:
        reg = get_skill_registry()
        if stage or query:
            items = reg.recommend(stage=stage, query=query, limit=15)
        else:
            items = reg.list_all()
        return {"count": len(items), "skills": [s.to_summary_dict() for s in items]}

    @router.get("/devops/loops")
    async def list_loops() -> list[dict[str, Any]]:
        return [lp.to_dict() for lp in _eng().ledger.list_all()]

    @router.post("/devops/loops")
    async def create_loop(req: CreateLoopRequest) -> dict[str, Any]:
        eng = _eng()
        lp = eng.new_loop(
            req.title,
            files=req.files,
            changed_lines=req.changed_lines,
            mode=Mode(req.mode),
            tier=RiskTier(req.tier) if req.tier else None,
            spec_ref=req.spec_ref,
            base=req.base,
        )
        return lp.to_dict()

    @router.get("/devops/loops/{loop_id}")
    async def get_loop(loop_id: str) -> dict[str, Any]:
        try:
            return _eng().ledger.load(loop_id).to_dict()
        except LoopNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/devops/loops/{loop_id}/build")
    async def build_loop(loop_id: str) -> dict[str, Any]:
        eng = _eng()
        try:
            lp = eng.ledger.load(loop_id)
            return eng.start_build(lp).to_dict()
        except LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/devops/loops/{loop_id}/review")
    async def review_loop(loop_id: str, req: ReviewLoopRequest) -> dict[str, Any]:
        eng = _eng()
        try:
            lp = eng.ledger.load(loop_id)
            diff = req.diff_text if req.diff_text is not None else diff_against(root, lp.base)
            bundle = eng.skills.build_context_bundle(lp.activated_skills)
            chain, _ = build_review_chain(
                req.reviewers, skill_context=bundle, checklist_rel_path=eng.cfg.skills.checklist_path
            )
            try:
                res = eng.review(lp, diff, chain)
            except ReviewerBackendUnavailable:
                res = eng.review(lp, diff, [r for r in chain if r.name != "neoagent-skills"])
            return res.to_dict()
        except LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/devops/loops/{loop_id}/pipeline")
    async def pipeline_loop(loop_id: str) -> dict[str, Any]:
        eng = _eng()
        try:
            lp = eng.ledger.load(loop_id)
            return eng.run_pipeline(lp).to_dict()
        except LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/devops/loops/{loop_id}/release")
    async def release_loop(loop_id: str, req: ReleaseLoopRequest) -> dict[str, Any]:
        eng = _eng()
        try:
            lp = eng.ledger.load(loop_id)
            return eng.release(lp, req.version, dry_run=req.dry_run).to_dict()
        except (LedgerError, ReleaseError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/devops/loops/{loop_id}/approve")
    async def approve_loop(loop_id: str, req: ApproveLoopRequest) -> dict[str, Any]:
        eng = _eng()
        try:
            lp = eng.ledger.load(loop_id)
            return eng.approve(lp, req.point, req.by, req.note).to_dict()
        except (LedgerError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/devops/metrics")
    async def devops_metrics() -> dict[str, Any]:
        return _eng().metrics_snapshot()

    # ── Platform Webhooks ─────────────────────────────────────────────────────

    @router.post("/devops/webhooks/mr")
    async def mr_auto_review_webhook(payload: MergeRequestWebhookPayload) -> dict[str, Any]:
        """Auto-review an incoming GitLab/GitHub/Gitea Merge Request webhook."""
        eng = _eng()
        lines = count_changed_lines(payload.diff_text)
        lp = eng.new_loop(
            f"[MR {payload.mr_id or 'webhook'}] {payload.title}",
            files=payload.files,
            changed_lines=lines,
            diff_text=payload.diff_text,
            mode=Mode.ATTENDED,
            origin="webhook",
            evidence={"project_id": payload.project_id, "mr_id": payload.mr_id},
        )
        eng.start_build(lp)
        bundle = eng.skills.build_context_bundle(lp.activated_skills)
        chain, _ = build_review_chain(
            payload.reviewers, skill_context=bundle, checklist_rel_path=eng.cfg.skills.checklist_path
        )
        try:
            res = eng.review(lp, payload.diff_text, chain)
        except ReviewerBackendUnavailable:
            res = eng.review(lp, payload.diff_text, [r for r in chain if r.name != "neoagent-skills"])

        return {
            "loop_id": lp.id,
            "risk_tier": lp.tier.value,
            "activated_skills": lp.activated_skills,
            "verdict": res.to_dict(),
            "inline_comments": findings_to_mr_comments(lp.findings, max_comments=payload.max_inline_comments),
            "sarif": findings_to_sarif(lp.findings),
        }

    @router.post("/devops/webhooks/feedback")
    async def online_feedback_webhook(payload: OnlineFeedbackWebhookPayload) -> dict[str, Any]:
        """Ingest live user tickets / ChatOps messages / alerts into an OBSERVE loop."""
        eng = _eng()
        try:
            lp = eng.ledger.load(payload.loop_id)
            if lp.stage not in (Stage.OBSERVE, Stage.ROLLED_BACK):
                raise HTTPException(
                    status_code=400,
                    detail=f"Loop {lp.id} is in stage '{lp.stage.value}', expected observe/rolled_back",
                )
            items: list[FeedbackItem] = [
                it
                for idx, raw in enumerate(payload.items)
                if (it := feedback_item_from_dict(raw, idx)) is not None
            ]
            return eng.ingest_feedback(lp, items).to_dict()
        except LoopNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
