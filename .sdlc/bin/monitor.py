#!/usr/bin/env python3
"""Closing the loop (playbook Stage 6): deterministic control bands that invoke Claude only on a breach.

  python .sdlc/bin/monitor.py [--config sdlc/monitor/bands.yaml] [--metric NAME] [--dry-run]

Detection is pure statistics (rolling mean/std + Western Electric rules), no model involved.
The tier decides what Claude may do:
  log       -> audit line only
  diagnose  -> Claude runs read-only with the tier's tools and writes intent.md (Stage 1 format)
  propose   -> as diagnose, plus it may open a PR into the review gate or run a pre-approved runbook
Run it from cron, a scheduled CI job or a monitoring webhook. A cooldown stops repeat invocations.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, find_root, load_config, read_state, state_path, utcnow  # noqa: E402

TIER_RANK = {"1sigma": 1, "2sigma": 2, "3sigma": 3}
EVAL_WINDOW = 8


def load_bands(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("bands.yaml needs PyYAML (pip install pyyaml) — or use bands.json with the same shape")
    return yaml.safe_load(text)


def read_series(root: Path, src: dict) -> list[float]:
    """Source is {csv: path, column: name} or {cmd: "..."} printing a JSON list or one number per line."""
    if "csv" in src:
        with open(root / src["csv"], newline="", encoding="utf-8") as f:
            return [float(r[src.get("column", "value")]) for r in csv.DictReader(f) if r.get(src.get("column", "value"))]
    out = subprocess.run(src["cmd"], shell=True, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300).stdout
    try:
        data = json.loads(out)
        return [float(x["v"] if isinstance(x, dict) else x) for x in data]
    except json.JSONDecodeError:
        return [float(x) for x in out.split() if x.strip()]


def western_electric(series: list[float], window: int, direction: str) -> tuple[int, list[str], dict]:
    """Return (tier 0..3, rules fired, stats). Baseline excludes the evaluation window."""
    base = series[:-EVAL_WINDOW][-window:]
    recent = series[-EVAL_WINDOW:]
    if len(base) < 10 or len(recent) < 3:
        return 0, ["insufficient data"], {}
    mu, sd = statistics.mean(base), statistics.pstdev(base) or 1e-9
    z = [(x - mu) / sd for x in recent]
    if direction == "up":
        z = [max(v, 0) for v in z]
    elif direction == "down":
        z = [min(v, 0) for v in z]

    def same_side(vals, k, n, thr):
        hi = sum(v > thr for v in vals[-n:]) >= k
        lo = sum(v < -thr for v in vals[-n:]) >= k
        return hi or lo

    tier, fired = 0, []
    if abs(z[-1]) > 3:
        tier, fired = 3, fired + ["R1: latest point beyond 3σ"]
    if same_side(z, 2, 3, 2):
        tier, fired = max(tier, 2), fired + ["R2: 2 of 3 beyond 2σ"]
    if len(z) >= 8 and (all(v > 0 for v in z[-8:]) or all(v < 0 for v in z[-8:])):
        tier, fired = max(tier, 2), fired + ["R4: 8 in a row one side of mean (drift)"]
    if same_side(z, 4, 5, 1):
        tier, fired = max(tier, 1), fired + ["R3: 4 of 5 beyond 1σ"]
    elif abs(z[-1]) > 2 and tier == 0:
        tier, fired = 1, ["latest point beyond 2σ"]
    return tier, fired, {"mean": round(mu, 4), "std": round(sd, 4), "recent": recent, "z": [round(v, 2) for v in z]}


def invoke_claude(root: Path, cfg: dict, m: dict, tier_name: str, tier: dict, fired: list[str], stats: dict,
                  dry: bool) -> None:
    action = tier.get("action", "log")
    tools = [t.strip() for t in str(tier.get("tools", "Read,Grep,Glob")).split(",") if t.strip()]
    tools += ["Read", "Grep", "Glob", "Write", "Bash(git log*)", "Bash(git show*)", "Bash(git diff*)",
              "Bash(python .sdlc/bin/sdlc.py new *)",
              "Bash(python3 .sdlc/bin/sdlc.py new *)"]
    routes = tier.get("routes", [])
    runbooks = m.get("runbooks", {})
    allowed_runbooks = {r.split(":", 1)[1]: runbooks[r.split(":", 1)[1]]
                        for r in routes if r.startswith("runbook:") and r.split(":", 1)[1] in runbooks}
    if action == "propose":
        tools += ["Edit", "MultiEdit", "Bash(git checkout -b *)", "Bash(git add *)", "Bash(git commit *)",
                  "Bash(git push -u origin *)", "Bash(gh pr create *)", "Bash(glab mr create *)"]
        tools += [f"Bash({c})" for c in allowed_runbooks.values()]
    brief = {
        "metric": m["metric"], "tier": tier_name, "action": action, "rules": fired, "stats": stats,
        "owner": m.get("owner"), "allowed_routes": routes, "runbooks": allowed_runbooks,
    }
    prompt = (f"/sdlc:triage --headless --alert '{json.dumps(brief)}'")
    cmd = ["claude", "-p", prompt, "--model", cfg["models"].get("triage", "sonnet"), "--output-format", "json",
           "--permission-mode", "acceptEdits", "--allowedTools", *tools]
    if os.environ.get("SDLC_PLUGIN_DIR"):
        cmd += ["--plugin-dir", os.environ["SDLC_PLUGIN_DIR"]]
    tools = list(dict.fromkeys(tools))
    print(f"  invoking Claude ({action}) with tools: {', '.join(tools)}")
    if dry:
        return
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    try:
        res = json.loads(r.stdout).get("result", "")
    except json.JSONDecodeError:
        res = (r.stderr or r.stdout)[-1000:]
    print("  " + res[-1500:].replace("\n", "\n  "))
    audit(root, "monitor-claude", metric=m["metric"], tier=tier_name, action=action, exit=r.returncode)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="sdlc/monitor/bands.yaml")
    ap.add_argument("--metric")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    bands = load_bands(root / a.config)
    metrics = bands.get("metrics", [bands]) if isinstance(bands, dict) else bands
    st = read_state(root, "monitor.json") or {}

    for m in metrics:
        if a.metric and m["metric"] != a.metric:
            continue
        if m.get("enabled", True) is False and not a.metric:
            print(f"{m['metric']}: disabled (enabled: false)")
            continue
        series = read_series(root, m["source"])
        tier, fired, stats = western_electric(series, int(m.get("baseline", {}).get("window", 30)),
                                              m.get("direction", "both"))
        name = {0: None, 1: "1sigma", 2: "2sigma", 3: "3sigma"}[tier]
        print(f"{m['metric']}: tier={name or 'in-band'} {'; '.join(fired)}")
        if not name:
            continue
        tiers = m.get("tiers", {})
        # use the highest configured tier at or below the detected one
        chosen = next((t for t in ("3sigma", "2sigma", "1sigma") if t in tiers and TIER_RANK[t] <= tier), None)
        if not chosen:
            continue
        audit(root, "band-breach", metric=m["metric"], tier=chosen, rules=fired, stats=stats)
        key = f"{m['metric']}:{chosen}"
        cool = int(m.get("cooldown_minutes", 240)) * 60
        if time.time() - st.get(key, 0) < cool:
            print(f"  cooldown active for {key}; skipping")
            continue
        if tiers[chosen].get("action", "log") != "log":
            invoke_claude(root, cfg, m, chosen, tiers[chosen], fired, stats, a.dry_run)
        st[key] = time.time()

    if not a.dry_run:
        p = state_path(root, "monitor.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2), encoding="utf-8")
    print(f"done {utcnow()}")


if __name__ == "__main__":
    main()
