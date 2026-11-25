"""Serve NeoAgent Studio against the synthetic demo workspace (for recording)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_workspace import build_demo_workspace, install_scripted_coder  # noqa: E402

from neoagent.desktop.server import DesktopControlServer  # noqa: E402
from neoagent.skills.registry import SkillRegistry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    repo, skills_dir = build_demo_workspace()
    install_scripted_coder()
    reg = SkillRegistry(roots=[skills_dir])
    srv = DesktopControlServer(workspace_root=repo, port=args.port, skill_registry=reg)
    url = srv.start_background()
    print(f"READY {url} skills={reg.count()}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
