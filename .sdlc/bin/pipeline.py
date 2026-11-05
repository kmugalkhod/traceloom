#!/usr/bin/env python3
"""Headless loop (playbook Stage 6): run accepted work through spec -> plan -> build -> review
with an independent confidence gate between stages. Each gate either continues or escalates to a human.

  python .sdlc/bin/pipeline.py <change> [--from spec|plan|build|review] [--to spec|plan|build|review]
                               [--no-commit] [--pr] [--dry-run]

<change> is a folder name (or unique substring) under changes_dir. intent.md must be Status: accepted
(the product owner's decision stays human). Needs the `claude` CLI on PATH with the sdlc plugin installed,
or SDLC_PLUGIN_DIR pointing at the plugin folder (used in CI).

Gates:
  spec   -> gatekeeper agent; unresolved flagged concerns always escalate (policy owners decide)
  plan   -> gatekeeper agent
  build  -> deterministic: configured verify commands must pass; up to max_fix_rounds fix passes
  review -> review.md tally: any Important finding triggers a fix pass, then escalates
Escalation writes ESCALATION.md in the change folder and exits 3. Nothing passes the production gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, changes, find_root, git, header_field, load_config, utcnow  # noqa: E402

STAGES = ["spec", "plan", "build", "review"]
READ_ONLY = ["Read", "Grep", "Glob"]
WRITE = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit"]
SAFE_BASH = ["Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)",
             "Bash(python .sdlc/bin/sdlc.py *)", "Bash(python3 .sdlc/bin/sdlc.py *)", "Bash(ls*)"]


class Escalate(Exception):
    pass


def claude(prompt: str, model: str, tools: list[str], root: Path, agent: str | None = None,
           dry: bool = False) -> str:
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--permission-mode", "acceptEdits", "--allowedTools", *tools]
    if agent:
        cmd += ["--agent", agent]
    if os.environ.get("SDLC_PLUGIN_DIR"):
        cmd += ["--plugin-dir", os.environ["SDLC_PLUGIN_DIR"]]
    print(f"  $ claude -p {prompt[:70]!r}... --model {model}" + (f" --agent {agent}" if agent else ""), flush=True)
    if dry:
        return '{"verdict":"pass","reasons":["dry run"]}'
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise Escalate(f"claude returned no JSON (exit {r.returncode}): {(r.stderr or r.stdout)[-1500:]}")
    if data.get("is_error"):
        raise Escalate(f"claude run failed: {str(data.get('result'))[-1500:]}")
    cost = data.get("total_cost_usd")
    if cost is not None:
        print(f"    cost (est.) ${cost:.3f}", flush=True)
    return data.get("result") or ""


def verdict(text: str) -> dict:
    m = re.findall(r"\{[^{}]*\"verdict\"[^{}]*\}", text, re.S)
    try:
        return json.loads(m[-1]) if m else {"verdict": "escalate", "reasons": ["gate gave no verdict"]}
    except json.JSONDecodeError:
        return {"verdict": "escalate", "reasons": ["gate verdict unparseable"]}


def set_status(path: Path, status: str, by: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(Status:\s*)[A-Za-z-]+", rf"\g<1>{status}", text, count=1)
    if n == 0:
        new = text.replace("\n", f"\nStatus: {status}\n", 1)
    new = new.rstrip() + f"\n\n<!-- {status} by {by} at {utcnow()} -->\n"
    path.write_text(new, encoding="utf-8")


def gate_agent(root: Path, cfg: dict, change: Path, artifact: str, dry: bool) -> None:
    rel = change.relative_to(root).as_posix()
    prompt = (f"Gate the {artifact} at {rel}/{artifact}. Read intent.md, spec.md and plan.md that exist in {rel}, "
              "CLAUDE.md and the relevant code. Decide whether it is safe to continue without a human. "
              'End your reply with one JSON line: {"verdict":"pass"|"escalate","reasons":["..."]}')
    v = verdict(claude(prompt, cfg["models"]["gate"], READ_ONLY, root, agent="sdlc:gatekeeper", dry=dry))
    audit(root, "gate", change=change.name, artifact=artifact, verdict=v.get("verdict"), reasons=v.get("reasons"))
    print(f"  gate[{artifact}]: {v.get('verdict')} — {'; '.join(v.get('reasons', []))}", flush=True)
    if v.get("verdict") != "pass":
        raise Escalate(f"gatekeeper escalated {artifact}: " + "; ".join(v.get("reasons", [])))


def verify(root: Path, cfg: dict) -> tuple[bool, str]:
    out, ok = [], True
    for k, c in cfg["verify"].items():
        if not c:
            continue
        r = subprocess.run(c, shell=True, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out.append(f"=== {k}: {c} -> exit {r.returncode}\n{(r.stdout + r.stderr)[-4000:]}")
        ok &= r.returncode == 0
    return ok, "\n".join(out) or "no verify commands configured"


def commit(root: Path, change: Path, stage: str, enabled: bool) -> None:
    if not enabled:
        return
    git(root, "add", "-A")
    if git(root, "diff", "--cached", "--name-only"):
        git(root, "commit", "-m", f"sdlc({stage}): {change.name}", "-m", "Generated by the headless SDLC loop.")
        print(f"  committed sdlc({stage})", flush=True)


def run(root: Path, cfg: dict, change: Path, a) -> None:
    rel = change.relative_to(root).as_posix()
    if (header_field(change / "intent.md", "Status") or "") not in ("accepted", "approved"):
        raise Escalate("intent.md is not Status: accepted — the product owner accepts intent before the loop runs")
    tools_build = (WRITE + SAFE_BASH + [f"Bash({c})" for c in cfg["verify"].values() if c]
                   + [f"Bash({c})" for c in cfg["pipeline"].get("allowed_bash", [])])
    bounds = STAGES[STAGES.index(a.from_stage): STAGES.index(a.to_stage) + 1]

    for stage in bounds:
        print(f"\n## {stage}", flush=True)
        if stage == "spec":
            claude(f"/sdlc:spec {rel} --headless", cfg["models"]["spec"], WRITE, root, dry=a.dry_run)
            spec = change / "spec.md"
            if not a.dry_run and not spec.is_file():
                raise Escalate("spec.md was not written")
            concerns = re.search(r"^##\s*Flagged concerns(.*?)(?=^##\s|\Z)",
                                 "" if a.dry_run else re.sub(r"<!--.*?-->", "", spec.read_text(encoding="utf-8"), flags=re.S),
                                 re.M | re.S)
            if concerns and re.search(r"^\s*[-*]\s*\[ \]", concerns.group(1), re.M):
                raise Escalate("spec.md has unresolved flagged concerns ([ ] items) — route to policy owners")
            gate_agent(root, cfg, change, "spec.md", a.dry_run)
            if not a.dry_run:
                set_status(spec, "accepted", "gatekeeper")
        elif stage == "plan":
            claude(f"/sdlc:plan {rel} --headless", cfg["models"]["plan"], READ_ONLY + ["Write", "Edit"] + SAFE_BASH,
                   root, dry=a.dry_run)
            if not a.dry_run and not (change / "plan.md").is_file():
                raise Escalate("plan.md was not written")
            gate_agent(root, cfg, change, "plan.md", a.dry_run)
            if not a.dry_run:
                set_status(change / "plan.md", "accepted", "gatekeeper")
        elif stage == "build":
            claude(f"/sdlc:build {rel} --headless", cfg["models"]["build"], tools_build, root, dry=a.dry_run)
            for i in range(int(cfg["pipeline"].get("max_fix_rounds", 3)) + 1):
                ok, log = (True, "") if a.dry_run else verify(root, cfg)
                print(f"  verify: {'PASS' if ok else 'FAIL'}", flush=True)
                if ok:
                    break
                if i == int(cfg["pipeline"].get("max_fix_rounds", 3)):
                    raise Escalate(f"verification still failing after {i} fix rounds:\n{log[-3000:]}")
                claude(f"Verification failed for {rel}. Fix the code, not the tests, then re-run verification.\n\n{log}",
                       cfg["models"]["build"], tools_build, root, dry=a.dry_run)
        elif stage == "review":
            for i in range(2):
                claude(f"/sdlc:review {rel} --headless", cfg["models"]["review"], READ_ONLY + ["Write"] + SAFE_BASH,
                       root, dry=a.dry_run)
                text = "" if a.dry_run else (change / "review.md").read_text(encoding="utf-8")
                m = re.search(r"Important:\s*(\d+)", text)
                important = int(m.group(1)) if m else 0
                print(f"  review: {important} Important finding(s)", flush=True)
                if important == 0:
                    break
                if i == 1:
                    raise Escalate(f"{important} Important review finding(s) remain — see {rel}/review.md")
                claude(f"Address every Important finding in {rel}/review.md. Fix code; never weaken tests. "
                       "Then re-run verification.", cfg["models"]["build"], tools_build, root, dry=a.dry_run)
        commit(root, change, stage, a.commit and not a.dry_run)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("change")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, default="spec")
    ap.add_argument("--to", dest="to_stage", choices=STAGES, default="review")
    ap.add_argument("--no-commit", dest="commit", action="store_false")
    ap.add_argument("--pr", action="store_true", help="push the branch and open a PR/MR (gh or glab)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    if not shutil.which("claude"):
        sys.exit("claude CLI not on PATH")
    cfg = load_config(root)
    a.commit = a.commit and cfg["pipeline"].get("commit", True)
    hits = [c for c in changes(root, cfg) if a.change in c.name]
    if len(hits) != 1:
        sys.exit(f"change '{a.change}' matched {len(hits)} folders: {[h.name for h in hits]}")
    change = hits[0]

    branch = f"{cfg['pipeline'].get('branch_prefix', 'sdlc/')}{change.name}"
    if a.commit and git(root, "rev-parse", "--abbrev-ref", "HEAD") in cfg.get("protected_branches", []):
        git(root, "checkout", "-B", branch)
        print(f"on branch {branch}")

    audit(root, "pipeline-start", change=change.name, stages=f"{a.from_stage}..{a.to_stage}")
    try:
        run(root, cfg, change, a)
    except Escalate as e:
        (change / "ESCALATION.md").write_text(
            f"# Escalation: {change.name}\n\nRaised: {utcnow()} by the headless loop.\n\n## Why\n\n{e}\n\n"
            "## Human decision needed\n\n- [ ] fix and re-run  - [ ] schedule  - [ ] dismiss (tune gate)\n",
            encoding="utf-8")
        audit(root, "pipeline-escalate", change=change.name, reason=str(e)[:500])
        print(f"\nESCALATED: {e}\nSee {change.relative_to(root)}/ESCALATION.md")
        sys.exit(3)

    audit(root, "pipeline-done", change=change.name)
    print("\nloop complete — waiting on code owner approval (branch protection) and the production gate.")
    if a.pr and a.commit and not a.dry_run:
        cur = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        title = f"sdlc: {change.name}"
        body = f"Artifacts: {change.relative_to(root).as_posix()}/ (intent, spec, plan, review)."
        remote = git(root, "remote", "get-url", "origin")
        gitlab = cfg.get("vcs") == "gitlab" or (cfg.get("vcs") == "auto" and "gitlab" in remote)
        if gitlab and not shutil.which("glab"):
            # GitLab push options open the MR without any extra CLI
            subprocess.run(["git", "push", "-u", "origin", cur, "-o", "merge_request.create",
                            "-o", f"merge_request.title={title}", "-o", f"merge_request.description={body}"], cwd=root)
            return
        subprocess.run(["git", "push", "-u", "origin", cur], cwd=root)
        if gitlab:
            subprocess.run(["glab", "mr", "create", "--title", title, "--description", body, "--yes"], cwd=root)
        elif shutil.which("gh"):
            subprocess.run(["gh", "pr", "create", "--title", title, "--body", body], cwd=root)
        else:
            print("no gh/glab on PATH — open the PR/MR by hand")


if __name__ == "__main__":
    main()
