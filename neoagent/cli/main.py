"""Rich terminal UI for NeoAgent — streaming markdown, syntax highlighting.

CyberClaw's CLI uses prompt_toolkit with a spinner — users wait blindly
for 20+ seconds. This CLI shows real-time token streaming with Rich
markdown rendering, colored tool calls, and split-panel layout.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import re
import time
from pathlib import Path

# Fix Windows terminal encoding for emoji / unicode support
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass  # Older Python or restricted stdout

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from dotenv import load_dotenv

# Ensure .env is loaded from project root before config is cached
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

from ..core.agent import AgentEvent, AgentEventKind, NeoAgent
from ..core.config import get_config
from ..core.observability import get_observability
from ..core.orchestrator import OrchestratorConfig
from ..core.tools.builtins import list_scheduled_tasks
from ..core.tools.scheduler import get_scheduler
from . import pet


console = Console()


def print_banner():
    """Display the NeoAgent banner in red-purple gradient style."""
    # Original ASCII art — top half in red, bottom half in purple
    lines = [
        ("███╗   ██╗███████╗ ██████╗      █████╗  ██████╗ ███████╗███╗   ██╗████████╗", "bold #ff3366"),
        ("████╗  ██║██╔════╝██╔═══██╗    ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝", "bold #ff3366"),
        ("██╔██╗ ██║█████╗  ██║   ██║    ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║",   "bold #cc3399"),
        ("██║╚██╗██║██╔══╝  ██║   ██║    ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║",   "bold #9933cc"),
        ("██║ ╚████║███████╗╚██████╔╝    ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║",   "bold #9933cc"),
        ("╚═╝  ╚═══╝╚══════╝ ╚═════╝     ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝",   "bold #9933cc"),
    ]

    for text, style in lines:
        console.print(Text(text, style=style))

    console.print("[dim]Production-Grade Async Agent Framework v2.0.0 — Async Streaming · True Sandbox · Multi-Agent[/dim]")


def _extract_parallel_topics(text: str) -> list[str] | None:
    """Detect if user wants parallel research on multiple topics."""
    import re

    # Pattern 1: list after colon (e.g. "三个话题：A、B、C")
    m = re.search(
        r"(?:同时|分别|一起|并行)?.*?(?:研究|分析|比较|对比|了解|说说|聊聊).*?(?:话题|主题|方面|问题|技术|框架|语言)[：:]\s*([^\n。；;]+?)(?:[,，。；;]|然后|最后|并|$)",
        text,
    )
    if m:
        raw = m.group(1)
        raw = raw.replace("、", ",").replace("，", ",")
        parts = [p.strip().strip("'\"") for p in raw.split(",") if p.strip()]
        parts = [p for p in parts if len(p) > 2]
        if len(parts) >= 2:
            return parts

    # Pattern 2: conjunctive list (e.g. "A、B 和 C")
    m = re.search(
        r"([^,，、]{3,}?)(?:[,，、和与跟]|以及)([^,，、]{3,}?)(?:[,，、和与跟]|以及)([^,，、]{3,}?)",
        text,
    )
    if m:
        topics = [g.strip() for g in m.groups()]
        topics = [t for t in topics if len(t) > 2]
        if len(topics) >= 2:
            return topics

    return None


def _is_image_path(text: str) -> str | None:
    """Check if user input is a direct image file path."""
    import re

    patterns = [
        r"^[a-zA-Z]:\\[^:*?\"<>|]+\.(png|jpg|jpeg|gif|webp|bmp)$",
        r"^[/~][^:*?\"<>|]+\.(png|jpg|jpeg|gif|webp|bmp)$",
    ]
    for pat in patterns:
        m = re.match(pat, text.strip(), re.IGNORECASE)
        if m:
            return text.strip()
    return None


async def interactive_chat():
    """Run the interactive chat loop with streaming output."""
    print_banner()

    # Clear config cache to pick up .env changes
    get_config.cache_clear()

    cfg = get_config()
    provider = cfg.llm.default_provider
    model = cfg.llm.default_model

    console.print(f"[dim]Provider: {provider} | Model: {model}[/dim]")
    console.print(f"[dim]Type /help for commands, /exit to quit[/dim]\n")

    agent = NeoAgent(provider=provider, model=model)
    obs = get_observability()
    scheduler = get_scheduler()
    scheduler.start()
    restored = scheduler.load_tasks()
    if restored:
        console.print(f"[dim]Restored {restored} scheduled task(s) from disk.[/dim]")

    # Register terminal alert callback for triggered tasks
    def _on_task_trigger(task_id: str, description: str) -> None:
        from rich.panel import Panel
        from rich.text import Text
        panel = Panel(
            Text(f"⏰ {description}", style="bold yellow"),
            title=f"[bold red]任务提醒[/bold red]",
            border_style="bright_red",
            padding=(1, 2),
        )
        console.print(panel)
        # Also ring the terminal bell
        console.print("\a", end="")

    scheduler.on_trigger(_on_task_trigger)

    # Prompt session
    prompt_style = Style.from_dict({
        "prompt": "bold cyan",
        "input": "white",
    })

    session = PromptSession(style=prompt_style)

    try:
        # Greeting easter egg
        console.print(f"[dim italic]{pet.get_random_greeting()}[/dim italic]\n")

        while True:
            try:
                user_input = await session.prompt_async(
                    HTML("<prompt>❯ </prompt>"),
                    multiline=False,
                )
            except (KeyboardInterrupt, EOFError):
                console.print(f"\n[dim]{pet.get_random_farewell()}[/dim]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.lower() in ("/exit", "/quit", "/q"):
                console.print("[dim]Shutting down...[/dim]")
                break

            if user_input.lower() == "/help":
                console.print(help_text())
                continue

            if user_input.lower() == "/metrics":
                metrics = obs.get_cost_summary()
                console.print(f"[dim]Cost: ${metrics['total_cost_usd']:.4f} | "
                             f"Tokens: {metrics['total_tokens']:,} | "
                             f"Requests: {metrics['total_requests']}[/dim]")
                continue

            if user_input.lower() in ("/clear", "cls", "clear"):
                console.clear()
                print_banner()
                agent.reset_session()
                console.print("[dim]Screen and conversation history cleared.[/dim]")
                continue

            if user_input.lower() == "/tasks":
                try:
                    result = await list_scheduled_tasks.ainvoke({})
                except Exception as e:
                    result = f"Error listing tasks: {e}"
                console.print(result)
                continue

            # ── Multi-agent orchestration (natural-language trigger) ──
            parallel_topics = _extract_parallel_topics(user_input)
            if parallel_topics:
                console.print(
                    f"[dim]Orchestrator: detected {len(parallel_topics)} parallel topics — "
                    f"{', '.join(parallel_topics)}[/dim]\n"
                )
                from ..core.orchestrator import AgentOrchestrator
                from ..core.orchestrator import SubAgentRole
                from ..core.orchestrator import SubAgentTask

                orchestrator = AgentOrchestrator(
                    OrchestratorConfig(default_model=cfg.llm.default_model)
                )
                tasks = [
                    SubAgentTask(
                        id=f"t{i+1}",
                        prompt=f"请深入研究以下话题，提供详尽的技术细节、核心概念、优缺点和典型代码示例：{topic}",
                        role=SubAgentRole.RESEARCHER,
                    )
                    for i, topic in enumerate(parallel_topics)
                ]

                with console.status(
                    "[bold yellow]Running parallel research sub-agents...[/bold yellow]"
                ) as status:
                    results = await orchestrator.run_parallel(tasks)

                research_blocks: list[str] = []
                for r in results:
                    if r.success:
                        research_blocks.append(
                            f"### {r.task_id} ({r.role.value})\n{r.output}"
                        )
                    else:
                        research_blocks.append(
                            f"### {r.task_id} ({r.role.value})\nError: {r.error}"
                        )

                # Rewrite user_input so the main agent synthesizes a final report
                user_input = (
                    f"Original request: {user_input}\n\n"
                    "Below are parallel research results from sub-agents. "
                    "Please synthesize them into ONE comprehensive Chinese report with clear sections, "
                    "comparison tables, and a final recommendation. "
                    "Do NOT mention that sub-agents were used.\n\n"
                    + "\n\n".join(research_blocks)
                )

            # ── Image path shortcut (bypass agent reasoning) ──
            image_path = _is_image_path(user_input)
            if image_path:
                console.print(f"[dim]Detected image: {image_path}[/dim]\n")
                from ..core.tools.vision import analyze_image

                with console.status(
                    pet.pet_text("thinking", 0),
                    spinner="dots",
                    spinner_style="green",
                ) as status:
                    result = await analyze_image.ainvoke(
                        {"image_path": image_path, "question": "Describe this image in detail."}
                    )
                console.print(result)
                continue

            # Print a separator line before the response
            console.print()

            # Stream response — accumulate silently with a status spinner,
            # then render final Markdown once. Cleanest and most stable.
            response_parts: list[str] = []
            tool_count = 0
            start_time = time.time()
            md_content = ""
            final_md_content = ""
            stats_line = ""
            pet_frame = 0
            pet_state = "thinking"
            cached_pet_state = "thinking"
            cached_pet_line: str | None = None
            last_pet_update = time.time()

            def _get_pet_line(state: str) -> str:
                """Return a cached line for the current state; only re-roll when state changes."""
                nonlocal cached_pet_state, cached_pet_line
                if state != cached_pet_state or cached_pet_line is None:
                    cached_pet_state = state
                    import random
                    lines = pet.PET_STATES.get(state, pet.PET_STATES["idle"])
                    cached_pet_line = random.choice(lines)
                return cached_pet_line

            try:
                with console.status(
                    pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                    spinner="dots",
                    spinner_style="green",
                ) as status:
                    async for event in agent.stream(user_input):
                        if event.kind == AgentEventKind.TOKEN:
                            token = str(event.data)
                            token = token.encode("utf-8", errors="ignore").decode("utf-8")
                            response_parts.append(token)
                            md_content += token
                            pet_state = "writing"
                            pet_frame += 1
                            # Update pet text every 3s so the line doesn't switch frantically
                            now = time.time()
                            if now - last_pet_update >= 3.0:
                                status.update(
                                    pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                                    spinner="dots",
                                    spinner_style="green",
                                )
                                last_pet_update = now

                        elif event.kind == AgentEventKind.TOOL_CALL_START:
                            tool_count += 1
                            name = event.data.get("name", "unknown") if isinstance(event.data, dict) else "unknown"
                            pet_state = pet.state_from_event("tool_call_start", name)
                            pet_frame += 1
                            status.update(
                                pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                                spinner="dots",
                                spinner_style="green",
                            )
                            last_pet_update = time.time()
                            console.print(f"[yellow]  ⚙ {name}[/yellow]")

                        elif event.kind == AgentEventKind.TOOL_CALL_END:
                            pet_state = "writing"
                            pet_frame += 1
                            status.update(
                                pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                                spinner="dots",
                                spinner_style="green",
                            )
                            last_pet_update = time.time()
                            console.print("[green]  ✓ done[/green]")

                        elif event.kind == AgentEventKind.ERROR:
                            pet_state = "error"
                            pet_frame += 1
                            status.update(
                                pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                                spinner="dots",
                                spinner_style="green",
                            )
                            last_pet_update = time.time()
                            console.print(f"\n[red]  **Error:** {event.data}[/red]\n")

                        elif event.kind == AgentEventKind.MESSAGE_COMPLETE:
                            elapsed = (time.time() - start_time) * 1000
                            budget_info = event.data.get("budget", "") if isinstance(event.data, dict) else ""
                            info_parts = [f"{elapsed:.0f}ms"]
                            if tool_count:
                                info_parts.append(f"{tool_count} tool(s)")
                            if budget_info:
                                info_parts.append(budget_info)
                            stats_line = f"[dim]── {' · '.join(info_parts)} ──[/dim]\n"
                            final_md_content = md_content
                            md_content = ""
                            response_parts = []
                            pet_state = "complete"
                            pet_frame += 1
                            status.update(
                                pet.pet_text(pet_state, pet_frame, line=_get_pet_line(pet_state), show_spinner=False),
                                spinner="dots",
                                spinner_style="green",
                            )
                            last_pet_update = time.time()

            except KeyboardInterrupt:
                console.print(f"\n[dim]{pet.get_random_farewell()}[/dim]\n")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                console.print(f"\n[bold red]Error: {e}[/bold red]\n")
            else:
                if final_md_content.strip():
                    sanitized = final_md_content.encode("utf-8", errors="ignore").decode("utf-8")
                    if "**" in sanitized:
                        sanitized = re.sub(r'\*\*([《「『（【"'"'"'‘])', '**\u200B\\1', sanitized)
                        sanitized = re.sub(r'([》」』）】"'"'"'’])\*\*', '\\1\u200B**', sanitized)
                    md_render = Markdown(sanitized, code_theme="monokai")
                    console.print(Panel(
                        md_render,
                        border_style="bright_blue",
                        padding=(1, 2),
                    ))
                if stats_line:
                    console.print(stats_line)

    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            obs.shutdown()
        except Exception:
            pass
        if sys.platform == "win32":
            # Reset Windows terminal mode on exit (prompt_toolkit raw mode recovery)
            os.system("")


def help_text() -> str:
    return """[bold]Commands:[/bold]
  /exit, /quit, /q  — Exit NeoAgent
  /help             — Show this help
  /metrics          — Show usage and cost metrics
  /tasks            — List scheduled reminders/tasks
  /clear            — Clear the screen

[bold]Tips:[/bold]
  • Use [italic]web_search[/italic] to find current information
  • Use [italic]run_python[/italic] to execute code in a sandbox
  • Use [italic]analyze_image[/italic] to analyze images (multimodal models)
  • Schedule reminders with natural language
  • Multi-agent: ask for parallel research or analysis
"""


def main():
    """CLI entry point."""
    # Clear cached config to ensure .env changes are picked up
    get_config.cache_clear()
    try:
        asyncio.run(interactive_chat())
    except KeyboardInterrupt:
        pass
    finally:
        # Force flush stdout/stderr before exit (helps Windows terminal recovery)
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    main()
