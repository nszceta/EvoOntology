"""Workspace resolution, initialization, and project-context tests."""

import hashlib
import json

import pytest

from evoontology import (
    EvolutionTrigger,
    SemanticStore,
    ensure_workspace,
    load_project,
    resolve_workspace,
    save_project,
)
from evoontology.workspace import canonicalize_project_root, resolve_project_workspace

PROJECT = {
    "schema_version": 1,
    "mode": "rolling_trajectory",
    "data_source": {"type": "sqlite", "path": "data/app.sqlite"},
    "workload_source": {"path": "data/questions.json"},
    "evaluation": {"type": "llm_judge"},
    "boundary": {"strategy": "rolling_trajectory"},
}


def test_resolve_workspace_defaults_to_project_root(tmp_path):
    assert resolve_workspace(project_root=tmp_path) == tmp_path / ".evoontology"


def test_explicit_workspace_takes_precedence(tmp_path):
    explicit = tmp_path / "benchmark-workspace"
    assert resolve_workspace(explicit, project_root=tmp_path / "ignored") == explicit


def test_ensure_workspace_creates_only_directory_skeleton(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)

    assert {path.name for path in workspace.iterdir()} == {
        "versions",
        "trajectories",
        "evolution",
    }
    assert not (workspace / "project.json").exists()
    assert not (workspace / "active.json").exists()
    assert not (workspace / "state.json").exists()


def test_save_and_load_project(tmp_path):
    path = save_project(PROJECT, project_root=tmp_path)

    assert path == tmp_path / ".evoontology" / "project.json"
    assert load_project(project_root=tmp_path) == PROJECT
    assert not path.with_name("project.json.tmp").exists()


def test_load_project_rejects_invalid_mode(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)
    invalid = dict(PROJECT, mode="unknown")
    (workspace / "project.json").write_text(
        json.dumps(invalid), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="mode"):
        load_project(workspace)


def test_save_project_normalizes_legacy_version_field(tmp_path):
    project = dict(PROJECT)
    project.pop("schema_version")
    project["version"] = 1

    save_project(project, project_root=tmp_path)

    loaded = load_project(project_root=tmp_path)
    assert loaded["schema_version"] == 1
    assert "version" not in loaded


def test_successful_initial_publication_completes_workspace(tmp_path):
    workspace = ensure_workspace(project_root=tmp_path)
    save_project(PROJECT, workspace)
    records = {
        "terms": [],
        "mappings": [],
        "relations": [],
        "constraints": [],
        "evidence": [],
    }
    SemanticStore.save_version(workspace, "ontology_v0", records)
    SemanticStore.load_version(workspace, "ontology_v0")
    SemanticStore.set_active(workspace, "ontology_v0")
    EvolutionTrigger(str(workspace)).initialize()

    assert {path.name for path in workspace.iterdir()} == {
        "project.json",
        "active.json",
        "state.json",
        "versions",
        "trajectories",
        "evolution",
    }


def _lane(base, root):
    canonical = str(root.expanduser().resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return base.expanduser().resolve() / "projects" / digest


def test_project_workspace_none_returns_legacy_flat(tmp_path):
    explicit = tmp_path / "shared"
    assert resolve_project_workspace(explicit, None) == resolve_workspace(explicit)
    assert resolve_project_workspace(explicit) == resolve_workspace(explicit)
    assert resolve_project_workspace(
        workspace=explicit, project_root=None
    ) == explicit.resolve()


def test_project_workspace_two_roots_isolated(tmp_path):
    shared = tmp_path / "shared"
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    root_a.mkdir()
    root_b.mkdir()

    lane_a = resolve_project_workspace(shared, root_a)
    lane_b = resolve_project_workspace(shared, root_b)

    assert lane_a == _lane(shared, root_a)
    assert lane_b == _lane(shared, root_b)
    assert lane_a != lane_b
    assert lane_a.parent.parent == shared.resolve()
    assert lane_a.parent.name == "projects"
    assert len(lane_a.name) == 16
    # Read-only: no lane directories created.
    assert not (shared / "projects").exists()


def test_project_workspace_symlink_alias_stable(tmp_path):
    shared = tmp_path / "shared"
    real = tmp_path / "real-proj"
    real.mkdir()
    alias = tmp_path / "alias-proj"
    alias.symlink_to(real, target_is_directory=True)

    assert resolve_project_workspace(shared, alias) == resolve_project_workspace(
        shared, real
    )
    assert resolve_project_workspace(shared, str(alias)) == _lane(shared, real)


def test_project_workspace_matching_flat_reused_via_project_root(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "proj"
    root.mkdir()
    project = dict(
        PROJECT, project_root=str(root.resolve()), data_source="other-source"
    )
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, root) == shared.resolve()
    assert resolve_project_workspace(shared, str(root.resolve())) == shared.resolve()


def test_project_workspace_matching_flat_reused_via_data_source(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "proj"
    root.mkdir()
    project = dict(
        PROJECT, data_source={"type": "omp_project", "root": str(root.resolve())}
    )
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, root) == shared.resolve()


def test_project_workspace_active_json_alone_never_reuses_flat(tmp_path):
    shared = tmp_path / "shared"
    (shared / "versions" / "ontology_v0").mkdir(parents=True)
    (shared / "active.json").write_text(
        json.dumps({"active_version": "ontology_v0"}), encoding="utf-8"
    )
    root = tmp_path / "proj"
    root.mkdir()

    assert resolve_project_workspace(shared, root) == _lane(shared, root)


def test_project_workspace_mismatched_flat_not_reused(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "proj"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    project = dict(PROJECT, project_root=str(other.resolve()))
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, root) == _lane(shared, root)
    assert resolve_project_workspace(shared, other) == shared.resolve()


def test_project_workspace_explicit_mismatch_beats_data_source(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "proj"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    project = dict(
        PROJECT,
        project_root=str(other.resolve()),
        data_source={"type": "omp_project", "root": str(root.resolve())},
    )
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, root) == _lane(shared, root)


def test_project_workspace_relative_project_root_rejected(tmp_path):
    shared = tmp_path / "shared"
    with pytest.raises(ValueError, match="absolute"):
        resolve_project_workspace(shared, "relative/path")
    with pytest.raises(ValueError, match="absolute|non-empty"):
        resolve_project_workspace(shared, "   ")


def test_project_workspace_requires_explicit_base(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    with pytest.raises(ValueError, match="workspace is required"):
        resolve_project_workspace(None, root)


def test_project_workspace_never_returns_per_repo_dir(tmp_path):
    shared = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    lane = resolve_project_workspace(shared, root)

    assert lane != root / ".evoontology"
    assert lane != root.resolve() / ".evoontology"
    assert lane.parent.parent == shared.resolve()
    assert not (root / ".evoontology").exists()
    assert not (shared / "project.json").exists()


def test_save_and_load_project_preserves_project_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    project = dict(PROJECT, project_root=str(root.resolve()))

    save_project(project, project_root=tmp_path)
    loaded = load_project(project_root=tmp_path)

    assert loaded["project_root"] == str(root.resolve())
    assert loaded["data_source"] == PROJECT["data_source"]


def test_project_workspace_lane_reentry_idempotent(tmp_path):
    shared = tmp_path / "shared"
    root = tmp_path / "proj"
    root.mkdir()

    lane = resolve_project_workspace(shared, root)

    # Empty lane (no project.json yet) re-enters unchanged, not nested.
    assert resolve_project_workspace(lane, root) == lane
    assert resolve_project_workspace(str(lane), str(root.resolve())) == lane
    assert not lane.exists()


def test_project_workspace_relative_base_rejected(tmp_path, monkeypatch):
    from pathlib import Path

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        resolve_project_workspace("relative/shared", root)
    with pytest.raises(ValueError, match="absolute"):
        resolve_project_workspace(Path("relative/shared"), root)
    # Legacy project_root=None behavior unchanged: relative still resolves.
    assert resolve_project_workspace(
        "relative/shared", None
    ) == resolve_workspace("relative/shared")


@pytest.mark.parametrize("bad_root", ["relative/path", "", "   ", 123, None])
def test_project_workspace_present_but_invalid_root_fails_closed(
    tmp_path, bad_root
):
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "proj"
    root.mkdir()
    project = dict(
        PROJECT,
        project_root=bad_root,
        data_source={"type": "omp_project", "root": str(root.resolve())},
    )
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, root) == _lane(shared, root)


def _git_lane(base, git_root):
    canonical = str(git_root.expanduser().resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return base.expanduser().resolve() / "projects" / digest


def test_canonicalize_project_root_nested_subdirs_share_git_root(tmp_path):
    repo = tmp_path / "repo"
    sub_a = repo / "pkg" / "sub_a"
    sub_b = repo / "other"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert canonicalize_project_root(sub_a) == repo.resolve()
    assert canonicalize_project_root(str(sub_b)) == repo.resolve()
    assert canonicalize_project_root(repo) == repo.resolve()


def test_canonicalize_project_root_git_file_counts_as_checkout(tmp_path):
    repo = tmp_path / "worktree"
    sub = repo / "nested"
    sub.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")

    assert canonicalize_project_root(sub) == repo.resolve()


def test_canonicalize_project_root_non_git_retains_own_identity(tmp_path):
    parent = tmp_path / "plain"
    child = parent / "nested"
    child.mkdir(parents=True)

    assert canonicalize_project_root(child) == child.resolve()
    assert canonicalize_project_root(parent) == parent.resolve()
    assert canonicalize_project_root(child) != canonicalize_project_root(parent)


def test_canonicalize_project_root_nested_git_inner_wins(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    deep = inner / "deep"
    deep.mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").mkdir()

    assert canonicalize_project_root(deep) == inner.resolve()
    sibling = outer / "sibling"
    sibling.mkdir()
    assert canonicalize_project_root(sibling) == outer.resolve()


@pytest.mark.parametrize("bad_root", ["relative/path", "", "   ", 123, None])
def test_canonicalize_project_root_rejects_non_absolute(tmp_path, bad_root):
    with pytest.raises(ValueError, match="project_root must be"):
        canonicalize_project_root(bad_root)


def test_project_workspace_nested_git_subdirs_share_lane(tmp_path):
    shared = tmp_path / "shared"
    repo = tmp_path / "repo"
    sub_a = repo / "pkg" / "sub_a"
    sub_b = repo / "other"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (repo / ".git").mkdir()

    lane_a = resolve_project_workspace(shared, sub_a)
    lane_b = resolve_project_workspace(shared, str(sub_b))
    lane_root = resolve_project_workspace(shared, repo)

    assert lane_a == lane_b == lane_root == _git_lane(shared, repo)
    assert not (shared / "projects").exists()
    assert not (repo / ".evoontology").exists()
    assert not (repo / "projects").exists()


def test_project_workspace_git_symlink_alias_stable(tmp_path):
    shared = tmp_path / "shared"
    repo = tmp_path / "repo"
    sub = repo / "pkg"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    alias = tmp_path / "alias-sub"
    alias.symlink_to(sub, target_is_directory=True)

    assert resolve_project_workspace(shared, alias) == _git_lane(shared, repo)


def test_project_workspace_distinct_git_repos_stay_separate(tmp_path):
    shared = tmp_path / "shared"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    (repo_a / "sub").mkdir(parents=True)
    (repo_b / "sub").mkdir(parents=True)
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()

    lane_a = resolve_project_workspace(shared, repo_a / "sub")
    lane_b = resolve_project_workspace(shared, repo_b / "sub")

    assert lane_a == _git_lane(shared, repo_a)
    assert lane_b == _git_lane(shared, repo_b)
    assert lane_a != lane_b


def test_project_workspace_non_git_nested_dirs_stay_separate(tmp_path):
    shared = tmp_path / "shared"
    parent = tmp_path / "plain"
    child = parent / "nested"
    child.mkdir(parents=True)

    assert resolve_project_workspace(shared, parent) == _lane(shared, parent)
    assert resolve_project_workspace(shared, child) == _lane(shared, child)
    assert resolve_project_workspace(shared, parent) != resolve_project_workspace(shared, child)


def test_project_workspace_flat_reused_via_subdir_declared_root(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    repo = tmp_path / "repo"
    sub_a = repo / "a"
    sub_b = repo / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (repo / ".git").mkdir()
    project = dict(PROJECT, project_root=str(sub_a.resolve()))
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, sub_b) == shared.resolve()
    assert resolve_project_workspace(shared, repo) == shared.resolve()


def test_project_workspace_flat_data_source_file_inside_repo_never_matches(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    db = repo / "data" / "app.sqlite"
    db.parent.mkdir(parents=True)
    db.write_text("sqlite", encoding="utf-8")
    project = dict(PROJECT)
    project["data_source"] = {"type": "sqlite", "path": str(db.resolve())}
    (shared / "project.json").write_text(json.dumps(project), encoding="utf-8")

    assert resolve_project_workspace(shared, repo) == _git_lane(shared, repo)
