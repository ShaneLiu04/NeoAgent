# NeoAgent Studio v3.5 — DeepSeek Harness Production Architecture

## 1. Design Goals

NeoAgent v3.5 unifies **four production runtime surfaces** on top of a single **7-Layer Harness Engineering (`L1–L7`)** and **Closed-Loop AI DevOps** kernel:

1. **NeoAgent CLI (`neo` / `neoagent`)** — Streaming terminal coding agent with DeepSeek `<think>` trace rendering, 40% context-zone telemetry, parallel tool execution, and inline `/skills`, `/devops`, and `/desktop` commands.
2. **NeoAgent Studio (`neo-desktop` + `desktop/`)** — iOS × Codex style desktop application (frosted sidebar with workspace / five views / recent loops + center Agent session, closed-loop stepper, skill library, Harness governance and insights + right inspector with 40% context ring, L1 boundary, activated skills and artifacts) running either via zero-install OS-native Chromium app-mode / PyWebView or packaged via Electron (`desktop/main.js`).
3. **Deterministic DevOps & Harness CLI (`neo-devops`)** — Scriptable CI/CD and local CLI driving the 9-stage state machine, 4-layer repo scaffolding, F1–F4 skill compliance audits, DLP privacy gates, and L7 self-evolution approvals.
4. **IDE / Webhook Plane (`neoagent-mcp` & `neoagent-server`)** — Stdio JSON-RPC 2.0 Model Context Protocol server (`13` tools + `3` resources) for Cursor / VS Code / Claude Code plus FastAPI REST/webhook endpoints.

---

## 2. System Architecture

```text
┌───────────────────────────────────────────────────────────────────────────────────┐
│                         USER & IDE INTERACTION SURFACES                           │
│                                                                                   │
│  ┌─────────────────┐  ┌─────────────────────────┐  ┌────────────┐  ┌───────────┐  │
│  │  NeoAgent CLI   │  │     NeoAgent Studio     │  │ neo-devops │  │  IDE MCP  │  │
│  │  `neo` (Rich +  │  │  `neo-desktop` /        │  │ Automation │  │ JSON-RPC  │  │
│  │  <think> fold)  │  │  Electron + SSE Stream  │  │    CLI     │  │  Server   │  │
│  └────────┬────────┘  └────────────┬────────────┘  └─────┬──────┘  └─────┬─────┘  │
└───────────┼────────────────────────┼─────────────────────┼───────────────┼────────┘
            │                        │                     │               │
            ▼                        ▼                     ▼               ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                     DEEPSEEK REASONING-ACTION HARNESS (`core/`)                   │
│  • `ThinkStreamParser`: Streaming `<think>...</think>` & `reasoning_content`      │
│  • `fold_completed_turn_reasoning`: Keeps intra-turn tool CoT; folds prior turns  │
│  • `HarnessRoleRouter`: `deepseek-reasoner` (Plan/Review/Sec) vs `deepseek-chat`  │
│  • Parallel `asyncio.gather` Tool Executor + `CapabilityToken` + Sandbox          │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│              7-LAYER HARNESS & CLOSED-LOOP DEVOPS ENGINE (`devops/`)              │
│                                                                                   │
│  L1 Information Boundary : `STAGE_BOUNDARIES`, `route_workflow_weight`,           │
│                            4-Layer AI-Native `RepoScaffold` (`AGENTS.md`->`AI-Map`)│
│  L2 Tool & Distill       : `ToolOutputDistiller` + Carlini single-line grep logs  │
│  L3 Subagent Topology    : `SubagentDispatchGate` (3-condition dispatch rule)     │
│  L4 Context Budget       : `ContextBudgetMonitor` (<=30% Smart / 40% Hard Cap) +  │
│                            Anthropic Context-Reset `checkpoint-handoff.md`        │
│  L5 Spec & Sprint        : `SpecPhaseGate` (`req.md`/`design.md` S/D/C) + Sprints │
│  L6 Guardrails & DLP     : SHA-256 Ledger + Anti-Cheat + `PrivacyPolicyConfig`    │
│  L7 Self-Evolution       : `EvolutionManager` (`SAFE`: Sense->Adapt->Formalize)   │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│             3-TIER PROGRESSIVE SKILL REGISTRY (`skills/registry.py`)              │
│  Tier-1 Frontmatter Index -> Tier-2 Stage/Lang SKILL.md -> Tier-3 Reference Docs  │
│  + Harness F1-F4 Structural Validator + Outbound Prompt DLP Redaction             │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Closed-Loop DevOps State Machine

```text
  [Requirement / Ticket / Online Alert]
                   │
                   ▼
             ┌───────────┐
             │  INTAKE   │ ◄── L1 workflow router (lite vs full) + stage skill bundle
             └─────┬─────┘
                   ▼
             ┌───────────┐
             │   BUILD   │ ◄── TDD + SpecPhaseGate + Sprint slicing
             └─────┬─────┘
                   ▼
             ┌───────────┐     BLOCKING / DENSITY CAP    ┌───────────┐
             │  REVIEW   │ ────────────────────────────► │    FIX    │
             │(3-Layer)  │ ◄──────────────────────────── │ (Bounded) │
             └─────┬─────┘     Fresh git diff re-verify  └─────▲─────┘
                   │ PASS                                      │
                   ▼                                           │
             ┌───────────┐           STEP / GATE FAIL          │
             │ PIPELINE  │ ────────────────────────────────────┘
             └─────┬─────┘
                   │ PASS
                   ▼
             ┌───────────┐
             │  RELEASE  │ ──► Progressive 5% -> 25% -> 100% canary + auto-rollback
             └─────┬─────┘      (Rollback triggers L7 canary-regression rule proposal)
                   ▼
             ┌───────────┐
             │  OBSERVE  │ ──► Jaccard incident clustering + RICE prioritization
             └─────┬─────┘
                   ▼
             ┌───────────┐
             │   LEARN   │ ──► L7 SAFE Evolution + `.bug-patterns/checklist.md`
             └─────┬─────┘      + Auto-spawned `HOTFIX` / `ITERATION` child loops
                   ▼
              [COMPLETED]
```

> Budget exhaustion (`max_review_rounds` or `max_pipeline_fixes`) transitions deterministically to `ESCALATED`, creates an L7 `RULE` evolution proposal after 3 rounds (Hashimoto 3-Strike), and requires an audited human `approve` or `waive` action.

---

## 4. Harness L1–L7 Engineering Mapping

| Layer | Module / Class | Enforced Contract |
|---|---|---|
| **L1** Information & Doc Architecture | `STAGE_BOUNDARIES`, `route_workflow_weight`, `RepoScaffold` | Explicit `must_know` / `forbidden_know` / `non_goals`; `lite` vs `full` SDD routing; two-phase 4-layer `AGENTS.md -> docs/AI-Map.md -> specs/changes/architecture/knowledges` scaffold |
| **L2** Tool Design & Distillation | `ToolOutputDistiller` | Distills raw test/lint logs into compact failure blocks (<30 lines); writes grep-friendly single-line logs |
| **L3** Subagent Orchestration | `SubagentDispatchGate` | Enforces `self_contained_input AND structured_output AND high_independence`; reserves orchestration, state, and user interaction strictly for mainline |
| **L4** Context Budget Management | `ContextBudgetMonitor`, `fold_completed_turn_reasoning`, `write_context_reset_handoff` | `<=30%` Smart Zone, `30–40%` Warning, `>40%` Dumb Zone -> Context Reset (`checkpoint-handoff.md`); `<5`-line subagent return summaries; DeepSeek inter-turn `<think>` folding |
| **L5** Spec-Driven & Sprint Execution | `SpecPhaseGate`, `SprintTracker` | Independent Spec & Design gate validation (`S/D/C` criteria) and 1–3 step Sprint decomposition |
| **L6** Guardrails, Anti-Cheat & DLP | `StaticASTReviewer`, `LedgerStore`, `PrivacyPolicyConfig` | Blocks test skip/xfail/noqa tampering, CWE-22 path escape, hardcoded secrets, workstation paths, and configured confidential terms; SHA-256 tamper-evident hash chain |
| **L7** Self-Evolution (`SAFE`) | `EvolutionManager` | `Sense -> Adapt -> Formalize -> Evaluate` under `.neoagent-devops/evolution/` with `DATA` (auto), `RULE` (reviewed), and `STRUCTURAL` (mandatory human approval) governance |

---

## 5. Zero-Leakage Privacy & DLP Architecture (`neoagent/devops/privacy.py`)

1. **Outbound Prompt & Skill Redaction (`redact_for_llm`)**: Scrubs local OS user paths (`C:\Users\...`, `/Users/...`, `/home/...`), internal domains (`*.internal`, `*.intra`, `*.corp`, `*.lan`), proxy credentials, and custom `[privacy].blocked_terms` / `blocked_regexes` before skill bodies or prompts are sent to external LLM endpoints.
2. **Pre-Review Static DLP Gate (`scan_line_privacy`)**: Embedded inside Layer-1 `StaticASTReviewer`; raises `CRITICAL` blocking findings if any diff introduces workstation paths, internal hostnames, or configured confidential terms.
3. **Full-Repository Audit (`neo-devops privacy-scan`)**: Scans tracked text files across the workspace and returns non-zero exit status in CI if any privacy violation is detected.
