"""CLI contract tests for OMP turn capture.

The extension spawns ``python -m evoontology.trajectory.omp_capture --store
ABS`` with one JSON packet on stdin and reads ``{task_id, status}`` on stdout.
These tests exercise that contract end to end: project separation, retry
idempotence, conflict rejection, malformed input, observed-error preservation,
and the no-publication guarantee.
"""

import json
import subprocess
import sys
from pathlib import Path

import evoontology
import pytest

from evoontology.trajectory import omp_capture
from evoontology.trajectory.omp_capture import make_task_id
from evoontology.workspace import resolve_project_workspace

REPO_ROOT = Path(evoontology.__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "evoontology.trajectory.omp_capture"]


def _packet(project_root, **overrides):
    packet = {
        "project_root": str(project_root),
        "session_id": "sess-1",
        "turn_id": "turn-1",
        "question": "What broke the nightly build?",
        "final_answer": "A flaky migration test.",
        "status": "completed",
        "calls": [
            {"tool": "read_logs", "arguments": {"lines": 50},
             "result": "migration_42 failed", "is_error": False},
        ],
    }
    packet.update(overrides)
    return packet


def _run(store, payload, *, raw=None):
    body = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [*CLI, "--store", str(store)],
        input=body,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def _recorded(store, task_id):
    matches = [path for path in Path(store).rglob(f"{task_id}.json")
               if path.parent.name == "trajectories"]
    assert len(matches) == 1, f"expected one trajectory for {task_id}: {matches}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _trajectory_files(store):
    return sorted(path for path in Path(store).rglob("*.json")
                  if path.parent.name == "trajectories")


def _flat_trajectory_files(store):
    return sorted((Path(store) / "trajectories").glob("*.json"))


_VERSION_FILES = ("terms.json", "mappings.json", "relations.json",
                  "constraints.json", "evidence.json")


def _seed_version(store, version, project_root, filenames=_VERSION_FILES):
    lane = resolve_project_workspace(store, str(Path(project_root).resolve()))
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "active.json").write_text(json.dumps({"active_version": version}), encoding="utf-8")
    # project_root is the project evidence; data_source names the SQLite
    # database and must not affect project attribution.
    (lane / "project.json").write_text(json.dumps(
        {"project_root": str(Path(project_root).resolve()),
         "data_source": {"path": "/data/example.sqlite"}}), encoding="utf-8")
    version_dir = lane / "versions" / version
    version_dir.mkdir(parents=True, exist_ok=True)
    for filename in filenames:
        (version_dir / filename).write_text("[]", encoding="utf-8")
    return lane


def test_two_roots_same_session_turn_stay_separate(tmp_path):
    store = tmp_path / "shared"
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()

    first = _run(store, _packet(root_a))
    second = _run(store, _packet(root_b))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    task_a = json.loads(first.stdout)["task_id"]
    task_b = json.loads(second.stdout)["task_id"]
    assert json.loads(first.stdout)["status"] == "recorded"
    assert json.loads(second.stdout)["status"] == "recorded"
    assert task_a != task_b

    record_a = _recorded(store, task_a)
    record_b = _recorded(store, task_b)
    assert record_a["project_root"] != record_b["project_root"]
    assert record_a["data_source"] == {"type": "omp_project", "root": record_a["project_root"]}
    assert record_b["data_source"]["root"] == record_b["project_root"]
    assert record_a["session_id"] == record_b["session_id"] == "sess-1"
    assert record_a["turn_id"] == record_b["turn_id"] == "turn-1"


def test_two_roots_write_distinct_lanes_without_flat_writes(tmp_path):
    store = tmp_path / "shared"
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()

    first = _run(store, _packet(root_a))
    second = _run(store, _packet(root_b))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    lane_a = resolve_project_workspace(store, str(root_a.resolve()))
    lane_b = resolve_project_workspace(store, str(root_b.resolve()))
    assert lane_a != lane_b
    task_a = json.loads(first.stdout)["task_id"]
    task_b = json.loads(second.stdout)["task_id"]
    assert (lane_a / "trajectories" / f"{task_a}.json").is_file()
    assert (lane_b / "trajectories" / f"{task_b}.json").is_file()
    assert _flat_trajectory_files(store) == []


def test_retry_is_idempotent_and_preserves_recorded_at(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root)

    first = _run(store, packet)
    second = _run(store, packet)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout) == {"task_id": json.loads(first.stdout)["task_id"],
                                        "status": "recorded"}
    assert json.loads(second.stdout)["status"] == "already_recorded"
    assert json.loads(second.stdout)["task_id"] == json.loads(first.stdout)["task_id"]
    assert len(_trajectory_files(store)) == 1

    before = _recorded(store, json.loads(first.stdout)["task_id"])["recorded_at"]
    third = _run(store, packet)
    assert third.returncode == 0
    after = _recorded(store, json.loads(first.stdout)["task_id"])["recorded_at"]
    assert before == after


def test_changed_content_rejected_without_overwrite(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    first = _run(store, _packet(root))
    assert first.returncode == 0, first.stderr
    task_id = json.loads(first.stdout)["task_id"]

    conflict = _run(store, _packet(root, question="A different prompt"))
    assert conflict.returncode == 3
    assert "different content" in json.loads(conflict.stderr)["error"]

    stored = _recorded(store, task_id)
    assert stored["question"] == "What broke the nightly build?"


def test_unreadable_slot_rejected_without_overwrite(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root)
    lane = resolve_project_workspace(store, str(root.resolve()))
    slot = lane / "trajectories" / (make_task_id(
        str(root.resolve()), packet["session_id"], packet["turn_id"]
    ) + ".json")
    slot.parent.mkdir(parents=True)
    slot.write_text("{corrupt", encoding="utf-8")

    result = _run(store, packet)

    assert result.returncode == 3
    assert "refusing to overwrite" in json.loads(result.stderr)["error"]
    assert slot.read_text(encoding="utf-8") == "{corrupt"


@pytest.mark.parametrize("mutate", [
    lambda p: {k: v for k, v in p.items() if k != "project_root"},
    lambda p: {k: v for k, v in p.items() if k != "session_id"},
    lambda p: {k: v for k, v in p.items() if k != "turn_id"},
    lambda p: {k: v for k, v in p.items() if k != "question"},
    lambda p: {**p, "project_root": "relative/path"},
    lambda p: {**p, "question": "   "},
    lambda p: {**p, "status": "done-ish"},
    lambda p: {**p, "calls": "not-a-list"},
    lambda p: {**p, "calls": [{"arguments": {}}]},
    lambda p: {**p, "calls": [{"tool": "x", "is_error": "yes"}]},
    lambda p: {**p, "calls": [{"tool": "x", "arguments": ["nope"]}]},
])
def test_malformed_packets_rejected_without_writes(tmp_path, mutate):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    result = _run(store, mutate(_packet(root)))

    assert result.returncode == 2
    assert "error" in json.loads(result.stderr)
    assert _trajectory_files(store) == []


def test_relative_store_rejected(tmp_path):
    result = _run("relative/store", _packet(tmp_path / "proj"))
    assert result.returncode == 2
    assert "error" in json.loads(result.stderr)


def test_non_json_stdin_rejected(tmp_path):
    store = tmp_path / "shared"
    assert _run(store, None, raw="{not json").returncode == 2
    assert _run(store, None, raw="   ").returncode == 2
    assert _trajectory_files(store) == []


def test_observed_tool_error_preserved(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root, status="failed", calls=[
        {"tool": "run_query", "arguments": {"sql": "SELECT 1"},
         "result": {"rows": [[1]]}, "is_error": False},
        {"tool": "run_query", "arguments": {"sql": "SELECT nope"},
         "result": "column nope does not exist", "is_error": True},
    ])

    result = _run(store, packet)
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    assert record["task_status"] == "failed"
    assert [call["tool"] for call in record["native_tool_calls"]] == ["run_query", "run_query"]
    assert record["native_tool_calls"][1]["result_summary"] == "column nope does not exist"
    assert record["errors"] == [{"tool": "run_query", "error": "column nope does not exist"}]


def test_capture_never_publishes_or_configures(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]

    lane = resolve_project_workspace(store, str(root.resolve()))
    assert (lane / "trajectories" / f"{task_id}.json").is_file()
    assert _flat_trajectory_files(store) == []
    assert not (store / "active.json").exists()
    assert not (store / "project.json").exists()
    assert not (store / "state.json").exists()
    assert not (store / "versions").exists()
    assert not (store / "tasks").exists()
    assert not (store / "workload").exists()


def test_sensitive_argument_keys_redacted(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root, calls=[
        {"tool": "fetch", "arguments": {
            "url": "https://example.test",
            "apiKey": "sk-live-123",
            "author": "ada",
            "nested": {"password": "hunter2", "retries": 3},
        }, "result": "ok", "is_error": False},
    ])

    result = _run(store, packet)
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    stored = record["native_tool_calls"][0]["input"]
    assert stored["url"] == "https://example.test"
    assert stored["apiKey"] == "[REDACTED]"
    assert stored["author"] == "ada"
    assert stored["nested"] == {"password": "[REDACTED]", "retries": 3}
    assert "sk-live-123" not in json.dumps(record)
    assert "hunter2" not in json.dumps(record)


def test_mcp_tool_prefixes_normalize_to_semantic_calls(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root, calls=[
        {"tool": "mcp__evo-semantic__browse_semantics",
         "arguments": {"query": "revenue"}, "result": {"terms": []}, "is_error": False},
        {"tool": "evo-semantic::resolve_semantics",
         "arguments": {"ids": ["t1"]}, "result": {"mappings": []}, "is_error": False},
        {"tool": "execute_sql", "arguments": {"sql": "SELECT 1"},
         "result": {"rows": []}, "is_error": False},
    ])

    result = _run(store, packet)
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    assert [call["tool"] for call in record["semantic_calls"]] == [
        "browse_semantics", "resolve_semantics"]
    assert [call["tool"] for call in record["native_tool_calls"]] == ["execute_sql"]


def test_ontology_version_uninitialized_without_proven_active(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    empty = _run(store, _packet(root))
    assert empty.returncode == 0, empty.stderr
    assert _recorded(store, json.loads(empty.stdout)["task_id"])["ontology_version"] == "uninitialized"

    # A flat active.json alone never proves a lane version: no flat
    # project.json names this root, so capture still lands in the lane.
    store.mkdir(exist_ok=True)
    (store / "active.json").write_text(json.dumps({"active_version": "ghost"}), encoding="utf-8")
    ghost = _run(store, _packet(root, turn_id="turn-2"))
    assert ghost.returncode == 0, ghost.stderr
    assert _recorded(store, json.loads(ghost.stdout)["task_id"])["ontology_version"] == "uninitialized"
    assert _flat_trajectory_files(store) == []

    _seed_version(store, "ghost", root, filenames=("terms.json",))
    partial = _run(store, _packet(root, turn_id="turn-3"))
    assert partial.returncode == 0, partial.stderr
    assert _recorded(store, json.loads(partial.stdout)["task_id"])["ontology_version"] == "uninitialized"


def test_proven_active_version_recorded(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    _seed_version(store, "v1", root)

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    assert _recorded(store, json.loads(result.stdout)["task_id"])["ontology_version"] == "v1"


def test_active_version_ignores_other_project_source(tmp_path):
    store = tmp_path / "shared"
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()
    _seed_version(store, "v1", root_a)

    other = _run(store, _packet(root_b))
    assert other.returncode == 0, other.stderr
    assert _recorded(store, json.loads(other.stdout)["task_id"])["ontology_version"] == "uninitialized"

    own = _run(store, _packet(root_a))
    assert own.returncode == 0, own.stderr
    assert _recorded(store, json.loads(own.stdout)["task_id"])["ontology_version"] == "v1"


def test_matching_legacy_flat_root_reused(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    store.mkdir()
    (store / "project.json").write_text(
        json.dumps({"project_root": str(root.resolve())}), encoding="utf-8")
    (store / "active.json").write_text(json.dumps({"active_version": "v1"}), encoding="utf-8")
    version_dir = store / "versions" / "v1"
    version_dir.mkdir(parents=True)
    for filename in _VERSION_FILES:
        (version_dir / filename).write_text("[]", encoding="utf-8")

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]
    assert (store / "trajectories" / f"{task_id}.json").is_file()
    assert _recorded(store, task_id)["ontology_version"] == "v1"


def test_mismatched_flat_root_not_reused(tmp_path):
    store = tmp_path / "shared"
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()
    store.mkdir()
    (store / "project.json").write_text(
        json.dumps({"project_root": str(root_a.resolve())}), encoding="utf-8")
    (store / "active.json").write_text(json.dumps({"active_version": "v1"}), encoding="utf-8")
    version_dir = store / "versions" / "v1"
    version_dir.mkdir(parents=True)
    for filename in _VERSION_FILES:
        (version_dir / filename).write_text("[]", encoding="utf-8")

    result = _run(store, _packet(root_b))
    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]
    lane_b = resolve_project_workspace(store, str(root_b.resolve()))
    assert lane_b != Path(store).resolve()
    assert (lane_b / "trajectories" / f"{task_id}.json").is_file()
    assert _flat_trajectory_files(store) == []
    assert _recorded(store, task_id)["ontology_version"] == "uninitialized"


def test_legacy_source_only_match_keeps_active_version(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    store.mkdir()
    # No project_root key: the flat data_source path alone names this root.
    (store / "project.json").write_text(
        json.dumps({"data_source": {"path": str(root.resolve())}}), encoding="utf-8")
    (store / "active.json").write_text(json.dumps({"active_version": "v1"}), encoding="utf-8")
    version_dir = store / "versions" / "v1"
    version_dir.mkdir(parents=True)
    for filename in _VERSION_FILES:
        (version_dir / filename).write_text("[]", encoding="utf-8")

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]
    assert (store / "trajectories" / f"{task_id}.json").is_file()
    assert _recorded(store, task_id)["ontology_version"] == "v1"


def test_unrelated_flat_source_routes_to_lane_uninitialized(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    store.mkdir()
    (store / "project.json").write_text(
        json.dumps({"data_source": {"path": "/data/unrelated.sqlite"}}), encoding="utf-8")
    (store / "active.json").write_text(json.dumps({"active_version": "v1"}), encoding="utf-8")
    version_dir = store / "versions" / "v1"
    version_dir.mkdir(parents=True)
    for filename in _VERSION_FILES:
        (version_dir / filename).write_text("[]", encoding="utf-8")

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    task_id = json.loads(result.stdout)["task_id"]
    lane = resolve_project_workspace(store, str(root.resolve()))
    assert lane != Path(store).resolve()
    assert (lane / "trajectories" / f"{task_id}.json").is_file()
    assert _flat_trajectory_files(store) == []
    assert _recorded(store, task_id)["ontology_version"] == "uninitialized"


def test_replay_after_version_change_stays_idempotent(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root)

    first = _run(store, packet)
    assert first.returncode == 0, first.stderr
    task_id = json.loads(first.stdout)["task_id"]
    assert _recorded(store, task_id)["ontology_version"] == "uninitialized"
    recorded_at = _recorded(store, task_id)["recorded_at"]

    _seed_version(store, "v1", root)
    replay = _run(store, packet)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout) == {"task_id": task_id, "status": "already_recorded"}

    stored = _recorded(store, task_id)
    assert stored["ontology_version"] == "uninitialized"
    assert stored["recorded_at"] == recorded_at


def test_split_stays_neutral(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    assert _recorded(store, json.loads(result.stdout)["task_id"])["split"] == ""


def test_sensitive_keys_redacted_in_results_and_errors(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root, status="failed", calls=[
        {"tool": "login", "arguments": {"user": "ada"},
         "result": {"token": "tok-abc", "user": "ada"}, "is_error": False},
        {"tool": "login", "arguments": {"user": "ada"},
         "result": {"error": "denied", "apiKey": "sk-xyz"}, "is_error": True},
    ])

    result = _run(store, packet)
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    native = record["native_tool_calls"]
    assert native[0]["result"] == {"token": "[REDACTED]", "user": "ada"}
    assert native[1]["result"] == {"error": "denied", "apiKey": "[REDACTED]"}
    assert record["errors"] == [
        {"tool": "login", "error": str({"error": "denied", "apiKey": "[REDACTED]"})}]
    blob = json.dumps(record)
    assert "tok-abc" not in blob
    assert "sk-xyz" not in blob


def test_oversized_fields_bounded_with_marker(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()
    packet = _packet(root, question="q" * 9000, final_answer="a" * 9000, calls=[
        {"tool": "dump", "arguments": {}, "result": "r\n" * 5000, "is_error": False},
    ])

    result = _run(store, packet)
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    assert record["question"].endswith("…[truncated 5000 chars]")
    assert record["final_answer"].endswith("…[truncated 5000 chars]")
    native = record["native_tool_calls"][0]
    assert native["result_truncated"] is True
    assert "result_summary" in native


def test_task_id_deterministic_collision_resistant_and_safe(tmp_path):
    root_a = str((tmp_path / "proj").resolve())
    root_b = str((tmp_path / "other").resolve())

    first = make_task_id(root_a, "sess", "turn")
    assert first == make_task_id(root_a, "sess", "turn")
    assert first != make_task_id(root_b, "sess", "turn")
    assert first != make_task_id(root_a, "sess", "other-turn")
    assert "/" not in first and "\x00" not in first
    assert len(first) <= 128


def test_provenance_marks_observed_omp_origin(tmp_path):
    store = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    result = _run(store, _packet(root))
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])

    assert record["origin"] == "omp_auto"
    assert record["provenance"]["source"] == "omp_agent_end"
    assert record["provenance"]["capture"] == "evoontology.trajectory.omp_capture"
    assert omp_capture.CAPTURE_SCHEMA == record["provenance"]["schema"]


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


def test_nested_git_subdirs_share_task_id_and_project_root(tmp_path):
    store = tmp_path / "shared"
    repo = _git_repo(tmp_path / "repo")
    sub_a = repo / "pkg" / "sub_a"
    sub_b = repo / "other"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    git_root = str(repo.resolve())

    first = _run(store, _packet(sub_a))
    assert first.returncode == 0, first.stderr
    task_a = json.loads(first.stdout)["task_id"]
    record = _recorded(store, task_a)
    assert record["project_root"] == git_root
    assert record["data_source"] == {"type": "omp_project", "root": git_root}

    second = _run(store, _packet(sub_b))
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["task_id"] == task_a
    assert payload["status"] == "already_recorded"
    assert len(_trajectory_files(store)) == 1
    assert _flat_trajectory_files(store) == []


def test_capture_from_subdir_proves_git_root_version(tmp_path):
    store = tmp_path / "shared"
    repo = _git_repo(tmp_path / "repo")
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    _seed_version(store, "v1", repo.resolve())

    result = _run(store, _packet(sub))
    assert result.returncode == 0, result.stderr
    record = _recorded(store, json.loads(result.stdout)["task_id"])
    assert record["ontology_version"] == "v1"
    assert record["project_root"] == str(repo.resolve())


def test_capture_subdir_declared_lane_proves_sibling_subdir(tmp_path):
    store = tmp_path / "shared"
    repo = _git_repo(tmp_path / "repo")
    sub_a = repo / "a"
    sub_b = repo / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    _seed_version(store, "v1", sub_a.resolve())

    result = _run(store, _packet(sub_b))
    assert result.returncode == 0, result.stderr
    assert _recorded(store, json.loads(result.stdout)["task_id"])["ontology_version"] == "v1"


def test_distinct_git_repos_stay_separate(tmp_path):
    store = tmp_path / "shared"
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    sub_a = repo_a / "sub"
    sub_b = repo_b / "sub"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)

    first = _run(store, _packet(sub_a))
    second = _run(store, _packet(sub_b))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    task_a = json.loads(first.stdout)["task_id"]
    task_b = json.loads(second.stdout)["task_id"]
    assert task_a != task_b
    assert _recorded(store, task_a)["project_root"] == str(repo_a.resolve())
    assert _recorded(store, task_b)["project_root"] == str(repo_b.resolve())
    assert len(_trajectory_files(store)) == 2


def test_non_git_nested_dirs_retain_own_identity(tmp_path):
    store = tmp_path / "shared"
    parent = tmp_path / "plain"
    child = parent / "nested"
    child.mkdir(parents=True)

    first = _run(store, _packet(parent))
    second = _run(store, _packet(child))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    task_parent = json.loads(first.stdout)["task_id"]
    task_child = json.loads(second.stdout)["task_id"]
    assert task_parent != task_child
    assert _recorded(store, task_parent)["project_root"] == str(parent.resolve())
    assert _recorded(store, task_child)["project_root"] == str(child.resolve())
