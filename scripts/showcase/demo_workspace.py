"""Synthetic, fully de-identified demo workspace used to record README showcase assets.

Creates a throw-away git repository (``payment-gateway``) with:
  * a deliberately flawed uncommitted change (mutable default arg + wrong discount math),
  * a ``devops.toml`` wired to pytest + canary simulation hooks,
  * a small neutral Skill pack following the 3-tier progressive-disclosure layout,
  * a scripted "AI coder" so the closed loop self-heals without any LLM key.

Nothing here references any real organisation, person, or workstation path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from neoagent.devops import engine as _engine_mod
from neoagent.devops.autofix import CallableCodingBackend, CodingOutcome
from neoagent.devops.harness import EvolutionManager
from neoagent.devops.models import LoopRecord

DEMO_NAME = "payment-gateway"

# (category, skill-name, description, body-bullets)
_SKILLS: list[tuple[str, str, str, list[str]]] = [
    ("01-engineering", "devflow-tdd", "Use when implementing any feature or bugfix: write the failing test first (RED), then minimal code (GREEN), then refactor.", ["Write a failing test before any production code.", "Keep each cycle under 15 minutes.", "Never weaken an assertion to make a test pass."]),
    ("01-engineering", "devflow-clean-code", "Use when writing or reviewing Python/TypeScript code for readability, naming, and function size.", ["Functions do one thing and stay under 40 lines.", "Prefer explicit type annotations.", "No mutable default arguments."]),
    ("01-engineering", "python-coding-standards", "Use when touching .py files: typing, exceptions, immutability and import hygiene rules.", ["Catch narrow exceptions only.", "Use pathlib over os.path.", "Prefer dataclasses for value objects."]),
    ("01-engineering", "harness-skill-creator", "Use when authoring or optimising a Skill so it follows the 7-layer Harness F1-F4 rules.", ["Frontmatter must carry name and description.", "name must equal the directory name.", "Keep SKILL.md under 500 lines; push detail into references/."]),
    ("02-quality", "code-review", "Use when reviewing a diff: severity ladder, evidence-first findings, and bounded Minor noise.", ["Report Critical/Major first.", "Every finding cites file:line.", "Cap Minor findings per 200 changed lines."]),
    ("02-quality", "bug-pattern-checker", "Use when a defect class repeats: turn it into a reusable checklist item for the next review.", ["Append, never delete, checklist rules.", "One rule per recurring defect.", "Link the originating loop id."]),
    ("02-quality", "verification-before-completion", "Use before claiming a task is done: run the real test command and read the output.", ["Evidence before assertions.", "Paste the exact failing line.", "Re-run after every fix."]),
    ("02-quality", "systematic-debugging", "Use when a pipeline step fails: classify code defect vs env blocker before touching code.", ["Reproduce first.", "Env blockers escalate, never thrash code.", "Spec mismatches fix the implementation, not the assertion."]),
    ("03-security", "secure-coding", "Use when handling input, files, SQL or secrets: validate at the boundary and fail closed.", ["Parameterise every SQL statement.", "Confine file access to the workspace root.", "Never hardcode credentials."]),
    ("03-security", "it-web-security-review", "Use when reviewing web endpoints for authN/authZ, injection, XSS and CSRF exposure.", ["Check every route for auth.", "Escape output by default.", "Rate-limit public endpoints."]),
    ("04-delivery", "devflow-ship", "Use when releasing: progressive canary 5% -> 25% -> 100% with a verified rollback command.", ["Never deploy without a rollback command.", "Watch error-rate and p95 at each stage.", "Auto-rollback on SLO breach."]),
    ("04-delivery", "spec-reliability", "Use when designing observability and SLOs for a release.", ["Define SLIs before the rollout.", "Alert on burn rate.", "Document the rollback trigger."]),
    ("05-requirements", "devflow-specify", "Use at intake: turn a vague request into scope, non-goals and acceptance criteria.", ["State explicit non-goals.", "Write acceptance criteria as Given/When/Then.", "Estimate blast radius."]),
    ("05-requirements", "brainstorming", "Use when the design space is open: enumerate options and trade-offs before choosing.", ["List at least three options.", "Score against constraints.", "Record the decision."]),
    ("06-evolution", "devflow-learn", "Use after a release: distil lessons into the checklist and AGENTS.md via the SAFE loop.", ["Sense, Adapt, Formalize, Evaluate.", "Structural change needs human approval.", "Every change has a rollback hint."]),
]

_CALC_V1 = "def add(a: int, b: int) -> int:\n    return a + b\n"

_CALC_FLAWED = (
    "def add(a: int, b: int) -> int:\n"
    "    return a + b\n\n\n"
    "def apply_discount(price: float, pct: float, history=[]) -> float:\n"
    "    history.append(pct)\n"
    "    return price - pct\n"
)

_TEST_V1 = "from app.calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

_TEST_NEW = (
    "from app.calculator import add, apply_discount\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_apply_discount():\n    assert apply_discount(100.0, 0.2) == 80.0\n"
)

_OPS_SIM = (
    "import json, pathlib, sys\n"
    "mode = sys.argv[1]\n"
    "state = pathlib.Path('.canary_sim.json')\n"
    "if mode == 'deploy':\n"
    "    state.write_text(json.dumps({'version': sys.argv[2], 'pct': int(sys.argv[3])}))\n"
    "elif mode == 'rollback':\n"
    "    state.write_text(json.dumps({'rolled_back': True, 'version': sys.argv[2]}))\n"
    "elif mode == 'metrics_healthy':\n"
    "    print(json.dumps({'requests': 1200, 'error_rate': 0.001, 'p95_ms': 42.0}))\n"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def build_skill_pack(dest: Path) -> Path:
    """Write the neutral demo Skill pack (F1-F4 compliant, with Tier-3 references)."""
    for category, name, desc, bullets in _SKILLS:
        sk_dir = dest / category / name
        (sk_dir / "references").mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"- {b}" for b in bullets)
        (sk_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n## Rules\n{body}\n\n"
            f"## References\nSee `references/checklist.md` for the full rubric.\n",
            encoding="utf-8",
        )
        (sk_dir / "references" / "checklist.md").write_text(
            f"# {name} checklist\n\n" + "\n".join(f"1. [ ] {b}" for b in bullets) + "\n",
            encoding="utf-8",
        )
    return dest


def build_demo_workspace(base: Path | None = None) -> tuple[Path, Path]:
    """Return ``(workspace_dir, skills_dir)`` for a fresh synthetic demo project."""
    base = base or Path(tempfile.mkdtemp(prefix="neoagent-showcase-"))
    repo = base / DEMO_NAME
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.org")
    _git(repo, "config", "user.name", "Demo Engineer")

    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "calculator.py").write_text(_CALC_V1, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(_TEST_V1, encoding="utf-8")
    (repo / "ops_sim.py").write_text(_OPS_SIM, encoding="utf-8")

    py = sys.executable.replace("\\", "/")
    (repo / "devops.toml").write_text(
        '[project]\nbase_branch = "main"\n\n'
        "[budget]\nmax_review_rounds = 3\nmax_pipeline_fixes = 3\n\n"
        f'[[pipeline.steps]]\nname = "pytest"\ncmd = ["{py}", "-m", "pytest", "tests", "-q"]\n\n'
        "[release]\nstages = [[5, 1], [25, 1], [100, 1]]\n"
        f'deploy_cmd = "{py} ops_sim.py deploy {{version}} {{percent}}"\n'
        f'rollback_cmd = "{py} ops_sim.py rollback {{version}}"\n'
        f'metrics_cmd = "{py} ops_sim.py metrics_healthy {{version}} {{percent}}"\n\n'
        "[feedback]\nhotfix_min_reach = 2\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text(
        ".neoagent-devops/\n.canary_sim.json\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial baseline")

    # Flawed, uncommitted AI-written change (the closed loop will review + repair it).
    (repo / "app" / "calculator.py").write_text(_CALC_FLAWED, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_TEST_NEW, encoding="utf-8")

    # A prevention rule left behind by an earlier canary rollback (L7 Hashimoto loop).
    EvolutionManager(repo).record_hashimoto_guardrail(
        "L-prev-007",
        "Coupon percentages above 100% produced negative order totals",
        "canary_rollback",
    )

    skills_dir = build_skill_pack(base / "skills")
    return repo, skills_dir


# ── Review-protocol scenario: a two-round review that shows attribution and escalation ──
# Round 0 (committed baseline): sign() + a private legacy digest that is later removed.
_AUTH_BASE = (
    "import hashlib\n\n\n"
    "def sign(payload: str) -> str:\n"
    "    return hashlib.md5(payload.encode()).hexdigest()\n\n\n"
    "def _legacy_digest(token: str) -> str:\n"
    "    return hashlib.md5(token.encode()).hexdigest()\n"
)
# Round 1 (AI-written, uncommitted): sign() is only re-formatted (carried over, not blamed),
# the legacy digest is removed (credited as fixed), and a new insecure reset code appears.
_AUTH_ROUND1 = (
    "import hashlib\nimport random\n\n\n"
    "def sign(payload: str) -> str:\n"
    "    return hashlib.md5( payload.encode() ).hexdigest()\n\n\n"
    "def reset_code() -> str:\n"
    "    code = random.randint(100000, 999999)\n"
    "    return str(code)\n"
)
# Round 2 (after the repair): the reset code now uses `secrets`, but the repair also edits billing.
_AUTH_ROUND2 = (
    "import hashlib\nimport secrets\n\n\n"
    "def sign(payload: str) -> str:\n"
    "    return hashlib.md5( payload.encode() ).hexdigest()\n\n\n"
    "def reset_code() -> str:\n"
    "    code = secrets.randbelow(900000) + 100000\n"
    "    return str(code)\n"
)
_BILLING_LIMITS = (
    "MAX_COUPON_PCT = 100\n\n\n"
    "def clamp_pct(value: float) -> float:\n"
    "    return min(value, MAX_COUPON_PCT)\n"
)


def build_review_demo(base: Path) -> Path:
    """Create the synthetic ``payment-gateway`` repo used by the review-protocol showcase."""
    repo = base / DEMO_NAME
    (repo / "svc").mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.org")
    _git(repo, "config", "user.name", "Demo Engineer")
    (repo / ".gitignore").write_text(".neoagent-devops/\n__pycache__/\n", encoding="utf-8")
    (repo / "svc" / "auth.py").write_text(_AUTH_BASE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Initial baseline")
    # The AI-written change under review (uncommitted, like the flawed change of the closed-loop demo).
    (repo / "svc" / "auth.py").write_text(_AUTH_ROUND1, encoding="utf-8")
    return repo


_AUTH_TEST = (
    "from svc.auth import reset_code\n\n\n"
    "def test_reset_code_is_six_digits():\n"
    "    assert len(reset_code()) == 6\n"
)


def review_round2_edit(repo: Path) -> None:
    """The scripted repair: fixes the finding and adds its test, but also touches an out-of-scope module."""
    (repo / "svc" / "auth.py").write_text(_AUTH_ROUND2, encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_auth.py").write_text(_AUTH_TEST, encoding="utf-8")
    (repo / "billing").mkdir(exist_ok=True)
    (repo / "billing" / "limits.py").write_text(_BILLING_LIMITS, encoding="utf-8")
    # Stage only the code the change owns (not the untracked CLI-generated devops.toml).
    _git(repo, "add", "svc", "tests", "billing")


def feedback_jsonl() -> str:
    rows = [
        {"id": "fb-1", "text": "coupon above 100% crashes checkout with a 500 error", "severity": "critical", "version": "v0.1.0", "reach": 5},
        {"id": "fb-2", "text": "coupon above 100% gives negative totals and a 500 error", "severity": "high", "version": "v0.1.0", "reach": 4},
        {"id": "fb-3", "text": "please add bulk CSV export for invoices feature request", "severity": "medium", "reach": 6},
    ]
    return "\n".join(json.dumps(r) for r in rows)


def _repair(loop: LoopRecord, brief_path: Path, root: Path, round_no: int) -> CodingOutcome:
    brief = brief_path.read_text(encoding="utf-8")
    calc = root / "app" / "calculator.py"
    if "mutable" in brief.lower():
        calc.write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
            "def apply_discount(price: float, pct: float) -> float:\n    return price - pct\n",
            encoding="utf-8",
        )
        return CodingOutcome(True, "Removed mutable default argument", ["app/calculator.py"], cost_usd=0.008)
    calc.write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n\n\n"
        "def apply_discount(price: float, pct: float) -> float:\n"
        "    return round(price * (1.0 - pct), 2)\n",
        encoding="utf-8",
    )
    return CodingOutcome(True, "Fixed discount formula", ["app/calculator.py"], cost_usd=0.009)


def install_scripted_coder() -> None:
    """Make ``autopilot`` use a deterministic, offline AI-coder stand-in."""
    _engine_mod.NeoAgentCodingBackend = lambda: CallableCodingBackend(  # type: ignore[assignment]
        implement_fn=None, repair_fn=_repair
    )
