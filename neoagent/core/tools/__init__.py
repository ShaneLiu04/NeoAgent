"""NeoAgent tool registry — collects all built-in and dynamic tools."""

from langchain_core.tools import BaseTool

from .builtins import (
    calculator,
    delete_scheduled_task,
    get_current_time,
    get_system_info,
    list_scheduled_tasks,
    modify_scheduled_task,
    save_user_profile,
    schedule_task,
)
from .executor import run_python, run_shell
from .fileops import delete_file, list_files, read_file, write_file
from .search import web_search
from .browser import browse_webpage
from .vision import analyze_image
from .orchestrator_tool import delegate_to_subagents

# Core built-in tools — always available
BUILTIN_TOOLS: list[BaseTool] = [
    get_current_time,
    calculator,
    get_system_info,
    save_user_profile,
    list_files,
    read_file,
    write_file,
    delete_file,
    run_python,
    run_shell,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
    modify_scheduled_task,
    web_search,
    browse_webpage,
    analyze_image,
    delegate_to_subagents,
]


def get_all_tools(include_dynamic: bool = True) -> list[BaseTool]:
    """Get all available tools, optionally including dynamic skills."""
    tools = list(BUILTIN_TOOLS)
    if include_dynamic:
        try:
            from ..skill_loader import load_skills
            tools.extend(load_skills())
        except Exception:
            pass
    return tools
