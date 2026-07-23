"""Hook installation: merge Mimir's capture hook into Claude Code's
settings.json, or write the PostToolUse script Cline picks up automatically.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sysconfig
from pathlib import Path
from typing import Iterable

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_COMMAND = "mimir-hook"
HOOK_EVENTS = ("PostToolUse", "SessionEnd")

MCP_SERVER_NAME = "mimir"
MCP_SERVER_COMMAND = "mimir-serve"

# Cline has no settings.json to merge into — it picks up an executable script named after
# the hook event from this directory (global scope; see docs.cline.bot/features/hooks).
DEFAULT_CLINE_HOOKS_DIR = Path.home() / "Documents" / "Cline" / "Rules" / "Hooks"
CLINE_HOOK_NAME = "PostToolUse"
CLINE_HOOK_COMMAND = "mimir-hook-cline"


# ---- resolving where an installed console script actually lives -----------

def _resolve_command(name: str) -> str:
    """Absolute path to the installed console script `name`, if we can find
    one -- otherwise `name` unchanged.

    Registration runs once, in this process. The hook or MCP server it
    points at runs later, in a different shell (Claude Code's bash, a fresh
    PowerShell) that may not share this process's PATH -- most commonly a
    `pip install --user` on Windows, whose Scripts directory isn't on PATH
    by default. Resolving to an absolute path now avoids a "command not
    found" later in a shell we don't control.
    """
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if os.name == "nt" else name
    for scheme in (sysconfig.get_default_scheme(), "nt_user" if os.name == "nt" else "posix_user"):
        try:
            scripts_dir = Path(sysconfig.get_path("scripts", scheme=scheme))
        except (KeyError, ValueError):
            continue
        candidate = scripts_dir / exe
        if candidate.exists():
            return str(candidate)
    return name


# ---- pure, testable settings merge ----------------------------------------

def add_hook_command(settings: dict, event: str, command: str) -> dict:
    """Return a new settings dict with `command` registered under hook `event`.

    Idempotent: if the command is already present for that event, the *same*
    input object is returned unchanged. Never mutates the argument.
    """
    hooks = dict(settings.get("hooks", {}))
    groups = [dict(g) for g in hooks.get(event, [])]
    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == command:
                return settings  # already registered — no-op
    groups.append({"hooks": [{"type": "command", "command": command}]})
    hooks[event] = groups
    return {**settings, "hooks": hooks}


def hook_block(command: str = HOOK_COMMAND,
               events: Iterable[str] = HOOK_EVENTS) -> dict:
    """The settings.json fragment that registers the capture hook."""
    settings: dict = {}
    for event in events:
        settings = add_hook_command(settings, event, command)
    return settings


# ---- IO around the merge ---------------------------------------------------

def _load_settings(path: Path) -> dict:
    """Load settings.json, or {} if absent/empty. Refuse to touch invalid JSON
    so we never clobber a file we can't safely round-trip."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}); refusing to overwrite. "
            "Fix it, or run `mimir install-hook --print` and paste the block yourself."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; refusing to overwrite.")
    return data


def install_hook(settings_path: Path = DEFAULT_SETTINGS, *,
                 command: str = HOOK_COMMAND,
                 events: Iterable[str] = HOOK_EVENTS) -> str:
    """Merge the capture hook into settings.json. Idempotent; backs up first."""
    command = _resolve_command(command)
    settings = _load_settings(settings_path)
    updated = settings
    for event in events:
        updated = add_hook_command(updated, event, command)
    if updated is settings:
        return f"already registered in {settings_path}"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        backup = settings_path.parent / (settings_path.name + ".bak")
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
    settings_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return f"registered {command} for {', '.join(events)} in {settings_path}"


def register_mcp_server(*, name: str = MCP_SERVER_NAME,
                        command: str = MCP_SERVER_COMMAND) -> str:
    """Register `mimir-serve` as an MCP server via the Claude Code CLI
    (`claude mcp add`). Skips, rather than errors, when `claude` isn't on
    PATH -- setup should still succeed for hook-only capture use. Idempotent:
    an "already exists" failure is reported as success, not retried."""
    claude = shutil.which("claude")
    if claude is None:
        return "claude CLI not found on PATH -- skipped MCP registration (capture hook still installed)"
    command = _resolve_command(command)
    result = subprocess.run(
        [claude, "mcp", "add", name, "--", command],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return f"registered {command} as MCP server '{name}'"
    output = f"{result.stderr}{result.stdout}".lower()
    if "already exists" in output:
        return f"MCP server '{name}' already registered"
    return f"MCP registration failed: {(result.stderr or result.stdout).strip()}"


def cline_hook_script(command: str = CLINE_HOOK_COMMAND) -> str:
    """The executable script Cline invokes for PostToolUse (POSIX shell)."""
    return f"#!/usr/bin/env sh\nexec {command}\n"


def install_cline_hook(hooks_dir: Path = DEFAULT_CLINE_HOOKS_DIR, *,
                       command: str = CLINE_HOOK_COMMAND) -> str:
    """Write the PostToolUse hook script Cline picks up automatically. It's mimir's own
    file (nothing to merge, unlike Claude Code's settings.json), so this just overwrites."""
    command = _resolve_command(command)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script_path = hooks_dir / CLINE_HOOK_NAME
    # newline="\n": the shebang line must stay LF-only even when written on Windows,
    # or a POSIX `sh` fails to resolve "/usr/bin/env sh\r" (bad-interpreter error).
    script_path.write_text(cline_hook_script(command), encoding="utf-8", newline="\n")
    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass  # ponytail: no POSIX exec bit on Windows; Cline's Windows invocation is unconfirmed
    return f"wrote {script_path}"
