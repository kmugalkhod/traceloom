"""Shared helpers for the AI-Native SDLC kit: config, glob matching, audit log, change discovery.

Used by the plugin hooks and by the repo-local CLI in .sdlc/bin/. Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

# Windows consoles default to cp1252, which cannot print the arrows and dashes these tools use
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timezone
from pathlib import Path

CONFIG_REL = ".sdlc/config.json"

DEFAULTS: dict = {
    "changes_dir": "sdlc/changes",
    "lessons_dir": "sdlc/lessons",
    "protected_paths": [],
    "test_globs": [
        "tests/**", "test/**", "**/__tests__/**",
        "**/test_*.py", "**/*_test.py", "**/*_test.go",
        "**/*.test.*", "**/*.spec.*", "**/src/test/**", "**/itest/**",
    ],
    "verify": {"setup": "", "build": "", "test": "", "lint": ""},
    "format_cmd": "",
    "lint_on_edit_cmd": "",
    "prod_gate": {
        "patterns": [r"\bdeploy\b.*\bprod(uction)?\b", r"\bhelm\b.*\b(upgrade|install)\b.*\bprod", r"\bkubectl\b.*\b(apply|delete|rollout)\b.*\bprod"],
        "approval_env": "RELEASE_APPROVAL",
        "approval_ttl_minutes": 120,
    },
    "protected_branches": ["main", "master"],
    "secret_scan": True,
    "secret_scan_exclude": [],
    "tracker": {"type": "internal", "url_template": "",
                "jira": {"base_url": "", "project_key": ""}, "linear": {"team_key": ""}},
    "vcs": "auto",
    "models": {
        "spec": "sonnet", "plan": "sonnet", "build": "sonnet",
        "review": "sonnet", "gate": "sonnet", "triage": "sonnet", "evals": "sonnet",
    },
    "pipeline": {"commit": True, "branch_prefix": "sdlc/", "open_pr": False, "max_fix_rounds": 3,
                 "allowed_bash": []},
    "evals": {"dir": "evals/cases", "threshold": 0.9},
    "team": {
        "developers": 2, "qa_agents": 1,
        "models": {"planner": "sonnet", "developer": "sonnet", "qa": "sonnet", "helper": "sonnet",
                   "designer": "sonnet"},
        "max_qa_rounds": 3, "max_dev_attempts": 2, "max_help_rounds": 2, "run_timeout_minutes": 40,
        "stall_minutes": 12,
        "branch_prefix": "team/", "worktree_dir": ".sdlc/worktrees",
        "allowed_bash": [],
        "qa": {"start_cmd": "", "health_path": "/", "port_base": 8600, "start_timeout": 60,
               "browser": "playwright-mcp", "browser_executable": "", "browser_args": [],
               "minutes_per_task": 4},
        "design": {"mode": "auto", "approval": "human", "max_rounds": 3, "approval_timeout_minutes": 240,
                   "plugin_dirs": [], "design_port": 8690,
                   "ui_globs": ["**/*.html", "**/*.css", "**/*.scss", "**/*.jsx", "**/*.tsx", "**/*.vue",
                                "**/*.svelte", "**/static/**", "**/templates/**", "**/components/**",
                                "**/pages/**", "**/app/**/page.*", "**/ui/**"]},
        "poll_seconds": 3, "sync_seconds": 10, "board_port": 8765,
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(start: str | os.PathLike | None = None) -> Path | None:
    """Walk up from start (or cwd) to the first directory holding .sdlc/config.json."""
    p = Path(start or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CONFIG_REL).is_file():
            return d
    return None


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(root: Path) -> dict:
    try:
        user = json.loads((root / CONFIG_REL).read_text(encoding="utf-8"))
    except Exception as e:  # broken config must not silently disable gates
        print(f"sdlc: cannot parse {CONFIG_REL}: {e}; using defaults", file=sys.stderr)
        user = {}
    return _merge(DEFAULTS, user)


def glob_to_regex(pattern: str) -> re.Pattern:
    """Gitignore-ish glob: ** spans directories, * and ? stay within a segment."""
    i, out = 0, ""
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pattern.startswith("**", i):
            out += ".*"; i += 2; continue
        out += {"*": "[^/]*", "?": "[^/]"}.get(c, re.escape(c)); i += 1
    return re.compile("^" + out + "$")


def matches_any(rel_path: str, patterns: list[str]) -> str | None:
    rel = rel_path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    for pat in patterns or []:
        if glob_to_regex(pat).match(rel):
            return pat
    return None


def rel_to_root(root: Path, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()  # outside the repo


def audit(root: Path, event: str, **fields) -> None:
    """Append one JSON line to .sdlc/audit.jsonl (who/what/when for gate decisions)."""
    try:
        line = {"ts": utcnow(), "event": event, "user": os.environ.get("USER") or os.environ.get("USERNAME"), **fields}
        with open(root / ".sdlc" / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def state_path(root: Path, name: str) -> Path:
    return root / ".sdlc" / "state" / name


def read_state(root: Path, name: str) -> dict | None:
    p = state_path(root, name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    except Exception:
        return None


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout.strip()
    except Exception:
        return ""


def changes(root: Path, cfg: dict) -> list[Path]:
    d = root / cfg["changes_dir"]
    return sorted([p for p in d.iterdir() if p.is_dir()]) if d.is_dir() else []


def header_field(md_path: Path, field: str) -> str | None:
    """Read 'Field: value' from the first 15 lines of an artifact (e.g. Status: accepted)."""
    if not md_path.is_file():
        return None
    for line in md_path.read_text(encoding="utf-8", errors="ignore").splitlines()[:15]:
        m = re.search(rf"\b{re.escape(field)}:\s*[*`]*([A-Za-z0-9][\w-]*)", line, re.I)
        if m:
            return m.group(1).lower()
    return None


def stage_of(change_dir: Path) -> str:
    """Infer where a change sits in the loop from which artifacts exist and their Status lines."""
    i, s, p = (change_dir / f for f in ("intent.md", "spec.md", "plan.md"))
    if (change_dir / "review.md").is_file():
        return "review"
    if p.is_file():
        st = header_field(p, "Status") or "draft"
        return "build" if st in ("accepted", "approved", "in-progress", "implemented") else "plan"
    if s.is_file():
        st = header_field(s, "Status") or "draft"
        return "plan-ready" if st in ("accepted", "approved") else "design"
    if i.is_file():
        st = header_field(i, "Status") or "draft"
        if st == "rejected":
            return "rejected"
        return "design-ready" if st in ("accepted", "approved") else "intent"
    return "empty"


MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.cfg", "setup.py", "Pipfile.lock",
             "poetry.lock", "uv.lock", "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")


def setup_signature(root: Path, cmd: str) -> str:
    """Hash of every dependency manifest plus whether the installed envs exist. Empty when nothing to compare."""
    h, seen = hashlib.sha1(cmd.encode()), 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "venv", "node_modules", ".sdlc", "dist", "build",
                                                "__pycache__", ".pytest_cache")]
        if len(Path(dirpath).relative_to(root).parts) > 3:
            dirs[:] = []
            continue
        for f in sorted(files):
            if f in MANIFESTS:
                fp = Path(dirpath) / f
                h.update(fp.relative_to(root).as_posix().encode())
                h.update(fp.read_bytes())
                seen += 1
                if f == "package.json":
                    h.update(b"nm" if (Path(dirpath) / "node_modules").is_dir() else b"no-nm")
    for venv in (".venv", "venv"):
        if venv in cmd:
            h.update(b"venv" if (root / venv).is_dir() else b"no-venv")
    return h.hexdigest() if seen else ""


_NPM_PREFIX_INSTALL = re.compile(
    r"npm\s+(?:--prefix[ =]|-C\s+)([^\s&|;]+)\s+((?:install|i|ci)\b[^&|;]*)"     # npm --prefix DIR install ...
    r"|npm\s+((?:install|i|ci)\b[^&|;]*?)\s+--prefix[ =]([^\s&|;]+)([^&|;]*)")       # npm install ... --prefix DIR


def portable_cmd(cmd: str) -> str:
    """Make a configured command work on this OS without the user editing config.
    - `npm --prefix DIR install` is unreliable (on Windows npm looks for package.json in the current folder):
      run it inside DIR instead, with pushd/popd so later commands still start from the repo root (bash and cmd).
    - Windows virtualenvs keep executables in .venv\\Scripts, not .venv/bin."""
    def fix(m: re.Match) -> str:
        if m.group(1):
            d, rest = m.group(1), m.group(2).strip()
        else:
            d, rest = m.group(4), (m.group(3) + (m.group(5) or "")).strip()
        tail = " " if m.group(0)[-1:].isspace() else ""
        if os.name == "nt":  # cmd.exe has no subshell; pushd/popd puts later commands back at the repo root
            return f"pushd {d} && npm {rest} && popd{tail}"
        return f"(cd {d} && npm {rest}){tail}"
    cmd = _NPM_PREFIX_INSTALL.sub(fix, cmd)
    if os.name == "nt":
        cmd = re.sub(r"(\.?venv)/bin/", r"\1\\Scripts\\", cmd)
    return cmd
