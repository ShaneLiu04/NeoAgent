# NeoAgent

> 下一代 AI Agent 框架，将透明、可控的 AI Agent 愿景推向生产级。

---

## 架构

```
neoagent/
├── core/
│   ├── agent.py          # 流式 LangGraph Agent 循环，集成 MemorySaver 检查点
│   ├── provider.py       # 多提供商工厂 + SmartRouter + TokenBudget
│   ├── security.py       # 能力系统 + Docker 沙箱 + AST 安全求值器
│   ├── memory.py         # Token 感知短期记忆 + ChromaDB 长期记忆 + 结构化画像
│   ├── orchestrator.py   # 并行与顺序多 Agent 执行
│   ├── observability.py  # 线程安全指标、成本追踪、结构化 JSONL 日志
│   ├── config.py         # Pydantic Settings，支持类型化环境变量解析
│   └── tools/
│       ├── base.py       # 声明能力权限的工具基类，支持异步/同步双模式
│       ├── builtins.py   # 核心工具，与实时 APScheduler 集成
│       ├── executor.py   # Docker + exec 模式的 Python/Shell 执行
│       ├── fileops.py    # 基于 realpath 的工作区隔离（免疫符号链接绕过）
│       ├── search.py     # 异步 Tavily 搜索，支持文件缓存
│       ├── browser.py    # 轻量级网页内容提取
│       ├── scheduler.py  # 事件驱动 APScheduler，SQLite 作业存储
│       └── vision.py     # 多模态图像分析
├── server/
│   └── app.py            # FastAPI + WebSocket，支持可选 API Key 认证
├── cli/
│   └── main.py           # Rich 终端 UI，零闪烁 Markdown 面板 + 自然语言编排触发
└── tests/                # 65 个单元测试（回归、安全、编排）
```

---

## 功能全景

### 自然语言多 Agent 编排

无需写代码，无需手动拆分任务。像日常对话一样直接说，Agent 自动识别并并行执行。

```bash
❯ 同时研究三个话题：Python asyncio、Rust concurrency、Go goroutine，然后给我汇总
Orchestrator: detected 3 parallel topics — Python asyncio, Rust concurrency, Go goroutine
```

CLI 层内置意图识别，正则匹配"同时研究/比较/分析 A、B、C"等自然语言模式。自动并行派出 Researcher 子代理，多个话题同时研究，互不阻塞。结果自动汇总，子代理产出被重写为一份合成提示，交由主 Agent 输出一份结构化报告。整个过程对用户完全透明。

编排器内置六类角色，每类配有专属系统提示。Researcher 鼓励广泛使用搜索并引用来源，Coder 强调用 Python 验证方案，Reviewer 专注审查代码缺陷与安全漏洞，Analyzer 负责数据拆解，Summarizer 提炼结论，Custom 则留给用户自由定义。

并发由 asyncio.Semaphore 控制，默认最多同时运行 5 个子代理，防止供应商限流和本地资源耗尽。子代理的工具列表中自动过滤掉 `delegate_to_subagents`，从源头杜绝无限套娃。每个任务设有 120 秒超时，失败时 orchestrator 不会崩溃，而是将错误隔离为失败结果返回。

顺序编排同样严谨。上下文传递使用 `dataclasses.replace()` 创建任务副本并注入前置结果，原始任务对象始终不被修改。这种不可变输入模式让复杂工作流在代码层面也保持清晰和可预测。

---

### 沉浸式终端交互

一条命令唤醒整个框架。

```bash
pip install -e .
neo            # 一键启动（同时保留 neoagent 兼容）
```

终端里有一位名叫 N.E.O. 的赛博朋克桌宠。它不只是一个静态的 loading 动画，而是真正感知 Agent 在做什么的实时伙伴。当 Agent 进入搜索状态时，N.E.O. 会切换成数据猎手模式。当 Python 解释器开始执行时，它又会变身代码工匠。七种状态总计 46 句随机台词，从"没有勺子，只有算法"到"在 Docker 容器里拿着剪刀奔跑"，每次等待都不会重复。

N.E.O. 的动画基于 10 帧 Braille 盲文字符序列，由每次事件递增 frame 计数器驱动。即使在同一个状态下，spinner 也会持续循环旋转，让长等待变得有生命感。

渲染层采用 console.status 做轻量进度指示，token 在后台静默缓存，不直接输出到屏幕。流结束后一次性渲染为蓝色边框的 Markdown Panel，代码高亮使用 monokai 主题。用户输入和过往对话始终完整保留在屏幕上，不会被任何光标清除操作影响。

如果你直接粘贴一张图片的绝对路径，CLI 会走一条快通道。正则匹配 Windows 和 Unix 路径格式，检测到 png/jpg/gif/webp/bmp 等扩展名后直接调用 `analyze_image`，绕过整个推理循环，瞬间返回图像描述。

`/clear` 命令同时执行三件事。清屏、重置 Agent 会话历史、重印 NEO AGENT 的 ASCII 艺术 banner。用户会获得一种"全新会话"的心理暗示，而实际上进程从未中断。

Windows 用户有四项专门优化。启动时重配 stdout/stderr 为 UTF-8 编码，token 传输前后各做一次 UTF-8 清理以消除乱码，退出时执行 `os.system("")` 重置终端模式，最后显式 flush 标准流。这些细节让 Windows 终端体验与 Unix 保持同等水准。

---

### 实时流式透明

每个 Token、每次工具调用、每次预算检查、每次路由决策，都会作为类型化事件发出。

```python
from neoagent.core.agent import NeoAgent, AgentEventKind

agent = NeoAgent(provider="openai", model="gpt-4o")

async for event in agent.stream("分析这份数据集"):
    if event.kind == AgentEventKind.TOKEN:
        print(event.data, end="")                    # 实时输出
    elif event.kind == AgentEventKind.TOOL_CALL_START:
        print(f"
[🔧 {event.data['name']}] ", end="")
    elif event.kind == AgentEventKind.TOOL_CALL_END:
        print("✓")
    elif event.kind == AgentEventKind.BUDGET_WARNING:
        print(f"
[⚠️ 预算告警: {event.data}]")
```

`AgentEventKind` 枚举覆盖完整生命周期，包括 THINKING、TOKEN、TOOL_CALL_START、TOOL_CALL_END、TOOL_RESULT、MESSAGE_COMPLETE、ERROR、BUDGET_WARNING。UI 层可以精准响应每一类事件。

底层基于 LangGraph 的 `astream_events`，实时拦截流事件并重新封装为领域特定的 `AgentEvent`。自定义 ToolNode 包装了 LangGraph 不再接受的原始字符串返回，将每个工具结果强制转换为规范的 `ToolMessage`，同时保留非空内容以防止序列化错误。多个独立工具调用在 asyncio 事件循环中并发执行。

---

### 三层记忆与热重载

| 层级 | 技术 | 作用 |
|------|------|------|
| 短期记忆 | Token 感知滑动窗口 | 按 turn 分组保留近期消息，一个代码块可能占用一万 Token，轮数计数毫无意义 |
| 长期记忆 | ChromaDB 向量存储 | cosine 相似度语义搜索，"我们上周讨论的部署策略是什么" |
| 用户画像 | Pydantic 结构化模型 | 类型化的偏好、事实、约束，可查询，非原始 Markdown |

```python
from neoagent.core.memory import UserProfile

profile = UserProfile.load("user_profile.json")
profile.add_fact("喜欢冰美式")
profile.set_preference("language", "zh-CN", category="communication")
```

短期记忆的核心是 Token 感知剪枝。消息按 turn 分组，每组以 HumanMessage 开头，保留完整的对话对。只有当超出 Token 预算或轮数上限时才从最早处丢弃，系统消息则永远保留。这比固定轮数剪枝精确得多，因为一个工具调用链可能在一轮内产生大量 Token。

长期记忆基于 ChromaDB，采用 PersistentClient 和 cosine 空间。懒加载设计让它只在首次使用时初始化，telemetry 默认关闭以保护隐私。查询时自动限制 `n_results` 不超过集合大小，避免空集合报错。

用户画像是 Pydantic 模型，每条偏好记录携带置信度和 UTC 时间戳。`to_system_prompt()` 方法在开头使用全大写紧迫语句 `[User Profile — IMPORTANT: you MUST personalize every response based on this data]`。这种格式显著提升了 LLM 对记忆内容的关注度。如果画像为空，它会显式写入 `(empty — no preferences or facts recorded yet)`，让 LLM 知道记忆系统已激活但尚未记录。

热重载是记忆系统的小巧思。`build_system_prompt()` 每次都会检查 `user_profile.json` 的修改时间。如果外部工具或用户手动更新了文件，画像会在同一会话内立即重新加载，无需重启 Agent。

记忆合并器定期将丢弃的消息摘要为 2 到 3 句话存入向量库。摘要使用配置的廉价模型以控制成本，低于 100 字符的消息会被跳过，摘要失败时回退到前 500 字符原文。

---

### 成本感知与智能路由

每个会话携带独立的 Token 和成本预算。每次 LLM 调用前都会检查并预留 headroom。

```python
from neoagent.core.provider import create_budget, get_model_info

budget = create_budget("gpt-4o")
budget.check_and_reserve(estimated_input=4000, estimated_output=1000)
# 超预算时抛出 BudgetExceededError
```

`SmartRouter` 通过启发式关键词分类查询复杂度，全程不消耗任何额外的 LLM 调用。COMPLEX 指标包括 implement、debug、refactor、architecture 等，SIMPLE 指标涵盖 hello、hi、thanks、ok 等。复杂任务走旗舰模型，简单问候自动降级到廉价模型。预估可节省 60% 到 80% 的成本。

路由甚至在 agent_node 内部也能动态生效。如果会话中途出现简单查询，SmartRouter 会实时切换为廉价模型继续处理，无需用户手动干预。

框架默认以 DeepSeek 作为供应商，默认模型 deepseek-v4-pro，廉价模型 deepseek-chat。这种默认配置本身就是成本优化的体现。

网络层配备指数退避重试，每次延迟为 `base_delay * 2^attempt + random jitter`，上限 60 秒。重试仅针对限流、超时和连接错误，其他错误立即失败，避免无意义等待。

---

### 纵深防御

安全不是一道门，而是一系列门。

| 层级 | 机制 | 说明 |
|------|------|------|
| 权限模型 | 基于能力的标志位 | FILESYSTEM_READ、PROCESS_SANDBOX 等精细粒度 |
| 路径隔离 | realpath 解析 | 完全解引用符号链接后再做前缀检查 |
| 文件写入 | 原子操作 | .tmp 临时文件 + Path.replace() 原子重命名 |
| 代码执行 | OS 级隔离 | Docker 的 read-only、cap-drop ALL、network none |
| 安全计算 | AST 遍历 | 白名单节点两阶段执行，无 eval() |
| 密钥管理 | OS keyring 优先 | 回退环境变量，写操作时明确警告不写入明文 |
| 服务端认证 | 可选 Bearer Token | 公开路径除外（/docs、/health、/openapi.json） |

```python
from neoagent.core.security import get_safe_evaluator

evaluator = get_safe_evaluator()
evaluator.evaluate("sqrt(16) + max(3, 5)")      # 8.0
evaluator.evaluate("[1, 2, 3][0]")              # 1
evaluator.evaluate("__import__('os')")          # ValueError 禁止的构造
```

Capability 系统使用 enum.Flag 实现可组合权限。READ_ONLY 组包含文件读取和网络访问，FULL_SANDBOX 额外开放文件写入和受控执行，ALL 则授予全部能力。权限不足时可以选择调用升级回调，未设置回调则默认拒绝。

文件操作基于 `Path.resolve()` 做 realpath 解析，免疫符号链接绕过。write_file 采用原子写入策略，先写入 .tmp 文件再用 Path.replace() 原子替换目标。覆盖现有文件前自动创建 .bak 备份。append 模式会在内容前自动补换行符。

Docker 沙箱不是虚设。每次运行都创建临时容器，施加内存限制 256MB、CPU 限制 1 核、网络隔离、只读根文件系统、临时可写层 64MB、禁止新特权、丢弃全部 capabilities、PID 上限 50。多层 timeout 嵌套保证容器内部超时优先于 asyncio 强制 kill。stdout 超过 5 万字符和 stderr 超过 2 万字符时自动截断，防止 token bomb。

AST 安全求值器完全替代 eval()。第一阶段遍历 AST 树，仅允许 Expression、Constant、BinOp、Call 等白名单节点。第二阶段用 match/case 模式匹配执行，不调用 eval()、exec() 或 compile()。支持的数学函数涵盖 sqrt、pow、log、sin、cos 等。

---

### 生产级可观测性

```python
from neoagent.core.observability import get_observability

obs = get_observability()
obs.record_llm_call(
    model="gpt-4o",
    input_tokens=1200,
    output_tokens=400,
    cost_usd=0.0032,
    latency_ms=1200,
)

summary = obs.get_cost_summary()
# {
#   "total_cost_usd": 0.0032,
#   "total_tokens": 1600,
#   "by_model": {"gpt-4o": 0.0032},
#   "session_duration_s": 45
# }
```

Observability 是线程安全单例，通过 `__new__` 和锁保证进程内唯一。后台独立线程持续从队列消费日志事件，异步写入 JSONL 文件，零阻塞主循环。

日志按会话隔离，文件名经过安全过滤，仅保留字母数字、连字符和下划线，杜绝路径遍历。JSONL 使用 `ensure_ascii=False` 保持 Unicode 原生可读。滚动平均延迟追踪让性能趋势一目了然，模型使用分布则为容量规划提供数据支撑。

atexit 注册优雅关闭处理器，发送队列哨兵值，等待队列排空后再退出。这种设计保证即使进程被信号终止，已记录的日志也不会丢失。

事件种类覆盖 LLM_INPUT、LLM_OUTPUT、TOOL_CALL_START、TOOL_ERROR、BUDGET_WARNING、BUDGET_EXCEEDED、MEMORY_CONSOLIDATION 等完整生命周期。

---

### 事件驱动调度

心跳轮询是过去的做法。NeoAgent 使用 APScheduler 实现精确时间触发。

- 零轮询，任务在目标时间精确触发
- SQLite 持久化存储，进程重启不丢失
- Cron 支持，hourly、daily、weekly、monthly 循环
- 跨平台桌面通知，任务触发时弹出系统级提醒
- 回调注册系统，支持同步和异步回调函数
- 过期任务自动清理，不会堆积

```python
# CLI 和 Server 启动时自动加载并运行调度器
scheduler = get_scheduler()
scheduler.start()  # 精确时间的事件驱动执行
```

调度器维护双重状态。SQLite 作为执行引擎的持久化后端，tasks.json 则作为列表查看的来源。这种设计让任务在精确触发的同时，也能被人类直接阅读和编辑。

任务回调被设计为模块级函数而非实例方法，这是为了确保 APScheduler 的 SQLAlchemy 存储能够正确序列化任务。回调注册表支持任意可调用对象，触发时会依次调用所有已注册的处理器。

---

### 模型能力诚实声明

NeoAgent 不会在能力问题上对 LLM 或用户撒谎。

`NeoAgent.__post_init__` 在初始化时查询模型元数据目录。如果当前模型不支持 vision，`analyze_image` 工具会被自动剥离，系统提示中追加一段诚实声明，告知用户当前模型的能力边界，并推荐 gpt-4o、claude-sonnet-4-6、gemini-2.0-flash 等多模态替代方案。当你问"介绍一下你自己"时，如果当前模型不支持图像分析，Agent 会如实告知，不会虚假宣传。

对 DeepSeek 系列模型有两项专门适配。模块级 monkey-patch 在导入时修改 langchain_openai 的消息转换函数，保留 reasoning_content 字段。子类级 patch 则在请求载荷中重新注入该字段，确保推理模型返回的深度思考内容不会丢失。同时禁用 parallel_tool_calls，因为 DeepSeek 不支持并行工具调用。

另一项是针对 DeepSeek null-content 的补丁。DeepSeek API 拒绝 content 为 null 或空字符串的 assistant 和 tool 消息，agent_node 会在发送前将空内容替换为单个空格，同时完整保留 tool_calls 和元数据。

这种诚实声明机制不仅限于 vision。任何模型能力限制都会通过同样的管道透明传达给用户和 LLM 自身，避免幻觉和过度承诺。

## 快速开始

### 1. 环境准备

- **Python**: >= 3.11
- **Docker**（可选，推荐用于代码沙箱）: 安装并启动 Docker Desktop

```bash
# 克隆仓库
git clone <repo-url>
cd neoagent

# 创建虚拟环境（强烈推荐）
python -m venv .venv

# 激活虚拟环境
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
```

### 2. 安装依赖

```bash
# 基础安装（含 CLI + Server + 核心功能）
pip install -e ".[dev,all]"

# 如果仅需最小运行（不含 playwright / groq / ollama 扩展）
pip install -e ".[dev]"
```

### 3. 配置 API Keys

```bash
# 复制环境变量模板
cp .env.example .env
```

编辑 `.env` 文件，填入你实际拥有的 API Key。以下是各平台的获取方式：

| 服务商 | 环境变量 | 获取地址 | 说明 |
|--------|---------|---------|------|
| **DeepSeek** | `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys | 国内可用，默认供应商 |
| **OpenAI** | `OPENAI_API_KEY` | https://platform.openai.com/api-keys | 需要海外支付 |
| **Anthropic** | `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys | Claude 系列 |
| **Google** | `GOOGLE_API_KEY` | https://aistudio.google.com/app/apikey | Gemini 系列 |
| **Groq** | `GROQ_API_KEY` | https://console.groq.com/keys | 高速推理，Llama/Mistral |
| **Tavily** | `TAVILY_API_KEY` | https://app.tavily.com/home | **联网搜索必备** |

#### 🔑 Tavily API Key 获取步骤（联网搜索必须）

1. 打开 https://app.tavily.com/home 并注册/登录账号。
2. 进入左侧菜单 **"API Keys"**。
3. 点击 **"Create API Key"**，为 Key 取一个名字（如 `neoagent-local`）。
4. 复制生成的 Key（格式为 `tvly-...`）。
5. 粘贴到 `.env` 文件中对应位置：
   ```bash
   TAVILY_API_KEY=tvly-xxxxxxxxxxxxxxxxxxxxxxxx
   ```

> ⚠️ **注意**：如果不配置 `TAVILY_API_KEY`，Agent 的 `web_search` 工具将无法使用，所有需要实时网络信息的查询都会失败。

#### 可选配置

- **自定义 Base URL**：如果你使用第三方代理或本地 Ollama，取消注释并修改 `.env` 中的 `*_BASE_URL` 字段。
- **服务端 API Key**：设置 `API_KEY=your-secret-token` 可为 FastAPI 服务添加 Bearer Token 认证。

### 4. 启动运行

```bash
# CLI 交互模式（推荐初次体验）
neo              # 快捷命令（同时兼容 neoagent）

# Server 服务模式
neoagent-server
# 访问 http://localhost:8000/docs 查看交互式 API 文档
```

---

## Python SDK

### 流式 Agent

```python
import asyncio
from neoagent.core.agent import NeoAgent, AgentEventKind

async def main():
    agent = NeoAgent(provider="anthropic", model="claude-sonnet-4-6")

    async for event in agent.stream("解释量子计算"):
        if event.kind == AgentEventKind.TOKEN:
            print(event.data, end="")
        elif event.kind == AgentEventKind.TOOL_CALL_START:
            print(f"\n[使用工具: {event.data['name']}]")

asyncio.run(main())
```

### 沙箱文件操作

```python
from neoagent.core.tools.fileops import read_file, write_file

# 限制在 ./workspace/office 内，符号链接和路径穿越被阻断
await write_file(filepath="notes.md", content="# 你好\n这是安全的。")
content = await read_file(filepath="notes.md")
```

### 成本追踪

```python
from neoagent.core.observability import get_observability

obs = get_observability()
metrics = obs.get_cost_summary()
print(f"会话成本: ${metrics['total_cost_usd']:.4f}")
```

---

## Server API

### REST 流式推送（SSE）

```bash
curl -N -H "Accept: text/event-stream" \
  -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"什么是量子计算？","stream":true}'
```

### WebSocket

```javascript
const ws = new WebSocket("ws://localhost:8000/ws/my-session");
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "token") process.stdout.write(msg.content);
};
ws.send(JSON.stringify({message: "解释 async/await"}));
```

### 会话管理

```bash
# 列总会话
curl http://localhost:8000/sessions

# 获取历史
curl http://localhost:8000/sessions/my-session/history

# 删除会话
curl -X DELETE http://localhost:8000/sessions/my-session

# 指标与成本
curl http://localhost:8000/metrics
```

---

## 配置

所有配置均在 `.env` 中（详见 `.env.example`）。首次使用请务必完成 **快速开始 → 配置 API Keys** 章节。

### 完整配置示例

```bash
# ── LLM ──────────────────────────────────────────
DEFAULT_PROVIDER=deepseek        # deepseek | openai | anthropic | ollama | groq | google
DEFAULT_MODEL=deepseek-v4-pro
TEMPERATURE=0.0
MAX_TOKENS=4096

# ── API 密钥（必填至少一个 LLM Key + Tavily Key）──
DEEPSEEK_API_KEY=sk-...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
GROQ_API_KEY=gsk_...
TAVILY_API_KEY=tvly-...          # 联网搜索必备，获取方式见上文

# ── 自定义 Base URL（可选）───────────────────────
# DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
# OPENAI_BASE_URL=https://api.openai.com/v1
# OLLAMA_BASE_URL=http://localhost:11434

# ── 路由（成本优化）──────────────────────────────
ROUTER_ENABLED=true
CHEAP_MODEL=deepseek-chat
POWERFUL_MODEL=deepseek-v4-pro

# ── 预算（每会话）────────────────────────────────
BUDGET_MAX_INPUT_TOKENS=100000
BUDGET_MAX_OUTPUT_TOKENS=50000
BUDGET_MAX_COST_USD=5.00

# ── 记忆 ─────────────────────────────────────────
MEMORY_SHORT_TERM_TURNS=20
MEMORY_LONG_TERM_ENABLED=true
MEMORY_CONSOLIDATION_INTERVAL=10

# ── 沙箱 ─────────────────────────────────────────
SANDBOX_MODE=docker              # docker | process | none
SANDBOX_TIMEOUT_SECONDS=30
SANDBOX_MAX_MEMORY_MB=256
SANDBOX_NETWORK_ENABLED=false

# ── 服务 ─────────────────────────────────────────
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
SERVER_WORKERS=4
SERVER_CORS_ORIGINS=*            # 逗号分隔的允许列表
API_KEY=                         # 可选的 Bearer Token API 保护

# ── 工作区
NEOAGENT_WORKSPACE=./workspace
```

**模型能力差异提示**

`deepseek-v4-pro`、`deepseek-chat`、`deepseek-reasoner` 等 DeepSeek 系列模型不支持图像/多模态分析。

如果你需要使用 `analyze_image` 图像分析功能，请切换到以下支持多模态的模型
- **OpenAI** `gpt-4o`, `gpt-4o-mini`
- **Anthropic** `claude-sonnet-4-6`, `claude-opus-4-7`
- **Google** `gemini-2.0-flash`, `gemini-2.5-pro`

NeoAgent 会自动检测当前模型是否支持 vision，并在系统提示中诚实声明能力边界。当你问"介绍一下自己"时，如果当前模型不支持图像分析，agent 会如实告知，不会虚假宣传。

---

## 测试

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

**当前测试套件：65 个测试**，覆盖
- Token 计数与记忆剪枝回归
- AST 安全求值（算术、比较、列表、字典、切片）
- 能力系统（标志位检查、升级、只读组）
- 路径隔离（穿越阻断、符号链接解析）
- Agent 初始化与事件种类
- 编排器不可变性与上下文传递
- 提供商路由与预算执行

---

## 安全模型

| 组件 | 黄金标准 | 降级方案 | 风险标注 |
|------|---------|---------|---------|
| Python 执行 | Docker 沙箱 | `create_subprocess_exec("python", "-I", ...)` | ⚠️ |
| Shell 执行 | Docker 沙箱 | `create_subprocess_exec("sh", "-c", ...)` | ⚠️ 可能存在 shell 注入 |
| 文件访问 | realpath + 前缀检查 | 同上 | ✓ 确定性 |
| 数学求值 | AST 白名单 | 同上 | ✓ 无 eval() |

**建议**。对不可信代码启用 Docker（`SANDBOX_MODE=docker`）。降级 `process` 模式有限制，但已明确标注风险。

---

## 许可证

MIT License，详见 [LICENSE](LICENSE) 文件。

---

## 致谢

NeoAgent 的诞生源于对 [CyberClaw](https://github.com/ttguy0707/CyberClaw) 的深深敬意，作者是 [@ttguy0707](https://github.com/ttguy0707)。CyberClaw 提出了一个正确的问题

> *"当 AI 开始黑箱操作，你需要一双透视眼。"*

NeoAgent 用生产级架构回答了这个问题。感谢 [LangGraph](https://github.com/langchain-ai/langgraph)、[LangChain](https://github.com/langchain-ai/langchain)、[Pydantic](https://github.com/pydantic/pydantic)、[Rich](https://github.com/Textualize/rich) 和 [FastAPI](https://github.com/tiangolo/fastapi) 。
