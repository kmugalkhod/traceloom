#!/usr/bin/env python3
"""The team supervisor: watches every teammate's live session and steps in when one is going in circles.

  python .sdlc/bin/supervisor.py            # one pass: print what it sees
  python .sdlc/bin/supervisor.py --json

It reads Claude Code's own session records (the same ones the agent panel shows) and the board, and finds:
  looping       the same command failing 3+ times in a row of recent actions
  rereading     the same file read 4+ times
  tooling       installs, ports or browsers failing again and again (the environment, not the code)
  silent_done   everything handed in (or every verdict given), but the lead was never told
  stalled       no action at all for team.stall_minutes while an assignment is open
Modes (team.supervisor.mode): off · trial (default: it only notes on the board what it would say) · act (the
lead relays each message to the teammate; `team.py watch` wakes the lead for it). With team.supervisor.model set
(e.g. "haiku"), a model reads the last actions and writes the hint; otherwise a plain rule-based hint is used.
It never assigns work itself — idle teammates get work from `team.py next` — and it can't merge or approve.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, find_root, load_config, read_state, state_path  # noqa: E402

TOOLING = re.compile(r"\b(npm (ci|i|install)|pip3? install|uv (sync|pip)|playwright install|poetry install)\b|"
                     r"EADDRINUSE|address already in use|ENOENT.*\.venv|No module named pip|command not found", re.I)
MSG = {
    "looping": "Supervisor: you've run `{cmd}` {n} times and it keeps failing ({err}). Stop retrying it as is: read "
               "the error, change one thing, or run only the failing test. If it's the environment, not the code, "
               "mark the task blocked with the exact error.",
    "rereading": "Supervisor: you've read {file} {n} times. It won't change — note what you need from it once, then "
                 "write the code.",
    "tooling": "Supervisor: the tooling keeps failing ({err}). Don't fight it: verify another way, or mark the task "
               "blocked with the exact error so a human fixes the setup.",
    "silent_done": "{agent} has handed in {keys} but never told the lead.",
    "stalled": "Supervisor: no activity from you for {m} min on {keys}. Post a board comment with where you are, or "
               "mark the task blocked with what you need.",
}


def _ts(e: dict) -> float:
    try:
        return datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _norm(cmd: str) -> str:
    cmd = re.sub(r'^\s*cd\s+("[^"]*"|\S+)\s*&&\s*', "", cmd or "")
    return " ".join(cmd.split())[:200]


def actions(root: Path, agent: str, since: float, tail_bytes: int = 400_000) -> list[dict]:
    """The agent's recent tool calls (newest last): tool, target, failed, error line, time."""
    import cost_report
    want = os.path.normcase(os.path.abspath(str(root)))
    out = []
    for f in cost_report.base_dir().glob("*/**/*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
            s = cost_report._scan(f)
        except OSError:
            continue
        cwd = os.path.normcase(os.path.abspath(s["cwd"])) if s["cwd"] else ""
        if s["agent"] != agent or not (cwd == want or cwd.startswith(want.rstrip("\\/") + os.sep)):
            continue
        with open(f, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - tail_bytes))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()[1:]
        uses, results = {}, {}
        for ln in lines:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            content = (e.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_use":
                    uses[c.get("id")] = {"ts": _ts(e), "tool": c.get("name"), "input": c.get("input") or {}}
                elif c.get("type") == "tool_result":
                    body = c.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        x.get("text", "") for x in body or [] if isinstance(x, dict))
                    results[c.get("tool_use_id")] = {"error": bool(c.get("is_error")) or bool(
                        re.search(r"^Exit code [1-9]", text, re.M)), "text": text}
        for uid, u in uses.items():
            r = results.get(uid, {})
            inp = u["input"]
            target = _norm(inp.get("command", "")) if u["tool"] == "Bash" else str(
                inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
            err = next((ln.strip() for ln in (r.get("text") or "").splitlines()[::-1]
                        if ln.strip() and not ln.startswith("Exit code")), "")[:160]
            out.append({"ts": u["ts"], "tool": u["tool"], "target": target, "failed": r.get("error", False),
                        "error": err, "done": bool(r)})
    return sorted(out, key=lambda a: a["ts"])


def detect(agent: str, acts: list[dict], job: dict, board, stall_min: float, now: float) -> list[dict]:
    found = []
    recent = [a for a in acts if a["ts"] >= job.get("started", 0) - 5][-30:]
    bash = [a for a in recent if a["tool"] == "Bash"][-25:]
    fails: dict = {}
    for a in bash:
        if a["failed"]:
            fails.setdefault(a["target"], []).append(a)
    loop = max(fails.items(), key=lambda kv: len(kv[1]), default=(None, []))
    if loop[0] and len(loop[1]) >= 3:
        kind = "tooling" if TOOLING.search(loop[0] + " " + loop[1][-1]["error"]) else "looping"
        found.append({"kind": kind, "detail": f"{loop[0][:80]} failed {len(loop[1])}x",
                      "vars": {"cmd": loop[0][:80], "n": len(loop[1]), "err": loop[1][-1]["error"] or "no output"}})
    elif sum(TOOLING.search(a["target"] + " " + a["error"]) is not None for a in bash if a["failed"]) >= 3:
        last = [a for a in bash if a["failed"]][-1]
        found.append({"kind": "tooling", "detail": "setup commands failing", "vars": {"err": last["error"] or last["target"][:80]}})
    reads: dict = {}
    for a in recent:
        if a["tool"] == "Read" and a["target"]:
            reads[a["target"]] = reads.get(a["target"], 0) + 1
    top = max(reads.items(), key=lambda kv: kv[1], default=(None, 0))
    if top[1] >= 4:
        found.append({"kind": "rereading", "detail": f"read {Path(top[0]).name} {top[1]}x",
                      "vars": {"file": Path(top[0]).name, "n": top[1]}})
    keys = job.get("keys") or []
    items = [board.item(k) or {} for k in keys]
    kind = job.get("kind")
    handed = items and (all(i.get("status") in ("in_qa", "done") for i in items) if kind == "developer" else
                        all((i.get("qa_by") or "").startswith(("pass:", "fail:")) or i.get("status") != "in_qa"
                            for i in items) if kind == "qa" else
                        all(i.get("status") in ("review", "done") for i in items) if kind == "designer" else False)
    if handed and now - job.get("started", now) > 60:
        found.append({"kind": "silent_done", "detail": f"{', '.join(keys)} handed in", "to": "lead",
                      "do": f"team.py next <change> --done {agent}", "vars": {"agent": agent, "keys": ", ".join(keys)}})
    last = acts[-1]["ts"] if acts else 0
    if not handed and now - max(last, job.get("started", now)) > stall_min * 60:
        found.append({"kind": "stalled", "detail": f"silent {int((now - max(last, job['started'])) / 60)} min",
                      "vars": {"m": int((now - max(last, job["started"])) / 60), "keys": ", ".join(keys)}})
    return found


def smart_hint(root: Path, cfg: dict, agent: str, f: dict, acts: list[dict]) -> str:
    model = ((cfg.get("team") or {}).get("supervisor") or {}).get("model") or ""
    if not model or f["kind"] in ("silent_done", "stalled"):
        return ""
    try:
        from pipeline import claude
        steps = "\n".join(f"- {a['tool']} {a['target'][:120]}" + (f" → FAILED: {a['error']}" if a["failed"] else "")
                          for a in acts[-20:])
        text = claude(f"You supervise {agent}, an AI developer on a team. Its last actions:\n{steps}\n\nIt looks like "
                      f"it is {f['kind']} ({f['detail']}). In at most two sentences, tell it the one thing to do "
                      "differently. Be concrete. Reply with only the advice.", model, ["Read"], root)
        return "Supervisor: " + " ".join(text.split())[:400] if text.strip() else ""
    except Exception:
        return ""


def check(root: Path, cfg: dict, board=None, now: float | None = None) -> list[dict]:
    """One pass. Returns NEW findings (each reported once per assignment). In trial mode they only go to the board."""
    sup = {"mode": "trial", **((cfg.get("team") or {}).get("supervisor") or {})}
    if sup["mode"] == "off":
        return []
    if board is None:
        from board import Board
        board = Board()
    now = now or time.time()
    jobs = (board.meta().get("tl") or {}).get("jobs") or {}
    seen = read_state(root, "supervisor.json") or {}
    stall = float((cfg.get("team") or {}).get("stall_minutes", 12))
    new = []
    for agent, job in jobs.items():
        acts = actions(root, agent, job.get("started", now) - 3600)
        for f in detect(agent, acts, job, board, stall, now):
            fid = f"{agent}:{f['kind']}:{int(job.get('started', 0))}"
            if fid in seen:
                continue
            seen[fid] = now
            msg = smart_hint(root, cfg, agent, f, acts) or MSG[f["kind"]].format(**f["vars"])
            item = {"agent": agent, "kind": f["kind"], "detail": f["detail"], "message": msg,
                    "to": f.get("to", agent), "mode": sup["mode"], **({"do": f["do"]} if f.get("do") else {})}
            whom = "the lead" if item["to"] == "lead" else agent
            verb = "told" if sup["mode"] == "act" else "would tell"
            board.event((job.get("keys") or [None])[0], "supervisor", "comment", f"🧭 {verb} {whom}: {msg}")
            audit(root, "supervisor", agent=agent, kind=f["kind"], mode=sup["mode"])
            new.append(item)
    p = state_path(root, "supervisor.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(list(seen.items())[-500:])), encoding="utf-8")
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    found = check(root, load_config(root))
    if a.json:
        print(json.dumps(found, indent=2))
    else:
        for f in found:
            print(f"{f['agent']:9} {f['kind']:12} {f['detail']}\n          → {f['message']}")
        if not found:
            print("nothing new: every teammate is either working normally or already told")


if __name__ == "__main__":
    main()
