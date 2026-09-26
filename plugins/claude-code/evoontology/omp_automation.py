"""Durable per-lane coordinator for OMP maintenance automation.

The OMP extension (same-session triggered custom turns) drives this module as a
CLI over stdin/stdout::

    python -m evoontology.omp_automation --store ABS_SHARED --project-root ABS_CWD

``stdin`` is one JSON object with ``op`` in
``status|seed|claim|heartbeat|finish|release``; ``stdout`` is exactly one JSON
object. There are no LLM calls and no fabricated facts here: every decision is
read from the lane's existing workspace state (project context, active
version, running runs, trigger numbers) and only ``<lane>/automation.json``
(+ a transient ``<lane>/automation.lock``) is ever written. ``project.json``,
``active.json``, ``versions/`` and trigger checkpoints are never modified, and
the previous version is always preserved.

Job kinds handed to the maintenance turn (which does the real build/evolve
work through the normal tool path, at most ``MAX_ROUNDS`` rounds within
``MAINTENANCE_TIMEOUT_SECONDS``):

- ``build``: no valid active version, but a positively qualified user seed is
  persisted (the extension classifies YES data+goal prompts; this module never
  infers goals from arbitrary captures). Carries ``seed``.
- ``evolve``: valid active version + matching configured project + trigger due.
  Carries ``parent_version``.
- ``resume``: a running run exists. Carries ``run_id`` and the run's
  ``trajectory_checkpoint`` (possibly null for pre-migration runs); the worker
  must freeze/split trajectories only at or below that cutoff.

Cross-process safety: every state mutation happens inside a short-lived OS
advisory lock on ``<lane>/automation.lock`` (``fcntl`` on POSIX, ``msvcrt``
on Windows; the OS releases it on process death, so no stale-unlink dance
can delete a live contender's lock). The lock is never held during
maintenance work; crash recovery comes from lease expiry: an expired lease
is reconciled from actual store state (accepted run / published version vs
unfinished), never by blindly rerunning accepted jobs. Unfinished or lost
execution yields a ``FAILURE_COOLDOWN_SECONDS`` cooldown instead of tight
redispatch, and the run itself is left untouched (this module never calls
``mark_incomplete``).
A new qualifying user seed clears a build cooldown, but repeating the same
seed never does.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from .evolution.session import RUNNING, EvolutionSession
from .ontology.store import SemanticStore
from .trajectory.omp_capture import redact_sensitive, truncate_text
from .trigger.trigger import EvolutionTrigger
from .workspace import (
    canonicalize_project_root,
    load_project,
    resolve_project_workspace,
)
from .workflow import question_key

AUTOMATION_SCHEMA = "evo-omp-automation/1"
AUTOMATION_FILENAME = "automation.json"
LOCK_FILENAME = "automation.lock"

DEFAULT_LEASE_SECONDS = 900
MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 3600
MAX_ROUNDS = 2
MAINTENANCE_TIMEOUT_SECONDS = 600
FAILURE_COOLDOWN_SECONDS = 24 * 3600
SUCCESS_NEXT_CHECK_SECONDS = 3600
IDLE_NEXT_CHECK_SECONDS = 3600
LOCK_WAIT_SECONDS = 10.0

MAX_QUESTION_CHARS = 4000
MAX_SOURCE_REF_CHARS = 1024
MAX_OWNER_CHARS = 256
MAX_JOB_ID_CHARS = 128

ENV_AUTOMATION = "EVO_ONTOLOGY_AUTOMATION"
ENV_STORE = "EVO_ONTOLOGY_STORE"

OPS = ("status", "seed", "claim", "heartbeat", "finish", "release")


class AutomationError(ValueError):
    """Malformed input, failed validation, or owner/token mismatch."""


class AutomationBusy(Exception):
    """Transient lock contention: the dispatcher should retry shortly."""


# ---- small helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _automation_disabled() -> bool:
    return os.environ.get(ENV_AUTOMATION, "").strip() == "0"


def _default_store() -> str:
    override = os.environ.get(ENV_STORE, "").strip()
    if override:
        return override
    return str(Path.home() / ".omp" / "ontologies" / "shared")


def _resolve_store(value: Any) -> str:
    text = str(value or "").strip() or _default_store()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise AutomationError("--store must be an absolute path")
    return str(candidate)


def _resolve_project_root(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return str(Path.cwd().resolve())
    if not isinstance(value, str) or not value.strip():
        raise AutomationError("--project-root must be an absolute path")
    candidate = Path(value.strip()).expanduser()
    if not candidate.is_absolute():
        raise AutomationError("--project-root must be an absolute path")
    return str(candidate)


def _lane(store: str, project_root: str) -> Tuple[Path, str]:
    try:
        canonical = str(canonicalize_project_root(project_root))
    except (ValueError, OSError) as exc:
        raise AutomationError(f"project_root is not usable: {exc}") from exc
    try:
        lane = resolve_project_workspace(store, project_root)
    except (ValueError, OSError) as exc:
        raise AutomationError(str(exc)) from exc
    return lane, canonical


# ---- lock + state ------------------------------------------------------------


def _acquire_lock(lane: Path):
    """Open the lane lock file and take a non-blocking exclusive OS lock.

    The returned file object keeps the lock until closed; the OS releases it
    on process death, so a crashed holder can never wedge contenders and no
    contender ever deletes another's lock. The file itself persists.
    """
    lane.mkdir(parents=True, exist_ok=True)
    handle = open(lane / LOCK_FILENAME, "a+b")  # noqa: PTH123
    start = time.monotonic()
    while True:
        try:
            if os.name == "nt":
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() - start > LOCK_WAIT_SECONDS:
                handle.close()
                raise AutomationBusy("automation lock busy; retry shortly")
            time.sleep(0.05)


def _default_state() -> Dict[str, Any]:
    return {
        "schema": AUTOMATION_SCHEMA,
        "seed": None,
        "lease": None,
        "last": None,
        "cooldown_until": None,
        "cooldown_reason": "",
        "cooldown_seed_key": None,
        "last_completed_seed_key": None,
        "last_released": None,
    }


def _load_state(lane: Path) -> Dict[str, Any]:
    path = lane / AUTOMATION_FILENAME
    if not path.is_file():
        return _default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AutomationError(f"automation state is unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise AutomationError("automation state is unreadable")
    merged = _default_state()
    merged.update({k: v for k, v in state.items() if k in merged})
    merged["schema"] = AUTOMATION_SCHEMA
    return merged


def _save_state(lane: Path, state: Dict[str, Any]) -> None:
    path = lane / AUTOMATION_FILENAME
    temporary = lane / (AUTOMATION_FILENAME + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


@contextlib.contextmanager
def _mutate(lane: Path) -> Iterator[Dict[str, Any]]:
    handle = _acquire_lock(lane)
    try:
        state = _load_state(lane)
        yield state
        _save_state(lane, state)
    finally:
        handle.close()


def _require_owner(payload: Dict[str, Any]) -> str:
    owner = payload.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise AutomationError("owner must be a non-empty string")
    if len(owner.strip()) > MAX_OWNER_CHARS:
        raise AutomationError("owner is too long")
    return owner.strip()


def _require_job_id(payload: Dict[str, Any]) -> str:
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise AutomationError("job_id must be a non-empty string")
    if len(job_id.strip()) > MAX_JOB_ID_CHARS:
        raise AutomationError("job_id is too long")
    return job_id.strip()


def _require_token(payload: Dict[str, Any]) -> str:
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise AutomationError("token is required")
    return token


def _lease_seconds(payload: Dict[str, Any]) -> int:
    raw = payload.get("lease_seconds", DEFAULT_LEASE_SECONDS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AutomationError("lease_seconds must be an integer")
    if not (MIN_LEASE_SECONDS <= raw <= MAX_LEASE_SECONDS):
        raise AutomationError(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )
    return raw


def _check_owner_token(lease: Dict[str, Any], owner: str, token: str) -> None:
    expected_owner = str(lease.get("owner") or "")
    expected_token = lease.get("token")
    if not isinstance(expected_token, str) or not expected_token:
        raise AutomationError("lease owner/token mismatch")
    if not hmac.compare_digest(owner, expected_owner) or not hmac.compare_digest(
        token.encode("utf-8"), expected_token.encode("utf-8")
    ):
        raise AutomationError("lease owner/token mismatch")


def _seed_key(question: str) -> str:
    digest = hashlib.sha256(question_key(question).encode("utf-8")).hexdigest()[:16]
    return f"seed_{digest}"


# ---- lane inspection (read-only; never publishes or checkpoints) -------------


def _active_state(lane: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(version, blocker)`` for the lane's active version.

    ``(None, None)`` is a blank lane; ``blocker`` names a present-but-broken
    ``active.json`` that automation must report instead of building over.
    """
    if not (lane / "active.json").is_file():
        return None, None
    try:
        version, _records = SemanticStore.load_records(str(lane))
    except (OSError, ValueError, KeyError) as exc:
        return None, f"active.json is invalid: {exc}"
    return version, None


def _read_project(lane: Path, canonical: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return ``(project, note)``; ``note`` explains a missing/unusable project."""
    try:
        project = load_project(str(lane))
    except FileNotFoundError:
        return None, "no project.json in lane"
    except (ValueError, OSError) as exc:
        return None, f"project.json unreadable: {exc}"
    explicit = project.get("project_root")
    if isinstance(explicit, str) and explicit.strip():
        try:
            if str(canonicalize_project_root(explicit.strip())) != canonical:
                return None, "project.json belongs to a different project root"
        except (ValueError, OSError):
            return None, "project.json project_root is not usable"
    return project, ""


def _inspect(lane: Path, canonical: str) -> Dict[str, Any]:
    """Read actual lane state without changing versions or checkpoints.

    ``EvolutionTrigger.check`` may flip its own cached ``evolution_due`` flag,
    but it never advances the checkpoint or publishes anything.
    """
    active, blocker = _active_state(lane)
    project, project_note = _read_project(lane, canonical)
    try:
        latest = EvolutionSession(str(lane)).latest_run()
    except (OSError, ValueError):
        latest = None
    if not isinstance(latest, dict):
        latest = None
    run = latest if latest is not None and latest.get("status") == RUNNING else None
    try:
        trigger = EvolutionTrigger(str(lane)).check()
    except (OSError, ValueError):
        trigger = {
            "evolution_due": False,
            "new_trajectories": 0,
            "reason": "trigger unreadable",
        }
    return {
        "active_version": active,
        "active_blocker": blocker,
        "project": project,
        "project_note": project_note,
        "run": run,
        "latest_run_id": str(latest.get("run_id")) if latest else None,
        "latest_run_status": str(latest.get("status")) if latest else None,
        "evolution_due": bool(trigger.get("evolution_due", False)),
        "due_reason": str(trigger.get("reason", "")),
    }


# ---- outcomes -----------------------------------------------------------------


def _infer_outcome(
    job: Dict[str, Any], inspection: Dict[str, Any]
) -> Tuple[str, str]:
    """Infer ``(outcome, reason)`` from actual store state, never caller flags.

    Success needs proof the job itself did something: an evolve job only
    completes when a run accepted *after* the claim baseline changed the
    active version, and a resume job only completes when its exact run
    accepted. A stale already-accepted run the job never touched is a
    cooldown, never a completion.
    """
    kind = str(job.get("kind") or "")
    active = inspection.get("active_version")
    if kind == "build":
        if active:
            return "completed", f"initial version {active} is published and active"
        return "cooldown", "build job ended without a published version"
    run = inspection.get("run")
    if isinstance(run, dict):
        # A run is still open: unfinished work, never auto-closed here.
        return "cooldown", f"run {run.get('run_id')} is still running"
    latest_id = inspection.get("latest_run_id")
    latest_status = inspection.get("latest_run_status")
    if kind == "resume":
        target = str(job.get("run_id") or "")
        if latest_id == target and latest_status == "accepted":
            return "completed", f"resumed run {target} reached accepted"
        if latest_status:
            return "cooldown", f"run {target} ended with status {latest_status!r}"
        return "cooldown", f"run {target} ended without an accepted run"
    parent = str(job.get("parent_version") or "")
    baseline = job.get("baseline_run_id")
    if latest_status == "accepted" and latest_id != baseline:
        if active and active != parent:
            return "completed", f"evolution published {active} from {parent}"
        return "cooldown", "latest accepted run did not change the active version"
    if latest_id == baseline and latest_id is not None:
        return "cooldown", "no new accepted run since claim; job did nothing"
    if latest_status:
        return "cooldown", f"latest run ended with status {latest_status!r}"
    return "cooldown", "evolution job ended without an accepted run"


def _set_cooldown(
    state: Dict[str, Any], reason: str, now: datetime, seed_key: Optional[str]
) -> None:
    state["cooldown_until"] = _next_at(now, FAILURE_COOLDOWN_SECONDS)
    state["cooldown_reason"] = reason
    state["cooldown_seed_key"] = seed_key


def _clear_cooldown(state: Dict[str, Any]) -> None:
    state["cooldown_until"] = None
    state["cooldown_reason"] = ""
    state["cooldown_seed_key"] = None


def _next_at(now: datetime, seconds: int) -> str:
    return _iso(
        datetime.fromtimestamp(now.timestamp() + seconds, tz=timezone.utc)
    )


def _token_digest(token: Any) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _settle_job(
    state: Dict[str, Any],
    job: Dict[str, Any],
    inspection: Dict[str, Any],
    now: datetime,
) -> Tuple[str, str, str]:
    """Fold one ended lease into durable outcome state; return outcome/reason/next."""
    outcome, reason = _infer_outcome(job, inspection)
    seed_key = None
    seed = job.get("seed")
    if isinstance(seed, dict) and seed.get("question_key"):
        seed_key = str(seed["question_key"])
    if outcome == "completed":
        if inspection.get("active_version"):
            state["seed"] = None
            if seed_key:
                state["last_completed_seed_key"] = seed_key
        _clear_cooldown(state)
        next_check = _next_at(now, SUCCESS_NEXT_CHECK_SECONDS)
    else:
        _set_cooldown(state, reason, now, seed_key)
        next_check = str(state.get("cooldown_until") or "")
    state["last"] = {
        "job_id": str(job.get("job_id")),
        "kind": str(job.get("kind")),
        "outcome": outcome,
        "ended_at": _iso(now),
        "reason": reason,
        "owner": str(job.get("owner") or ""),
        "token_sha256": _token_digest(job.get("token")),
    }
    state["lease"] = None
    return outcome, reason, next_check


def _reconcile_expired(
    state: Dict[str, Any], inspection: Dict[str, Any], now: datetime
) -> Tuple[str, str, str]:
    """Fold an expired lease into durable outcome state from actual store."""
    job = state["lease"]
    assert isinstance(job, dict)
    return _settle_job(state, job, inspection, now)


def _public_lease(lease: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": str(lease.get("job_id")),
        "kind": str(lease.get("kind")),
        "owner": str(lease.get("owner")),
        "expires_at": str(lease.get("expires_at")),
    }



def _public_last(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Completion metadata safe for responses: never the token digest."""
    last = state.get("last")
    if not isinstance(last, dict):
        return None
    return {
        "job_id": str(last.get("job_id")),
        "kind": str(last.get("kind")),
        "outcome": str(last.get("outcome")),
        "ended_at": str(last.get("ended_at")),
        "reason": str(last.get("reason")),
    }


def _finished_slot(state: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    for key in ("last", "last_released"):
        slot = state.get(key)
        if isinstance(slot, dict) and str(slot.get("job_id")) == job_id:
            return slot
    return None


def _check_finished_owner(slot: Dict[str, Any], owner: str, token: str) -> None:
    """Fail closed on finished-slot repeats: the digest stays server-side."""
    expected_owner = slot.get("owner")
    if not isinstance(expected_owner, str) or not hmac.compare_digest(
        owner, expected_owner
    ):
        raise AutomationError("lease owner/token mismatch")
    expected_digest = slot.get("token_sha256")
    if not isinstance(expected_digest, str) or not hmac.compare_digest(
        _token_digest(token), expected_digest
    ):
        raise AutomationError("lease owner/token mismatch")


def _lease_expired(lease: Dict[str, Any], now: datetime) -> bool:
    expires = _parse_time(lease.get("expires_at"))
    return expires is None or expires <= now


# ---- ops ----------------------------------------------------------------------


def _cooldown_active(state: Dict[str, Any], now: datetime) -> bool:
    until = _parse_time(state.get("cooldown_until"))
    return until is not None and until > now


def _maybe_wake_with_seed(
    state: Dict[str, Any], inspection: Dict[str, Any]
) -> bool:
    """Clear a cooldown when a genuinely new seed waits for an initial build.

    Only a seed whose key differs from both the cooldown-time key and the last
    completed key wakes the lane; repeating the same seed never loops.
    """
    seed = state.get("seed")
    if not isinstance(seed, dict) or not seed.get("question_key"):
        return False
    if inspection.get("active_version"):
        return False
    key = str(seed["question_key"])
    if key == state.get("cooldown_seed_key"):
        return False
    if key == state.get("last_completed_seed_key"):
        return False
    _clear_cooldown(state)
    return True


def op_status(
    lane: Path, canonical: str, state: Dict[str, Any], now: datetime
) -> Dict[str, Any]:
    inspection = _inspect(lane, canonical)
    lease = state.get("lease")
    if isinstance(lease, dict) and _lease_expired(lease, now):
        _reconcile_expired(state, inspection, now)
        lease = None
    seed = state.get("seed")
    return {
        "status": "ok",
        "lane": str(lane),
        "active_version": inspection.get("active_version"),
        "active_blocker": inspection.get("active_blocker"),
        "has_project": inspection.get("project") is not None,
        "project_note": inspection.get("project_note"),
        "evolution_due": inspection.get("evolution_due"),
        "due_reason": inspection.get("due_reason"),
        "running_run": (
            str(inspection["run"].get("run_id")) if inspection.get("run") else None
        ),
        "seed_present": isinstance(seed, dict),
        "lease": _public_lease(lease) if isinstance(lease, dict) else None,
        "cooldown_until": state.get("cooldown_until"),
        "cooldown_reason": state.get("cooldown_reason", ""),
        "last": _public_last(state),
        "next_check_at": _next_at(now, IDLE_NEXT_CHECK_SECONDS),
    }


def op_seed(
    state: Dict[str, Any],
    payload: Dict[str, Any],
    lane: Path,
    canonical: str,
    now: datetime,
) -> Dict[str, Any]:
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AutomationError("question must be a non-empty string")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise AutomationError(
            f"question must be at most {MAX_QUESTION_CHARS} characters"
        )
    # The seed is untrusted user data: reuse the capture scrub helpers so no
    # credential-bearing text is persisted or handed back out (the same policy
    # as captured trajectories, no second abstraction). The dedup key covers
    # the scrubbed text so repeats stay stable.
    scrubbed = redact_sensitive(question)
    question = truncate_text(
        scrubbed if isinstance(scrubbed, str) else str(scrubbed), MAX_QUESTION_CHARS
    )
    if not question_key(question):
        raise AutomationError("question contains no usable content")
    source_ref = payload.get("source_ref", "")
    if source_ref is None:
        source_ref = ""
    if not isinstance(source_ref, str):
        raise AutomationError("source_ref must be a string")
    if len(source_ref) > MAX_SOURCE_REF_CHARS:
        raise AutomationError("source_ref is too long")
    key = _seed_key(question)
    existing = state.get("seed")
    duplicate = isinstance(existing, dict) and existing.get("question_key") == key
    if not duplicate:
        # Seeds never touch project context; only automation.json changes.
        state["seed"] = {
            "question": question,
            "source_ref": source_ref,
            "question_key": key,
            "received_at": _iso(now),
            "provenance": {"source": "user"},
        }
    inspection = _inspect(lane, canonical)
    cleared = False
    if _cooldown_active(state, now):
        cleared = _maybe_wake_with_seed(state, inspection)
    return {
        "status": "seeded",
        "seed_key": key,
        "duplicate": bool(duplicate),
        "cleared_cooldown": cleared,
    }


def _decide_job(
    state: Dict[str, Any], inspection: Dict[str, Any]
) -> Tuple[Optional[str], str, Dict[str, Any]]:
    """Return ``(kind, reason, extra)`` for the next job, or ``(None, ...)``."""
    run = inspection.get("run")
    if isinstance(run, dict):
        return "resume", f"run {run.get('run_id')} is still running", {
            "run_id": str(run.get("run_id")),
            # Preserved verbatim (may be absent on pre-migration runs):
            # the worker freezes/splits only at or below this cutoff.
            "trajectory_checkpoint": run.get("trajectory_checkpoint"),
        }
    if not inspection.get("active_version"):
        blocker = inspection.get("active_blocker")
        if blocker:
            return None, f"blocked: {blocker}; repair or remove it, then retry", {}
        seed = state.get("seed")
        if isinstance(seed, dict) and seed.get("question"):
            return "build", "no active version; qualified user seed is pending", {
                "seed": {
                    "question": str(seed["question"]),
                    "source_ref": str(seed.get("source_ref") or ""),
                },
            }
        return None, "blank lane without a qualified user seed", {}
    if inspection.get("project") is None:
        note = inspection.get("project_note") or "no configured project"
        return None, f"missing prerequisites: {note}", {}
    if inspection.get("evolution_due"):
        return (
            "evolve",
            str(inspection.get("due_reason") or "evolution is due"),
            {
                "parent_version": str(inspection.get("active_version")),
                "baseline_run_id": inspection.get("latest_run_id"),
            },
        )
    return None, "evolution is not due", {}


def _build_job(
    kind: str, owner: str, seconds: int, extra: Dict[str, Any], now: datetime
) -> Dict[str, Any]:
    job: Dict[str, Any] = {
        "job_id": f"job_{secrets.token_hex(8)}",
        "kind": kind,
        "owner": owner,
        "token": secrets.token_hex(16),
        "claimed_at": _iso(now),
        "expires_at": _next_at(now, seconds),
        "lease_seconds": seconds,
        "max_rounds": MAX_ROUNDS,
        "baseline_run_id": extra.get("baseline_run_id"),
    }
    if kind == "build" and isinstance(extra.get("seed"), dict):
        seed = extra["seed"]
        assert isinstance(seed, dict)
        job["seed"] = {
            "question": seed["question"],
            "source_ref": seed.get("source_ref", ""),
            "question_key": _seed_key(str(seed["question"])),
        }
    if kind == "evolve":
        job["parent_version"] = str(extra.get("parent_version") or "")
        if not job["parent_version"]:
            raise AutomationError("cannot evolve without an active parent version")
    if kind == "resume":
        job["run_id"] = str(extra.get("run_id") or "")
        if not job["run_id"]:
            raise AutomationError("cannot resume without a run id")
        job["baseline_run_id"] = job["run_id"]
        job["trajectory_checkpoint"] = extra.get("trajectory_checkpoint")
    return job


def _claimed_result(job: Dict[str, Any]) -> Dict[str, Any]:
    view: Dict[str, Any] = {
        "id": job["job_id"],
        "kind": job["kind"],
        "owner": job["owner"],
        "lease_token": job["token"],
        "expires_at": job["expires_at"],
        "max_rounds": MAX_ROUNDS,
        "timeout_seconds": MAINTENANCE_TIMEOUT_SECONDS,
    }
    if job["kind"] == "build" and isinstance(job.get("seed"), dict):
        seed = job["seed"]
        assert isinstance(seed, dict)
        view["seed"] = {
            "question": seed["question"],
            "source_ref": seed.get("source_ref", ""),
        }
    if job["kind"] == "evolve":
        view["parent_version"] = job["parent_version"]
    if job["kind"] == "resume":
        view["run_id"] = job["run_id"]
        view["trajectory_checkpoint"] = job.get("trajectory_checkpoint")
    return {"status": "claimed", "job": view}


def op_claim(
    state: Dict[str, Any],
    payload: Dict[str, Any],
    lane: Path,
    canonical: str,
    now: datetime,
) -> Dict[str, Any]:
    owner = _require_owner(payload)
    seconds = _lease_seconds(payload)
    inspection = _inspect(lane, canonical)
    lease = state.get("lease")
    if isinstance(lease, dict):
        if _lease_expired(lease, now):
            _reconcile_expired(state, inspection, now)
            lease = None
        elif str(lease.get("owner") or "") == owner:
            return _claimed_result(lease)
        else:
            return {
                "status": "busy",
                "reason": f"job {lease.get('job_id')} is leased to another owner",
                "lease": _public_lease(lease),
                "next_check_at": str(lease.get("expires_at")),
            }
    if _cooldown_active(state, now):
        if not _maybe_wake_with_seed(state, inspection):
            return {
                "status": "cooldown",
                "reason": str(state.get("cooldown_reason") or "cooling down"),
                "next_check_at": str(state.get("cooldown_until")),
            }
    kind, reason, extra = _decide_job(state, inspection)
    if kind is None:
        return {
            "status": "idle",
            "reason": reason,
            "next_check_at": _next_at(now, IDLE_NEXT_CHECK_SECONDS),
        }
    job = _build_job(kind, owner, seconds, extra, now)
    state["lease"] = job
    return _claimed_result(job)


def _require_lease(
    state: Dict[str, Any], payload: Dict[str, Any]
) -> Dict[str, Any]:
    job_id = _require_job_id(payload)
    owner = _require_owner(payload)
    token = _require_token(payload)
    lease = state.get("lease")
    if not isinstance(lease, dict) or str(lease.get("job_id")) != job_id:
        last = state.get("last")
        if isinstance(last, dict) and str(last.get("job_id")) == job_id:
            raise AutomationError("job already finished")
        raise AutomationError("unknown job")
    _check_owner_token(lease, owner, token)
    return lease


def op_heartbeat(
    state: Dict[str, Any],
    payload: Dict[str, Any],
    lane: Path,
    canonical: str,
    now: datetime,
) -> Dict[str, Any]:
    seconds = _lease_seconds(payload)
    try:
        lease = _require_lease(state, payload)
    except AutomationError as exc:
        if str(exc) == "job already finished":
            slot = _finished_slot(state, _require_job_id(payload))
            assert slot is not None
            _check_finished_owner(
                slot, _require_owner(payload), _require_token(payload)
            )
            return {
                "status": "released",
                "job_id": _require_job_id(payload),
                "idempotent": True,
            }
        raise
    if _lease_expired(lease, now):
        inspection = _inspect(lane, canonical)
        outcome, reason, next_check = _reconcile_expired(state, inspection, now)
        return {
            "status": "expired",
            "job_id": str(lease.get("job_id")),
            "outcome": outcome,
            "reason": reason,
            "next_check_at": next_check,
        }
    lease["expires_at"] = _next_at(now, seconds)
    lease["lease_seconds"] = seconds
    return {
        "status": "heartbeat",
        "job_id": str(lease.get("job_id")),
        "expires_at": lease["expires_at"],
    }


def op_finish(
    state: Dict[str, Any],
    payload: Dict[str, Any],
    lane: Path,
    canonical: str,
    now: datetime,
) -> Dict[str, Any]:
    try:
        lease = _require_lease(state, payload)
    except AutomationError as exc:
        if str(exc) == "job already finished":
            slot = _finished_slot(state, _require_job_id(payload))
            assert slot is not None
            _check_finished_owner(
                slot, _require_owner(payload), _require_token(payload)
            )
        raise
    # The outcome comes from actual store state, never from caller flags.
    inspection = _inspect(lane, canonical)
    outcome, reason, next_check = _settle_job(state, lease, inspection, now)
    return {
        "status": "finished",
        "outcome": outcome,
        "reason": reason,
        "next_check_at": next_check,
    }


def op_release(state: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    job_id = _require_job_id(payload)
    owner = _require_owner(payload)
    token = _require_token(payload)
    lease = state.get("lease")
    if not isinstance(lease, dict) or str(lease.get("job_id")) != job_id:
        # Idempotent for an already-finished or already-released slot, but
        # only for the recorded owner/token; an error otherwise so a wrong
        # job id or a stranger's replay never looks like a release.
        slot = _finished_slot(state, job_id)
        if slot is None:
            raise AutomationError("unknown job")
        _check_finished_owner(slot, owner, token)
        return {"status": "released", "job_id": job_id, "idempotent": True}
    _check_owner_token(lease, owner, token)
    state["lease"] = None
    state["last_released"] = {
        "job_id": job_id,
        "released_at": _iso(_now()),
        "owner": owner,
        "token_sha256": _token_digest(token),
    }
    return {"status": "released", "job_id": job_id}


# ---- entry point --------------------------------------------------------------


def _disabled_result() -> Dict[str, Any]:
    return {
        "status": "disabled",
        "reason": f"automation disabled via {ENV_AUTOMATION}=0",
    }


def run_op(
    payload: Dict[str, Any], store: str, project_root: str
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise AutomationError("packet must be a JSON object")
    op = payload.get("op")
    if op not in OPS:
        raise AutomationError(f"op must be one of {sorted(OPS)}")
    lane, canonical = _lane(store, project_root)
    now = _now()
    if op == "release":
        with _mutate(lane) as state:
            return op_release(state, payload)
    with _mutate(lane) as state:
        if op == "status":
            return op_status(lane, canonical, state, now)
        if op == "seed":
            return op_seed(state, payload, lane, canonical, now)
        if op == "claim":
            return op_claim(state, payload, lane, canonical, now)
        if op == "heartbeat":
            return op_heartbeat(state, payload, lane, canonical, now)
        if op == "finish":
            return op_finish(state, payload, lane, canonical, now)
    raise AutomationError(f"op must be one of {sorted(OPS)}")  # pragma: no cover


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Coordinate OMP maintenance automation for one project lane."
    )
    parser.add_argument("--store", default="", help="Absolute shared ontology root.")
    parser.add_argument(
        "--project-root", default="", help="Absolute project root (defaults to cwd)."
    )
    args = parser.parse_args(argv)
    try:
        env_store = os.environ.get(ENV_STORE, "")
        store = _resolve_store(args.store or env_store)
        project_root = _resolve_project_root(args.project_root)
    except AutomationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    disabled = _automation_disabled()
    try:
        raw = sys.stdin.read()
    except OSError as exc:
        if not disabled:
            print(json.dumps({"status": "error", "error": f"cannot read stdin: {exc}"}))
            return 2
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        payload = None
        parse_error = f"packet is not valid JSON: {exc}"
    else:
        parse_error = ""
    # Opt-out stops new automation, but an owner-authenticated release/finish
    # still runs so disabling never strands a live lease.
    if disabled and (
        not isinstance(payload, dict) or payload.get("op") not in ("release", "finish")
    ):
        print(json.dumps(_disabled_result()))
        return 0
    if not isinstance(payload, dict):
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": parse_error or "packet must be a JSON object",
                }
            )
        )
        return 2
    try:
        result = run_op(payload, store, project_root)
    except AutomationBusy as exc:
        print(json.dumps({"status": "busy", "reason": str(exc)}))
        return 0
    except AutomationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    except OSError as exc:
        print(json.dumps({"status": "error", "error": f"storage failure: {exc}"}))
        return 1
    except Exception as exc:  # never leak internals; no secrets are logged
        print(
            json.dumps(
                {"status": "error", "error": f"storage failure: {type(exc).__name__}"}
            )
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
