"""Cross-platform desktop notifications for NeoAgent.

Tries platform-native mechanisms in order of preference:
  1. Third-party libraries (win10toast, plyer, etc.) if installed
  2. OS-specific CLI tools (notify-send, osascript, powershell toast)
  3. Blocking fallback (ctypes MessageBox on Windows, print elsewhere)

All notifications are non-blocking (fired via subprocess or thread)
so the scheduler loop never stalls.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
import threading

logger = logging.getLogger("neoagent.notify")


# ── Windows Toast via PowerShell (non-blocking) ──────────────────

_POWERSHELL_TOAST_XML = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null

$template = @"
<toast>
  <visual>
    <binding template='ToastGeneric'>
      <text>{title}</text>
      <text>{message}</text>
    </binding>
  </visual>
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("NeoAgent").Show($toast)
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""


def _strip_emoji(text: str) -> str:
    """Remove emoji and other non-BMP chars that break Windows cmdline encoding."""
    import re
    # Remove emoji ranges
    return re.sub(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002702-\U000027B0\U000024C2\U0001F251\U0000200D\U00002600-\U000026FF\U00002700-\U000027BF]",
        "",
        text,
    )


def _windows_powershell_toast(title: str, message: str) -> bool:
    """Try Windows 10/11 native toast via PowerShell."""
    try:
        safe_title = _strip_emoji(title).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_message = _strip_emoji(message).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ps_code = _POWERSHELL_TOAST_XML.format(title=safe_title, message=safe_message)
        # Write to a temp .ps1 file to avoid cmdline encoding issues with Unicode
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
            f.write(ps_code)
            ps_path = f.name
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", ps_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception as e:
        logger.debug(f"PowerShell toast failed: {e}")
        return False


def _windows_messagebox(title: str, message: str) -> bool:
    """Fallback: ctypes MessageBoxW (blocking, so run in subprocess)."""
    try:
        # Launch a tiny standalone python process so we don't block
        code = (
            "import ctypes; ctypes.windll.user32.MessageBoxW(0, "
            f"{message!r}, {title!r}, 0x40 | 0x0)"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception as e:
        logger.debug(f"MessageBox fallback failed: {e}")
        return False


# ── macOS ────────────────────────────────────────────────────────

def _macos_notify(title: str, message: str) -> bool:
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.debug(f"macOS osascript failed: {e}")
        return False


# ── Linux ────────────────────────────────────────────────────────

def _linux_notify(title: str, message: str) -> bool:
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=NeoAgent", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.debug(f"Linux notify-send failed: {e}")
        return False


# ── Public API ───────────────────────────────────────────────────

def send_notification(title: str, message: str) -> bool:
    """Send a non-blocking desktop notification.

    Returns True if a notification mechanism was successfully invoked.
    """
    system = platform.system()
    ok = False

    if system == "Windows" or system == "MINGW64_NT" or sys.platform == "win32":
        ok = _windows_powershell_toast(title, message) or _windows_messagebox(title, message)
    elif system == "Darwin":
        ok = _macos_notify(title, message)
    elif system == "Linux":
        ok = _linux_notify(title, message)

    if not ok:
        logger.warning(f"Could not send desktop notification: {title} — {message}")
    return ok


def notify_in_thread(title: str, message: str) -> None:
    """Fire notification in a background thread (totally non-blocking)."""
    threading.Thread(
        target=send_notification,
        args=(title, message),
        daemon=True,
    ).start()
