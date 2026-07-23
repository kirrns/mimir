import json
import logging

import pytest

from mimir.hook_install import (
    add_hook_command,
    cline_hook_script,
    install_cline_hook,
    install_hook,
    register_mcp_server,
)


def test_add_hook_command_is_idempotent():
    once = add_hook_command({}, "PostToolUse", "mimir-hook")
    twice = add_hook_command(once, "PostToolUse", "mimir-hook")
    assert twice is once  # second add is a no-op, no duplicate
    groups = once["hooks"]["PostToolUse"]
    n = sum(e["command"] == "mimir-hook" for g in groups for e in g["hooks"])
    assert n == 1


def test_add_hook_command_preserves_existing_and_does_not_mutate():
    existing = {"hooks": {"PostToolUse": [
        {"hooks": [{"type": "command", "command": "other-tool"}]}]}}
    updated = add_hook_command(existing, "PostToolUse", "mimir-hook")
    cmds = [e["command"] for g in updated["hooks"]["PostToolUse"] for e in g["hooks"]]
    assert cmds == ["other-tool", "mimir-hook"]
    # original untouched (immutability)
    assert len(existing["hooks"]["PostToolUse"]) == 1
    assert existing["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "other-tool"


def test_install_hook_writes_backs_up_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

    msg = install_hook(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"  # unrelated settings preserved
    assert "PostToolUse" in data["hooks"] and "SessionEnd" in data["hooks"]
    assert (tmp_path / "settings.json.bak").exists()  # backup made
    assert "registered" in msg

    msg2 = install_hook(settings)  # second run is a no-op
    assert "already registered" in msg2


def test_install_cline_hook_writes_executable_script(tmp_path):
    hooks_dir = tmp_path / "Hooks"
    msg = install_cline_hook(hooks_dir)
    script = hooks_dir / "PostToolUse"
    assert script.exists()
    assert "mimir-hook-cline" in script.read_text(encoding="utf-8")
    assert str(script) in msg

    install_cline_hook(hooks_dir)  # re-running just overwrites; no error, still one file
    assert list(hooks_dir.iterdir()) == [script]


def test_install_cline_hook_uses_lf_line_endings_not_crlf(tmp_path):
    """The shebang line must stay POSIX-clean even when written on Windows -- a
    trailing \\r on '#!/usr/bin/env sh' breaks interpreter resolution."""
    hooks_dir = tmp_path / "Hooks"
    install_cline_hook(hooks_dir)
    raw = (hooks_dir / "PostToolUse").read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"#!/usr/bin/env sh\n")


def test_cline_hook_script_execs_given_command():
    assert "exec mimir-hook-cline" in cline_hook_script()
    assert "exec other-cmd" in cline_hook_script("other-cmd")


def test_ensure_utf8_stdio_reconfigures_streams_that_support_it(monkeypatch):
    """Windows terminals often default to a legacy codepage that mangles the
    em-dashes in Mimir's CLI text into mojibake; the CLI entry points call this
    once so every print() doesn't have to avoid non-ASCII."""
    import mimir.cli as cli

    calls = []

    class _Reconfigurable:
        def reconfigure(self, encoding):
            calls.append(encoding)

    class _NotReconfigurable:
        pass  # e.g. some redirected/piped stream types lack reconfigure entirely

    monkeypatch.setattr(cli.sys, "stdout", _Reconfigurable())
    monkeypatch.setattr(cli.sys, "stderr", _NotReconfigurable())

    cli._ensure_utf8_stdio()  # must not raise even though stderr can't reconfigure

    assert calls == ["utf-8"]


def test_ensure_utf8_stdio_swallows_reconfigure_errors(monkeypatch):
    import mimir.cli as cli

    class _Uncooperative:
        def reconfigure(self, encoding):
            raise ValueError("stream already in use")

    monkeypatch.setattr(cli.sys, "stdout", _Uncooperative())
    monkeypatch.setattr(cli.sys, "stderr", _Uncooperative())

    cli._ensure_utf8_stdio()  # must not raise


def test_install_hook_refuses_invalid_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")
    try:
        install_hook(settings)
        assert False, "should have refused invalid JSON"
    except SystemExit:
        pass


def test_register_mcp_server_skips_when_claude_cli_missing(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which", lambda name: None)
    msg = register_mcp_server()
    assert "not found on PATH" in msg


def test_register_mcp_server_succeeds(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which",
                        lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(hook_install, "_resolve_command", lambda name: name)  # deterministic: skip real fs lookup
    calls = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(hook_install.subprocess, "run", fake_run)
    msg = register_mcp_server()
    assert "registered" in msg
    assert calls == [["/usr/bin/claude", "mcp", "add", "mimir", "--", "mimir-serve"]]


def test_register_mcp_server_resolves_command_to_absolute_path(monkeypatch, tmp_path):
    import mimir.hook_install as hook_install

    exe = tmp_path / "mimir-serve"
    exe.touch()
    monkeypatch.setattr(hook_install.shutil, "which",
                        lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(hook_install, "_resolve_command", lambda name: str(exe))
    calls = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(hook_install.subprocess, "run",
                        lambda cmd, **kwargs: calls.append(cmd) or _Result())
    register_mcp_server()
    assert calls == [["/usr/bin/claude", "mcp", "add", "mimir", "--", str(exe)]]


def test_register_mcp_server_is_idempotent_when_already_registered(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which",
                        lambda name: "/usr/bin/claude" if name == "claude" else None)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "MCP server mimir already exists"

    monkeypatch.setattr(hook_install.subprocess, "run", lambda cmd, **kwargs: _Result())
    msg = register_mcp_server()
    assert "already registered" in msg


def test_register_mcp_server_reports_other_failures(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which",
                        lambda name: "/usr/bin/claude" if name == "claude" else None)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom: permission denied"

    monkeypatch.setattr(hook_install.subprocess, "run", lambda cmd, **kwargs: _Result())
    msg = register_mcp_server()
    assert "failed" in msg
    assert "permission denied" in msg


def test_resolve_command_returns_which_result_when_on_path(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert hook_install._resolve_command("mimir-serve") == "/usr/bin/mimir-serve"


def test_resolve_command_falls_back_to_scripts_dir_when_not_on_path(monkeypatch, tmp_path):
    import mimir.hook_install as hook_install

    scripts_dir = tmp_path / "Scripts"
    scripts_dir.mkdir()
    exe = scripts_dir / ("mimir-serve.exe" if hook_install.os.name == "nt" else "mimir-serve")
    exe.touch()
    monkeypatch.setattr(hook_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(hook_install.sysconfig, "get_path", lambda kind, scheme=None: str(scripts_dir))
    assert hook_install._resolve_command("mimir-serve") == str(exe)


def test_resolve_command_falls_back_to_bare_name_when_unresolvable(monkeypatch):
    import mimir.hook_install as hook_install

    monkeypatch.setattr(hook_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(hook_install.sysconfig, "get_path",
                        lambda kind, scheme=None: "/nonexistent/scripts/dir")
    assert hook_install._resolve_command("mimir-serve") == "mimir-serve"


def test_install_hook_writes_resolved_absolute_command(monkeypatch, tmp_path):
    import mimir.hook_install as hook_install

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(hook_install, "_resolve_command", lambda name: f"/resolved/{name}")
    install_hook(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = {e["command"] for g in data["hooks"]["PostToolUse"] for e in g["hooks"]}
    assert cmds == {"/resolved/mimir-hook"}


def test_install_cline_hook_writes_resolved_absolute_command(monkeypatch, tmp_path):
    import mimir.hook_install as hook_install

    hooks_dir = tmp_path / "Hooks"
    monkeypatch.setattr(hook_install, "_resolve_command", lambda name: f"/resolved/{name}")
    install_cline_hook(hooks_dir)
    script = (hooks_dir / "PostToolUse").read_text(encoding="utf-8")
    assert "exec /resolved/mimir-hook-cline" in script


# --- end-to-end: consolidate (log -> gated lessons) -> serve-side store (#1/#2) ---

def test_consolidate_fills_store_and_serve_side_loads_it(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.mcp_server import recall
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="json.loads", context="empty stdin",
                    consequence="JSONDecodeError", outcome_score=0.0, task_id="t"), log_path=log)
    capture(Episode(action="ok", context="c", consequence="fine",
                    outcome_score=1.0), log_path=log)  # a success: not a MISTAKE, must be ignored

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    fake_judge = lambda ep: Verdict(rule="guard json decode against empty input",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    fake_probe = lambda lessons: float(len(lessons))  # mimic original no-op; live probe wiring covered separately
    assert cli.consolidate_main(judge=fake_judge, probe=fake_probe) == 0
    assert (tmp_path / "lessons.json").exists()

    # a fresh (serve-side) store built from the same paths recalls what consolidate wrote
    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    res = recall(store, "json parser crashes on empty input")
    assert res.lessons and "json" in res.lessons[0].rule


def test_store_persistence_survives_reload_bitemporally(tmp_path):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    lessons = tmp_path / "lessons.json"
    s1 = cli.build_store(lance_url=tmp_path / "a.db", lessons_path=lessons)
    old = s1.add(Lesson(rule="retry once", confidence=0.5, id="L1"))
    s1.supersede(old, Lesson(rule="retry with exponential backoff", confidence=0.8, id="L2"))
    cli.save_lessons(s1, lessons)

    s2 = cli.build_store(lance_url=tmp_path / "b.db", lessons_path=lessons)
    assert {lo.id for lo in s2.active()} == {"L2"}        # bi-temporal state survives reload
    assert s2.get("L1").status == "superseded"


def test_build_store_defaults_to_hash_embed_without_env_var(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.store_semantic import hash_embed

    monkeypatch.delenv(cli.EMBED_MODEL_ENV, raising=False)
    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    assert store._index._embed is hash_embed  # unchanged zero-dependency default


def test_build_store_uses_fastembed_when_env_var_set(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    import mimir.store_semantic as sc
    from mimir.models import Lesson

    calls = []

    def fake_fastembed(texts, *, model_name):
        calls.append(model_name)
        return sc.hash_embed(texts)  # any valid Embed impl -- proving it's routed, not its quality

    monkeypatch.setattr(sc, "fastembed_embed", fake_fastembed)
    monkeypatch.setenv(cli.EMBED_MODEL_ENV, "fake/model")

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    store.add(Lesson(rule="flush the buffer", id="L1"))

    assert calls == ["fake/model"]


def test_split_for_probe_holds_out_last_third_minimum_one():
    import mimir.cli as cli
    from mimir.models import Episode

    eps = [Episode(id=str(i)) for i in range(6)]
    held_out, extraction = cli._split_for_probe(eps)

    assert held_out == eps[-2:]      # 6 // 3 = 2
    assert extraction == eps[:-2]


def test_split_for_probe_extracts_all_when_fewer_than_two_episodes():
    import mimir.cli as cli
    from mimir.models import Episode

    assert cli._split_for_probe([]) == ([], [])
    one = [Episode(id="1")]
    assert cli._split_for_probe(one) == ([], one)


def test_consolidate_main_wires_live_probe_with_held_out_split(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import bench.claude_judge as cj
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)
    capture(Episode(action="a2", context="c2", consequence="boom2", outcome_score=0.0), log_path=log)
    capture(Episode(action="a3", context="c3", consequence="boom3", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    seen_held_out = []

    def fake_make_probe(held_out, **kw):
        seen_held_out.append(held_out)
        return lambda lessons: float(len(lessons))  # improvement proportional to lessons admitted

    monkeypatch.setattr(cj, "make_live_counterfactual_probe", fake_make_probe)

    fake_judge = lambda ep: Verdict(rule=f"rule for {ep.action}",
                                    specificity=0.9, generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0

    assert len(seen_held_out) == 1
    held_out = seen_held_out[0]
    assert len(held_out) == 1           # 3 episodes -> 1 held out (last third, min 1)
    assert held_out[0].action == "a3"   # last episode held out, not extracted from


def test_consolidate_main_admits_nothing_with_one_episode_no_probe_injected(tmp_path, monkeypatch):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.capture import capture
    from mimir.consolidate import Verdict
    from mimir.models import Episode

    log = tmp_path / "episodes.jsonl"
    capture(Episode(action="a1", context="c1", consequence="boom1", outcome_score=0.0), log_path=log)

    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    fake_judge = lambda ep: Verdict(rule="a rule", specificity=0.9,
                                    generalizability=0.8, non_sycophancy=0.9)
    assert cli.consolidate_main(judge=fake_judge) == 0  # still exits 0

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    assert store.active() == []  # fail-closed: no held-out evidence, nothing admitted


def test_render_digest_sorts_by_confidence_descending():
    import mimir.cli as cli
    from mimir.models import Lesson

    lessons = [Lesson(rule="low confidence rule", confidence=0.3, id="L1"),
              Lesson(rule="high confidence rule", confidence=0.9, id="L2")]
    digest = cli.render_digest(lessons)
    assert digest.index("high confidence rule") < digest.index("low confidence rule")
    assert "confidence: 0.90" in digest
    assert "id: L2" in digest


def test_render_digest_handles_no_active_lessons():
    import mimir.cli as cli

    assert "no active lessons" in cli.render_digest([])


def test_export_main_requires_digest_flag():
    import mimir.cli as cli

    assert cli.export_main([]) == 2


def test_export_main_prints_digest_from_store(tmp_path, monkeypatch, capsys):
    pytest.importorskip("lancedb")
    import mimir.cli as cli
    from mimir.models import Lesson

    monkeypatch.setattr(cli, "DEFAULT_LESSONS", tmp_path / "lessons.json")
    monkeypatch.setattr(cli, "DEFAULT_LANCE", tmp_path / "lance.db")

    store = cli.build_store(lance_url=tmp_path / "lance.db", lessons_path=tmp_path / "lessons.json")
    store.add(Lesson(rule="pin tool versions before release", confidence=0.8, id="L1"))
    cli.save_lessons(store, cli.DEFAULT_LESSONS)

    assert cli.export_main(["--digest"]) == 0
    out = capsys.readouterr().out
    assert "pin tool versions before release" in out


def test_export_main_handles_missing_serve_deps(monkeypatch, capsys):
    import mimir.cli as cli

    monkeypatch.setattr(cli, "build_store", lambda **kw: (_ for _ in ()).throw(ImportError("No module named 'lancedb'")))
    assert cli.export_main(["--digest"]) == 1
    err = capsys.readouterr().err
    assert "pip install 'mimir-agent-memory[mcp]'" in err


def test_main_dispatches_export_command(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "export_main", lambda rest: calls.append(rest) or 0)
    assert cli.main(["export", "--digest"]) == 0
    assert calls == [["--digest"]]


def test_main_dispatches_setup_command_runs_hook_and_mcp_registration(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "install_hook", lambda: calls.append("hook") or "hook done")
    monkeypatch.setattr(cli, "register_mcp_server", lambda: calls.append("mcp") or "mcp done")
    assert cli.main(["setup"]) == 0
    assert calls == ["hook", "mcp"]


import io


def test_hook_main_calls_auto_consolidate_maybe_trigger(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger",
                        lambda log_path: calls.append(log_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.hook_main() == 0
    assert calls == [cli._log_path()]


def test_hook_main_cline_calls_auto_consolidate_maybe_trigger(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger",
                        lambda log_path: calls.append(log_path))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.hook_main_cline() == 0
    assert calls == [cli._log_path()]


def test_extract_flag_value_returns_following_token():
    import mimir.cli as cli

    assert cli._extract_flag_value(["--config", "path.json"], "--config") == "path.json"


def test_extract_flag_value_returns_none_when_absent():
    import mimir.cli as cli

    assert cli._extract_flag_value([], "--config") is None


def test_extract_flag_value_returns_none_when_flag_is_last_token():
    import mimir.cli as cli

    assert cli._extract_flag_value(["--config"], "--config") is None


def test_hook_main_uses_config_mapper_when_config_flag_given(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    config_path = tmp_path / "foo.json"
    config_path.write_text(json.dumps({
        "action_path": "tool_name",
        "outcome_path": "result.status",
        "fail_values": ["error"],
    }), encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin",
                        io.StringIO(json.dumps({"tool_name": "foo.run",
                                                "result": {"status": "error"}})))
    rc = cli.hook_main(["--config", str(config_path)])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "foo.run"
    assert row["outcome_score"] == cli.OUTCOME_FAIL


def test_hook_main_uses_config_mapper_from_env_var(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    config_path = tmp_path / "foo.json"
    config_path.write_text(json.dumps({"action_path": "tool_name"}), encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setenv("MIMIR_HOOK_CONFIG", str(config_path))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"tool_name": "foo.run"})))
    rc = cli.hook_main([])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "foo.run"


def test_hook_main_expands_tilde_in_config_path(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    config_path = tmp_path / "foo.json"
    config_path.write_text(json.dumps({"action_path": "tool_name"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser() checks this
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"tool_name": "foo.run"})))
    rc = cli.hook_main(["--config", "~/foo.json"])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "foo.run"


def test_hook_main_skips_capture_on_malformed_config(tmp_path, monkeypatch, caplog):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("not json{", encoding="utf-8")
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps({"tool_name": "foo.run"})))
    with caplog.at_level(logging.ERROR):
        rc = cli.hook_main(["--config", str(bad_config)])
    assert rc == 0
    assert not log.exists()
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_hook_main_without_config_still_uses_claude_code_mapper(tmp_path, monkeypatch):
    import mimir.cli as cli

    log = tmp_path / "episodes.jsonl"
    monkeypatch.setenv("MIMIR_EPISODE_LOG", str(log))
    monkeypatch.setattr(cli.auto_consolidate, "maybe_trigger", lambda log_path: None)
    event = {"tool_name": "Bash", "is_error": True, "session_id": "s1"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    rc = cli.hook_main([])
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "Bash"
    assert row["outcome_score"] == cli.OUTCOME_FAIL


def test_auto_consolidate_worker_main_calls_consolidate_then_finish_run(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "consolidate_main", lambda *a, **k: calls.append("consolidate") or 0)
    monkeypatch.setattr(cli.auto_consolidate, "finish_run",
                        lambda **kw: calls.append(("finish", kw.get("advance_baseline"))))
    assert cli._auto_consolidate_worker_main() == 0
    assert calls == ["consolidate", ("finish", True)]


def test_auto_consolidate_worker_main_still_finishes_when_consolidate_raises(monkeypatch):
    import mimir.cli as cli

    calls = []

    def _boom(*a, **k):
        raise RuntimeError("judge unreachable")

    monkeypatch.setattr(cli, "consolidate_main", _boom)
    monkeypatch.setattr(cli.auto_consolidate, "finish_run",
                        lambda **kw: calls.append(("finish", kw.get("advance_baseline"))))
    try:
        cli._auto_consolidate_worker_main()
        assert False, "the worker's own crash should propagate to its process exit code"
    except RuntimeError:
        pass
    assert calls == [("finish", False)]  # cleanup still ran, baseline NOT advanced


def test_auto_consolidate_worker_main_does_not_advance_baseline_on_nonzero_return(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "consolidate_main", lambda *a, **k: 1)  # e.g. missing bench judge
    monkeypatch.setattr(cli.auto_consolidate, "finish_run",
                        lambda **kw: calls.append(("finish", kw.get("advance_baseline"))))
    assert cli._auto_consolidate_worker_main() == 1
    assert calls == [("finish", False)]


def test_main_dispatches_auto_consolidate_worker_command(monkeypatch):
    import mimir.cli as cli

    calls = []
    monkeypatch.setattr(cli, "_auto_consolidate_worker_main", lambda: calls.append(True) or 0)
    assert cli.main(["_auto-consolidate-worker"]) == 0
    assert calls == [True]
