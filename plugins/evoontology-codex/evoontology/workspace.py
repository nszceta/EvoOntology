"""Canonical EvoOntology workspace resolution and project context storage."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, Path]
WORKSPACE_DIRNAME = ".evoontology"
PROJECT_SCHEMA_VERSION = 1
PROJECT_MODES = {"fixed_split", "rolling_trajectory"}
PROJECTS_DIRNAME = "projects"
PROJECT_LANE_HASH_LEN = 16


def resolve_workspace(
    workspace: Optional[PathLike] = None,
    *,
    project_root: Optional[PathLike] = None,
) -> Path:
    """Return an absolute workspace path.

    An explicit ``workspace`` takes precedence. Otherwise the default is
    ``<project_root>/.evoontology`` and ``project_root`` defaults to the
    current working directory.
    """
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    root = Path(project_root) if project_root is not None else Path.cwd()
    return (root.expanduser().resolve() / WORKSPACE_DIRNAME)


def canonicalize_project_root(project_root: PathLike) -> Path:
    """Return the canonical Git-aware identity for ``project_root``.

    Validates that the value is a non-empty absolute path, resolves symlinks,
    then walks upward to the nearest ancestor (or self) containing a ``.git``
    file or directory, so nested cwd directories of one checkout share a lane.
    A ``.git`` file counts (worktrees/submodules). When no marker is found the
    resolved path itself is the identity, so non-Git directories keep their own
    lane. Only ``.git`` marker existence is read; no Git subprocess runs.

    This is the single canonical helper: lane hashing, capture identity, and
    persisted ``project_root`` must all delegate here instead of duplicating
    the traversal.
    """
    if isinstance(project_root, Path):
        candidate = project_root.expanduser()
    elif isinstance(project_root, str):
        if not project_root.strip():
            raise ValueError("project_root must be a non-empty absolute path")
        candidate = Path(project_root.strip()).expanduser()
    else:
        raise ValueError("project_root must be a non-empty absolute path")
    if not candidate.is_absolute():
        raise ValueError("project_root must be an absolute path")
    resolved = candidate.resolve()
    current = resolved
    while True:
        try:
            if os.path.lexists(current / ".git"):
                return current
        except OSError:
            pass
        if current.parent == current:
            break
        current = current.parent
    return resolved


def _candidate_source_roots(data_source: Any) -> List[str]:
    """Return path strings by which a data_source may name a project root."""
    if isinstance(data_source, str):
        return [data_source]
    if isinstance(data_source, dict):
        roots: List[str] = []
        for key in ("root", "project_root", "path"):
            value = data_source.get(key)
            if isinstance(value, str) and value.strip():
                roots.append(value)
        return roots
    return []


def _flat_matches_canonical_root(flat: Path, canonical: str) -> bool:
    """True when ``flat/project.json`` demonstrably names ``canonical``.

    A present ``project_root`` key is authoritative and fails closed: only a
    valid absolute value canonicalizing (Git-aware) to ``canonical`` reuses the
    flat root, while any present-but-invalid or mismatched value rejects it
    without consulting ``data_source``. Only when the key is absent does an
    absolute ``data_source`` root/path resolving exactly to ``canonical``
    reuse it; the ``data_source`` comparison stays an exact resolve so a
    SQLite file inside the repo never collapses to the repo root.
    Missing/unparseable files, relative values, and ``active.json`` alone
    never match.
    """
    path = flat / "project.json"
    if not path.is_file():
        return False
    try:
        project = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(project, dict):
        return False
    if "project_root" in project:
        explicit = project["project_root"]
        if isinstance(explicit, str) and explicit.strip():
            try:
                return str(canonicalize_project_root(explicit.strip())) == canonical
            except (ValueError, OSError):
                return False
        return False
    for candidate_text in _candidate_source_roots(project.get("data_source")):
        candidate = Path(candidate_text.strip()).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            if str(candidate.resolve()) == canonical:
                return True
        except OSError:
            continue
    return False


def resolve_project_workspace(
    workspace: Optional[PathLike] = None,
    project_root: Optional[PathLike] = None,
) -> Path:
    """Return the internal workspace for one project lane.

    ``workspace`` is the external base (typically the shared store) and
    ``project_root`` is the canonical project identity. Both may be passed
    positionally or by keyword; the return is always an absolute ``Path``.
    ``project_root`` is canonicalized Git-aware via
    :func:`canonicalize_project_root`, so nested cwd directories of one
    checkout hash to the same lane while distinct checkouts and non-Git
    directories keep their own.

    When ``project_root`` is ``None`` this is exactly
    :func:`resolve_workspace` (legacy flat behavior, unchanged). When it is
    provided, ``workspace`` must be an explicit absolute path and
    ``project_root`` must be absolute; the result is
    ``<base>/projects/<sha256(canonical)[:16]>`` unless ``<base>/project.json``
    demonstrably names the same canonical root via ``project_root`` or
    ``data_source``, in which case the flat ``<base>`` itself is reused.
    ``active.json`` alone never triggers reuse. Passing an already-resolved
    lane for the same root is idempotent.

    This resolver is read-only: it never creates directories and never
    writes ``project.json``. Empty lanes are valid; callers create them via
    ``ensure_workspace``/``save_project`` on the returned path.
    """
    if project_root is None:
        return resolve_workspace(workspace)
    if workspace is None or (
        isinstance(workspace, str) and not workspace.strip()
    ):
        raise ValueError(
            "workspace is required when project_root is provided: "
            "pass the absolute shared store path"
        )
    if isinstance(workspace, str):
        base_candidate = Path(workspace.strip()).expanduser()
    elif isinstance(workspace, Path):
        base_candidate = workspace.expanduser()
    else:
        raise ValueError(
            "workspace must be an absolute path when project_root is provided"
        )
    if not base_candidate.is_absolute():
        raise ValueError(
            "workspace must be an absolute path when project_root is provided"
        )
    canonical = str(canonicalize_project_root(project_root))
    base = base_candidate.resolve()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :PROJECT_LANE_HASH_LEN
    ]
    if base.parent.name == PROJECTS_DIRNAME and base.name == digest:
        return base
    if _flat_matches_canonical_root(base, canonical):
        return base
    return base / PROJECTS_DIRNAME / digest


def resolve_workspace_for_version(
    workspace: Optional[PathLike] = None,
    *,
    project_root: Optional[PathLike] = None,
    version: str = "active",
) -> Path:
    """Find one existing workspace that can serve a read-only version request.

    ``workspace`` may name the final workspace, a ``.evoontology`` container
    holding database-specific workspaces, or a project root containing such a
    container. Direct matches win. A unique nested match is discovered at any
    depth; ambiguous matches raise instead of selecting an arbitrary database.

    This resolver never creates or modifies workspace state. Write operations
    must continue to use :func:`resolve_workspace` with an exact destination.
    """
    requested = str(version or "active").strip() or "active"
    root = resolve_workspace(workspace, project_root=project_root)
    if _workspace_has_version(root, requested):
        return root

    nested_container = root / WORKSPACE_DIRNAME
    search_root = nested_container if nested_container.is_dir() else root
    if not search_root.is_dir():
        return search_root

    if requested == "active":
        possible = {path.parent for path in search_root.rglob("active.json")}
    else:
        possible = {path.parent for path in search_root.rglob("versions")}
    candidates = sorted(
        {
            candidate.resolve()
            for candidate in possible
            if _workspace_has_version(candidate, requested)
        },
        key=lambda path: str(path).casefold(),
    )
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        listed = "\n".join(f"- {path}" for path in candidates)
        raise ValueError(
            f"Multiple EvoOntology workspaces match version {requested!r} under "
            f"{search_root}:\n{listed}\nPass the exact workspace path."
        )
    return search_root


def _workspace_has_version(root: Path, version: str) -> bool:
    if version != "active":
        return (root / "versions" / version).is_dir()
    active_file = root / "active.json"
    if not active_file.is_file():
        return False
    try:
        active = json.loads(active_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(active, dict):
        return False
    active_version = str(
        active.get("active_version") or active.get("version") or ""
    ).strip()
    return bool(active_version) and (root / "versions" / active_version).is_dir()


def ensure_workspace(
    workspace: Optional[PathLike] = None,
    *,
    project_root: Optional[PathLike] = None,
) -> Path:
    """Create the idempotent workspace directory skeleton.

    State-bearing JSON files are written only when their corresponding
    lifecycle step completes: Step 0 writes ``project.json``; publication
    writes ``active.json`` and initializes ``state.json``.
    """
    root = resolve_workspace(workspace, project_root=project_root)
    for directory in ("versions", "trajectories", "evolution"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    return root


def load_project(
    workspace: Optional[PathLike] = None,
    *,
    project_root: Optional[PathLike] = None,
) -> Dict[str, Any]:
    """Load and minimally validate ``<workspace>/project.json``."""
    root = resolve_workspace(workspace, project_root=project_root)
    path = root / "project.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing EvoOntology project context: {path}")
    try:
        project = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid project.json: {exc}") from exc
    return _validate_project(project)


def save_project(
    project: Dict[str, Any],
    workspace: Optional[PathLike] = None,
    *,
    project_root: Optional[PathLike] = None,
) -> Path:
    """Validate and atomically persist ``project.json``."""
    normalized = _validate_project(project)
    root = ensure_workspace(workspace, project_root=project_root)
    path = root / "project.json"
    temporary = root / "project.json.tmp"
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _validate_project(project: Any) -> Dict[str, Any]:
    if not isinstance(project, dict):
        raise ValueError("project.json must contain a JSON object")
    schema_version = project.get("schema_version", project.get("version"))
    if schema_version != PROJECT_SCHEMA_VERSION:
        raise ValueError(
            f"project.json schema_version must be {PROJECT_SCHEMA_VERSION}"
        )
    mode = project.get("mode")
    if mode not in PROJECT_MODES:
        raise ValueError(f"project.json mode must be one of {sorted(PROJECT_MODES)}")
    for field in ("data_source", "workload_source", "evaluation", "boundary"):
        if field not in project:
            raise ValueError(f"project.json is missing required field: {field}")
    for field in ("evaluation", "boundary"):
        if not isinstance(project[field], dict):
            raise ValueError(f"project.json field {field!r} must be an object")
    normalized = dict(project)
    normalized.pop("version", None)
    normalized["schema_version"] = PROJECT_SCHEMA_VERSION
    return normalized
