# NeoAgent · AI DevOps 工程化手册

> 用 AI DevOps / AIOps 高级工程师的标准审视 NeoAgent：它不应只是“会写代码的 Agent”，而是一条 **从变更到线上、再从线上回到变更** 的受治理交付链路——每一步可度量、可回滚、可审计、可离线运行。

---

## 1. JD 要求 → NeoAgent 的落点

AI DevOps / AIOps 高级岗位的要求通常落在八个方向。下表逐项对应到仓库中 **已经实现、可以指到代码** 的能力：

| JD 要求 | 工程含义 | NeoAgent 实现 | 代码 |
|---|---|---|---|
| CI/CD 与流水线即代码 | 平台无关、可重试、失败可归因 | `devops.toml` 声明步骤；失败三分类 + flaky 识别 + 有界自愈 | `devops/pipeline.py` · `devops/autofix.py` |
| 渐进式发布与 SRE | 金丝雀、SLO 门禁、自动回滚 | 5% → 25% → 100%；相对基线的错误率 / P95 门禁；四条 SRE 不变量（见 §3） | `devops/release.py` · `devops/gates.py` |
| AIOps：告警降噪与根因 | 信号聚类、严重度、回归识别 | 中英双语分词 + Jaccard 聚类、严重度分级、回归检测；回滚原因自动注入为反馈信号 | `devops/feedback.py` |
| 故障自愈闭环 | 从线上信号回到代码 | HOTFIX / ITERATION 子闭环自动派生并附 SRS 草稿 | `devops/engine.py::ingest_feedback` |
| 工程效能度量 | DORA 四项 + AI 自治度 | 直接从哈希链账本计算，无需外部 BI | `devops/metrics.py` |
| 安全与合规 | 审计、DLP、最小权限、人工签署 | SHA-256 哈希链账本、出站提示词脱敏、全仓隐私门禁、命令参数化防注入、审批点 | `devops/ledger.py` · `devops/privacy.py` · `devops/release.py` |
| LLMOps | 模型路由、成本、上下文治理 | 角色路由（推理型 / 执行型）、单会话成本上限、40% 上下文红线与 Context Reset | `core/deepseek_harness.py` · `devops/harness.py` |
| 平台工程与集成 | 接入现有工具链而非替代 | CLI `--json`、MCP（IDE）、SARIF 2.1.0（Code Scanning）、GitHub Actions 示例 | `cli/devops_cli.py` · `mcp/server.py` · `.github/workflows/` |

## 2. 深度优化的七个角度

### 2.1 交付可信：声明即事实

**问题**：README 宣称“控制平面只依赖标准库”，但 `pyproject.toml` 把 langgraph、chromadb、docker、sqlalchemy、OpenTelemetry 等全部列为必装依赖，其中 8 个包在代码里 **从未被 import**；README 引用的 `[full,desktop]` extras 也不存在。CI 里额外 `pip install tiktoken` 掩盖了一个隐藏依赖。

**本轮修复**：

- 核心依赖清零，按能力拆分 extras：`skills / agent / memory / server / desktop / full / all / dev`；删除未使用的依赖。
- 修复两处隐藏依赖：`core/agent.py` 的 `typing_extensions`、`core/memory.py` 的 `tiktoken`（改为延迟导入，复用已有离线分词器）。
- 新增 `tests/test_control_plane_stdlib.py`：在 **屏蔽所有第三方 import** 的进程里导入 Studio 服务端、`neo-devops`、MCP、技能注册表，并真实执行 `init` 与 `status`。从此“零依赖”是测试守护的事实。
- CI 去掉 `tiktoken` 拐杖，流水线本身证明零依赖。
- 修复兼容层缺陷：安装了 pydantic 但没有 pydantic-settings 时，配置类初始化直接崩溃（4 个测试文件收集失败）。

### 2.2 发布工程：把 SRE 原则写成代码

见 §3 的四条不变量。下一步是把 `deploy_cmd / rollback_cmd / metrics_cmd` 的模板做成现成适配：Kubernetes（`kubectl argo rollouts`）、Prometheus 查询脚本、Nginx 权重切换。

### 2.3 AIOps：信号到变更的最短路径

已实现：反馈与回滚原因统一进入聚类；HOTFIX 立即派生，ITERATION 受 `max_spawned_iterations` 限流。

可深化：

- 把 CI 失败日志、告警事件也作为信号源（同一聚类管线）。
- 根因关联：回滚时把健康样本、触达文件、评审发现拼成事件时间线，写入 `fix-brief.md`。

### 2.4 可观测性：从“有指标”到“可接入”

现状：DORA 与自治度指标从账本计算，Studio 与 `neo-devops metrics` 可看；`core/observability.py` 有进程内指标与成本记录，**尚未导出** OpenTelemetry / Prometheus。

路线：按 OpenTelemetry GenAI 语义约定导出 span（loop → stage → LLM 调用 / 工具调用），并提供 `/metrics`（Prometheus）暴露 DORA 与自治度，接入客户已有 APM。

### 2.5 安全与供应链

已实现：账本哈希链 + `verify-ledger`；出站 DLP；仓库隐私门禁；版本号白名单与 `shlex` 先分词再格式化（防命令注入）；反作弊 AST（禁止删断言、跳过测试、降覆盖率）。

可深化：每个闭环产出 in-toto / SLSA 风格的出处声明（谁、在哪个提交、经过哪些门禁）；生成 SBOM；审批规则改为 policy-as-code。

### 2.6 LLMOps：成本、上下文与评测

已实现：角色路由、成本上限、40% 上下文红线、无 API Key 时降级为确定性 Harness 引擎。

可深化：为评审器与自动修复建立离线评测集（已知缺陷注入 → 检出率 / 误报率 / 修复成功率），把模型或提示词变更纳入回归门禁。

### 2.7 平台化与开发者体验

已实现：同一引擎的 CLI、Studio、MCP 三个入口；`--json` 机器可读；SARIF 接入 Code Scanning。

可深化：多用户鉴权与团队视图；把 `devops.toml` 的“黄金路径”模板化（Python / Node / Java 服务各一份）。

## 3. 发布工程的四条 SRE 不变量

这些约束在 `devops/release.py` 中机械执行，不依赖提示词：

| 不变量 | 行为 |
|---|---|
| 没有回滚能力，就没有发布权限 | `rollback_cmd` 为空时拒绝真实发布 |
| 遥测中断等于不健康 | 拉取健康指标失败立即回滚，绝不“盲飞” |
| 样本不足不等于健康 | 请求量低于 `min_requests` 时 `held`，暂停放量而不是默认通过 |
| 可重入 | 记录 `completed_percent`，人工批准 100% 后从断点继续，不重复已完成阶段 |

此外：发布版本号只允许 `[A-Za-z0-9._+-]`；命令模板先 `shlex.split` 再格式化，参数无法注入额外命令；回滚本身失败时明确提示“page on-call”。

## 4. CI 失败三分类

| 类别 | 例子 | 策略 |
|---|---|---|
| `CODE_DEFECT` | 语法错误、导入错误、lint、运行时异常 | 生成修复简报，交给自动修复子代理，有界重试 |
| `ENV_BLOCKER` | 超时、连接拒绝、缺少二进制、磁盘满 | **停止并升级**，不让模型去“修”环境 |
| `SPEC_ASSERTION_MISMATCH` | 断言失败 | 修实现，**禁止削弱断言**（反作弊 AST 兜底） |

## 5. 借鉴的开源项目与差异

| 项目 | 借鉴点 | NeoAgent 的差异 |
|---|---|---|
| [Argo Rollouts](https://github.com/argoproj/argo-rollouts) / [Flagger](https://github.com/fluxcd/flagger) | 渐进式发布、基于指标的自动分析与回滚 | 回滚不是终点：回滚原因回流为反馈信号并派生 HOTFIX 代码闭环 |
| [Keptn](https://github.com/keptn/lifecycle-toolkit) | SLO 质量门禁 | 门禁同时覆盖评审、流水线、发布三段，结果写入同一账本 |
| [Four Keys](https://github.com/dora-team/fourkeys) | DORA 四项指标 | 指标直接从防篡改账本计算，附带 AI 自治度与 AI 成本 |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) / [SWE-agent](https://github.com/SWE-agent/SWE-agent) / [Aider](https://github.com/Aider-AI/aider) | 自主编码 Agent | 生成者与评审者上下文隔离、反作弊、预算与升级——Agent 被治理而不是被信任 |
| [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt) / [HolmesGPT](https://github.com/robusta-dev/holmesgpt) | LLM 辅助诊断与根因分析 | 诊断结果直接驱动可审计的修复闭环，而不止于给出建议 |
| [reviewdog](https://github.com/reviewdog/reviewdog) / [CodeQL](https://github.com/github/codeql) | 评审结果以标准格式回写平台 | 输出 SARIF 2.1.0，可上传 Code Scanning |
| [OpenLLMetry](https://github.com/traceloop/openllmetry) / [Langfuse](https://github.com/langfuse/langfuse) | LLM 调用追踪 | 路线图：按 OTel GenAI 语义约定导出闭环级 span |
| [OPA](https://github.com/open-policy-agent/opa) / [Conftest](https://github.com/open-policy-agent/conftest) | policy-as-code | 路线图：审批与门禁规则策略化 |
| [in-toto](https://github.com/in-toto/in-toto) / [SLSA](https://slsa.dev/) | 软件供应链出处 | 账本已是出处链；路线图：导出标准出处声明 |
| [gitleaks](https://github.com/gitleaks/gitleaks) | 密钥扫描 | 额外覆盖工作站路径、内部网址、自定义敏感词，并对出站提示词脱敏 |
| `AGENTS.md` 约定 | 面向 Agent 的仓库说明 | L1 四层仓库：`AGENTS.md → docs/AI-Map.md → specs / changes / architecture / knowledges` |

## 6. 本项目的独特价值

1. **Dev 与 Ops 闭成一个环**：线上信号（用户反馈、金丝雀回滚）自动回到代码变更，HOTFIX 子闭环带着 SRS 草稿和修复简报启动。多数工具停在“部署完成”或“代码合并”。
2. **治理是机械约束**：L1–L7 七层、反作弊 AST、生成者 / 评审者隔离、失败三分类禁止削弱断言——约束由代码执行，不靠提示词自觉。
3. **SRE 原则内建**：没有回滚就不能发布、遥测中断即回滚、样本不足即暂停、断点可重入。
4. **账本是唯一事实来源**：每次状态迁移写入 SHA-256 哈希链；DORA、自治度、成本都从账本算出，可复现、可审计。
5. **零依赖、可离线**：控制平面只用标准库（有测试守护）；没有 API Key 时降级为确定性 Harness 引擎，适合内网与气隙环境。
6. **一套引擎，多种入口**：CLI、Studio、MCP、SARIF、GitHub Actions——接入现有工具链，而不是要求替换它。

## 7. 路线图

| 优先级 | 事项 | 价值 |
|---|---|---|
| P0 | OpenTelemetry GenAI span 导出 + Prometheus `/metrics` | 接入现有 APM，闭环可在 Grafana / Jaeger 中观察 |
| P0 | 发布适配模板：Argo Rollouts、Prometheus 查询、Nginx 权重 | 从 dry-run 到真实集群的最后一公里 |
| P1 | 评审器 / 自动修复离线评测集（缺陷注入） | 模型与提示词变更有回归门禁 |
| P1 | policy-as-code 审批规则 | 合规规则由平台团队维护 |
| P1 | 回滚事件时间线与根因关联 | 缩短 MTTR |
| P2 | in-toto / SLSA 出处声明 + SBOM | 供应链合规 |
| P2 | Studio 多用户鉴权与团队视图 | 从个人工具到团队平台 |
