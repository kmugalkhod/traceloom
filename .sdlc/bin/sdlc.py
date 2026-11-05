#!/usr/bin/env python3
"""Repo-local helper for the AI-Native SDLC loop. Lives in .sdlc/bin/ after /sdlc:init.

  python .sdlc/bin/sdlc.py new <slug> [--title "..."]      start a change folder with intent.md
  python .sdlc/bin/sdlc.py status [--json]                  where every change sits in the loop
  python .sdlc/bin/sdlc.py metrics [--json]                 playbook leading/lagging indicators from git
  python .sdlc/bin/sdlc.py verify [setup|build|test|lint ...] install deps, then build/test/lint; show output
  python .sdlc/bin/sdlc.py fix-mode on|off|status [--change X] [--lock FILE ...]
  python .sdlc/bin/sdlc.py approve-release <ticket> [--approver NAME]   (human terminal only)
  python .sdlc/bin/sdlc.py revoke-release

Human-only actions (fix-mode off, approve-release) require an interactive terminal, so the agent's
Bash tool cannot perform them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import (audit, changes, find_root, git, header_field, load_config, read_state,  # noqa: E402
                       portable_cmd, setup_signature, stage_of, state_path, utcnow)

NEXT_STEP = {
    "intent": "product owner reviews intent.md, sets Status: accepted -> /sdlc:spec",
    "design-ready": "/sdlc:spec",
    "design": "resolve flagged concerns, set spec Status: accepted -> /sdlc:plan",
    "plan-ready": "/sdlc:plan (plan mode)",
    "plan": "engineer interrogates plan.md, sets Status: accepted -> /sdlc:build",
    "build": "/sdlc:build, then /sdlc:review",
    "review": "open PR/MR; /sdlc:babysit until green; code owner approves",
    "rejected": "closed",
    "empty": "/sdlc:intent",
}


def human_only(action: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("CLAUDECODE"):
        sys.exit(f"'{action}' must be run by a human in their own terminal, not by the agent.")


def cmd_new(root: Path, cfg: dict, a) -> None:
    slug = re.sub(r"[^a-z0-9]+", "-", a.slug.lower()).strip("-")
    d = root / cfg["changes_dir"] / f"{date.today().isoformat()}-{slug}"
    if d.exists():
        sys.exit(f"exists: {d.relative_to(root)}")
    d.mkdir(parents=True)
    tmpl = root / ".sdlc" / "templates" / "intent.md"
    text = tmpl.read_text(encoding="utf-8") if tmpl.is_file() else "# Intent: {title}\n"
    author = git(root, "config", "user.name") or os.environ.get("USER") or "unknown"
    text = (text.replace("{title}", a.title or slug.replace("-", " "))
                .replace("{author}", author).replace("{date}", date.today().isoformat()))
    (d / "intent.md").write_text(text, encoding="utf-8")
    print(d.relative_to(root).as_posix())


def cmd_status(root: Path, cfg: dict, a) -> None:
    rows = []
    for c in changes(root, cfg):
        st = stage_of(c)
        rows.append({"change": c.name, "stage": st,
                     "intent": header_field(c / "intent.md", "Status"),
                     "spec": header_field(c / "spec.md", "Status"),
                     "plan": header_field(c / "plan.md", "Status"),
                     "next": NEXT_STEP.get(st, "")})
    if a.json:
        print(json.dumps(rows, indent=2)); return
    if not rows:
        print("no changes yet — start one with /sdlc:intent"); return
    w = max(len(r["change"]) for r in rows)
    print(f"{'CHANGE'.ljust(w)}  {'STAGE':13} NEXT")
    for r in rows:
        print(f"{r['change'].ljust(w)}  {r['stage']:13} {r['next']}")


def _commit_times(root: Path, rel: str) -> list[datetime]:
    out = git(root, "log", "--format=%aI", "--reverse", "--", rel)
    return [datetime.fromisoformat(x.replace("Z", "+00:00")) for x in out.splitlines() if x]


def _hours(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 3600, 1) if a and b else None


def cmd_metrics(root: Path, cfg: dict, a) -> None:
    """Indicators named in the playbook, read straight from git history of the artifacts."""
    out = []
    for c in changes(root, cfg):
        rel = c.relative_to(root).as_posix()
        t = {k: _commit_times(root, f"{rel}/{k}.md") for k in ("intent", "spec", "plan")}
        first = {k: (v[0] if v else None) for k, v in t.items()}
        out.append({
            "change": c.name,
            "stage": stage_of(c),
            "intent_to_spec_h": _hours(first["intent"], first["spec"]),
            "spec_to_plan_h": _hours(first["spec"], first["plan"]),
            "intent_edits_after_spec": sum(1 for x in t["intent"] if first["spec"] and x > first["spec"]),
            "spec_rework_after_plan": sum(1 for x in t["spec"] if first["plan"] and x > first["plan"]),
            "plan_revisions": max(len(t["plan"]) - 1, 0),
        })
    if a.json:
        print(json.dumps(out, indent=2)); return
    if not out:
        print("no committed changes yet"); return
    hdr = ["change", "stage", "intent_to_spec_h", "spec_to_plan_h", "intent_edits_after_spec",
           "spec_rework_after_plan", "plan_revisions"]
    print("\t".join(hdr))
    for r in out:
        print("\t".join("-" if r[h] is None else str(r[h]) for h in hdr))
    accepted = [r for r in out if r["stage"] not in ("intent", "rejected", "empty")]
    decided = [r for r in out if r["stage"] not in ("intent", "empty")]
    if decided:
        print(f"\nintent survival rate: {len(accepted)}/{len(decided)} = {len(accepted)/len(decided):.0%}")


def cmd_verify(root: Path, cfg: dict, a) -> None:
    steps = a.steps or [k for k, v in cfg["verify"].items() if v]
    if not steps:
        sys.exit("no verify commands configured in .sdlc/config.json -> verify")
    failed = []
    for k in steps:
        cmd = cfg["verify"].get(k)
        if not cmd:
            continue
        if k == "setup" and (not a.steps or os.environ.get("SDLC_SETUP_IF_CHANGED")) \
                and not os.environ.get("SDLC_FORCE_SETUP"):
            sig = setup_signature(root, cmd)
            if sig and (read_state(root, "setup.json") or {}).get("sig") == sig:
                print(f"\n=== setup: SKIP (dependencies unchanged since the last install; "
                      f"`verify setup` forces it)", flush=True)
                continue
        run_cmd = portable_cmd(cmd)
        print(f"\n=== {k}: {run_cmd}", flush=True)
        r = subprocess.run(run_cmd, shell=True, cwd=root)
        print(f"=== {k}: {'PASS' if r.returncode == 0 else f'FAIL (exit {r.returncode})'}", flush=True)
        if r.returncode:
            failed.append(k)
        elif k == "setup":
            sig = setup_signature(root, cmd)
            if sig:
                sp = state_path(root, "setup.json")
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_text(json.dumps({"sig": sig, "at": utcnow()}), encoding="utf-8")
    sys.exit(1 if failed else 0)


def cmd_fix_mode(root: Path, cfg: dict, a) -> None:
    p = state_path(root, "fix-mode.json")
    if a.action == "on":
        p.parent.mkdir(parents=True, exist_ok=True)
        st = {"since": utcnow(), "change": a.change, "locked_files": a.lock or []}
        p.write_text(json.dumps(st, indent=2), encoding="utf-8")
        audit(root, "fix-mode-on", **st)
        print(f"fix mode ON — test files locked ({', '.join(a.lock or ['test_globs'])}). "
              "Only a human can turn it off: python .sdlc/bin/sdlc.py fix-mode off")
    elif a.action == "off":
        human_only("fix-mode off")
        if p.exists():
            p.unlink()
        audit(root, "fix-mode-off")
        print("fix mode OFF")
    else:
        print(json.dumps(read_state(root, "fix-mode.json") or {"fix_mode": "off"}, indent=2))


def cmd_approve(root: Path, cfg: dict, a) -> None:
    human_only("approve-release")
    approver = a.approver or git(root, "config", "user.name") or os.environ.get("USER") or "unknown"
    st = {"approver": approver, "reference": a.reference, "granted_at": utcnow(),
          "ttl_minutes": cfg["prod_gate"].get("approval_ttl_minutes", 120)}
    p = state_path(root, "release-approval.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")
    audit(root, "release-approved", **st)
    print(f"release authorised by {approver} ({a.reference}) for {st['ttl_minutes']} min")


def cmd_revoke(root: Path, cfg: dict, a) -> None:
    p = state_path(root, "release-approval.json")
    if p.exists():
        p.unlink()
    audit(root, "release-revoked")
    print("release authorisation revoked")


def main() -> None:
    ap = argparse.ArgumentParser(prog="sdlc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new"); s.add_argument("slug"); s.add_argument("--title")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("metrics"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("verify"); s.add_argument("steps", nargs="*")
    s = sub.add_parser("fix-mode"); s.add_argument("action", choices=["on", "off", "status"])
    s.add_argument("--change"); s.add_argument("--lock", nargs="*")
    s = sub.add_parser("approve-release"); s.add_argument("reference"); s.add_argument("--approver")
    sub.add_parser("revoke-release")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first (.sdlc/config.json missing)")
    cfg = load_config(root)
    {"new": cmd_new, "status": cmd_status, "metrics": cmd_metrics, "verify": cmd_verify,
     "fix-mode": cmd_fix_mode, "approve-release": cmd_approve, "revoke-release": cmd_revoke}[a.cmd](root, cfg, a)


if __name__ == "__main__":
    main()
