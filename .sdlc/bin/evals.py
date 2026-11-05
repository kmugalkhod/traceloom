#!/usr/bin/env python3
"""Continuous evals for the agent's configuration (playbook Stage 4). Regression-tests CLAUDE.md,
.claude/ (skills, agents, hooks, settings) and REVIEW.md the way code is tested.

  python .sdlc/bin/evals.py [--case ID ...] [--threshold 0.9] [--model sonnet] [--keep] [--json out.json]

Each case is a JSON file in evals.dir (default evals/cases/):
  {
    "id": "status-endpoint-auth",
    "prompt": "Add GET /claims/{id}/status behind the existing auth ...",
    "ref": "HEAD",                                   optional git ref the task starts from
    "setup": ["make deps"],                          optional shell commands before the run
    "allowed_tools": ["Read","Grep","Glob","Edit","Write","Bash(make test)"],
    "checks": [{"name": "tests pass", "cmd": "make test"},
               {"name": "no anonymous routes", "cmd": "! grep -rn 'allow_anonymous' api/"}],
    "must_not_change": ["tests/**", "src/gen/**"],
    "max_turns": 40
  }
Every case runs in its own throwaway git worktree, with the CURRENT working-tree CLAUDE.md, REVIEW.md
and .claude/ copied in, so an uncommitted config change is what gets tested. Exit 1 if the pass
rate is below the threshold (use it as a merge check).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, find_root, git, load_config, matches_any, utcnow  # noqa: E402

CONFIG_PATHS = ["CLAUDE.md", "REVIEW.md", ".claude"]


def sh(cmd: str, cwd: Path, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def run_case(root: Path, case: dict, model: str, keep: bool) -> dict:
    wt = Path(tempfile.mkdtemp(prefix=f"sdlc-eval-{case['id']}-"))
    res = {"id": case["id"], "passed": False, "checks": [], "changed": [], "seconds": 0, "error": None}
    t0 = time.time()
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), case.get("ref", "HEAD")], cwd=root,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        for p in CONFIG_PATHS:  # test the working-tree configuration, committed or not
            src, dst = root / p, wt / p
            if src.is_dir():
                shutil.rmtree(dst, ignore_errors=True); shutil.copytree(src, dst)
            elif src.is_file():
                shutil.copy2(src, dst)
        if (root / ".sdlc").is_dir():
            shutil.copytree(root / ".sdlc", wt / ".sdlc", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("state", "audit.jsonl"))
        for s in case.get("setup", []):
            r = sh(s, wt)
            if r.returncode:
                raise RuntimeError(f"setup failed: {s}\n{r.stderr[-1500:]}")
        git(wt, "add", "-A"); git(wt, "-c", "user.name=sdlc-evals", "-c", "user.email=evals@localhost",
                                   "commit", "-qm", "eval baseline", "--allow-empty")

        cmd = ["claude", "-p", case["prompt"], "--model", case.get("model", model), "--output-format", "json",
               "--permission-mode", "acceptEdits", "--max-turns", str(case.get("max_turns", 40)),
               "--allowedTools", *case.get("allowed_tools", ["Read", "Grep", "Glob", "Edit", "Write"])]
        if os.environ.get("SDLC_PLUGIN_DIR"):
            cmd += ["--plugin-dir", os.environ["SDLC_PLUGIN_DIR"]]
        r = subprocess.run(cmd, cwd=wt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=case.get("timeout", 1800))
        try:
            out = json.loads(r.stdout)
            res["cost_usd"] = out.get("total_cost_usd")
            if out.get("is_error"):
                res["error"] = str(out.get("result"))[-500:]
        except json.JSONDecodeError:
            res["error"] = (r.stderr or r.stdout)[-500:]

        changed = [x for x in (git(wt, "diff", "--name-only", "HEAD") + "\n" +
                               git(wt, "ls-files", "--others", "--exclude-standard")).splitlines() if x]
        res["changed"] = changed
        for f in changed:
            hit = matches_any(f, case.get("must_not_change", []))
            if hit:
                res["checks"].append({"name": f"must not change {hit}", "ok": False, "detail": f})
        for c in case.get("checks", []):
            cr = sh(c["cmd"], wt)
            res["checks"].append({"name": c.get("name", c["cmd"]), "ok": cr.returncode == 0,
                                  "detail": (cr.stdout + cr.stderr)[-800:] if cr.returncode else ""})
        res["passed"] = not res["error"] and all(c["ok"] for c in res["checks"])
    except Exception as e:  # a broken case is a failed case
        res["error"] = str(e)[-800:]
    finally:
        res["seconds"] = round(time.time() - t0, 1)
        if keep:
            res["worktree"] = str(wt)
        else:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, capture_output=True)
            shutil.rmtree(wt, ignore_errors=True)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", nargs="*", help="only these case ids")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--model")
    ap.add_argument("--keep", action="store_true", help="keep worktrees for inspection")
    ap.add_argument("--json", help="write results JSON here")
    a = ap.parse_args()

    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    d = root / cfg["evals"]["dir"]
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]
    cases = [c for c in cases if not c.get("disabled") and (not a.case or c["id"] in a.case)]
    if not cases:
        sys.exit(f"no eval cases in {d.relative_to(root)}")
    threshold = a.threshold if a.threshold is not None else float(cfg["evals"].get("threshold", 0.9))
    model = a.model or cfg["models"].get("evals", "sonnet")

    results = []
    for c in cases:
        print(f"- {c['id']} ...", end=" ", flush=True)
        r = run_case(root, c, model, a.keep)
        results.append(r)
        print("PASS" if r["passed"] else "FAIL", f"({r['seconds']}s)", flush=True)
        for ch in r["checks"]:
            if not ch["ok"]:
                print(f"    x {ch['name']}: {ch['detail'][:300]}")
        if r["error"]:
            print(f"    x error: {r['error'][:300]}")

    rate = sum(r["passed"] for r in results) / len(results)
    summary = {"ts": utcnow(), "pass_rate": round(rate, 3), "threshold": threshold, "model": model,
               "head": git(root, "rev-parse", "--short", "HEAD"), "results": results}
    audit(root, "evals", pass_rate=summary["pass_rate"], threshold=threshold, cases=len(results))
    if a.json:
        Path(a.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\npass rate {rate:.0%} (threshold {threshold:.0%}) over {len(results)} case(s)")
    sys.exit(0 if rate >= threshold else 1)


if __name__ == "__main__":
    main()
