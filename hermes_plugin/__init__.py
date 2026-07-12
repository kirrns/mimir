"""Hermes Agent plugin: captures post_tool_call failures into Mimir's EPISODE log.

Install (Hermes has no git-required install path — a local copy works fine):

    cp -r hermes_plugin ~/.hermes/plugins/mimir
    # or project scope: cp -r hermes_plugin .hermes/plugins/mimir
    #   (needs HERMES_ENABLE_PROJECT_PLUGINS=true)

`mimir` must be importable in the same Python environment Hermes runs in
(pip install mimir). Override the log location with MIMIR_EPISODE_LOG.
"""
from __future__ import annotations

import os
from pathlib import Path

from mimir.capture import capture, from_hermes_call

DEFAULT_LOG = Path.home() / ".mimir" / "episodes.jsonl"


def _log_path() -> Path:
    return Path(os.environ.get("MIMIR_EPISODE_LOG", str(DEFAULT_LOG)))


def register(ctx):
    """Hermes calls this on plugin load; wires capture into post_tool_call."""

    def on_post_tool_call(tool_name, params, result):
        capture(from_hermes_call(tool_name, params, result), log_path=_log_path())

    ctx.register_hook("post_tool_call", on_post_tool_call)
