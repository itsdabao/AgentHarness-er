# Minder Agent Harness

Python 3.13 project foundation for the Celesnity AI Track Agent Harness.

The implementation will remain a small, bounded runtime. The stable
architecture and phase scope are documented in [docs/project-brief.md](docs/project-brief.md)
and [docs/phases](docs/phases).

## Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/)

The repository pins Python 3.13 in .python-version.

## Setup

~~~powershell
uv sync --locked
~~~

Use `uv sync` only when intentionally updating dependency resolution and
regenerating `uv.lock`.

## Checks

Run the deterministic, offline test suite:

~~~powershell
uv run pytest
~~~

Run formatting, linting, and type checking:

~~~powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy
~~~

Phase 0 intentionally contains no Agent Loop, MCP server, persistence,
RPC transport, or real-provider implementation. Those are introduced in
later phases according to the phase JSON files.
