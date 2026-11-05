# Traceloom

Self-hosted LLM-engineering platform: ingest traces from LLM apps/agents, debug them, track cost, manage prompts, evaluate quality. Original product — never copy Langfuse code, text, logos or styling. Product brief and phased roadmap: `prompt.md`.

## Commands
- Setup (install deps): `python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' -e ./sdk/python`
- Build: `.venv/bin/python -m compileall -q traceloom sdk/python/traceloom_sdk`
- Test: `.venv/bin/pytest -q`
- Lint: `.venv/bin/ruff check .`
- Run (dev): `.venv/bin/uvicorn traceloom.main:app --port 8000`

## Verifying your work
- Run `python .sdlc/bin/sdlc.py verify` (setup → build → test → lint) before reporting any task complete, and paste the output.
- New dependency: add it to the manifest (requirements.txt / pyproject.toml / package.json) with a version constraint, then run verify — the setup step installs it. Never pip/npm install ad hoc.
- If a test fails, fix the code, not the test. Never skip, delete or loosen a failing test.
- For bug fixes: reproduce the bug as a failing test first (`/sdlc:bugfix`).

## Conventions
- Python 3.12+, FastAPI, SQLAlchemy 2.x, server-rendered pages + HTMX. Ruff for lint and format.
- Nothing hardcoded: config comes from env (see `.env.example`) or the settings UI.
- Every query is project-scoped and permission-checked. User content renders escaped, never as raw HTML.
- Storage goes through the `TraceStore` / `BlobStore` ports; lite mode (Postgres/SQLite-only, in-process queue, local folder) is the default in tests.

## Architecture
<!-- Filled in as the code lands: top-level folders and what lives where. -->
- `traceloom/` — web + API server and worker. `sdk/python/` — Python SDK. `sdlc/changes/` — change artifacts.

## How work flows here (AI-native SDLC)
- Every change lives in `sdlc/changes/<date>-<slug>/`: intent.md -> spec.md -> plan.md -> code + tests -> review.md.
- Nothing is implemented without an accepted plan.md. When the implementation departs from the plan, update plan.md in the same commit.
- Governance files (`.sdlc/config.json`, `.sdlc/state/`, `.sdlc/bin/`) are human-owned; hooks block edits to them.
- The agent never pushes to a protected branch and never runs a production action without a release authorization.

## Things Claude gets wrong
<!-- One line per repeated mistake. -->
