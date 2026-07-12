"""hermes_plugin — glue between Hermes' register_hook API and mimir.capture.

A fake ctx stands in for Hermes' real one; we only depend on register_hook existing.
"""
import json

import hermes_plugin


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_register_wires_post_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(tmp_path / "episodes.jsonl"))
    ctx = FakeCtx()

    hermes_plugin.register(ctx)

    assert "post_tool_call" in ctx.hooks
    ctx.hooks["post_tool_call"]("execute_command", {"cmd": "pytest"}, RuntimeError("boom"))

    log = tmp_path / "episodes.jsonl"
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "execute_command"
    assert row["outcome_score"] == 0.0
