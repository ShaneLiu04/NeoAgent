"""Declarative ``devops.toml`` configuration for the AI DevOps closed loop."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .privacy import PrivacyPolicyConfig

DEVOPS_CONFIG_NAME = "devops.toml"


@dataclass
class RiskPolicyConfig:
    high_paths: list[str] = field(
        default_factory=lambda: [
            "**/auth/**",
            "**/security*",
            "**/payment*/**",
            "**/migrations/**",
            "**/*.sql",
            "**/infra/**",
            "**/deploy/**",
            "**/.github/**",
            "**/.gitlab-ci*",
            "**/Dockerfile*",
            "**/*secret*",
            "**/*.pem",
        ]
    )
    medium_paths: list[str] = field(
        default_factory=lambda: [
            "**/api/**",
            "**/server/**",
            "**/config*",
            "**/*.toml",
            "**/*.yml",
            "**/*.yaml",
        ]
    )
    medium_lines: int = 200
    high_lines: int = 800


@dataclass
class PipelineStepConfig:
    name: str
    cmd: list[str] | str
    required: bool = True
    timeout: int = 900
    retries: int = 0
    coverage_regex: str = ""


@dataclass
class PipelinePolicyConfig:
    steps: list[PipelineStepConfig] = field(default_factory=list)
    coverage_min: float = 0.0
    max_flaky: int = 0


@dataclass
class CanaryHealthConfig:
    min_requests: int = 200
    max_error_rate: float = 0.01
    error_rate_ratio: float = 2.0
    p95_ratio: float = 1.3


@dataclass
class ReleasePolicyConfig:
    stages: list[tuple[int, int]] = field(
        default_factory=lambda: [(5, 300), (25, 300), (100, 600)]
    )
    deploy_cmd: str = ""
    rollback_cmd: str = ""
    metrics_cmd: str = ""
    health: CanaryHealthConfig = field(default_factory=CanaryHealthConfig)


@dataclass
class ConvergenceBudgetConfig:
    max_review_rounds: int = 3
    max_pipeline_fixes: int = 3
    max_minor_findings: int = 10
    minor_density_lines: int = 200  # Clean-code review density cap: ceil(changed_lines / 200)


@dataclass
class FeedbackPolicyConfig:
    cluster_threshold: float = 0.3
    hotfix_min_reach: int = 3
    max_spawned_iterations: int = 5


@dataclass
class SkillPolicyConfig:
    auto_recommend: bool = True
    checklist_path: str = ".bug-patterns/checklist.md"
    auto_evolve_checklist: bool = True


@dataclass
class DevOpsConfig:
    base_branch: str = "main"
    risk: RiskPolicyConfig = field(default_factory=RiskPolicyConfig)
    pipeline: PipelinePolicyConfig = field(default_factory=PipelinePolicyConfig)
    release: ReleasePolicyConfig = field(default_factory=ReleasePolicyConfig)
    budget: ConvergenceBudgetConfig = field(default_factory=ConvergenceBudgetConfig)
    feedback: FeedbackPolicyConfig = field(default_factory=FeedbackPolicyConfig)
    skills: SkillPolicyConfig = field(default_factory=SkillPolicyConfig)
    privacy: PrivacyPolicyConfig = field(default_factory=PrivacyPolicyConfig)


def _merge_dc(dc_cls: type[Any], raw: dict[str, Any]) -> Any:
    inst = dc_cls()
    for k, v in raw.items():
        if hasattr(inst, k):
            setattr(inst, k, v)
    return inst


def _merge_step(raw: dict[str, Any]) -> PipelineStepConfig:
    if "name" not in raw or "cmd" not in raw:
        raise ValueError(f"Each [[pipeline.steps]] entry requires 'name' and 'cmd': {raw}")
    return PipelineStepConfig(
        name=str(raw["name"]),
        cmd=raw["cmd"],
        required=bool(raw.get("required", True)),
        timeout=int(raw.get("timeout", 900)),
        retries=int(raw.get("retries", 0)),
        coverage_regex=str(raw.get("coverage_regex", "")),
    )


def _validate_config(cfg: DevOpsConfig) -> None:
    pcts = [p for p, _ in cfg.release.stages]
    if not pcts or pcts[-1] != 100 or pcts != sorted(pcts) or len(set(pcts)) != len(pcts):
        raise ValueError("release.stages must be strictly increasing and end at 100%")
    if cfg.budget.max_review_rounds < 1 or cfg.budget.max_pipeline_fixes < 1:
        raise ValueError("budget rounds must be >= 1")


def load_devops_config(root: Path | str, filename: str = DEVOPS_CONFIG_NAME) -> DevOpsConfig:
    path = Path(root) / filename
    if not path.is_file():
        return DevOpsConfig()

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = DevOpsConfig(base_branch=str(data.get("project", {}).get("base_branch", "main")))
    cfg.risk = _merge_dc(RiskPolicyConfig, data.get("risk", {}))

    pipe = data.get("pipeline", {})
    cfg.pipeline = PipelinePolicyConfig(
        coverage_min=float(pipe.get("coverage_min", 0.0)),
        max_flaky=int(pipe.get("max_flaky", 0)),
        steps=[_merge_step(s) for s in pipe.get("steps", [])],
    )

    rel = data.get("release", {})
    cfg.release = _merge_dc(
        ReleasePolicyConfig, {k: v for k, v in rel.items() if k not in ("health", "stages")}
    )
    if "stages" in rel:
        cfg.release.stages = [(int(s[0]), int(s[1])) for s in rel["stages"]]
    cfg.release.health = _merge_dc(CanaryHealthConfig, rel.get("health", {}))

    cfg.budget = _merge_dc(ConvergenceBudgetConfig, data.get("budget", {}))
    cfg.feedback = _merge_dc(FeedbackPolicyConfig, data.get("feedback", {}))
    cfg.skills = _merge_dc(SkillPolicyConfig, data.get("skills", {}))
    cfg.privacy = _merge_dc(PrivacyPolicyConfig, data.get("privacy", {}))
    _validate_config(cfg)
    return cfg


DEFAULT_DEVOPS_TOML = """\
[project]
base_branch = "main"

[budget]
max_review_rounds = 3      # Max autonomous review->fix cycles before human escalation
max_pipeline_fixes = 3     # Max autonomous pipeline->fix cycles before human escalation
max_minor_findings = 10
minor_density_lines = 200  # Clean-code review density cap: <= ceil(changed_lines / 200)

[privacy]
enabled = true
redact_llm_prompts = true
block_workstation_paths = true
block_internal_urls = true
blocked_terms = []

[skills]
auto_recommend = true
checklist_path = ".bug-patterns/checklist.md"
auto_evolve_checklist = true

[pipeline]
coverage_min = 0
max_flaky = 0

[[pipeline.steps]]
name = "ruff-check"
cmd = ["python", "-m", "ruff", "check", "neoagent"]

[[pipeline.steps]]
name = "unit-tests"
cmd = ["python", "-m", "pytest", "tests", "-q"]
retries = 1

[release]
stages = [[5, 300], [25, 300], [100, 600]]
deploy_cmd = ""
rollback_cmd = ""
metrics_cmd = ""

[release.health]
min_requests = 200
max_error_rate = 0.01
error_rate_ratio = 2.0
p95_ratio = 1.3

[feedback]
cluster_threshold = 0.3
hotfix_min_reach = 3
max_spawned_iterations = 5
"""
