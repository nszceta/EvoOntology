"""Behavior tests for the OMP maintenance coordinator.

Covers the dispatcher contract the extension relies on: readiness per actual
lane state, cross-owner lease serialization, owner/token enforcement without
secret leaks, failure cooldowns with new-seed wake vs same-seed loop guard,
expiry reconciliation from store state, and CLI/validation edges. All state is
per-test under ``tmp_path``; no suites or global installs are touched.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import evoontology
import pytest

from evoontology import EvolutionSession, SemanticStore
from evoontology.omp_automation import (
    AUTOMATION_FILENAME,
    AutomationError,
    run_op,
)
from evoontology.trajectory.trajectory import TrajectoryStore
from evoontology.workspace import resolve_project_workspace

REPO_ROOT = Path(evoontology.__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "evoontology.omp_automation"]

RECORDS = {
    "terms": [{"id": "t1", "name": "net_income", "type": "metric"}],
    "mappings": [
        {"id": "m1", "term_id": "t1", "table": "financials", "column": "net_income"}
    ],
    "relations": [],
    "constraints": [],
    "evidence": [],
}


def _lane(store: Path, proj: Path) -> Path:
    return resolve_project_workspace(str(store), str(proj))


def _seed_version(lane: Path, version: str = "ontology_v1") -> None:
    SemanticStore.save_version(str(lane), version, RECORDS)
    SemanticStore.set_active(str(lane), version)


def _write_project(lane: Path, proj: Path) -> None:
    (lane / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "rolling_trajectory",
                "project_root": str(proj.resolve()),
                "data_source": {"path": "/data/example.sqlite"},
                "workload_source": "trajectories",
                "evaluation": {"protocol": "ground_truth"},
                "boundary": {"scope": "test lane"},
            }
        ),
        encoding="utf-8",
    )


def _add_trajectories(lane: Path, count: int) -> None:
    store = TrajectoryStore(str(lane))
    for index in range(count):
        store.append({"task_id": f"task_{index}", "question": f"q {index}"})


def _due_soon(lane: Path, min_new: int = 1) -> None:
    (lane / "state.json").write_text(
        json.dumps(
            {
                "checkpoint_trajectory": None,
                "checkpoint_time": datetime.now(timezone.utc).isoformat(),
                "evolution_due": False,
                "thresholds": {"min_new_trajectories": min_new, "min_days": 3650},
            }
        ),
        encoding="utf-8",
    )


def _automation(store: Path, proj: Path) -> dict:
    return json.loads((_lane(store, proj) / AUTOMATION_FILENAME).read_text())


def _expire_lease(store: Path, proj: Path) -> None:
    lane = _lane(store, proj)
    path = lane / AUTOMATION_FILENAME
    state = json.loads(path.read_text(encoding="utf-8"))
    state["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(state), encoding="utf-8")


def _run_cli(store: Path, proj: Path, payload: dict, raw: str | None = None):
    body = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [*CLI, "--store", str(store), "--project-root", str(proj)],
        input=body,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "shared"


# ---- readiness ---------------------------------------------------------------


def test_blank_lane_claim_is_idle_without_seed(store, proj):
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "idle"
    assert "seed" in result["reason"]
    status = run_op({"op": "status"}, str(store), str(proj))
    assert status["status"] == "ok"
    assert status["active_version"] is None
    assert status["has_project"] is False
    assert status["seed_present"] is False


def test_seed_never_creates_project_context(store, proj):
    lane = _lane(store, proj)
    result = run_op(
        {"op": "seed", "question": "Cluster nightly failures by service?"},
        str(store),
        str(proj),
    )
    assert result["status"] == "seeded"
    assert not (lane / "project.json").exists()
    assert not (lane / "active.json").exists()
    state = json.loads((lane / AUTOMATION_FILENAME).read_text(encoding="utf-8"))
    assert state["seed"]["provenance"] == {"source": "user"}


def test_seed_then_claim_build_carries_seed(store, proj):
    run_op(
        {"op": "seed", "question": "Cluster nightly failures?", "source_ref": "s/t"},
        str(store),
        str(proj),
    )
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "claimed"
    job = result["job"]
    assert job["kind"] == "build"
    assert job["max_rounds"] == 2
    assert job["timeout_seconds"] == 600
    assert job["seed"]["question"] == "Cluster nightly failures?"
    assert job["seed"]["source_ref"] == "s/t"
    assert job["lease_token"]


def test_version_without_project_is_idle_missing_prerequisites(store, proj):
    _seed_version(_lane(store, proj))
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "idle"
    assert "project" in result["reason"]


def test_evolve_when_due_carries_parent_version(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    _due_soon(lane)
    _add_trajectories(lane, 2)
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "claimed"
    assert result["job"]["kind"] == "evolve"
    assert result["job"]["parent_version"] == "ontology_v1"


def test_quiet_lane_is_idle_not_due(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "idle"
    assert result["reason"] == "evolution is not due"


def test_running_run_claims_resume_with_checkpoint(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    run = EvolutionSession(str(lane)).start_run("ontology_v1", max_rounds=2)
    run_path = lane / "evolution" / run["run_id"] / "run.json"
    record = json.loads(run_path.read_text(encoding="utf-8"))
    record["trajectory_checkpoint"] = "task_7"
    run_path.write_text(json.dumps(record), encoding="utf-8")
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "claimed"
    assert result["job"]["kind"] == "resume"
    assert result["job"]["run_id"] == run["run_id"]
    assert result["job"]["trajectory_checkpoint"] == "task_7"


def test_finish_never_auto_closes_running_run(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    EvolutionSession(str(lane)).start_run("ontology_v1", max_rounds=2)
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    finished = run_op(
        {
            "op": "finish",
            "job_id": job["id"],
            "owner": "sess-1",
            "token": job["lease_token"],
        },
        str(store),
        str(proj),
    )
    assert finished["status"] == "finished"
    assert finished["outcome"] == "cooldown"
    assert EvolutionSession(str(lane)).latest_run()["status"] == "running"


# ---- lease serialization + owner enforcement ---------------------------------


def test_second_owner_gets_busy_without_secret(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    first = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    busy = run_op({"op": "claim", "owner": "sess-2"}, str(store), str(proj))
    assert busy["status"] == "busy"
    assert busy["lease"]["job_id"] == first["job"]["id"]
    assert "token" not in json.dumps(busy)
    assert "lease_token" not in json.dumps(busy)


def test_same_owner_reclaim_returns_same_job(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    first = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    second = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert second["job"]["id"] == first["job"]["id"]
    assert second["job"]["lease_token"] == first["job"]["lease_token"]


def test_heartbeat_rejects_wrong_token_without_leak(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {"op": "heartbeat", "job_id": job["id"], "owner": "sess-1", "token": "wrong"},
            str(store),
            str(proj),
        )
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {
                "op": "heartbeat",
                "job_id": job["id"],
                "owner": "sess-2",
                "token": job["lease_token"],
            },
            str(store),
            str(proj),
        )
    try:
        run_op(
            {"op": "heartbeat", "job_id": job["id"], "owner": "sess-1", "token": "wrong"},
            str(store),
            str(proj),
        )
    except AutomationError as exc:
        assert job["lease_token"] not in str(exc)


def test_heartbeat_extends_lease(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op(
        {"op": "claim", "owner": "sess-1", "lease_seconds": 60}, str(store), str(proj)
    )
    job = claimed["job"]
    beaten = run_op(
        {
            "op": "heartbeat",
            "job_id": job["id"],
            "owner": "sess-1",
            "token": job["lease_token"],
            "lease_seconds": 3600,
        },
        str(store),
        str(proj),
    )
    assert beaten["status"] == "heartbeat"
    assert beaten["expires_at"] >= job["expires_at"]


def test_release_is_idempotent_and_owner_checked(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {"op": "release", "job_id": job["id"], "owner": "sess-2", "token": job["lease_token"]},
            str(store),
            str(proj),
        )
    released = run_op(
        {"op": "release", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert released == {"status": "released", "job_id": job["id"]}
    again = run_op(
        {"op": "release", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert again["status"] == "released"
    assert again["idempotent"] is True


# ---- finish outcomes + cooldown ----------------------------------------------


def test_finish_build_completed_consumes_seed(store, proj):
    lane = _lane(store, proj)
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    _seed_version(lane)
    finished = run_op(
        {"op": "finish", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert finished == {
        "status": "finished",
        "outcome": "completed",
        "reason": finished["reason"],
        "next_check_at": finished["next_check_at"],
    }
    assert "ontology_v1" in finished["reason"]
    assert _automation(store, proj)["seed"] is None
    # Success never repeats: the published version now routes past build.
    followup = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert followup["status"] == "idle"


def test_finish_ignores_caller_outcome_flags(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    finished = run_op(
        {
            "op": "finish",
            "job_id": job["id"],
            "owner": "sess-1",
            "token": job["lease_token"],
            "success": True,
            "outcome": "completed",
        },
        str(store),
        str(proj),
    )
    assert finished["outcome"] == "cooldown"


def test_failed_build_cools_down_and_same_seed_never_loops(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    finished = run_op(
        {"op": "finish", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert finished["outcome"] == "cooldown"
    assert run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))["status"] == "cooldown"
    reseeded = run_op(
        {"op": "seed", "question": "Cluster nightly failures?"},
        str(store),
        str(proj),
    )
    assert reseeded["duplicate"] is True
    assert reseeded["cleared_cooldown"] is False
    assert run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))["status"] == "cooldown"


def test_new_seed_wakes_build_cooldown(store, proj):
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    run_op(
        {"op": "finish", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    woken = run_op(
        {"op": "seed", "question": "Which service owns the checkout latency SLO?"},
        str(store),
        str(proj),
    )
    assert woken["cleared_cooldown"] is True
    reclaim = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert reclaim["status"] == "claimed"
    assert reclaim["job"]["kind"] == "build"
    assert "checkout latency" in reclaim["job"]["seed"]["question"]


def test_expired_build_lease_reconciles_published_version(store, proj):
    lane = _lane(store, proj)
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    _seed_version(lane)  # worker published while the lease lapsed
    _expire_lease(store, proj)
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "idle"  # reconciled as completed, never rebuilt
    assert _automation(store, proj)["last"]["outcome"] == "completed"


def test_expired_unfinished_lease_cools_down_instead_of_rerun(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    _due_soon(lane)
    _add_trajectories(lane, 2)
    run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    _expire_lease(store, proj)
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "cooldown"
    assert _automation(store, proj)["last"]["outcome"] == "cooldown"


# ---- validation, lock recovery, CLI ------------------------------------------


def test_stale_lock_recovers(store, proj):
    lane = _lane(store, proj)
    lane.mkdir(parents=True, exist_ok=True)
    lock = lane / "automation.lock"
    lock.write_text("9:stale", encoding="utf-8")
    old = lock.stat().st_mtime - 600
    os.utime(lock, (old, old))
    # Advisory OS locks live on open handles, never on file mtime: a stale
    # leftover file neither blocks nor is deleted from under a live holder.
    assert run_op({"op": "status"}, str(store), str(proj))["status"] == "ok"
    assert lock.exists()


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"op": "nope"}, "op must be"),
        ({"op": "claim"}, "owner must be"),
        ({"op": "claim", "owner": "s", "lease_seconds": 5}, "lease_seconds"),
        ({"op": "claim", "owner": "s", "lease_seconds": 1.5}, "lease_seconds"),
        ({"op": "seed", "question": "   "}, "non-empty string"),
        ({"op": "seed", "question": "x" * 4001}, "at most"),
        ({"op": "finish", "job_id": "j", "owner": "s"}, "token is required"),
        ({"op": "release", "job_id": "nope", "owner": "s", "token": "t"}, "unknown job"),
    ],
)
def test_input_validation(store, proj, payload, match):
    with pytest.raises(AutomationError, match=match):
        run_op(payload, str(store), str(proj))


def test_relative_store_rejected(proj):
    with pytest.raises(AutomationError, match="absolute path"):
        run_op({"op": "status"}, "relative/store", str(proj))


def test_cli_status_round_trip_is_exact_json(store, proj):
    proc = _run_cli(store, proj, {"op": "status"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert json.loads(proc.stdout)["status"] == "ok"


def test_cli_malformed_input_is_explicit_json(store, proj):
    proc = _run_cli(store, proj, {}, raw="not json")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["status"] == "error"


def test_cli_unknown_job_reports_error(store, proj):
    proc = _run_cli(
        store, proj, {"op": "finish", "job_id": "job_x", "owner": "s", "token": "t"}
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout) == {"status": "error", "error": "unknown job"}


def test_disabled_env_short_circuits(store, proj, monkeypatch, capsys):
    monkeypatch.setenv("EVO_ONTOLOGY_AUTOMATION", "0")
    from evoontology.omp_automation import main

    assert main(["--store", str(store), "--project-root", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"


def test_seed_prompt_is_scrubbed_and_bounded(store, proj):
    from evoontology.trajectory.omp_capture import redact_sensitive, truncate_text

    question = "Summarize checkout latency by service. " + "d=1 " * 900
    assert len(question) <= 4000
    result = run_op({"op": "seed", "question": question}, str(store), str(proj))
    assert result["status"] == "seeded"
    expected = truncate_text(redact_sensitive(question.strip()), 4000)
    stored = _automation(store, proj)["seed"]
    assert stored["question"] == expected
    assert len(stored["question"]) <= 4000 + 64
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert claimed["job"]["seed"]["question"] == expected


def _patch_run(lane: Path, run_id: str, **fields) -> None:
    path = lane / "evolution" / run_id / "run.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_stale_accepted_run_without_execution_is_cooldown(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    _due_soon(lane)
    _add_trajectories(lane, 2)
    run = EvolutionSession(str(lane)).start_run("ontology_v1", max_rounds=2)
    # A prior run already accepted before claim, active version untouched.
    _patch_run(lane, run["run_id"], status="accepted", accepted_version="ontology_v2")
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert claimed["status"] == "claimed"
    assert claimed["job"]["kind"] == "evolve"
    job = claimed["job"]
    finished = run_op(
        {"op": "finish", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert finished["outcome"] == "cooldown"
    assert "no new accepted run" in finished["reason"]


def test_resume_other_accepted_run_is_not_success(store, proj):
    lane = _lane(store, proj)
    _seed_version(lane)
    _write_project(lane, proj)
    EvolutionSession(str(lane)).start_run("ontology_v1", max_rounds=2)
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert claimed["job"]["kind"] == "resume"
    job = claimed["job"]
    EvolutionSession(str(lane)).mark_incomplete("user_interrupted")
    other = EvolutionSession(str(lane)).start_run("ontology_v1", max_rounds=2)
    _patch_run(lane, other["run_id"], status="accepted", accepted_version="ontology_v2")
    finished = run_op(
        {"op": "finish", "job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]},
        str(store),
        str(proj),
    )
    assert finished["outcome"] == "cooldown"


def test_finished_slot_rejects_stranger_replay(store, proj):
    lane = _lane(store, proj)
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    _seed_version(lane)
    creds = {"job_id": job["id"], "owner": "sess-1", "token": job["lease_token"]}
    assert run_op({"op": "finish", **creds}, str(store), str(proj))["outcome"] == "completed"
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {"op": "finish", "job_id": job["id"], "owner": "sess-2", "token": job["lease_token"]},
            str(store),
            str(proj),
        )
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {"op": "release", "job_id": job["id"], "owner": "sess-1", "token": "wrong"},
            str(store),
            str(proj),
        )
    with pytest.raises(AutomationError, match="owner/token mismatch"):
        run_op(
            {"op": "heartbeat", "job_id": job["id"], "owner": "sess-1", "token": "wrong"},
            str(store),
            str(proj),
        )
    with pytest.raises(AutomationError, match="job already finished"):
        run_op({"op": "finish", **creds}, str(store), str(proj))


def test_disabled_release_succeeds(store, proj, monkeypatch, capsys):
    import io

    from evoontology.omp_automation import main

    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    claimed = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    job = claimed["job"]
    monkeypatch.setenv("EVO_ONTOLOGY_AUTOMATION", "0")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "op": "release",
                    "job_id": job["id"],
                    "owner": "sess-1",
                    "token": job["lease_token"],
                }
            )
        ),
    )
    assert main(["--store", str(store), "--project-root", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "released",
        "job_id": job["id"],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"op": "status"})))
    assert main(["--store", str(store), "--project-root", str(proj)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"


def test_invalid_active_blocks_seed_build(store, proj):
    lane = _lane(store, proj)
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "active.json").write_text(
        json.dumps({"active_version": "ontology_v9"}), encoding="utf-8"
    )
    run_op({"op": "seed", "question": "Cluster nightly failures?"}, str(store), str(proj))
    result = run_op({"op": "claim", "owner": "sess-1"}, str(store), str(proj))
    assert result["status"] == "idle"
    assert "blocked" in result["reason"]
    assert "active.json is invalid" in result["reason"]
    status = run_op({"op": "status"}, str(store), str(proj))
    assert status["active_version"] is None
    assert status["active_blocker"] is not None
    assert not (lane / "versions").exists()
    assert not (lane / "project.json").exists()
