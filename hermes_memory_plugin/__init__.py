"""Hermes Agent MemoryProvider plugin: mimir's recall as Hermes' memory backend.

Install (no git repo required — a local copy works):

    cp -r hermes_memory_plugin ~/.hermes/plugins/mimir-memory

Then set `memory.provider: mimir` in Hermes' config.yaml. `mimir` must be
importable in the same Python environment Hermes runs in (pip install
'mimir[cognee]'). Only one memory provider is active at a time in Hermes —
this replaces whatever provider was configured before it.
"""
from __future__ import annotations

from mimir.hermes_memory import MimirMemoryProvider


def register(ctx):
    ctx.register_memory_provider(MimirMemoryProvider())
