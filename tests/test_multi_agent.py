"""Multi-agent tests: codex/gemini adapters, fallback on usage limit,
per-agent session threading, window recording, and the --agents/--windows CLIs."""
from __future__ import annotations

import json

import claude_runner

from conftest import read_state, run_runner, write_playbook


def _patch_env(stub, monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER_HOME", str(stub.home))
    for s in (stub.claude, stub.codex, stub.gemini):
        for k, v in s.env_vars().items():
            monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------- #
# adapters in isolation
# --------------------------------------------------------------------------- #
def test_codex_adapter_success(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.codex.set_plan([{"kind": "success", "result": "codex did it"}])
    res = claude_runner.ADAPTERS["codex"].run("do work", {}, None, str(tmp_path))
    assert res["ok"] is True
    assert res["text"] == "codex did it"
    assert res["session_id"] == "codex-thread-0001"
    call = stub.codex.calls()[0]
    assert "exec" in call["argv"] and "--json" in call["argv"]
    assert call["prompt"] == "do work"


def test_codex_adapter_usage_limit(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.codex.set_plan([{"kind": "usage"}])
    res = claude_runner.ADAPTERS["codex"].run("do work", {}, None, str(tmp_path))
    assert res["ok"] is False and res["limit"] == claude_runner.Limit.USAGE


def test_codex_adapter_resumes_thread(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.codex.set_plan([{"kind": "success"}])
    claude_runner.ADAPTERS["codex"].run("more work", {}, "codex-thread-0001", str(tmp_path))
    argv = stub.codex.calls()[0]["argv"]
    i = argv.index("resume")
    assert argv[i + 1] == "codex-thread-0001"


def test_gemini_adapter_success_and_usage(stub, tmp_path, monkeypatch):
    _patch_env(stub, monkeypatch)
    stub.gemini.set_plan([{"kind": "success", "result": "gemini did it"}, {"kind": "usage"}])
    res = claude_runner.ADAPTERS["gemini"].run("do work", {}, None, str(tmp_path))
    assert res["ok"] is True and res["text"] == "gemini did it"
    assert res["session_id"] is None            # gemini runs stateless
    res2 = claude_runner.ADAPTERS["gemini"].run("again", {}, None, str(tmp_path))
    assert res2["ok"] is False and res2["limit"] == claude_runner.Limit.USAGE


def test_gemini_plain_text_fallback_for_old_cli(stub, tmp_path, monkeypatch):
    """gemini 0.1.x has no --output-format; the adapter must retry plain-text."""
    _patch_env(stub, monkeypatch)
    stub.gemini.set_plan([{"kind": "old_cli_success", "result": "plain ok"}])
    adapter = claude_runner.GeminiAdapter()      # fresh instance: isolated _json_ok
    res = adapter.run("hi", {}, None, str(tmp_path))
    assert res["ok"] is True and res["text"] == "plain ok"
    assert adapter._json_ok is False
    argvs = [c["argv"] for c in stub.gemini.calls()]
    assert "--output-format" in argvs[0]
    assert "--output-format" not in argvs[1]


# --------------------------------------------------------------------------- #
# fallback chains in full playbook runs
# --------------------------------------------------------------------------- #
def test_fallback_switches_agent_on_usage_limit(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "big job", "fallback_agents": ["codex"]}])
    stub.claude.set_plan([{"kind": "usage"}])
    stub.codex.set_plan([{"kind": "success", "result": "codex finished it"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "switching to next agent" in r.stdout
    assert "DONE prompt #1 [codex]" in r.stdout
    state = read_state(pb)
    assert state["sessions"]["codex"] == "codex-thread-0001"
    # the claude limit event was recorded for --windows
    events = json.loads((stub.home / "windows.json").read_text())
    assert any(e["agent"] == "claude" for e in events)


def test_all_agents_limited_waits_then_retries_chain(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "fallback_agents": ["codex"]}])
    stub.claude.set_plan([{"kind": "usage"}, {"kind": "usage"}])
    stub.codex.set_plan([{"kind": "usage"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "USAGE LIMIT (all agents: claude, codex)" in r.stdout
    assert len(stub.claude.calls()) == 2
    assert len(stub.codex.calls()) == 2


def test_per_agent_sessions_thread_independently(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "step one", "agent": "codex"},
                         {"prompt": "step two", "agent": "codex"}])
    stub.codex.set_plan([{"kind": "success"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    calls = stub.codex.calls()
    assert "resume" not in calls[0]["argv"]
    i = calls[1]["argv"].index("resume")
    assert calls[1]["argv"][i + 1] == "codex-thread-0001"


def test_unknown_agent_fails_fast(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job", "agent": "gpt99"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode != 0
    assert "unknown agent 'gpt99'" in r.stdout + r.stderr
    assert stub.claude.calls() == []


def test_legacy_state_migrates_to_sessions(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "continue the work"}])
    pb.with_suffix(".state.json").write_text(json.dumps(
        {"next_index": 0, "session_id": "old-legacy-sid", "total_cost_usd": 0.5}))
    stub.claude.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    argv = stub.claude.calls()[0]["argv"]
    i = argv.index("--resume")
    assert argv[i + 1] == "old-legacy-sid"


def test_models_map_routes_per_agent(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml",
                        [{"prompt": "job", "agent": "codex",
                          "models": {"codex": "gpt-5-codex", "claude": "haiku"}}])
    stub.codex.set_plan([{"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    argv = stub.codex.calls()[0]["argv"]
    i = argv.index("--model")
    assert argv[i + 1] == "gpt-5-codex"


# --------------------------------------------------------------------------- #
# CLI: --agents and --windows
# --------------------------------------------------------------------------- #
def test_agents_flag_lists_adapters(stub, tmp_path):
    r = run_runner(["--agents"], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr
    for name in ("claude", "codex", "gemini"):
        assert name in r.stdout
    assert "installed" in r.stdout          # stub binaries resolve


def test_windows_flag_reports_recorded_events(stub, tmp_path):
    pb = write_playbook(tmp_path / "pb.yaml", [{"prompt": "job"}])
    stub.claude.set_plan([{"kind": "usage"}, {"kind": "success"}])
    r = run_runner([str(pb)], tmp_path, stub.env())
    assert r.returncode == 0, r.stdout + r.stderr

    r2 = run_runner(["--windows"], tmp_path, stub.env())
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "claude" in r2.stdout and "AGENT" in r2.stdout


def test_windows_flag_with_no_events(stub, tmp_path):
    r = run_runner(["--windows"], tmp_path, stub.env())
    assert r.returncode == 0
    assert "No usage-limit events" in r.stdout
