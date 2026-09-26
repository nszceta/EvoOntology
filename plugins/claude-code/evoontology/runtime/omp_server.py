"""OMP-native stdio MCP server for the shared EvoOntology workspace.

Thin wrapper over :mod:`evoontology.runtime.mcp_server`: identical JSON-RPC
transport and tool set, but with OMP-native defaults — the shared store
defaults to the current user's ``~/.omp/ontologies/shared`` (overridable via
``--store`` or an absolute ``EVO_ONTOLOGY_STORE``) and project-aware lane
routing is always on, with the launch cwd as the default ``project_root``.

The original ``mcp_server.main`` behaviour is unchanged; Codex and other
non-OMP hosts keep using it directly.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .mcp_server import SemanticMCPServer, force_utf8_stdio

STORE_ENV_VAR = "EVO_ONTOLOGY_STORE"


def default_shared_store() -> Path:
    """Return the OMP shared store: ``EVO_ONTOLOGY_STORE`` when absolute."""
    override = os.environ.get(STORE_ENV_VAR, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    return (Path.home() / ".omp" / "ontologies" / "shared").resolve()


def resolve_store(explicit: str | None) -> Path:
    """Resolve absolute ``EVO_ONTOLOGY_STORE`` > ``--store`` > HOME default.

    ``--store`` carries the installer's baked-in default; an explicit runtime
    environment always wins over it so a later env override is never shadowed
    by the install-time value.
    """
    override = os.environ.get(STORE_ENV_VAR, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    if explicit and explicit.strip():
        return Path(explicit.strip()).expanduser().resolve()
    return default_shared_store()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        default=None,
        help=(
            "Shared workspace root: the installer's baked-in default. An absolute "
            "$EVO_ONTOLOGY_STORE in the server process environment wins over this "
            "value at runtime; otherwise ~/.omp/ontologies/shared for the current user."
        ),
    )
    parser.add_argument(
        "--version",
        default="",
        help="Explicit semantic version to serve (default: active version)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    override = os.environ.get(STORE_ENV_VAR, "").strip()
    if override and not Path(override).expanduser().is_absolute():
        print(
            f"error: {STORE_ENV_VAR} must be an absolute path, got {override!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    store = resolve_store(args.store)
    default_root = str(Path.cwd().resolve())
    SemanticMCPServer(
        str(store),
        version=args.version,
        project_aware=True,
        default_project_root=default_root,
    ).run()


if __name__ == "__main__":
    main()
