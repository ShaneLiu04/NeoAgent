"""Build README showcase assets (GIF / JPG) from real captures.

    python scripts/showcase/make_assets.py --ui-frames <capture_dir> [--out docs/assets]

* UI scenes  : frames recorded by ``capture_ui.mjs`` -> animated GIF + JPG stills + hero composite
* CLI scenes : a real ``neo-devops`` session (``cli_story.py``) rendered as a terminal GIF / JPG
* Blueprint  : real ``blueprint`` / ``gate`` / ``next-task`` / ``acceptance`` / ``standards`` sessions
* Quality    : real ``pytest`` / ``ruff`` / ``privacy-scan`` output of this repository
* Diagram    : layered architecture, blueprint pipeline and quality-stack infographics
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

_FONT_CANDIDATES = {
    "mono": ["C:/Windows/Fonts/consola.ttf", "/System/Library/Fonts/SFNSMono.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    "mono_bold": ["C:/Windows/Fonts/consolab.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
    "cjk": ["C:/Windows/Fonts/msyh.ttc", "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "cjk_bold": ["C:/Windows/Fonts/msyhbd.ttc", "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
    "sans": ["C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/SFNS.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "sans_bold": ["C:/Windows/Fonts/segoeuib.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES[kind]:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


# ══════════════════════════════════════════════════════════════════════════════
# Terminal renderer (Codex-dark window)
# ══════════════════════════════════════════════════════════════════════════════

T_BG = (17, 19, 24)
T_BAR = (32, 35, 42)
T_FG = (213, 218, 226)
T_DIM = (120, 128, 140)
T_GREEN = (80, 220, 140)
T_BLUE = (100, 170, 255)
T_RED = (255, 99, 92)
T_ORANGE = (255, 176, 64)
T_PURPLE = (190, 140, 255)
T_CYAN = (95, 210, 230)

_COLOR_RULES: list[tuple[re.Pattern[str], tuple[int, int, int]]] = [
    (re.compile(r"\b(CLEAN|PASS(?:ED)?|passed|aligned|healthy|Verified|verified|created|All checks passed!|OK)\b"), T_GREEN),
    (re.compile(r"\b(failed|FAIL(?:ED)?|CRITICAL|BLOCKED|violation)\b", re.I), T_RED),
    (re.compile(r"\b(MAJOR|WARNING|tier=\w+|round \d+/\d+)\b"), T_ORANGE),
    (re.compile(r"\bL\d{8}-\d{3}\b"), T_PURPLE),
    (re.compile(r"`[^`]+`|\[[\w-]+\]"), T_CYAN),
    (re.compile(r"\bL[1-7]\b"), T_BLUE),
]


def _spans(line: str, cwd: str) -> list[tuple[str, tuple[int, int, int]]]:
    if line.startswith("$ "):
        return [(cwd + " ", T_BLUE), ("$ ", T_GREEN), (line[2:], (255, 255, 255))]
    if line.startswith("#"):
        return [(line, T_PURPLE)]
    colors = [T_FG] * len(line)
    for rx, col in _COLOR_RULES:
        for m in rx.finditer(line):
            for i in range(m.start(), m.end()):
                colors[i] = col
    spans: list[tuple[str, tuple[int, int, int]]] = []
    for ch, col in zip(line, colors, strict=True):
        if spans and spans[-1][1] == col:
            spans[-1] = (spans[-1][0] + ch, col)
        else:
            spans.append((ch, col))
    return spans


class Terminal:
    def __init__(
        self, width: int = 1280, rows: int = 26, size: int = 17, title: str = "neo-devops — zsh", cwd: str = "~/demo/payment-gateway"
    ) -> None:
        self.width = width
        self.cwd = cwd
        self.rows = rows
        self.f = font("mono", size)
        self.fc = font("cjk", size - 1)
        self.line_h = int(size * 1.55)
        self.pad = 26
        self.bar_h = 40
        self.height = self.bar_h + self.pad * 2 + self.line_h * rows
        self.title = title
        self.cols = int((width - self.pad * 2) / self.f.getlength("M"))

    def wrap(self, text: str) -> list[str]:
        out: list[str] = []
        for raw in text.splitlines() or [""]:
            line = raw.rstrip()
            while len(line) > self.cols:
                out.append(line[: self.cols])
                line = "  " + line[self.cols :]
            out.append(line)
        return out

    def render(self, lines: list[str], cursor: bool = False) -> Image.Image:
        img = Image.new("RGB", (self.width, self.height), T_BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, self.width, self.bar_h], fill=T_BAR)
        for i, col in enumerate([(255, 95, 87), (254, 188, 46), (40, 200, 64)]):
            d.ellipse([18 + i * 22, 14, 30 + i * 22, 26], fill=col)
        tf = font("sans", 14)
        tw = d.textlength(self.title, font=tf)
        d.text(((self.width - tw) / 2, 11), self.title, font=tf, fill=T_DIM)
        visible = lines[-self.rows :]
        y = self.bar_h + self.pad
        x0 = self.pad
        last_x = x0
        for ln in visible:
            x = x0
            for text, col in _spans(ln, self.cwd):
                for ch in text:
                    f = self.f if ord(ch) < 0x2E80 else self.fc
                    d.text((x, y + (0 if f is self.f else 1)), ch, font=f, fill=col)
                    x += f.getlength(ch)
            last_x = x
            y += self.line_h
        if cursor:
            cy = self.bar_h + self.pad + (len(visible) - 1) * self.line_h
            d.rectangle([last_x + 2, cy + 3, last_x + 11, cy + self.line_h - 5], fill=T_GREEN)
        return img


def window_frame(img: Image.Image, radius: int = 16, margin: int = 36, bg: tuple[int, int, int] = (238, 240, 246)) -> Image.Image:
    """Rounded corners + soft drop shadow on a light canvas (README-friendly)."""
    w, h = img.size
    canvas = Image.new("RGB", (w + margin * 2, h + margin * 2), bg)
    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle([margin, margin + 10, margin + w, margin + h + 10], radius, fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.paste(Image.new("RGB", canvas.size, (40, 44, 60)), mask=shadow)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius, fill=255)
    canvas.paste(img, (margin, margin), mask)
    return canvas


# ══════════════════════════════════════════════════════════════════════════════
# GIF helpers
# ══════════════════════════════════════════════════════════════════════════════


def save_gif(frames: list[Image.Image], durations: list[int], path: Path, colors: int = 192) -> None:
    merged_f: list[Image.Image] = []
    merged_d: list[int] = []
    for fr, du in zip(frames, durations, strict=True):
        if merged_f and ImageChops.difference(merged_f[-1], fr).getbbox() is None:
            merged_d[-1] += du
            continue
        merged_f.append(fr)
        merged_d.append(du)
    pal = [f.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in merged_f]
    pal[0].save(path, save_all=True, append_images=pal[1:], duration=merged_d, loop=0, optimize=True, disposal=1)
    print(f"  gif {path.name}: {len(pal)} frames, {path.stat().st_size / 1e6:.2f} MB")


def save_jpg(img: Image.Image, path: Path, quality: int = 90) -> None:
    img.convert("RGB").save(path, "JPEG", quality=quality, optimize=True, progressive=True)
    print(f"  jpg {path.name}: {img.size[0]}x{img.size[1]}, {path.stat().st_size / 1e3:.0f} KB")


# ══════════════════════════════════════════════════════════════════════════════
# Scenes
# ══════════════════════════════════════════════════════════════════════════════


_LEAK_PATTERN = re.compile(r"[A-Za-z]:[\\/]|/Users/|/home/|neoagent-showcase-|\\Users\\", re.I)


def _assert_clean(story: list[tuple[str, str]]) -> None:
    """Refuse to render a recording that still shows a workstation path or a company identifier."""
    for cmd, text in story:
        for chunk in (cmd, text):
            hit = _LEAK_PATTERN.search(chunk)
            if hit:
                raise RuntimeError(f"privacy leak in recording: {hit.group(0)!r} in {cmd!r}")


def _story_gif(story: list[tuple[str, str]], path: Path, title: str, rows: int = 28) -> None:
    """Type each command (``# `` narration is typed without a prompt), then reveal its output."""
    _assert_clean(story)
    term = Terminal(width=1280, rows=rows, title=title)
    frames: list[Image.Image] = []
    durs: list[int] = []
    buf: list[str] = []
    for cmd, text in story:
        prompt = "" if cmd.startswith("# ") else "$ "
        for i in range(0, len(cmd) + 1, 5):
            frames.append(term.render(buf + [prompt + cmd[:i]], cursor=True))
            durs.append(45)
        buf.append(prompt + cmd)
        frames.append(term.render(buf, cursor=False))
        durs.append(350)
        body = term.wrap(text)
        step = 3 if len(body) > 12 else 2
        for i in range(0, len(body), step):
            buf.extend(body[i : i + step])
            frames.append(term.render(buf))
            durs.append(70)
        durs[-1] = 1300
        buf.append("")
    durs[-1] = 3500
    save_gif([window_frame(f) for f in frames], durs, path, colors=96)


def _story_transcript(story: list[tuple[str, str]], path: Path, title: str, skip: tuple[str, ...] = ()) -> None:
    """Static tall transcript of a story, skipping boilerplate steps."""
    _assert_clean(story)
    term = Terminal(width=1280, rows=1, title=title)
    lines: list[str] = []
    for cmd, text in story:
        if cmd.startswith(skip):
            continue
        lines.append(cmd if cmd.startswith("# ") else "$ " + cmd)
        lines.extend(term.wrap(text))
        lines.append("")
    tall = Terminal(width=1280, rows=len(lines), title=title)
    save_jpg(window_frame(tall.render(lines)), path)


def build_cli_assets(out: Path) -> None:
    from cli_story import run_story

    print("[cli] running real neo-devops session on synthetic workspace …")
    story = run_story()
    _story_gif(story, out / "cli-devops.gif", "neo-devops — closed-loop CLI")
    _story_transcript(
        story, out / "cli-devops.jpg", "neo-devops — real session transcript",
        skip=("neo-devops init", "neo-devops build"),
    )


def build_review_protocol_assets(out: Path) -> None:
    from cli_story import run_review_story

    print("[review] running a two-round governed review on a synthetic repository …")
    story = run_review_story()
    _story_gif(story, out / "review-protocol.gif", "neo-devops — governed review protocol", rows=30)
    _story_transcript(
        story, out / "review-protocol.jpg", "neo-devops — review protocol transcript",
        skip=("neo-devops init", "neo-devops build"),
    )


def build_blueprint_assets(out: Path) -> None:
    from cli_story import run_blueprint_story, run_guardrail_story, run_standards_story

    print("[blueprint] running the Spec-to-Code Blueprint end to end on a synthetic repository …")
    story = run_blueprint_story()
    _story_gif(story, out / "blueprint-engine.gif", "neo-devops — spec-to-code blueprint", rows=32)
    _story_transcript(
        story, out / "blueprint-engine.jpg", "neo-devops — spec-to-code blueprint transcript",
        skip=("neo-devops init",),
    )

    print("[guardrails] strict-clarify pause + Lite red-line upgrade …")
    story = run_guardrail_story()
    _story_gif(story, out / "blueprint-guardrails.gif", "neo-devops — blueprint guardrails", rows=26)
    _story_transcript(
        story, out / "blueprint-guardrails.jpg", "neo-devops — blueprint guardrails transcript",
        skip=("neo-devops init",),
    )

    print("[standards] 3-layer quality stack + deterministic clean-code review …")
    story = run_standards_story()
    _story_gif(story, out / "quality-stack.gif", "neo-devops — 3-layer coding standards stack", rows=30)
    _story_transcript(
        story, out / "quality-stack.jpg", "neo-devops — quality stack transcript",
        skip=("neo-devops init", "neo-devops build"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Diagrams (blueprint pipeline, quality stack)
# ══════════════════════════════════════════════════════════════════════════════

_INK = (28, 28, 30)
_MUTED = (110, 114, 128)
_BLUE, _INDIGO, _GREEN, _ORANGE, _RED, _PURPLE, _TEAL = (
    (0, 122, 255), (88, 86, 214), (52, 199, 89), (255, 149, 0), (255, 59, 48), (175, 82, 222), (48, 176, 199)
)


class _Canvas:
    """Tiny helper around Pillow for the README infographics (cards, pills, arrows, labels)."""

    def __init__(self, w: int, h: int) -> None:
        self.img = Image.new("RGB", (w, h), (250, 250, 252))
        self.d = ImageDraw.Draw(self.img)
        self.f_title = font("cjk_bold", 32)
        self.f_h = font("cjk_bold", 22)
        self.f_t = font("cjk", 18)
        self.f_s = font("cjk", 16)
        self.f_mono = font("mono", 17)

    def text_c(self, cx: float, y: float, s: str, f: ImageFont.FreeTypeFont, fill: tuple[int, int, int]) -> None:
        self.d.text((cx - self.d.textlength(s, font=f) / 2, y), s, font=f, fill=fill)

    def card(self, x: int, y: int, w: int, h: int, title: str, lines: list[str], color: tuple[int, int, int], solid: bool = True) -> None:
        self.d.rounded_rectangle([x + 3, y + 5, x + w + 3, y + h + 5], 18, fill=(225, 227, 235))
        if solid:
            self.d.rounded_rectangle([x, y, x + w, y + h], 18, fill=color)
            fg, sub = (255, 255, 255), (235, 240, 255)
        else:
            self.d.rounded_rectangle([x, y, x + w, y + h], 18, fill=(255, 255, 255), outline=color, width=3)
            fg, sub = color, _MUTED
        self.text_c(x + w / 2, y + 14, title, self.f_h, fg)
        for i, ln in enumerate(lines):
            self.text_c(x + w / 2, y + 52 + i * 25, ln, self.f_s, sub)

    def pill(self, x: int, y: int, w: int, h: int, label: str, color: tuple[int, int, int], solid: bool = False, f: ImageFont.FreeTypeFont | None = None) -> None:
        f = f or self.f_mono
        self.d.rounded_rectangle([x, y, x + w, y + h], h // 2, fill=color if solid else (255, 255, 255), outline=color, width=2)
        self.text_c(x + w / 2, y + (h - f.size) / 2 - 2, label, f, (255, 255, 255) if solid else _INK)

    def arrow(self, x1: int, y: int, x2: int, color: tuple[int, int, int] = (150, 155, 170)) -> None:
        self.d.line([(x1, y), (x2 - 8, y)], fill=color, width=3)
        self.d.polygon([(x2 - 12, y - 7), (x2, y), (x2 - 12, y + 7)], fill=color)

    def band(self, y: int, label: str) -> None:
        self.d.text((60, y), label, font=font("cjk_bold", 17), fill=(120, 124, 140))


def build_blueprint_pipeline(path: Path) -> None:
    c = _Canvas(2000, 1160)
    c.d.text((60, 36), "Spec-to-Code Blueprint — 契约化需求分解流水线", font=c.f_title, fill=_INK)
    c.d.text((60, 84), "阶段由磁盘上的产物与门禁记录推导（detect_phase），而不是由模型“自述”；每个闸门只读、独立、可复跑。", font=c.f_t, fill=_MUTED)

    c.band(130, "产物 ARTIFACTS  ·  .neoagent-devops/artifacts/<loop>/blueprint/")
    arts = [
        ("spec.md", ["EARS 语句 · Given/When/Then", "Existing Baseline（AST 取证）", "ATAM QAS · OpenQuestions"], _BLUE),
        ("design.md", ["A/B 方案对比与爆炸半径", "6 段接口契约 · 错误模型", "规范化 TC-* 测试设计"], _INDIGO),
        ("tasks.md", ["micro / standard 任务 DAG", "Consumes / Produces 精确签名", "TDD 证据 · 门禁记录"], _PURPLE),
        ("traceability.md", ["需求 → 设计 → 任务 → 代码", "→ 测试 双向追溯矩阵", "S/D/C 合规的数据源"], _TEAL),
        ("acceptance-cases.md", ["黑盒 AT-* 验收用例", "Go / Conditional-Go / No-Go", "干净归档（不含 tasks.md）"], _GREEN),
    ]
    for i, (t, ls, col) in enumerate(arts):
        c.card(60 + i * 376, 160, 352, 132, t, ls, col)

    c.band(326, "阶段与闸门 PHASE ROUTER & GATES")
    steps = [
        ("SPEC", _BLUE, True), ("Spec Gate", _ORANGE, False), ("DESIGN", _INDIGO, True), ("Design Gate", _ORANGE, False),
        ("PLAN", _PURPLE, True), ("Plan Gate", _ORANGE, False), ("DEVELOP", _GREEN, True), ("SDC Review", _ORANGE, False),
        ("ACCEPT", _TEAL, True),
    ]
    x = 60
    for label, col, solid in steps:
        c.pill(x, 356, 178, 54, label, col, solid)
        if label != "ACCEPT":
            c.arrow(x + 180, 383, x + 208)
        x += 208
    gates = ["G1–G9", "G1–G15", "P1–P6", "S · D · C"]
    gx = [60 + 208, 60 + 208 * 3, 60 + 208 * 5, 60 + 208 * 7]
    for g, px in zip(gates, gx, strict=True):
        c.text_c(px + 89, 418, g, c.f_s, _MUTED)
    c.d.text((60, 452), "Plan Gate：Kahn 环检测 · TC-* 集合相等 · 零占位符扫描 · 任务粒度与依赖闭合   |   同一闸门 3 轮不过 → 升级人类裁决，不盲目重试", font=c.f_s, fill=_MUTED)

    c.band(500, "隔离式 TDD DEVELOP  ·  每个任务一个零历史上下文包")
    flow = [
        ("Context Pack", "task-T00k-brief.md\n仅含本任务所需切片", _TEAL),
        ("RED 会话", "只写失败测试\n独立上下文", _RED),
        ("GREEN 会话", "最小实现 + 5D 规范\nCleanCodeCheck 取证", _GREEN),
        ("REFACTOR", "仅限已授权文件\n越界即阻断", _INDIGO),
        ("证据落盘", "red/green 输出 · 会话 ID\n写入 tasks.md", _PURPLE),
    ]
    for i, (t, s, col) in enumerate(flow):
        bx = 60 + i * 376
        c.card(bx, 530, 330, 120, t, s.split("\n"), col, solid=False)
        if i < len(flow) - 1:
            c.arrow(bx + 336, 590, bx + 372)
    c.d.text((60, 668), "ModelTierSelector：micro → cheap · 标准任务 → standard · 多文件/多需求 → reasoner（按复杂度分配模型，成本与质量同时最优）", font=c.f_s, fill=_MUTED)

    c.band(716, "护栏 GUARDRAILS")
    rails = [
        ("Lite 6 条升级红线", ["高风险/多文件/跨模块等", "任一触发 → 强制升级 Full"], _RED),
        ("strict-clarify", ["未量化需求 → OpenQuestion", "回答后折入需求并复验"], _ORANGE),
        ("返工重置", ["apply_rework_reset", "只回退受影响阶段"], _INDIGO),
        ("压缩快照", ["build_compaction_snapshot", "≤ 2400 字符续航摘要"], _TEAL),
        ("隐私出站", ["redact_for_llm", "上下文包出站前脱敏"], _GREEN),
    ]
    for i, (t, ls, col) in enumerate(rails):
        c.card(60 + i * 376, 746, 352, 118, t, ls, col, solid=False)

    c.band(902, "入口 SURFACES")
    surf = [
        ("CLI", "neo-devops blueprint · gate · next-task · acceptance"),
        ("MCP", "devops_blueprint · devops_phase_gate · standards_compile"),
        ("Studio", "交付闭环 · 需求分解与阶段门禁面板（一键推进）"),
        ("Python API", "DevOpsEngine.blueprint / run_phase_gate / execute_blueprint_tasks"),
    ]
    for i, (t, s) in enumerate(surf):
        bx = 60 + i * 470
        c.pill(bx, 934, 440, 52, t, _BLUE, solid=True, f=c.f_h)
        c.text_c(bx + 220, 996, s, c.f_s, _MUTED)
    c.d.text((60, 1070), "无 API Key 时由确定性合成器（AST 取证 + 模板）产出同样结构的文档，闸门与追溯照常运行；接入模型后仅提升内容质量，不改变门禁语义。", font=c.f_s, fill=_MUTED)
    c.d.text((60, 1102), "控制平面仅依赖 Python 标准库（由测试守卫），可在内网/离线环境运行。", font=c.f_s, fill=_MUTED)
    save_jpg(c.img, path, quality=92)


def build_quality_stack_diagram(path: Path) -> None:
    c = _Canvas(2000, 1000)
    c.d.text((60, 36), "三层代码规范栈 — 把“规范”编译成提示词，再用确定性检视兜底", font=c.f_title, fill=_INK)
    c.d.text((60, 84), "按文件语言与生命周期阶段按需加载，预算受控；写代码的模型与检视代码的引擎共用同一套标准。", font=c.f_t, fill=_MUTED)

    c.band(130, "编译 COMPILE（QualityStackCompiler）")
    layers = [
        ("Layer 1 · Clean-Code 5D 底线", ["简洁 · 可靠 · 可维护 · 可测 · 性能", "+ 范围纪律：一次 diff 只做一件事"], _BLUE),
        ("Layer 2 · 7 级最小代码阶梯", ["规格核对 → 仓库复用 → 标准库 → 平台原生", "→ 已装依赖 → 直接表达 → 最小必要代码"], _INDIGO),
        ("Layer 3 · 语言护栏（9 种语言）", ["Python · TS/JS · C/C++ · Go · Rust · Java · SQL", "按文件扩展名自动推断并加载"], _PURPLE),
    ]
    for i, (t, ls, col) in enumerate(layers):
        c.card(60, 160 + i * 150, 760, 128, t, ls, col)
    c.card(60, 618, 760, 112, "不可裁剪安全底线 · 有界捷径", ["输入校验 · 错误处理 · 安全控制 · 断言强度永不削减", "捷径必须写成  # debt: <上限>, revisit when <触发条件>"], _RED, solid=False)

    c.arrow(830, 395, 950)
    c.d.text((845, 340), "预算内", font=c.f_s, fill=_MUTED)
    c.d.text((845, 362), "拼装", font=c.f_s, fill=_MUTED)

    c.d.text((960, 130), "注入 INJECT", font=font("cjk_bold", 17), fill=(120, 124, 140))
    inj = [
        ("Autofix 编码后端", "system prompt 自动附带规范栈", _TEAL),
        ("Blueprint TDD 任务", "task-brief 与 Context Pack 携带规范栈", _GREEN),
        ("交接简报 task-brief.md", "供 IDE / 其他 Agent 直接复用", _ORANGE),
    ]
    for i, (t, s, col) in enumerate(inj):
        c.card(960, 160 + i * 150, 440, 128, t, [s], col, solid=False)

    c.arrow(1410, 395, 1500)
    c.d.text((1520, 130), "强制 ENFORCE", font=font("cjk_bold", 17), fill=(120, 124, 140))
    c.card(1520, 160, 420, 128, "CleanCodeReviewer", ["12 条确定性规则（AST + diff）", "长函数 · 深嵌套 · 复杂度 · 参数过多"], _RED)
    c.card(1520, 310, 420, 128, "旧侧归因", ["在“修改前”的代码上重跑同一规则", "既有问题 ≠ 本次引入，不冤枉作者"], _ORANGE)
    c.card(1520, 460, 420, 128, "治理型检视协议", ["证据落地 · 独立性证明", "决策简报 · 升级人类裁决"], _INDIGO)
    c.d.line([(1730, 288), (1730, 310)], fill=(150, 155, 170), width=3)
    c.d.line([(1730, 438), (1730, 460)], fill=(150, 155, 170), width=3)

    c.d.text((60, 790), "规则目录：neo-devops rules 共 60 条（含 cc-* 规范规则、py-* 语言规则、sdc-* 规格/设计合规规则），每条引用公开标准（PEP 8 / PEP 3134 / McCabe / CWE / OWASP）。", font=c.f_s, fill=_MUTED)
    c.d.text((60, 822), "写代码、审代码、验收代码三处对齐同一把尺子——这是“最大限度发挥 LLM”的前提：先把标准说清楚，再让确定性引擎兜底。", font=c.f_s, fill=_MUTED)
    save_jpg(c.img, path, quality=92)


def build_quality_asset(out: Path) -> None:
    print("[quality] running ruff + pytest + privacy-scan on this repository …")
    from demo_workspace import build_skill_pack

    py = sys.executable
    # Pin skill discovery to the synthetic pack so local private skill libraries never leak into renders.
    pack = build_skill_pack(Path(tempfile.mkdtemp(prefix="neoagent-pack-")) / "skills")
    env = {**os.environ, "NEOAGENT_SKILL_REPO": str(pack), "NEOAGENT_EXTRA_SKILL_DIRS": ""}

    def run(args: list[str]) -> str:
        p = subprocess.run(args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return (p.stdout + p.stderr).strip()

    ruff = run([py, "-m", "ruff", "check", "neoagent", "tests"])
    ruff = re.sub(r"\x1b\[[0-9;]*m", "", ruff)
    pytest_out = run([py, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"])
    summary = [ln for ln in pytest_out.splitlines() if ln.strip()]
    privacy = run([py, "-m", "neoagent.cli.devops_cli", "privacy-scan"])
    audit = run([py, "-m", "neoagent.cli.devops_cli", "harness-audit"])
    lines = ["$ python -m ruff check neoagent tests", ruff, "", "$ python -m pytest tests -q"]
    lines += summary[-6:]
    lines += ["", "$ neo-devops privacy-scan", privacy, "", "$ neo-devops harness-audit", audit]
    term = Terminal(width=1280, rows=1, title="quality gates — NeoAgent repository", cwd="~/NeoAgent")
    wrapped: list[str] = []
    for ln in lines:
        wrapped.extend(term.wrap(ln) if ln else [""])
    term = Terminal(width=1280, rows=len(wrapped), title="quality gates — NeoAgent repository", cwd="~/NeoAgent")
    save_jpg(window_frame(term.render(wrapped)), out / "quality-gates.jpg")


def build_ui_assets(frames_dir: Path, out: Path) -> None:
    print(f"[ui] assembling GIFs from {frames_dir} …")
    scenes = {
        "chat": "ui-chat.gif",
        "loop": "ui-closed-loop.gif",
        "skills": "ui-skills.gif",
        "harness": "ui-harness.gif",
        "blueprint": "ui-blueprint.gif",
    }
    shutil.copyfile(REPO_ROOT / "neoagent" / "desktop" / "static" / "logo.svg", out / "logo.svg")
    for scene, name in scenes.items():
        meta_path = frames_dir / scene / "frames.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        imgs = [Image.open(m["file"]).convert("RGB").resize((1280, 768), Image.LANCZOS) for m in meta]
        durs = []
        for i, m in enumerate(meta):
            gap = (meta[i + 1]["t"] - m["t"]) if i + 1 < len(meta) else 0
            durs.append(int(max(70, min(gap, 1500)) + m.get("hold", 0)))
        durs[-1] = max(durs[-1], 2500)
        save_gif(imgs, durs, out / name, colors=160)

    stills = frames_dir / "stills"
    mapping = {
        "01-home": "ui-home.jpg",
        "02-chat": "ui-chat.jpg",
        "02-chat-reasoning": "ui-chat-reasoning.jpg",
        "03-new-loop-sheet": "ui-new-loop.jpg",
        "04-loop-autopilot": "ui-autopilot.jpg",
        "05-loop-done": "ui-closed-loop.jpg",
        "05-protocol": "ui-review-protocol.jpg",
        "06-skills": "ui-skills.jpg",
        "06-skills-validate": "ui-skills-validate.jpg",
        "07-harness-before": "ui-harness-before.jpg",
        "07-harness": "ui-harness.jpg",
        "08-insights": "ui-insights.jpg",
        "09-dark-loops": "ui-dark-loops.jpg",
        "10-dark-chat": "ui-dark-chat.jpg",
        "11-dark-harness": "ui-dark-harness.jpg",
        "12-blueprint-synth": "ui-blueprint-synth.jpg",
        "12-blueprint": "ui-blueprint.jpg",
        "12-blueprint-spec": "ui-blueprint-spec.jpg",
        "13-dark-blueprint": "ui-dark-blueprint.jpg",
    }
    for src, dst in mapping.items():
        p = stills / f"{src}.png"
        if p.is_file():
            save_jpg(Image.open(p), out / dst, quality=88)

    light = stills / "05-loop-done.png"
    dark = stills / "10-dark-chat.png"
    if light.is_file() and dark.is_file():
        build_hero(Image.open(light).convert("RGB"), Image.open(dark).convert("RGB"), out / "hero.jpg")


def _rounded(img: Image.Image, radius: int) -> tuple[Image.Image, Image.Image]:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius, fill=255)
    return img, mask


def build_hero(back: Image.Image, front: Image.Image, path: Path) -> None:
    W, H = 2400, 1240
    canvas = Image.new("RGB", (W, H))
    top, bottom = (232, 236, 255), (246, 238, 255)
    grad = ImageDraw.Draw(canvas)
    for y in range(H):
        t = y / H
        grad.line([(0, y), (W, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-300, -400, 1100, 700], fill=120)
    gd.ellipse([1500, 600, 2800, 1700], fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    canvas.paste(Image.new("RGB", (W, H), (150, 170, 255)), mask=glow)

    def place(img: Image.Image, x: int, y: int, w: int) -> None:
        h = int(img.size[1] * w / img.size[0])
        im = img.resize((w, h), Image.LANCZOS)
        shadow = Image.new("L", (W, H), 0)
        ImageDraw.Draw(shadow).rounded_rectangle([x, y + 24, x + w, y + h + 24], 26, fill=120)
        shadow = shadow.filter(ImageFilter.GaussianBlur(34))
        canvas.paste(Image.new("RGB", (W, H), (30, 34, 70)), mask=shadow)
        im, mask = _rounded(im, 22)
        canvas.paste(im, (x, y), mask)

    place(back, 90, 80, 1560)
    place(front, 900, 380, 1400)
    save_jpg(canvas, path, quality=88)


def build_architecture(path: Path) -> None:
    W, H = 2000, 1420
    img = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(img)
    hb = font("cjk_bold", 30)
    h2 = font("cjk_bold", 22)
    tx = font("cjk", 18)
    mono = font("mono", 17)

    def box(x: int, y: int, w: int, h: int, fill: tuple[int, int, int], title: str, sub: str = "", fg=(255, 255, 255)) -> None:
        d.rounded_rectangle([x + 3, y + 5, x + w + 3, y + h + 5], 18, fill=(225, 227, 235))
        d.rounded_rectangle([x, y, x + w, y + h], 18, fill=fill)
        tw = d.textlength(title, font=h2)
        d.text((x + (w - tw) / 2, y + (16 if sub else (h - 28) / 2)), title, font=h2, fill=fg)
        if sub:
            for i, ln in enumerate(sub.split("\n")):
                sw = d.textlength(ln, font=tx)
                d.text((x + (w - sw) / 2, y + 52 + i * 26), ln, font=tx, fill=fg if fg != (255, 255, 255) else (235, 240, 255))

    def band(y: int, label: str) -> None:
        d.text((60, y), label, font=font("cjk_bold", 17), fill=(120, 124, 140))

    d.text((60, 40), "NeoAgent Studio v3.5 — 生产级架构总览", font=hb, fill=(28, 28, 30))
    d.text((60, 86), "CLI × Studio 双端体验 × 推理-行动 Harness × 契约化需求分解 × 三层代码规范栈 × 治理型检视协议 × Harness L1–L7 × 渐进式 Skill 仓库", font=tx, fill=(110, 114, 128))

    band(140, "交互层 SURFACES")
    surf = [
        ("NeoAgent CLI", "neo · Rich 流式\n<think> 折叠渲染", (0, 122, 255)),
        ("NeoAgent Studio", "neo-desktop · iOS × Codex 设计\nSSE 流式 · 深浅双色", (88, 86, 214)),
        ("DevOps CLI", "neo-devops · 27 子命令\n--json 机器可读", (52, 199, 89)),
        ("IDE / Webhook", "neoagent-mcp (MCP · 16 工具)\nFastAPI 服务", (255, 149, 0)),
    ]
    for i, (t, s, c) in enumerate(surf):
        box(60 + i * 475, 170, 445, 120, c, t, s)

    band(320, "推理层 REASONING-ACTION HARNESS")
    box(60, 350, 1880, 100, (28, 28, 30), "ThinkStreamParser  ·  跨轮 CoT 折叠  ·  HarnessRoleRouter (reasoner / chat)  ·  并发工具 + CapabilityToken", "")

    band(480, "治理层 HARNESS ENGINEERING L1–L7")
    layers = [
        ("L1", "信息边界\n四层仓库", (0, 122, 255)),
        ("L2", "工具输出\n蒸馏", (48, 176, 199)),
        ("L3", "三条件\n子代理门禁", (88, 86, 214)),
        ("L4", "40% 上下文\nContext Reset", (52, 199, 89)),
        ("L5", "独立性实测\n检视协议", (255, 149, 0)),
        ("L6", "反作弊 · DLP\nSHA-256 账本", (255, 59, 48)),
        ("L7", "SAFE\n自演进", (175, 82, 222)),
    ]
    for i, (t, s, c) in enumerate(layers):
        box(60 + i * 270, 510, 250, 140, c, t, s)

    band(680, "规格层 SPEC-TO-CODE BLUEPRINT · 3-LAYER CODING STANDARDS STACK")
    bp = ["SPEC", "DESIGN", "PLAN", "RED/GREEN TDD", "S/D/C", "ACCEPT"]
    bx = 60
    for i, st in enumerate(bp):
        w = 270
        d.rounded_rectangle([bx, 712, bx + w, 772], 30, fill=(255, 255, 255), outline=(88, 86, 214), width=3)
        tw = d.textlength(st, font=mono)
        d.text((bx + (w - tw) / 2, 732), st, font=mono, fill=(28, 28, 30))
        if i < len(bp) - 1:
            d.polygon([(bx + w + 6, 735), (bx + w + 24, 742), (bx + w + 6, 749)], fill=(150, 155, 170))
        bx += w + 30
    d.text((60, 786), "契约化需求分解：EARS + G/W/T → 6 段接口契约 + TC-* → 微任务 DAG → 隔离式 Red/Green 会话 → 追溯矩阵合规 → 黑盒验收与干净归档；Spec/Design/Plan 三道只读闸门（G1–G9 · G1–G15 · P1–P6）", font=tx, fill=(110, 114, 128))
    d.text((60, 818), "三层规范栈：5D 底线 + 7 级最小代码阶梯 + 9 种语言护栏 → 编译进编码提示词；CleanCodeReviewer 12 条确定性规则 + 旧侧归因强制执行", font=tx, fill=(110, 114, 128))

    band(870, "执行层 CLOSED-LOOP DEVOPS ENGINE · GOVERNED REVIEW PROTOCOL")
    stages = ["INTAKE", "BUILD", "REVIEW", "PIPELINE", "RELEASE", "OBSERVE", "LEARN"]
    sx = 60
    for i, st in enumerate(stages):
        w = 228
        d.rounded_rectangle([sx, 902, sx + w, 972], 35, fill=(255, 255, 255), outline=(210, 214, 225), width=2)
        tw = d.textlength(st, font=mono)
        d.text((sx + (w - tw) / 2, 926), st, font=mono, fill=(28, 28, 30))
        if i < len(stages) - 1:
            d.polygon([(sx + w + 8, 930), (sx + w + 26, 937), (sx + w + 8, 944)], fill=(150, 155, 170))
        sx += w + 46
    d.text((60, 986), "REVIEW 检视协议：归因（引入 / 既有 / 已修复）· 证据落地 · 独立性证明 · 决策简报   PIPELINE 有界自愈 · 金丝雀 5%→25%→100% 自动回滚 · 反馈派生 HOTFIX", font=tx, fill=(110, 114, 128))

    band(1040, "能力层 KNOWLEDGE & SAFETY")
    box(60, 1070, 610, 150, (255, 255, 255), "三级渐进式 Skill Registry", "Tier1 索引 → Tier2 SKILL.md → Tier3 references\n阶段 / 语言路由 · F1–F4 校验", fg=(28, 28, 30))
    box(695, 1070, 610, 150, (255, 255, 255), "DLP 隐私引擎", "出站脱敏 redact_for_llm · 静态检视阻断\n全仓 privacy-scan 门禁", fg=(28, 28, 30))
    box(1330, 1070, 610, 150, (255, 255, 255), "防篡改账本 & 交接产物", ".neoagent-devops/ · SHA-256 哈希链\ntask/fix-brief · SARIF · blueprint/*.md", fg=(28, 28, 30))

    d.text((60, 1270), "零外部依赖控制平面：Python 3.11+ stdlib (ThreadingHTTPServer + SSE)；LangGraph / FastAPI / Docker 为可选数据面。", font=tx, fill=(110, 114, 128))
    d.text((60, 1302), "无 API Key 时自动降级为本地确定性 Harness 引擎（静态 / AST / 反作弊 / 隐私检查 / 蓝图合成），内网离线可用。", font=tx, fill=(110, 114, 128))
    save_jpg(img, path, quality=92)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui-frames", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "assets")
    ap.add_argument("--skip-cli", action="store_true")
    ap.add_argument("--skip-quality", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.ui_frames:
        build_ui_assets(args.ui_frames, args.out)
    if not args.skip_cli:
        build_cli_assets(args.out)
        build_review_protocol_assets(args.out)
        build_blueprint_assets(args.out)
    if not args.skip_quality:
        build_quality_asset(args.out)
    build_architecture(args.out / "architecture.jpg")
    build_blueprint_pipeline(args.out / "blueprint-pipeline.jpg")
    build_quality_stack_diagram(args.out / "quality-stack-diagram.jpg")


if __name__ == "__main__":
    main()
