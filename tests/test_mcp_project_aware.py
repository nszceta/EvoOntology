"""Project-aware lane routing for the shared MCP workspace."""

import hashlib
import json

import pytest

from evoontology.runtime import ops
from evoontology.runtime.mcp_server import SemanticMCPServer
from evoontology.runtime.tools import OPERATIONS, TOOLS
from evoontology.workspace import resolve_project_workspace

SAMPLE_A = {
    "terms": [{"id": "t1", "name": "revenue", "type": "metric", "definition": "Revenue"}],
    "mappings": [{"id": "m1", "term_id": "t1", "table": "financials", "column": "revenue"}],
    "relations": [],
    "constraints": [],
    "evidence": [{"id": "e1", "source": "schema", "query": "PRAGMA table_info(financials)"}],
}

SAMPLE_B = {
    "terms": [{"id": "t2", "name": "profit", "type": "metric", "definition": "Profit"}],
    "mappings": [{"id": "m2", "term_id": "t2", "table": "financials", "column": "profit"}],
    "relations": [],
    "constraints": [],
    "evidence": [{"id": "e2", "source": "schema", "query": "PRAGMA table_info(financials)"}],
}


def _shared_and_projects(tmp_path):
    shared = tmp_path / "shared"
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    proj_a.mkdir()
    proj_b.mkdir()
    return str(shared), str(proj_a.resolve()), str(proj_b.resolve())


def _lane(shared, project_root):
    return resolve_project_workspace(shared, project_root)


def _project(data_source):
    return {
        "schema_version": 1,
        "mode": "rolling_trajectory",
        "data_source": data_source,
        "workload_source": {"type": "project_workload"},
        "evaluation": {"type": "llm_judge"},
        "boundary": {"strategy": "rolling_trajectory"},
    }


def test_all_tool_schemas_expose_optional_project_root():
    for spec in TOOLS + OPERATIONS:
        schema = spec["inputSchema"]
        assert "project_root" in schema["properties"]
        assert schema["additionalProperties"] is False
        assert "project_root" not in schema["required"]


def test_two_projects_isolated_versions_same_shared_root(tmp_path):
    shared, proj_a, proj_b = _shared_and_projects(tmp_path)
    ops.execute("save_version", {"workspace": shared, "project_root": proj_a,
                                 "version": "ontology_v0", "records": SAMPLE_A})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_a,
                                       "version": "ontology_v0"})
    # Version A never visible from B lane.
    assert ops.execute("list_versions", {"workspace": shared, "project_root": proj_b})["versions"] == []
    ops.execute("save_version", {"workspace": shared, "project_root": proj_b,
                                 "version": "ontology_b0", "records": SAMPLE_B})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_b,
                                       "version": "ontology_b0"})
    listing_a = ops.execute("list_versions", {"workspace": shared, "project_root": proj_a})
    listing_b = ops.execute("list_versions", {"workspace": shared, "project_root": proj_b})
    assert listing_a["versions"] == ["ontology_v0"]
    assert listing_b["versions"] == ["ontology_b0"]
    # Lanes live under the shared base; no per-repo .evoontology is created.
    lane_a = _lane(shared, proj_a)
    lane_b = _lane(shared, proj_b)
    assert str(lane_a).startswith(shared)
    assert str(lane_b).startswith(shared)
    assert lane_a != lane_b
    assert not (tmp_path / "projA" / ".evoontology").exists()
    assert not (tmp_path / "projB" / ".evoontology").exists()


def test_project_context_isolation_and_persisted_root(tmp_path):
    shared, proj_a, proj_b = _shared_and_projects(tmp_path)
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    ops.execute("configure_ontology_project", {"workspace": shared, "project_root": proj_a,
                                               "project": _project(db_a)})
    ops.execute("configure_ontology_project", {"workspace": shared, "project_root": proj_b,
                                               "project": _project(db_b)})
    status_a = ops.execute("ontology_workflow_status", {"workspace": shared, "project_root": proj_a})
    status_b = ops.execute("ontology_workflow_status", {"workspace": shared, "project_root": proj_b})
    assert status_a["project"]["data_source"] == db_a
    assert status_b["project"]["data_source"] == db_b
    # project_root persisted separately without altering data_source.
    proj_a_json = json.loads((_lane(shared, proj_a) / "project.json").read_text(encoding="utf-8"))
    proj_b_json = json.loads((_lane(shared, proj_b) / "project.json").read_text(encoding="utf-8"))
    assert proj_a_json["project_root"] == proj_a
    assert proj_b_json["project_root"] == proj_b
    assert proj_a_json["data_source"] == db_a
    assert proj_b_json["data_source"] == db_b


def test_explicit_project_root_overrides_server_default(tmp_path):
    shared, proj_a, proj_b = _shared_and_projects(tmp_path)
    ops.execute("save_version", {"workspace": shared, "project_root": proj_a,
                                 "version": "ontology_v0", "records": SAMPLE_A})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_a,
                                       "version": "ontology_v0"})
    ops.execute("save_version", {"workspace": shared, "project_root": proj_b,
                                 "version": "ontology_b0", "records": SAMPLE_B})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_b,
                                       "version": "ontology_b0"})
    server = SemanticMCPServer(shared, project_aware=True, default_project_root=proj_a)
    # No explicit project_root: explicit shared workspace still routes to default lane A.
    default_call = server.dispatch("tools/call", {"name": "list_versions",
                                                  "arguments": {"workspace": shared}})
    default_payload = json.loads(default_call["content"][0]["text"])
    assert default_payload["versions"] == ["ontology_v0"]
    # Explicit per-call project_root overrides the launch-cwd default.
    override_call = server.dispatch("tools/call", {"name": "list_versions",
                                                   "arguments": {"workspace": shared, "project_root": proj_b}})
    override_payload = json.loads(override_call["content"][0]["text"])
    assert override_payload["versions"] == ["ontology_b0"]


def test_semantic_and_manifest_route_lane(tmp_path):
    shared, proj_a, proj_b = _shared_and_projects(tmp_path)
    ops.execute("save_version", {"workspace": shared, "project_root": proj_a,
                                 "version": "ontology_v0", "records": SAMPLE_A})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_a,
                                       "version": "ontology_v0"})
    ops.execute("save_version", {"workspace": shared, "project_root": proj_b,
                                 "version": "ontology_b0", "records": SAMPLE_B})
    ops.execute("set_active_version", {"workspace": shared, "project_root": proj_b,
                                       "version": "ontology_b0"})
    server = SemanticMCPServer(shared, project_aware=True, default_project_root=proj_a)
    browse_default = server.dispatch("tools/call", {"name": "browse_semantics",
                                                    "arguments": {"workspace": shared, "query": "revenue"}})
    browse_default_payload = json.loads(browse_default["content"][0]["text"])
    assert browse_default_payload["workspace"].startswith(str(_lane(shared, proj_a)))
    browse_override = server.dispatch("tools/call", {"name": "browse_semantics",
                                                     "arguments": {"workspace": shared, "project_root": proj_b,
                                                                   "query": "profit"}})
    browse_override_payload = json.loads(browse_override["content"][0]["text"])
    assert browse_override_payload["workspace"].startswith(str(_lane(shared, proj_b)))
    manifest = server.dispatch("resources/read", {"uri": "evo-semantic://session-manifest"})
    assert "ontology_v0" in manifest["contents"][0]["text"]
    server_b = SemanticMCPServer(shared, project_aware=True, default_project_root=proj_b)
    manifest_b = server_b.dispatch("resources/read", {"uri": "evo-semantic://session-manifest"})
    assert "ontology_b0" in manifest_b["contents"][0]["text"]


def test_explicit_other_workspace_preserved_as_base(tmp_path):
    shared, proj_a, _ = _shared_and_projects(tmp_path)
    other = str(tmp_path / "other-shared")
    ops.execute("save_version", {"workspace": other, "project_root": proj_a,
                                 "version": "ontology_v0", "records": SAMPLE_A})
    lane_other = _lane(other, proj_a)
    assert (lane_other / "versions" / "ontology_v0").is_dir()
    assert not (tmp_path / "shared" / "projects").exists()


def test_legacy_flat_preserved_without_project_root(tmp_path):
    shared, proj_a, _ = _shared_and_projects(tmp_path)
    ops.execute("save_version", {"workspace": shared, "version": "ontology_flat", "records": SAMPLE_A})
    assert ops.execute("list_versions", {"workspace": shared})["versions"] == ["ontology_flat"]
    assert ops.execute("list_versions", {"workspace": shared, "project_root": proj_a})["versions"] == []
    with pytest.raises(ValueError, match="workspace is required"):
        ops.execute("list_versions", {})
    legacy_server = SemanticMCPServer(shared)
    assert legacy_server.project_aware is False
    assert legacy_server.default_project_root == ""


def test_publish_and_finalize_do_not_nest_lanes(tmp_path):
    shared, proj_a, _ = _shared_and_projects(tmp_path)
    ops.execute("configure_ontology_project", {"workspace": shared, "project_root": proj_a,
                                               "project": _project(str(tmp_path / "a.sqlite"))})
    ops.execute("save_version", {"workspace": shared, "project_root": proj_a,
                                 "version": "ontology_v0", "records": SAMPLE_A})
    published = ops.execute("publish_ontology_build", {"workspace": shared, "project_root": proj_a,
                                                       "open_browser": False})
    assert published["active_version"] == "ontology_v0"
    lane = _lane(shared, proj_a)
    assert (lane / "active.json").is_file()
    assert not (lane / "projects").exists()
    run_args = {"workspace": shared, "project_root": proj_a, "parent_version": "ontology_v0", "max_rounds": 1}
    ops.execute("start_evolution_run", run_args)
    ops.execute("begin_evolution_round", {"workspace": shared, "project_root": proj_a,
                                          "hypothesis": "h", "candidate_version": "v0-c1"})
    candidate = {k: list(v) for k, v in SAMPLE_A.items()}
    candidate["terms"] = SAMPLE_A["terms"] + [{"id": "t9", "name": "margin", "type": "metric"}]
    ops.execute("save_version", {"workspace": shared, "project_root": proj_a,
                                 "version": "v0-c1", "records": candidate})
    ops.execute("record_evolution_evaluation", {"workspace": shared, "project_root": proj_a,
                                                "subject": "v0-c1", "result": {"metrics": {"ex": 0.6}},
                                                "role": "candidate"})
    ops.execute("record_evolution_evaluation", {"workspace": shared, "project_root": proj_a,
                                                "subject": "v0-c1", "role": "candidate",
                                                "result": {"metrics": {"ex": 0.6}, "gate_input": {
                                                    "protocol": "ground_truth", "case_ids": ["case1"],
                                                    "parent_scores": [0.4], "candidate_scores": [0.6],
                                                    "unacceptable_regressions": False}}})
    ops.execute("accept_evolution", {"workspace": shared, "project_root": proj_a})
    finalized = ops.execute("finalize_evolution_run", {"workspace": shared, "project_root": proj_a,
                                                       "open_browser": False})
    assert finalized["status"] == "accepted"
    assert not (lane / "projects").exists()
    digest = hashlib.sha256(proj_a.encode("utf-8")).hexdigest()[:16]
    assert lane.name == digest


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


def test_nested_git_subdirs_share_lane_and_persist_git_root(tmp_path):
    shared = str(tmp_path / "shared")
    repo = _git_repo(tmp_path / "repo")
    sub_a = repo / "pkg" / "sub_a"
    sub_b = repo / "other"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    git_root = str(repo.resolve())

    ops.execute("configure_ontology_project", {"workspace": shared, "project_root": str(sub_a.resolve()),
                                               "project": _project(str(tmp_path / "a.sqlite"))})
    lane = _lane(shared, git_root)
    persisted = json.loads((lane / "project.json").read_text(encoding="utf-8"))
    assert persisted["project_root"] == git_root

    ops.execute("save_version", {"workspace": shared, "project_root": str(sub_b.resolve()),
                                 "version": "ontology_v0", "records": SAMPLE_A})
    ops.execute("set_active_version", {"workspace": shared, "project_root": git_root,
                                       "version": "ontology_v0"})
    assert _lane(shared, str(sub_a.resolve())) == lane
    assert _lane(shared, str(sub_b.resolve())) == lane
    digest = hashlib.sha256(git_root.encode("utf-8")).hexdigest()[:16]
    assert lane.name == digest
    listing = ops.execute("list_versions", {"workspace": shared, "project_root": str(sub_b.resolve())})
    assert listing["versions"] == ["ontology_v0"]
    assert not (repo / ".evoontology").exists()
    assert not (repo / "projects").exists()


def test_distinct_git_repos_stay_separate(tmp_path):
    shared = str(tmp_path / "shared")
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    sub_a = repo_a / "sub"
    sub_b = repo_b / "sub"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)

    ops.execute("save_version", {"workspace": shared, "project_root": str(sub_a.resolve()),
                                 "version": "ontology_v0", "records": SAMPLE_A})
    ops.execute("save_version", {"workspace": shared, "project_root": str(sub_b.resolve()),
                                 "version": "ontology_b0", "records": SAMPLE_B})
    lane_a = _lane(shared, str(sub_a.resolve()))
    lane_b = _lane(shared, str(sub_b.resolve()))
    assert lane_a != lane_b
    listing_a = ops.execute("list_versions", {"workspace": shared, "project_root": str(repo_a.resolve())})
    listing_b = ops.execute("list_versions", {"workspace": shared, "project_root": str(repo_b.resolve())})
    assert listing_a["versions"] == ["ontology_v0"]
    assert listing_b["versions"] == ["ontology_b0"]


def test_non_git_nested_dirs_retain_own_identity(tmp_path):
    shared = str(tmp_path / "shared")
    parent = tmp_path / "plain"
    child = parent / "nested"
    child.mkdir(parents=True)

    ops.execute("save_version", {"workspace": shared, "project_root": str(parent.resolve()),
                                 "version": "ontology_v0", "records": SAMPLE_A})
    assert _lane(shared, str(parent.resolve())) != _lane(shared, str(child.resolve()))
    assert ops.execute("list_versions", {"workspace": shared, "project_root": str(child.resolve())})["versions"] == []
    assert ops.execute("list_versions", {"workspace": shared, "project_root": str(parent.resolve())})["versions"] == ["ontology_v0"]
