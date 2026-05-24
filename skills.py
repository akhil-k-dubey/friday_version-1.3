"""
skills.py — Built-in skill engine for FRIDAY.

Every skill is a callable that returns a string result.
The LLM triggers skills by emitting [ACTION:skill_name:args] in its response.
The main loop intercepts these, runs the skill, and feeds the result back.

All tools are FREE FOREVER — no API keys required.
"""
from __future__ import annotations

import ast
import datetime
import json
import math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib import parse as urlparse, request as urlreq


# ── Executable path resolver ───────────────────────────────────────────────

def _find_executable(name: str, known_paths: Optional[list[str]] = None) -> str:
    """Resolve an executable path. Tries: shutil.which → registry → known paths."""
    # Try PATH first
    resolved = shutil.which(name)
    if resolved:
        return resolved
    # Try registry (App Paths)
    if platform.system() == "Windows":
        try:
            import winreg
            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
                resolved = winreg.QueryValue(key, None)
                if resolved and os.path.exists(resolved):
                    return resolved
        except (OSError, ImportError):
            pass
        try:
            # Also check HKCU
            import winreg
            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                resolved = winreg.QueryValue(key, None)
                if resolved and os.path.exists(resolved):
                    return resolved
        except (OSError, ImportError):
            pass
    # Known fallback paths
    if known_paths:
        for p in known_paths:
            expanded = os.path.expandvars(os.path.expanduser(p))
            if os.path.exists(expanded):
                return expanded
    return name  # return as-is, might work via shell


# ── App registry — common Windows apps ──────────────────────────────────────

_APP_REGISTRY: Dict[str, str] = {
    "notepad":    "notepad.exe",
    "calculator": "calc.exe",
    "calc":       "calc.exe",
    "paint":      "mspaint.exe",
    "explorer":   "explorer.exe",
    "file explorer": "explorer.exe",
    "files":      "explorer.exe",
    "cmd":        "cmd.exe",
    "terminal":   "wt.exe",
    "powershell": "powershell.exe",
    "task manager": "taskmgr.exe",
    "taskmgr":    "taskmgr.exe",
    "settings":   "ms-settings:",
    "control panel": "control.exe",
    "chrome":     r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox":    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "edge":       "msedge.exe",
    "microsoft edge": "msedge.exe",
    "brave":      r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "vscode":     "code",
    "vs code":    "code",
    "visual studio code": "code",
    "spotify":    r"C:\Users\{user}\AppData\Roaming\Spotify\Spotify.exe",
    "discord":    r"C:\Users\{user}\AppData\Local\Discord\Update.exe --processStart Discord.exe",
    "vlc":        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    "obs":        r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
}

# Browser process name → executable to launch a URL in that browser
_BROWSER_PROC_MAP: Dict[str, str] = {
    "msedge.exe":    "msedge.exe",
    "chrome.exe":    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "brave.exe":     r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    "firefox.exe":   r"C:\Program Files\Mozilla Firefox\firefox.exe",
}

# WhatsApp state storage
_last_whatsapp_target: Optional[str] = None

# Apps that should run in the web browser
_BROWSER_APPS: Dict[str, str] = {
    "youtube":    "https://www.youtube.com",
    "gmail":      "https://mail.google.com",
    "whatsapp":   "https://web.whatsapp.com",
    "instagram":  "https://www.instagram.com",
    "google":     "https://www.google.com",
    "netflix":    "https://www.netflix.com",
    "chatgpt":    "https://chatgpt.com",
    "spotify":    "https://open.spotify.com",
}

# Site name → URL (for smart_open)
_SITE_MAP: Dict[str, str] = {
    "youtube":    "https://www.youtube.com",
    "gmail":      "https://mail.google.com",
    "github":     "https://github.com",
    "google":     "https://www.google.com",
    "chatgpt":    "https://chat.openai.com",
    "netflix":    "https://www.netflix.com",
    "twitter":    "https://twitter.com",
    "x":          "https://twitter.com",
    "reddit":     "https://www.reddit.com",
    "linkedin":   "https://www.linkedin.com",
    "stackoverflow": "https://stackoverflow.com",
    "wikipedia":  "https://www.wikipedia.org",
    "whatsapp":   "https://web.whatsapp.com",
    "instagram":  "https://www.instagram.com",
    "spotify":    "https://open.spotify.com",
    "notion":     "https://notion.so",
    # AI & Dev
    "claude":     "https://claude.ai",
    "claude ai":  "https://claude.ai",
    "perplexity": "https://www.perplexity.ai",
    "copilot":    "https://copilot.microsoft.com",
    "huggingface":"https://huggingface.co",
    "kaggle":     "https://www.kaggle.com",
    "colab":      "https://colab.research.google.com",
    "figma":      "https://www.figma.com",
    "vercel":     "https://vercel.com",
    # Browser aliases — so smart_open doesn't turn "browser" into "https://browser/"
    "browser":          "https://www.google.com",
    "edge":             "https://www.google.com",
    "microsoft edge":   "https://www.google.com",
    "chrome":           "https://www.google.com",
    "firefox":          "https://www.google.com",
}


# ── Timer storage ───────────────────────────────────────────────────────────

_active_timers: List[dict] = []

# ── Sounddevice beep helper (replaces [console]::beep) ──────────────────────
try:
    import sounddevice as _sd_beep
    import numpy as _np_beep
    _HAVE_SD = True
except ImportError:
    _HAVE_SD = False


def _play_beep(freq: float, duration_ms: int, volume: float = 0.3) -> None:
    """Play a sine-wave beep via sounddevice (non-blocking)."""
    if not _HAVE_SD:
        return
    try:
        sr = 22050
        t = _np_beep.linspace(0, duration_ms / 1000, int(sr * duration_ms / 1000), endpoint=False)
        tone = (volume * _np_beep.sin(2 * _np_beep.pi * freq * t)).astype(_np_beep.float32)
        _sd_beep.play(tone, sr)
    except Exception:
        pass


# ── Announce hook ────────────────────────────────────────────────────────────
# Set to voice.speak by main.py so timer/remind fire spoken alerts.
# If None, falls back to print-only.
_announce_fn: Optional[Callable[[str], None]] = None

def _announce(msg: str) -> None:
    """Print and optionally speak a timed alert message."""
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")
    if _announce_fn is not None:
        try:
            _announce_fn(msg)
        except Exception:
            pass


# ── Skill implementations ──────────────────────────────────────────────────

def skill_open_app(args: str) -> str:
    """Open a Windows application by name."""
    app_name = args.strip().lower()
    if not app_name:
        return "No application name provided."

    # Intercept browser apps
    if app_name in _BROWSER_APPS:
        return skill_open_url_in_existing_browser(_BROWSER_APPS[app_name])

    # Clean leading articles and common conversational prefixes
    for prefix in ["the ", "a ", "an ", "my "]:
        if app_name.startswith(prefix):
            app_name = app_name[len(prefix):].strip()
    # Clean trailing noise words like "app" or "application"
    for suffix in [" app", " application"]:
        if app_name.endswith(suffix):
            app_name = app_name[:-len(suffix)].strip()

    exe = _APP_REGISTRY.get(app_name)
    if exe:
        # Replace {user} placeholder
        user = os.environ.get("USERNAME", os.environ.get("USER", ""))
        exe = exe.replace("{user}", user)
        # Resolve hardcoded paths through _find_executable if the path doesn't exist
        # (handles different install locations, e.g. Chrome in Program Files vs AppData)
        known_paths = [exe] if not os.path.exists(exe) and os.path.isabs(exe) else []
        if known_paths:
            exe = _find_executable(os.path.basename(exe), known_paths=known_paths)
    else:
        # Try running it directly (works for PATH-accessible apps)
        exe = app_name

    # Try resolving via shutil.which for absolute path resolution
    resolved = shutil.which(exe.split()[0] if exe else "")
    if resolved:
        # Re-attach any arguments if present
        parts = exe.split(maxsplit=1)
        if len(parts) > 1:
            exe = f'"{resolved}" {parts[1]}'
        else:
            exe = resolved

    try:
        if exe.startswith("ms-") or exe.endswith(".lnk") or exe.endswith(".url"):
            # UWP / ms-settings links / shortcuts
            os.startfile(exe)
        elif " " not in exe or os.path.exists(exe):
            # No arguments and is a simple exe/path — use os.startfile for reliable interactive GUI launch
            os.startfile(exe)
        else:
            # Has arguments (e.g. Discord) — use Popen
            subprocess.Popen(
                exe, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return f"Opened {app_name}."
    except Exception as exc:
        # Fallback to Popen if startfile fails
        try:
            subprocess.Popen(
                exe, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"Opened {app_name}."
        except Exception as exc2:
            return f"Could not open {app_name}: {exc2}"


def skill_close_app(args: str) -> str:
    """Close a running application by name."""
    app_name = args.strip().lower()
    if not app_name:
        return "No application name provided."

    # Clean leading articles and common conversational prefixes
    for prefix in ["the ", "a ", "an ", "my "]:
        if app_name.startswith(prefix):
            app_name = app_name[len(prefix):].strip()
    # Clean trailing noise words like "app" or "application"
    for suffix in [" app", " application"]:
        if app_name.endswith(suffix):
            app_name = app_name[:-len(suffix)].strip()

    # Alias map: common names → actual Windows process names
    _PROCESS_ALIASES = {
        "chrome": "msedge",      # user has Edge, not Chrome
        "browser": "msedge",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "explorer": "explorer",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "vscode": "code",
        "vs code": "code",
        "terminal": "WindowsTerminal",
        "cmd": "cmd",
        "calculator": "CalculatorApp",
        "calc": "CalculatorApp",
    }
    process_name = _PROCESS_ALIASES.get(app_name, app_name)

    try:
        result = subprocess.run(
            ["taskkill", "/IM", f"{process_name}.exe", "/F"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"Closed {app_name}."

        # If it was calculator, try Calculator.exe as a fallback (for post-2021 Windows versions)
        if process_name == "CalculatorApp":
            result_calc = subprocess.run(
                ["taskkill", "/IM", "Calculator.exe", "/F"],
                capture_output=True, text=True, timeout=5,
            )
            if result_calc.returncode == 0:
                return f"Closed {app_name}."

        # If alias didn't work, try original name as fallback
        if process_name != app_name:
            result2 = subprocess.run(
                ["taskkill", "/IM", f"{app_name}.exe", "/F"],
                capture_output=True, text=True, timeout=5,
            )
            if result2.returncode == 0:
                return f"Closed {app_name}."
        return f"Could not find {app_name} running."
    except Exception as exc:
        return f"Failed to close {app_name}: {exc}"


def skill_close_tab(args: str) -> str:
    """Close the current browser tab (Ctrl+W). Does NOT kill the entire browser."""
    from automation import press_hotkey, focus_window
    import time
    
    hint = args.strip()
    import re
    hint = re.sub(r"\s+tabs?$", "", hint, flags=re.IGNORECASE).strip()
    focused = False
    if hint:
        res = focus_window(hint)
        if "Focused" in res:
            focused = True
            time.sleep(0.3)
    else:
        # Safe default: try to focus any active browser window first
        for browser_hint in ("edge", "chrome", "firefox", "browser", "msedge"):
            res = focus_window(browser_hint)
            if "Focused" in res:
                focused = True
                time.sleep(0.3)
                break

    if not focused and not hint:
        return "Refused: Could not find or focus any browser window to safely close a tab."

    result = press_hotkey("ctrl+w")
    if "Pressed" in result:
        return f"Closed current tab{' (' + hint + ')' if hint else ''}."
    return f"Could not close tab: {result}"


def skill_read_screen(args: str = "") -> str:
    """Reads visible text content from the currently focused window
    using Windows UI Automation. No GPU, no screenshot needed.
    """
    import uiautomation as auto
    import win32gui
    import threading

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "No focused window active."
        title = win32gui.GetWindowText(hwnd)
        ctrl = auto.ControlFromHandle(hwnd)
        if not ctrl:
            return f"Could not read screen: Unable to bind to '{title}'."

        texts = []
        # Walk descendant nodes on a separate thread to handle heavy layouts and enforce timeout
        def _worker():
            try:
                for c in ctrl.GetChildren():
                    _collect_text(c, texts, depth=0, max_depth=4)
            except Exception:
                pass

        t = threading.Thread(target=_worker)
        t.daemon = True
        t.start()
        t.join(timeout=0.15)  # 150ms strict hardware-friendly watchdog timeout

        # Clean duplicates and empty spaces
        seen = set()
        unique_texts = []
        for text in texts:
            cleaned = text.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique_texts.append(cleaned)

        combined = "\n".join(unique_texts)
        if not combined:
            return f"No readable text found in '{title}'."
        # Truncate to avoid overwhelming LLM context
        if len(combined) > 2000:
            combined = combined[:2000] + "\n... (truncated)"
        return f"Screen content from '{title}':\n{combined}"
    except Exception as e:
        return f"Could not read screen: {e}"


def _collect_text(ctrl, results: list, depth: int, max_depth: int):
    if depth > max_depth or len(results) >= 150:  # limit total collected nodes
        return
    
    # Ignore purely structural layout panels
    name = getattr(ctrl, "Name", "") or ""
    val = ""
    try:
        val = ctrl.GetValuePattern().Value or ""
    except Exception:
        pass
    
    text = (name + " " + val).strip()
    if text and len(text) > 1 and not text.isdigit():
        results.append(text)
        
    for child in ctrl.GetChildren():
        _collect_text(child, results, depth + 1, max_depth)


def skill_web_search(args: str) -> str:
    """Search the web using DuckDuckGo (free, no API key)."""
    query = args.strip()
    if not query:
        return "No search query provided."

    # Try the new 'ddgs' package first, then legacy 'duckduckgo_search'
    ddgs_cls = None
    try:
        from ddgs import DDGS as _DDGS  # type: ignore
        ddgs_cls = _DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS as _DDGS2  # type: ignore
            ddgs_cls = _DDGS2
        except ImportError:
            pass

    if ddgs_cls is not None:
        try:
            with ddgs_cls() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            if not results:
                return _web_search_fallback(query)

            summary_parts = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "")
                body = r.get("body", "")
                summary_parts.append(f"{i}. {title}: {body}")

            return "Here is what I found. " + " ".join(summary_parts)
        except Exception as exc:
            return _web_search_fallback(query)
    else:
        return _web_search_fallback(query)


def _web_search_fallback(query: str) -> str:
    """Minimal DuckDuckGo search without extra packages."""
    try:
        url = f"https://api.duckduckgo.com/?q={urlparse.quote(query)}&format=json&no_html=1"
        req = urlreq.Request(url, headers={"User-Agent": "FRIDAY/1.0"})
        with urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        abstract = data.get("AbstractText", "")
        answer = data.get("Answer", "")
        related = data.get("RelatedTopics", [])

        parts = []
        if answer:
            parts.append(answer)
        if abstract:
            parts.append(abstract)
        for topic in related[:3]:
            text = topic.get("Text", "")
            if text:
                parts.append(text)

        if parts:
            return "Here is what I found. " + " ".join(parts)
        return f"I could not find a clear answer for: {query}"
    except Exception as exc:
        return f"Web search failed: {exc}"


def skill_system_info(args: str) -> str:
    """Get system information: CPU, RAM, disk, battery."""
    try:
        import psutil
    except ImportError:
        return _system_info_basic()

    parts = []
    cpu_pct = psutil.cpu_percent(interval=0.5)
    parts.append(f"CPU usage is {cpu_pct} percent")

    mem = psutil.virtual_memory()
    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    parts.append(f"RAM: {used_gb:.1f} of {total_gb:.1f} GB used ({mem.percent} percent)")

    disk = psutil.disk_usage("C:\\")
    free_gb = disk.free / (1024 ** 3)
    parts.append(f"C drive: {free_gb:.0f} GB free")

    bat = psutil.sensors_battery()
    if bat:
        plug = "plugged in" if bat.power_plugged else "on battery"
        parts.append(f"Battery: {bat.percent} percent, {plug}")

    # GPU / VRAM via nvidia-smi (graceful fallback if unavailable)
    try:
        nv = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if nv.returncode == 0 and nv.stdout.strip():
            gpu_line = nv.stdout.strip().split("\n")[0]
            gpu_parts = [p.strip() for p in gpu_line.split(",")]
            if len(gpu_parts) >= 4:
                parts.append(
                    f"GPU: {gpu_parts[0]}, VRAM {gpu_parts[1]} of "
                    f"{gpu_parts[2]} MB used, utilization {gpu_parts[3]} percent"
                )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # nvidia-smi not installed or failed — skip GPU info

    return ". ".join(parts) + "."


def _system_info_basic() -> str:
    """Fallback system info without psutil."""
    info = platform.uname()
    return (
        f"System: {info.system} {info.release}, "
        f"Machine: {info.machine}, "
        f"Processor: {info.processor}."
    )


def skill_clipboard_read(args: str) -> str:
    """Read current clipboard contents."""
    try:
        import pyperclip
        try:
            content = pyperclip.paste()
        except Exception as e:
            raise ImportError(f"pyperclip locked: {e}")
        if content:
            # Truncate for voice
            if len(content) > 300:
                return f"Clipboard contains: {content[:300]}... (truncated)"
            return f"Clipboard contains: {content}"
        return "Clipboard is empty."
    except ImportError:
        # PowerShell fallback
        try:
            result = subprocess.run(
                ["powershell", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5,
            )
            content = result.stdout.strip()
            if content:
                if len(content) > 300:
                    return f"Clipboard contains: {content[:300]}... (truncated)"
                return f"Clipboard contains: {content}"
            return "Clipboard is empty."
        except Exception:
            return "Could not read clipboard."


def skill_clipboard_write(args: str) -> str:
    """Write text to clipboard."""
    text = args.strip()
    if not text:
        return "No text to copy."
    try:
        import pyperclip
        try:
            pyperclip.copy(text)
        except Exception as e:
            raise ImportError(f"pyperclip locked: {e}")
        return "Copied to clipboard."
    except ImportError:
        try:
            # Escape single quotes in powershell
            escaped = text.replace("'", "''")
            subprocess.run(
                ["powershell", "-Command", f"Set-Clipboard '{escaped}'"],
                timeout=5,
            )
            return "Copied to clipboard."
        except Exception:
            return "Could not write to clipboard."


def _set_mute_explicit(mute: bool) -> str:
    """Explicitly set mute on/off via Windows Core Audio COM API.

    Uses IAudioEndpointVolume.SetMute(bool) instead of the VK_VOLUME_MUTE
    toggle key, so 'mute' always mutes and 'unmute' always unmutes
    regardless of the current state.
    Falls back to the toggle key if COM interop fails.
    """
    mute_ps = "$true" if mute else "$false"
    # PowerShell inline C# — defines the minimum Core Audio COM interfaces
    # needed to call SetMute() on the default audio endpoint.
    # Vtable stubs (_0.._10) occupy slots we don't call.
    ps_script = (
        "Add-Type @'\n"
        "using System; using System.Runtime.InteropServices;\n"
        '[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IAudioEndpointVolume {\n"
        "  int _0(); int _1(); int _2(); int _3(); int _4();\n"
        "  int SetMasterVolumeLevelScalar(float f, Guid g);\n"
        "  int GetMasterVolumeLevelScalar(out float f);\n"
        "  int _7(); int _8(); int _9(); int _10();\n"
        "  int SetMute([MarshalAs(UnmanagedType.Bool)] bool b, Guid g);\n"
        "  int GetMute(out bool b);\n"
        "}\n"
        '[Guid("D666063F-1587-4E43-81F1-B948E807363F"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IMMDevice {\n"
        "  int Activate(ref Guid iid, int c, IntPtr p, "
        "[MarshalAs(UnmanagedType.IUnknown)] out object o);\n"
        "}\n"
        '[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IMMDeviceEnumerator {\n"
        "  int GetDefaultAudioEndpoint(int d, int r, out IMMDevice dev);\n"
        "}\n"
        '[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] '
        "class MMDeviceEnumerator {}\n"
        "'@\n"
        "$e = New-Object MMDeviceEnumerator; $d = $null\n"
        "$e.GetDefaultAudioEndpoint(0, 1, [ref]$d)\n"
        "$iid = [Guid]'5CDF2C82-841E-4546-9722-0CF74078229A'\n"
        "$o = $null; $d.Activate([ref]$iid, 1, [IntPtr]::Zero, [ref]$o)\n"
        f"([IAudioEndpointVolume]$o).SetMute({mute_ps}, [Guid]::Empty)\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return "Volume muted." if mute else "Volume unmuted."
    except Exception:
        pass
    # Fallback: toggle key (may not match desired state)
    try:
        subprocess.run(
            ["powershell", "-Command",
             "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass
    return "Volume muted." if mute else "Volume unmuted."


def _set_volume_scalar(level: float) -> str:
    """Set master volume level using Windows Core Audio COM API.
    level is a float between 0.0 and 1.0.
    """
    ps_script = (
        "Add-Type @'\n"
        "using System; using System.Runtime.InteropServices;\n"
        '[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IAudioEndpointVolume {\n"
        "  int _0(); int _1(); int _2(); int _3(); int _4();\n"
        "  int SetMasterVolumeLevelScalar(float f, Guid g);\n"
        "  int GetMasterVolumeLevelScalar(out float f);\n"
        "  int _7(); int _8(); int _9(); int _10();\n"
        "  int SetMute([MarshalAs(UnmanagedType.Bool)] bool b, Guid g);\n"
        "  int GetMute(out bool b);\n"
        "}\n"
        '[Guid("D666063F-1587-4E43-81F1-B948E807363F"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IMMDevice {\n"
        "  int Activate(ref Guid iid, int c, IntPtr p, "
        "[MarshalAs(UnmanagedType.IUnknown)] out object o);\n"
        "}\n"
        '[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), '
        "InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]\n"
        "interface IMMDeviceEnumerator {\n"
        "  int GetDefaultAudioEndpoint(int d, int r, out IMMDevice dev);\n"
        "}\n"
        '[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] '
        "class MMDeviceEnumerator {}\n"
        "'@\n"
        "$e = New-Object MMDeviceEnumerator; $d = $null\n"
        "$e.GetDefaultAudioEndpoint(0, 1, [ref]$d)\n"
        "$iid = [Guid]'5CDF2C82-841E-4546-9722-0CF74078229A'\n"
        "$o = $null; $d.Activate([ref]$iid, 1, [IntPtr]::Zero, [ref]$o)\n"
        f"([IAudioEndpointVolume]$o).SetMasterVolumeLevelScalar({level:.4f}, [Guid]::Empty)\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return f"Volume set to {int(level * 100)}%."
    except Exception:
        pass
    return "Failed to set volume."


def skill_volume(args: str) -> str:
    """Control system volume using PowerShell/nircmd."""
    action = args.strip().lower()
    if not action:
        return "Specify: mute, unmute, up, down, or a number 0-100."

    # Strip prefixes like "volume " or "volume:" or trailing "%"
    action = re.sub(r"^(?:volume\s*[:\s]*|set\s+volume\s+(?:to\s+)?|turn\s+volume\s+(?:to\s+)?)", "", action)
    action = action.rstrip("%")

    # Spoken/string aliases
    aliases = {
        "max": 100,
        "maximum": 100,
        "full": 100,
        "high": 80,
        "medium": 50,
        "half": 50,
        "low": 20,
        "min": 0,
        "minimum": 0,
        "zero": 0,
        "off": 0,
    }

    if action in aliases:
        target_pct = aliases[action]
        return _set_volume_scalar(target_pct / 100.0)

    # Check if numeric digit exists
    digits = re.findall(r"\d+", action)
    if digits:
        try:
            val = int(digits[0])
            if 0 <= val <= 100:
                return _set_volume_scalar(val / 100.0)
        except ValueError:
            pass

    try:
        if action == "mute":
            return _set_mute_explicit(True)
        elif action == "unmute":
            return _set_mute_explicit(False)
        elif action in ("up", "louder"):
            for _ in range(5):
                subprocess.run(
                    ["powershell", "-Command",
                     "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"],
                    timeout=5, capture_output=True,
                )
            return "Volume increased."
        elif action in ("down", "quieter", "lower"):
            for _ in range(5):
                subprocess.run(
                    ["powershell", "-Command",
                     "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"],
                    timeout=5, capture_output=True,
                )
            return "Volume decreased."
        else:
            return f"Unknown volume command: {action}. Use mute, unmute, up, down, or a number 0-100."
    except Exception as exc:
        return f"Volume control failed: {exc}"


def skill_timer(args: str) -> str:
    """Set a countdown timer. Args: seconds or 'Xm' for minutes."""
    raw = args.strip().lower()
    if not raw:
        return "Specify a duration, like 30 for 30 seconds or 5m for 5 minutes."

    try:
        if raw.endswith("m"):
            seconds = int(raw[:-1]) * 60
        elif raw.endswith("min"):
            seconds = int(raw[:-3]) * 60
        elif raw.endswith("h"):
            seconds = int(raw[:-1]) * 3600
        elif raw.endswith("s"):
            seconds = int(raw[:-1])
        else:
            seconds = int(raw)
    except ValueError:
        return f"Could not parse duration: {raw}"

    if seconds <= 0:
        return "Duration must be positive."
    if seconds > 86400: 
        return "Maximum timer is 24 hours."

    def _alarm():
        time.sleep(seconds)
        label_done = f"{seconds}s" if seconds < 60 else f"{seconds//60}m{seconds%60:02d}s"
        _announce(f"Timer done! {label_done} elapsed.")
        _play_beep(880, 300)
        _play_beep(1047, 300)
        _play_beep(1319, 400)
        # Cleanup: remove this timer from the active list
        _active_timers[:] = [t for t in _active_timers if t.get("thread") is not threading.current_thread()]

    t = threading.Thread(target=_alarm, daemon=True, name=f"timer-{seconds}s")
    t.start()
    _active_timers.append({"seconds": seconds, "started": time.time(), "thread": t})

    if seconds >= 60:
        mins = seconds // 60
        secs = seconds % 60
        label = f"{mins} minute{'s' if mins != 1 else ''}"
        if secs:
            label += f" {secs} second{'s' if secs != 1 else ''}"
    else:
        label = f"{seconds} second{'s' if seconds != 1 else ''}"

    return f"Timer set for {label}."


def skill_calculator(args: str) -> str:
    """Safely evaluate a math expression."""
    expr = args.strip()
    if not expr:
        return "No expression to evaluate."

    # Clean up spoken math
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")
    expr = expr.replace("plus", "+").replace("minus", "-")
    expr = expr.replace("times", "*").replace("divided by", "/")
    expr = expr.replace("to the power of", "**")
    expr = expr.replace("mod", "%").replace("modulo", "%")

    # Allow only safe characters
    safe_pattern = re.compile(r'^[\d\s\+\-\*\/\.\(\)\%\,]+$')
    # Also allow math function names
    func_pattern = re.compile(r'\b(sqrt|sin|cos|tan|log|log2|log10|abs|pow|round|pi|e)\b')

    clean = func_pattern.sub('', expr)
    if not safe_pattern.match(clean.replace(' ', '').replace(',', '')):
        return f"I cannot safely evaluate: {expr}"

    # Replace math functions with math module calls
    expr = re.sub(r'\bsqrt\b', 'math.sqrt', expr)
    expr = re.sub(r'\bsin\b', 'math.sin', expr)
    expr = re.sub(r'\bcos\b', 'math.cos', expr)
    expr = re.sub(r'\btan\b', 'math.tan', expr)
    expr = re.sub(r'\blog10\b', 'math.log10', expr)
    expr = re.sub(r'\blog2\b', 'math.log2', expr)
    expr = re.sub(r'\blog\b', 'math.log', expr)
    expr = re.sub(r'\bpi\b', 'math.pi', expr)
    expr = re.sub(r'\be\b', 'math.e', expr)
    expr = re.sub(r'\babs\b', 'abs', expr)
    expr = re.sub(r'\bround\b', 'round', expr)

    try:
        result = eval(expr, {"__builtins__": {}, "math": math, "abs": abs, "round": round, "pow": pow})
        # Format nicely
        if isinstance(result, float):
            if result == int(result):
                ans = str(int(result))
            else:
                ans = f"{result:.6g}"
        else:
            ans = str(result)

        # Copy answer to clipboard
        try:
            import pyperclip
            pyperclip.copy(ans)
        except Exception:
            try:
                escaped = ans.replace("'", "''")
                subprocess.run(
                    ["powershell", "-Command", f"Set-Clipboard '{escaped}'"],
                    timeout=3,
                )
            except Exception:
                pass

        return f"The answer is {ans}."
    except Exception as exc:
        return f"Could not calculate: {exc}"


def skill_weather(args: str) -> str:
    """Get weather info from wttr.in (free, no API key)."""
    location = args.strip() or "Indore"
    try:
        url = f"https://wttr.in/{urlparse.quote(location)}?format=j1"
        req = urlreq.Request(url, headers={"User-Agent": "FRIDAY/1.0"})
        with urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        current = data.get("current_condition", [{}])[0]
        temp_c = current.get("temp_C", "?")
        desc = current.get("weatherDesc", [{}])[0].get("value", "unknown")
        humidity = current.get("humidity", "?")
        feels = current.get("FeelsLikeC", "?")
        wind = current.get("windspeedKmph", "?")

        area = data.get("nearest_area", [{}])[0]
        city = area.get("areaName", [{}])[0].get("value", location)

        return (
            f"Weather in {city}: {desc}, {temp_c} degrees celsius, "
            f"feels like {feels} degrees. "
            f"Humidity is {humidity} percent. "
            f"Wind speed is {wind} kilometers per hour."
        )
    except Exception as exc:
        return f"Could not get weather: {exc}"


def skill_screenshot(args: str) -> str:
    """Take a screenshot and save it to Desktop."""
    try:
        from PIL import ImageGrab
    except ImportError:
        # Fallback: use PowerShell/Snipping Tool
        try:
            subprocess.Popen(["snippingtool", "/clip"], shell=True)
            return "Opened Snipping Tool for screenshot."
        except Exception:
            return "Screenshot requires Pillow: pip install Pillow"

    try:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(desktop, f"screenshot_{ts}.png")
        img = ImageGrab.grab()
        img.save(path)
        return f"Screenshot saved to Desktop as screenshot_{ts}.png."
    except Exception as exc:
        return f"Screenshot failed: {exc}"


def skill_file_search(args: str) -> str:
    """Search for files by name pattern on common locations."""
    pattern = args.strip().lower()
    if not pattern:
        return "Specify a filename or pattern to search for."

    search_dirs = [
        os.path.expanduser("~\\Desktop"),
        os.path.expanduser("~\\Documents"),
        os.path.expanduser("~\\Downloads"),
    ]

    found = []
    for root_dir in search_dirs:
        if not os.path.isdir(root_dir):
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                # Limit depth to 3 levels
                depth = dirpath.replace(root_dir, "").count(os.sep)
                if depth > 3:
                    dirnames.clear()
                    continue
                for fn in filenames:
                    if pattern in fn.lower():
                        found.append(os.path.join(dirpath, fn))
                        if len(found) >= 10:
                            break
                if len(found) >= 10:
                    break
        except PermissionError:
            continue

    if not found:
        return f"No files matching '{pattern}' found in Desktop, Documents, or Downloads."

    file_list = ", ".join(os.path.basename(f) for f in found[:5])
    return f"Found {len(found)} file{'s' if len(found) > 1 else ''}: {file_list}."


def skill_datetime(args: str) -> str:
    """Get current date, time, or day information."""
    now = datetime.datetime.now()
    query = args.strip().lower()

    if "date" in query:
        return f"Today is {now.strftime('%A, %d %B %Y')}."
    elif "day" in query:
        return f"Today is {now.strftime('%A')}."
    else:
        return f"It is {now.strftime('%I:%M %p')} on {now.strftime('%A, %d %B %Y')}."


def skill_remind(args: str) -> str:
    """Set a quick reminder (same as timer but with a message)."""
    # Parse "in Xm message" or "X seconds message"
    match = re.match(r'(?:in\s+)?(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)\s*(.*)', args.strip(), re.IGNORECASE)
    if not match:
        return "Format: remind in 5m check the oven"

    amount = int(match.group(1))
    unit = match.group(2).lower()
    message = match.group(3).strip() or "Time's up!"

    if unit.startswith("m"):
        seconds = amount * 60
    elif unit.startswith("h"):
        seconds = amount * 3600
    else:
        seconds = amount

    def _remind():
        time.sleep(seconds)
        _announce(f"Reminder: {message}")
        _play_beep(600, 300)
        _play_beep(880, 300)
        _play_beep(600, 300)
        # Cleanup: remove from active list
        _active_timers[:] = [t for t in _active_timers if t.get("thread") is not threading.current_thread()]

    remind_thread = threading.Thread(target=_remind, daemon=True, name=f"remind-{seconds}s")
    remind_thread.start()
    _active_timers.append({"seconds": seconds, "started": time.time(), "thread": remind_thread, "msg": message})

    if seconds >= 60:
        label = f"{seconds // 60} minute{'s' if seconds // 60 != 1 else ''}"
    else:
        label = f"{seconds} second{'s' if seconds != 1 else ''}"

    return f"Reminder set: '{message}' in {label}."


# ── Skill Registry ──────────────────────────────────────────────────────────

def skill_open_url(args: str) -> str:
    """Navigate the focused browser to a URL. Uses Ctrl+L to guarantee address bar focus."""
    import pyautogui
    import time
    url = args.strip()
    if not url:
        return "No URL provided."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        pyautogui.hotkey("ctrl", "l")   # focus address bar, always
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")   # clear existing URL
        time.sleep(0.1)
        pyautogui.typewrite(url, interval=0.01)
        time.sleep(0.1)
        pyautogui.press("enter")
        return f"Navigated to {url}"
    except Exception as exc:
        return f"Could not navigate browser: {exc}"


def skill_open_url_in_existing_browser(url: str) -> str:
    """Open a URL in the existing browser window, or launch a new one if not running."""
    import win32gui
    import win32con
    import pyautogui
    import time
    import subprocess

    # Find existing Edge window
    edge_hwnd = 0
    def _cb(h, _):
        nonlocal edge_hwnd
        t = win32gui.GetWindowText(h)
        if "edge" in t.lower() or "microsoft" in t.lower():
            if win32gui.IsWindowVisible(h):
                edge_hwnd = h
    win32gui.EnumWindows(_cb, None)

    if edge_hwnd:
        # Reuse existing Edge — open new tab and navigate
        try:
            from automation import force_foreground
            force_foreground(edge_hwnd)
        except Exception:
            try:
                win32gui.ShowWindow(edge_hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(edge_hwnd)
            except Exception:
                pass
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "t")   # new tab
        time.sleep(0.3)
    else:
        # No Edge open — launch it
        try:
            subprocess.Popen(["msedge.exe"])
            time.sleep(1.5)
        except Exception:
            import webbrowser
            webbrowser.open(url)
            return f"Opened {url} in default browser."

    # Navigate
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite(url, interval=0.01)
    time.sleep(0.1)
    pyautogui.press("enter")
    return f"Opened {url} in active browser."


def skill_whatsapp(args: str) -> str:
    """Send a WhatsApp message. Args format: 'contact||message' or 'message' (reuses last contact)."""
    import pyautogui
    import time
    global _last_whatsapp_target

    parts = args.split("||", 1)
    if len(parts) == 2:
        contact, message = parts[0].strip(), parts[1].strip()
        _last_whatsapp_target = contact
    else:
        message = parts[0].strip()
        contact = _last_whatsapp_target

    if not contact:
        return "No contact target specified for WhatsApp."
    if not message:
        return "No message content provided."

    # Open/focus WhatsApp
    skill_open_url_in_existing_browser("https://web.whatsapp.com")
    time.sleep(1.5)  # wait for tab to focus

    # Ctrl+Alt+/ is standard WhatsApp Web shortcut to focus contact search bar
    pyautogui.hotkey("ctrl", "alt", "/")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    time.sleep(0.1)
    pyautogui.typewrite(contact, interval=0.02)
    time.sleep(1.0)  # wait for results

    # Select contact and enter chat
    pyautogui.press("down")
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.5)

    # Send message
    pyautogui.typewrite(message, interval=0.01)
    time.sleep(0.1)
    pyautogui.press("enter")
    return f"Sent WhatsApp message to {contact}: '{message}'"


def skill_whatsapp_check_messages(args: str = "") -> str:
    """Checks WhatsApp Web for any new or unread messages using UI Automation screen reading."""
    import win32gui
    import time

    # Find and focus WhatsApp tab/Edge window
    edge_hwnd = 0
    def _cb(h, _):
        nonlocal edge_hwnd
        t = win32gui.GetWindowText(h)
        if "whatsapp" in t.lower() or "edge" in t.lower():
            if win32gui.IsWindowVisible(h):
                edge_hwnd = h
    win32gui.EnumWindows(_cb, None)

    if edge_hwnd:
        try:
            from automation import force_foreground
            force_foreground(edge_hwnd)
        except Exception:
            try:
                win32gui.ShowWindow(edge_hwnd, 9)
                win32gui.SetForegroundWindow(edge_hwnd)
            except Exception:
                pass
        time.sleep(0.5)

    # Read screen content
    content = skill_read_screen()
    lines = content.split("\n")
    unread_updates = []
    for line in lines:
        if "unread" in line.lower() or "new message" in line.lower() or "notification" in line.lower():
            unread_updates.append(line)

    if unread_updates:
        return "Found unread updates on WhatsApp:\n" + "\n".join(unread_updates)
    else:
        recent_lines = [l for l in lines if len(l) > 3][:10]
        if recent_lines:
            return "Checked WhatsApp screen, here is the active view:\n" + "\n".join(recent_lines)
        return "No unread messages or active chats found on WhatsApp screen."


def skill_clear_app(args: str = "") -> str:
    """Selects all content in the focused app and deletes it."""
    import pyautogui
    import time
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Cleared all content in focused window."


# ── Phase 4 skills ────────────────────────────────────────────────────────────

_RESULT_LIMIT = 500   # max chars spoken to avoid wall-of-text in voice

# Paths that file_write must never touch
_BLOCKED_WRITE_DIRS = [
    os.environ.get("SystemRoot", r"C:\Windows"),
    r"C:\Windows", r"C:\System32", r"C:\Program Files",
    r"C:\Program Files (x86)", "/etc", "/usr", "/bin", "/sbin",
]

# Search roots for find_and_open — limited to user directories only.
# C:\ removed: walking the entire drive is too slow and may trigger
# Windows Defender or permission delays.
_SEARCH_ROOTS = [
    os.path.expanduser("~\\Desktop"),
    os.path.expanduser("~\\Documents"),
    os.path.expanduser("~\\Downloads"),
    os.path.expanduser("~\\Pictures"),
    os.path.expanduser("~\\Videos"),
    os.path.expanduser("~\\Music"),
]

# Directories to skip during deep search (speeds up + avoids noise)
_SKIP_DIRS = {
    "Windows", "System32", "SysWOW64", "$Recycle.Bin", "ProgramData",
    "AppData\\Local\\Temp", "node_modules", ".git", "__pycache__",
    "WinSxS", "Temp",
}


def _truncate(text: str, limit: int = _RESULT_LIMIT) -> str:
    """Trim result to voice-safe length."""
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + f"… [{len(text)-limit} chars truncated]"
    return text


def _get_active_browser() -> Optional[str]:
    """
    Detect which browser is currently running using psutil.
    Returns the executable path/name to use for opening a URL,
    or None if no browser is found (caller falls back to webbrowser.open).
    Priority: Edge > Chrome > Brave > Firefox (matches most common Windows setups).
    """
    try:
        import psutil
        running = {p.name().lower() for p in psutil.process_iter(["name"])}
        for proc_name, exe in _BROWSER_PROC_MAP.items():
            if proc_name.lower() in running:
                return exe
    except Exception:
        pass
    return None


def _open_url_in_browser(url: str, browser_exe: Optional[str] = None) -> str:
    """
    Open a URL — in the active browser if detected, else via webbrowser.open().
    Always opens as a new tab in the existing window (--new-tab flag).
    """
    if browser_exe is None:
        browser_exe = _get_active_browser()

    if browser_exe:
        try:
            # --new-tab opens in the existing browser window
            subprocess.Popen([browser_exe, "--new-tab", url],
                             creationflags=subprocess.DETACHED_PROCESS)
            return browser_exe
        except Exception:
            pass   # fall through to webbrowser

    import webbrowser
    webbrowser.open(url)
    return "default browser"


def skill_smart_open(args: str) -> str:
    """
    Intelligently open a website or URL in the currently active browser.
    Knows common sites by name (youtube, github, gmail, reddit, etc.).
    Detects the running browser first — avoids duplicate windows.
    args: site name or full URL
    """
    target = args.strip().lower().strip('"\'')
    if not target:
        return "No site or URL provided."

    # Resolve friendly name to URL
    url = _SITE_MAP.get(target)
    if url is None:
        # Try partial match
        for key, val in _SITE_MAP.items():
            if key in target or target in key:
                url = val
                break
    if url is None:
        # Treat as raw URL
        url = target if target.startswith(("http://", "https://")) else "https://" + target

    # Detect running browser first — opens as new tab, no duplicate window
    active_browser = _get_active_browser()
    browser_exe = _open_url_in_browser(url, browser_exe=active_browser)
    browser_label = os.path.basename(browser_exe).replace('.exe', '').replace('.', '').title()
    if browser_label.lower() in ("default browser", "default", ""):
        browser_label = "browser"
    site_label = target.title()
    return f"Opening {site_label} in {browser_label}."


def skill_find_and_open(args: str) -> str:
    """
    Find a file by name across common directories, then open it.
    args: filename or partial name (e.g. 'resume.pdf', 'budget')
    Searches Desktop, Documents, Downloads, then the full user profile.
    """
    query = args.strip().strip('"\'').lower()
    if not query:
        return "No filename provided."

    found: list[str] = []
    _search_deadline = time.monotonic() + 5.0  # 5s hard timeout

    for root in _SEARCH_ROOTS:
        if not os.path.exists(root):
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if time.monotonic() > _search_deadline:
                    break  # 5s hard timeout
                # Prune skip dirs in-place so os.walk doesn't descend into them
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIRS and not d.startswith(".")
                ]
                for fname in filenames:
                    if time.monotonic() > _search_deadline:
                        break
                    if query in fname.lower():
                        found.append(os.path.join(dirpath, fname))
                if len(found) >= 10 or time.monotonic() > _search_deadline:
                    break   # stop at 10 matches or timeout
        except PermissionError:
            continue
        if found:
            break   # found in first matching root — stop searching deeper

    if not found:
        return f"No file matching '{args.strip()}' found in your common folders."

    # Open the best match (first found)
    best = found[0]
    try:
        os.startfile(best)   # Windows: opens with default associated app
        extra = f" ({len(found)-1} more match{'es' if len(found)-1 != 1 else ''} found)" if len(found) > 1 else ""
        return f"Opening {os.path.basename(best)}{extra}."
    except Exception as exc:
        return f"Found {os.path.basename(best)} but couldn't open it: {exc}"


def skill_youtube_search(args: str) -> str:
    """
    Search YouTube for a query, opening in the active browser.
    Tries to open the first video directly; falls back to the results page.
    args: search query (e.g. 'lofi music', 'python tutorial')
    """
    query = args.strip()
    if not query:
        return "No search query provided."

    results_url = "https://www.youtube.com/results?search_query=" + urlparse.quote_plus(query)

    # Try to fetch the first video URL from the results page
    video_url = None
    try:
        req = urlreq.Request(results_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlreq.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Extract first video ID from the results page HTML
        vid_match = re.search(r'/watch\?v=([a-zA-Z0-9_-]{11})', html)
        if vid_match:
            video_url = f"https://www.youtube.com/watch?v={vid_match.group(1)}"
    except Exception:
        pass  # fall back to results page

    url_to_open = video_url or results_url
    browser = _open_url_in_browser(url_to_open)
    browser_name = os.path.basename(browser).replace(".exe", "").title()
    if video_url:
        return f"Playing top result for '{query}' in {browser_name}."
    return f"Searching YouTube for '{query}' in {browser_name}."



def skill_file_read(args: str) -> str:
    """
    Read a file and return its first 200 lines.
    args: file path (absolute or relative to user home)
    """
    path = args.strip().strip('"\'')
    if not path:
        return "No file path provided."

    # Expand ~ and env vars
    path = os.path.expandvars(os.path.expanduser(path))

    if not os.path.exists(path):
        return f"File not found: {path}"
    if os.path.isdir(path):
        items = os.listdir(path)[:20]
        return f"That's a directory. Contents: {', '.join(items)}"
    if not os.access(path, os.R_OK):
        return f"Permission denied: {path}"

    try:
        size_kb = os.path.getsize(path) / 1024
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total = len(lines)
        preview = "".join(lines[:200])
        suffix = f"\n[{total} lines total, {size_kb:.1f} KB]" if total > 200 else f"\n[{total} lines, {size_kb:.1f} KB]"
        return _truncate(preview + suffix)
    except Exception as exc:
        return f"Error reading file: {exc}"


def skill_file_write(args: str) -> str:
    """
    Write content to a file.
    args: path||content   (pipe-delimited)
    Blocked: system directories (C:\\Windows, /etc, /usr, etc.)
    """
    if "||" not in args:
        return "Format: file_write:path||content"
    path, content = args.split("||", 1)
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"\'')))
    abs_path = os.path.abspath(path)

    # Security: block system directories
    for blocked in _BLOCKED_WRITE_DIRS:
        try:
            blocked_abs = os.path.abspath(blocked)
            if abs_path.startswith(blocked_abs):
                return f"Refused: writing to {blocked} is not allowed."
        except Exception:
            pass

    try:
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        lines_written = content.count("\n") + 1
        return f"Written {lines_written} line(s) to {abs_path}."
    except Exception as exc:
        return f"Error writing file: {exc}"


def skill_file_edit(args: str) -> str:
    """
    Surgical find-and-replace in a file.
    args: path||old_text||new_text
    Replaces the FIRST occurrence of old_text with new_text.
    Blocked: system directories (same as file_write).
    """
    parts = args.split("||", 2)
    if len(parts) != 3:
        return "Format: file_edit:path||old_text||new_text"
    path, old_text, new_text = parts
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"\'' )))
    abs_path = os.path.abspath(path)

    # Security: block system directories
    for blocked in _BLOCKED_WRITE_DIRS:
        try:
            blocked_abs = os.path.abspath(blocked)
            if abs_path.startswith(blocked_abs):
                return f"Refused: editing {blocked} is not allowed."
        except Exception:
            pass

    if not os.path.exists(abs_path):
        return f"File not found: {abs_path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        if old_text not in content:
            return f"Text not found in {os.path.basename(abs_path)}. No changes made."

        new_content = content.replace(old_text, new_text, 1)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return (
            f"Edited {os.path.basename(abs_path)}: replaced "
            f"{len(old_text)} chars with {len(new_text)} chars."
        )
    except Exception as exc:
        return f"Edit failed: {exc}"


def _validate_code_ast(code: str) -> Optional[str]:
    """AST-based code validator. Returns None if safe, or an error string."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error: {e}"

    _DANGEROUS_MODULES = {
        "os", "subprocess", "sys", "ctypes", "shutil", "socket",
        "requests", "urllib", "pathlib", "importlib", "code",
        "codeop", "pdb", "traceback", "inspect",
    }
    
    _BLOCKED_NAMES = {
        "eval", "exec", "compile", "open", "__import__", 
        "__builtins__", "__dict__", "globals", "locals", 
        "getattr", "setattr", "vars", "type", "__class__",
        "__bases__", "__subclasses__", "__mro__", "__code__",
        "__globals__"
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _DANGEROUS_MODULES:
                    return f"Refused: 'import {alias.name}' is not allowed in sandbox."

        if isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in _DANGEROUS_MODULES:
                    return f"Refused: 'from {node.module}' is not allowed in sandbox."

        if isinstance(node, ast.Name):
            if node.id in _BLOCKED_NAMES:
                return f"Refused: Use of '{node.id}' is not allowed in sandbox."

        if isinstance(node, ast.Attribute):
            if node.attr in _BLOCKED_NAMES:
                return f"Refused: Attribute access to '{node.attr}' is not allowed in sandbox."

        if isinstance(node, ast.Subscript):
            slice_node = node.slice
            # In older python versions, slice might be wrapped in ast.Index
            if hasattr(slice_node, "value"):
                slice_node = getattr(slice_node, "value")
            if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                val = slice_node.value
                if val in _BLOCKED_NAMES:
                    return f"Refused: Subscript access to '{val}' is not allowed in sandbox."

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    mod = node.func.value.id
                    if mod in _DANGEROUS_MODULES or mod in _BLOCKED_NAMES:
                        return f"Refused: '{mod}.{node.func.attr}' is not allowed in sandbox."

    return None


def skill_run_code(args: str) -> str:
    """
    Execute a code snippet in a sandboxed subprocess.
    args: lang:code  (e.g. python:print(1+1))
    Supported: python, python3, node, js
    Security: 10s timeout, AST-based code validation, isolated subprocess.
    """
    if ":" not in args:
        return "Format: run_code:python:print('hello')"
    lang, code = args.split(":", 1)
    lang = lang.strip().lower()

    # AST-based validation replaces the old substring blacklist
    err = _validate_code_ast(code)
    if err:
        return err

    lang_map = {"python": "python", "python3": "python", "node": "node", "js": "node"}
    interpreter = lang_map.get(lang)
    if not interpreter:
        return f"Unsupported language: {lang}. Use python or node."

    try:
        # Python: -I = isolated mode, -c = command
        # Node.js: -e = evaluate (no -I equivalent, no -c)
        if lang.startswith("python"):
            cmd = [interpreter, "-I", "-c", code]
        else:
            cmd = [interpreter, "-e", code]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=10,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if err and not out:
            return _truncate(f"Error: {err}")
        if err:
            return _truncate(f"Output: {out}\nStderr: {err}")
        return _truncate(out or "(no output)")
    except subprocess.TimeoutExpired:
        return "Code execution timed out after 10 seconds."
    except FileNotFoundError:
        return f"Interpreter '{interpreter}' not found on PATH."
    except Exception as exc:
        return f"Execution error: {exc}"


def skill_git_query(args: str) -> str:
    """
    Query a git repository (read-only: status, diff, log, branch).
    args: command  (status | diff | log | branch | show)
    Blocked: push, commit, reset, checkout, merge, rebase.
    """
    cmd = args.strip().lower().split()[0] if args.strip() else "status"

    _READONLY = {"status", "diff", "log", "branch", "show", "stash"}
    _BLOCKED_GIT = {"push", "commit", "reset", "checkout", "merge",
                    "rebase", "rm", "mv", "add", "clean", "fetch", "pull"}

    if cmd in _BLOCKED_GIT:
        return f"Refused: 'git {cmd}' is not allowed via skill (write operations only via terminal)."
    if cmd not in _READONLY:
        return f"Unknown git command: {cmd}. Use: status, diff, log, branch, show."

    git_args = args.strip().split()
    # Build safe git command
    git_cmd = ["git"] + git_args
    if cmd == "log":
        # Limit log output
        if "--oneline" not in git_args:
            git_cmd += ["--oneline", "-10"]

    try:
        result = subprocess.run(
            git_cmd,
            capture_output=True, text=True,
            timeout=10, cwd=os.getcwd(),
        )
        out = result.stdout.strip() or result.stderr.strip()
        return _truncate(out or f"git {cmd}: no output.")
    except subprocess.TimeoutExpired:
        return "git command timed out."
    except FileNotFoundError:
        return "git is not installed or not on PATH."
    except Exception as exc:
        return f"git error: {exc}"


def skill_browser_search(args: str) -> str:
    """
    Search something in the browser using the default search engine.
    args: search query
    """
    query = args.strip()
    if not query:
        return "No search query provided."
    try:
        import webbrowser
        url = "https://www.google.com/search?q=" + urlparse.quote_plus(query)
        webbrowser.open(url)
        return f"Searching for '{query}' in browser."
    except Exception as exc:
        return f"Could not open browser: {exc}"


# ── Phase 4+ skills — Desktop Automation & PowerShell ──────────────────────

def skill_powershell(args: str) -> str:
    """Run a PowerShell command (sandboxed, dangerous commands blocked)."""
    from automation import powershell_exec
    return powershell_exec(args)


def skill_type_text(args: str) -> str:
    """Type text at the current cursor position using keyboard simulation."""
    from automation import type_text
    return type_text(args)


def skill_press_keys(args: str) -> str:
    """Press a keyboard shortcut (e.g. ctrl+c, alt+tab, enter, ctrl+shift+n)."""
    from automation import press_hotkey
    return press_hotkey(args)


def skill_click(args: str) -> str:
    """Click at x,y coordinates or current cursor position. Args: 'x,y' or empty."""
    from automation import click_at
    return click_at(args)


def skill_scroll(args: str) -> str:
    """Scroll up or down. Args: 'up 5' or 'down 3'."""
    from automation import scroll_mouse
    return scroll_mouse(args)


def skill_focus_window(args: str) -> str:
    """Focus/switch to a window by title. Args: partial window title."""
    from automation import focus_window
    return focus_window(args)


def skill_list_windows(args: str) -> str:
    """List all visible windows with their titles."""
    from automation import list_windows
    return list_windows()


def skill_wait(args: str) -> str:
    """Wait for N seconds (max 30). Use for page/app load delays."""
    from automation import wait_seconds
    return wait_seconds(args)


def skill_process_list(args: str) -> str:
    """List running processes, optionally filtered. Args: filter name or empty."""
    from automation import list_processes
    return list_processes(args)


@dataclass
class SkillEngine:
    """
    Dispatches [ACTION:skill:args] tags from LLM output.
    """

    debug_fn: Optional[Callable] = None
    skills: Dict[str, Callable[[str], str]] = field(default_factory=dict)

    # Pattern: [ACTION:skill_name:arguments]  — tolerates whitespace around colons
    ACTION_PATTERN = re.compile(r'\[ACTION:\s*(\w+)\s*:\s*(.*?)\s*\]', re.DOTALL | re.IGNORECASE)
    # Fallback: catches common malformations (missing second colon, spaces)
    ACTION_FALLBACK = re.compile(r'\[ACTION:\s*(\w+)[\s:]+(.+?)\]', re.DOTALL | re.IGNORECASE)

    def __post_init__(self):
        self.skills = {
            "open_app":        skill_open_app,
            "close_app":       skill_close_app,
            "open_url":        skill_open_url,
            "web_search":      skill_web_search,
            "system_info":     skill_system_info,
            "clipboard_read":  skill_clipboard_read,
            "clipboard_write": skill_clipboard_write,
            "volume":          skill_volume,
            "timer":           skill_timer,
            "calculator":      skill_calculator,
            "weather":         skill_weather,
            "screenshot":      skill_screenshot,
            "file_search":     skill_file_search,
            "datetime":        skill_datetime,
            "remind":          skill_remind,
            # Phase 4 — Code, file, git, browser
            "file_read":       skill_file_read,
            "file_write":      skill_file_write,
            "file_edit":       skill_file_edit,
            "run_code":        skill_run_code,
            "git_query":       skill_git_query,
            "browser_search":  skill_browser_search,
            # Smart open, file finder, YouTube
            "smart_open":      skill_smart_open,
            "find_and_open":   skill_find_and_open,
            "youtube_search":  skill_youtube_search,
            # Phase 4+ — Desktop Automation & PowerShell
            "powershell":      skill_powershell,
            "type_text":       skill_type_text,
            "press_keys":      skill_press_keys,
            "click":           skill_click,
            "scroll":          skill_scroll,
            "focus_window":    skill_focus_window,
            "list_windows":    skill_list_windows,
            "wait":            skill_wait,
            "process_list":    skill_process_list,
            "close_tab":       skill_close_tab,
            "read_screen":     skill_read_screen,
            "open_url_in_existing_browser": skill_open_url_in_existing_browser,
            "whatsapp":        skill_whatsapp,
            "whatsapp_check_messages": skill_whatsapp_check_messages,
            "clear_app":       skill_clear_app,
        }

    def _debug(self, msg: str) -> None:
        if self.debug_fn:
            self.debug_fn(f"[skills] {msg}")

    def list_skills(self) -> str:
        """Human-readable list of available skills."""
        lines = []
        for name, fn in self.skills.items():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            lines.append(f"  {name}: {doc}")
        return "Available skills:\n" + "\n".join(lines)

    def _fuzzy_match_skill(self, name: str):
        """Fuzzy-match an unknown skill name to the closest known skill."""
        from difflib import get_close_matches
        matches = get_close_matches(name, self.skills.keys(), n=1, cutoff=0.6)
        return matches[0] if matches else None

    def validate_action_tags(self, text: str) -> tuple:
        """Check if [ACTION:] tags in text are well-formed and use valid skills.
        Returns (is_valid: bool, error_description: str).
        """
        if "[ACTION" not in text:
            return True, ""

        matches = list(self.ACTION_PATTERN.finditer(text))
        if not matches:
            # Tag marker present but primary regex failed — try fallback
            fallback = list(self.ACTION_FALLBACK.finditer(text))
            if not fallback:
                return False, (
                    "Malformed action tag. "
                    "Correct format: [ACTION:skill_name:arguments]"
                )
            matches = fallback

        for match in matches:
            skill_name = match.group(1).strip()
            if skill_name not in self.skills:
                closest = self._fuzzy_match_skill(skill_name)
                if not closest:
                    return False, (
                        f"Unknown skill '{skill_name}'. "
                        f"Available: {', '.join(sorted(self.skills.keys()))}"
                    )
        return True, ""

    def extract_and_execute(self, text: str) -> tuple[str, list[str]]:
        """
        Find all [ACTION:...] tags in text, execute them,
        and return (cleaned_text, list_of_results).
        Uses fuzzy matching for unknown skill names.
        Falls back to a lenient regex for malformed tags.
        """
        results = []
        matches = list(self.ACTION_PATTERN.finditer(text))

        # Fallback regex for malformed tags
        if not matches and "[ACTION" in text:
            matches = list(self.ACTION_FALLBACK.finditer(text))
            if matches:
                self._debug("Used fallback regex for malformed action tag")

        if not matches:
            return text, []

        for match in matches:
            skill_name = match.group(1).strip()
            skill_args = match.group(2).strip()

            # Fuzzy matching: auto-correct slightly wrong skill names
            fn = self.skills.get(skill_name)
            if fn is None:
                closest = self._fuzzy_match_skill(skill_name)
                if closest:
                    self._debug(f"Fuzzy matched '{skill_name}' → '{closest}'")
                    skill_name = closest
                    fn = self.skills[closest]

            self._debug(f"Executing skill: {skill_name}({skill_args})")

            if fn:
                try:
                    result = fn(skill_args)
                    results.append(result)
                    self._debug(f"Skill result: {result[:100]}")
                except Exception as exc:
                    error_msg = f"Skill {skill_name} failed: {exc}"
                    results.append(error_msg)
                    self._debug(error_msg)
            else:
                available = ", ".join(sorted(self.skills.keys()))
                results.append(f"Unknown skill: {skill_name}. Try: {available}")

        # Remove action tags from text (both patterns)
        cleaned = self.ACTION_PATTERN.sub("", text)
        cleaned = self.ACTION_FALLBACK.sub("", cleaned).strip()
        # Clean up leftover whitespace
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        cleaned = re.sub(r'  +', ' ', cleaned)

        return cleaned, results

    def get_skill_prompt(self) -> str:
        """
        Returns instruction block for the LLM system prompt
        so it knows how to invoke skills.
        """
        return (
            "TOOL USE:\n"
            "You can perform real actions by including action tags in your response.\n"
            "Format: [ACTION:skill_name:arguments]\n\n"
            "═══ APPS & SYSTEM ═══\n"
            "  [ACTION:open_app:chrome] — open an application\n"
            "  [ACTION:close_app:notepad] — close an application\n"
            "  [ACTION:system_info:] — CPU, RAM, GPU/VRAM, battery\n"
            "  [ACTION:process_list:] — list running processes (optional filter name)\n"
            "  [ACTION:volume:up/down/mute/unmute] — control volume\n"
            "  [ACTION:clipboard_read:] — read clipboard\n"
            "  [ACTION:clipboard_write:text] — write to clipboard\n"
            "  [ACTION:screenshot:] — screenshot to Desktop\n"
            "  [ACTION:timer:30s or 5m] — countdown timer\n"
            "  [ACTION:remind:5m check oven] — timed reminder\n"
            "  [ACTION:calculator:2+2*3] — math evaluation\n"
            "  [ACTION:weather:Indore] — weather info\n"
            "  [ACTION:datetime:] — current date/time\n\n"
            "═══ BROWSER & WEB ═══\n"
            "  [ACTION:smart_open:youtube] — open named site in active browser\n"
            "  [ACTION:open_url:github.com] — open any URL\n"
            "  [ACTION:browser_search:query] — Google search in browser\n"
            "  [ACTION:youtube_search:lofi music] — play first YouTube result\n"
            "  [ACTION:web_search:query] — fetch search results as text\n\n"
            "═══ FILES & CODE ═══\n"
            "  [ACTION:file_read:/path/to/file.py] — read file (200 lines)\n"
            "  [ACTION:file_write:/path/file.txt||content] — write/create file\n"
            "  [ACTION:file_edit:/path/file.py||old_text||new_text] — surgical find-and-replace\n"
            "  [ACTION:file_search:report.pdf] — find files in common folders\n"
            "  [ACTION:find_and_open:resume.pdf] — find and open a file\n"
            "  [ACTION:run_code:python:print(1+2)] — run code (python/node, 10s)\n"
            "  [ACTION:git_query:status] — git status/diff/log/branch (read-only)\n\n"
            "═══ DESKTOP AUTOMATION ═══\n"
            "  [ACTION:type_text:Hello World] — type text at cursor position\n"
            "  [ACTION:press_keys:ctrl+c] — press keyboard shortcut\n"
            "  [ACTION:click:500,300] — click at x,y coordinates\n"
            "  [ACTION:scroll:down 5] — scroll up/down N clicks\n"
            "  [ACTION:focus_window:Chrome] — focus/switch to window by title\n"
            "  [ACTION:list_windows:] — list all visible windows\n"
            "  [ACTION:wait:3] — wait N seconds (for page loads etc.)\n"
            "  [ACTION:powershell:Get-Process] — run PowerShell command (sandboxed)\n\n"
            "═══ RULES ═══\n"
            "BROWSER: Use smart_open for named sites. Use youtube_search to play videos.\n"
            "  smart_open detects the active browser and opens in it.\n"
            "DESKTOP AUTOMATION WORKFLOW (for interacting with apps/websites):\n"
            "  1. smart_open or open_app to launch target\n"
            "  2. wait:2-3 for it to load\n"
            "  3. focus_window to ensure it's active\n"
            "  4. type_text / press_keys / click to interact\n"
            "  Example: open Claude AI, wait, focus, click text field, type prompt\n"
            "SECURITY:\n"
            "  file_write: blocked for C:\\Windows, system dirs. Safe paths are allowed.\n"
            "  run_code: no os.system, subprocess, network, file I/O in sandboxed code\n"
            "  powershell: destructive commands (format disk, delete system files) blocked\n"
            "  git_query: read-only (no push/commit/reset)\n"
            "ACTIONS: Use action tags ONLY when user asks to DO something.\n"
            "  For questions, answer normally without action tags.\n"
            "  You may include ONE action tag per response. Put it at the END.\n"
            "  After the action tag, do NOT add any more text.\n"
        )


# ── Pre-LLM Intent Detector ───────────────────────────────────────────────
# Qwen3 8B frequently ignores the [ACTION:...] tag format and responds
# conversationally ("Sure, opening notepad for you.") without actually
# invoking the skill. This fast regex-based detector intercepts obvious
# commands BEFORE they reach the LLM, ensuring instant + reliable execution.
#
# Returns (skill_name, skill_args, spoken_response) or None if no match.
# ────────────────────────────────────────────────────────────────────────────

def detect_direct_intent(user_input: str) -> Optional[tuple[str, str, str]]:
    """
    Fast pre-LLM intent detector for obvious skill commands.

    Returns (skill_name, skill_args, spoken_response) if a clear intent
    is detected, or None if the input should go to the LLM.
    """
    text = user_input.strip()
    lower = text.lower()
    # Normalize common speech artifacts
    clean = re.sub(r"[^a-z0-9\s]", " ", lower)
    clean = re.sub(r"\s+", " ", clean).strip()

    # ── Open app ────────────────────────────────────────────────────────
    m = re.match(
        r"(?:please\s+)?(?:can you\s+)?(?:hey\s+friday\s+)?"
        r"(?:open|launch|start|run)\s+(.+)",
        clean,
    )
    if m:
        app_raw = m.group(1).strip()
        # Remove trailing filler ("for me", "please", "now")
        app_raw = re.sub(r"\s+(for me|please|now|real quick|quickly)$", "", app_raw)

        # Check if it's a website (smart_open) vs a desktop app (open_app)
        if app_raw in _SITE_MAP:
            return ("smart_open", app_raw, f"Opening {app_raw.title()} for you, sir.")
        if app_raw in _APP_REGISTRY:
            return ("open_app", app_raw, f"Opening {app_raw.title()} for you, sir.")
        # Could be an app not in registry — still try open_app
        # but only if it's a single word (avoids "open the door" etc.)
        if len(app_raw.split()) <= 2 and not any(
            w in app_raw for w in ("the", "a", "my", "this", "that", "door", "window", "curtain")
        ):
            return ("open_app", app_raw, f"Opening {app_raw} for you, sir.")

    # ── Close app ───────────────────────────────────────────────────────
    m = re.match(
        r"(?:please\s+)?(?:can you\s+)?"
        r"(?:close|kill|stop|quit|exit|terminate|end)\s+(.+)",
        clean,
    )
    if m:
        app_raw = m.group(1).strip()
        app_raw = re.sub(r"\s+(for me|please|now)$", "", app_raw)
         # Route tab/site close commands to close_tab, not close_app
        _TAB_HINTS = {"tab", "this tab", "the tab", "current tab"}
        _SITE_CLOSE_HINTS = {
            "youtube", "gmail", "github", "reddit", "twitter",
            "netflix", "whatsapp", "instagram", "google",
            "linkedin", "stackoverflow", "wikipedia", "spotify",
            "chatgpt", "claude", "perplexity", "copilot",
        }
        _PRONOUNS = {"it", "this", "that", "them", "app", "the app", "application", "the application", "window", "the window", "program", "the program"}
        
        is_tab = "tab" in app_raw.lower()
        is_site = any(site in app_raw.lower() for site in _SITE_CLOSE_HINTS)
        
        if app_raw in _PRONOUNS:
            # Skip direct routing; let LLM resolve the context-dependent pronoun/noun
            pass
        elif is_tab or is_site or app_raw in _TAB_HINTS:
            return ("close_tab", app_raw, f"Closing {app_raw} tab.")
        elif app_raw and len(app_raw.split()) <= 3:
            return ("close_app", app_raw, f"Closing {app_raw}.")

    # ── Volume ──────────────────────────────────────────────────────────
    if re.match(r"(?:turn\s+)?volume\s+(up|down|mute|unmute)", clean):
        action = re.search(r"(up|down|mute|unmute)", clean).group(1)
        return ("volume", action, f"Volume {action}.")
    if clean in ("mute", "unmute", "mute volume", "unmute volume"):
        action = "mute" if "unmute" not in clean else "unmute"
        return ("volume", action, f"Volume {action}.")
    
    # Match: "volume 100", "volume 50%", "set volume to max", "turn volume to 50", etc.
    volume_match = re.match(
        r"(?:set\s+|turn\s+)?volume\s+(?:to\s+)?(\d+%?|max|maximum|full|high|medium|half|low|min|minimum|zero|off)",
        clean
    )
    if volume_match:
        action = volume_match.group(1)
        return ("volume", action, f"Setting volume to {action}.")

    # ── Weather ─────────────────────────────────────────────────────────
    m = re.match(
        r"(?:what(?:'s| is) the )?weather\s*(?:in\s+(.+))?|"
        r"(?:how(?:'s| is) the )?weather\s*(?:in\s+(.+))?|"
        r"(?:what(?:'s| is) the )?temperature\s*(?:in\s+(.+))?",
        clean,
    )
    if m:
        location = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        return ("weather", location or "Indore", "Let me check the weather.")

    # ── Timer ───────────────────────────────────────────────────────────
    m = re.match(
        r"(?:set\s+(?:a\s+)?)?timer\s+(?:for\s+)?(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)",
        clean,
    )
    if m:
        amount = m.group(1)
        unit = m.group(2)[0]  # s, m, or h
        return ("timer", f"{amount}{unit}", f"Setting a timer for {amount} {m.group(2)}.")

    # ── Remind ──────────────────────────────────────────────────────────
    m = re.match(
        r"remind\s+(?:me\s+)?(?:to\s+)?(?:in\s+)?(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)\s*(.*)",
        clean,
    )
    if m:
        amount = m.group(1)
        unit = m.group(2)[0]
        message = m.group(3).strip() or "Time's up"
        return ("remind", f"{amount}{unit} {message}", f"Reminder set.")

    # ── Screenshot ──────────────────────────────────────────────────────
    if re.match(r"(?:take\s+(?:a\s+)?)?screenshot", clean):
        return ("screenshot", "", "Taking a screenshot.")

    # ── Date/time ───────────────────────────────────────────────────────
    if clean in (
        "what time is it", "whats the time", "what is the time",
        "current time", "time", "time please",
    ):
        return ("datetime", "time", "")
    if clean in (
        "what day is it", "whats the date", "what is the date",
        "current date", "date", "todays date", "what date is it",
    ):
        return ("datetime", "date", "")

    # ── System info ─────────────────────────────────────────────────────
    if any(p in clean for p in (
        "system info", "system status", "cpu usage", "ram usage",
        "battery status", "battery level", "how much ram", "how much battery",
        "how is my system", "system health",
    )):
        return ("system_info", "", "Checking system status.")

    # ── Read Screen ─────────────────────────────────────────────────────
    if any(p in clean for p in (
        "read screen", "read my screen", "what s on my screen", "what's on my screen", "what is on my screen",
        "read this for me", "read this", "what do you see on my screen",
        "what do you see", "analyze screen", "analyze my screen"
    )):
        return ("read_screen", "", "Let me read your screen.")

    # ── Calculator ──────────────────────────────────────────────────────
    m = re.match(
        r"(?:calculate|what(?:'s| is)|compute|solve)\s+(.+)",
        clean,
    )
    if m:
        expr = m.group(1).strip()
        # Only match if it looks like a math expression
        if re.search(r"\d", expr) and re.search(r"[+\-*/^%]|(?:plus|minus|times|divided|power|sqrt|root)", expr):
            return ("calculator", expr, "")

    # ── YouTube search ──────────────────────────────────────────────────
    m = re.match(
        r"(?:play|search|find)\s+(.+?)\s+(?:on|in)\s+youtube|"
        r"(?:youtube|yt)\s+(?:search\s+)?(.+)|"
        r"(?:play|search for)\s+(.+?)\s+on\s+youtube|"
        r"play\s+(.+)",
        clean,
    )
    if m:
        query = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "").strip()
        if query and ("youtube" in clean or "yt" in clean or "play" in clean):
            # Don't match "play" for app names — only for media-like queries
            if len(query.split()) >= 1 and query not in _APP_REGISTRY:
                # If matched via the generic "play <query>" pattern (m.group(4) is not None)
                # and there is no explicit "youtube" or "yt" keyword in user request:
                if m.group(4) is not None and not any(w in clean for w in ("youtube", "yt")):
                    # Require media keywords or longer phrase to direct route; otherwise let LLM decide!
                    _MEDIA_WORDS = {"song", "music", "video", "playlist", "mix", "album", "audio", "track", "live", "cover", "sing", "band", "soundtrack", "concert"}
                    if not (any(w in query for w in _MEDIA_WORDS) or len(query.split()) >= 3):
                        return None
                return ("youtube_search", query, f"Searching YouTube for {query}.")

    # ── Web search ──────────────────────────────────────────────────────
    m = re.match(
        r"(?:search|google|look up|find)\s+(?:for\s+)?(.+?)(?:\s+(?:on|in)\s+(?:the\s+)?(?:web|internet|google|online))?$",
        clean,
    )
    if m:
        query = m.group(1).strip()
        # Only match if the query has substance and looks like a search
        if (
            len(query.split()) >= 2
            and query not in _APP_REGISTRY
            and query not in _SITE_MAP
            and not query.startswith(("for ", "the "))
        ):
            return ("browser_search", query, f"Searching for {query}.")

    # ── Clipboard ───────────────────────────────────────────────────────
    if clean in ("read clipboard", "paste clipboard", "whats in clipboard",
                 "what is in clipboard", "clipboard", "show clipboard"):
        return ("clipboard_read", "", "Reading clipboard.")
    m = re.match(r"copy\s+(.+?)(?:\s+to\s+clipboard)?$", clean)
    if m:
        text_to_copy = m.group(1).strip()
        if text_to_copy:
            return ("clipboard_write", text_to_copy, "Copied to clipboard.")

    # ── WhatsApp check/send direct routing ──────────────────────────────
    global _last_whatsapp_target

    # Check for unread or new messages
    if any(p in clean for p in ("check whatsapp", "any new messages", "get a new message", "whatsapp notifications")):
        return ("whatsapp_check_messages", "", "Checking WhatsApp messages for you.")

    # Match corrections
    if _last_whatsapp_target:
        correction_match = re.match(
            r"(?:no\s+|not\s+that\s+|i\s+meant\s+|tell\s+him\s+|tell\s+her\s+|say\s+|send\s+)(.+)",
            clean
        )
        if correction_match:
            corrected_msg = correction_match.group(1).strip()
            # Strip sub-verbs like "say" or "send" if present at the start of corrected_msg
            corrected_msg = re.sub(r"^(?:say\s+|send\s+|tell\s+him\s+|tell\s+her\s+)", "", corrected_msg).strip()
            if len(corrected_msg.split()) > 0 and corrected_msg not in ("yes", "no", "ok", "sure"):
                return ("whatsapp", f"{_last_whatsapp_target.lower()}||{corrected_msg}", f"Correcting message to {_last_whatsapp_target.title()}.")

    # Match new WhatsApp send message
    msg_match = re.match(
        r"(?:please\s+)?(?:can you\s+)?(?:send\s+)?(?:a\s+)?(?:message|whatsapp)\s+to\s+(\w+)\s+(?:saying|that|to)\s+(.+)|"
        r"(?:please\s+)?(?:can you\s+)?(?:whatsapp|message|tell)\s+(\w+)\s+(?:saying|that|to)?\s*(.+)",
        clean,
    )
    if msg_match:
        contact = (msg_match.group(1) or msg_match.group(3) or "").strip()
        message = (msg_match.group(2) or msg_match.group(4) or "").strip()
        if contact and message and contact.lower() not in ("me", "him", "her", "them"):
            _last_whatsapp_target = contact
            return ("whatsapp", f"{contact}||{message}", f"Sending WhatsApp message to {contact.title()}.")

    # ── Clear content direct routing ────────────────────────────────────
    if re.match(r"(?:please\s+)?(?:clear|erase|delete|wipe)\s+(everything|all|the\s+content|it|notepad|the\s+notepad)", clean):
        return ("clear_app", "", "Clearing the screen.")

    # ── Switch to / focus window direct routing ──────────────────────────
    switch_match = re.match(
        r"(?:please\s+)?(?:can you\s+)?(?:switch\s+to|go\s+to|focus\s+on)\s+(.+)",
        clean
    )
    if switch_match:
        app_raw = switch_match.group(1).strip()
        app_raw = re.sub(r"\s+(for me|please|now)$", "", app_raw)
        if app_raw and len(app_raw.split()) <= 3:
            return ("focus_window", app_raw, f"Switching to {app_raw.title()}.")

    # No clear intent detected — let the LLM handle it
    return None
