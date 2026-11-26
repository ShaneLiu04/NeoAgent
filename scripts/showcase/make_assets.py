"""Build README showcase assets (GIF / JPG) from real captures.

    python scripts/showcase/make_assets.py --ui-frames <capture_dir> [--out docs/assets]

* UI scenes  : frames recorded by ``capture_ui.mjs`` -> animated GIF + JPG stills + hero composite
* CLI scenes : a real ``neo-devops`` session (``cli_story.py``) rendered as a terminal GIF / JPG
* Quality    : real ``pytest`` / ``ruff`` / ``privacy-scan`` output of this repository
* Diagram    : layered architecture infographic
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
    W, H = 2000, 1240
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
    d.text((60, 86), "CLI × Studio 双端体验 × 推理-行动 Harness × 治理型检视协议 × Harness L1–L7 × 渐进式 Skill 仓库", font=tx, fill=(110, 114, 128))

    band(140, "交互层 SURFACES")
    surf = [
        ("NeoAgent CLI", "neo · Rich 流式\n<think> 折叠渲染", (0, 122, 255)),
        ("NeoAgent Studio", "neo-desktop · iOS × Codex 设计\nSSE 流式 · 深浅双色", (88, 86, 214)),
        ("DevOps CLI", "neo-devops · 22 子命令\n--json 机器可读", (52, 199, 89)),
        ("IDE / Webhook", "neoagent-mcp (MCP)\nFastAPI 服务", (255, 149, 0)),
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

    band(680, "执行层 CLOSED-LOOP DEVOPS ENGINE · GOVERNED REVIEW PROTOCOL")
    stages = ["INTAKE", "BUILD", "REVIEW", "PIPELINE", "RELEASE", "OBSERVE", "LEARN"]
    sx = 60
    for i, st in enumerate(stages):
        w = 228
        d.rounded_rectangle([sx, 712, sx + w, 782], 35, fill=(255, 255, 255), outline=(210, 214, 225), width=2)
        tw = d.textlength(st, font=mono)
        d.text((sx + (w - tw) / 2, 736), st, font=mono, fill=(28, 28, 30))
        if i < len(stages) - 1:
            d.polygon([(sx + w + 8, 740), (sx + w + 26, 747), (sx + w + 8, 754)], fill=(150, 155, 170))
        sx += w + 46
    d.text((60, 796), "REVIEW 检视协议：归因（引入 / 既有 / 已修复）· 证据落地 · 独立性证明 · 决策简报   PIPELINE 有界自愈 · 金丝雀 5%→25%→100% 自动回滚 · 反馈派生 HOTFIX", font=tx, fill=(110, 114, 128))

    band(850, "能力层 KNOWLEDGE & SAFETY")
    box(60, 880, 610, 150, (255, 255, 255), "三级渐进式 Skill Registry", "Tier1 索引 → Tier2 SKILL.md → Tier3 references\n阶段 / 语言路由 · F1–F4 校验", fg=(28, 28, 30))
    box(695, 880, 610, 150, (255, 255, 255), "DLP 隐私引擎", "出站脱敏 redact_for_llm · 静态检视阻断\n全仓 privacy-scan 门禁", fg=(28, 28, 30))
    box(1330, 880, 610, 150, (255, 255, 255), "防篡改账本 & 交接产物", ".neoagent-devops/ · SHA-256 哈希链\ntask/fix-brief · SARIF · srs-draft", fg=(28, 28, 30))

    d.text((60, 1080), "零外部依赖控制平面：Python 3.11+ stdlib (ThreadingHTTPServer + SSE)；LangGraph / FastAPI / Docker 为可选数据面。", font=tx, fill=(110, 114, 128))
    d.text((60, 1112), "无 API Key 时自动降级为本地确定性 Harness 引擎（静态 / AST / 反作弊 / 隐私检查），内网离线可用。", font=tx, fill=(110, 114, 128))
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
    if not args.skip_quality:
        build_quality_asset(args.out)
    build_architecture(args.out / "architecture.jpg")


if __name__ == "__main__":
    main()
