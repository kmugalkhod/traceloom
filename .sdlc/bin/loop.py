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
        log(f"{c.name}: running pipeline from {frm}")
        cmd = [sys.executable, str(HERE / "pipeline.py"), c.name, "--from", frm]
        cmd += ["--pr"] if a.pr else []
        cmd += ["--dry-run"] if a.dry_run else []
        r = subprocess.run(cmd, cwd=root)
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        result = {0: "done", 3: "escalated"}.get(r.returncode, f"failed (exit {r.returncode})")
        st[c.name] = {"result": result, "branch": branch, "at": utcnow()}
        audit(root, "loop-change", change=c.name, result=result, branch=branch)
        log(f"{c.name}: {result} on {branch}")
        if not a.dry_run:
            save_state(root, st)
        if subprocess.run(["git", "checkout", "-q", base], cwd=root, capture_output=True).returncode:
            log(f"could not switch back to {base} (uncommitted files on {branch}?) — later changes wait until "
                "the working tree is clean")
            return


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

    audit(root, "loop-start", interval=a.interval, auto_accept=a.auto_accept_intent)
    log(f"loop started (interval {a.interval}s{', auto-accept intent' if a.auto_accept_intent else ''}) — Ctrl+C to stop")
    try:
        while True:
            tick(root, cfg, a, st)
            if a.once:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log("loop stopped")
    if not a.dry_run:
        save_state(root, st)


if __name__ == "__main__":
    main()
