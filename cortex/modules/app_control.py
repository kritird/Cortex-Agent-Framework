"""AppControl — cross-platform application launch and control.

Two-path execution model
─────────────────────────
Primary   Scripting dictionary discovery
          macOS  : sdef XML → parsed commands/classes
          Windows: UI Automation tree or COM type-library introspection
          Linux  : AT-SPI / xdotool window info
          The summary is injected into the LLM prompt so it generates
          actions against the app's actual API.

Fallback  Screenshot vision loop
          When no scripting dictionary is found, take a screenshot, send it
          to a vision-capable LLM with the current task, execute the returned
          action, and repeat up to max_vision_steps times.

Every mutating action (launch, script, screenshot) requires HITL approval.
Read-only queries (get_running_apps, get_window_text) never prompt.
"""
import asyncio
import base64
import logging
import os
import platform
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cortex.exceptions import CortexHITLDeniedError

logger = logging.getLogger(__name__)

_PLATFORM = platform.system()   # 'Darwin' | 'Windows' | 'Linux'


# ── Capability discovery result ───────────────────────────────────────────────

@dataclass
class AppCapability:
    app_name: str
    type: str                   # "sdef" | "uia" | "com" | "none"
    summary: str                # compact human-readable for the LLM prompt
    raw: str = ""               # truncated raw output (for debug)
    app_path: str = ""          # resolved bundle / exe path (macOS/Windows)


# ── AppCapabilityScout ────────────────────────────────────────────────────────

class AppCapabilityScout:
    """
    Discover what automation interface an application exposes.

    macOS  : locate the .app bundle → run `sdef` → parse XML
    Windows: try COM type-library introspection; fall back to
             UI Automation tree dump via PowerShell
    Linux  : xdotool + AT-SPI atspi-info (best-effort)
    """

    def __init__(self, timeout_seconds: int = 15, sdef_max_chars: int = 8000):
        self._timeout = timeout_seconds
        self._sdef_max_chars = sdef_max_chars

    async def discover(self, app_name: str) -> AppCapability:
        """Return an AppCapability for app_name, or type='none' if nothing found."""
        if not app_name:
            return AppCapability(app_name="", type="none", summary="No app name provided.")

        if _PLATFORM == "Darwin":
            return await self._discover_macos(app_name)
        elif _PLATFORM == "Windows":
            return await self._discover_windows(app_name)
        else:
            return await self._discover_linux(app_name)

    # ── macOS ─────────────────────────────────────────────────────────────────

    async def _discover_macos(self, app_name: str) -> AppCapability:
        app_path = await self._find_app_path_macos(app_name)
        if not app_path:
            return AppCapability(
                app_name=app_name,
                type="none",
                summary=f"App '{app_name}' not found on this machine.",
            )

        sdef_xml = await self._run_sdef(app_path)
        if not sdef_xml:
            # App exists but has no scripting dictionary — note it and return none
            # so the caller falls back to the vision loop
            return AppCapability(
                app_name=app_name,
                type="none",
                app_path=app_path,
                summary=(
                    f"'{app_name}' found at {app_path} but has no AppleScript "
                    "scripting dictionary. Use System Events UI scripting or "
                    "vision loop."
                ),
            )

        summary = self._parse_sdef(app_name, sdef_xml)
        return AppCapability(
            app_name=app_name,
            type="sdef",
            app_path=app_path,
            summary=summary,
            raw=sdef_xml[:500],
        )

    async def _find_app_path_macos(self, app_name: str) -> Optional[str]:
        """Try mdfind spotlight, then common directories."""
        # mdfind: fastest — searches spotlight index
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind",
                f"kMDItemCFBundleIdentifier == '*{app_name}*'cd",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            paths = [p for p in stdout.decode().strip().splitlines() if p.endswith(".app")]
            if paths:
                return paths[0]
        except Exception:
            pass

        # mdfind by display name
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind",
                f"kMDItemDisplayName == '{app_name}'cd",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            paths = [p for p in stdout.decode().strip().splitlines() if p.endswith(".app")]
            if paths:
                return paths[0]
        except Exception:
            pass

        # Fallback: check common directories
        search_dirs = [
            "/Applications",
            "/System/Applications",
            "/System/Applications/Utilities",
            os.path.expanduser("~/Applications"),
        ]
        candidates = [app_name, f"{app_name}.app"]
        for d in search_dirs:
            for c in candidates:
                p = os.path.join(d, c)
                if os.path.exists(p):
                    return p

        return None

    async def _run_sdef(self, app_path: str) -> str:
        """Run `sdef <app_path>` and return raw XML, or '' on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "sdef", app_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            xml = stdout.decode("utf-8", errors="replace").strip()
            return xml if xml.startswith("<?xml") or xml.startswith("<dictionary") else ""
        except Exception:
            return ""

    def _parse_sdef(self, app_name: str, sdef_xml: str) -> str:
        """Convert raw sdef XML into a compact LLM-friendly summary."""
        try:
            root = ET.fromstring(sdef_xml)
        except ET.ParseError:
            return f"[sdef parse error for {app_name}]"

        commands: list[str] = []
        classes: list[str] = []

        for suite in root.iter("suite"):
            for cmd in suite.findall("command"):
                name = cmd.get("name", "")
                desc = (cmd.get("description") or "")[:80]
                if name:
                    commands.append(f"  {name}" + (f": {desc}" if desc else ""))
            for cls in suite.findall("class"):
                name = cls.get("name", "")
                props = [p.get("name", "") for p in cls.findall("property") if p.get("name")]
                if name:
                    prop_str = f" (properties: {', '.join(props[:8])})" if props else ""
                    classes.append(f"  {name}{prop_str}")

        lines = [
            f"AppleScript Scripting Dictionary — {app_name}",
            f"App path: {self._find_app_path_macos.__doc__}",   # placeholder, overwritten below
        ]
        lines = [f"AppleScript Scripting Dictionary — {app_name}"]

        if commands:
            lines.append(f"\nCommands ({len(commands)} total):")
            lines.extend(commands[:25])
            if len(commands) > 25:
                lines.append(f"  ... and {len(commands) - 25} more")

        if classes:
            lines.append(f"\nClasses ({len(classes)} total):")
            lines.extend(classes[:20])
            if len(classes) > 20:
                lines.append(f"  ... and {len(classes) - 20} more")

        lines.append(
            "\nNote: System Events UI scripting is always available for any app:\n"
            '  tell application "System Events" to tell process "{app_name}" to ...\n'
            "  (get buttons, click, keystroke, etc.)"
        )

        result = "\n".join(lines)
        return result[:self._sdef_max_chars]

    # ── Windows ───────────────────────────────────────────────────────────────

    async def _discover_windows(self, app_name: str) -> AppCapability:
        # Try UI Automation tree first (works for any GUI app)
        uia_summary = await self._get_uia_tree_windows(app_name)
        if uia_summary:
            return AppCapability(
                app_name=app_name,
                type="uia",
                summary=uia_summary,
                raw=uia_summary[:500],
            )

        # Try COM introspection (works for Office, IE, Shell, etc.)
        com_summary = await self._get_com_members_windows(app_name)
        if com_summary:
            return AppCapability(
                app_name=app_name,
                type="com",
                summary=com_summary,
                raw=com_summary[:500],
            )

        return AppCapability(
            app_name=app_name,
            type="none",
            summary=f"No scripting interface found for '{app_name}' on Windows.",
        )

    async def _get_uia_tree_windows(self, app_name: str) -> str:
        """Dump the UI Automation control tree for a running app window."""
        ps = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::NameProperty, "{app_name}")
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if (-not $win) {{
    $cond2 = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ClassNameProperty, "{app_name}")
    $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond2)
}}
if ($win) {{
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $output = @()
    function Get-Tree($elem, $depth) {{
        if ($depth -gt 4) {{ return }}
        $name = try {{ $elem.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::NameProperty) }} catch {{ "" }}
        $type = try {{ $elem.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty) }} catch {{ "" }}
        $id = try {{ $elem.GetCurrentPropertyValue(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty) }} catch {{ "" }}
        if ($name -or $id) {{
            $script:output += ("  " * $depth) + "$type '$name' id='$id'"
        }}
        $child = $walker.GetFirstChild($elem)
        while ($child) {{
            Get-Tree $child ($depth+1)
            $child = $walker.GetNextSibling($child)
        }}
    }}
    Get-Tree $win 0
    $output | Select-Object -First 60 | Out-String
}} else {{
    Write-Output ""
}}
"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell", "-NonInteractive", "-Command", ps,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            out = stdout.decode("utf-8", errors="replace").strip()
            if not out:
                return ""
            header = f"UI Automation tree — {app_name} (Windows)\n"
            return (header + out)[:self._sdef_max_chars]
        except Exception:
            return ""

    async def _get_com_members_windows(self, app_name: str) -> str:
        """Try COM introspection for scriptable apps (Office, IE, Shell, etc.)."""
        # Common ProgID patterns
        prog_ids = [
            app_name,
            f"{app_name}.Application",
            "Word.Application", "Excel.Application",
            "Outlook.Application", "PowerPoint.Application",
        ]
        for prog_id in prog_ids:
            ps = f"""
try {{
    $app = New-Object -ComObject "{prog_id}" -ErrorAction Stop
    $members = $app | Get-Member -MemberType Method,Property | Select-Object Name,MemberType |
               Select-Object -First 40 | Format-Table -HideTableHeaders | Out-String
    Write-Output "COM interface: {prog_id}`n$members"
    $app.Quit() 2>$null
}} catch {{ Write-Output "" }}
"""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "powershell", "-NonInteractive", "-Command", ps,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                out = stdout.decode("utf-8", errors="replace").strip()
                if out and "COM interface:" in out:
                    return out[:self._sdef_max_chars]
            except Exception:
                continue
        return ""

    # ── Linux ─────────────────────────────────────────────────────────────────

    async def _discover_linux(self, app_name: str) -> AppCapability:
        summary = await self._get_atspi_info_linux(app_name)
        if summary:
            return AppCapability(
                app_name=app_name, type="uia", summary=summary, raw=summary[:500]
            )
        return AppCapability(
            app_name=app_name, type="none",
            summary=f"No accessibility interface found for '{app_name}' on Linux.",
        )

    async def _get_atspi_info_linux(self, app_name: str) -> str:
        """Try AT-SPI accessibility info via atspi-info or xdotool."""
        import shutil

        if shutil.which("atspi-info"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "atspi-info", app_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
                out = stdout.decode("utf-8", errors="replace").strip()
                if out:
                    return (f"AT-SPI accessibility tree — {app_name}\n" + out)[:self._sdef_max_chars]
            except Exception:
                pass

        if shutil.which("xdotool"):
            try:
                proc = await asyncio.create_subprocess_shell(
                    f"xdotool search --name '{app_name}' getwindowname",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                out = stdout.decode("utf-8", errors="replace").strip()
                if out:
                    return f"xdotool window info for '{app_name}':\n{out}"
            except Exception:
                pass

        return ""


# ── AppControl ────────────────────────────────────────────────────────────────

class AppControl:
    """
    Launch and control native applications on the host machine.

    Primary path: use AppCapabilityScout to discover the app's scripting
    interface, inject it into the LLM prompt, and execute the generated
    actions.

    Fallback path (vision loop): when no scripting interface is found, take a
    screenshot, ask a vision-capable LLM for the next action, execute it, and
    repeat up to max_vision_steps times.
    """

    def __init__(
        self,
        event_queue: Optional[asyncio.Queue] = None,
        hitl_enabled: bool = True,
        timeout_seconds: int = 30,
    ):
        self._event_queue = event_queue
        self._hitl_enabled = hitl_enabled
        self._timeout = timeout_seconds
        # When set by request_batch_hitl(), individual HITL requests are
        # auto-approved for the duration of one app_control task.
        self._batch_hitl_token: Optional[str] = None
        # Accessibility permission is cached after the first check — the result
        # rarely changes within a session and the probe takes ~200ms.
        self._accessibility_cache: Optional[tuple[bool, str]] = None

    # ── Accessibility / permission preflight (macOS) ──────────────────────────

    async def check_accessibility_permission(self) -> tuple[bool, str]:
        """Return (granted, message) for macOS Accessibility permission.

        Returns (True, "") on non-macOS or when permission appears granted.
        Returns (False, "<instructions>") when the host process is not in the
        Accessibility allow-list — so the caller can surface a clear HITL
        message instead of a cryptic osascript -1743 error.
        """
        if _PLATFORM != "Darwin":
            return True, ""
        if self._accessibility_cache is not None:
            return self._accessibility_cache

        # Probe: ask System Events to count processes. If we lack Accessibility
        # permission, osascript returns -1743 with a "not allowed" error.
        rc, _, err = await self._run([
            "osascript", "-e",
            'tell application "System Events" to count processes',
        ])
        if rc == 0:
            result = (True, "")
        elif "-1743" in err or "not allowed assistive access" in err.lower():
            result = (False, (
                "Accessibility permission required.\n"
                "Open: System Settings → Privacy & Security → Accessibility\n"
                "and enable your terminal / IDE (the process running Cortex)."
            ))
        else:
            # Some other error — let the caller continue and surface it naturally.
            result = (True, "")
        self._accessibility_cache = result
        return result

    # ── Batch HITL approval (one prompt covers the whole task) ────────────────

    async def request_batch_hitl(
        self,
        instruction: str,
        max_actions: int,
        task=None,
        session_id: str = "",
    ) -> bool:
        """Ask the user to approve a whole task up to max_actions.

        When approved, individual _request_hitl() calls return True without
        prompting again. Used by the vision loop so the user isn't prompted
        on every screenshot/action.
        """
        if not self._hitl_enabled or self._event_queue is None:
            self._batch_hitl_token = "auto"
            return True

        approved = await self._request_hitl(
            action="batch_approval",
            detail=(
                f"Allow up to {max_actions} automated actions (screenshots + "
                f"scripts) for:\n{instruction[:300]}"
            ),
            task=task,
            session_id=session_id,
        )
        if approved:
            self._batch_hitl_token = uuid.uuid4().hex[:8]
        return approved

    def clear_batch_hitl(self) -> None:
        self._batch_hitl_token = None

    # ── HITL gate ─────────────────────────────────────────────────────────────

    async def _request_hitl(
        self,
        action: str,
        detail: str,
        task=None,
        session_id: str = "",
    ) -> bool:
        """Prompt the user to approve an action. Returns True if approved."""
        if not self._hitl_enabled or self._event_queue is None:
            return True
        # If the user pre-approved a batch (e.g., the whole vision loop),
        # skip per-action prompts. Cleared by clear_batch_hitl().
        if self._batch_hitl_token and action != "batch_approval":
            return True

        from cortex.streaming.status_events import ClarificationRequestEvent

        try:
            from cortex.framework import _PENDING_TASK_CLARIFICATIONS
        except ImportError:
            return True

        clarification_id = f"appctrl_{uuid.uuid4().hex[:8]}"
        wait_event = asyncio.Event()
        _PENDING_TASK_CLARIFICATIONS[clarification_id] = {
            "event": wait_event,
            "answer": None,
            "loop": asyncio.get_event_loop(),
        }

        question = f"[AppControl] Allow {action}?\n{detail}"
        await self._event_queue.put(ClarificationRequestEvent(
            question=question,
            session_id=session_id,
            clarification_id=clarification_id,
            task_id=getattr(task, "task_id", "app_control/unknown"),
            task_name=getattr(task, "task_name", "app_control"),
            context=detail,
            options=["yes", "no"],
        ))

        try:
            await asyncio.wait_for(wait_event.wait(), timeout=300)
            entry = _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, {}) or {}
            answer = (entry.get("answer") or "").strip().lower()
            return answer in ("yes", "y", "ok", "approve", "allow")
        except asyncio.TimeoutError:
            _PENDING_TASK_CLARIFICATIONS.pop(clarification_id, None)
            logger.info("AppControl HITL timed out for action=%s", action)
            return False

    async def _run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Run a subprocess and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", f"[timed out after {self._timeout}s]"
        return (
            proc.returncode,
            stdout_b.decode("utf-8", errors="replace").strip(),
            stderr_b.decode("utf-8", errors="replace").strip(),
        )

    # ── Launch ────────────────────────────────────────────────────────────────

    async def is_app_running(self, app_name: str) -> bool:
        """Cheap check whether a named app currently has a running process.

        Uses a single subprocess call per platform (no HITL — read-only).
        Failures are non-fatal: returns False so the caller proceeds to launch.
        """
        norm = app_name.strip()
        if not norm:
            return False
        try:
            if _PLATFORM == "Darwin":
                query = norm[:-4] if norm.lower().endswith(".app") else norm
                rc, out, _ = await self._run(["pgrep", "-x", query])
                return rc == 0 and bool(out.strip())
            if _PLATFORM == "Windows":
                ps = f"@(Get-Process -Name '{norm}' -ErrorAction SilentlyContinue).Count"
                rc, out, _ = await self._run(
                    ["powershell", "-NonInteractive", "-Command", ps]
                )
                try:
                    return int(out.strip() or "0") > 0
                except ValueError:
                    return False
            rc, out, _ = await self._run(["pgrep", "-f", norm])
            return rc == 0 and bool(out.strip())
        except Exception:
            return False

    async def launch_app(
        self,
        app_name: str,
        args: Optional[list] = None,
        task=None,
        session_id: str = "",
    ) -> str:
        """Launch an application by name.

        Skips the launch when the app is already running (and no args are
        supplied), to avoid opening duplicate windows. The window is still
        activated so subsequent scripting targets the right app.
        """
        args = args or []

        # Skip if already running — but still bring it to the front.
        if not args and await self.is_app_running(app_name):
            if _PLATFORM == "Darwin":
                # Bring to front without re-launching
                await self._run([
                    "osascript", "-e",
                    f'tell application "{app_name}" to activate',
                ])
            return f"Already running: {app_name}"

        detail = f"App: {app_name}" + (f"  Args: {' '.join(args)}" if args else "")
        if not await self._request_hitl("launch_app", detail, task, session_id):
            raise CortexHITLDeniedError(f"launch_app denied for: {app_name}")

        if _PLATFORM == "Darwin":
            cmd = ["open", "-a", app_name] + args
        elif _PLATFORM == "Windows":
            ps = f"Start-Process '{app_name}'" + (
                f" -ArgumentList '{' '.join(args)}'" if args else ""
            )
            cmd = ["powershell", "-NonInteractive", "-Command", ps]
        else:
            cmd = ["xdg-open", app_name] + args

        rc, out, err = await self._run(cmd)
        if rc != 0 and err:
            return f"[launch_app error ({rc})]: {err}"
        return f"Launched: {app_name}"

    # ── Clipboard actions ─────────────────────────────────────────────────────

    async def copy_to_clipboard(
        self,
        text: str,
        task=None,
        session_id: str = "",
    ) -> str:
        """Put text on the system clipboard. Cross-platform."""
        if not await self._request_hitl(
            "copy_to_clipboard", text[:200], task, session_id,
        ):
            raise CortexHITLDeniedError("copy_to_clipboard denied")

        if _PLATFORM == "Darwin":
            proc = await asyncio.create_subprocess_exec(
                "pbcopy",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        elif _PLATFORM == "Windows":
            proc = await asyncio.create_subprocess_exec(
                "clip",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            import shutil as _shutil
            if _shutil.which("xclip"):
                proc = await asyncio.create_subprocess_exec(
                    "xclip", "-selection", "clipboard",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            elif _shutil.which("xsel"):
                proc = await asyncio.create_subprocess_exec(
                    "xsel", "--clipboard", "--input",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "[copy_to_clipboard: no clipboard tool found (install xclip or xsel)]"

        await proc.communicate(input=text.encode("utf-8"))
        if proc.returncode != 0:
            return f"[copy_to_clipboard error: rc={proc.returncode}]"
        return f"Copied {len(text)} chars to clipboard"

    async def paste_from_clipboard(self) -> str:
        """Read the current clipboard contents. No HITL (read-only)."""
        if _PLATFORM == "Darwin":
            rc, out, err = await self._run(["pbpaste"])
        elif _PLATFORM == "Windows":
            rc, out, err = await self._run([
                "powershell", "-NonInteractive", "-Command", "Get-Clipboard",
            ])
        else:
            import shutil as _shutil
            if _shutil.which("xclip"):
                rc, out, err = await self._run(
                    ["xclip", "-selection", "clipboard", "-o"]
                )
            elif _shutil.which("xsel"):
                rc, out, err = await self._run(
                    ["xsel", "--clipboard", "--output"]
                )
            else:
                return "[paste_from_clipboard: no clipboard tool found]"
        if rc != 0:
            return f"[paste_from_clipboard error]: {err}"
        return out

    # ── AppleScript (macOS) ───────────────────────────────────────────────────

    @staticmethod
    def _ensure_activated(script: str) -> str:
        """If the script targets a specific app but doesn't activate it,
        prepend an `activate` so the window is frontmost before scripting.

        Matches `tell application "AppName"` and skips when an `activate`
        statement is already present.
        """
        if "activate" in script.lower():
            return script
        m = re.search(r'tell\s+application\s+"([^"]+)"', script, re.IGNORECASE)
        if not m:
            return script
        app = m.group(1)
        # Skip pure "System Events" scripts — they don't need to be frontmost.
        if app.lower() == "system events":
            return script
        prelude = (
            f'tell application "{app}" to activate\n'
            'delay 0.3\n'
        )
        return prelude + script

    async def run_applescript(
        self,
        script: str,
        task=None,
        session_id: str = "",
    ) -> str:
        """Execute an AppleScript. macOS only.

        Automatically prepends `activate` for the target app when the script
        doesn't already contain one, so the window is frontmost before any
        keystroke/click is sent.
        """
        if _PLATFORM != "Darwin":
            return "[run_applescript: macOS only]"

        # Accessibility preflight — surface a clear message instead of -1743.
        if any(kw in script.lower() for kw in ("keystroke", "system events", "key code")):
            granted, msg = await self.check_accessibility_permission()
            if not granted:
                return f"[run_applescript denied]: {msg}"

        if not await self._request_hitl("run_applescript", script[:300], task, session_id):
            raise CortexHITLDeniedError("run_applescript denied")

        script = self._ensure_activated(script)
        rc, out, err = await self._run(["osascript", "-e", script])
        if rc != 0:
            return f"[AppleScript error ({rc})]: {err or out}"
        return out or "(no output)"

    # ── PowerShell (Windows) ──────────────────────────────────────────────────

    async def run_powershell(
        self,
        script: str,
        task=None,
        session_id: str = "",
    ) -> str:
        """Execute a PowerShell script. Windows only."""
        if _PLATFORM != "Windows":
            return "[run_powershell: Windows only]"
        if not await self._request_hitl("run_powershell", script[:300], task, session_id):
            raise CortexHITLDeniedError("run_powershell denied")

        rc, out, err = await self._run(
            ["powershell", "-NonInteractive", "-Command", script]
        )
        if rc != 0:
            return f"[PowerShell error ({rc})]: {err or out}"
        return out or "(no output)"

    # ── Shell command (Linux / fallback) ──────────────────────────────────────

    async def run_shell_command(
        self,
        command: str,
        task=None,
        session_id: str = "",
    ) -> str:
        """Run an arbitrary shell command (requires user approval)."""
        if not await self._request_hitl("run_shell_command", command[:300], task, session_id):
            raise CortexHITLDeniedError("run_shell_command denied")

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return "[run_shell_command: timed out]"
        out = stdout_b.decode("utf-8", errors="replace").strip()
        err = stderr_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and err:
            return f"[shell error ({proc.returncode})]: {err}"
        return out or "(no output)"

    # ── Screenshot ────────────────────────────────────────────────────────────

    async def screenshot(
        self,
        output_path: str,
        task=None,
        session_id: str = "",
    ) -> str:
        """Capture the full screen and save to output_path (.png)."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if not await self._request_hitl("screenshot", f"Save to: {output_path}", task, session_id):
            raise CortexHITLDeniedError("screenshot denied")

        if _PLATFORM == "Darwin":
            cmd = ["screencapture", "-x", output_path]
        elif _PLATFORM == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
                "$bmp = New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
                "$g = [System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
                f"$bmp.Save('{output_path}');"
            )
            cmd = ["powershell", "-NonInteractive", "-Command", ps]
        else:
            import shutil
            if shutil.which("scrot"):
                cmd = ["scrot", output_path]
            elif shutil.which("gnome-screenshot"):
                cmd = ["gnome-screenshot", "-f", output_path]
            else:
                return "[screenshot: no capture tool found (install scrot or gnome-screenshot)]"

        rc, _, err = await self._run(cmd)
        if rc != 0:
            return f"[screenshot error ({rc})]: {err}"
        return f"Screenshot saved: {output_path}"

    # ── Read-only queries (no HITL) ───────────────────────────────────────────

    async def get_running_apps(self) -> str:
        """List currently running (visible) applications. No HITL required."""
        if _PLATFORM == "Darwin":
            script = (
                'tell application "System Events" to get name of every process '
                'whose background only is false'
            )
            rc, out, err = await self._run(["osascript", "-e", script])
            return out if rc == 0 else f"[get_running_apps error]: {err}"
        elif _PLATFORM == "Windows":
            ps = (
                "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} "
                "| Select-Object -ExpandProperty Name | Sort-Object -Unique"
            )
            rc, out, err = await self._run(
                ["powershell", "-NonInteractive", "-Command", ps]
            )
            return out if rc == 0 else f"[get_running_apps error]: {err}"
        else:
            rc, out, err = await self._run(["ps", "-axo", "comm="])
            return out if rc == 0 else f"[get_running_apps error]: {err}"

    async def get_window_text(self, app_name: str) -> str:
        """Get visible text/title from a running application window. No HITL."""
        if _PLATFORM == "Darwin":
            script = (
                f'tell application "System Events" to tell process "{app_name}" '
                f'to get value of every text field'
            )
            rc, out, err = await self._run(["osascript", "-e", script])
            return out if rc == 0 else f"[get_window_text error]: {err}"
        elif _PLATFORM == "Windows":
            ps = (
                f"$p = Get-Process -Name '{app_name}' -ErrorAction SilentlyContinue;"
                "$p.MainWindowTitle"
            )
            rc, out, _ = await self._run(
                ["powershell", "-NonInteractive", "-Command", ps]
            )
            return out or f"[{app_name}: no window title found]"
        return "[get_window_text: not supported on this platform]"

    # ── Vision loop (fallback when no scripting dict) ─────────────────────────

    async def execute_with_vision_loop(
        self,
        instruction: str,
        app_name: str,
        llm_client,
        session_id: str,
        task,
        output_dir: str,
        max_steps: int,
        vision_provider: str,
        event_queue,
    ) -> str:
        """
        Drive the app by observing screenshots and asking a vision LLM for
        the next action. Loops until 'DONE:' is returned or max_steps is hit.

        Each iteration:
          1. Take a screenshot
          2. Encode as base64 and send to the vision LLM with the task context
          3. Parse the response for an action or a DONE signal
          4. Execute the action
        """
        from cortex.prompts import APP_CONTROL_SYSTEM, APP_CONTROL_VISION_USER

        self._event_queue = event_queue
        os.makedirs(output_dir, exist_ok=True)
        action_history: list[str] = []

        # Single batch HITL approval — avoids N separate prompts during the loop.
        approved = await self.request_batch_hitl(
            instruction=instruction,
            max_actions=max_steps,
            task=task,
            session_id=session_id,
        )
        if not approved:
            return "[vision loop: user denied batch approval]"

        try:
            return await self._vision_loop_body(
                instruction, app_name, llm_client, session_id, task,
                output_dir, max_steps, vision_provider, action_history,
            )
        finally:
            self.clear_batch_hitl()

    async def _vision_loop_body(
        self,
        instruction: str,
        app_name: str,
        llm_client,
        session_id: str,
        task,
        output_dir: str,
        max_steps: int,
        vision_provider: str,
        action_history: list,
    ) -> str:
        from cortex.prompts import APP_CONTROL_SYSTEM, APP_CONTROL_VISION_USER

        for step in range(1, max_steps + 1):
            # 1. Take screenshot
            screenshot_path = os.path.join(output_dir, f"vision_step_{step:02d}.png")
            screen_result = await self.screenshot(screenshot_path, task, session_id)
            if "error" in screen_result.lower() or "denied" in screen_result.lower():
                return f"[vision loop: screenshot failed at step {step} — {screen_result}]"

            # 2. Encode screenshot
            try:
                with open(screenshot_path, "rb") as fh:
                    img_b64 = base64.b64encode(fh.read()).decode("ascii")
            except OSError as e:
                return f"[vision loop: could not read screenshot at step {step} — {e}]"

            # 3. Build vision message
            prompt_text = APP_CONTROL_VISION_USER.format(
                instruction=instruction,
                app_name=app_name,
                step=step,
                max_steps=max_steps,
                history="\n".join(action_history) if action_history else "(none yet)",
                platform=_PLATFORM,
            )
            messages = [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }]

            # 4. Call vision LLM
            try:
                response = await llm_client.complete(
                    messages=messages,
                    system=APP_CONTROL_SYSTEM,
                    provider_name=vision_provider,
                    max_tokens=500,
                )
                raw = (response.content or "").strip()
            except Exception as e:
                return f"[vision loop: LLM call failed at step {step} — {e}]"

            # 5. Check for DONE
            done_match = re.search(
                r'^DONE:\s*(.+)', raw, re.IGNORECASE | re.MULTILINE | re.DOTALL
            )
            if done_match:
                return done_match.group(1).strip()

            # 6. Execute the action block
            from cortex.modules.generic_mcp_agent import _extract_field
            action = (_extract_field(raw, "ACTION") or "").strip().lower()
            if not action:
                # LLM returned something unparseable — log and continue
                logger.warning(
                    "vision loop step %d: no ACTION found in LLM response: %r",
                    step, raw[:200],
                )
                action_history.append(f"Step {step}: [no parseable action]")
                continue

            action_summary = raw[:120].replace("\n", " ")
            try:
                result = await self._execute_action_block(raw, task, session_id)
            except CortexHITLDeniedError as e:
                return f"[vision loop: action denied at step {step} — {e}]"
            except Exception as e:
                result = f"[error: {e}]"

            action_history.append(f"Step {step}: {action_summary} → {result[:80]}")
            logger.info("vision loop step %d/%d: %s → %s", step, max_steps, action, result[:60])

        return f"[vision loop: reached max {max_steps} steps — task may be incomplete]"

    async def _execute_action_block(
        self, block: str, task, session_id: str
    ) -> str:
        """Execute a single structured action block (shared by scripted and vision paths)."""
        from cortex.modules.generic_mcp_agent import _extract_field

        action = (_extract_field(block, "ACTION") or "").strip().lower()

        if action == "launch_app":
            app = _extract_field(block, "APP") or ""
            args_raw = _extract_field(block, "ARGS") or ""
            args = args_raw.split() if args_raw else []
            return await self.launch_app(app, args, task, session_id)

        elif action == "run_applescript":
            script = _extract_field(block, "SCRIPT") or ""
            return await self.run_applescript(script, task, session_id)

        elif action == "run_powershell":
            script = _extract_field(block, "SCRIPT") or ""
            return await self.run_powershell(script, task, session_id)

        elif action == "run_shell_command":
            cmd = _extract_field(block, "COMMAND") or _extract_field(block, "SCRIPT") or ""
            return await self.run_shell_command(cmd, task, session_id)

        elif action == "screenshot":
            out = _extract_field(block, "OUTPUT_PATH") or ""
            if not out:
                import time
                out = f"/tmp/cortex_screenshot_{int(time.time())}.png"
            return await self.screenshot(out, task, session_id)

        elif action == "get_running_apps":
            return await self.get_running_apps()

        elif action == "get_window_text":
            app = _extract_field(block, "APP") or ""
            return await self.get_window_text(app)

        elif action == "copy_to_clipboard":
            text = _extract_field(block, "TEXT") or ""
            return await self.copy_to_clipboard(text, task, session_id)

        elif action == "paste_from_clipboard":
            return await self.paste_from_clipboard()

        else:
            return f"[app_control: unknown action '{action}']"
