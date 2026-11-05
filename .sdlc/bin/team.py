#!/usr/bin/env python3
"""The agent team: break an accepted change into Epic → Stories → Tasks, then run a team of developer and
QA agents on it like a real squad.

  python .sdlc/bin/team.py plan <change>                  planner agent writes breakdown.json → board (+ Jira/Linear)
  python .sdlc/bin/team.py run <change> [--developers 2] [--qa 1] [--pr] [--simulate]
  python .sdlc/bin/team.py status
  python .sdlc/bin/team.py reset                          wipe the board (human, terminal only)

How a run works
  • The lead (this script) owns the board and never writes code. It assigns tasks, respects dependencies, keeps
    two developers off the same files, and mirrors everything into Jira/Linear when configured (hybrid sync).
  • Every developer agent has its own git worktree (.sdlc/worktrees/dev-N) and a branch per task
    (team/<change>/T-3). It implements, runs verify until green, commits, and runs `board.py submit`.
  • A QA agent takes the hand-off in its own worktree, starts the app (team.qa.start_cmd), tests every acceptance
    criterion in a real browser (Playwright MCP) and through the API, adds automated tests, then `qa-pass` or
    `qa-fail` with a bug report. A failed task goes straight back to its developer with the report.
  • Agents talk through the board: `board.py ask/reply/inbox`. A developer who runs `board.py blocked` gets an
    idle teammate sent to read their worktree and answer; after max_help_rounds the task escalates to a human.
  • QA-passed work is merged into the integration branch team/<change>. A merge conflict goes back to the developer.
    The lead never pushes to main; --pr pushes the integration branch and opens a PR/MR for a human.
Everything is visible live in the board page: python .sdlc/bin/board_server.py
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import functools
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board import Board, STATUS_LABEL  # noqa: E402
from sdlc_core import (audit, changes, find_root, git, header_field, load_config, matches_any,  # noqa: E402
                       portable_cmd, setup_signature, utcnow)
import trackers  # noqa: E402

BOARD_CLI = "python3 .sdlc/bin/board.py"
BASE_TOOLS = ["Read", "Grep", "Glob"]
EDIT_TOOLS = ["Edit", "Write", "MultiEdit"]
SAFE_BASH = ["Bash(python3 .sdlc/bin/*)", "Bash(python .sdlc/bin/*)", "Bash(git status*)", "Bash(git diff*)",
             "Bash(git log*)", "Bash(git show*)", "Bash(git add *)", "Bash(git commit *)", "Bash(ls*)",
             "Bash(cat *)", "Bash(mkdir *)", "Bash(pwd)"]
DEV_BASH = ["Bash(git merge *)", "Bash(git checkout -- *)", "Bash(.venv/bin/*)", "Bash(python3 -m pytest*)", "Bash(python -m pytest*)", "Bash(.venv/Scripts/*)",
            "Bash(npm *)", "Bash(npx *)", "Bash(node *)"]
QA_BASH = ["Bash(curl *)", "Bash(.venv/bin/*)", "Bash(python3 -m pytest*)", "Bash(python -m pytest*)", "Bash(.venv/Scripts/*)", "Bash(npx playwright*)"]


def log(msg: str) -> None:
    out = sys.stderr if os.environ.get("SDLC_LOG_STDERR") else sys.stdout
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=out, flush=True)


def sh(cmd, cwd, timeout=600, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)


NEW_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})


def kill_tree(proc) -> None:
    """Kill a process and everything it started. On Windows terminate() leaves the children (npm, node, uvicorn)
    running, still holding file locks and ports."""
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as so:
        so.settimeout(0.5)
        return so.connect_ex(("127.0.0.1", port)) != 0


def resolve_change(root: Path, cfg: dict, name: str) -> Path:
    hits = [c for c in changes(root, cfg) if name in c.name]
    if len(hits) != 1:
        sys.exit(f"change '{name}' matched {len(hits)} folders: {[h.name for h in hits]}")
    return hits[0]


def extract_json(text: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ====================================================================== claude runner
class AgentRun:
    """One headless Claude session. Streams tool use into the board so the kanban shows live activity."""

    def __init__(self, board: Board, name: str, cwd: Path, prompt: str, model: str, tools: list[str],
                 item_key: str | None, timeout_min: int, mcp: dict | None = None, extra_env: dict | None = None,
                 add_dirs: list[str] | None = None, plugin_dir: str | list | None = None, stall_min: float = 12):
        self.b, self.name, self.cwd, self.item_key = board, name, cwd, item_key
        self.prompt = prompt
        self.cmd = ["claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
                    "--permission-mode", "acceptEdits", "--allowedTools", *tools,
                    "--disallowedTools", "Bash(git push*)"]
        if mcp:
            mcp_file = Path(board.path).parent / f"mcp-{name}.json"
            mcp_file.write_text(json.dumps({"mcpServers": mcp}, indent=2), encoding="utf-8")
            self.cmd += ["--mcp-config", str(mcp_file)]
        for d in add_dirs or []:
            self.cmd += ["--add-dir", d]
        for d in ([plugin_dir] if isinstance(plugin_dir, str) else (plugin_dir or [])):
            if d:
                self.cmd += ["--plugin-dir", d]
        self.env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **(extra_env or {})}
        self.timeout = timeout_min * 60
        self.stall_min = float(stall_min)
        self.proc: subprocess.Popen | None = None
        self.result_text, self.cost, self.error = "", 0.0, None

    def describe(self, block: dict) -> str | None:
        n, i = block.get("name", ""), block.get("input") or {}
        if n in ("Edit", "Write", "MultiEdit"):
            return f"{n.lower()} {i.get('file_path', '').split('/worktrees/')[-1].split('/', 1)[-1]}"
        if n == "Bash":
            c = (i.get("command") or "").replace("\n", " ")
            return f"$ {c[:110]}"
        if n.startswith("mcp__playwright__"):
            what = i.get("url") or i.get("element") or i.get("text") or ""
            return f"browser: {n.split('__')[-1].replace('browser_', '')} {str(what)[:80]}".strip()
        if n == "Skill":
            return f"skill: {i.get('skill', '')} {str(i.get('args', ''))[:60]}".strip()
        if n.startswith("mcp__tracker__"):
            return f"tracker: {n.split('__')[-1]}"
        return None

    def run(self) -> None:
        started = last = time.time()
        log_dir = Path(self.b.path).parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        err_path = log_dir / f"{self.name}-{self.item_key or 'x'}-{time.strftime('%Y%m%d-%H%M%S')}.stderr.log"
        stall = int(self.stall_min * 60)
        done = threading.Event()

        def watchdog():  # enforces the limits even when the agent is silent (e.g. a hung npm/pip command)
            while not done.wait(5):
                now = time.time()
                if now - started > self.timeout:
                    self.error = f"timed out after {self.timeout // 60} min"
                elif stall and now - last > stall:
                    self.error = f"stalled: no activity for {stall // 60} min (a command probably hung)"
                else:
                    continue
                kill_tree(self.proc)
                return
        try:
            exe = shutil.which(self.cmd[0]) or self.cmd[0]
            with open(err_path, "w", encoding="utf-8", errors="replace") as err:
                self.proc = subprocess.Popen([exe, *self.cmd[1:]], cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8",
                                             errors="replace", bufsize=1, **NEW_GROUP)
                threading.Thread(target=watchdog, daemon=True).start()
                try:  # the prompt goes in on stdin: no command-line length limit (cmd.exe stops at 8191 chars)
                    self.proc.stdin.write(self.prompt)
                    self.proc.stdin.close()
                except OSError:
                    pass
                for line in self.proc.stdout:
                    last = time.time()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "assistant":
                        for blk in (ev.get("message") or {}).get("content") or []:
                            if blk.get("type") == "tool_use":
                                d = self.describe(blk)
                                if d:
                                    self.b.upsert_agent(self.name, activity=d)
                                    if not d.startswith(("$ python3 .sdlc/bin/board.py", "$ python .sdlc/bin/board.py")):
                                        self.b.event(self.item_key, self.name, "activity", d)
                            elif blk.get("type") == "text" and blk.get("text", "").strip():
                                self.b.upsert_agent(self.name, activity="💭 " + blk["text"].strip()[:140])
                    elif ev.get("type") == "result":
                        self.result_text = ev.get("result") or ""
                        self.cost = ev.get("total_cost_usd") or 0.0
                        if ev.get("is_error"):
                            self.error = (self.result_text or "claude reported an error")[:500]
                try:
                    self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    kill_tree(self.proc)
            if self.proc.returncode and not self.error:
                tail = err_path.read_text(encoding="utf-8", errors="replace")[-500:].strip()
                self.error = tail or f"exit {self.proc.returncode}"
        except Exception as e:  # never let one agent crash the team
            self.error = str(e)[:500]
        finally:
            done.set()
            kill_tree(self.proc)  # leftover children (npm, node, uvicorn, browsers) must not hold locks or ports
            self.b.add_cost(self.name, self.cost)

    def stop(self) -> None:
        kill_tree(self.proc)


class SimulatedRun(AgentRun):
    """--simulate: scripted agents (no model calls) to exercise the board, hand-offs, merges and the kanban."""

    def __init__(self, *a, role: str = "developer", behaviour: str = "", **kw):
        super().__init__(*a, **kw)
        self.role, self.behaviour = role, behaviour

    def run(self) -> None:
        env = {**os.environ, **self.env}
        k = self.item_key
        def board(*args):
            subprocess.run([sys.executable, ".sdlc/bin/board.py", *args], cwd=self.cwd, env=env, capture_output=True)
        steps = 3
        for s in range(steps):
            self.b.upsert_agent(self.name, activity=f"{'coding' if self.role == 'developer' else 'testing'} {k} ({s + 1}/{steps})")
            self.b.event(k, self.name, "activity", f"edit src/{k.lower()}.py" if self.role == "developer"
                         else f"browser: navigate http://localhost/{k.lower()}")
            time.sleep(1.2)
        if self.role == "developer":
            Path(self.cwd, "src").mkdir(exist_ok=True)
            Path(self.cwd, "src", f"{k.lower()}.py").write_text(f"# {k} implemented by {self.name}\n")
            subprocess.run(["git", "add", "-A"], cwd=self.cwd, capture_output=True)
            subprocess.run(["git", "-c", "user.name=sim", "-c", "user.email=s@x", "commit", "-qm", f"{k}: implement"],
                           cwd=self.cwd, capture_output=True)
            if self.behaviour == "block":
                board("blocked", k, "Which date format does the API expect?")
            else:
                board("submit", k, "implemented; verify green")
        elif self.role == "qa":
            if self.behaviour == "fail":
                board("qa-fail", k, "Empty title is accepted; expected a validation error (400).")
            else:
                board("qa-pass", k, "All acceptance criteria checked in the browser; screenshot attached.")
        elif self.role == "helper":
            board("ask", self.behaviour or "developer", "Use ISO 8601 (YYYY-MM-DD); see spec FR-3.", "--task", k)
        elif self.role == "designer":
            d = Path(self.cwd, self.behaviour, "design")
            d.mkdir(parents=True, exist_ok=True)
            Path(self.cwd, "DESIGN.md").write_text("# DESIGN\n\nInk #0e0f12, lime #c6f432, Space Grotesk / Inter.\n")
            (d / "mockup.html").write_text("<!doctype html><title>Mockup</title><body style='font-family:sans-serif;"
                                           "background:#0e0f12;color:#ecedef;padding:40px'><h1>Bookmarks</h1>"
                                           "<p>Simulated mockup: form, list, search, tag chips, empty + error states.</p>")
            (d / "design-notes.md").write_text("# Design notes\n\nSimulated.\n")
            subprocess.run(["git", "add", "-A"], cwd=self.cwd, capture_output=True)
            subprocess.run(["git", "-c", "user.name=sim", "-c", "user.email=s@x", "commit", "-qm", f"{k}: design"],
                           cwd=self.cwd, capture_output=True)
            board("design-ready", k, "Dark ink + lime direction; 5 states per screen; see mockup.html.")


# ====================================================================== planning
PLAN_PROMPT = """You are the planner of an AI-native SDLC agent team. Break the accepted change at {rel} into an
Epic → Stories → Tasks breakdown that {devs} developer agents can build IN PARALLEL in separate git worktrees.

Read {rel}/intent.md, {rel}/spec.md and {rel}/plan.md (whichever exist), CLAUDE.md and the existing code.

Rules:
- One epic for the change. Stories follow the spec's user-facing capabilities (2–6 stories).
- Tasks are 15–30 minutes of agent work each, 2–5 per story. Each task lists the files it will create or change.
  Keep tasks file-disjoint where possible so parallel developers don't collide.
- Plan for parallel work: with {devs} developers, at least {devs} tasks should be startable right after the first
  one. A task depends only on tasks whose code it truly needs.
- Keep chains short: no dependency chain longer than 3 tasks after the skeleton, and no single task that more than a
  third of the tasks wait on. When later work only needs an interface (a provider base class, a DB model, an API
  client), put that interface in the skeleton or an early small task and let the work depend on that, not on the
  full feature. Different stories should be buildable side by side.
- If the project needs a skeleton, keep it SMALL (under 15 minutes): entry point, package layout, dependency
  manifests, config loading, test setup — no features. If there is a backend and a frontend, make them two separate
  skeleton tasks with no dependency between them, and let each later task depend only on the skeleton it builds on.
- depends_on uses task ids from this file. No cycles.
- Each task has testable acceptance criteria and a "qa" note: how a QA agent proves it in a browser or via the API.
  For pure setup tasks with nothing a user can try (skeleton, config, tooling, CI), set "qa": "none" — the lead
  checks those with verify instead of a QA agent.
- Mark "ui": true on every task that creates or changes something a user sees (pages, components, styles, copy),
  and set the top-level "ui" true if the change has any UI. A designer agent designs those before they are built,
  so UI tasks wait for the design approval and everything else starts at once.
- Keep the skeleton and all backend/API/data tasks "ui": false and free of page, template, CSS or static files:
  put the first page/HTML shell in a UI task of its own. Otherwise the whole build waits for the design.

Do not write any files. Reply with ONLY the breakdown as one JSON object in a ```json block, exactly this shape:
{{"epic": {{"title": "...", "description": "..."}}, "ui": true,
  "stories": [{{"id": "S1", "title": "...", "description": "...", "acceptance": ["..."],
               "tasks": [{{"id": "T1", "title": "...", "description": "...", "files": ["..."],
                          "acceptance": ["..."], "qa": "...", "ui": false, "depends_on": []}}]}}]}}
"""


def cmd_plan(root: Path, cfg: dict, a) -> None:
    change = resolve_change(root, cfg, a.change)
    rel = change.relative_to(root).as_posix()
    if (header_field(change / "intent.md", "Status") or "") not in ("accepted", "approved"):
        sys.exit("intent.md must be Status: accepted before the team plans it")
    out = change / "breakdown.json"
    if not out.is_file() or a.replan:
        if a.simulate:
            out.write_text(json.dumps(SIM_BREAKDOWN, indent=2), encoding="utf-8")
        else:
            tcfg = cfg["team"]
            log(f"planner: breaking {change.name} into epic → stories → tasks")
            b = Board()
            b.upsert_agent("planner", role="planner", status="working", activity="planning")
            prompt, data, problem = PLAN_PROMPT.format(rel=rel, devs=a.developers or tcfg["developers"]), None, ""
            for attempt in range(2):  # the lead validates the JSON and writes the file itself
                r = AgentRun(b, "planner", root, prompt + (f"\n\nYour previous answer was rejected: {problem}. "
                             "Reply again with ONLY the corrected JSON." if problem else ""),
                             tcfg["models"]["planner"], BASE_TOOLS, None, 20,
                             plugin_dir=os.environ.get("SDLC_PLUGIN_DIR"))
                r.run()
                data = extract_json(r.result_text)
                problem = validate_breakdown(data) if data else f"no JSON found ({r.error or r.result_text[-200:]})"
                if not problem:
                    break
            b.upsert_agent("planner", status="idle", activity="breakdown ready" if not problem else problem[:140])
            if problem:
                sys.exit(f"planner could not produce a valid breakdown: {problem}")
            out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    data = json.loads(out.read_text(encoding="utf-8"))
    problem = validate_breakdown(data)
    if problem:
        sys.exit(f"{out.name} is not a valid breakdown: {problem}")
    load_breakdown(root, cfg, change, data, max(1, a.developers if a.developers is not None else cfg["team"]["developers"]))
    if not a.simulate and cfg["pipeline"].get("commit", True):
        git(root, "add", str(out))
        git(root, "commit", "-q", "-m", f"breakdown: {change.name}", "-m", "Epic/stories/tasks for the agent team.")
    sync_once(cfg)
    print_tree(Board())


def breakdown_sha(data: dict) -> str:
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def board_untouched(b: Board) -> bool:
    """True while no agent has done any work: nothing past To Do and only the planner has run."""
    work = [i for i in b.items() if i["type"] in ("task", "design")]
    return all(i["status"] == "todo" for i in work) and not any(
        (a["runs"] or 0) > 0 for a in b.agents() if a["name"] != "planner")


def validate_breakdown(d: dict) -> str:
    """Empty string when the breakdown is usable, else what is wrong with it."""
    if not isinstance(d, dict) or not isinstance(d.get("epic"), dict) or not d["epic"].get("title"):
        return "missing epic.title"
    stories = d.get("stories")
    if not isinstance(stories, list) or not stories:
        return "no stories"
    ids = set()
    for s in stories:
        if not s.get("title") or not isinstance(s.get("tasks"), list) or not s["tasks"]:
            return f"story {s.get('id')} needs a title and at least one task"
        for t in s["tasks"]:
            if not t.get("id") or not t.get("title"):
                return "every task needs id and title"
            if t["id"] in ids:
                return f"duplicate task id {t['id']}"
            ids.add(t["id"])
    for s in stories:
        for t in s["tasks"]:
            bad = [x for x in t.get("depends_on", []) if x not in ids]
            if bad:
                return f"task {t['id']} depends on unknown {bad}"
    graph = {t["id"]: t.get("depends_on", []) for s in stories for t in s["tasks"]}
    seen, stack = set(), set()
    def cyc(n):
        if n in stack:
            return True
        if n in seen:
            return False
        seen.add(n); stack.add(n)
        hit = any(cyc(m) for m in graph[n])
        stack.discard(n)
        return hit
    if any(cyc(n) for n in graph):
        return "dependency cycle"
    return ""


def ui_world(root: Path, cfg: dict) -> tuple[str, str]:
    """brownfield when the repo already has a UI (or a DESIGN.md) the new work must match; else greenfield."""
    if (root / "DESIGN.md").is_file():
        return "brownfield", "DESIGN.md exists"
    globs = cfg["team"]["design"]["ui_globs"]
    files = [f for f in git(root, "ls-files").splitlines()
             if not f.startswith(("sdlc/", ".sdlc/", "tests/", "test/", "evals/", ".claude/", ".github/", ".gitlab/"))]
    ui = [f for f in files if matches_any(f, globs)]
    return ("brownfield", f"existing UI files: {', '.join(ui[:5])}") if ui else ("greenfield", "no UI in the repo yet")


def load_breakdown(root: Path, cfg: dict, change: Path, data: dict, devs: int) -> None:
    b = Board()
    sha = breakdown_sha(data)
    if b.items(type_="epic"):
        m = b.meta()
        existing = m.get("change")
        if existing == change.name and m.get("breakdown_sha") == sha:
            log("board already holds this breakdown — keeping it")
            return
        if board_untouched(b):
            # nothing has been worked on yet, so reloading loses nothing — no human reset needed
            log(f"breakdown changed and no work has started — reloading the board"
                + (f" (was {existing})" if existing != change.name else ""))
            b.clear()
        elif existing == change.name and m.get("breakdown_sha") is None:
            b.set_meta(breakdown_sha=sha)  # board from an older kit with work in progress: adopt, keep it
            return
        elif existing == change.name:
            log("breakdown.json changed but agents have already worked on this board — keeping the board. "
                "To start over, a human runs: python .sdlc/bin/team.py reset")
            return
        else:
            sys.exit(f"the board holds another change ({existing}) with work in progress; a human runs team.py reset first")
    b.set_meta(change=change.name, project=root.name, created_at=utcnow(), status="planned", breakdown_sha=sha,
               tracker=(cfg.get("tracker") or {}).get("type", "internal"),
               integration_branch=f"{cfg['team']['branch_prefix']}{change.name}")
    e = b.add_item("epic", data["epic"]["title"], description=data["epic"].get("description", ""))
    idmap, dev_names = {}, [f"dev-{i + 1}" for i in range(devs)]
    pending, no_qa = [], []
    for s in data["stories"]:
        sk = b.add_item("story", s["title"], e, description=s.get("description", ""),
                        acceptance=s.get("acceptance", []))
        for t in s.get("tasks", []):
            desc = t.get("description", "") + (f"\n\nQA: {t['qa']}" if t.get("qa") else "")
            tk = b.add_item("task", t["title"], sk, description=desc, acceptance=t.get("acceptance", []),
                            files=t.get("files", []))
            idmap[t["id"]] = tk
            # the planner's explicit flag wins; file globs only decide when it said nothing
            is_ui = bool(t["ui"]) if "ui" in t else \
                any(matches_any(f, cfg["team"]["design"]["ui_globs"]) for f in t.get("files", []))
            pending.append((tk, t.get("depends_on", []), is_ui))
            if str(t.get("qa", "")).strip().lower() in ("none", "skip", "-", "n/a") and not is_ui:
                no_qa.append(tk)
    # guard: the foundation task must never wait for the design. A "UI" task with no dependencies of its own that
    # non-UI tasks build on directly (typically the skeleton with a placeholder page) starts at once. Real screens
    # (app shell, pages) depend on something, so they keep waiting for the approved design.
    direct = {}
    for s_ in data["stories"]:
        for t in s_.get("tasks", []):
            for d in t.get("depends_on", []):
                if d in idmap:
                    direct.setdefault(idmap[d], []).append(idmap[t["id"]])
    ui_of = {tk: ui for tk, _d, ui in pending}
    freed = [tk for tk, deps, ui in pending
             if ui and not [d for d in deps if d in idmap] and any(not ui_of[c] for c in direct.get(tk, []))]
    pending = [(tk, d, ui and tk not in freed) for tk, d, ui in pending]
    # design: a designer agent designs every UI task before it is built (backend tasks don't wait)
    dmode = cfg["team"]["design"]["mode"]
    ui_tasks = [tk for tk, _d, is_ui in pending if is_ui]
    need = dmode == "always" or (dmode == "auto" and (bool(data.get("ui")) or bool(ui_tasks)))
    design_key = None
    if need:
        world, evidence = ui_world(root, cfg)
        design_key = b.add_item("design", f"Design: {data['epic']['title']}", e,
                                description=f"{world} design ({evidence}). Covers {', '.join(ui_tasks) or 'all UI'}.",
                                acceptance=["DESIGN.md is the standard (created for greenfield, followed for brownfield)",
                                            "mockup.html shows every screen and state the UI tasks need",
                                            "The human approves the design"])
        b.update(design_key, "lead", assignee="designer")
        b.set_meta(design={"key": design_key, "mode": world, "evidence": evidence, "ui_tasks": ui_tasks})
        b.event(design_key, "lead", "assign", f"{world}: designer designs {len(ui_tasks)} UI task(s) first")
        if freed:
            b.event(design_key, "lead", "system", f"{', '.join(freed)}: foundation task that backend work builds on — "
                                                  "starts now instead of waiting for the design")
            log(f"{', '.join(freed)}: foundation task — not waiting for the design")
    # dependencies, then a first assignment that spreads the work (the lead rebalances at run time)
    load = {d: 0 for d in dev_names}
    for tk, deps, is_ui in pending:
        keys = [idmap[d] for d in deps if d in idmap] + ([design_key] if design_key and is_ui else [])
        owner = min(load, key=load.get)
        load[owner] += 1
        b.update(tk, "lead", depends_on=json.dumps(keys), assignee=owner)
        b.event(tk, "lead", "assign", f"assigned to {owner}")
    b.set_meta(no_qa=no_qa)
    b.event(None, "lead", "system", f"plan loaded: {len(data['stories'])} stories, {len(pending)} tasks, "
                                    f"{devs} developers" + (f", design {design_key} first for {len(ui_tasks)} UI task(s)"
                                                            if design_key else ", no UI — no designer needed"))


def print_tree(b: Board) -> None:
    for e in b.items(type_="epic"):
        print(f"\n{e['key']}  {e['title']}  [{STATUS_LABEL.get(e['status'], e['status'])}]  {e.get('ext_url') or ''}")
        for d in b.items(type_="design", parent_key=e["key"]):
            print(f"  {d['key']}  {d['title']}  [{STATUS_LABEL.get(d['status'], d['status'])}]  designer · "
                  f"{(b.meta().get('design') or {}).get('mode', '')}")
        for s in b.items(type_="story", parent_key=e["key"]):
            print(f"  {s['key']}  {s['title']}  [{STATUS_LABEL.get(s['status'], s['status'])}]")
            for t in b.items(parent_key=s["key"]):
                deps = f" after {','.join(t['depends_on'])}" if t["depends_on"] else ""
                print(f"     {t['key']:5} {t['title'][:60]:60} {t['assignee'] or '-':6} "
                      f"{STATUS_LABEL.get(t['status'], t['status'])}{deps}")


def sync_once(cfg: dict) -> None:
    try:
        client = trackers.make(cfg)
    except trackers.TrackerError as e:
        log(f"tracker not configured correctly, internal board only: {e}")
        return
    if client:
        errs = trackers.Sync(Board(), client).run()
        log(f"synced to {client.name}" + (f" ({len(errs)} errors — see board)" if errs else ""))


SIM_BREAKDOWN = {
    "epic": {"title": "Bookmarks manager", "description": "Save, tag and search links."}, "ui": True,
    "stories": [
        {"id": "S1", "title": "Save bookmarks", "acceptance": ["A user can save a URL with a title"],
         "tasks": [{"id": "T1", "title": "Project skeleton and data model", "files": ["src/t-1.py"], "depends_on": []},
                   {"id": "T2", "title": "POST /api/bookmarks", "files": ["src/t-2.py"], "depends_on": ["T1"]},
                   {"id": "T3", "title": "Add-bookmark form", "files": ["src/t-3.py"], "ui": True, "depends_on": ["T1"]}]},
        {"id": "S2", "title": "Find bookmarks", "acceptance": ["Search by title and tag"],
         "tasks": [{"id": "T4", "title": "Search endpoint", "files": ["src/t-4.py"], "depends_on": ["T1"]},
                   {"id": "T5", "title": "Tag filter chips", "files": ["src/t-5.py"], "ui": True, "depends_on": ["T3"]},
                   {"id": "T6", "title": "Date range filter", "files": ["src/t-6.py"], "depends_on": ["T4"]}]},
    ]}


# ====================================================================== the lead
DEV_PROMPT = """You are {agent}, a developer on an AI-native SDLC agent team (teammates: {others}).
Your own git worktree is {wt} on branch {branch}. Work only there; other developers work in parallel.

YOUR TASK {key}: {title}
{description}
Files you own for this task: {files}
Acceptance criteria:
{acceptance}

It belongs to story {story_key} "{story_title}" of epic "{epic_title}". Everything you need is above; look things up
in {rel}/spec.md or {rel}/plan.md only when a detail is missing (search them, don't read them whole). Follow CLAUDE.md.
{extra}
How to work:
1. Check messages first: {cli} inbox  (check again after each major step — teammates may answer you).
2. Implement the task. Scaffolding (a new frontend/backend project, config files) comes from the official
   generators — e.g. `npm create vite@latest frontend -- --template react-ts`, then adjust — instead of typing every
   file yourself. New packages go into the manifest (requirements.txt / package.json) and are installed by
   `python3 .sdlc/bin/sdlc.py verify setup` — never pip/npm install by hand.
3. Write or extend tests for the acceptance criteria. While iterating run only the narrowest check (one test file,
   e.g. `python3 -m pytest tests/test_x.py -q`, or one npm script). Run the full `python3 .sdlc/bin/sdlc.py verify`
   once at the end (it skips reinstalling unchanged dependencies); fix the code, never weaken a test.
   Time budget: about {budget} minutes. Do not loop on tooling problems (npm/pip paths, shells) for more than a few
   tries — if the toolchain itself is broken, `{cli} blocked {key} "what fails"` and stop.
4. Commit on your branch: git add -A && git commit -m "{key}: <what>"
5. Talk to the team when it helps: {cli} ask <dev-N|qa|developer> "question" --task {key}   ·   {cli} comment {key} "progress"
   If you cannot continue without a teammate or a human: {cli} blocked {key} "exactly what you need"  — then stop.
6. When verify is green and everything is committed: {cli} submit {key} "what you built and how QA can test it"  — then stop.
"""

QA_PROMPT = """You are {agent}, the QA engineer on an AI-native SDLC agent team (developers: {devs}).
{dev} handed you task {key}: {title}
{description}
Acceptance criteria:
{acceptance}
Story {story_key} "{story_title}" acceptance: {story_acceptance}

The code is checked out in your own worktree {wt} (branch {branch}).
{app}
Do this:
1. {cli} inbox — then run `python3 .sdlc/bin/sdlc.py verify` (setup, build, tests, lint). If it fails because the
   toolchain itself is broken (pip/npm install errors, missing tools) — not because of the code — do NOT debug or
   work around it: `{cli} blocked {key} "verify setup fails: <the error>"` and stop. A human fixes the config.
2. Test every acceptance criterion the way a user would. {browser}
   For APIs use curl. Try the edge cases a careful tester would (empty input, bad input, refresh, repeat).
3. If a criterion has no automated test, add one under tests/ (e2e or API) and commit it:
   git add -A && git commit -m "{key}: QA tests"   — do NOT change application code yourself.
4. Unclear behaviour? Ask the developer: {cli} ask {dev} "question" --task {key}
5. Verdict — exactly one, then stop:
   {cli} qa-pass {key} "what you checked and the evidence"
   {cli} qa-fail {key} "steps to reproduce · expected · actual"
"""

HELP_PROMPT = """You are {agent} on an AI-native SDLC agent team. Your teammate {dev} is BLOCKED on task {key}: {title}
Their message: "{reason}"

Their worktree is {wt} — read it with Read/Grep/Glob (do NOT edit it). Also read {rel}/spec.md and {rel}/plan.md.
Work out the answer or the fix, then send it to them with concrete steps, file names and code snippets:
  {cli} ask {dev} "<your answer>" --task {key}
If it really needs a human decision, say so in the same message and also run:
  {cli} ask lead "<the decision needed>" --task {key}
Then stop.
"""


DESIGN_PROMPT = """You are the designer on an AI-native SDLC agent team. Design the UI for epic "{epic_title}" BEFORE
the developers build it. A human reviews your mockup and must approve it.

Mode: {mode} ({evidence})
{mode_rules}

Your own git worktree is {wt} on branch {branch}. Work only there.
Read {rel}/intent.md, {rel}/spec.md, {rel}/plan.md, {rel}/breakdown.json (whichever exist), CLAUDE.md, PRODUCT.md and
DESIGN.md if present, and the existing UI code.

UI tasks the developers will build from your design:
{ui_tasks}

Use the impeccable design skill. Load it with the Skill tool (name: impeccable) and follow its Setup, then:
{impeccable_steps}
You cannot ask the human questions in this run: take answers from the intent and spec, and list every assumption in
design-notes.md. If the impeccable skill is not available, write that in design-notes.md and apply the same
discipline yourself (clear hierarchy, real copy, accessible contrast, every state designed, no generic AI look).

Deliverables — commit all of them:
1. DESIGN.md at the repo root: the design standard (tokens: color, type, spacing, radius, shadow; components; states).
2. {rel}/design/mockup.html — ONE self-contained file (inline CSS/JS; only Google Fonts may load from the web) that
   shows every screen and state the UI tasks need: default, filled, empty, loading, validation/error, hover/focus,
   at desktop width and inside a 390px mobile frame. Real copy from the spec — no lorem ipsum. Use the DESIGN.md
   tokens as CSS variables so developers can lift them directly.
3. {rel}/design/design-notes.md — the direction in two sentences, screens, components, tokens, interactions,
   accessibility, assumptions, and for EACH UI task key exactly what to build and which part of the mockup it is.
4. Check it before you submit: open {mockup_url} in the browser tool at 1280px and 390px wide, look at it, fix what
   is off{critique}. At most TWO rounds of check-and-fix — the human reviews it next and can ask for changes.
Time budget: about {budget} minutes. As soon as mockup.html covers every screen and state and the notes are written,
commit and run design-ready. Do not keep polishing.

Then commit (git add -A && git commit -m "{key}: design") and run:
  {cli} design-ready {key} "two-sentence summary: the direction, the screens, what the reviewer should look at"
and stop.
{feedback}"""

DESIGN_MODES = {
    "greenfield": ("There is no UI yet: you set the visual standard everyone else follows.",
                   "- run its init flow to write PRODUCT.md (product context) from the intent/spec;\n"
                   "- treat this as new work: choose a distinct, fitting visual direction and write DESIGN.md;\n"
                   "- use its shape flow to plan each screen, then build the mockup."),
    "brownfield": ("The app already has a look. DESIGN.md (or the existing UI) is the standard: new screens must look "
                   "like they always belonged. Do NOT redesign, re-theme or change existing tokens; add only the new "
                   "components and patterns this change needs, in the same style.",
                   "- if DESIGN.md is missing, run its document flow first to generate DESIGN.md from the existing code;\n"
                   "- if PRODUCT.md is missing, run its init flow from the intent/spec;\n"
                   "- use its shape flow for the new screens only, then build the mockup that reuses existing tokens."),
}

DESIGN_FOR_DEV = """
APPROVED DESIGN — build this task's UI to match it exactly: {rel}/design/mockup.html (open {mockup_url} in a
browser if you have one) and DESIGN.md: layout, tokens (CSS variables), type, spacing, copy and every state.
{rel}/design/design-notes.md says what belongs to {key}. Don't invent new styles; if the design lacks something you
need, ask: {cli} ask designer "..." --task {key}
"""

DESIGN_FOR_QA = """
DESIGN CHECK — this task has an approved design. Open the mockup {mockup_url} and the running page in the browser at
the same widths (1280 and 390) and compare them side by side, the way a designer reviewing a screenshot would.
Fail the task only for deviations a user would notice: wrong or missing layout, element, state or copy; a colour or
font that is not from DESIGN.md; broken responsive layout or horizontal scroll; spacing or size visibly off
(more than ~8px or ~20%). Do NOT fail for sub-pixel or few-pixel differences, slightly different icon sizes/stroke
widths, or anything you only find by measuring with getComputedStyle — list those under "Design nits" in a PASS.
"""


# Windows: `python3` is often missing or the Microsoft Store stub, so agents are told to use `python`
PY = "python" if os.name == "nt" else "python3"
if PY != "python3":
    for _n, _v in list(globals().items()):
        if _n.isupper() and isinstance(_v, str) and "python3 " in _v:
            globals()[_n] = _v.replace("python3 ", PY + " ")


class Lead:
    def __init__(self, root: Path, cfg: dict, change: Path, a):
        self.root, self.cfg, self.change, self.a = root, cfg, change, a
        self.t = cfg["team"]
        self.b = Board()
        self.rel = change.relative_to(root).as_posix()
        self.integration = f"{self.t['branch_prefix']}{change.name}"
        self.wt_root = root / self.t["worktree_dir"]
        self.devs = [f"dev-{i + 1}" for i in range(a.developers if a.developers is not None else self.t["developers"])]
        self.qas = [f"qa-{i + 1}" for i in range(a.qa if a.qa is not None else self.t["qa_agents"])]
        self.running: dict[str, tuple[threading.Thread, AgentRun, str, str]] = {}  # agent -> (thread, run, task, kind)
        self.attempts: dict[str, int] = {}
        self.help_rounds: dict[str, int] = {}
        self.escalated: set[str] = set(self.b.meta().get("escalated", []))
        self.pending: list[tuple] = []  # results from background prep threads, applied by the lead thread
        self.stop = threading.Event()
        try:
            self.tracker = trackers.make(cfg)
        except trackers.TrackerError as e:
            log(f"tracker disabled: {e}")
            self.tracker = None
        self.sync = trackers.Sync(self.b, self.tracker) if self.tracker else None
        self.sim_counter = 0
        self.d = self.t["design"]
        self.design = self.b.meta().get("design")          # {"key", "mode", ...} or None
        self.designers = ["designer"] if self.design else []
        self._design_srv = None
        self._waiting_note = 0

    # ---------------------------------------------------------------- setup
    def wt(self, name: str) -> Path:
        return self.wt_root / name

    def setup(self) -> None:
        if not git(self.root, "rev-parse", "--verify", "--quiet", self.integration):
            git(self.root, "branch", self.integration, "HEAD")
            log(f"created integration branch {self.integration}")
        self.wt_root.mkdir(parents=True, exist_ok=True)
        for name in ["integration", *self.devs, *self.qas, *self.designers]:
            p = self.wt(name)
            if not (p / ".git").exists():
                args = ["worktree", "add", str(p), self.integration] if name == "integration" else \
                       ["worktree", "add", "--detach", str(p), self.integration]
                r = sh(["git", *args], self.root)
                if r.returncode:
                    sys.exit(f"git worktree add {name} failed: {r.stderr}")
                log(f"worktree {name} → {p.relative_to(self.root)}")
            role = "developer" if name.startswith("dev") else "qa" if name.startswith("qa") else \
                   "designer" if name == "designer" else None
            if role:
                self.b.upsert_agent(name, role=role, status="idle", item_key=None, activity="ready",
                                    worktree=str(p.relative_to(self.root)))
        # a previous run that died (crash, Ctrl+C, closed terminal) can leave QA claims nobody holds: no agent is
        # running yet, so every bare claim is stale — release it so QA picks the task up again
        for t in self.b.items(type_="task"):
            claim = t.get("qa_by") or ""
            if t["status"] == "in_qa" and claim and ":" not in claim:
                self.b.update(t["key"], "lead", qa_by=None)
                self.b.event(t["key"], "lead", "system", f"released stale QA claim by {claim} from an earlier run")
                log(f"{t['key']}: released stale QA claim by {claim}")
        self.sync_bin()
        self.b.set_meta(status="running", started_at=utcnow(), integration_branch=self.integration,
                        team={"developers": self.devs, "qa": self.qas, "designers": self.designers})
        if self.design:
            self.start_design_server()
        self.b.event(None, "lead", "system", f"team started: {', '.join(self.designers + self.devs + self.qas)}")
        self.warm(self.devs + self.qas)

    def start_design_server(self) -> None:
        """Serve the worktrees' design folders on localhost so designer, devs, QA and the human see the mockup."""
        port = self.d.get("design_port", 8690)
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(self.wt_root))
        handler.log_message = lambda *a, **k: None
        try:
            self._design_srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as e:
            log(f"design preview server not started on :{port}: {e}")
            return
        threading.Thread(target=self._design_srv.serve_forever, daemon=True).start()
        self.design["preview_base"] = f"http://127.0.0.1:{port}"
        self.b.set_meta(design=self.design)

    def mockup_url(self, where: str = "integration") -> str:
        base = (self.design or {}).get("preview_base")
        rel = f"{where}/{self.rel}/design/mockup.html"
        return f"{base}/{rel}" if base else str(self.wt_root / rel)

    def env_for(self, agent: str, role: str) -> dict:
        env = {"SDLC_AGENT": agent, "SDLC_ROLE": role, "SDLC_BOARD_DB": str(self.b.path)}
        if self.tracker:
            env.update(self.tracker.mcp_env())
        return env

    def mcp_for(self, role: str, port: int | None = None) -> dict:
        servers = {}
        if self.tracker:
            servers.update(self.tracker.mcp_config())
        if role in ("qa", "designer") and self.t["qa"].get("browser") == "playwright-mcp":
            args = ["-y", "@playwright/mcp@latest", "--headless", "--isolated"]
            if self.t["qa"].get("browser_executable"):
                args += ["--executable-path", self.t["qa"]["browser_executable"]]
            args += list(self.t["qa"].get("browser_args") or [])  # e.g. ["--no-sandbox"] when running as root
            servers["playwright"] = {"command": "npx", "args": args}
        return servers or None

    def tools_for(self, role: str) -> list[str]:
        extra = [f"Bash({c})" for c in self.cfg["verify"].values() if c]
        extra += [f"Bash({c})" for c in self.t.get("allowed_bash", []) + self.cfg["pipeline"].get("allowed_bash", [])]
        mcp = ["mcp__tracker"] if self.tracker else []
        if role == "developer":
            return BASE_TOOLS + EDIT_TOOLS + SAFE_BASH + DEV_BASH + extra + mcp
        if role == "qa":
            pw = ["mcp__playwright"] if self.t["qa"].get("browser") == "playwright-mcp" else []
            return BASE_TOOLS + EDIT_TOOLS + SAFE_BASH + QA_BASH + extra + mcp + pw
        if role == "designer":
            pw = ["mcp__playwright"] if self.t["qa"].get("browser") == "playwright-mcp" else []
            return BASE_TOOLS + EDIT_TOOLS + SAFE_BASH + ["Skill", "Bash(*impeccable*)", "Bash(npx impeccable*)"] + \
                extra + mcp + pw
        return BASE_TOOLS + ["Bash(python3 .sdlc/bin/board.py *)", "Bash(python .sdlc/bin/board.py *)"] + mcp

    # ---------------------------------------------------------------- helpers
    def tasks(self) -> list[dict]:
        return self.b.items(type_="task")

    def sync_bin(self) -> None:
        """Agents run the helper scripts from their own worktree, i.e. from the team branch. After the human upgrades
        the kit (init.py --upgrade-bin), copy the new scripts into the team branch so every new task uses them."""
        src, iwt = self.root / ".sdlc" / "bin", self.wt("integration")
        dst = iwt / ".sdlc" / "bin"
        if not src.is_dir():
            return
        changed = []
        for f in sorted(src.glob("*.py")):
            target = dst / f.name
            if not target.exists() or target.read_bytes() != f.read_bytes():
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                changed.append(f.name)
        if changed:
            sh(["git", "add", ".sdlc/bin"], iwt)
            sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "commit", "-qm",
                "sdlc: update kit scripts on the team branch"], iwt)
            log(f"updated kit scripts on {self.integration}: {', '.join(changed)}")
            self.b.event(None, "lead", "system", f"kit scripts updated on the team branch ({len(changed)} files)")

    def has_work(self, t: dict, wt: Path) -> bool:
        """The task's branch already carries commits beyond the integration branch and nothing sent it back."""
        if not t.get("branch") or t["status"] != "in_progress" or t.get("qa_rounds"):
            return False
        n = sh(["git", "rev-list", "--count", f"{self.integration}..{t['branch']}"], wt).stdout.strip()
        return n.isdigit() and int(n) > 0

    def busy(self, agent: str) -> bool:
        w = getattr(self, "warming", {}).get(agent)
        queued = any(j[0] == "qa" and j[1] == agent for j in getattr(self, "pending", []))
        return agent in self.running or bool(w and w.is_alive()) or queued

    def warm(self, names: list[str]) -> None:
        """Install dependencies in idle agents' worktrees in the background, so agents don't spend their own time
        (and time budget) on pip/npm. Skips when the manifests are unchanged since that worktree's last install."""
        cmd = (self.cfg.get("verify") or {}).get("setup")
        if not cmd or not setup_signature(self.wt("integration"), cmd):
            return  # nothing to install yet (e.g. before the skeleton task lands)
        self.warming = getattr(self, "warming", {})
        for n in names:
            if self.busy(n):
                continue
            wt = self.wt(n)

            def job(n=n, wt=wt):
                sh(["git", "checkout", "-q", "--detach", self.integration], wt)
                self.fresh_bin(wt)
                env = {**os.environ, "SDLC_SETUP_IF_CHANGED": "1", "PYTHONIOENCODING": "utf-8"}
                r = sh([sys.executable, ".sdlc/bin/sdlc.py", "verify", "setup"], wt, timeout=1800, env=env)
                skipped = "SKIP" in (r.stdout or "")
                if not skipped:
                    self.b.event(None, "lead", "system", f"{n}: dependencies "
                                 + ("installed in its worktree" if r.returncode == 0 else
                                    f"install failed — the agent will retry: {(r.stdout + r.stderr).strip()[-200:]}"))
                self.b.upsert_agent(n, activity="ready")
            self.b.upsert_agent(n, activity="installing dependencies")
            th = threading.Thread(target=job, daemon=True)
            th.start()
            self.warming[n] = th

    def idle(self, names: list[str]) -> list[str]:
        return [n for n in names if not self.busy(n)]

    def ready(self, t: dict) -> bool:
        return all((self.b.item(d) or {}).get("status") == "done" for d in t["depends_on"])

    def file_clash(self, t: dict) -> str | None:
        mine = set(t["files"])
        for other in self.tasks():
            if other["key"] != t["key"] and other["status"] in ("in_progress", "blocked") and mine & set(other["files"]):
                if any(r[2] == other["key"] for r in self.running.values()):
                    return other["key"]
        return None

    def context(self, t: dict) -> dict:
        story = self.b.item(t["parent_key"]) or {}
        epic = self.b.item(story.get("parent_key") or "") or {}
        return {"key": t["key"], "title": t["title"], "description": t["description"] or "(see story)",
                "files": ", ".join(t["files"]) or "(decide, keep it small)",
                "acceptance": "\n".join(f"- {x}" for x in t["acceptance"]) or "- see story",
                "story_key": story.get("key"), "story_title": story.get("title"),
                "story_acceptance": "; ".join(story.get("acceptance") or []) or "-",
                "epic_title": epic.get("title"), "rel": self.rel, "cli": BOARD_CLI}

    def fresh_bin(self, wt: Path) -> None:
        """Every agent runs the lead's current helper scripts, whatever its branch was created with (a branch
        made before a kit upgrade still carries the old .sdlc/bin)."""
        src, dst = self.root / ".sdlc" / "bin", Path(wt) / ".sdlc" / "bin"
        if not src.is_dir() or src.resolve() == dst.resolve():
            return
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.py"):
            target = dst / f.name
            if not target.exists() or target.read_bytes() != f.read_bytes():
                shutil.copy2(f, target)

    def launch(self, agent: str, role: str, kind: str, t: dict, prompt: str, cwd: Path, model: str,
               add_dirs=None, port=None, behaviour="") -> None:
        self.fresh_bin(cwd)
        if self.a.simulate:
            run = SimulatedRun(self.b, agent, cwd, prompt, model, [], t["key"], 5,
                               extra_env=self.env_for(agent, role), role=kind, behaviour=behaviour)
        else:
            run = AgentRun(self.b, agent, cwd, prompt, model, self.tools_for(role), t["key"],
                           self.t["run_timeout_minutes"], mcp=self.mcp_for(role, port),
                           extra_env=self.env_for(agent, role), add_dirs=add_dirs,
                           stall_min=self.t.get("stall_minutes", 12),
                           plugin_dir=[os.environ.get("SDLC_PLUGIN_DIR")] +
                                      (list(self.d.get("plugin_dirs") or []) if role == "designer" else []))
        th = threading.Thread(target=run.run, daemon=True, name=f"{agent}:{t['key']}")
        self.running[agent] = (th, run, t["key"], kind)
        self.b.upsert_agent(agent, status="helping" if kind == "helper" else "designing" if kind == "designer" else "working",
                            item_key=t["key"],
                            activity=f"started {kind} on {t['key']}")
        th.start()
        log(f"{agent} → {kind} {t['key']} {t['title'][:60]}")

    # ---------------------------------------------------------------- dispatch
    def start_dev(self, agent: str, t: dict) -> None:
        wt = self.wt(agent)
        branch = t.get("branch") or f"{self.integration}--{t['key']}"  # not "/": git can't nest under a branch
        if t.get("branch"):  # returning work (QA fail, conflict, help): keep the branch
            r = sh(["git", "checkout", "-q", branch], wt)
            qa_branch = f"qa/{t['key']}"
            if git(self.root, "rev-parse", "--verify", "--quiet", qa_branch):
                sh(["git", "merge", "-q", "--no-edit", qa_branch], wt)  # keep QA's tests
        else:
            sh("git reset -q --hard && git clean -qfd", wt)
            r = sh(["git", "checkout", "-q", "-B", branch, self.integration], wt)
        if r.returncode:
            self.escalate(t, f"could not check out {branch} in {agent}'s worktree: {r.stderr.strip()[:200]}")
            return
        others = ", ".join(n for n in self.devs + self.qas if n != agent)
        extra = ""
        if self.attempts.get(t["key"]) or self.has_work(t, wt):
            extra += ("\nYOUR PREVIOUS RUN ON THIS TASK STOPPED (time limit or exit) BEFORE YOU SUBMITTED. Its work is "
                      "committed on your branch: read `git log --oneline -5` and `git diff " + self.integration + " --stat`, "
                      "CONTINUE from it — do not start over — get verify green and submit.\n")
        fails = [e for e in self.b.events(item_key=t["key"], limit=50) if e["kind"] == "qa" and e["text"].startswith("FAIL")]
        if fails:
            extra += f"\nQA SENT THIS BACK (round {len(fails)}): {fails[-1]['text'][6:]}\nFix it, then submit again.\n"
        if self.b.meta().get(f"conflict:{t['key']}"):
            extra += (f"\nYOUR BRANCH CONFLICTS with {self.integration}. Run `git merge {self.integration}`, resolve the "
                      "conflicts keeping both features, run verify, commit, submit again.\n")
        helps = [m for m in self.b.messages(item_key=t["key"]) if m["recipient"] == agent and m["sender"] != agent]
        if helps:
            extra += "\nMessages from your team about this task:\n" + "\n".join(
                f"- {m['sender']}: {m['text']}" for m in helps[-5:]) + "\n"
        if self.design and self.design["key"] in t["depends_on"]:
            extra += DESIGN_FOR_DEV.format(rel=self.rel, mockup_url=self.mockup_url(), key=t["key"], cli=BOARD_CLI)
        ctx = {**self.context(t), "agent": agent, "others": others, "wt": wt, "branch": branch, "extra": extra,
               "budget": max(10, int(self.t["run_timeout_minutes"] * 0.6))}
        self.b.update(t["key"], "lead", status="in_progress", assignee=agent, branch=branch)
        behaviour = "block" if self.a.simulate and t["key"] == "T-4" and not self.help_rounds.get("T-4") else ""
        self.launch(agent, "developer", "developer", t, DEV_PROMPT.format(**ctx), wt,
                    self.t["models"]["developer"], behaviour=behaviour)

    def start_qa(self, agent: str, t: dict) -> None:
        """Claim the task now; check out, install and start the app in the background so the lead keeps
        dispatching developers and merging while QA gets ready."""
        self.b.update(t["key"], "lead", qa_by=agent)
        self.b.event(t["key"], "lead", "assign", f"QA {agent} picked up {t['key']} from {t['assignee']}")
        self.b.upsert_agent(agent, status="working", item_key=t["key"], activity="preparing: checkout, install, start app")
        self.warming = getattr(self, "warming", {})

        def prep():
            try:
                wt, qa_branch = self.wt(agent), f"qa/{t['key']}"
                sh("git reset -q --hard && git clean -qfd", wt)
                r = sh(["git", "checkout", "-q", "-B", qa_branch, t["branch"]], wt)
                if r.returncode:
                    self.pending.append(("escalate", t["key"], f"QA could not check out {t['branch']}: {r.stderr.strip()[:200]}"))
                    return
                self.fresh_bin(wt)
                port = self.t["qa"]["port_base"] + self.qas.index(agent)
                app = self.start_app(agent, wt, port)
                self.pending.append(("qa", agent, t["key"], qa_branch, app))
            except Exception as e:
                self.pending.append(("escalate", t["key"], f"QA preparation failed: {e}"))
        th = threading.Thread(target=prep, daemon=True, name=f"{agent}:prep:{t['key']}")
        th.start()
        self.warming[agent] = th

    def launch_qa(self, agent: str, key: str, qa_branch: str, app: str | None) -> None:
        t = self.b.item(key)
        wt = self.wt(agent)
        browser = ("Use the browser tools (mcp__playwright__browser_navigate, browser_snapshot, browser_click, "
                   "browser_type, browser_take_screenshot) against the running app."
                   if self.t["qa"].get("browser") == "playwright-mcp" else
                   "Drive the UI with Playwright scripts (npx playwright / python playwright).")
        ctx = {**self.context(t), "agent": agent, "devs": ", ".join(self.devs), "dev": t["assignee"], "wt": wt,
               "branch": qa_branch, "browser": browser,
               "app": f"The lead started the app for you at {app}." if app else
                      "No app server is configured (team.qa.start_cmd); test through tests, API calls and files."}
        behaviour = "fail" if self.a.simulate and t["key"] == "T-2" and not t["qa_rounds"] else ""
        prompt = QA_PROMPT.format(**ctx)
        if self.design and self.design["key"] in t["depends_on"]:
            prompt += DESIGN_FOR_QA.format(mockup_url=self.mockup_url())
        port = int(app.split(":")[2].split()[0].split("/")[0]) if app else None
        self.launch(agent, "qa", "qa", t, prompt, wt, self.t["models"]["qa"], port=port, behaviour=behaviour)

    def lead_check(self, t: dict) -> None:
        """Verify a no-QA task in its developer's worktree (still on the task branch) in the background."""
        agent = t["assignee"]
        self.b.update(t["key"], "lead", qa_by="lead")
        self.b.event(t["key"], "lead", "assign", f"setup task — the lead verifies {t['key']} (no QA agent needed)")
        self.warming = getattr(self, "warming", {})

        def job():
            wt = self.wt(agent)
            if self.a.simulate or not any((self.cfg.get("verify") or {}).values()):
                self.pending.append(("checked", t["key"], True, "no verify commands configured"))
                return
            sh(["git", "checkout", "-q", t["branch"]], wt)
            self.fresh_bin(wt)
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            r = sh([sys.executable, ".sdlc/bin/sdlc.py", "verify"], wt, timeout=1800, env=env)
            self.pending.append(("checked", t["key"], r.returncode == 0, (r.stdout + r.stderr).strip()[-600:]))
        th = threading.Thread(target=job, daemon=True, name=f"lead:check:{t['key']}")
        th.start()
        self.warming[agent] = th  # keeps the developer's worktree on this branch until the check is done

    def drain(self) -> None:
        """Apply results that background preparation threads queued (only the lead thread touches dispatch)."""
        while self.pending:
            job = self.pending.pop(0)
            if job[0] == "qa":
                _, agent, key, qa_branch, app = job
                self.launch_qa(agent, key, qa_branch, app)
            elif job[0] == "checked":
                _, key, ok, out = job
                t = self.b.item(key)
                if not t or t["status"] != "in_qa":
                    continue
                if ok:
                    self.b.event(key, "lead", "qa", "PASS: setup task — verify green (checked by the lead)")
                    self.b.update(key, "lead", qa_by="pass:lead")  # merged on the next tick
                else:
                    self.b.event(key, "lead", "qa", f"FAIL: verify is not green on {t['branch']}: {out}")
                    self.b.update(key, "lead", note="verify failed in the lead's check", status="in_progress",
                                  qa_rounds=(t.get("qa_rounds") or 0) + 1, qa_by="fail:lead")
                    if t["assignee"]:
                        self.b.send("lead", t["assignee"], f"{key}: verify failed when I checked it:\n{out}", key)
            elif job[0] == "escalate":
                t = self.b.item(job[1])
                if t:
                    self.escalate(t, job[2])

    def start_design(self, d: dict) -> None:
        agent, wt = "designer", self.wt("designer")
        branch = d.get("branch") or f"{self.integration}--{d['key']}"
        if d.get("branch"):
            r = sh(["git", "checkout", "-q", branch], wt)
        else:
            sh("git reset -q --hard && git clean -qfd", wt)
            r = sh(["git", "checkout", "-q", "-B", branch, self.integration], wt)
        if r.returncode:
            self.escalate(d, f"could not check out {branch} for the designer: {r.stderr.strip()[:200]}")
            return
        mode = self.design.get("mode", "greenfield")
        rules, steps = DESIGN_MODES[mode]
        ui = [self.b.item(k) for k in self.design.get("ui_tasks", [])]
        ui_text = "\n".join(f"- {t['key']} {t['title']}: " + "; ".join(t["acceptance"]) for t in ui if t) or "- (all UI)"
        changes = [e["text"][9:] for e in self.b.events(item_key=d["key"], limit=80)
                   if e["kind"] == "design" and e["text"].startswith("CHANGES:")]
        feedback = (f"\nTHE HUMAN ASKED FOR CHANGES (round {len(changes)}): {changes[-1]}\n"
                    "Update the design (keep what they did not mention), then design-ready again.\n") if changes else ""
        if (self.attempts.get(d["key"]) or self.has_work(d, wt)) and not changes:
            feedback += ("\nYOUR PREVIOUS RUN STOPPED BEFORE design-ready. Your files are committed in this worktree "
                         "(DESIGN.md, the mockup, the notes): check what is missing, finish only that, then commit and "
                         "run design-ready right away.\n")
        epic = self.b.item(d["parent_key"]) or {}
        prompt = DESIGN_PROMPT.format(
            epic_title=epic.get("title", ""), mode=mode, evidence=self.design.get("evidence", ""), mode_rules=rules,
            impeccable_steps=steps, wt=wt, branch=branch, rel=self.rel, ui_tasks=ui_text, key=d["key"], cli=BOARD_CLI,
            mockup_url=self.mockup_url("designer"), feedback=feedback,
            budget=max(10, int(self.t["run_timeout_minutes"] * 0.6)),
            critique="; if the impeccable skill is loaded, run its critique and audit on the mockup and fix the findings")
        self.b.update(d["key"], "lead", status="in_progress", branch=branch)
        self.launch(agent, "designer", "designer", d, prompt, wt, self.t["models"].get("designer", "sonnet"),
                    behaviour=self.rel)

    def start_help(self, agent: str, t: dict) -> None:
        reason = next((e["text"].split(": ", 1)[-1] for e in reversed(self.b.events(item_key=t["key"], limit=50))
                       if e["kind"] == "status" and "Blocked" in e["text"]), "see the task history")
        ctx = {**self.context(t), "agent": agent, "dev": t["assignee"], "wt": self.wt(t["assignee"]),
               "reason": reason}
        self.help_rounds[t["key"]] = self.help_rounds.get(t["key"], 0) + 1
        self.b.event(t["key"], "lead", "help", f"{agent} sent to help {t['assignee']} (round {self.help_rounds[t['key']]})")
        cwd = self.wt(agent)
        self.launch(agent, "helper", "helper", t, HELP_PROMPT.format(**ctx), cwd, self.t["models"]["helper"],
                    add_dirs=[str(self.wt(t["assignee"]))], behaviour=t["assignee"])

    # ---------------------------------------------------------------- app for QA
    def start_app(self, agent: str, wt: Path, port: int) -> str | None:
        cmd = (self.t["qa"].get("start_cmd") or "").strip()
        if not cmd or self.a.simulate:
            return None
        if self.cfg["verify"].get("setup"):  # cached: a no-op when the worktree's dependencies are already installed
            env = {**os.environ, "SDLC_SETUP_IF_CHANGED": "1", "PYTHONIOENCODING": "utf-8"}
            sh([sys.executable, ".sdlc/bin/sdlc.py", "verify", "setup"], wt, timeout=1800, env=env)
        for cand in [port + 10 * i for i in range(10)]:  # a crashed run may have left an app on the port
            if port_free(cand):
                port = cand
                break
        proc = subprocess.Popen(portable_cmd(cmd).replace("{port}", str(port)), shell=True, cwd=wt, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env={**os.environ, "PORT": str(port)}, **NEW_GROUP)
        self.apps = getattr(self, "apps", {})
        self.apps[agent] = proc
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + self.t["qa"].get("start_timeout", 60)
        while time.time() < deadline:
            if proc.poll() is not None:
                return url + " (the app exited at startup — check start_cmd; report that in your verdict)"
            try:
                urllib.request.urlopen(url + self.t["qa"].get("health_path", "/"), timeout=2)
                return url
            except urllib.error.HTTPError:
                return url  # server answers; a 4xx on the health path is fine
            except Exception:
                time.sleep(1)
        log(f"{agent}: app did not answer on {url} within the timeout")
        return url + " (it did not answer the health check — report that if it stays down)"

    def stop_app(self, agent: str) -> None:
        kill_tree(getattr(self, "apps", {}).pop(agent, None))

    # ---------------------------------------------------------------- after a run
    def finished(self, agent: str) -> None:
        th, run, key, kind = self.running.pop(agent)
        t = self.b.item(key)
        if run.error:
            self.b.event(key, agent, "system", f"{kind} run error: {run.error[:300]}")
        if kind == "developer":
            wt = self.wt(agent)
            if sh(["git", "status", "--porcelain"], wt).stdout.strip():
                sh("git add -A", wt)
                sh(["git", "-c", f"user.name={agent}", "-c", "user.email=agents@sdlc.local", "commit", "-qm",
                    f"{key}: uncommitted work from {agent}"], wt)
            if t["status"] == "in_progress":
                n = self.attempts[key] = self.attempts.get(key, 0) + 1
                if n >= self.t["max_dev_attempts"]:
                    self.escalate(t, f"{agent} ran {n} times without submitting")
                else:
                    self.b.event(key, "lead", "system", f"{agent} stopped without submitting — retrying")
            elif t["status"] == "in_qa":
                self.attempts.pop(key, None)
                self.b.set_meta(**{f"conflict:{key}": False})
        elif kind == "qa":
            self.stop_app(agent)
            sh(["git", "checkout", "-q", "--detach"], self.wt(agent))  # free qa/<task> for any QA agent
            if t["status"] == "in_qa" and not (t.get("qa_by") or "").startswith("pass:"):
                n = self.attempts[f"qa:{key}"] = self.attempts.get(f"qa:{key}", 0) + 1
                if n >= 2:
                    self.escalate(t, f"QA could not reach a verdict ({run.error or 'no qa-pass/qa-fail'})")
                else:
                    self.b.update(key, "lead", qa_by=None)  # no verdict: another QA run picks it up
                    self.b.event(key, "lead", "system", f"{agent} gave no verdict — QA will retry. Its last words: "
                                                        f"{(run.result_text or run.error or '-')[-300:]}")
            if (t.get("qa_rounds") or 0) >= self.t["max_qa_rounds"] and t["status"] != "in_qa":
                self.escalate(t, f"failed QA {t['qa_rounds']} times")
        elif kind == "designer":
            wt = self.wt(agent)
            if sh(["git", "status", "--porcelain"], wt).stdout.strip():
                sh("git add -A", wt)
                sh(["git", "-c", "user.name=designer", "-c", "user.email=agents@sdlc.local", "commit", "-qm",
                    f"{key}: uncommitted design work"], wt)
            if t["status"] == "in_progress":
                n = self.attempts[key] = self.attempts.get(key, 0) + 1
                if n >= self.t["max_dev_attempts"]:
                    self.escalate(t, f"designer ran {n} times without submitting a design")
                else:
                    self.b.event(key, "lead", "system", "designer stopped without design-ready — retrying. Last words: "
                                                        f"{(run.result_text or run.error or '-')[-300:]}")
            elif t["status"] == "review":
                self.attempts.pop(key, None)
                log(f"design {key} is ready — waiting for the human to approve it on the board")
        elif kind == "helper":
            if t["status"] == "blocked":
                if self.help_rounds.get(key, 0) >= self.t["max_help_rounds"]:
                    self.escalate(t, "still blocked after team help")
                else:
                    self.b.update(key, "lead", note=f"{agent} answered — back to {t['assignee']}", status="in_progress")
        self.b.upsert_agent(agent, status="idle", item_key=None, activity=f"finished {kind} {key}")

    def escalate(self, t: dict, why: str) -> None:
        self.escalated.add(t["key"])
        self.b.set_meta(escalated=sorted(self.escalated))
        self.b.update(t["key"], "lead", note=f"ESCALATED to a human: {why}", status="blocked")
        self.b.send("lead", "human", f"{t['key']} needs you: {why}", t["key"])
        log(f"ESCALATED {t['key']}: {why}")

    # ---------------------------------------------------------------- merging
    def merge(self, t: dict) -> None:
        iwt = self.wt("integration")
        src = f"qa/{t['key']}" if git(self.root, "rev-parse", "--verify", "--quiet", f"qa/{t['key']}") else t["branch"]
        who = (f"designer, approved by {t['qa_by'].split(':')[-1]}" if t["type"] == "design" else
               f"dev {t['assignee']}, QA {t['qa_by'].split(':')[-1]}")
        r = sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "merge", "--no-ff", "-q",
                "-m", f"{t['key']}: {t['title']} ({who})", src], iwt)
        if r.returncode:
            sh(["git", "merge", "--abort"], iwt)
            n = self.attempts[f"merge:{t['key']}"] = self.attempts.get(f"merge:{t['key']}", 0) + 1
            if n >= 3:
                self.escalate(t, f"merge into {self.integration} failed {n} times: {(r.stderr or r.stdout).strip()[:200]}")
                return
            self.b.set_meta(**{f"conflict:{t['key']}": True})
            self.b.update(t["key"], "lead", note=f"merge conflict with {self.integration} — back to {t['assignee']}",
                          status="in_progress", qa_by=None)
            self.b.send("lead", t["assignee"], f"{t['key']} conflicts with {self.integration}; merge it into your "
                                               "branch, resolve, re-run verify and submit again.", t["key"])
            log(f"merge conflict on {t['key']} → back to {t['assignee']}")
            return
        self.b.update(t["key"], "lead", note=f"merged into {self.integration}", status="done")
        self.b.event(t["key"], "lead", "merge", f"merged {src} into {self.integration}")
        log(f"merged {t['key']} into {self.integration}")
        self.warm(self.devs + self.qas)  # new manifests from this merge get installed while agents are idle

    # ---------------------------------------------------------------- the loop
    def tick(self) -> None:
        self.escalated = set(self.b.meta().get("escalated", []))  # the human may resume one from the board
        self.drain()
        for agent, (th, *_rest) in list(self.running.items()):
            if not th.is_alive():
                self.finished(agent)
        # 0. design: designer works, the human approves, the lead merges it so UI tasks can start
        for d in self.b.items(type_="design"):
            if d["key"] in self.escalated:
                continue
            busy = any(r[2] == d["key"] for r in self.running.values())
            if d["status"] in ("todo", "in_progress") and not busy and self.idle(self.designers):
                rounds = d.get("qa_rounds") or 0
                if rounds > self.d.get("max_rounds", 3):
                    self.escalate(d, f"design sent back {rounds} times")
                else:
                    self.start_design(d)
            elif d["status"] == "review" and not busy:
                verdict = d.get("qa_by") or ""
                if verdict.startswith("approved:"):
                    self.merge(d)
                elif self.d.get("approval") == "auto" or getattr(self.a, "auto_approve_design", False):
                    self.b.approve_design(d["key"], "auto-approved (team.design.approval = auto)", actor="lead")
                elif time.time() - self._waiting_note > 300:
                    self._waiting_note = time.time()
                    self.b.event(d["key"], "lead", "system", "waiting for the human to approve the design — "
                                 "open the board, view the mockup, then Approve or Request changes")
                    log(f"waiting for design approval on {d['key']}: {self.mockup_url('designer')}")
        tasks = self.tasks()
        # 1. merge QA-passed work
        for t in tasks:
            if t["status"] == "in_qa" and (t.get("qa_by") or "").startswith("pass:") and not any(
                    r[2] == t["key"] for r in self.running.values()):
                self.merge(t)
        tasks = self.tasks()
        # 2a. setup tasks the planner marked "qa": "none": the lead runs verify itself instead of a QA agent
        no_qa = set(self.b.meta().get("no_qa") or [])
        for t in tasks:
            if t["status"] == "in_qa" and not t.get("qa_by") and t["key"] in no_qa:
                self.lead_check(t)
        tasks = self.tasks()
        # 2. QA picks up hand-offs
        for t in tasks:
            if t["status"] == "in_qa" and not t.get("qa_by"):
                free = self.idle(self.qas)
                if free:
                    self.start_qa(free[0], t)
        # 3. help blocked teammates
        for t in tasks:
            if t["status"] == "blocked" and t["key"] not in self.escalated and not any(
                    r[2] == t["key"] for r in self.running.values()):
                helpers = [n for n in self.idle(self.devs) if n != t["assignee"]] or \
                          [n for n in self.idle(self.qas)]
                if helpers:
                    self.start_help(helpers[0], t)
                elif len(self.devs) == 1 and not self.qas:
                    self.escalate(t, "no teammate available to help")
        # 4. developers: returning work first, then their own queue, then steal
        for dev in self.idle(self.devs):
            mine = [t for t in self.tasks() if t["assignee"] == dev and t["key"] not in self.escalated]
            back = [t for t in mine if t["status"] == "in_progress" and t.get("branch")
                    and not any(r[2] == t["key"] for r in self.running.values())]
            todo = [t for t in mine if t["status"] == "todo" and self.ready(t) and not self.file_clash(t)]
            if not back and not todo:
                todo = [t for t in self.tasks() if t["status"] == "todo" and self.ready(t) and not self.file_clash(t)
                        and not self.busy_owner_has(t)]
                if todo:
                    self.b.event(todo[0]["key"], "lead", "assign",
                                 f"reassigned from {todo[0]['assignee']} to {dev} (it was free)")
            pick = (back or todo or [None])[0]
            if pick:
                self.start_dev(dev, pick)
        if self.sync and time.time() - getattr(self, "_last_sync", 0) > self.t["sync_seconds"]:
            self._last_sync = time.time()
            self.sync.run()

    def busy_owner_has(self, t: dict) -> bool:
        """Don't steal a task its owner will reach soon — only steal when the owner is busy on something else."""
        return t["assignee"] in self.idle(self.devs)

    def done(self) -> bool:
        ts = self.tasks()
        return bool(ts) and all(t["status"] == "done" or t["key"] in self.escalated for t in ts) and not self.running

    def run(self) -> int:
        self.setup()
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        last_progress, last_state = time.time(), None
        while not self.stop.is_set():
            self.tick()
            state = json.dumps([(t["key"], t["status"], t.get("qa_by")) for t in self.b.items()
                                if t["type"] in ("task", "design")])
            if state != last_state or self.running:
                last_progress, last_state = time.time(), state
            if self.done():
                break
            waiting_human = any(d["status"] == "review" for d in self.b.items(type_="design"))
            if waiting_human and time.time() - last_progress < self.d.get("approval_timeout_minutes", 240) * 60:
                time.sleep(1 if self.a.simulate else self.t["poll_seconds"])
                continue
            if not self.running and time.time() - last_progress > 60:
                log("no progress possible — everything left is blocked or waiting on a human")
                break
            time.sleep(1 if self.a.simulate else self.t["poll_seconds"])
        for agent, (_, run, *_r) in list(self.running.items()):
            run.stop()
        for agent in list(getattr(self, "apps", {})):
            self.stop_app(agent)
        if self._design_srv:
            self._design_srv.shutdown()
        return self.wrap_up()

    def wrap_up(self) -> int:
        ts = self.tasks()
        done = [t for t in ts if t["status"] == "done"]
        iwt = self.wt("integration")
        verify_ok = None
        if done and not self.a.simulate and any(self.cfg["verify"].values()):
            r = sh([sys.executable, ".sdlc/bin/sdlc.py", "verify"], iwt, timeout=1800)
            verify_ok = r.returncode == 0
            self.b.event(None, "lead", "system", f"integration verify: {'PASS' if verify_ok else 'FAIL'}")
        agents = self.b.agents()
        report = [f"# Team report: {self.change.name}", "",
                  f"Integration branch: `{self.integration}` · finished {utcnow()}", "",
                  f"Tasks done: {len(done)}/{len(ts)} · escalated: {len(self.escalated)}"
                  + (f" · integration verify: {'PASS' if verify_ok else 'FAIL'}" if verify_ok is not None else ""), "",
                  "| Task | Title | Developer | QA | QA rounds | Status |", "|---|---|---|---|---|---|"]
        for t in ts:
            report.append(f"| {t['key']} | {t['title']} | {t['assignee'] or '-'} | {(t.get('qa_by') or '-').split(':')[-1]} "
                          f"| {t.get('qa_rounds') or 0} | {STATUS_LABEL.get(t['status'], t['status'])} |")
        report += ["", "| Agent | Role | Runs | Cost (est.) |", "|---|---|---|---|"]
        report += [f"| {a['name']} | {a['role']} | {a['runs']} | ${a['cost'] or 0:.2f} |" for a in agents]
        (iwt / self.rel).mkdir(parents=True, exist_ok=True)
        (iwt / self.rel / "team-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        sh("git add -A", iwt)
        sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "commit", "-qm",
            f"team-report: {self.change.name}"], iwt)
        status = "done" if len(done) == len(ts) else "needs-human"
        self.b.set_meta(status=status, finished_at=utcnow())
        for a in agents:
            self.b.upsert_agent(a["name"], status="stopped", item_key=None)
        if self.sync:
            self.sync.run()
        log(f"team finished: {len(done)}/{len(ts)} tasks done, {len(self.escalated)} escalated → {self.integration}")
        if self.a.pr and status == "done" and not self.a.simulate:
            sh(["git", "push", "-u", "origin", self.integration], self.root)
            if shutil.which("gh"):
                sh(["gh", "pr", "create", "--head", self.integration, "--title", f"team: {self.change.name}",
                    "--body", f"Built by the agent team. See {self.rel}/team-report.md."], self.root)
        return 0 if status == "done" else 3


# ====================================================================== Claude Code agent-teams mode
# The session you talk to is the team lead. It spawns persistent teammates (dev-1, dev-2, qa-1, designer) once and
# relays assignments that this deterministic helper computes. All git, board, merge and app work happens here in
# seconds; teammates only do the thinking. State lives on the board (meta "tl"), so every `team.py next` call is
# stateless and a crashed or restarted session picks up where it was.

TL_ROLES = {"developer": "sdlc:developer", "qa": "sdlc:qa", "designer": "sdlc:designer", "helper": "sdlc:developer"}

TL_HEADER = """You are {agent}, the {role} on a Claude Code agent team. The team lead (the session that messaged you)
gives you one assignment at a time. You stay alive between assignments, so keep what you learn about the codebase.

YOUR WORKTREE: {wt}
  Work ONLY there. Start every Bash command with  cd "{wt}" &&  and use absolute paths under it for Read/Edit/Write.
  Never touch another teammate's worktree, never merge or push branches — the lead does that.
BOARD CLI (always with your identity): {cli}
"""

TL_FOOTER = """
When this assignment is finished (submitted / verdicts given / blocked / design-ready), send the team lead ONE short
message: "done {keys}: <one line>". Then wait for the next assignment. Do not pick up other work yourself.
Toolchain broken (installs fail, a tool is missing)? Don't fight it for more than two tries: report it with
{cli} blocked <key> "<the error>" and tell the lead.
"""

TL_DEV = """ASSIGNMENT — story {story_key} "{story_title}" (epic "{epic_title}"), branch {branch}.
Build these {n} task(s) in this order:
{tasks}
{extra}
How to work (fast, and the quality bar does not move):
1. The tasks above are your spec. Only when a detail is missing, search {rel}/spec.md or {rel}/plan.md — don't read
   them whole. Follow CLAUDE.md.
2. Scaffolding (a new frontend/backend project, config) comes from the official generators, e.g.
   `npm create vite@latest frontend -- --template react-ts`, then adjust — don't hand-type boilerplate.
3. New packages go into the manifest, then `{py} .sdlc/bin/sdlc.py verify setup`. Never install by hand.
4. Keep the team informed — one short line each, people watch these live on the board:
   at the start:     {cli} comment <first key> "started: <your plan in a few words>"
   after each task:  {cli} comment <key> "done: <what works now> · next: <what you do next>"
   Per task: implement, write its tests, run ONLY those tests (one file), commit:
   git add -A && git commit -m "<key>: <what>"
5. After the last task run the full `{py} .sdlc/bin/sdlc.py verify` once. It must be green — fix the code, never
   weaken a test.
6. Submit every task: {cli} submit <key> "what you built and how QA checks it"
7. Questions: SendMessage the teammate who knows, or {cli} ask <name> "..." --task <key>.
   Blocked: {cli} blocked <key> "exactly what you need".
Budget: about {budget} minutes for this assignment.
"""

TL_QA = """ASSIGNMENT — QA for story {story_key} "{story_title}" on branch {branch} (built by {dev}).
Tasks to verify:
{tasks}
{app}
How to test (fast, thorough) — time budget: about {budget} minutes. Effort matches the size of the change.
1. Your worktree is checked out at {branch} and its dependencies are installed. Run `{py} .sdlc/bin/sdlc.py verify test`
   ONCE (don't re-run the whole suite after each check; re-run only a test file you added).
   Post a line now and after every check, people watch them live on the board:
   {cli} comment <key> "testing: <what you are checking now>"
2. Prove every acceptance criterion the way a user would: API checks with curl, and ONE browser pass through the
   story's user-visible flow{browser}. Try the edge cases a careful tester would (empty, bad input, refresh, repeat).
   Don't fight tooling: if the browser or app won't start within a couple of minutes, verify through tests and API
   calls instead and say so in the verdict. Don't install browsers or packages.
3. A criterion without an automated test? Add one under tests/ and commit it (git add -A && git commit -m "<key>: QA
   tests"). Do NOT change application code.
4. One verdict per task, each with evidence:
   {cli} qa-pass <key> "what you checked and the evidence"
   {cli} qa-fail <key> "steps to reproduce · expected · actual"
{design}"""


class _Done:
    error, result_text, cost = None, "", 0.0

    def stop(self) -> None:
        pass


class _Alive:
    def __init__(self, alive: bool):
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


def kill_pid(pid) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(int(pid), signal.SIGKILL)
    except Exception:
        pass


class TeamLead(Lead):
    """Deterministic side of an agent-teams run. Each call: finish jobs → design → merge → checks → QA → help →
    developers, then return the assignments the session lead sends to its teammates."""

    def __init__(self, root: Path, cfg: dict, change: Path, a, session_start: bool = False):
        super().__init__(root, cfg, change, a)
        st = self.b.meta().get("tl") or {}
        self.jobs: dict = st.get("jobs", {})
        self.spawned: list = [] if session_start else st.get("spawned", [])
        self.attempts = st.get("attempts", {})
        self.help_rounds = st.get("help_rounds", {})
        self.apps: dict = st.get("apps", {})
        self.no_base: set = set(st.get("no_base", []))
        self.assignments: list = []
        self.done_flags = set(getattr(a, "done", None) or [])
        if session_start:  # teammates of an earlier session are gone: every open job ended without a result
            self.done_flags |= set(self.jobs)
        self.batch_max = int(self.t.get("batch_max", 4))
        self.no_qa = set(self.b.meta().get("no_qa") or [])
        self.designers = ["designer"] if self.design else []
        self.board_url = f"http://127.0.0.1:{self.t.get('board_port', 8765)}"
        self.adir = Path(self.b.path).parent / "assignments"
        self.adir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- state
    def save(self) -> None:
        self.b.set_meta(tl={"jobs": self.jobs, "spawned": self.spawned, "attempts": self.attempts,
                            "help_rounds": self.help_rounds, "apps": self.apps, "no_base": sorted(self.no_base)})

    def busy(self, agent: str) -> bool:
        w = getattr(self, "warming", {}).get(agent)
        return agent in self.jobs or bool(w and w.is_alive())

    def running_keys(self) -> set:
        return {k for j in self.jobs.values() for k in j["keys"]}

    def mockup_url(self, where: str = "integration") -> str:
        if where == "designer":
            return (self.wt("designer") / self.rel / "design" / "mockup.html").as_uri()
        return f"{self.board_url}/design/{(self.design or {}).get('key', 'D-1')}"

    def file_clash(self, t: dict, batch_files: set | None = None) -> str | None:
        mine = set(t["files"])
        for other in self.tasks():
            if other["key"] != t["key"] and other["key"] in self.running_keys() and mine & set(other["files"]):
                return other["key"]
        return None

    # ---------------------------------------------------------------- assignments
    def assign(self, agent: str, role: str, kind: str, keys: list, title: str, body: str, wt: Path) -> None:
        self.fresh_bin(wt)
        cli = f"{BOARD_CLI} --as {agent} --role {'developer' if kind == 'helper' else role}"
        text = (TL_HEADER.format(agent=agent, role=role, wt=wt, cli=cli)
                + "\n" + body.replace(BOARD_CLI + " ", cli + " ")
                + TL_FOOTER.format(keys=", ".join(keys), cli=cli))
        n = len(list(self.adir.glob(f"{agent}-*.md"))) + 1
        f = self.adir / f"{agent}-{n:03d}-{keys[0]}.md"
        f.write_text(text, encoding="utf-8")
        action = "send" if agent in self.spawned else "spawn"
        if action == "spawn":
            self.spawned.append(agent)
        bm = re.search(r"budget:? about (\d+) minutes", text, re.I)
        self.jobs[agent] = {"keys": keys, "kind": kind, "started": time.time(), "budget": int(bm.group(1)) if bm else 0}
        model = self.t["models"].get("helper" if kind == "helper" else role, "sonnet")
        self.assignments.append({
            "agent": agent, "action": action, "agent_type": TL_ROLES[kind], "model": model, "keys": keys,
            "title": title, "file": str(f),
            "message": f"New assignment ({', '.join(keys)}): read {f} and do exactly what it says."})
        status = {"designer": "designing", "helper": "helping"}.get(kind, "working")
        self.b.upsert_agent(agent, status=status, item_key=keys[0], activity=f"assigned: {title[:80]}")
        self.b.event(keys[0], "lead", "assign", f"{agent} ← {kind} {', '.join(keys)}")

    def tasks_text(self, ts: list) -> str:
        out = []
        for t in ts:
            ctx = self.context(t)
            out.append(f"- {t['key']} {t['title']}\n  {ctx['description']}\n  Files: {ctx['files']}\n"
                       f"  Acceptance:\n" + "\n".join("    " + x for x in ctx["acceptance"].splitlines()))
        return "\n".join(out)

    # ---------------------------------------------------------------- 1. finished jobs
    def job_done(self, agent: str, j: dict) -> bool:
        if agent in self.done_flags:
            return True
        ts = [self.b.item(k) or {} for k in j["keys"]]
        kind = j["kind"]
        if kind == "developer":
            return all(t.get("status") != "in_progress" for t in ts)
        if kind == "qa":
            return all(t.get("status") != "in_qa" or (t.get("qa_by") or "").startswith(("pass:", "fail:")) for t in ts)
        if kind == "designer":
            return all(t.get("status") != "in_progress" for t in ts)
        if kind == "helper":
            return all(t.get("status") != "blocked" for t in ts)
        return True

    def finish_jobs(self) -> None:
        for agent, j in list(self.jobs.items()):
            if not self.job_done(agent, j):
                continue
            del self.jobs[agent]
            kind, wt = j["kind"], self.wt(agent)
            if kind in ("developer", "designer"):
                # keep the teammate's unsaved work; the kit scripts the lead refreshed are not part of it
                sh(["git", "add", "-A", "--", ".", ":(exclude).sdlc/bin"], wt)
                if sh(["git", "diff", "--cached", "--quiet"], wt).returncode:
                    sh(["git", "-c", f"user.name={agent}", "-c", "user.email=agents@sdlc.local", "commit", "-qm",
                        f"{j['keys'][0]}: uncommitted work from {agent}"], wt)
            for k in j["keys"]:
                t = self.b.item(k)
                if not t:
                    continue
                if kind in ("developer", "designer") and t["status"] == "in_progress":
                    n = self.attempts[k] = self.attempts.get(k, 0) + 1
                    if n >= self.t["max_dev_attempts"] + 1:
                        self.escalate(t, f"{agent} returned {n} times without finishing it")
                    else:
                        self.b.event(k, "lead", "system", f"{agent} stopped before finishing — it continues next")
                elif kind == "qa":
                    if t["status"] == "in_qa" and not (t.get("qa_by") or "").startswith(("pass:", "fail:")):
                        n = self.attempts[f"qa:{k}"] = self.attempts.get(f"qa:{k}", 0) + 1
                        if n >= 3:
                            self.escalate(t, "QA could not reach a verdict")
                        else:
                            self.b.update(k, "lead", qa_by=None)
                    if (t.get("qa_rounds") or 0) >= self.t["max_qa_rounds"] and t["status"] == "in_progress":
                        self.escalate(t, f"failed QA {t['qa_rounds']} times")
                elif kind == "helper" and t["status"] == "blocked":
                    if self.help_rounds.get(k, 0) >= self.t["max_help_rounds"]:
                        self.escalate(t, "still blocked after team help")
                    else:
                        self.b.update(k, "lead", note=f"{agent} answered — back to {t['assignee']}",
                                      status="in_progress")
            if kind == "qa":
                kill_pid(self.apps.pop(agent, None))
                sh(["git", "checkout", "-q", "--detach"], wt)
            self.b.upsert_agent(agent, status="idle", item_key=None, activity=f"finished {kind} {', '.join(j['keys'])}")

    # ---------------------------------------------------------------- 2. design
    def design_step(self) -> None:
        for d in self.b.items(type_="design"):
            if d["key"] in self.escalated or d["key"] in self.running_keys():
                continue
            if d["status"] in ("todo", "in_progress") and not self.busy("designer"):
                if (d.get("qa_rounds") or 0) > self.d.get("max_rounds", 3):
                    self.escalate(d, f"design sent back {d['qa_rounds']} times")
                    continue
                self.assign_design(d)
            elif d["status"] == "review" and (d.get("qa_by") or "").startswith("approved:"):
                self.merge_branch(d["branch"], [d])
            elif d["status"] == "review" and (self.d.get("approval") == "auto" or self.a.auto_approve_design):
                self.b.approve_design(d["key"], "auto-approved (team.design.approval = auto)", actor="lead")
                self.merge_branch(d["branch"], [self.b.item(d["key"])])

    def assign_design(self, d: dict) -> None:
        wt, branch = self.wt("designer"), d.get("branch") or f"{self.integration}--{d['key']}"
        if d.get("branch"):
            r = sh(["git", "checkout", "-q", branch], wt)
        else:
            sh("git reset -q --hard && git clean -qfd", wt)
            r = sh(["git", "checkout", "-q", "-B", branch, self.integration], wt)
        if r.returncode:
            self.escalate(d, f"could not check out {branch} for the designer: {r.stderr.strip()[:200]}")
            return
        mode = self.design.get("mode", "greenfield")
        rules, steps = DESIGN_MODES[mode]
        ui = [self.b.item(k) for k in self.design.get("ui_tasks", [])]
        ui_text = "\n".join(f"- {t['key']} {t['title']}: " + "; ".join(t["acceptance"]) for t in ui if t) or "- (all UI)"
        changes = [e["text"][9:] for e in self.b.events(item_key=d["key"], limit=80)
                   if e["kind"] == "design" and e["text"].startswith("CHANGES:")]
        feedback = (f"\nTHE HUMAN ASKED FOR CHANGES (round {len(changes)}): {changes[-1]}\n"
                    "Update the design (keep what they did not mention), then design-ready again.\n") if changes else ""
        if not changes and (self.attempts.get(d["key"]) or self.has_work({**d, "status": "in_progress"}, wt)):
            feedback += ("\nYOUR EARLIER WORK IS IN THIS WORKTREE (DESIGN.md, the mockup, the notes): finish only what is "
                         "missing, then commit and run design-ready right away.\n")
        epic = self.b.item(d["parent_key"]) or {}
        body = DESIGN_PROMPT.format(
            epic_title=epic.get("title", ""), mode=mode, evidence=self.design.get("evidence", ""), mode_rules=rules,
            impeccable_steps=steps, wt=wt, branch=branch, rel=self.rel, ui_tasks=ui_text, key=d["key"], cli=BOARD_CLI,
            mockup_url=self.mockup_url("designer"), feedback=feedback,
            budget=max(10, int(self.t["run_timeout_minutes"] * 0.6)),
            critique="; if the impeccable skill is loaded, run its critique and audit on the mockup and fix the findings")
        self.b.update(d["key"], "lead", status="in_progress", branch=branch)
        self.assign("designer", "designer", "designer", [d["key"]], d["title"], body, wt)

    # ---------------------------------------------------------------- 3. merge
    def branch_groups(self, status: str) -> dict:
        groups: dict = {}
        for t in self.tasks():
            if t["status"] == status and t.get("branch") and t["key"] not in self.escalated:
                groups.setdefault(t["branch"], []).append(t)
        return groups

    def complete(self, branch: str) -> bool:
        """Every task on the branch has been handed over (none still being built)."""
        return all(t["status"] in ("in_qa", "done") for t in self.tasks() if t.get("branch") == branch
                   and t["key"] not in self.escalated)

    def merge_step(self) -> None:
        for branch, ts in self.branch_groups("in_qa").items():
            keys = {t["key"] for t in ts}
            deps_merged = all((self.b.item(d) or {}).get("status") == "done"
                              for t in ts for d in t["depends_on"] if d not in keys)
            if self.complete(branch) and deps_merged and all((t.get("qa_by") or "").startswith("pass:") for t in ts) \
                    and not (keys & self.running_keys()):
                self.merge_branch(branch, ts)

    def merge_branch(self, branch: str, ts: list) -> None:
        iwt = self.wt("integration")
        keys = ", ".join(t["key"] for t in ts)
        r = sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "merge", "--no-ff", "-q",
                "-m", f"{keys}: {ts[0]['title']}" + (f" (+{len(ts) - 1})" if len(ts) > 1 else ""), branch], iwt)
        for t in ts if not r.returncode else []:  # QA's added tests
            qa = f"qa/{t['key']}"
            if git(self.root, "rev-parse", "--verify", "--quiet", qa):
                r = sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "merge", "--no-ff",
                        "-q", "-m", f"{t['key']}: QA tests", qa], iwt)
                if r.returncode:
                    break
        if r.returncode:
            sh(["git", "merge", "--abort"], iwt)
            n = self.attempts[f"merge:{branch}"] = self.attempts.get(f"merge:{branch}", 0) + 1
            if n >= 3:
                for t in ts:
                    self.escalate(t, f"merge into {self.integration} failed {n} times")
                return
            for t in ts:
                self.b.set_meta(**{f"conflict:{t['key']}": True})
                self.b.update(t["key"], "lead", note=f"merge conflict with {self.integration}", status="in_progress")
            log(f"merge conflict on {branch} → back to {ts[0].get('assignee')}")
            return
        for t in ts:
            self.b.update(t["key"], "lead", note=f"merged into {self.integration}", status="done")
            self.b.event(t["key"], "lead", "merge", f"merged {branch} into {self.integration}")
            self.b.set_meta(**{f"conflict:{t['key']}": False})
        log(f"merged {keys} into {self.integration}")
        self.warm(self.devs + self.qas)

    # ---------------------------------------------------------------- 4. lead checks for setup-only work
    def check_step(self) -> None:
        for branch, ts in self.branch_groups("in_qa").items():
            if not self.complete(branch) or any(t.get("qa_by") for t in ts) or not all(t["key"] in self.no_qa for t in ts):
                continue
            wt = self.wt("check")
            sh(["git", "checkout", "-q", "--detach", branch], wt)
            self.fresh_bin(wt)
            ok, out = True, "no verify commands configured"
            if not self.a.simulate and any((self.cfg.get("verify") or {}).values()):
                r = sh([sys.executable, ".sdlc/bin/sdlc.py", "verify"], wt, timeout=1800,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()[-600:]
            for t in ts:
                if ok:
                    self.b.event(t["key"], "lead", "qa", "PASS: setup task — verify green (checked by the lead)")
                    self.b.update(t["key"], "lead", qa_by="pass:lead")
                else:
                    self.b.event(t["key"], "lead", "qa", f"FAIL: verify is not green: {out}")
                    self.b.update(t["key"], "lead", status="in_progress", qa_by="fail:lead",
                                  qa_rounds=(t.get("qa_rounds") or 0) + 1)
            if ok:
                self.merge_step()

    # ---------------------------------------------------------------- 5. QA per story
    def qa_step(self) -> None:
        for branch, ts in self.branch_groups("in_qa").items():
            todo = [t for t in ts if not t.get("qa_by")]
            if not todo or not self.complete(branch) or any(t["key"] in self.running_keys() for t in ts):
                continue
            free = [q for q in self.qas if not self.busy(q)]
            if not free:
                return
            self.assign_qa(free[0], branch, todo)
        # QA would sit idle while a developer is still on the rest of its story: test what was handed in already
        # (on its own qa/ branch). The branch still merges only when every task on it has passed.
        for branch, ts in self.branch_groups("in_qa").items():
            testing = {k for j in self.jobs.values() if j["kind"] != "developer" for k in j["keys"]}
            todo = [t for t in ts if not t.get("qa_by") and t["key"] not in testing]  # handed in, even mid-batch
            free = [q for q in self.qas if not self.busy(q)]
            if not free:
                return
            if todo and not self.complete(branch):
                self.assign_qa(free[0], branch, todo)

    def start_app_detached(self, agent: str, wt: Path, port: int) -> str | None:
        cmd = (self.t["qa"].get("start_cmd") or "").strip()
        if not cmd or self.a.simulate:
            return None
        if self.cfg["verify"].get("setup"):
            sh([sys.executable, ".sdlc/bin/sdlc.py", "verify", "setup"], wt, timeout=1800,
               env={**os.environ, "SDLC_SETUP_IF_CHANGED": "1", "PYTHONIOENCODING": "utf-8"})
        for cand in [port + 10 * i for i in range(10)]:
            if port_free(cand):
                port = cand
                break
        kw = dict(NEW_GROUP)
        if os.name == "nt":
            kw["creationflags"] = kw.get("creationflags", 0) | 0x00000008  # DETACHED_PROCESS: outlive this call
        (Path(self.b.path).parent / "logs").mkdir(parents=True, exist_ok=True)
        logf = open(Path(self.b.path).parent / "logs" / f"app-{agent}.log", "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(portable_cmd(cmd).replace("{port}", str(port)), shell=True, cwd=wt, stdout=logf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, "PORT": str(port)}, **kw)
        self.apps[agent] = proc.pid
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + self.t["qa"].get("start_timeout", 60)
        while time.time() < deadline:
            if proc.poll() is not None:
                return url + f" (the app exited at startup — see {logf.name}; report that in your verdicts)"
            try:
                urllib.request.urlopen(url + self.t["qa"].get("health_path", "/"), timeout=2)
                return url
            except urllib.error.HTTPError:
                return url
            except Exception:
                time.sleep(1)
        return url + " (it did not answer the health check yet — retry, and report it if it stays down)"

    def assign_qa(self, agent: str, branch: str, ts: list) -> None:
        wt, qa_branch = self.wt(agent), f"qa/{ts[0]['key']}"
        for t in ts:
            self.b.update(t["key"], "lead", qa_by=agent)
        sh("git reset -q --hard && git clean -qfd", wt)
        r = sh(["git", "checkout", "-q", "-B", qa_branch, branch], wt)
        if r.returncode:
            for t in ts:
                self.escalate(t, f"QA could not check out {branch}: {r.stderr.strip()[:200]}")
            return
        self.fresh_bin(wt)
        port = self.t["qa"]["port_base"] + self.qas.index(agent)
        app = self.start_app_detached(agent, wt, port)
        story = self.b.item(ts[0]["parent_key"]) or {}
        browser = (" with the browser tools (mcp__playwright__*: navigate, snapshot, click, type, screenshot)"
                   if self.t["qa"].get("browser") == "playwright-mcp" else " with Playwright (npx playwright / python)")
        design = (DESIGN_FOR_QA.format(mockup_url=self.mockup_url())
                  if self.design and any(self.design["key"] in t["depends_on"] for t in ts) else "")
        body = TL_QA.format(story_key=story.get("key"), story_title=story.get("title"), branch=qa_branch,
                            dev=ts[0].get("assignee"), tasks=self.tasks_text(ts), py=PY, cli=BOARD_CLI,
                            app=(f"The lead started the app for you at {app}. Stop nothing; the lead stops it."
                                 if app else "No app server is configured (team.qa.start_cmd): test through the "
                                             "test suite and API calls."),
                            browser=browser, design=design,
                            budget=max(5, int(self.t["qa"].get("minutes_per_task", 4)) * len(ts)))
        self.assign(agent, "qa", "qa", [t["key"] for t in ts], f"QA {story.get('key')}: {story.get('title')}", body, wt)

    # ---------------------------------------------------------------- 6. help
    def help_step(self) -> None:
        for t in self.tasks():
            if t["status"] != "blocked" or t["key"] in self.escalated or t["key"] in self.running_keys():
                continue
            helpers = [n for n in self.devs if not self.busy(n) and n != t["assignee"]] or \
                      [n for n in self.qas if not self.busy(n)]
            if not helpers:
                if len(self.devs) == 1 and not self.qas:
                    self.escalate(t, "no teammate available to help")
                continue
            agent = helpers[0]
            reason = next((e["text"].split(": ", 1)[-1] for e in reversed(self.b.events(item_key=t["key"], limit=50))
                           if e["kind"] == "status" and "Blocked" in e["text"]), "see the task history")
            self.help_rounds[t["key"]] = self.help_rounds.get(t["key"], 0) + 1
            ctx = {**self.context(t), "agent": agent, "dev": t["assignee"], "wt": self.wt(t["assignee"]),
                   "reason": reason}
            body = HELP_PROMPT.format(**ctx).replace("Then stop.", "")
            self.assign(agent, "developer" if agent in self.devs else "qa", "helper", [t["key"]],
                        f"help {t['assignee']} on {t['key']}", body, self.wt(agent))

    # ---------------------------------------------------------------- 7. developers, one story batch at a time
    def pick_batch(self, dev: str) -> list:
        busy_keys = self.running_keys()
        open_ts = [t for t in self.tasks() if t["key"] not in self.escalated and t["key"] not in busy_keys]
        back = [t for t in open_ts if t["status"] == "in_progress" and t.get("branch") and t["assignee"] == dev]
        if back:
            b = back[0]["branch"]
            return [t for t in open_ts if t["status"] == "in_progress" and t.get("branch") == b]
        done = {t["key"] for t in self.tasks() if t["status"] == "done"} | \
               {d["key"] for d in self.b.items(type_="design") if d["status"] == "done"}
        if self.t.get("optimistic", True):
            # optimistic start: build on work that is already handed over (in QA) instead of idling until it merges;
            # nothing merges before its dependencies passed QA, and designs still need the human's approval
            done |= {t["key"] for t in self.tasks() if t["status"] == "in_qa" and t.get("branch")
                     and t["key"] not in self.escalated and t["key"] not in self.no_base}
        ready = [t for t in open_ts if t["status"] == "todo" and set(t["depends_on"]) <= done and not self.file_clash(t)]
        if not ready:
            return []
        mine = [t for t in ready if t["assignee"] == dev]
        first = (mine or ready)[0]
        batch, keys = [first], {first["key"]}
        for t in [x for x in open_ts if x["status"] == "todo" and x["parent_key"] == first["parent_key"]]:
            if len(batch) >= self.batch_max:
                break
            if t["key"] not in keys and set(t["depends_on"]) <= done | keys and not self.file_clash(t):
                batch.append(t)
                keys.add(t["key"])
        return sorted(batch, key=lambda t: int(re.sub(r"\D", "", t["key"]) or 0))

    def dev_step(self) -> None:
        for dev in self.devs:
            if self.busy(dev):
                continue
            batch = self.pick_batch(dev) or self.rebalance(dev)
            if batch:
                self.assign_dev(dev, batch)

    # ---------------------------------------------------------------- rebalancing, like a real team lead
    def started(self, agent: str, t: dict) -> bool:
        """Has this developer begun the task? A commit for it, or any of its planned files changed."""
        wt = self.wt(agent)
        subjects = sh(["git", "log", "--format=%s", f"{self.integration}..HEAD"], wt).stdout.splitlines()
        if any(x.startswith(t["key"] + ":") for x in subjects):
            return True
        changed = set(sh(["git", "diff", "--name-only", f"{self.integration}...HEAD"], wt).stdout.split())
        changed |= {ln[3:].strip().strip('"').replace("\\", "/")
                    for ln in sh(["git", "status", "--porcelain", "-uall"], wt).stdout.splitlines()}
        planned = {f.replace("\\", "/").rstrip("/") for f in t.get("files") or []}
        return any(c in planned or any(c.startswith(f + "/") for f in planned) for c in changed)

    def rebalance(self, idle: str) -> list:
        """An idle developer takes tasks another developer was given but has not started — only work that does not
        depend on anything that developer is still building. The busy developer is told to skip them."""
        done = {t["key"] for t in self.tasks() if t["status"] in ("done", "in_qa")} | \
               {d["key"] for d in self.b.items(type_="design") if d["status"] == "done"}
        for agent, j in list(self.jobs.items()):
            if j["kind"] != "developer" or agent == idle or len(j["keys"]) < 2:
                continue
            ts = [t for t in (self.b.item(k) for k in j["keys"]) if t and t["status"] == "in_progress"]
            keep = {t["key"] for t in ts if self.started(agent, t)} or ({ts[0]["key"]} if ts else set())
            # (nothing begun yet → it is on the first one; the rest can still go to the idle developer)
            move = []
            for t in ts:
                if t["key"] in keep:
                    continue
                inside = set(t["depends_on"]) & set(j["keys"])
                if inside and inside <= keep:
                    keep.add(t["key"])  # continues what it is already building: stays, keeps the context
                else:
                    move.append(t)
            takeable = [t for t in move if set(t["depends_on"]) <= done | {m["key"] for m in move}
                        and not (set(t["depends_on"]) & keep)]
            if not takeable:
                continue
            for t in move:
                self.b.update(t["key"], "lead", status="todo", assignee=idle if t in takeable else t["assignee"],
                              branch=None, note=f"moved from {agent} (not started)")
                self.b.event(t["key"], "lead", "assign", f"moved from {agent} to {idle}: {agent} had not started it")
            j["keys"] = [k for k in j["keys"] if k in keep]
            cli = f"{BOARD_CLI} --as {agent} --role developer"
            note = (f"UPDATE from the team lead: {', '.join(t['key'] for t in move)} moved to "
                    f"{idle if takeable else 'later'} because you had not started them. Do NOT build them. Finish only "
                    f"{', '.join(j['keys'])}, submit with {cli} submit <key> \"...\", then message the lead: "
                    f"done {', '.join(j['keys'])}.")
            f = self.adir / f"{agent}-update-{int(time.time())}.md"
            f.write_text(note + "\n", encoding="utf-8")
            self.assignments.append({"agent": agent, "action": "send", "agent_type": TL_ROLES["developer"],
                                     "model": self.t["models"].get("developer", "sonnet"), "keys": j["keys"],
                                     "title": "scope change", "file": str(f), "message": note})
            log(f"rebalanced: {', '.join(t['key'] for t in move)} from {agent} → {idle}")
            return [t for t in (self.b.item(m["key"]) for m in takeable) if t]
        return []

    def assign_dev(self, dev: str, batch: list) -> None:
        wt = self.wt(dev)
        returning = batch[0]["status"] == "in_progress" and batch[0].get("branch")
        branch = batch[0].get("branch") if returning else f"{self.integration}--{batch[0]['key']}"
        if returning:
            r = sh(["git", "checkout", "-q", branch], wt)
            for t in batch:
                if git(self.root, "rev-parse", "--verify", "--quiet", f"qa/{t['key']}"):
                    sh(["git", "merge", "-q", "--no-edit", f"qa/{t['key']}"], wt)
        else:
            sh("git reset -q --hard && git clean -qfd", wt)
            r = sh(["git", "checkout", "-q", "-B", branch, self.integration], wt)
            bases = sorted({d for t in batch for d in t["depends_on"]
                            if (self.b.item(d) or {}).get("status") == "in_qa"} - {t["key"] for t in batch})
            for d in bases:  # optimistic start: bring in the handed-over work this batch builds on
                dep = self.b.item(d)
                m = sh(["git", "-c", "user.name=team-lead", "-c", "user.email=lead@sdlc.local", "merge", "-q",
                        "--no-edit", dep["branch"]], wt)
                if m.returncode:
                    sh(["git", "merge", "--abort"], wt)
                    sh(["git", "checkout", "-q", "-B", branch, self.integration], wt)
                    self.no_base.add(d)
                    self.save()
                    log(f"{', '.join(t['key'] for t in batch)}: can't build on {d} yet (conflict) — waits for its merge")
                    return
            if bases:
                self.b.event(batch[0]["key"], "lead", "system", f"optimistic start on top of {', '.join(bases)} (in QA)")
        if r.returncode:
            for t in batch:
                self.escalate(t, f"could not check out {branch}: {r.stderr.strip()[:200]}")
            return
        extra = ""
        for t in batch:
            fails = [e for e in self.b.events(item_key=t["key"], limit=50) if e["kind"] == "qa" and e["text"].startswith("FAIL")]
            if t["status"] == "in_progress" and fails:
                extra += f"\nQA SENT {t['key']} BACK: {fails[-1]['text'][6:]}\nFix it, re-run its tests, submit again.\n"
            elif returning and (self.attempts.get(t["key"]) or self.has_work(t, wt)):
                extra += f"\n{t['key']}: your earlier work is committed on this branch — continue it, don't start over.\n"
            if self.b.meta().get(f"conflict:{t['key']}"):
                extra += (f"\n{t['key']} CONFLICTS with {self.integration}: run `git merge {self.integration}`, resolve "
                          "keeping both features, run verify, commit, submit again.\n")
        if self.design and any(self.design["key"] in t["depends_on"] for t in batch):
            extra += DESIGN_FOR_DEV.format(rel=self.rel, mockup_url=self.mockup_url(), key=", ".join(
                t["key"] for t in batch), cli=BOARD_CLI)
        story = self.b.item(batch[0]["parent_key"]) or {}
        epic = self.b.item(story.get("parent_key") or "") or {}
        body = TL_DEV.format(story_key=story.get("key"), story_title=story.get("title"), epic_title=epic.get("title"),
                             branch=branch, n=len(batch), tasks=self.tasks_text(batch), extra=extra, rel=self.rel,
                             py=PY, cli=BOARD_CLI, budget=max(10, int(self.t["run_timeout_minutes"] * 0.6)))
        for t in batch:
            self.b.update(t["key"], "lead", status="in_progress", assignee=dev, branch=branch)
        self.assign(dev, "developer", "developer", [t["key"] for t in batch],
                    f"{story.get('key')}: " + ", ".join(t["key"] for t in batch), body, wt)

    # ---------------------------------------------------------------- one step
    def setup_session(self) -> None:
        super().setup()  # worktrees, script sync, stale QA claims, pre-warm
        p = self.wt("check")
        if not (p / ".git").exists():
            sh(["git", "worktree", "add", "--detach", str(p), self.integration], self.root)

    def join_warm(self) -> None:
        """Installs started by setup or a merge run in parallel across worktrees; finish them before anyone gets
        work there (a checkout during an install would race it)."""
        for th in list(getattr(self, "warming", {}).values()):
            th.join(timeout=1800)
        self.warming = {}

    def step(self) -> dict:
        self.finish_jobs()
        self.design_step()
        self.merge_step()
        self.check_step()
        self.join_warm()
        self.qa_step()
        self.help_step()
        self.dev_step()
        self.join_warm()
        if self.sync:
            self.sync.run()
        self.save()
        ts = self.tasks()
        human = []
        for d in self.b.items(type_="design"):
            if d["status"] == "review" and not (d.get("qa_by") or "").startswith("approved:"):
                human.append({"key": d["key"], "what": "design approval", "mockup": f"{self.board_url}/design/{d['key']}",
                              "summary": d.get("note") or ""})
        for k in sorted(self.escalated):
            t = self.b.item(k) or {}
            human.append({"key": k, "what": "escalated", "why": t.get("note") or ""})
        working = [{"agent": a, "keys": j["keys"], "kind": j["kind"],
                    "minutes": int((time.time() - j["started"]) / 60), "budget": j.get("budget", 0)}
                   for a, j in self.jobs.items()]
        finished = bool(ts) and all(t["status"] == "done" or t["key"] in self.escalated for t in ts) and not self.jobs
        if finished:
            self.b.set_meta(status="done" if not self.escalated else "needs-human")
        return {"assignments": self.assignments, "waiting_for_human": human, "working": working,
                "progress": f"{sum(t['status'] == 'done' for t in ts)}/{len(ts)} tasks done",
                "finished": finished, "board": self.board_url}


def tl_args(a):
    for k, v in (("developers", None), ("qa", None), ("simulate", False), ("auto_approve_design", False),
                 ("replan", False), ("pr", False), ("done", [])):
        if not hasattr(a, k):
            setattr(a, k, v)
    return a


def cmd_prepare(root: Path, cfg: dict, a) -> None:
    """Start (or restart) an agent-teams session: plan if needed, worktrees, installs, first assignments."""
    os.environ["SDLC_LOG_STDERR"] = "1"
    a = tl_args(a)
    change = resolve_change(root, cfg, a.change)
    with contextlib.redirect_stdout(sys.stderr):  # stdout carries only the JSON for the session lead
        cmd_plan(root, cfg, a)
    lead = TeamLead(root, cfg, change, a, session_start=True)
    lead.setup_session()
    out = lead.step()
    out["worktrees"] = {n: str(lead.wt(n)) for n in ["integration", *lead.devs, *lead.qas, *lead.designers]}
    out["integration_branch"] = lead.integration
    print(json.dumps(out, indent=2))


def cmd_next(root: Path, cfg: dict, a) -> None:
    """One lead step: finish what teammates completed, merge, hand out the next assignments."""
    os.environ["SDLC_LOG_STDERR"] = "1"
    a = tl_args(a)
    change = resolve_change(root, cfg, a.change)
    lead = TeamLead(root, cfg, change, a)
    out = lead.step()
    print(json.dumps(out, indent=2))


def cmd_wrap(root: Path, cfg: dict, a) -> None:
    """Final integration verify + team report, once every task is done or escalated."""
    a = tl_args(a)
    change = resolve_change(root, cfg, a.change)
    lead = TeamLead(root, cfg, change, a)
    lead.running = {}
    for agent in list(lead.apps):
        kill_pid(lead.apps.pop(agent))
    lead.save()
    sys.exit(lead.wrap_up())



NUDGE = ("Time check from the lead (not a stop): you are {minutes} min into a ~{budget} min assignment on {keys}. "
         "Post one board comment with where you are and what is left. If you are fighting tooling (installs, "
         "browser, ports, shells), stop that and {alt}. Otherwise keep going and finish properly — never skip "
         "a check to save time.")


def cmd_watch(root: Path, cfg: dict, a) -> None:
    """Run in the background next to an agent-teams run. Exits (printing JSON) as soon as a teammate is past its time
    budget, so the lead can nudge it without polling. Each assignment is reported once."""
    stamp = root / ".sdlc" / "state" / "nudged.json"
    deadline = time.time() + max(1, a.max_minutes) * 60
    wt_root = root / cfg["team"].get("worktree_dir", ".sdlc/worktrees")
    start_heads: dict = {}
    while True:
        try:
            seen = json.loads(stamp.read_text(encoding="utf-8")) if stamp.exists() else {}
        except ValueError:
            seen = {}
        b = Board()
        jobs = (b.meta().get("tl") or {}).get("jobs", {})
        # an idle developer while another developer moves on to its next task → `next` may hand over untouched work
        heads = {ag: git(wt_root / ag, "rev-parse", "HEAD") for ag, j in jobs.items() if j.get("kind") == "developer"
                 and len(j.get("keys", [])) > 1}
        idle_dev = any((ag.get("role") or "") == "developer" and ag["name"] not in jobs for ag in b.agents())
        moved = sorted(ag for ag, h in heads.items() if ag in start_heads and h != start_heads[ag])
        start_heads.update({ag: h for ag, h in heads.items() if ag not in start_heads})
        if idle_dev and moved:
            print(json.dumps({"over_budget": [], "work_waiting": moved,
                              "do": "run team.py next <change> (no --done) and relay its assignments"}, indent=2))
            return
        late = []
        # handed-in work waiting while a QA teammate has nothing to do → the lead should run `next` now
        qas = [ag["name"] for ag in b.agents() if (ag.get("role") or "") == "qa"]
        waiting = sorted(t["key"] for t in b.items(type_="task") if t["status"] == "in_qa" and not t.get("qa_by"))
        if waiting and any(q not in jobs for q in qas):
            print(json.dumps({"over_budget": [], "work_waiting": waiting,
                              "do": "run team.py next <change> (no --done) and relay its assignments"}, indent=2))
            return
        for agent, j in jobs.items():
            mins = (time.time() - j.get("started", time.time())) / 60
            budget = j.get("budget") or 0
            if budget and mins > budget * a.grace and seen.get(agent) != j.get("started"):
                alt = ("verify through the tests and API calls instead" if j.get("kind") == "qa"
                       else "mark the task blocked with exactly what you need")
                late.append({"agent": agent, "keys": j.get("keys", []), "minutes": int(mins), "budget": budget,
                             "message": NUDGE.format(minutes=int(mins), budget=budget,
                                                     keys=", ".join(j.get("keys", [])), alt=alt)})
                seen[agent] = j.get("started")
        if late or time.time() > deadline:
            if late:
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(json.dumps(seen), encoding="utf-8")
            print(json.dumps({"over_budget": late, "work_waiting": []}, indent=2))
            return
        time.sleep(max(5, a.every))


TEAM_ALLOW = ["Bash(cd *)", "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git add*)",
              "Bash(git commit*)", "Bash(git checkout*)", "Bash(git merge*)", "Bash(git show*)", "Bash(git rev-parse*)",
              "Bash(python *)", "Bash(python3 *)", "Bash(py *)", "Bash(pytest*)", "Bash(npm *)", "Bash(npx *)",
              "Bash(node *)", "Bash(uv *)", "Bash(pip *)", "Bash(curl *)", "Bash(ls*)", "Bash(cat *)", "Bash(head *)",
              "Bash(tail *)", "Bash(grep *)", "Bash(mkdir *)", "Bash(echo *)", "Bash(.venv/*)", "Bash(.venv\\*)",
              "Read", "Edit", "Write", "Glob", "Grep", "mcp__playwright"]
TEAM_DENY = ["Bash(git push*)", "Bash(git reset --hard*)", "Bash(rm -rf *)"]


def cmd_allow(root: Path, cfg: dict, a) -> None:
    """Pre-approve the commands teammates need (git, python, npm, tests, edits) in .claude/settings.local.json, so they
    don't wait on permission prompts in the lead's window. Run only after the human agreed. Push stays denied."""
    p = root / ".claude" / "settings.local.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    perms = data.setdefault("permissions", {})
    allow, deny = perms.setdefault("allow", []), perms.setdefault("deny", [])
    added = [r for r in TEAM_ALLOW if r not in allow]
    allow += added
    deny += [r for r in TEAM_DENY if r not in deny]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{p}: {len(added)} rules allowed, push/reset --hard/rm -rf denied")


def cmd_run(root: Path, cfg: dict, a) -> None:
    change = resolve_change(root, cfg, a.change)
    b = Board()
    if not b.items(type_="task"):
        log("board is empty — planning first")
    cmd_plan(root, cfg, a)  # plans if needed; reloads an edited breakdown.json while no work has started
    if b.meta().get("change") != change.name:
        sys.exit(f"the board holds {b.meta().get('change')}; run team.py reset first")
    if not shutil.which("claude") and not a.simulate:
        sys.exit("claude CLI not on PATH")
    audit(root, "team-start", change=change.name)
    rc = Lead(root, cfg, change, a).run()
    audit(root, "team-end", change=change.name, rc=rc)
    sys.exit(rc)


def cmd_status(root: Path, cfg: dict, a) -> None:
    b = Board()
    m = b.meta()
    print(f"change {m.get('change')} · {m.get('status')} · tracker {m.get('tracker')} · branch {m.get('integration_branch')}")
    print_tree(b)
    print()
    for ag in b.agents():
        print(f"  {ag['name']:8} {ag['role'] or '':9} {ag['status'] or '':8} {ag['item_key'] or '-':5} "
              f"runs {ag['runs']} ${ag['cost'] or 0:.2f}  {ag['activity'] or ''}"[:160])


def cmd_reset(root: Path, cfg: dict, a) -> None:
    if os.environ.get("CLAUDECODE") or (not a.yes and not sys.stdin.isatty()):
        sys.exit("reset wipes the board; run it yourself in a terminal (or pass --yes)")
    Board().clear()  # in place: also works while the board server holds the file open (Windows)
    print("board cleared (worktrees and branches are kept; remove them with `git worktree remove`)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        s = sub.add_parser(name)
        s.add_argument("change")
        s.add_argument("--developers", type=int)
        s.add_argument("--qa", type=int)
        s.add_argument("--simulate", action="store_true", help="scripted agents, no model calls (demo/test)")
        s.add_argument("--replan", action="store_true")
        s.add_argument("--pr", action="store_true")
        s.add_argument("--auto-approve-design", action="store_true", help="skip the human design gate (demos only)")
    for name in ("prepare", "next", "wrap"):  # Claude Code agent-teams mode (the session is the lead)
        s = sub.add_parser(name)
        s.add_argument("change")
        s.add_argument("--developers", type=int)
        s.add_argument("--qa", type=int)
        s.add_argument("--done", nargs="*", default=[], help="teammates that reported done (or stopped)")
        s.add_argument("--simulate", action="store_true")
        s.add_argument("--auto-approve-design", action="store_true")
    s = sub.add_parser("watch")
    s.add_argument("change", nargs="?")
    s.add_argument("--grace", type=float, default=1.0, help="report at budget × grace")
    s.add_argument("--every", type=int, default=30, help="seconds between checks")
    s.add_argument("--max-minutes", type=int, default=30, help="exit with an empty list after this long")
    sub.add_parser("allow")
    sub.add_parser("status")
    s = sub.add_parser("reset"); s.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "reset": cmd_reset, "prepare": cmd_prepare,
     "next": cmd_next, "wrap": cmd_wrap, "allow": cmd_allow, "watch": cmd_watch}[a.cmd](root, cfg, a)


if __name__ == "__main__":
    main()
