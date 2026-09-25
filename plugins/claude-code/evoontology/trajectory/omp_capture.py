"""Capture one completed OMP user turn as a project-qualified trajectory.

Invoked by the user-global OMP extension on ``agent_end`` (the extension has no
MCP client, so it spawns this helper)::

    python -m evoontology.trajectory.omp_capture --store ABS_SHARED_ROOT < packet.json

``stdin`` JSON packet::

    {"project_root": "/abs/path/to/project", "session_id": "...", "turn_id": "...",
     "question": "first user prompt", "final_answer": "last assistant text",
     "status": "completed|failed|interrupted",
     "calls": [{"tool": "...", "arguments": {...}, "result": ..., "is_error": false}]}

``stdout`` JSON result::

    {"task_id": "omp_...", "status": "recorded|already_recorded"}

Exit codes: 0 on success (including idempotent retry), 2 on malformed input,
3 when the same project/session/turn was already recorded with different
content.

Design notes:

- Persistence reuses :func:`from_message_trace` (observable tool I/O only,
  prose/chain-of-thought dropped) and :class:`TrajectoryStore` in the
  packet's project lane from :func:`resolve_project_workspace` (``--store``
  stays the shared root; a matching legacy flat root is reused, otherwise one
  lane per project). The ``ProjectWorkflow`` start/record/finish path is
  deliberately not used: it requires prepared questions, none of which exist
  for arbitrary OMP turns.
- ``task_id`` is deterministic over
  ``(canonical project_root, session_id, turn_id)`` with a sha256 suffix, so
  retries overwrite nothing (content-identical replays return
  ``already_recorded`` preserving ``recorded_at`` and the on-disk
  ``ontology_version``, even across active-version changes) while distinct
  turns or distinct project roots never share a file.
- ``ontology_version`` is ``"uninitialized"`` unless the lane's
  ``active.json`` selects a complete version directory AND project
  attribution holds: a reused legacy flat root is proven by the resolver
  itself, while an internal lane's ``project.json`` must declare the same
  canonical ``project_root``. Versions are never guessed or created here,
  and one lane's active version is never attributed to another project.
- Only ``<lane>/trajectories/<task_id>.json`` is ever written. No
  ``project.json``, ``active.json``, ``versions/`` or ``state.json`` writes:
  capture never publishes semantic facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..ontology.store import VERSION_FILES, SemanticStore
from ..workspace import canonicalize_project_root as _canonicalize_root
from ..workspace import resolve_project_workspace
from .trajectory import TrajectoryStore, from_message_trace, truncate_result

CAPTURE_SCHEMA = "evo-omp-capture/1"
UNINITIALIZED_VERSION = "uninitialized"

TASK_STATUSES = ("completed", "failed", "interrupted")
SEMANTIC_TOOLS = ("browse_semantics", "resolve_semantics")

# Bounds keep one turn's payload small even for verbose sessions.
MAX_TEXT_CHARS = 4000
MAX_CALLS = 200
MAX_ARG_STRING_CHARS = 2000
MAX_ARG_ITEMS = 100
MAX_ARG_DEPTH = 10
MAX_ARG_CHARS = 20000
MAX_ERROR_CHARS = 2000

REDACTED = "[REDACTED]"

# Argument keys whose values are secrets, matched per token after splitting on
# separators and camelCase boundaries (so "author" never matches "auth").
_SENSITIVE_TOKENS = frozenset({
    "password", "passwd", "pwd", "secret", "secrets", "token", "tokens",
    "apikey", "apikeys", "auth", "authorization", "credential", "credentials",
    "bearer", "cookie", "cookies", "privatekey", "accesskey", "sessionkey",
    "key", "keys",
})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_SLUG_CLEAN = re.compile(r"[^A-Za-z0-9_.-]+")
_TOOL_SEP = re.compile(r"__|::|/")


class CaptureError(ValueError):
    """Malformed packet, path, or field."""


class TrajectoryConflictError(Exception):
    """Same project/session/turn already recorded with different content."""


def canonicalize_project_root(value: Any) -> str:
    """Return the canonical Git-aware identity for a project root (string form).

    Delegates to :func:`evoontology.workspace.canonicalize_project_root` so
    nested directories of one checkout share a task id, record, and lane.
    Raises :class:`CaptureError` unless ``value`` is a non-empty absolute path.
    """
    if not isinstance(value, str) or not value.strip():
        raise CaptureError("project_root must be a non-empty string")
    try:
        return str(_canonicalize_root(value.strip()))
    except ValueError as exc:
        raise CaptureError(str(exc)) from exc
    except OSError as exc:
        raise CaptureError(f"project_root is not usable: {exc}") from exc


def validate_store(value: Any) -> str:
    """Return the store path after requiring it to be absolute."""
    if not isinstance(value, str) or not value.strip():
        raise CaptureError("--store must be a non-empty absolute path")
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        raise CaptureError("--store must be an absolute path")
    return str(candidate)


def sanitize_slug(value: str, max_len: int) -> str:
    """Make ``value`` safe for a filename task-id segment."""
    slug = _SLUG_CLEAN.sub("_", value.strip())[:max_len].strip("._")
    return slug or "turn"


def make_task_id(project_root: str, session_id: str, turn_id: str) -> str:
    """Build the deterministic collision-resistant task id for one turn.

    ``project_root`` must already be canonicalized. The sha256 digest over the
    full triple keeps distinct roots/sessions/turns separate even when the
    human-readable slugs truncate.
    """
    digest = hashlib.sha256(
        f"{project_root}\0{session_id}\0{turn_id}".encode("utf-8")
    ).hexdigest()[:16]
    project_slug = sanitize_slug(Path(project_root).name, 32)
    session_slug = sanitize_slug(session_id, 16)
    turn_slug = sanitize_slug(turn_id, 16)
    return f"omp_{project_slug}_{session_slug}_{turn_slug}_{digest}"


def normalize_tool_name(tool: str) -> str:
    """Strip MCP transport prefixes from semantic tool names.

    OMP observes MCP tools under prefixed names such as
    ``mcp__evo-semantic__browse_semantics``; only the bare
    ``browse_semantics``/``resolve_semantics`` names route to ``semantic_calls``
    in :func:`from_message_trace`, so normalize those and leave native tool
    names untouched.
    """
    segments = [part for part in _TOOL_SEP.split(tool.strip()) if part]
    base = segments[-1] if segments else ""
    if base in SEMANTIC_TOOLS:
        return base
    return tool.strip()


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key names a secret-bearing field."""
    spaced = _CAMEL_BOUNDARY.sub(" ", str(key))
    tokens = _TOKEN_SPLIT.split(spaced.lower())
    return any(token in _SENSITIVE_TOKENS for token in tokens if token)


def redact_sensitive(value: Any) -> Any:
    """Recursively replace sensitive-key values with ``[REDACTED]`` (best-effort).

    Applied to call arguments, tool results, and error payloads alike; only
    mapping keys are inspected, so secrets embedded in free text are not caught.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def truncate_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Bound free text with an explicit truncation marker."""
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n…[truncated {len(text) - max_chars} chars]"


def bound_value(value: Any, depth: int = 0) -> Any:
    """Recursively bound an arguments payload (strings/items/depth)."""
    if isinstance(value, str):
        return truncate_text(value, MAX_ARG_STRING_CHARS)
    if depth >= MAX_ARG_DEPTH:
        return truncate_text(str(value), MAX_ARG_STRING_CHARS)
    if isinstance(value, dict):
        items = list(value.items())[:MAX_ARG_ITEMS]
        bounded = {key: bound_value(item, depth + 1) for key, item in items}
        if len(value) > MAX_ARG_ITEMS:
            bounded["_truncated"] = f"dropped {len(value) - MAX_ARG_ITEMS} keys"
        return bounded
    if isinstance(value, (list, tuple)):
        bounded = [bound_value(item, depth + 1) for item in list(value)[:MAX_ARG_ITEMS]]
        if len(value) > MAX_ARG_ITEMS:
            bounded.append(f"…[truncated {len(value) - MAX_ARG_ITEMS} items]")
        return bounded
    return value


def bound_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Redact then bound one call's arguments mapping."""
    bounded = bound_value(redact_sensitive(arguments))
    if not isinstance(bounded, dict):
        return {}
    serialized = json.dumps(bounded, ensure_ascii=False, sort_keys=True)
    if len(serialized) <= MAX_ARG_CHARS:
        return bounded
    return {
        "_truncated": True,
        "preview": serialized[:MAX_ARG_CHARS],
        "summary": f"{len(serialized)} chars; preview shows first {MAX_ARG_CHARS} chars.",
    }


def _require_text(payload: Dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CaptureError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(payload: Dict[str, Any], field: str, default: str = "") -> str:
    value = payload.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CaptureError(f"{field} must be a string")
    return value


def _validated_calls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls = payload.get("calls", [])
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise CaptureError("calls must be a list")
    validated: List[Dict[str, Any]] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise CaptureError(f"calls[{index}] must be an object")
        tool = call.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise CaptureError(f"calls[{index}].tool must be a non-empty string")
        arguments = call.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise CaptureError(f"calls[{index}].arguments must be an object")
        is_error = call.get("is_error", False)
        if not isinstance(is_error, bool):
            raise CaptureError(f"calls[{index}].is_error must be a boolean")
        validated.append({
            "tool": normalize_tool_name(tool),
            "arguments": bound_arguments(arguments),
            "result": call.get("result"),
            "is_error": is_error,
        })
    return validated


def _project_root_matches(project: Any, project_root: str) -> bool:
    """True when the lane's project.json declares this packet's canonical root.

    The declared value is canonicalized Git-aware via the shared workspace
    helper, so a lane configured from one subdirectory still proves a packet
    from a sibling subdirectory of the same checkout.
    """
    if not isinstance(project, dict):
        return False
    declared = project.get("project_root")
    if not isinstance(declared, str) or not declared.strip():
        return False
    try:
        return str(_canonicalize_root(declared.strip())) == project_root
    except (ValueError, OSError):
        return False


def _is_legacy_lane(lane: str, store: str) -> bool:
    """True when the resolver reused the shared root itself for this project."""
    try:
        return Path(lane).expanduser().resolve() == Path(store).expanduser().resolve()
    except OSError:
        return False


def resolve_ontology_version(lane: str, project_root: str, *, legacy_proven: bool = False) -> str:
    """Return the lane's active version only when proven applicable.

    ``project_root`` must already be canonicalized and ``lane`` is the project
    workspace from :func:`resolve_project_workspace`. The active version is
    used only if the lane's ``active.json`` parses and the selected version
    directory holds every required record file, AND project attribution holds:
    for a reused legacy flat root (``legacy_proven``) the resolver has already
    proven the flat ``project.json`` names this root, while an internal lane
    must declare the same canonical ``project_root`` in its own
    ``project.json``. (A lane's ``data_source`` may name a SQLite path rather
    than the project, so it is not project evidence.) Anything else falls back
    to ``"uninitialized"``: versions are never guessed, and one lane's active
    version is never attributed to another project.
    """
    try:
        version = SemanticStore.active_version(lane)
    except (FileNotFoundError, ValueError, OSError):
        return UNINITIALIZED_VERSION
    try:
        lane_dir = Path(lane).expanduser().resolve()
        versions_dir = lane_dir / "versions"
        version_dir = (versions_dir / version).resolve()
    except OSError:
        return UNINITIALIZED_VERSION
    if version_dir.parent != versions_dir:
        return UNINITIALIZED_VERSION
    try:
        if not all((version_dir / filename).is_file() for filename in VERSION_FILES):
            return UNINITIALIZED_VERSION
    except OSError:
        return UNINITIALIZED_VERSION
    if legacy_proven:
        return version
    try:
        project = json.loads((lane_dir / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return UNINITIALIZED_VERSION
    if not _project_root_matches(project, project_root):
        return UNINITIALIZED_VERSION
    return version


def build_record(payload: Dict[str, Any], store: str) -> Dict[str, Any]:
    """Validate one capture packet and build its trajectory record.

    ``store`` is the shared root; the record's lane workspace is resolved from
    it for version lookup.
    """
    if not isinstance(payload, dict):
        raise CaptureError("packet must be a JSON object")
    project_root = canonicalize_project_root(payload.get("project_root"))
    session_id = _require_text(payload, "session_id")
    turn_id = _require_text(payload, "turn_id")
    question = truncate_text(_require_text(payload, "question"))
    final_answer = truncate_text(_optional_text(payload, "final_answer"))
    status = payload.get("status", "completed")
    if status is None:
        status = "completed"
    if status not in TASK_STATUSES:
        raise CaptureError(f"status must be one of {sorted(TASK_STATUSES)}")
    calls = _validated_calls(payload)

    task_id = make_task_id(project_root, session_id, turn_id)
    lane = str(resolve_project_workspace(store, project_root))
    legacy_proven = _is_legacy_lane(lane, store)
    ontology_version = resolve_ontology_version(lane, project_root, legacy_proven=legacy_proven)

    messages: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    kept = calls[:MAX_CALLS]
    for call in kept:
        messages.append({"tool_call": {"tool": call["tool"], "arguments": call["arguments"]}})
        # from_message_trace pairs a call only with a non-None result; an
        # explicit empty string preserves calls whose result was null while
        # the errors list below preserves the observed failure itself.
        raw_result = call["result"]
        result = redact_sensitive(raw_result) if raw_result is not None else ""
        messages.append({"tool_result": result})
        if call["is_error"]:
            errors.append({
                "tool": call["tool"],
                "error": truncate_text(str(result), MAX_ERROR_CHARS)
                if raw_result is not None else "",
            })
    if len(calls) > MAX_CALLS:
        errors.append({
            "tool": "_capture",
            "error": f"dropped {len(calls) - MAX_CALLS} calls over the {MAX_CALLS}-call cap",
        })

    # Neutral split: ordinary OMP turns are observed behavior without
    # evidence-backed construction provenance, and must never land in a
    # validation reserve.
    record = from_message_trace(
        task_id=task_id,
        question=question,
        ontology_version=ontology_version,
        split="",
        messages=messages,
        final_answer=final_answer,
        task_status=status,
        errors=errors,
    )
    # from_message_trace bounds native results but keeps semantic results whole;
    # bound oversized semantic results the same way without touching small ones.
    for call in record["semantic_calls"]:
        text = str(call.get("result"))
        if len(text) > 2000 or len(text.splitlines()) > 20:
            call.update(truncate_result(call.get("result")))
    record.update({
        "data_source": {"type": "omp_project", "root": project_root},
        "project_root": project_root,
        "session_id": session_id,
        "turn_id": turn_id,
        "origin": "omp_auto",
        "provenance": {
            "source": "omp_agent_end",
            "capture": "evoontology.trajectory.omp_capture",
            "schema": CAPTURE_SCHEMA,
        },
    })
    return record


def _canonical(record: Dict[str, Any]) -> str:
    """Observed packet content for replay comparison.

    ``recorded_at`` is a write stamp and ``ontology_version`` is derived from
    store state at capture time, so both are excluded: a replay after an
    active-version change is still idempotent, and the on-disk version is
    preserved.
    """
    comparable = {
        key: value for key, value in record.items()
        if key not in {"recorded_at", "ontology_version"}
    }
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True)


def record_turn(store: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    """Persist one turn to its project lane; return ``(task_id, status)``.

    ``store`` is the shared root; trajectories land in the lane workspace from
    :func:`resolve_project_workspace`. A content-identical replay returns
    ``already_recorded`` and preserves the original ``recorded_at`` and
    ``ontology_version``, even when the lane's active version changed since
    the first write. A replay with different observed content — or an
    unreadable file already occupying the slot — raises
    :class:`TrajectoryConflictError` instead of overwriting.
    """
    store = validate_store(store)
    record = build_record(payload, store)
    lane = str(resolve_project_workspace(store, record["project_root"]))
    trajectories = TrajectoryStore(lane)
    try:
        existing = trajectories.load(record["task_id"])
    except FileNotFoundError:
        existing = None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrajectoryConflictError(
            f"task {record['task_id']} exists but is unreadable; refusing to overwrite: {exc}"
        )
    if existing is not None:
        if _canonical(existing) == _canonical(record):
            return record["task_id"], "already_recorded"
        raise TrajectoryConflictError(
            f"task {record['task_id']} already recorded with different content"
        )
    trajectories.append(record)
    return record["task_id"], "recorded"


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: ``--store ABS`` + packet on stdin, result on stdout."""
    parser = argparse.ArgumentParser(description="Record one OMP turn as a trajectory.")
    parser.add_argument("--store", required=True, help="Absolute shared ontology root.")
    args = parser.parse_args(argv)
    try:
        store = validate_store(args.store)
    except CaptureError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    try:
        raw = sys.stdin.read()
    except OSError as exc:
        print(json.dumps({"error": f"cannot read stdin: {exc}"}), file=sys.stderr)
        return 2
    if not raw.strip():
        print(json.dumps({"error": "packet must be a JSON object"}), file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"packet is not valid JSON: {exc}"}), file=sys.stderr)
        return 2
    try:
        task_id, status = record_turn(store, payload)
    except TrajectoryConflictError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 3
    except CaptureError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": f"storage failure: {exc}"}), file=sys.stderr)
        return 1
    print(json.dumps({"task_id": task_id, "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
