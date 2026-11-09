"""Where did the time go? Reads Claude Code's own records of every teammate's session for this project and splits
each one into MODEL time (the agent thinking between steps, on Anthropic's servers) and COMMAND time (npm, pytest,
git... running on this computer), plus the slowest commands.

Run from the project folder:   python why_slow.py            (last 12 hours)
                               python why_slow.py 24         (last 24 hours)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 12
ROOT = os.path.normcase(os.path.abspath(os.getcwd()))
BASE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
NAME = re.compile(r"assignments[\\/]+((?:dev|qa)-\d+|designer)-|\byou are ((?:dev|qa)-\d+|designer)\b", re.I)


def ts(e):
    try:
        return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def who(entries):
    for e in entries[:6]:
        if e.get("type") == "user":
            m = NAME.search(json.dumps(e.get("message", ""))[:4000])
            if m:
                return (m.group(1) or m.group(2)).lower()
    return None


def short(tool, inp):
    if tool == "Bash":
        c = re.sub(r"^\s*cd\s+\S+\s*(&&|;)\s*", "", str(inp.get("command", "")))
        return "$ " + c.replace("\n", " ")[:90]
    p = inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("url") or ""
    return f"{tool} {str(p)[-70:]}"


def main():
    since = time.time() - HOURS * 3600
    agents: dict = {}
    for f in BASE.glob("*/**/*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        entries = []
        for ln in lines:
            try:
                entries.append(json.loads(ln))
            except ValueError:
                pass
        cwd = next((e.get("cwd") for e in entries if e.get("cwd")), "")
        if not cwd or not os.path.normcase(os.path.abspath(cwd)).startswith(ROOT):
            continue
        name = who(entries) or "lead"
        a = agents.setdefault(name, {"model": 0.0, "cmd": 0.0, "steps": 0, "slow": [], "kinds": {}})
        pending, prev = {}, None
        for e in entries:
            t = ts(e)
            if t is None or t < since:
                continue
            content = (e.get("message") or {}).get("content")
            if e.get("type") == "assistant":
                if prev is not None and 0 <= t - prev < 1800:
                    a["model"] += t - prev
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "tool_use":
                        pending[c.get("id")] = (t, short(c.get("name", ""), c.get("input") or {}), c.get("name", ""))
                        a["steps"] += 1
            elif e.get("type") == "user":
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        t0, what, kind = pending.pop(c["tool_use_id"])
                        d = t - t0
                        if 0 <= d < 3600:
                            a["cmd"] += d
                            a["kinds"][kind] = a["kinds"].get(kind, 0) + d
                            a["slow"].append((d, what))
            prev = t
    if not agents:
        print(f"No Claude Code sessions for {ROOT} in the last {HOURS:g} h (looked in {BASE}).")
        return
    fmt = lambda s: f"{int(s // 60)}m{int(s % 60):02d}s"
    print(f"Where the time went — last {HOURS:g} h, {ROOT}\n")
    for name, a in sorted(agents.items()):
        tot = a["model"] + a["cmd"] or 1
        print(f"{name:9} steps {a['steps']:4}   MODEL thinking {fmt(a['model']):>8} ({100 * a['model'] / tot:3.0f}%)   "
              f"COMMANDS {fmt(a['cmd']):>8} ({100 * a['cmd'] / tot:3.0f}%)   avg {a['model'] / max(a['steps'], 1):.0f}s think/step")
        kinds = sorted(a["kinds"].items(), key=lambda x: -x[1])[:4]
        print("          commands by tool: " + ", ".join(f"{k} {fmt(v)}" for k, v in kinds))
        for d, what in sorted(a["slow"], reverse=True)[:5]:
            print(f"          {fmt(d):>8}  {what}")
        quick = sorted(d for d, w in a["slow"] if w.startswith(("$ sed", "$ cat", "$ ls", "$ grep", "$ head",
                                                                     "$ python .sdlc/bin/board.py", "$ python3 .sdlc/bin/board.py")))
        if quick and quick[len(quick) // 2] > 20:
            print("          ⚠ simple commands (cat, grep, board.py) take "
                  f"{fmt(quick[len(quick) // 2])} each: they are waiting for a permission click, not running. "
                  "Fix: python .sdlc/bin/team.py allow")
        print()


if __name__ == "__main__":
    main()
