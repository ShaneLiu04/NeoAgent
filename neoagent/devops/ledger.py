"""Tamper-evident DevOps Loop Ledger & File-Handoff Artifact Manager.

Engineering highlights:
1. Atomic state writes (``.json.tmp`` + ``os.replace``) — immune to mid-write crashes.
2. Strict state-machine transition guard against ``models.TRANSITIONS``.
3. Cryptographic SHA-256 hash-chained ``events.jsonl`` audit trail — every state
   transition, review verdict, waiver, and human approval links ``prev_hash -> hash``
   so enterprise compliance/security teams can verify zero tampering.
4. File-handoff artifact directory (``task-brief.md``, ``fix-brief.md``, ``srs-draft.md``,
   ``findings.sarif.json``) keeping sub-agent contexts compact (<40% window rule).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import RESUMABLE, TRANSITIONS, LoopRecord, Mode, RiskTier, Stage

LEDGER_DIR_NAME = ".neoagent-devops"
_LOOP_ID_RE = re.compile(r"^L\d{8}-\d{3,}$")
_GENESIS_HASH = "0" * 64


class LedgerError(Exception):
    """Base error for DevOps ledger operations."""


class IllegalTransition(LedgerError):
    """Raised when an invalid stage transition is attempted."""


class LoopNotFound(LedgerError):
    """Raised when a loop ID does not exist or is malformed."""


class AuditChainTamperedError(LedgerError):
    """Raised when SHA-256 hash chain verification fails on events.jsonl."""


def _compute_record_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


class DevOpsLedger:
    def __init__(self, root: Path | str, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / LEDGER_DIR_NAME
        self.loops_dir = self.dir / "loops"
        self.artifacts_dir = self.dir / "artifacts"
        self.events_path = self.dir / "events.jsonl"
        self._now = now

    def init(self) -> None:
        self.loops_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        gi = self.dir / ".gitignore"
        if not gi.exists():
            # Keep loops/ and events.jsonl auditable in VCS; ignore bulky build logs
            gi.write_text("artifacts/\nworktrees/\n", encoding="utf-8")

    def create(
        self,
        title: str,
        tier: RiskTier = RiskTier.LOW,
        mode: Mode = Mode.ATTENDED,
        *,
        parent_id: str | None = None,
        origin: str = "engineer",
        spec_ref: str = "",
        base: str = "main",
        files: list[str] | None = None,
        activated_skills: list[str] | None = None,
    ) -> LoopRecord:
        self.init()
        clean_title = title.strip()
        if not clean_title:
            raise LedgerError("Loop title cannot be empty")

        # Hard invariant: HIGH risk changes are NEVER allowed to run unattended
        if tier == RiskTier.HIGH:
            mode = Mode.ATTENDED

        ts = self._now()
        day = time.strftime("%Y%m%d", time.localtime(ts))
        seq = 1 + sum(1 for _ in self.loops_dir.glob(f"L{day}-*.json"))
        loop = LoopRecord(
            id=f"L{day}-{seq:03d}",
            title=clean_title,
            tier=tier,
            mode=mode,
            created_at=ts,
            updated_at=ts,
            parent_id=parent_id,
            origin=origin,
            spec_ref=spec_ref,
            base=base,
            files=list(files or []),
            activated_skills=list(activated_skills or []),
        )
        loop.history.append(
            {
                "at": ts,
                "from": None,
                "to": Stage.INTAKE.value,
                "reason": f"created ({origin})",
                "actor": "neoagent-devops",
            }
        )
        self.save(loop)
        self.log_event(
            loop,
            "created",
            tier=tier.value,
            mode=mode.value,
            parent=parent_id,
            skills=loop.activated_skills,
        )
        return loop

    def path(self, loop_id: str) -> Path:
        if not _LOOP_ID_RE.match(loop_id):
            raise LoopNotFound(f"Invalid loop ID (path traversal blocked): {loop_id!r}")
        target = (self.loops_dir / f"{loop_id}.json").resolve()
        if not target.is_relative_to(self.loops_dir.resolve()):
            raise LoopNotFound(f"Loop path escapes ledger: {loop_id!r}")
        return target

    def load(self, loop_id: str) -> LoopRecord:
        p = self.path(loop_id)
        if not p.is_file():
            raise LoopNotFound(f"Loop not found: {loop_id}")
        return LoopRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, loop: LoopRecord) -> None:
        self.init()
        loop.updated_at = self._now()
        p = self.path(loop.id)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(loop.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, p)

    def list_all(self) -> list[LoopRecord]:
        if not self.loops_dir.is_dir():
            return []
        return [
            LoopRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in sorted(self.loops_dir.glob("L*.json"))
        ]

    def artifact_dir(self, loop_id: str) -> Path:
        if not _LOOP_ID_RE.match(loop_id):
            raise LoopNotFound(f"Invalid loop ID: {loop_id!r}")
        d = (self.artifacts_dir / loop_id).resolve()
        if not d.is_relative_to(self.artifacts_dir.resolve()):
            raise LoopNotFound(f"Artifact dir escapes ledger: {loop_id!r}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── State Transitions ─────────────────────────────────────────────────────

    def transition(
        self, loop: LoopRecord, to: Stage, reason: str, actor: str = "neoagent-devops"
    ) -> LoopRecord:
        allowed = TRANSITIONS.get(loop.stage, set())
        if to not in allowed:
            raise IllegalTransition(
                f"{loop.id}: illegal transition {loop.stage.value} -> {to.value} "
                f"(allowed: {sorted(s.value for s in allowed) or 'none'})"
            )
        if to == Stage.ESCALATED:
            loop.escalated_from = loop.stage.value
            loop.escalation_reason = reason
        self._apply_transition(loop, to, reason, actor)
        return loop

    def escalate(self, loop: LoopRecord, reason: str) -> LoopRecord:
        if loop.stage in (Stage.DONE, Stage.ESCALATED):
            raise IllegalTransition(f"{loop.id}: cannot escalate from {loop.stage.value}")
        loop.escalated_from = loop.stage.value
        loop.escalation_reason = reason
        self._apply_transition(loop, Stage.ESCALATED, reason, "neoagent-devops")
        return loop

    def resume(
        self, loop: LoopRecord, by: str, to: Stage | None = None, note: str = ""
    ) -> LoopRecord:
        if loop.stage != Stage.ESCALATED:
            raise IllegalTransition(f"{loop.id}: only ESCALATED loops can be resumed")
        if not by.strip():
            raise LedgerError("Human sign-off ('by') is required to resume an escalated loop")

        default_stage = loop.escalated_from or Stage.BUILD.value
        if default_stage in (Stage.REVIEW.value, Stage.PIPELINE.value):
            default_stage = Stage.BUILD.value
        target = to or Stage(default_stage)
        if target not in RESUMABLE:
            raise IllegalTransition(f"Cannot resume to stage '{target.value}'")

        loop.review_rounds = 0
        loop.pipeline_fixes = 0
        loop.escalated_from = None
        loop.escalation_reason = ""
        self._apply_transition(loop, target, f"resumed by {by}: {note}".strip(), by)
        return loop

    def approve(self, loop: LoopRecord, point: str, by: str, note: str = "") -> LoopRecord:
        if not by.strip():
            raise LedgerError("Approval requires non-empty human sign-off ('by')")
        if loop.has_approval(point):
            return loop
        loop.approvals.append({"point": point, "by": by, "at": self._now(), "note": note})
        self.save(loop)
        self.log_event(loop, "approved", point=point, by=by, note=note)
        return loop

    def _apply_transition(
        self, loop: LoopRecord, to: Stage, reason: str, actor: str
    ) -> None:
        prev = loop.stage
        loop.stage = to
        loop.history.append(
            {
                "at": self._now(),
                "from": prev.value,
                "to": to.value,
                "reason": reason,
                "actor": actor,
            }
        )
        self.save(loop)
        self.log_event(
            loop,
            "transition",
            from_stage=prev.value,
            to_stage=to.value,
            reason=reason,
            actor=actor,
        )

    # ── Hash-Chained Audit Trail ──────────────────────────────────────────────

    def _last_event_hash(self) -> str:
        if not self.events_path.is_file():
            return _GENESIS_HASH
        last_line = ""
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line
        if not last_line:
            return _GENESIS_HASH
        try:
            obj = json.loads(last_line)
            return str(obj.get("hash") or _GENESIS_HASH)
        except json.JSONDecodeError:
            return _GENESIS_HASH

    def log_event(self, loop: LoopRecord, kind: str, **data: Any) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        prev_hash = self._last_event_hash()
        body: dict[str, Any] = {
            "at": self._now(),
            "loop": loop.id,
            "kind": kind,
            **data,
        }
        rec_hash = _compute_record_hash(prev_hash, body)
        envelope = {**body, "prev_hash": prev_hash, "hash": rec_hash}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
        return rec_hash

    def verify_audit_chain(self) -> tuple[bool, int, str]:
        """Verify cryptographic integrity of every event in events.jsonl.

        Returns ``(is_valid, verified_count, detail_message)``.
        """
        if not self.events_path.is_file():
            return True, 0, "No events recorded yet."
        expected_prev = _GENESIS_HASH
        count = 0
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AuditChainTamperedError(
                        f"Corrupted JSON at events.jsonl line {line_no}: {exc}"
                    ) from exc
                prev_hash = entry.pop("prev_hash", None)
                rec_hash = entry.pop("hash", None)
                if prev_hash != expected_prev:
                    raise AuditChainTamperedError(
                        f"Hash chain broken at line {line_no}: expected prev_hash={expected_prev[:12]}, "
                        f"got {str(prev_hash)[:12]}"
                    )
                computed = _compute_record_hash(expected_prev, entry)
                if rec_hash != computed:
                    raise AuditChainTamperedError(
                        f"Tampered event payload at line {line_no}: expected hash={computed[:12]}, "
                        f"got {str(rec_hash)[:12]}"
                    )
                expected_prev = computed
                count += 1
        return True, count, f"Verified {count} hash-chained audit event(s)."
