#!/usr/bin/env python3
"""The merge decision for out-of-the-loop work: facts first, then the agent's own confidence.

  python .sdlc/bin/merge_gate.py decide <change> [--mode off|trial|auto] [--branch B] [--base main]
  python .sdlc/bin/merge_gate.py show [<change>]

Hard limits are facts no agent can argue with. Any one of them hands the change to a human:
  - the working tree is clean and the branch head is the commit that gets verified and merged
  - every verify command passes at exactly that commit (the log is kept as evidence)
  - the branch merges into the base without conflicts
  - no Important review finding is open and no ESCALATION.md exists
  - no protected path (config protected_paths + governance files) is touched
  - hidden holdout scenarios pass, when the change has a holdout verdict (Phase 2)
Then the agent's confidence report (<change>/assessment.json): confident + assumptions, or a reason why not.
"Not confident" always goes to a human.

Modes (config pipeline.merge_mode, or --mode):
  off    nothing is evaluated; the change waits as a branch / PR, like before
  trial  every decision is evaluated and recorded ("would merge" / "would hand over"), nothing is merged
  auto   a change that clears everything is merged; main is verified right after and the merge is
         reverted automatically if main goes red
Decisions: .sdlc/state/merge/<change>.json (latest) and .sdlc/state/merge-decisions.jsonl (history).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import (audit, changes, find_root, git, load_config, matches_any, portable_cmd,  # noqa: E402
                       read_state, setup_signature, state_path, utcnow)

MODES = ("off", "trial", "auto")
# files that govern the agents: a change that edits them is always the owner's to merge
GOVERNANCE = [".sdlc/config.json", ".sdlc/bin/**", ".claude/settings.json", ".claude/settings.local.json",
              ".github/workflows/**", ".gitlab-ci.yml", "CLAUDE.md", "REVIEW.md", "sdlc/MISSION.md",
              "sdlc/holdout/**", "evals/defects.json", ".sdlc/holdout/**"]

ASSESS_PROMPT = """Assess the change at {rel} before it can be merged without a human reading the diff.
Read {rel}/intent.md, spec.md, plan.md, review.md and the diff (git diff {base}...HEAD). Then write
{rel}/assessment.json with exactly these fields:
  "confident":      true only if you are sure it does what intent.md asks, nothing more, and it works
  "recommendation": "merge" when confident, otherwise "handover"
  "reason":         required when not confident: what you are unsure about, in one sentence
  "assumptions":    a list of every assumption you made that intent.md did not state
  "notes":          anything a reviewer should look at first (may be empty)
Be honest: an unsure "merge" costs far more than a handover. Reply with one line when the file is written."""


def sh(cmd, cwd: Path, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def read_assessment(change: Path) -> tuple[dict | None, str]:
    """(assessment, problem). The schema refuses a merge without confidence, a handover with it, and a
    missing reason — the same rules for every agent."""
    p = change / "assessment.json"
    if not p.is_file():
        return None, "The agent didn't report an assessment."
    try:
        a = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        return None, f"The assessment is not valid JSON ({e})."
    if not isinstance(a, dict) or not isinstance(a.get("confident"), bool):
        return None, "The assessment has no true/false 'confident'."
    rec = a.get("recommendation")
    if rec not in ("merge", "handover"):
        return a, "The assessment's recommendation is neither 'merge' nor 'handover'."
    if a["confident"] and rec != "merge":
        return a, "The assessment is confident but recommends a handover."
    if not a["confident"] and rec != "handover":
        return a, "The assessment recommends a merge without being confident."
    if not a["confident"] and not str(a.get("reason") or "").strip():
        return a, "The assessment is not confident but gives no reason."
    if not isinstance(a.get("assumptions", []), list):
        return a, "The assessment's assumptions are not a list."
    return a, ""


def verify_at_head(root: Path, cfg: dict, change: str, head: str) -> tuple[bool, str, str]:
    """Run every configured verify command in the checkout (which must be at `head`). Returns ok, summary,
    and the path of the evidence log."""
    cmds = [(k, c) for k, c in cfg["verify"].items() if c]
    ev = state_path(root, f"evidence/{change}/verify-{head[:12]}.log")
    ev.parent.mkdir(parents=True, exist_ok=True)
    if not cmds:
        ev.write_text("no verify commands configured\n", encoding="utf-8")
        return False, "No checks defined for this repo (verify commands in .sdlc/config.json).", str(ev)
    failed, out = [], [f"verify at {head} — {utcnow()}"]
    for k, c in cmds:
        if k == "setup" and setup_signature(root, c) and \
                (read_state(root, "setup.json") or {}).get("sig") == setup_signature(root, c):
            out.append("=== setup: skipped (dependencies unchanged since the last install)")
            continue
        r = sh(portable_cmd(c), root)
        out.append(f"=== {k}: {c} -> exit {r.returncode}\n{(r.stdout + r.stderr)[-6000:]}")
        if r.returncode:
            failed.append(k)
    ev.write_text("\n".join(out) + "\n", encoding="utf-8")
    return (not failed), (f"Checks failed at {head[:12]}: {', '.join(failed)}." if failed else
                          f"All checks passed at {head[:12]} ({', '.join(k for k, _ in cmds)})."), str(ev)


def conflicts(root: Path, base: str, head: str) -> str:
    """'' when head merges cleanly into base, else what conflicts."""
    r = sh(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", base, head], root)
    if r.returncode in (0, 1) and "usage" not in r.stderr.lower():
        return "" if r.returncode == 0 else ", ".join(r.stdout.split()[1:6]) or "conflicts"
    tmp = Path(tempfile.mkdtemp(prefix="sdlc-merge-check-"))  # git older than 2.38: try it for real, aside
    try:
        if sh(["git", "worktree", "add", "-q", "--detach", str(tmp), base], root).returncode:
            return "could not check for conflicts"
        r = sh(["git", "-c", "user.name=merge-gate", "-c", "user.email=gate@sdlc.local", "merge", "--no-commit",
                "--no-ff", head], tmp)
        bad = sh(["git", "diff", "--name-only", "--diff-filter=U"], tmp).stdout.split()
        return "" if r.returncode == 0 else (", ".join(bad[:5]) or "conflicts")
    finally:
        sh(["git", "worktree", "remove", "--force", str(tmp)], root)
        shutil.rmtree(tmp, ignore_errors=True)


def open_findings(change: Path) -> str:
    import re
    p = change / "review.md"
    if not p.is_file():
        return ""
    m = re.search(r"Important:\s*(\d+)", p.read_text(encoding="utf-8", errors="replace"))
    return f"{m.group(1)} Important review finding(s) open." if m and int(m.group(1)) else ""


def holdout_result(root: Path, change: str, head: str) -> str:
    """Phase 2 hook: '' when there is no holdout verdict or it passed at this head; else why it blocks."""
    p = state_path(root, f"holdout/{change}.json")
    if not p.is_file():
        return ""
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return "The holdout verdict is unreadable."
    if v.get("head") and v["head"] != head:
        return f"Holdouts were checked at {str(v['head'])[:12]}, not at {head[:12]}."
    return "" if v.get("verdict") == "pass" else f"Hidden holdout scenarios failed: {v.get('summary', '')}".strip()


RISKY = ["**/auth*", "**/*auth*/**", "**/login*", "**/permission*", "**/*role*", "**/payment*", "**/billing*",
         "**/*money*", "**/migrations/**", "**/*migration*", "**/security*", "**/*secret*", "**/*crypt*"]


def check_depth(cfg: dict, change: Path, touched: list) -> str:
    """light | normal | deep. intent.md's `Check:` line wins; otherwise risky paths are deep, docs-only is light."""
    from sdlc_core import header_field
    v = (header_field(change / "intent.md", "Check") or "").strip().lower()
    if v in ("light", "normal", "deep"):
        return v
    ch = cfg.get("checks") or {}
    if any(matches_any(p, ch.get("deep_globs") or RISKY) for p in touched):
        return "deep"
    if touched and all(p.endswith((".md", ".txt", ".rst")) or p.startswith(("docs/", "sdlc/")) for p in touched):
        return "light"
    return "normal"


def exposures(root: Path, branch: str) -> int:
    common = git(root, "rev-parse", "--git-common-dir") or ".git"
    log = Path(common) if Path(common).is_absolute() else root / common
    log = log / "sdlc-holdout-exposures.jsonl"
    if not log.is_file():
        return 0
    n = 0
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            n += json.loads(ln).get("branch") == branch
        except ValueError:
            pass
    return n


def start_app(root: Path, cfg: dict) -> tuple[object, str]:
    cmd = ((cfg.get("holdout") or {}).get("start_cmd") or cfg["team"]["qa"].get("start_cmd") or "").strip()
    if not cmd:
        return None, ""
    import socket
    import time
    import urllib.error
    import urllib.request
    port = int((cfg.get("holdout") or {}).get("port", 8650))
    for cand in range(port, port + 50):
        with socket.socket() as so:
            if so.connect_ex(("127.0.0.1", cand)):
                port = cand
                break
    kw = {"start_new_session": True} if sys.platform != "win32" else {"creationflags": 0x00000200}
    proc = subprocess.Popen(portable_cmd(cmd).replace("{port}", str(port)), shell=True, cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            env={**__import__("os").environ, "PORT": str(port)}, **kw)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + int(cfg["team"]["qa"].get("start_timeout", 60))
    while time.time() < deadline and proc.poll() is None:
        try:
            urllib.request.urlopen(url + cfg["team"]["qa"].get("health_path", "/"), timeout=2)
            return proc, url
        except urllib.error.HTTPError:
            return proc, url
        except Exception:
            time.sleep(1)
    return proc, url + " (did not answer at startup)"


def stop_app(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    import signal
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            __import__("os").killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def run_holdouts(root: Path, cfg: dict, change: Path, head: str) -> None:
    """The verifier runs the hidden scenarios against the running app; only verdicts come back."""
    if not (root / "sdlc" / "holdout" / f"{change.name}.md").is_file():
        return
    from pipeline import READ_ONLY, Escalate, claude, verdict  # noqa: F401
    proc, url = start_app(root, cfg)
    try:
        prompt = (f"Verify the change {change.name} against its hidden holdout scenarios in "
                  f"sdlc/holdout/{change.name}.md. " + (f"The app is running at {url}. " if url else
                  "No app server is configured: use the project's own commands and its test suite. ")
                  + 'End with one JSON line: {"verdict":"pass"|"fail","results":[{"id":"H-1","pass":true,"observed":"..."}]}')
        tools = READ_ONLY + ["Bash(curl *)", "Bash(python *)", "Bash(python3 *)", "Bash(npm *)", "Bash(npx *)",
                             "Bash(node *)", "Bash(pytest*)", "Bash(ls*)", "Bash(cat *)"]
        try:
            text = claude(prompt, cfg["models"].get("gate", "sonnet"), tools, root, agent="sdlc:holdout-verifier",
                          env={"SDLC_HOLDOUT_ACCESS": "1"})
        except Escalate as e:
            text = f'{{"verdict":"fail","results":[],"error":{json.dumps(str(e)[:300])}}}'
    finally:
        stop_app(proc)
    import re
    m = re.findall(r"\{.*\"verdict\".*\}", text)
    try:
        v = json.loads(m[-1]) if m else {"verdict": "fail", "results": [], "error": "the verifier gave no verdict"}
    except ValueError:
        v = {"verdict": "fail", "results": [], "error": "the verifier's verdict is unreadable"}
    res = v.get("results") or []
    bad = [r for r in res if not r.get("pass")]
    summary = (f"{len(res) - len(bad)}/{len(res)} passed" + "".join(f"; {r.get('id')}: {r.get('observed', '')}"[:160]
               for r in bad)) if res else v.get("error", "no results")
    if v.get("verdict") == "pass" and bad:
        v["verdict"] = "fail"
    p = state_path(root, f"holdout/{change.name}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"head": head, "verdict": v.get("verdict"), "summary": summary, "results": res,
                             "url": url, "at": utcnow()}, indent=2), encoding="utf-8")


def decide(root: Path, cfg: dict, change: Path, mode: str, branch: str | None = None,
           base: str | None = None, run_verify: bool = True, owner: str = "") -> dict:
    """owner: a human approved the merge (e.g. from the phone). Their approval overrules only the agent's doubt;
    every hard limit still applies."""
    pc = cfg["pipeline"]
    branch = branch or f"{pc.get('branch_prefix', 'sdlc/')}{change.name}"
    base = base or next((b for b in cfg.get("protected_branches", ["main"])
                         if git(root, "rev-parse", "--verify", "--quiet", b)), "main")
    rec = {"change": change.name, "mode": mode, "branch": branch, "base": base, "at": utcnow(),
           "decision": "handover", "reasons": [], "facts": {}}
    head = git(root, "rev-parse", "--verify", "--quiet", branch)
    if not head:
        rec["reasons"].append(f"Branch {branch} does not exist.")
        return save(root, rec)
    rec["head"] = head
    facts, reasons = rec["facts"], rec["reasons"]

    if git(root, "status", "--porcelain", "--untracked-files=no"):
        reasons.append("The working tree has uncommitted changes, so the exact commit can't be verified.")
    elif git(root, "rev-parse", "HEAD") != head and sh(["git", "checkout", "-q", branch], root).returncode:
        reasons.append(f"Could not check out {branch} to verify it.")
    if (change / "ESCALATION.md").is_file():
        reasons.append("The change is escalated (ESCALATION.md).")
    f = open_findings(change)
    if f:
        reasons.append(f)
    touched = sh(["git", "diff", "--name-only", f"{base}...{head}"], root).stdout.split()
    facts["files_changed"] = len(touched)
    own_holdout = f"sdlc/holdout/{change.name}.md"  # written by the gated holdout writer for this very change
    guarded = [p for p in touched if p != own_holdout
               and matches_any(p, list(cfg.get("protected_paths", [])) + GOVERNANCE)]
    if guarded:
        reasons.append(f"Touches protected paths ({', '.join(guarded[:5])}): the owner makes that change.")
    c = conflicts(root, base, head)
    if c:
        reasons.append(f"Conflicts with {base}: {c}.")
    if not reasons and run_verify:
        ok, summary, ev = verify_at_head(root, cfg, change.name, head)
        facts["verify"], facts["evidence"] = summary, ev
        if not ok:
            reasons.append(summary)
        elif git(root, "rev-parse", "HEAD") != head or git(root, "status", "--porcelain", "--untracked-files=no"):
            reasons.append("The checkout changed while it was being verified.")
    depth = check_depth(cfg, change, touched)
    facts["check_depth"] = depth
    if not reasons and depth != "light":
        run_holdouts(root, cfg, change, head)
        h = holdout_result(root, change.name, head)
        if h:
            reasons.append(h)
        n = exposures(root, branch)
        if n:
            reasons.append(f"Hidden holdout scenarios were exposed to a builder {n} time(s) on {branch}, so they no "
                           "longer prove anything for this change.")
    if not reasons and depth == "deep":
        import mutate
        if mutate.load_defects(root):
            rep = mutate.run_defects(root, cfg)
            facts["mutations"] = {"escaped": rep["escaped"], "error": rep["error"],
                                  "caught": sum(r.get("result") == "caught" for r in rep["results"])}
            if rep["escaped"]:
                reasons.append(f"Deliberate bugs escaped the tests: {', '.join(map(str, rep['escaped']))}. "
                               "Tests that miss them can't vouch for this change.")
            elif rep["error"]:
                reasons.append(f"Deliberate-bug check could not run: {rep['error']}.")
        else:
            facts["mutations"] = "no deliberate bugs defined (sdlc/defects.json)"

    a, problem = read_assessment(change)
    rec["assessment"] = a
    if owner:
        facts["approved_by"] = owner
    elif problem:
        reasons.append(problem)
    elif not a["confident"]:
        reasons.append(f"The agent isn't confident: {a.get('reason')}")
    if not reasons:
        rec["decision"] = "merge"
    return save(root, rec)


def save(root: Path, rec: dict) -> dict:
    p = state_path(root, f"merge/{rec['change']}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    with open(state_path(root, "merge-decisions.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in rec.items() if k != "assessment"}) + "\n")
    audit(root, "merge-decision", change=rec["change"], mode=rec["mode"], decision=rec["decision"],
          result=rec.get("result"), reasons=rec["reasons"][:5], head=rec.get("head"))
    _notify(root, rec)
    return rec


def _notify(root: Path, rec: dict) -> None:
    res = rec.get("result") or ""
    if rec.get("mode") == "trial" or not res:
        return
    try:
        import notify
        cfg = load_config(root)
        why = "".join(f"\n- {r}" for r in rec["reasons"][:4])
        if res == "handed over":
            notify.send(root, cfg, "needs_you", f"🧾 {rec['change']} needs your review", f"The merge gate handed it "
                        f"over:{why}", [{"label": "Merge anyway", "action": f"merge:{rec['change']}"}], rec["change"])
        elif res == "merged" and rec.get("watch", {}).get("state") == "green":
            notify.send(root, cfg, "merged", f"🔀 {rec['change']} merged", f"Checks green at {rec.get('head', '')[:12]}, "
                        "main still green after the merge.", key=rec["change"])
        elif "revert" in res:
            notify.send(root, cfg, "reverted", f"↩️ {rec['change']}: {res}",
                        (rec.get("watch") or {}).get("verify", ""), key=rec["change"])
    except Exception:
        pass


def remote_kind(root: Path, cfg: dict) -> str:
    url = git(root, "remote", "get-url", "origin")
    if not url:
        return ""
    if cfg.get("vcs") == "gitlab" or (cfg.get("vcs") == "auto" and "gitlab" in url):
        return "gitlab" if shutil.which("glab") else ""
    return "github" if shutil.which("gh") else ""


def apply(root: Path, cfg: dict, change: Path, rec: dict, pr: bool = False) -> dict:
    """trial: record only. auto: merge exactly the verified head, then watch main and revert if it goes red."""
    if rec["decision"] != "merge":
        rec["result"] = "handed over" if rec["mode"] != "trial" else "would hand over"
        return save(root, rec)
    if rec["mode"] == "trial":
        rec["result"] = "would merge"
        return save(root, rec)
    base, head, kind = rec["base"], rec["head"], (remote_kind(root, cfg) if pr else "")
    if git(root, "rev-parse", rec["branch"]) != head:
        rec.update(decision="handover", result="handed over")
        rec["reasons"].append("The branch moved after it was verified.")
        return save(root, rec)
    msg = f"{change.name}: merge (auto, {'approved by ' + rec['facts']['approved_by'] if rec['facts'].get('approved_by') else 'agent confident'}, checks green at {head[:12]})"
    where = main_checkout(root)
    if not kind and where != root:  # a parallel runner's worktree: merge in the checkout that holds the base
        with merge_lock(where):
            if git(where, "rev-parse", "--abbrev-ref", "HEAD") != base or \
                    git(where, "status", "--porcelain", "--untracked-files=no"):
                rec.update(decision="handover", result="handed over")
                rec["reasons"].append(f"The main checkout is not clean on {base}, so it can't be merged there now.")
                return save(root, rec)
            r = sh(["git", "-c", "user.name=sdlc-merge-gate", "-c", "user.email=merge-gate@sdlc.local",
                    "merge", "--no-ff", "-m", msg, head], where)
            if r.returncode:
                sh(["git", "merge", "--abort"], where)
                rec.update(decision="handover", result="handed over")
                rec["reasons"].append(f"The merge was refused: {(r.stderr or r.stdout).strip()[-300:]}")
                return save(root, rec)
            rec["result"], rec["merged_commit"] = "merged", git(where, "rev-parse", "HEAD")
            save(root, rec)
            return watch(root, cfg, change, rec, "", where=where)
    if kind == "github":
        r = sh(["gh", "pr", "merge", rec["branch"], "--merge", "--match-head-commit", head, "--subject", msg], root)
    elif kind == "gitlab":
        r = sh(["glab", "mr", "merge", rec["branch"], "--sha", head, "--yes"], root)
    else:
        r = sh(["git", "checkout", "-q", base], root)
        if not r.returncode:
            r = sh(["git", "-c", "user.name=sdlc-merge-gate", "-c", "user.email=merge-gate@sdlc.local",
                    "merge", "--no-ff", "-m", msg, head], root)
            if r.returncode:
                sh(["git", "merge", "--abort"], root)
    if r.returncode:
        rec.update(decision="handover", result="handed over")
        rec["reasons"].append(f"The merge was refused: {(r.stderr or r.stdout).strip()[-300:]}")
        return save(root, rec)
    if kind:
        sh(["git", "checkout", "-q", base], root)
        sh(["git", "pull", "-q", "--ff-only", "origin", base], root)
    rec["result"], rec["merged_commit"] = "merged", git(root, "rev-parse", "HEAD")
    save(root, rec)
    return watch(root, cfg, change, rec, kind)


def main_checkout(root: Path) -> Path:
    """The checkout that holds the base branch: the main one, also when called from a linked worktree."""
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent if common else root


class merge_lock:
    """Merges into the base happen one at a time, even with parallel runners."""

    def __init__(self, where: Path):
        self.p = where / ".sdlc" / "state" / "merge.lock"

    def __enter__(self):
        import os
        import time
        self.p.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 900
        while True:
            try:
                fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - self.p.stat().st_mtime > 3600 or time.time() > deadline:
                    self.p.unlink(missing_ok=True)  # a lock older than an hour was left by a crash
                time.sleep(1)

    def __exit__(self, *exc):
        self.p.unlink(missing_ok=True)


def watch(root: Path, cfg: dict, change: Path, rec: dict, kind: str = "", where: Path | None = None) -> dict:
    """Verify main right after the merge. Red → revert the merge at once (never retried)."""
    if where is not None and where != root:
        ok, summary, ev = verify_at_head(where, cfg, f"{change.name}-main", rec["merged_commit"])
        rec["watch"] = {"verify": summary, "evidence": ev}
        if ok:
            rec["watch"]["state"] = "green"
            return save(root, rec)
        parents = git(where, "rev-list", "--parents", "-n", "1", rec["merged_commit"]).split()
        r = sh(["git", "-c", "user.name=sdlc-merge-gate", "-c", "user.email=merge-gate@sdlc.local", "revert",
                "--no-edit"] + (["-m", "1"] if len(parents) > 2 else []) + [rec["merged_commit"]], where)
        if r.returncode:
            sh(["git", "revert", "--abort"], where)
        rec["watch"]["state"] = "revert_failed" if r.returncode else "reverted"
        rec["result"] = "revert failed — fix main by hand" if r.returncode else "merged, then reverted (main went red)"
        return save(root, rec)
    ok, summary, ev = verify_at_head(root, cfg, f"{change.name}-main", rec["merged_commit"])
    rec["watch"] = {"verify": summary, "evidence": ev}
    if ok:
        rec["watch"]["state"] = "green"
        return save(root, rec)
    merge = rec["merged_commit"]
    parents = git(root, "rev-list", "--parents", "-n", "1", merge).split()
    rev = ["git", "-c", "user.name=sdlc-merge-gate", "-c", "user.email=merge-gate@sdlc.local", "revert", "--no-edit"]
    rev += (["-m", "1"] if len(parents) > 2 else []) + [merge]
    if kind:
        rb = f"{cfg['pipeline'].get('branch_prefix', 'sdlc/')}revert-{change.name}"
        sh(["git", "checkout", "-q", "-B", rb, rec["base"]], root)
        r = sh(rev, root)
        if not r.returncode:
            r = sh(["git", "push", "-u", "origin", rb], root)
        if not r.returncode and kind == "github":
            title = f"Revert {change.name}: main went red after merge"
            r = sh(["gh", "pr", "create", "--title", title, "--body", summary, "--head", rb, "--base", rec["base"]], root)
            if not r.returncode:
                r = sh(["gh", "pr", "merge", rb, "--merge"], root)
        elif not r.returncode and kind == "gitlab":
            r = sh(["glab", "mr", "create", "--source-branch", rb, "--target-branch", rec["base"], "--title",
                    f"Revert {change.name}", "--description", summary, "--yes"], root)
            if not r.returncode:
                r = sh(["glab", "mr", "merge", rb, "--yes"], root)
        sh(["git", "checkout", "-q", rec["base"]], root)
        sh(["git", "pull", "-q", "--ff-only", "origin", rec["base"]], root)
    else:
        r = sh(rev, root)
        if r.returncode:
            sh(["git", "revert", "--abort"], root)
    rec["watch"]["state"] = "revert_failed" if r.returncode else "reverted"
    rec["result"] = "revert failed — fix main by hand" if r.returncode else "merged, then reverted (main went red)"
    if r.returncode:
        rec["watch"]["error"] = (r.stderr or r.stdout).strip()[-300:]
    return save(root, rec)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("decide")
    s.add_argument("change")
    s.add_argument("--mode", choices=MODES)
    s.add_argument("--branch")
    s.add_argument("--base")
    s.add_argument("--pr", action="store_true", help="merge through the PR/MR (gh / glab) instead of locally")
    s = sub.add_parser("show")
    s.add_argument("change", nargs="?")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    if a.cmd == "show":
        d = state_path(root, "merge")
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            if a.change and a.change not in p.stem:
                continue
            r = json.loads(p.read_text(encoding="utf-8"))
            print(f"{r['change']}: {r.get('result') or r['decision']} ({r['mode']}) at {r.get('head', '')[:12]}")
            for x in r["reasons"]:
                print(f"   - {x}")
        return
    hits = [c for c in changes(root, cfg) if a.change in c.name]
    if len(hits) != 1:
        sys.exit(f"change '{a.change}' matched {len(hits)} folders")
    mode = a.mode or cfg["pipeline"].get("merge_mode", "trial")
    if mode == "off":
        print("merge_mode is off: nothing evaluated")
        return
    rec = apply(root, cfg, hits[0], decide(root, cfg, hits[0], mode, a.branch, a.base), pr=a.pr)
    print(json.dumps({k: rec.get(k) for k in ("decision", "result", "reasons", "head", "watch")}, indent=2))
    sys.exit(0 if rec["decision"] == "merge" else 4)


if __name__ == "__main__":
    main()
