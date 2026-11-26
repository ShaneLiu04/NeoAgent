<p align="center">
  <img src="docs/assets/banner.png" alt="NeoAgent Studio" width="100%" />
</p>

<p align="center">
  <b>Agentic Engineering, governed.</b><br/>
  让 AI 的每一处改动，走完从代码到线上、再从线上回到代码的完整闭环 —— 每一步可度量、可回滚、可审计，并可离线运行。
</p>

<p align="center">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-111114?style=flat-square&logo=python&logoColor=white" />
  <img alt="tests" src="https://img.shields.io/badge/tests-140%20passed-34c759?style=flat-square" />
  <img alt="ruff" src="https://img.shields.io/badge/ruff-clean-0a84ff?style=flat-square" />
  <img alt="dlp" src="https://img.shields.io/badge/privacy--scan-CLEAN-34c759?style=flat-square" />
  <img alt="harness" src="https://img.shields.io/badge/governance-L1%E2%80%93L7-7c83ff?style=flat-square" />
  <img alt="deps" src="https://img.shields.io/badge/control%20plane-zero%20deps-e37bff?style=flat-square" />
</p>

<p align="center">
  <a href="#-本项目的独特价值">独特价值</a> ·
  <a href="#-功能导览">功能导览</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-架构">架构</a> ·
  <a href="#-治理-l1l7">治理 L1–L7</a> ·
  <a href="#-ai-devops-工程视角">工程视角</a> ·
  <a href="#-与开源方案的关系">开源对照</a> ·
  <a href="#-质量门禁">质量门禁</a> ·
  <a href="#-路线图">路线图</a>
</p>

<p align="center">
  <img src="docs/assets/hero.jpg" alt="NeoAgent Studio — 浅色交付闭环 × 深色 Agent 会话" width="100%" />
</p>

**NeoAgent Studio** 是一个把 AI 编码纳入工程交付与运维闭环的平台，而不是又一个聊天式编码助手。变更从 Git Diff 进入，经过独立评审、CI 门禁、金丝雀分级发布，线上反馈与回滚信号再聚类、派生为下一轮变更；每一步都写入 SHA-256 哈希链账本。**CLI、桌面 Studio、IDE（MCP）三个入口共享同一个引擎。**

> 没有模型 API Key 也能运行闭环的确定性环节：静态检视、AST 反作弊、流水线、金丝雀发布、反馈聚类、隐私扫描，全部离线可用。

---

## ✦ 本项目的独特价值

多数 AI 编码工具停在“生成代码”。NeoAgent 关心的是代码生成之后的每一步，并且把这些步骤写成可以机械执行的规则。

| 我们做了什么 | 为什么重要 | 实现位置 |
|---|---|---|
| **闭环，而不是聊天** | 需求 → 编码 → 评审 → 流水线 → 金丝雀 → 观测 → 演进，七阶段可自动推进，也可在任一门禁人工签署 | `devops/engine.py` |
| **运维信号回流到代码** | 金丝雀回滚的原因与用户反馈一起聚类，自动派生 HOTFIX / ITERATION 子闭环，并附 SRS 草稿 | `devops/feedback.py` · `engine.py::ingest_feedback` |
| **SRE 原则写进代码** | 没有回滚能力就不允许发布；遥测中断即回滚；样本不足则暂停放量；人工批准后从断点续发 | `devops/release.py` |
| **评审本身可被验证** | 独立性由每轮证明实测而非宣称；只追究“这次变更”引入的问题；LLM 发现必须落在真实 diff 行上；需要人拍板的问题产出决策简报而不是让 AI 乱改 —— [治理型检视协议](docs/REVIEW_PROTOCOL.md) | `devops/review/` |
| **治理是机械的，不靠提示词** | 反作弊 AST 拦截删断言、加 skip、降覆盖率；失败三分类，环境类故障直接升级给人，不让模型去“修”环境 | `devops/reviewers.py` · `devops/pipeline.py` |
| **度量来自账本** | DORA 四项与 AI 自治度、AI 成本都从防篡改账本直接计算，结果可复现、可审计 | `devops/metrics.py` · `devops/ledger.py` |
| **控制平面零依赖** | Studio 服务端、`neo-devops`、MCP、技能库只用标准库，由测试在屏蔽第三方 import 的进程中强制验证 | `tests/test_control_plane_stdlib.py` |
| **离线可运行** | 内网、气隙环境开箱即用；只有生成式编码与 LLM 评审需要模型 | `devops/harness.py` |
| **模型可替换** | 统一 Provider 工厂，支持 OpenAI 兼容端点、Anthropic、Google、Ollama 本地模型，不绑定单一厂商 | `core/provider.py` |

### 工程贡献的可验证事实

| 指标 | 结果 | 怎么验证 |
|---|---|---|
| 自动化测试 | 140 通过，覆盖闭环端到端、检视协议、评审与安全、技能注册表、Studio API / SSE、MCP | `pytest tests -q` |
| 检视规则目录 | 44 条规则，逐条带公开出处（CWE / 语言规范 / 流程条目），并映射 LLM 自由命名的规则 | `neo-devops rules` |
| 控制平面第三方依赖 | 0（`pyproject.toml` 核心依赖为空，由测试守护） | `tests/test_control_plane_stdlib.py` |
| 治理层级 | L1–L7 七层，每层对应可执行的检查或门禁 | `neo-devops harness-audit` |
| 发布不变量 | 四条 SRE 规则，由代码强制执行 | `devops/release.py` |
| 失败分类 | 代码缺陷 / 环境阻断 / 断言不符，三类处理策略不同 | `devops/pipeline.py` |
| 隐私门禁 | 全仓扫描，CI 强制执行 | `neo-devops privacy-scan` |

---

## ✦ 功能导览

> 以下素材均由本地真实运行录制：合成的 `payment-gateway` 演示仓库 + 15 项中性技能，驱动真实的 Studio 前端与 `neo-devops` CLI。为保证可复现，AI 编码环节由确定性的离线替身完成（不依赖模型 API Key），评审、流水线、金丝雀、账本与判决全部是真实执行的结果（见 [复现展示素材](#-复现展示素材)）。

### 1 · Agent 会话 —— 推理可见、过程可折叠

Codex 式居中输入与建议卡片；按角色（架构师 / 编码 / 评审 / 安全）路由到强推理或高性价比模型；推理过程实时流式展开，完成后自动折叠为「已推理 N 步」，右侧上下文圆环实时显示 40% 红线占用。

<p align="center"><img src="docs/assets/ui-chat.gif" alt="Agent 会话：流式推理与折叠" width="92%" /></p>

### 2 · 交付闭环 —— Autopilot 自愈到金丝雀全量

从 Git Diff 一键建环。Autopilot 驱动完整闭环：独立评审拦下「可变默认参数」→ 生成 `fix-brief.md` 修复 → 流水线发现折扣公式缺陷 → 三分类诊断后自动修复 → 5% → 25% → 100% 金丝雀发布。随后粘贴线上反馈，自动聚类并派生 HOTFIX 子闭环。

<p align="center"><img src="docs/assets/ui-closed-loop.gif" alt="交付闭环：Autopilot 自愈与反馈派生" width="92%" /></p>

### 3 · 检视协议 —— 归因、证据与分流

独立评审不再只是“再跑一遍”。下面是一段真实的两轮检视：第 1 轮把发现归因为 *本次引入 / 既有 / 本次修复*，只追究这次变更的责任，判决为「需修改」，交给修复器；修复后，第 2 轮发现修复补丁顺带改动了声明范围之外的计费模块，协议没有放行，而是生成**决策简报**交给负责人确认，自动修复就此暂停。每轮都写入独立性证明与检视记录。

<p align="center"><img src="docs/assets/review-protocol.gif" alt="治理型检视协议：两轮检视、归因、判决流转与决策简报" width="92%" /></p>

Studio 中同一判决链以逐轮卡片呈现：判决、独立性证明、本次引入 / 既有 / 修复计数与标准引用率一屏可见。

<p align="center"><img src="docs/assets/ui-review-protocol.jpg" alt="Studio 检视协议卡片：逐轮判决、独立性证明与归因计数" width="92%" /></p>

完整设计见 [docs/REVIEW_PROTOCOL.md](docs/REVIEW_PROTOCOL.md)。

### 4 · 技能库 —— 三级渐进式披露

按阶段筛选、按触达文件智能匹配；点开即读 Tier 2 `SKILL.md`，Tier 3 参考资料按需加载；一键运行 F1–F4 合规校验（frontmatter / 命名 / 目录一致 / 触发描述）。

<p align="center"><img src="docs/assets/ui-skills.gif" alt="技能库：渐进式加载与 F1–F4 校验" width="92%" /></p>

### 5 · 治理与合规 —— 七层健康度一屏可见

L1–L7 实时健康卡片；一键补齐四层 AI 原生文档骨架；L7 自演进提案（来自历史回滚的预防规则）经人工确认后固化进 `AGENTS.md`；DORA 四项指标、AI 自治度与全仓隐私扫描同屏呈现。

<p align="center"><img src="docs/assets/ui-harness.gif" alt="治理与合规：L1–L7、L7 自演进与隐私扫描" width="92%" /></p>

### 6 · CLI —— 同一引擎，终端原生

`neo-devops` 22 个子命令覆盖完整闭环，`--json` 输出机器可读结果，可直接接入 CI。下图是一次真实会话：初始化 → 骨架补齐 → 从 Git Diff 建环 → 评审拦截并生成修复简报 → 技能路由 → F1–F4 校验 → 治理审计 → 隐私扫描 → 账本校验。

<p align="center"><img src="docs/assets/cli-devops.gif" alt="neo-devops CLI 真实会话" width="92%" /></p>

<details>
<summary><b>完整 CLI 会话转录（静态长图）</b></summary>
<p align="center"><img src="docs/assets/cli-devops.jpg" alt="CLI 会话转录" width="80%" /></p>
</details>

<details>
<summary><b>检视协议 CLI 转录：归因、决策简报与检视记录</b></summary>
<p align="center"><img src="docs/assets/review-protocol.jpg" alt="检视协议 CLI 会话转录" width="80%" /></p>
</details>

### 截图画廊

| 浅色 | 深色 |
|---|---|
| ![首页](docs/assets/ui-home.jpg) | ![深色 Agent 会话](docs/assets/ui-dark-chat.jpg) |
| ![交付闭环完成](docs/assets/ui-closed-loop.jpg) | ![深色交付闭环](docs/assets/ui-dark-loops.jpg) |
| ![治理 L1–L7](docs/assets/ui-harness.jpg) | ![深色治理](docs/assets/ui-dark-harness.jpg) |

<details>
<summary><b>更多截图：推理展开、新建闭环、Autopilot、技能校验、效能与合规</b></summary>

| | |
|---|---|
| ![推理展开](docs/assets/ui-chat-reasoning.jpg) | ![新建闭环底部弹层](docs/assets/ui-new-loop.jpg) |
| ![Autopilot 观测阶段](docs/assets/ui-autopilot.jpg) | ![技能 Tier 3 参考](docs/assets/ui-skills.jpg) |
| ![F1–F4 校验报告](docs/assets/ui-skills-validate.jpg) | ![效能与合规](docs/assets/ui-insights.jpg) |

</details>

---

## ✦ 快速开始

### 1. 安装

```bash
pip install -e .                 # 零第三方依赖：neo-devops · neo-desktop --headless · neoagent-mcp
pip install -e ".[full]"         # 全部能力：终端 Agent + 向量记忆 + FastAPI 数据面 + 桌面窗口
```

控制平面只依赖 Python 3.11+ 标准库（`ThreadingHTTPServer` + SSE），安装后即可运行 Studio 与全部闭环命令。其余能力按需安装：

| extra | 解锁 | 主要依赖 |
|---|---|---|
| `skills` | `SKILL.md` frontmatter 的完整 YAML 解析（未安装时用内置宽松解析） | pyyaml |
| `agent` | `neo` 终端 Agent、LangGraph 编排、多模型 Provider | langgraph · langchain-* · rich · prompt-toolkit · tiktoken |
| `memory` | 长期向量记忆 | chromadb |
| `server` | 可选 FastAPI 数据面 `neoagent-server` | fastapi · uvicorn |
| `desktop` | 原生 Studio 窗口（否则用 `--headless` + 浏览器） | pywebview |
| `full` / `all` | 以上全部 / 再加 Playwright、Groq、Ollama、keyring | — |

### 2. 打开 NeoAgent Studio

```bash
neo-desktop                         # 打开 Studio 窗口（自动选择空闲端口）
neo-desktop --root path/to/repo     # 指定工作区
neo-desktop --headless --port 8765  # 仅启动服务，浏览器访问 http://127.0.0.1:8765
cd desktop && npm install && npm start   # 可选：Electron 外壳
```

外观支持浅色 / 深色（侧栏右上角切换，或 URL 参数 `?theme=dark`）。

### 3. 选择模型（可选）

模型是可替换的组件。没有配置模型时，闭环中的确定性环节照常运行；配置后，生成式编码与 LLM 评审启用。

```bash
export DEFAULT_PROVIDER="openai"              # openai · anthropic · google · ollama · deepseek · groq …
export OPENAI_API_KEY="..."                   # 对应 <PROVIDER>_API_KEY，如 ANTHROPIC_API_KEY / GOOGLE_API_KEY
export POWERFUL_MODEL="<强推理模型>"           # 架构师 / 评审 / 安全 等推理型角色
export CHEAP_MODEL="<高性价比模型>"            # 编码 / 蒸馏 等执行型角色
export NEOAGENT_SKILL_REPO="path/to/skills"   # 技能库根目录；也会自动发现同级 ../skill
```

| 变量 | 作用 | 代码默认值 |
|---|---|---|
| `DEFAULT_PROVIDER` / `DEFAULT_MODEL` | 默认模型提供方与模型名 | `deepseek` / `deepseek-v4-pro` |
| `POWERFUL_MODEL` / `CHEAP_MODEL` | 角色路由的两档模型 | `deepseek-v4-pro` / `deepseek-chat` |
| `<PROVIDER>_API_KEY` | 对应提供方的密钥 | — |
| `NEOAGENT_SKILL_REPO` | 主技能库根目录 | 同级 `../skill`（若存在） |
| `NEOAGENT_EXTRA_SKILL_DIRS` | 额外技能目录（`os.pathsep` 分隔） | — |
| `BUDGET_MAX_COST_USD` | 单会话成本上限 | `5.00` |

> 代码默认值目前指向 DeepSeek，这是出厂配置而非架构假设；任一提供方均可通过上表变量替换。

### 4. 终端里的 Agent

```bash
neo        # 流式终端 Agent：<think> 折叠渲染、40% 上下文徽标、并发工具
           # 内置命令：/skills  /devops  /desktop  /metrics  /tasks  /help
```

### 5. 闭环交付（CLI）

```bash
neo-devops init                      # 初始化 .neoagent-devops/ 账本与 devops.toml
neo-devops scaffold --apply          # L1：补齐四层 AI 原生文档骨架（非破坏）

neo-devops new "Add apply_discount pricing" --from-git --mode unattended
neo-devops autopilot <loop_id> --dry-run       # 编码 → 检视⇄修复 → CI⇄修复 → 金丝雀
neo-devops feedback <loop_id> --input feedback.jsonl   # 反馈聚类 → 派生 HOTFIX / ITERATION
neo-devops approve <loop_id> --point release --by tech-lead

neo-devops status | metrics | verify-ledger
neo-devops skills --stage review --files app/calculator.py
neo-devops harness-audit && neo-devops privacy-scan
```

### 6. IDE 集成（MCP）

```json
{
  "mcpServers": {
    "neoagent-devops": { "command": "neoagent-mcp", "args": ["--root", "."] }
  }
}
```

---

## ✦ 架构

<p align="center"><img src="docs/assets/architecture.jpg" alt="NeoAgent Studio 架构总览" width="100%" /></p>

```mermaid
flowchart LR
    I[INTAKE<br/>风险分级 · 技能路由] --> B[BUILD<br/>TDD 编码]
    B --> R{REVIEW<br/>独立评审}
    R -- 问题 --> F1[fix-brief.md] --> B
    R -- 通过 --> P{PIPELINE<br/>CI 门禁}
    P -- 失败 · 三分类 --> F2[repair brief] --> B
    P -- 通过 --> RL[RELEASE<br/>5% → 25% → 100%]
    RL -- SLO 劣化 --> RB[自动回滚] --> L
    RL --> O[OBSERVE<br/>反馈聚类]
    O -- HOTFIX / ITERATION --> I
    O --> L[LEARN<br/>L7 SAFE 自演进]
```

| 层 | 组成 | 关键模块 |
|---|---|---|
| 交互层 | NeoAgent CLI · NeoAgent Studio · `neo-devops` · MCP / Webhook | `neoagent/cli/` · `neoagent/desktop/` · `neoagent/mcp/` |
| 模型网关 | 多 Provider 工厂 · 角色路由（强推理 / 高性价比）· `<think>` 推理流解析 · 并发工具 + CapabilityToken | `neoagent/core/provider.py` · `neoagent/core/deepseek_harness.py`* |
| 治理层 | Harness L1–L7 | `neoagent/devops/harness.py` |
| 执行层 | 七阶段闭环引擎 · 有界自愈 · 金丝雀发布 · 反馈派生 | `neoagent/devops/engine.py` |
| 能力层 | 渐进式技能库 · DLP 隐私引擎 · SHA-256 账本 · 交接产物 | `neoagent/skills/registry.py` · `neoagent/devops/privacy.py` |

\* 文件名沿用早期命名；模块本身只处理 `<think>` 推理流与角色路由，与具体厂商无关。

---

## ✦ 治理 L1–L7

| 层 | 名称 | 机械约束 |
|---|---|---|
| **L1** | 信息边界 | 每个阶段声明 `must_know` / `forbidden_know`；`lite` / `full` SDD 路由；四层仓库 `AGENTS.md → docs/AI-Map.md → specs / changes / architecture / knowledges` |
| **L2** | 工具结果提炼 | 单行 grep 式日志、失败输出蒸馏、超时重试守护 |
| **L3** | 子代理门禁 | 仅当 `自包含输入 ∧ 结构化输出 ∧ 高独立性` 才派发子代理 |
| **L4** | 记忆与上下文 | `SMART ≤ 30%` · `WARNING 30–40%` · `> 40%` 强制 Context Reset 与 `checkpoint-handoff.md` |
| **L5** | 独立评估 | 评审只接收 diff 与技能包，不继承生成者的对话上下文；生成会话按 `session_id` 隔离，**每轮写入独立性证明并由 `harness-audit` 实测**（会话重叠或声明禁用输入即判违例）；规格 / 设计阶段门禁 |
| **L6** | 机械约束 | 反作弊 AST 检视（禁止删断言 / skip / 降覆盖率）、路径越界防护、SHA-256 防篡改账本、出站 DLP |
| **L7** | SAFE 自演进 | Sense → Adapt → Formalize → Evaluate；`DATA` / `RULE` / `STRUCTURAL` 分级，结构性变更必须人工确认 |

---

## ✦ 治理型检视协议

检视器的数量不是评审可信的原因。NeoAgent 在检视器之外加了五条**机械执行**的不变量，让一次评审成为一份可以被相信、可以被审计的记录：

| 不变量 | 做法 |
|---|---|
| **独立性被证明** | 每轮记录评审者实际收到的输入摘要、所在会话，以及与作者会话是否重叠；`harness-audit` 读取实测值，没评审过就如实报告“未知” |
| **有证据才有意见** | LLM 发现必须落在 diff 的真实新增行上并引用原文；落不了地的丢弃并计数，丢弃率即幻觉指标 |
| **只追究这次变更** | 反转 diff 在“旧代码”上重放确定性检视器，把发现分为 *本次引入 / 既有 / 本次修复*；既有问题降级不阻塞（CRITICAL 除外），修复了问题的变更能得到记功 |
| **按谁有权决定分流** | AI 可修的回到 BUILD；需要业务事实或架构裁决的产出**决策简报**升级给人；缺陷在规格/设计时判 `REDESIGN` 而不是对着代码打补丁 |
| **发现不能无故消失** | LLM 发现仅仅“没再被提起”而代码原样存在时保持 OPEN；修复器可用 `DISPUTE <id>: 证据` 申诉，申诉走人工而不是被静默忽略 |

默认检视链还包含 21 条带 CWE 引用的安全规则（`--deep` 把旧问题记为安全债务而非阻塞项）、范围漂移、测试证据、公开契约漂移与“无效测试”检测，全部确定性、零 LLM 成本。每轮产出 `review-record-rN.md`：独立性证明 + 责任划分 + 逐条发现的处置状态。

```bash
neo-devops review <loop> --deep      # 判决 · 归因 · 独立性 · 检视记录
neo-devops respond <loop> <finding> --by me --reason "调用方已校验，见 test_x"
neo-devops rules                     # 规则 → 维度 → 公开出处
```

两轮检视的真实录屏与 Studio 判决卡片见上文 **功能导览 · 3**。设计细节、判决流转、`devops.toml [review]` 配置与度量见 **[docs/REVIEW_PROTOCOL.md](docs/REVIEW_PROTOCOL.md)**。

---

## ✦ AI DevOps 工程视角

NeoAgent 按 AI DevOps / AIOps 高级工程师的要求设计：**变更 → 线上 → 再回到变更**，每一步可度量、可回滚、可审计。

| 工程要求 | NeoAgent 的实现 |
|---|---|
| CI/CD · 流水线即代码 | `devops.toml` 声明步骤；失败三分类 + flaky 识别 + 有界自愈（`devops/pipeline.py`） |
| 渐进式发布 · SRE | 5% → 25% → 100% 金丝雀，相对基线的错误率 / P95 门禁，四条 SRE 不变量（`devops/release.py`） |
| AIOps · 信号到变更 | 反馈与回滚原因统一聚类、分级、识别回归，自动派生 HOTFIX / ITERATION 子闭环（`devops/feedback.py`） |
| 工程效能 | DORA 四项 + AI 自治度 + AI 成本，直接从哈希链账本计算（`devops/metrics.py`） |
| 安全 · 合规 | SHA-256 账本、出站 DLP、隐私门禁、命令防注入、人工审批点、反作弊 AST |
| LLMOps | 模型角色分档、成本上限、40% 上下文红线；无模型时确定性环节照常运行 |
| 平台集成 | CLI `--json` · MCP · SARIF 2.1.0 · GitHub Actions |

**发布的四条 SRE 不变量**——由代码执行，不靠提示词：

| 不变量 | 行为 |
|---|---|
| 没有回滚能力，就没有发布权限 | `rollback_cmd` 为空时拒绝真实发布 |
| 遥测中断 = 不健康 | 拉取健康指标失败立即回滚，绝不盲飞 |
| 样本不足 ≠ 健康 | 请求量低于 `min_requests` 时暂停放量（`held`） |
| 可重入 | 记录已完成比例，人工批准全量后从断点继续 |

**CI 失败三分类**：`CODE_DEFECT` 交给自动修复（有界重试）；`ENV_BLOCKER`（超时、连接拒绝、缺二进制）**停止并升级**，不让模型去“修”环境；`SPEC_ASSERTION_MISMATCH` 只能修实现，**禁止削弱断言**——反作弊 AST 会拦下删断言、加 skip、降覆盖率。

**Ops → Dev 回流**：金丝雀回滚的原因会作为一条反馈信号与用户反馈一起聚类，HOTFIX 子闭环带着 SRS 草稿与修复简报自动启动。

> 完整的岗位要求对照、七个优化角度、开源借鉴与路线图见 **[docs/AIOPS_PLAYBOOK.md](docs/AIOPS_PLAYBOOK.md)**。

---

## ✦ 与开源方案的关系

NeoAgent 不替代这些优秀项目，而是把它们各自擅长的一段，用一个受治理的闭环串起来：

| 方向 | 代表项目 | NeoAgent 借鉴 | NeoAgent 多做的一步 |
|---|---|---|---|
| 渐进式发布 | Argo Rollouts · Flagger · Keptn | 基于指标的分析、SLO 门禁、自动回滚 | 回滚原因回流为反馈信号，自动派生 HOTFIX 代码闭环 |
| 自主编码 Agent | OpenHands · SWE-agent · Aider | 计划-执行-验证的 Agent 循环 | 生成与评审分离、反作弊、预算与升级——Agent 被治理而不是被信任 |
| AIOps 诊断 | K8sGPT · HolmesGPT | LLM 辅助诊断与根因 | 诊断直接驱动可审计的修复闭环，而不止于建议 |
| 效能度量 | Four Keys | DORA 四项 | 从防篡改账本计算，附 AI 自治度与 AI 成本 |
| 评审回写 | reviewdog · CodeQL | 标准格式回写平台 | SARIF 2.1.0 上传 Code Scanning |
| 密钥 / 隐私扫描 | gitleaks | 仓库扫描门禁 | 覆盖工作站路径、内部网址、自定义词表，并对出站提示词脱敏 |

逐项差异与链接见 [AIOPS_PLAYBOOK.md §5](docs/AIOPS_PLAYBOOK.md#5-借鉴的开源项目与差异)。

---

## ✦ 设计语言

Studio 以 **iOS 的克制质感 × Codex 的专注工作流**为设计基调：

- **品牌标识** —— 圆角超椭圆应用图标，霓虹渐变 `N` 字形如一条从起点折返再上升的路径，象征「生成 → 评审 → 再上升」的闭环；环绕的轨道与卫星点代表持续运行的 Agent。源文件：[`neoagent/desktop/static/logo.svg`](neoagent/desktop/static/logo.svg)。
- **系统级组件** —— 毛玻璃侧栏、分段控件、iOS 开关、底部弹层、胶囊 Toast、SF 风格线性图标，全部手写零依赖。
- **Codex 式会话** —— 居中大标题与建议卡片、悬浮输入框、推理过程流式展开并自动折叠。
- **数据可视化** —— Apple Watch 式上下文圆环（带 40% 红线刻度）、七阶段步进器、KPI 卡片。
- **深浅双色** —— 完整的设计令牌体系，跟随偏好持久化。

<p align="center"><img src="docs/assets/logo.png" alt="NeoAgent Studio logo" width="140" /></p>

---

## ✦ 质量门禁

<p align="center"><img src="docs/assets/quality-gates.jpg" alt="ruff · pytest · privacy-scan · harness-audit" width="92%" /></p>

```bash
python -m ruff check neoagent tests
python -m pytest tests -v
python -m neoagent.cli.devops_cli privacy-scan
python -m neoagent.cli.devops_cli harness-audit
```

测试覆盖闭环端到端（评审拦截 → 修复 → CI 自愈 → 金丝雀 → 回滚 / 反馈派生）、评审器与安全、技能注册表、治理各层、Studio API 与 SSE、MCP 服务、记忆与上下文回归等。

`tests/test_control_plane_stdlib.py` 在屏蔽所有第三方 import 的子进程中加载 Studio 服务端、`neo-devops`、MCP 与技能注册表，并真实执行 `init` / `status`——“零依赖控制平面”是被测试守护的事实，而不是宣传语。CI（`.github/workflows/ai-devops-loop.yml`）只安装 `pytest ruff pyyaml`，随后对当前提交跑一遍完整闭环：建环 → 静态评审 → 流水线 → 账本校验，并上传 SARIF。

---

## ✦ 复现展示素材

所有 GIF / JPG 均可在本地一键复现（需要 Node 18+ 与 Chrome / Edge，Python 依赖 Pillow）：

```bash
python scripts/showcase/serve_demo.py --port 8765          # 合成演示仓库 + 离线脚本化编码器
node scripts/showcase/capture_ui.mjs http://127.0.0.1:8765 out/   # 无头浏览器录制 Studio
python scripts/showcase/make_assets.py --ui-frames out/    # 合成 GIF / JPG / 架构图 / 质量门禁
```

| 素材 | 生成脚本 | 内容 |
|---|---|---|
| `ui-*.gif` / `ui-*.jpg` / `hero.jpg` | `capture_ui.mjs` → `make_assets.py` | 真实 Studio 前端的录制帧与静态截图 |
| `cli-devops.gif` / `.jpg` | `cli_story.py::run_story` | 闭环 CLI 会话 |
| `review-protocol.gif` / `.jpg` | `cli_story.py::run_review_story` | 两轮检视：归因、修复、决策简报、检视记录 |
| `architecture.jpg` · `quality-gates.jpg` | `make_assets.py` | 架构图；真实运行的 ruff / pytest / 隐私扫描 / 治理审计输出 |

演示数据完全合成：中性的支付网关仓库、15 项中性技能、临时目录路径在录制时统一替换为 `~/demo`，素材生成时技能发现被固定到合成技能包。检视协议录屏使用同名合成仓库的另一个场景（`svc/auth.py` 的重置验证码改动）。录屏文本在渲染前经过路径脱敏，并由脚本自动扫描：若仍出现工作站路径或内部标识，渲染会直接失败。

---

## ✦ 项目结构

```text
neoagent/
├── core/          # 模型网关（多 Provider）、推理流解析、记忆与 40% 上下文、安全与能力令牌
├── devops/        # 闭环引擎、评审器、自动修复、发布、治理 L1–L7、DLP、指标
│   └── review/    # 治理型检视协议：归因、证据落地、规则目录、安全/契约/测试检视器、检视记录
├── skills/        # 三级渐进式技能注册表与 F1–F4 校验
├── desktop/       # NeoAgent Studio：零依赖服务端 + static/（iOS × Codex 前端）
├── cli/           # neo（终端 Agent）与 neo-devops（闭环 CLI）
├── mcp/           # neoagent-mcp：IDE 集成
└── server/        # 可选 FastAPI 数据面
desktop/           # 可选 Electron 外壳
scripts/showcase/  # 演示仓库、录制与素材合成
docs/              # 架构文档与展示素材
```

更多细节见 [`docs/AI-Map.md`](docs/AI-Map.md)、[`docs/ARCHITECTURE_DEVOPS.md`](docs/ARCHITECTURE_DEVOPS.md)、[`docs/AIOPS_PLAYBOOK.md`](docs/AIOPS_PLAYBOOK.md) 与 [`docs/REVIEW_PROTOCOL.md`](docs/REVIEW_PROTOCOL.md)。

---

## ✦ 路线图

| 优先级 | 事项 | 价值 |
|---|---|---|
| P0 | 按 OpenTelemetry GenAI 语义约定导出闭环级 span + Prometheus `/metrics` | 闭环可在现有 APM（Grafana / Jaeger）中观察 |
| P0 | 发布适配模板：Argo Rollouts、Prometheus 查询、Nginx 权重 | 从 dry-run 到真实集群的最后一公里 |
| P1 | 评审器 / 自动修复离线评测集（缺陷注入） | 模型与提示词变更有回归门禁 |
| P1 | policy-as-code 审批规则 · 回滚事件时间线与根因关联 | 合规可维护 · 缩短 MTTR |
| P2 | in-toto / SLSA 出处声明 + SBOM · Studio 多用户 | 供应链合规 · 团队平台 |

---

## ✦ 隐私承诺

本仓库不包含任何组织名称、内部系统名、内部网址、个人工作站路径或凭据。`privacy-scan` 在 CI 中作为强制门禁运行，可通过 `devops.toml` 的 `[privacy].blocked_terms` 追加自定义敏感词。
