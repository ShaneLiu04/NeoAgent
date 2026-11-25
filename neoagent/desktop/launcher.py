"""Cross-Platform Native Desktop Launcher (`neo-desktop`).

Auto-selects the highest-fidelity desktop shell available on the engineer's OS:
  1. Electron Desktop Shell (`desktop/` if `electron` binary is installed)
  2. Native OS WebView (`pywebview` — WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux)
  3. Frameless Chromium/Edge Native App Window (`msedge --app=...` / `chrome --app=...`)
  4. System default browser fallback (or `--headless` server mode)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

from .server import create_desktop_server


def _find_chromium_app_binary() -> str | None:
    """Locate Edge or Chrome binary capable of frameless `--app=<url>` desktop window mode."""
    candidates = [
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ]
    if sys.platform == "win32":
        prog_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.extend(
            [
                str(Path(prog_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(prog) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(prog) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )

    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def launch_desktop(
    *,
    workspace_root: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    shell_mode: str = "auto",
    headless: bool = False,
) -> int:
    root = Path(workspace_root or Path.cwd()).resolve()
    srv = create_desktop_server(workspace_root=root, host=host, port=port)
    url = srv.start_background()

    print(f"NeoAgent Studio control plane listening at {url}")
    print(f"Workspace: {root}")

    try:
        if headless or shell_mode == "headless":
            print("Running in headless daemon mode. Press Ctrl+C to stop.")
            while True:
                time.sleep(1.0)

        # Tier 1: pywebview native OS window if installed
        if shell_mode in ("auto", "webview"):
            try:
                import webview  # type: ignore[import-not-found]

                webview.create_window(
                    "NeoAgent Studio",
                    url,
                    width=1440,
                    height=900,
                    min_size=(1024, 680),
                )
                webview.start()
                return 0
            except ImportError:
                if shell_mode == "webview":
                    raise

        # Tier 2: Frameless Chromium / Edge native app window (`--app=...`)
        if shell_mode in ("auto", "app"):
            browser_bin = _find_chromium_app_binary()
            if browser_bin:
                profile_dir = Path(tempfile.gettempdir()) / "neoagent-studio-window"
                profile_dir.mkdir(parents=True, exist_ok=True)
                proc = subprocess.Popen(
                    [
                        browser_bin,
                        f"--app={url}",
                        "--window-size=1440,900",
                        f"--user-data-dir={profile_dir}",
                        "--no-first-run",
                        "--disable-extensions",
                    ]
                )
                proc.wait()
                return 0

        # Tier 3: Default system browser + keep server alive
        webbrowser.open(url)
        print("Opened NeoAgent Studio in system browser. Press Ctrl+C to exit.")
        while True:
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nShutting down NeoAgent Studio...")
        return 0
    finally:
        srv.shutdown()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="neo-desktop",
        description="Launch the NeoAgent Studio desktop app",
    )
    p.add_argument("--root", default=".", help="Project workspace root directory")
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=0, help="Bind port (0 = auto-pick free port)")
    p.add_argument(
        "--shell",
        choices=["auto", "webview", "app", "browser", "headless"],
        default="auto",
        help="Desktop window container backend",
    )
    p.add_argument("--headless", action="store_true", help="Run server without opening a GUI window")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return launch_desktop(
        workspace_root=args.root,
        host=args.host,
        port=args.port,
        shell_mode=args.shell,
        headless=args.headless,
    )
