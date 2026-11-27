"""The control plane (Studio server, neo-devops, MCP, skills) must import with the stdlib alone."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROBE = r"""
import importlib, importlib.abc, sys

STD = set(sys.stdlib_module_names)


class BlockThirdParty(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        base = name.split(".")[0]
        if base in STD or base in ("neoagent", "_distutils_hack"):
            return None
        raise ModuleNotFoundError(f"third-party import on control-plane path: {name}")


sys.meta_path.insert(0, BlockThirdParty())
workdir, modules = sys.argv[1], sys.argv[2:]
for mod in modules:
    importlib.import_module(mod)
from neoagent.cli.devops_cli import run_cli
assert run_cli(["--root", workdir, "init"]) == 0
assert run_cli(["--root", workdir, "--json", "status"]) == 0
print("ok")
"""

CONTROL_PLANE = [
    "neoagent.devops.engine",
    "neoagent.devops.blueprint",
    "neoagent.devops.standards",
    "neoagent.cli.devops_cli",
    "neoagent.desktop.server",
    "neoagent.mcp.server",
    "neoagent.skills.registry",
]


def test_control_plane_runs_with_stdlib_only(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path), *CONTROL_PLANE],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip().endswith("ok")
