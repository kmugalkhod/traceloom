"""What would this project's agent runs cost on the Claude API? Reads Claude Code's own session records (they store the
token usage of every model call, also on a subscription) and prices them at API rates, per agent and per model.

Run from the project folder:   python .sdlc/bin/cost_report.py            (last 24 hours)
                               python .sdlc/bin/cost_report.py 72         (last 72 hours)
                               python .sdlc/bin/cost_report.py --prices my_prices.json   (your own rates)
The live board shows the same numbers: a total for the run, per teammate and per card.
Prices: USD per million tokens, from platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-05).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# model family + version → (input, 5m cache write, 1h cache write, cache read, output) per million tokens
PRICES = {
    "fable-5-1": (10, 12.5, 20, 0.25, 50), "mythos-5-1": (10, 12.5, 20, 0.25, 50),
    "fable-5": (10, 12.5, 20, 1, 50), "mythos-5": (10, 12.5, 20, 1, 50),
    "opus-5-5": (4, 5, 8, 0.20, 20), "opus-5": (5, 6.25, 10, 0.50, 25),
    "opus-4-8": (5, 6.25, 10, 0.50, 25), "opus-4-7": (5, 6.25, 10, 0.50, 25), "opus-4-6": (5, 6.25, 10, 0.50, 25),
    "opus-4-5": (5, 6.25, 10, 0.50, 25), "opus-4-1": (15, 18.75, 30, 1.50, 75), "opus-4": (15, 18.75, 30, 1.50, 75),
    "sonnet-5-5": (2, 2.5, 4, 0.10, 10), "sonnet-5": (2, 2.5, 4, 0.20, 10),
    "sonnet-4-6": (3, 3.75, 6, 0.30, 15), "sonnet-4-5": (3, 3.75, 6, 0.30, 15), "sonnet-4": (3, 3.75, 6, 0.30, 15),
    "haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50), "haiku-5-5-long": (0.50, 0.625, 1, 0.05, 2.50),
    "haiku-4-5": (1, 1.25, 2, 0.10, 5), "haiku-3-5": (0.80, 1, 1.60, 0.08, 4),
}
HAIKU_LONG = 100_000  # Haiku 5.5: prompts over this many tokens (incl. cache) pay the long rate
NAME = re.compile(r"assignments[\\/]+((?:dev|qa)-\d+|designer)-|\byou are ((?:dev|qa)-\d+|designer|the designer|"
                  r"the planner|the architect|architect|the team lead|the independent gate)\b", re.I)
# which cards a session is working on: an agent-team assignment, or the one task of a background-mode session
KEYS = [re.compile(r"New assignment \(([^)]*)\)"), re.compile(r"YOUR TASK ((?:T|D)-\d+)"),
        re.compile(r"handed you task ((?:T|D)-\d+)"), re.compile(r"BLOCKED on task ((?:T|D)-\d+)"),
        re.compile(r"\bdesign(?:er)?\b[^\n]{0,200}?\b(D-\d+)\b", re.I)]
_FILES: dict = {}  # path -> incremental parse state, so a long-running board re-reads only what was appended


def base_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def price_key(model: str, prompt_tokens: int) -> str | None:
    m = re.search(r"(fable|mythos|opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model or "")
    if not m:
        return None
    fam, major, minor = m.group(1), m.group(2), m.group(3)
    for k in ([f"{fam}-{major}-{minor}"] if minor and len(minor) < 3 else []) + [f"{fam}-{major}"]:
        if k in PRICES:
            return k + "-long" if k == "haiku-5-5" and prompt_tokens > HAIKU_LONG else k
    return None


def _agent_of(entry: dict) -> str | None:
    if entry.get("type") != "user":
        return None
    m = NAME.search(json.dumps(entry.get("message", ""))[:4000])
    if not m:
        return None
    return (m.group(1) or m.group(2)).lower().replace("the independent gate", "gatekeeper").replace("the ", "")


def _keys_of(entry: dict) -> list:
    """Task keys a prompt hands to the agent (ignores tool results, which can mention any task)."""
    c = (entry.get("message") or {}).get("content")
    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict)
                                                and x.get("type") == "text")
    for rx in KEYS:
        m = rx.search(text[:3000])
        if m:
            return [k.strip() for k in m.group(1).split(",") if re.match(r"^[TD]-\d+$", k.strip())]
    return []


def _ts(e: dict) -> float:
    try:
        return datetime.fromisoformat(str(e["timestamp"]).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _call(e: dict) -> dict | None:
    msg = e.get("message") or {}
    u = msg.get("usage")
    model = msg.get("model") or "?"
    if e.get("type") != "assistant" or not u or model.startswith("<"):  # synthetic messages: no model call
        return None
    inp, out = u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0
    cr, cw = u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0
    cc = u.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    w5m = cc.get("ephemeral_5m_input_tokens", cw - w1h) or 0
    k = price_key(model, inp + cr + cw)
    p = PRICES.get(k) if k else None
    return {"id": msg.get("id") or e.get("requestId") or e.get("uuid"), "ts": _ts(e), "model": model,
            "family": (k or model).replace("-long", ""), "priced": bool(p), "inp": inp, "cw": cw, "cr": cr,
            "out": out, "cost": ((inp * p[0] + w5m * p[1] + w1h * p[2] + cr * p[3] + out * p[4]) / 1e6) if p else 0.0}


def _scan(f: Path) -> dict:
    """Incremental: parse only the lines appended since the last call."""
    st = f.stat()
    s = _FILES.get(f)
    if s is None or st.st_size < s["offset"]:
        s = _FILES[f] = {"offset": 0, "agent": None, "cwd": "", "calls": {}, "head": 0, "cur": []}
    if st.st_size == s["offset"]:
        return s
    with open(f, "rb") as fh:
        fh.seek(s["offset"])
        chunk = fh.read()
    end = chunk.rfind(b"\n") + 1  # keep a half-written last line for next time
    for ln in chunk[:end].decode("utf-8", errors="replace").splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if not s["cwd"] and e.get("cwd"):
            s["cwd"] = e["cwd"]
        if s["agent"] is None and s["head"] < 6 and e.get("type") == "user":
            s["head"] += 1
            s["agent"] = _agent_of(e)
        if e.get("type") == "user":
            k = _keys_of(e)
            if k:
                s["cur"] = k
        c = _call(e)
        if c:
            c["keys"] = list(s.get("cur") or [])
            s["calls"][c["id"]] = c  # one call is written as several lines (one per content block)
    s["offset"] += end
    return s


def collect(root: Path, since: float = 0.0) -> list[dict]:
    """Every priced model call made by a Claude Code session working in `root` (or its worktrees) since `since`."""
    want = os.path.normcase(os.path.abspath(str(root)))
    out, seen = [], set()
    for f in base_dir().glob("*/**/*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
            s = _scan(f)
        except OSError:
            continue
        cwd = os.path.normcase(os.path.abspath(s["cwd"])) if s["cwd"] else ""
        if not cwd or not (cwd == want or cwd.startswith(want.rstrip("\\/") + os.sep)):
            continue
        agent = s["agent"] or "lead"
        for c in s["calls"].values():
            if c["id"] in seen or c["ts"] < since:
                continue
            seen.add(c["id"])
            out.append({**c, "agent": agent})
    return sorted(out, key=lambda c: c["ts"])


def by_task(calls: list[dict], assignments: list[tuple[float, str, list]]) -> dict:
    """Split each teammate call over the cards it was assigned at that moment. assignments: (ts, agent, keys)."""
    per: dict = {}
    for c in calls:
        cur = c.get("keys") or None  # the session says which cards it is on
        for ts, agent, keys in assignments if not cur else []:
            if agent == c["agent"] and ts <= c["ts"] + 1:
                cur = keys
        for k in cur or []:
            per[k] = per.get(k, 0.0) + c["cost"] / len(cur)
    return per


def summary(calls: list[dict]) -> dict:
    agents, models = {}, {}
    for c in calls:
        agents[c["agent"]] = agents.get(c["agent"], 0.0) + c["cost"]
        models[c["family"]] = models.get(c["family"], 0.0) + c["cost"]
    allin = sum(c["inp"] + c["cw"] + c["cr"] for c in calls)
    return {"total": sum(agents.values()), "calls": len(calls), "agents": agents, "models": models,
            "cache_share": (sum(c["cr"] for c in calls) / allin) if allin else 0.0,
            "unpriced": sorted({c["model"] for c in calls if not c["priced"]})}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hours", nargs="?", type=float, default=24)
    ap.add_argument("--prices", help="JSON file {model-key: [input, cache5m, cache1h, cache_read, output]}")
    a = ap.parse_args()
    if a.prices:
        PRICES.update({k: tuple(v) for k, v in json.loads(Path(a.prices).read_text(encoding="utf-8")).items()})
    root = Path.cwd()
    calls = collect(root, time.time() - a.hours * 3600)
    if not calls:
        print(f"No Claude Code sessions for {root} in the last {a.hours:g} h (looked in {base_dir()}).")
        return
    rows: dict = {}
    for c in calls:
        r = rows.setdefault((c["agent"], c["family"]), [0, 0, 0, 0, 0, 0.0])
        for i, v in enumerate((1, c["inp"], c["cw"], c["cr"], c["out"])):
            r[i] += v
        r[5] += c["cost"]
    fm = lambda n: f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    print(f"API-equivalent cost — last {a.hours:g} h, {root}\n")
    print(f"{'agent':12} {'model':12} {'calls':>6} {'input':>8} {'cache write':>12} {'cache read':>11} {'output':>8} {'cost $':>9}")
    tot = [0, 0, 0, 0, 0, 0.0]
    for (agent, model), r in sorted(rows.items(), key=lambda x: -x[1][5]):
        print(f"{agent:12} {model:12} {r[0]:6} {fm(r[1]):>8} {fm(r[2]):>12} {fm(r[3]):>11} {fm(r[4]):>8} {r[5]:9.2f}")
        tot = [x + y for x, y in zip(tot, r)]
    print(f"{'TOTAL':12} {'':12} {tot[0]:6} {fm(tot[1]):>8} {fm(tot[2]):>12} {fm(tot[3]):>11} {fm(tot[4]):>8} {tot[5]:9.2f}")
    s = summary(calls)
    print(f"\n{100 * s['cache_share']:.0f}% of input tokens were cache reads (billed at about a tenth of normal input).")
    top = max(s["agents"], key=s["agents"].get)
    print(f"Most expensive agent: {top} (${s['agents'][top]:.2f}, {100 * s['agents'][top] / max(s['total'], 1e-9):.0f}% "
          "of the total).")
    if s["unpriced"]:
        print(f"Not priced (unknown model, add it with --prices): {', '.join(s['unpriced'])}")
    print("On a Claude subscription you pay the plan, not this. This is what the same run would cost on the API.")


if __name__ == "__main__":
    main()
