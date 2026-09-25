# EvoOntology Project Context

## Purpose

Project Context stores persistent project-level information shared by Build,
Runtime, and Evolution.

It is created during the initial build and reused by later evolution runs.

The context is stored at the selected project lane's `project.json`:

```text
OMP: <shared root + project_root>/project.json (internal lane under /home/adam/.omp/ontologies/shared/projects/<hash>/project.json; never address directly — pass shared root as workspace plus the current canonical project_root and let the server route)
Codex: <project-root>/.evoontology/project.json
```

When running under OMP, this installation uses one user-global external workspace by default. A different
workspace must be explicitly selected by the user. The server routes the shared root + current canonical `project_root` (OMP ctx.cwd / repo root) internally to one project lane under `shared/projects/<hash>`; the lane path is an implementation detail. The context describes the
authorized data environment, not whichever repository happens to be current.
Preserve project/data-source provenance and do not overwrite another lane's context on a cwd change. An empty lane (no `project.json`) is valid uninitialized state. Legacy flat files at the shared root are reused only when their persisted `project_root` or `data_source` unambiguously matches the current canonical `project_root`.

When running under Codex, the context is project-local under the current project's `.evoontology/` directory.

---

## Fields

```json
{
  "version": 1,
  "mode": "fixed_split | rolling_trajectory",
  "project_root": "...",
  "data_source": "...",
  "workload_source": "...",
  "evaluation": {},
  "boundary": {}
}
```

### `mode`

Supported values:

* `fixed_split`
* `rolling_trajectory`

### `project_root`

Canonical absolute repository root used as routing identity under OMP. Persisted separately from the actual data environment by `configure_ontology_project`; never conflate the two. Under Codex this field is absent and the project-local `.evoontology/` path already scopes the context.

### `data_source`

Location or identifier of the target data environment.

### `workload_source`

For `fixed_split`, the predefined question/workload source.

For `rolling_trajectory`, the seed workload used for initial construction.

### `evaluation`

Fixed-Split example:

```json
{
  "type": "external_evaluator",
  "adapter": "bird",
  "ground_truth_source": "..."
}
```

Rolling-Trajectory example:

```json
{
  "type": "llm_judge"
}
```

Ground Truth may be registered for Evaluator use but MUST NOT be read by
Builder or Evolver.

### `boundary`

Fixed-Split example:

```json
{
  "direction": "A_to_B",
  "evolution_split": "...",
  "validation_split": "...",
  "heldout_split": "..."
}
```

Rolling-Trajectory example:

```json
{
  "strategy": "rolling_trajectory"
}
```

Per-evolution Evolution Pool and Validation Reserve assignments belong to the
evolution record, not `project.json`.

---

## Usage

Build MUST establish and persist Project Context before semantic construction. Under OMP the context, `active.json`, `versions/`, `trajectories/`, and `evolution/` are all scoped to the selected project lane; a missing lane context means uninitialized for this project, never a cue to read another lane or the legacy flat root.

Evolve MUST reuse the persisted mode, data sources, and evaluation setup rather
than infer them again.

Project Context does not store:

* active ontology version;
* Task trajectories;
* evolution checkpoints;
* Candidate history.
