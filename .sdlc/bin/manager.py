#!/usr/bin/env python3
"""The manager: steps back every few hours and keeps the factory itself healthy.

  python .sdlc/bin/manager.py            # report
  python .sdlc/bin/manager.py --fix      # also do the safe fixes
  python .sdlc/bin/manager.py --json

It checks:
  loop not running   no heartbeat from loop.py for 3× its interval
  failed runs        a change whose pipeline crashed ("failed (exit N)"): --fix retries it once, then reports it
  waiting for you    escalated changes and merges handed over to you, and how long they have waited
  starved            accepted work is waiting but nothing merged for manager.starved_hours (default 8)
  revert failed      main went red after a merge and the revert failed: fix main by hand (urgent)
  stuck teammates    an agent-team assignment open for 3× its time budget
  housekeeping       stale git locks and worktree records (--fix removes them)
Report: .sdlc/state/manager.json. Problems are sent to your phone (notify, event "manager").
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, changes, find_root, load_config, read_state, stage_of, state_path, utcnow  # noqa: E402


def _age_h(iso: str) -> float:
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0
    return (time.time() - t) / 3600


def run(root: Path, cfg: dict, fix: bool = False) -> dict:
    mc = {"starved_hours": 8, **(cfg.get("manager") or {})}
    mst = read_state(root, "manager-state.json") or {"retries": {}}
    loop = read_state(root, "loop.json") or {}
    found = []
    add = lambda level, what, detail: found.append({"level": level, "what": what, "detail": detail})

    hb = read_state(root, "loop-heartbeat.json")
    if hb and _age_h(hb.get("at", "")) * 3600 > 3 * max(60, int(hb.get("interval", 300))):
        add("warn", "loop not running", f"last tick {_age_h(hb['at']):.1f} h ago (interval {hb.get('interval')} s)")

    for name, r in sorted(loop.items()):
        res = str(r.get("result", ""))
        if res.startswith("failed"):
            n = mst["retries"].get(name, 0)
            if fix and n < 1:
                loop.pop(name)
                mst["retries"][name] = n + 1
                add("info", "retrying a failed run", f"{name}: {res} — queued again (retry {n + 1})")
            else:
                add("warn", "failed run", f"{name}: {res} — see .sdlc/audit.jsonl; retried {n}x")
        elif res == "escalated":
            add("info", "waiting for you", f"{name}: escalated {_age_h(r.get('at', '')):.1f} h ago — see its ESCALATION.md")

    merged_recent = False
    md = state_path(root, "merge")
    for p in sorted(md.glob("*.json")) if md.is_dir() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        res = d.get("result") or ""
        if res == "handed over":
            add("info", "waiting for you", f"{d['change']}: merge handed over {_age_h(d.get('at', '')):.1f} h ago — "
                                           + "; ".join(d.get("reasons", [])[:2]))
        if "revert failed" in res:
            add("urgent", "revert failed", f"{d['change']}: main is red and the automatic revert failed — fix main by hand")
        if res.startswith("merged") and _age_h(d.get("at", "")) < mc["starved_hours"]:
            merged_recent = True
    queued = [c.name for c in changes(root, cfg) if stage_of(c) in ("design-ready", "plan-ready", "build", "plan",
                                                                     "design") and c.name not in loop]
    if queued and not merged_recent and loop:
        add("warn", "starved", f"{len(queued)} accepted change(s) waiting, nothing merged in {mc['starved_hours']} h: "
                               + ", ".join(queued[:5]))

    try:
        from board import Board
        jobs = (Board().meta().get("tl") or {}).get("jobs") or {}
    except Exception:
        jobs = {}
    for agent, j in jobs.items():
        mins = (time.time() - j.get("started", time.time())) / 60
        if j.get("budget") and mins > 3 * j["budget"]:
            add("warn", "stuck teammate", f"{agent} on {', '.join(j.get('keys', []))}: {int(mins)} min, budget {j['budget']}")

    import doctor
    locks = doctor.stale_locks(root)
    if locks:
        if fix:
            for f in locks:
                try:
                    f.unlink()
                except OSError:
                    pass
        add("info" if fix else "warn", "stale git locks", ("removed: " if fix else "") + ", ".join(f.name for f in locks))
    if fix:
        import subprocess
        subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)
        st = state_path(root, "loop.json")
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps(loop, indent=2), encoding="utf-8")
    rep = {"at": utcnow(), "findings": found}
    for name, p in (("manager.json", rep), ("manager-state.json", mst)):
        f = state_path(root, name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(p, indent=2), encoding="utf-8")
    audit(root, "manager", findings=len(found), urgent=sum(x["level"] == "urgent" for x in found))
    alerts = [x for x in found if x["level"] in ("warn", "urgent")]
    if alerts:
        try:
            import notify
            notify.send(root, cfg, "manager", "🧭 Factory check: " + ", ".join(sorted({x["what"] for x in alerts})),
                        "\n".join(f"- {x['what']}: {x['detail']}" for x in alerts)[:3000], key=rep["at"][:13])
        except Exception:
            pass
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    rep = run(root, load_config(root), a.fix)
    if a.json:
        print(json.dumps(rep, indent=2))
        return
    icon = {"info": "·", "warn": "⚠", "urgent": "✗"}
    for x in rep["findings"]:
        print(f"{icon[x['level']]} {x['what']}: {x['detail']}")
    if not rep["findings"]:
        print("✓ the factory is healthy")


if __name__ == "__main__":
    main()
