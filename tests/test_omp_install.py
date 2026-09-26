"""Focused tests for the portable OMP installer (scripts/install_omp.py).

All cases run against temporary agent dirs / HOME; the real ``~/.omp``
config is never touched.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_omp.py"

CLONE = REPO_ROOT
STORE_SUB = Path(".omp") / "ontologies" / "shared"


def run_install(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=merged,
    )


def fresh_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Path.home() consults HOME on POSIX; guard other lookups too.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("EVO_ONTOLOGY_STORE", raising=False)
    return home / ".omp" / "agent", home / STORE_SUB


def read_mcp(agent: Path) -> dict:
    return json.loads((agent / "mcp.json").read_text(encoding="utf-8"))


def test_install_registers_and_launches_server(tmp_path, monkeypatch):
    """Default temp-HOME install, then an actual stdio launch via real uv."""
    agent, store = fresh_dirs(tmp_path, monkeypatch)
    assert shutil.which("uv") is not None
    completed = run_install("--agent-dir", str(agent))
    assert completed.returncode == 0, completed.stderr
    entry = read_mcp(agent)["mcpServers"]["evo-semantic"]
    assert entry["type"] == "stdio"
    assert entry["enabled"] is True
    # Install-resolved default store is always persisted in the entry.
    assert entry["args"][-2:] == ["--store", str(store)]

    project = tmp_path / "proj"
    project.mkdir()
    launched = subprocess.run(
        [entry["command"], *entry["args"]],
        input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
        capture_output=True,
        text=True,
        cwd=str(project),
        timeout=180,
    )
    assert launched.returncode == 0, launched.stderr
    response = json.loads(launched.stdout.splitlines()[0])
    assert response["id"] == 1
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["serverInfo"]["name"] == "evo-semantic-mcp"

    wrapper = agent / "extensions" / "evo-capture.ts"
    text = wrapper.read_text(encoding="utf-8")
    assert "baseExtension(pi, INSTALL_DEFAULTS)" in text
    assert str(store) in text
    for skill in ("build-ontology", "evolve-ontology", "explore-ontology"):
        dest = agent / "skills" / skill
        assert dest.is_symlink(), skill
        assert Path(os.path.realpath(dest)) == (
            CLONE / "plugins" / "evoontology-codex" / "skills" / skill
        )


def test_install_preserves_unrelated_servers_and_fields(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    agent.mkdir(parents=True)
    before = {
        "$schema": "https://example/schema.json",
        "mcpServers": {
            "slack": {"type": "http", "url": "https://example", "enabled": False},
        },
        "customTopLevel": {"keep": True},
    }
    (agent / "mcp.json").write_text(json.dumps(before), encoding="utf-8")
    assert run_install("--agent-dir", str(agent)).returncode == 0
    data = read_mcp(agent)
    assert data["$schema"] == before["$schema"]
    assert data["customTopLevel"] == {"keep": True}
    assert data["mcpServers"]["slack"] == before["mcpServers"]["slack"]
    assert "evo-semantic" in data["mcpServers"]


def test_install_idempotent(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    args = ["--agent-dir", str(agent)]
    assert run_install(*args).returncode == 0
    mcp_snapshot = (agent / "mcp.json").read_text(encoding="utf-8")
    wrapper_snapshot = (agent / "extensions" / "evo-capture.ts").read_text(encoding="utf-8")
    second = run_install(*args)
    assert second.returncode == 0, second.stderr
    assert (agent / "mcp.json").read_text(encoding="utf-8") == mcp_snapshot
    assert (agent / "extensions" / "evo-capture.ts").read_text(encoding="utf-8") == wrapper_snapshot


def test_install_migrates_legacy_own_entry(tmp_path, monkeypatch):
    agent, store = fresh_dirs(tmp_path, monkeypatch)
    agent.mkdir(parents=True)
    legacy = {
        "mcpServers": {
            "evo-semantic": {
                "type": "stdio",
                "command": shutil.which("uv"),
                "args": [
                    "run", "--offline", "--no-sync", "--project", str(CLONE),
                    "python", "-m", "evoontology.runtime.mcp_server",
                    "--store", str(store), "--project-aware",
                ],
                "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                "enabled": True,
                "timeout": 120000,
            }
        }
    }
    (agent / "mcp.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert run_install("--agent-dir", str(agent)).returncode == 0
    entry = read_mcp(agent)["mcpServers"]["evo-semantic"]
    assert "evoontology.runtime.omp_server" in entry["args"]
    assert "evoontology.runtime.mcp_server" not in entry["args"]
    assert "--project-aware" not in entry["args"]


def test_install_conflicts_on_foreign_server_without_mutation(tmp_path, monkeypatch):
    agent, store = fresh_dirs(tmp_path, monkeypatch)
    agent.mkdir(parents=True)
    foreign = {"mcpServers": {"evo-semantic": {"type": "stdio", "command": "other"}}}
    mcp_path = agent / "mcp.json"
    mcp_path.write_text(json.dumps(foreign), encoding="utf-8")
    completed = run_install("--agent-dir", str(agent))
    assert completed.returncode != 0
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == foreign
    assert not os.path.lexists(agent / "extensions" / "evo-capture.ts")
    assert not store.exists()


def test_install_conflicts_on_foreign_skill_without_mutation(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    clash = agent / "skills" / "build-ontology"
    clash.parent.mkdir(parents=True)
    clash.write_text("user file", encoding="utf-8")
    completed = run_install("--agent-dir", str(agent))
    assert completed.returncode != 0
    assert clash.read_text(encoding="utf-8") == "user file"
    assert not (agent / "mcp.json").exists()


def test_install_conflicts_on_foreign_extension_without_mutation(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    clash = agent / "extensions" / "evo-capture.ts"
    clash.parent.mkdir(parents=True)
    clash.write_text("user extension", encoding="utf-8")
    completed = run_install("--agent-dir", str(agent))
    assert completed.returncode != 0
    assert clash.read_text(encoding="utf-8") == "user extension"
    assert not (agent / "mcp.json").exists()


def test_install_always_persists_resolved_store(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    custom = tmp_path / "custom store" / "shared"
    monkeypatch.setenv("EVO_ONTOLOGY_STORE", str(custom))
    assert run_install("--agent-dir", str(agent)).returncode == 0
    entry = read_mcp(agent)["mcpServers"]["evo-semantic"]
    # Env-chosen at install time is persisted too, so a later launch without
    # env resolves identically; runtime env still overrides it (see below).
    assert entry["args"][-2:] == ["--store", str(custom.resolve())]
    assert str(custom.resolve()) in (agent / "extensions" / "evo-capture.ts").read_text(encoding="utf-8")

    agent2 = tmp_path / "agent2"
    explicit = tmp_path / "explicit store"
    assert run_install("--agent-dir", str(agent2), "--store", str(explicit)).returncode == 0
    entry2 = read_mcp(agent2)["mcpServers"]["evo-semantic"]
    assert entry2["args"][-2:] == ["--store", str(explicit.resolve())]


def _launch(entry, proj, requests, extra_env=None):
    env = dict(os.environ)
    env.pop("EVO_ONTOLOGY_STORE", None)
    if extra_env:
        env.update(extra_env)
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    completed = subprocess.run(
        [entry["command"], *entry["args"]],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(proj),
        timeout=180,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return [json.loads(line) for line in completed.stdout.splitlines()]


def _browse(i, proj, workspace=None):
    args: dict = {"query": "precedence probe", "project_root": str(proj)}
    if workspace is not None:
        args["workspace"] = workspace
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": "browse_semantics", "arguments": args},
    }


def _browse_workspace(response):
    body = response["result"]
    assert body["isError"] is False, body
    return json.loads(body["content"][0]["text"])["workspace"]


def test_runtime_env_beats_baked_store_through_route(tmp_path, monkeypatch):
    """Precedence through the real project-aware route: env > baked > HOME."""
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    custom = (tmp_path / "custom store").resolve()
    override = (tmp_path / "override store").resolve()
    proj = tmp_path / "proj"
    proj.mkdir()
    assert run_install("--agent-dir", str(agent), "--store", str(custom)).returncode == 0
    entry = read_mcp(agent)["mcpServers"]["evo-semantic"]

    # 1. No env: the baked install default routes the lane.
    (resp,) = _launch(entry, proj, [_browse(1, proj)])
    assert _browse_workspace(resp).startswith(str(custom) + os.sep)

    # 2. Runtime env beats the baked --store through the same route.
    (resp,) = _launch(
        entry, proj, [_browse(2, proj)],
        extra_env={"EVO_ONTOLOGY_STORE": str(override)},
    )
    lane = _browse_workspace(resp)
    assert lane.startswith(str(override) + os.sep)
    assert not lane.startswith(str(custom) + os.sep)

    # 3. An explicit per-call workspace still wins over runtime env.
    (resp,) = _launch(
        entry, proj, [_browse(3, proj, workspace=str(custom))],
        extra_env={"EVO_ONTOLOGY_STORE": str(override)},
    )
    assert _browse_workspace(resp).startswith(str(custom) + os.sep)


def test_install_rejects_non_executable_uv(tmp_path, monkeypatch):
    agent, _store = fresh_dirs(tmp_path, monkeypatch)
    completed = run_install("--agent-dir", str(agent), "--uv", str(tmp_path / "no-such-uv"))
    assert completed.returncode != 0
    assert not (agent / "mcp.json").exists()
    assert not os.path.lexists(agent / "extensions" / "evo-capture.ts")
