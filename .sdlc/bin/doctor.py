#!/usr/bin/env python3
"""Check this project's setup before an agent run, and say how to fix what is wrong.

  python .sdlc/bin/doctor.py            # every check, one line each
  python .sdlc/bin/doctor.py --fix      # also do the safe fixes (stale git locks, prunable worktrees)
  python .sdlc/bin/doctor.py --json

✓ ok · ⚠ works, but slower or less safe · ✗ will break a run. Exit 1 when anything is ✗.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import CONFIG_REL, find_root, git, load_config  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOCKS = ("index.lock", "HEAD.lock", "ORIG_HEAD.lock", "config.lock", "packed-refs.lock",
         "worktrees/*/index.lock", "worktrees/*/HEAD.lock", "refs/**/*.lock")
SHELL_WORDS = {"cd", "set", "export", "env", "call", "pushd", "popd", "if", "then", "echo", "true", "test", "["}


def _check(name, status, detail, fix=""):
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _tools_in(cmd: str) -> list[str]:
    """First word of every part of a shell command line (`a && b | c`), skipping env assignments."""
    out = []
    for part in re.split(r"&&|\|\||[;|]", cmd):
        try:
            words = shlex.split(part, posix=os.name != "nt")
        except ValueError:
            words = part.split()
        words = [w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        w = words[0].strip('"\'') if words else ""
        # bare command names only: paths like .venv/bin/pytest are created by the setup step
        if w and w not in SHELL_WORDS and not w.startswith(("$", "%", "(")) and not re.search(r"[\\/]", w):
            out.append(w)
    return out


def _teams_enabled() -> bool:
    if os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "1":
        return True
    p = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("env", {})
                   .get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")) == "1"
    except (OSError, ValueError):
        return False


def stale_locks(root: Path, older_than: float = 120) -> list[Path]:
    gitdir = root / ".git"
    if not gitdir.is_dir():
        return []
    out = []
    for pat in LOCKS:
        for f in gitdir.glob(pat):
            try:
                if time.time() - f.stat().st_mtime > older_than:
                    out.append(f)
            except OSError:
                pass
    return out


def run_checks(root: Path, need_claude: bool = False, fix: bool = False) -> list[dict]:
    res = []
    add = res.append
    v = sys.version_info
    add(_check("python", "ok" if v >= (3, 10) else "fail", f"{v.major}.{v.minor}.{v.micro}",
               "" if v >= (3, 10) else "install Python 3.10 or newer"))

    if not shutil.which("git"):
        add(_check("git", "fail", "git is not on PATH", "install git"))
        return res
    gv = git(root, "--version")
    m = re.search(r"(\d+)\.(\d+)", gv)
    new = m and (int(m.group(1)), int(m.group(2))) >= (2, 38)
    add(_check("git", "ok" if new else "warn", gv,
               "" if new else "git 2.38+ checks merge conflicts faster; older works, more slowly"))
    if not git(root, "rev-parse", "--is-inside-work-tree"):
        add(_check("git repo", "fail", f"{root} is not a git repository", "git init"))
        return res
    who = git(root, "config", "user.name"), git(root, "config", "user.email")
    add(_check("git identity", "ok" if all(who) else "fail", " / ".join(x or "(missing)" for x in who),
               "" if all(who) else 'git config --global user.name "You"; git config --global user.email you@example.com'))
    base = next((b for b in ["main", "master"] if git(root, "rev-parse", "--verify", "--quiet", b)), "")
    add(_check("base branch", "ok" if base else "warn", base or "no main/master branch yet",
               "" if base else "make a first commit on main"))

    try:
        json.loads((root / CONFIG_REL).read_text(encoding="utf-8"))
        cfg = load_config(root)
        add(_check("config", "ok", CONFIG_REL))
    except (OSError, ValueError) as e:
        add(_check("config", "fail", f"{CONFIG_REL}: {e}", "fix the JSON, or run /sdlc:init again"))
        return res
    verify = {k: c for k, c in cfg.get("verify", {}).items() if c}
    add(_check("verify commands", "ok" if verify.get("test") else "warn",
               ", ".join(verify) or "none configured",
               "" if verify.get("test") else "set verify.test in .sdlc/config.json: QA and the merge gate need it"))
    missing = sorted({t for c in verify.values() for t in _tools_in(c)
                      if not (shutil.which(t) or (root / t).exists() or Path(t).exists())})
    add(_check("verify tools", "ok" if not missing else "fail",
               "all found" if not missing else "not found: " + ", ".join(missing),
               "" if not missing else "install them or fix the verify commands"))
    bin_ok = all((root / ".sdlc" / "bin" / f).is_file() for f in ("sdlc.py", "team.py", "board.py"))
    add(_check("kit scripts", "ok" if bin_ok else "fail", ".sdlc/bin" if bin_ok else ".sdlc/bin is incomplete",
               "" if bin_ok else "python <kit>/plugins/sdlc/scripts/init.py --upgrade-bin"))
    leak = [d for d in (".sdlc/state/x", ".sdlc/worktrees/x", ".sdlc/evidence/x")
            if subprocess.run(["git", "check-ignore", "-q", d], cwd=root).returncode != 0]
    add(_check("ignored folders", "ok" if not leak else "warn",
               "kit state, worktrees and evidence stay out of commits" if not leak else
               "would be committed: " + ", ".join(x[:-2] for x in leak),
               "" if not leak else "python <kit>/plugins/sdlc/scripts/init.py --upgrade-bin"))

    has_claude = bool(shutil.which("claude") or os.environ.get("SDLC_CLAUDE_CMD"))
    add(_check("claude CLI", "ok" if has_claude else ("fail" if need_claude else "warn"),
               shutil.which("claude") or "not on PATH",
               "" if has_claude else "install Claude Code and sign in (claude auth login)"))
    add(_check("agent teams", "ok" if _teams_enabled() else "warn",
               "on" if _teams_enabled() else "off: /sdlc:team falls back to slower background mode",
               "" if _teams_enabled() else 'add "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"} to ~/.claude/settings.json'))
    try:
        import team
        gap = team.missing_allow(root)
    except Exception:
        gap = []
    add(_check("permissions", "ok" if not gap else "warn",
               "teammate commands pre-approved" if not gap else
               f"{len(gap)} teammate commands not pre-approved: each one waits for a click",
               "" if not gap else "python .sdlc/bin/team.py allow"))

    locks = stale_locks(root)
    if locks and fix:
        for f in locks:
            try:
                f.unlink()
            except OSError:
                pass
    add(_check("git locks", "ok" if not locks or fix else "fail",
               "none" if not locks else ("removed " if fix else "") + ", ".join(str(f.relative_to(root)) for f in locks),
               "" if not locks or fix else "python .sdlc/bin/doctor.py --fix  (left by a crash or power cut)"))
    prunable = [ln for ln in git(root, "worktree", "list", "--porcelain").splitlines() if ln.startswith("prunable")]
    if prunable and fix:
        git(root, "worktree", "prune")
    add(_check("worktrees", "ok" if not prunable or fix else "warn",
               "clean" if not prunable else f"{len(prunable)} stale worktree record(s)" + (" pruned" if fix else ""),
               "" if not prunable or fix else "python .sdlc/bin/doctor.py --fix"))

    db = root / ".sdlc" / "state" / "board.db"
    if db.is_file():
        try:
            c = sqlite3.connect(db, timeout=5)
            ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            c.close()
            add(_check("board", "ok" if ok else "fail", "board.db healthy" if ok else "board.db is damaged",
                       "" if ok else "python .sdlc/bin/team.py reset (keeps git work)"))
        except sqlite3.Error as e:
            add(_check("board", "fail", f"board.db: {e}", "close other board servers, then try again"))
    dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    dot = shutil.which("dot")
    add(_check("diagram layout", "ok" if dot else "warn", "Graphviz" if dot else "built-in layout (readable, less tidy)",
               "" if dot else "for the tidiest architecture diagrams: winget install graphviz / brew install graphviz / "
               "apt install graphviz"))
    add(_check("working tree", "ok" if not dirty else "warn", "clean" if not dirty else "uncommitted changes",
               "" if not dirty else "commit or stash them: the out-of-the-loop runner skips ticks on a dirty tree"))
    free = shutil.disk_usage(root).free / 2**30
    add(_check("disk", "ok" if free > 2 else "warn", f"{free:.1f} GB free",
               "" if free > 2 else "free some space: every worktree installs its own dependencies"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--loop", action="store_true", help="also require what the headless loop needs (claude CLI)")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    res = run_checks(root, need_claude=a.loop, fix=a.fix)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        icon = {"ok": "✓", "warn": "⚠", "fail": "✗"}
        for c in res:
            print(f"{icon[c['status']]} {c['name']:16} {c['detail']}" + (f"\n    fix: {c['fix']}" if c["fix"] else ""))
        n = {s: sum(c["status"] == s for c in res) for s in icon}
        print(f"\n{n['ok']} ok · {n['warn']} warnings · {n['fail']} problems")
    sys.exit(1 if any(c["status"] == "fail" for c in res) else 0)


if __name__ == "__main__":
    main()
