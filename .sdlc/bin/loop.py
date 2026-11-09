#!/usr/bin/env python3
"""The closed loop, as one long-running command (playbook: "the loop keeps running, human judgement stays above it").

  python .sdlc/bin/loop.py                       # every 5 min: monitor -> run accepted intents -> repeat
  python .sdlc/bin/loop.py --interval 120 --pr   # faster, open a PR/MR per finished change
  python .sdlc/bin/loop.py --once                # one tick (for cron / CI)
  python .sdlc/bin/loop.py --auto-accept-intent  # fully autonomous demo: also accepts draft intents itself
  python .sdlc/bin/loop.py --retry <change>      # clear a change's escalated/done state so the loop picks it up again

Each tick:
  1. monitor.py      deterministic control bands; a breach makes Claude write a new draft intent.md
  2. intake          draft intents wait for a human to set Status: accepted (unless --auto-accept-intent)
  3. pipeline.py     every accepted intent not yet processed runs spec -> gate -> plan -> gate -> build ->
                     verify/fix -> review/fix on its own branch sdlc/<change>
  4. record          done / escalated (ESCALATION.md) in .sdlc/state/loop.json, so nothing runs twice
Humans stay above the loop: accepting intent, resolving escalations, approving and merging PRs, production.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import (audit, changes, find_root, git, load_config, read_state,  # noqa: E402
                       stage_of, state_path, utcnow)

HERE = Path(__file__).resolve().parent
RUNNABLE = {"design-ready": "spec", "design": "spec", "plan-ready": "plan", "plan": "plan", "build": "build"}


def log(msg: str) -> None:
    print(f"[{utcnow()}] {msg}", flush=True)


def save_state(root: Path, st: dict) -> None:
    p = state_path(root, "loop.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")


def default_branch(root: Path, cfg: dict) -> str:
    for b in cfg.get("protected_branches", ["main", "master"]):
        if git(root, "rev-parse", "--verify", "--quiet", b):
            return b
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def accept_intent(change: Path) -> None:
    p = change / "intent.md"
    text = p.read_text(encoding="utf-8").replace("Status: draft", "Status: accepted", 1)
    p.write_text(text.rstrip() + f"\n\n<!-- accepted by loop.py --auto-accept-intent at {utcnow()} -->\n",
                 encoding="utf-8")


def tick(root: Path, cfg: dict, a, st: dict) -> None:
    base = default_branch(root, cfg)
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        log("working tree has uncommitted changes — skipping this tick (commit or stash them)")
        return
    if git(root, "rev-parse", "--abbrev-ref", "HEAD") != base:
        git(root, "checkout", "-q", base)

    # 1. monitor
    bands = root / a.bands
    if bands.is_file() and not a.no_monitor:
        log("monitor: checking control bands")
        subprocess.run([sys.executable, str(HERE / "monitor.py"), "--config", a.bands]
                       + (["--dry-run"] if a.dry_run else []), cwd=root)
        if git(root, "status", "--porcelain"):  # triage wrote a new intent.md on the base branch
            git(root, "add", "-A", cfg["changes_dir"], cfg.get("lessons_dir", "sdlc/lessons"))
            git(root, "commit", "-q", "-m", "intent: from control-band breach (loop)")

    # 2 + 3. intake and run
    queue: list = []
    for c in changes(root, cfg):
        if c.name in st:
            continue
        stage = stage_of(c)
        if stage == "intent" and a.auto_accept_intent:
            accept_intent(c)
            git(root, "add", str(c / "intent.md"))
            git(root, "commit", "-q", "-m", f"intent: auto-accept {c.name} (loop)")
            audit(root, "loop-auto-accept", change=c.name)
            stage = stage_of(c)
        if stage == "intent":
            if a.verbose:
                log(f"{c.name}: waiting for a human to accept intent.md")
            continue
        if stage not in RUNNABLE:
            continue
        if (c / "ESCALATION.md").is_file():
            st[c.name] = {"result": "escalated", "at": utcnow()}
            continue

        frm = RUNNABLE[stage]
        if a.parallel > 1:
            queue.append((c, frm))
            continue
        log(f"{c.name}: running pipeline from {frm}")
        cmd = [sys.executable, str(HERE / "pipeline.py"), c.name, "--from", frm]
        cmd += ["--pr"] if a.pr else []
        cmd += ["--dry-run"] if a.dry_run else []
        cmd += ["--merge-mode", a.merge_mode] if a.merge_mode else []
        started = utcnow()
        r = subprocess.run(cmd, cwd=root)
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        result = {0: "done", 3: "escalated"}.get(r.returncode, f"failed (exit {r.returncode})")
        md = read_state(root, f"merge/{c.name}.json") or {}
        if r.returncode == 0 and md.get("at", "") >= started:  # what the merge gate decided for this run
            result = md.get("result") or md.get("decision") or result
        st[c.name] = {"result": result, "branch": branch, "at": utcnow(),
                      **({"reasons": md.get("reasons", [])[:5]} if md.get("at", "") >= started else {})}
        audit(root, "loop-change", change=c.name, result=result, branch=branch)
        log(f"{c.name}: {result} on {branch}")
        if not a.dry_run:
            save_state(root, st)
        if subprocess.run(["git", "checkout", "-q", base], cwd=root, capture_output=True).returncode:
            log(f"could not switch back to {base} (uncommitted files on {branch}?) — later changes wait until "
                "the working tree is clean")
            return
    if queue:
        run_parallel(root, cfg, a, st, base, queue)


def claim(root: Path, name: str) -> bool:
    """One runner per change, even with several loops or machines on one checkout: the claim file wins."""
    import os
    import socket
    p = state_path(root, f"claims/{name}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = read_state(root, f"claims/{name}.json")
    if cur:
        same_host = cur.get("host") == socket.gethostname()
        try:
            os.kill(int(cur.get("pid", 0)), 0)
            alive = same_host
        except (OSError, ValueError):
            alive = False
        if alive or (not same_host and time.time() - float(cur.get("ts", 0)) < 6 * 3600):
            return False
    p.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "ts": time.time(), "at": utcnow()}),
                 encoding="utf-8")
    return True


def run_parallel(root: Path, cfg: dict, a, st: dict, base: str, queue: list) -> None:
    """Several changes at once, each in its own worktree and branch. Merges stay one at a time (merge_gate locks)."""
    from concurrent.futures import ThreadPoolExecutor
    if not git(root, "ls-files", ".sdlc/config.json"):
        log("parallel mode needs .sdlc/config.json and .sdlc/bin committed (each worktree checks them out) — "
            "commit them, or run without --parallel")
        return
    mine = [(c, f) for c, f in queue if claim(root, c.name)]
    prefix = cfg["pipeline"].get("branch_prefix", "sdlc/")

    def work(c: Path, frm: str) -> tuple[str, dict]:
        wt = root / cfg["team"].get("worktree_dir", ".sdlc/worktrees") / f"loop-{c.name}"
        branch = f"{prefix}{c.name}"
        try:
            if not wt.exists():
                exists = git(root, "rev-parse", "--verify", "--quiet", branch)
                r = subprocess.run(["git", "worktree", "add", "-q", str(wt)] + ([branch] if exists else
                                   ["-b", branch, base]), cwd=root, capture_output=True, text=True)
                if r.returncode:
                    return c.name, {"result": f"failed (worktree: {r.stderr.strip()[:120]})", "at": utcnow()}
            log(f"{c.name}: running pipeline from {frm} in its own worktree")
            cmd = [sys.executable, str(wt / ".sdlc" / "bin" / "pipeline.py"), c.name, "--from", frm]
            cmd += (["--pr"] if a.pr else []) + (["--merge-mode", a.merge_mode] if a.merge_mode else [])
            started = utcnow()
            r = subprocess.run(cmd, cwd=wt)
            result = {0: "done", 3: "escalated"}.get(r.returncode, f"failed (exit {r.returncode})")
            md = read_state(wt, f"merge/{c.name}.json") or {}
            if r.returncode == 0 and md.get("at", "") >= started:
                result = md.get("result") or md.get("decision") or result
            out = {"result": result, "branch": branch, "at": utcnow()}
            if md.get("at", "") >= started:
                out["reasons"] = md.get("reasons", [])[:5]
            return c.name, out
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, capture_output=True)
            state_path(root, f"claims/{c.name}.json").unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=a.parallel) as ex:
        for name, res in ex.map(lambda x: work(*x), mine):
            st[name] = res
            audit(root, "loop-change", change=name, result=res["result"], branch=res.get("branch"), parallel=True)
            log(f"{name}: {res['result']}")
    if not a.dry_run:
        save_state(root, st)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=300, help="seconds between ticks (default 300)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--pr", action="store_true", help="push and open a PR/MR for each finished change")
    ap.add_argument("--auto-accept-intent", action="store_true",
                    help="accept draft intents automatically (removes the product-owner gate — demos only)")
    ap.add_argument("--bands", default="sdlc/monitor/bands.yaml")
    ap.add_argument("--no-monitor", action="store_true")
    ap.add_argument("--retry", metavar="CHANGE")
    ap.add_argument("--merge-mode", choices=["off", "trial", "auto"],
                    help="merge decision per change (default: config pipeline.merge_mode, 'trial')")
    ap.add_argument("--skip-doctor", action="store_true", help="start even if `doctor` finds a blocking problem")
    ap.add_argument("--parallel", type=int, default=1, metavar="N",
                    help="work on up to N changes at once, each in its own worktree (merges stay one at a time)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    st = read_state(root, "loop.json") or {}
    if a.retry:
        hit = [k for k in st if a.retry in k]
        for k in hit:
            st.pop(k)
        save_state(root, st)
        print(f"cleared: {hit or 'nothing matched'}")
        return

    if not a.skip_doctor and not a.dry_run:
        import doctor
        bad = [c for c in doctor.run_checks(root, need_claude=True) if c["status"] == "fail"]
        if bad:
            for c in bad:
                print(f"✗ {c['name']}: {c['detail']}" + (f"\n   fix: {c['fix']}" if c.get("fix") else ""))
            sys.exit("doctor found problems that would stop the loop — fix them, or run with --skip-doctor")
    audit(root, "loop-start", interval=a.interval, auto_accept=a.auto_accept_intent)
    log(f"loop started (interval {a.interval}s{', auto-accept intent' if a.auto_accept_intent else ''}) — Ctrl+C to stop")
    try:
        while True:
            tick(root, cfg, a, st)
            hb = state_path(root, "loop-heartbeat.json")
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(json.dumps({"at": utcnow(), "interval": a.interval, "parallel": a.parallel}), encoding="utf-8")
            if a.once:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log("loop stopped")
    if not a.dry_run:
        save_state(root, st)


if __name__ == "__main__":
    main()
