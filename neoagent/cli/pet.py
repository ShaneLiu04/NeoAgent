"""Cyber-pet companion for the NeoAgent CLI.

Provides dynamic visual feedback and entertaining lines during agent
operations, inspired by Claude Code's crab mascot.

Theme: The Matrix / Cyberpunk — our little AI sprite "NEO" lives in the
terminal and reacts to everything the agent does.
"""

from __future__ import annotations

import random

from rich.text import Text

# ── State definitions ──

PET_STATES: dict[str, list[str]] = {
    "idle": [
        "NEO standing by...",
        "The construct is ready.",
        "Waiting for the signal...",
        "Reality is just a simulation.",
        "Jacked in, standing by.",
    ],
    "thinking": [
        "Loading construct program...",
        "Dodging bullets in the code...",
        "There is no spoon, only algorithms.",
        "The Matrix is telling my brain it's delicious.",
        "Deconstructing the problem...",
        "Running neural simulation...",
        "Optimizing the reality kernel...",
        "Consulting the Oracle...",
    ],
    "searching": [
        "Scanning the construct for data...",
        "There's no place I can't go.",
        "Downloading knowledge from the Source...",
        "Tracing packets through the Matrix...",
        "Jacking into the information stream...",
        "Browsing the infinite library...",
        "Spidering the web — literally.",
    ],
    "coding": [
        "Executing sandbox protocol...",
        "There is no bug.",
        "Watch me code. Whoa.",
        "Compiling reality...",
        "Running with scissors in a Docker container...",
        "Injecting caffeine into the compiler...",
        "Syntax error? I don't even know her.",
        "Writing code that would make the Architect proud.",
    ],
    "writing": [
        "Assembling the narrative...",
        "Typing faster than bullets fly...",
        "Streaming bytes of wisdom...",
        "The pen is mightier than the bug.",
        "Generating words of power...",
        "Weaving the digital tapestry...",
    ],
    "complete": [
        "Mission accomplished. Neo out.",
        "The prophecy has been fulfilled.",
        "Choice is an illusion, but good code is real.",
        "You are the One... who asked the right question.",
        "Another glitch in the Matrix, solved.",
        "Reality successfully modified.",
    ],
    "error": [
        "Glitch in the Matrix detected.",
        "Déjà vu... something's wrong.",
        "Agent Smith is interfering.",
        "System anomaly. Reloading...",
        "I can taste the code... and it's bitter.",
        "Critical failure in sector 7.",
    ],
}

GREETINGS: list[str] = [
    "Wake up, Neo... The Matrix has you.",
    "Follow the white rabbit.",
    "I know kung fu... and Python.",
    "The answer is out there, Neo. It's looking for you.",
    "Knock, knock, Neo.",
    "Free your mind.",
]

FAREWELLS: list[str] = [
    "Goodbye, Neo. We'll meet again in the Matrix.",
    "Remember: all I'm offering is the truth.",
    "Wake up when you're ready.",
    "The construct is closing. See you on the other side.",
    "Don't forget to take the red pill next time.",
]

# Spinner frame cycle for the matrix-green feel
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_STYLE_MAP: dict[str, str] = {
    "idle": "dim",
    "thinking": "bold cyan",
    "searching": "bold yellow",
    "coding": "bold green",
    "writing": "bold cyan",
    "complete": "bold green",
    "error": "bold red",
}


# ── Public API ──


def get_random_greeting() -> str:
    """Return a random startup greeting."""
    return random.choice(GREETINGS)


def get_random_farewell() -> str:
    """Return a random shutdown farewell."""
    return random.choice(FAREWELLS)


def pet_text(state: str, frame: int = 0, line: str | None = None, show_spinner: bool = True) -> Text:
    """Build a Rich Text object for the given pet state.

    Args:
        state: One of the PET_STATES keys.
        frame: Animation frame counter (cycles the spinner char).
        line: Optional fixed line to display. When None, a random line is chosen.
              Callers can cache the returned line to avoid rapid text switching.
        show_spinner: When False, the Braille spinner char is omitted. Useful when
              the caller (e.g. console.status) already provides its own spinner.
    """
    lines = PET_STATES.get(state, PET_STATES["idle"])
    if line is None:
        line = random.choice(lines)
    style = _STYLE_MAP.get(state, "dim")

    text = Text()
    if show_spinner:
        spinner = _SPINNER[frame % len(_SPINNER)]
        text.append(f"{spinner} ", style="bold green")
    text.append(line, style=style)
    return text


def state_from_event(event_kind: str, tool_name: str = "") -> str:
    """Map an AgentEventKind to a pet state.

    Args:
        event_kind: AgentEventKind value.
        tool_name: Tool name (for tool_call events).
    """
    kind = str(event_kind).lower()
    if "error" in kind:
        return "error"
    if "tool_call_start" in kind:
        if "search" in tool_name.lower():
            return "searching"
        if tool_name.lower() in ("run_python", "run_shell"):
            return "coding"
        return "thinking"
    if "tool_call_end" in kind:
        return "writing"
    if "message_complete" in kind:
        return "complete"
    if "token" in kind:
        return "writing"
    return "thinking"
