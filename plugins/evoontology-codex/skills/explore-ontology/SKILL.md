---
name: explore-ontology
description: Explore ontology questions, grounded concepts, evidence, actual task results and evolution outcomes in a read-only interactive version explorer.
---

# Explore Ontology

Render all ontology versions as one standalone, offline, single-file interactive
HTML explorer (Results & questions / Content / Schema / Tool views), with in-page version switching
and side-by-side comparison across all three layers. Difference highlighting can
be toggled without changing the compared versions. The operation is strictly
read-only.

## Workflow

1. Resolve the workspace input. When running under OMP: default to the user-global workspace `/home/adam/.omp/ontologies/shared`; pass that absolute path as `workspace` and do not derive it from the current project. Also pass the current canonical `project_root` (OMP ctx.cwd / repo root, absolute resolved path) for project-aware routing — the server renders the selected project lane under `shared/projects/<hash>`; the lane path is an implementation detail, never pass it as `workspace`, never construct, guess, or list it. A server launched with `--project-aware` binds its launch cwd as the default `project_root`; an explicit per-call `project_root` always overrides it, so always send the current project explicitly. Use another workspace only when the user explicitly requests it. When running under Codex: default is the current project's
   `.evoontology/`; use an explicit path when the user provides one. Under Codex the Core
   accepts an exact workspace, a `.evoontology` container, or a project root.
   It uses the requested version to discover one matching nested workspace at
   any depth. If multiple workspaces match, report the candidates and ask for
   the exact path instead of guessing. Under OMP, shared root + `project_root` selects exactly one lane; do not use nested-workspace discovery across lanes.
2. Resolve the initially shown version: default `active` (the version referenced
   by `active.json` in the selected project lane under OMP). An explicit `ontology_vN` changes only the initial page
   selection and never changes `active.json`; every available version in that lane is embedded. An empty lane (no active version) is a valid empty state, not an error — never fabricate content to fill it.
3. Call the `evo-semantic` MCP tool `visualize_ontology` (the single rendering
   entry point), passing the absolute workspace input plus the current canonical `project_root` under OMP, and optionally `version`
   and `open_browser`. Do not run
   `python -m evoontology.visualization`.

4. When running under OMP: call visualize_ontology with `workspace` as the shared root plus the current canonical `project_root` and open_browser:false. Return the generated HTML path, or inspect it with the available OMP browser tools when visualization is requested. Do not call Codex-only UI tools or claim a browser opened without evidence. When running under Codex: in Codex desktop call visualize_ontology with presentation:"codex" and
   open_browser:false. The tool returns html_path and a loopback browser_url.
   Open browser_url with the available Codex open_in_codex tool using
   target:{type:"browser",url:browser_url}, placement:"right". Reuse the preview
   tab when available. The MCP tool does not open an external browser in this mode.
   If the app browser tool is unavailable, return the URL and HTML path explicitly;
   do not claim the page opened. For an explicitly requested external browser use
   presentation:"external". No browser is opened twice.

## Boundaries

- Do not implement HTML generation inside this skill; all rendering lives in
  EvoOntology Core (`evoontology.visualization`).
- Never modify Build, Evolve, Runtime, `active.json`, `versions/`, or any
  other ontology/evolution state.
- Errors stay explicit: selected project lane not initialized, no active version in the lane, the
  requested version does not exist, or (Codex nested discovery only) multiple workspaces match.
  Broken references only produce warnings; graph objects are never fabricated.
- Content edges follow the semantic model: solid Semantic Relations connect
  Terms, while dotted Structural References attach Mapping, Constraint, and
  Evidence records according to schema reference rules.

The Results & questions page distinguishes semantic coverage, observed execution
and independent evaluation. Click a question to highlight its associated semantic
objects. Terminal run summaries show provenance-labelled metrics and public task
replays; no reserved validation cases are exposed. Missing reports or executions
remain explicit empty states. Never fabricate examples to fill the page.
