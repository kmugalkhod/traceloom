#!/usr/bin/env python3
"""One approval point for every human gate, answered on the board (Review tab) OR in the chat — never both.

  gate.py ask <change> <kind> [--doc PATH] [--summary TEXT] [--wait MIN]
        Opens the gate (it shows up in the board's Review tab with Approve / Request changes) and waits until the
        human decides, then prints the decision as JSON. With YOLO on it approves at once (and also the moment the
        human switches YOLO on while it waits). Run it in the background; it returns {"decision": "timeout"} after
        gates.wait_minutes so it can simply be asked again.
  gate.py decide <change> <kind> approve|changes|stop|resume [TEXT] [--by NAME]
        Records the human's answer when they gave it in the chat instead (the waiting `ask` then returns).
  gate.py status [<change>]        what is waiting
  gate.py yolo [on|off]            show or switch YOLO (the human only)
  gate.py models [PHASE=MODEL ...] show or set the model per phase (the human only; MODEL "" = config default)

Kinds: intent, spec, plan, review (the loop's gates), architecture, design:<KEY>, escalated:<KEY>.
YOLO never covers pushes to protected branches or production deploys: those keep their own human gates.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import (PHASE_KEYS, audit, changes, find_root, git, header_field, load_config, phase_models,  # noqa: E402
                       read_controls, utcnow, write_controls, yolo)

DOC_KINDS = {"intent": "intent.md", "spec": "spec.md", "plan": "plan.md", "review": "review.md"}
ACTIONS = ("approve", "changes", "stop", "resume")


def resolve(root: Path, cfg: dict, name: str) -> Path:
    hits = [c for c in changes(root, cfg) if c.name == name] or [c for c in changes(root, cfg) if name in c.name]
    if len(hits) != 1:
        raise SystemExit(f"change '{name}' matched {len(hits)} folders: {[h.name for h in hits]}")
    return hits[0]


def _file(root: Path, change: str) -> Path:
    return root / ".sdlc" / "state" / "gates" / f"{change}.json"


def load(root: Path, change: str) -> dict:
    try:
        return json.loads(_file(root, change).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(root: Path, change: str, d: dict) -> None:
    f = _file(root, change)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, f)


_BOARD = None  # the board server passes its own


def _board():
    if _BOARD is not None:
        return _BOARD
    from board import Board
    return Board()


def _human() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "human"


def waiting(root: Path, change: str) -> dict:
    """The loop's open gates for this change: {kind: entry}."""
    return {k: v for k, v in load(root, change).items() if v.get("status") == "waiting"}


def open_gate(root: Path, change: str, kind: str, doc: str = "", summary: str = "", quiet: bool = False) -> dict:
    d = load(root, change)
    e = d.get(kind) or {}
    if e.get("status") != "waiting":
        e = {"status": "waiting", "opened": utcnow(), "doc": doc, "summary": summary[:1500], "round": e.get("round", 0) + 1}
        d[kind] = e
        save(root, change, d)
        try:
            if not quiet:
                _board().event(None, "lead", "status", f"waiting for you: approve the {kind.split(':')[0]}"
                               + (f" ({doc})" if doc else "") + " — Review tab, or answer in the chat")
        except Exception:
            pass
    return e


def resolved(root: Path, cfg: dict, change: Path, kind: str) -> dict | None:
    """The decision, if the human (board or chat) or YOLO has made one."""
    e = load(root, change.name).get(kind) or {}
    if e.get("status") in ACTIONS + ("approved",):
        return {"decision": "approve" if e["status"] == "approved" else e["status"], "text": e.get("text", ""),
                "by": e.get("by", "")}
    if kind == "architecture":
        st = (header_field(root / "sdlc" / "ARCHITECTURE.md", "Status") or "").lower()
        if st in ("approved", "accepted"):
            return {"decision": "approve", "text": "", "by": header_field(root / "sdlc" / "ARCHITECTURE.md", "Approved by") or ""}
        try:
            a = json.loads((root / ".sdlc/state/architecture" / f"{change.name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            a = {}
        if a.get("feedback_pending"):
            return {"decision": "changes", "text": (a.get("feedback") or [""])[-1], "by": ""}
    elif kind.startswith("design:"):
        it = _board().item(kind.split(":", 1)[1]) or {}
        v = str(it.get("qa_by") or "")
        if v.startswith("approved"):
            return {"decision": "approve", "text": "", "by": v.split(":", 1)[-1]}
        if v.startswith("changes") or it.get("status") not in ("review", None):
            return {"decision": "changes", "text": "", "by": v.split(":", 1)[-1]}
    elif kind.startswith("escalated:"):
        if kind.split(":", 1)[1] not in (_board().meta().get("escalated") or []):
            return {"decision": "resume", "text": "", "by": ""}
    return None


def decide(root: Path, cfg: dict, change: Path, kind: str, action: str, text: str = "", by: str = "") -> dict:
    """Apply the human's decision. Used by the CLI (answer given in chat) and the board (Review tab)."""
    text, by = (text or "").strip(), by or (git(root, "config", "user.name") or "").strip() or _human()
    if action not in ACTIONS:
        return {"error": f"action must be one of {', '.join(ACTIONS)}"}
    if action in ("changes", "resume") and not text:
        return {"error": "say what to change" if action == "changes" else "give the team some guidance to resume"}
    bin_ = Path(__file__).resolve().parent
    if kind == "architecture":
        if action not in ("approve", "changes"):
            return {"error": "approve, or changes with text"}
        if (header_field(root / "sdlc" / "ARCHITECTURE.md", "Status") or "").lower() != "proposed":
            return {"error": "the architecture is not waiting for approval"}
        args = ["architecture-approve", change.name, text[:1000], "--by", by] if action == "approve" else \
            ["architecture-changes", change.name, text[:2000]]
        r = subprocess.run([sys.executable, str(bin_ / "team.py"), *args], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip()[-400:]}
    elif kind.startswith("design:"):
        key, b = kind.split(":", 1)[1], _board()
        it = b.item(key) or {}
        if it.get("status") != "review" or str(it.get("qa_by") or "").startswith("approved"):
            return {"error": "that design is not waiting for approval"}
        if action == "approve":
            b.approve_design(key, text[:1000], actor=by)
        elif action == "changes":
            b.request_design_changes(key, text[:2000], actor=by)
        else:
            return {"error": "approve, or changes with text"}
    elif kind.startswith("escalated:"):
        key, b = kind.split(":", 1)[1], _board()
        if key not in (b.meta().get("escalated") or []):
            return {"error": "that task is not waiting for you"}
        if action not in ("resume", "approve") or not text:
            return {"error": "give the team some guidance to resume"}
        b.resume(key, text[:2000], actor=by)
        action = "resume"
    elif kind not in DOC_KINDS:
        return {"error": f"unknown gate '{kind}'"}
    d = load(root, change.name)
    e = d.get(kind) or {}
    if kind in DOC_KINDS and e.get("status") != "waiting":
        return {"error": f"the {kind} is not waiting for approval"}
    e.update(status="approved" if action == "approve" else action, text=text, by=by, decided=utcnow())
    d[kind] = e
    save(root, change.name, d)
    audit(root, "gate", change=change.name, kind=kind, decision=action, by=by)
    try:
        _board().event(None, by, "status", f"{kind}: {action}" + (f" — {text[:200]}" if text else ""))
    except Exception:
        pass
    return {"ok": True, "decision": action, "text": text, "by": by}


def auto(root: Path, cfg: dict, change: Path, kind: str) -> dict:
    """YOLO: approve without asking. A stuck task goes back to the team with a decide-yourself note."""
    who = f"YOLO ({read_controls(root).get('updated_by') or 'human'})"
    if kind.startswith("escalated:"):
        import team
        r = decide(root, cfg, change, kind, "resume", team.YOLO_GUIDANCE.format(why="see the task history"), who)
    else:
        if kind in DOC_KINDS:
            open_gate(root, change.name, kind, quiet=True)
        r = decide(root, cfg, change, kind, "approve", "approved automatically: YOLO mode is on", who)
    if r.get("error"):
        return resolved(root, cfg, change, kind) or {"decision": "approve", "text": "", "by": who}
    return {"decision": r["decision"], "text": r["text"], "by": who, "yolo": True}


def _consume(root: Path, change: Path, kind: str, r: dict) -> dict:
    """A decision is handed to the asker once; the next ask on this gate is a new round."""
    d = load(root, change.name)
    if kind in d:
        d[kind]["consumed"] = True
        save(root, change.name, d)
    return r


def ask(root: Path, cfg: dict, change: Path, kind: str, doc: str = "", summary: str = "", wait_min: float | None = None) -> dict:
    if kind in DOC_KINDS:
        e = load(root, change.name).get(kind) or {}
        if e.get("status") not in (None, "waiting"):
            if not e.get("consumed"):  # decided while nobody was asking (e.g. after a timeout): hand it over
                return _consume(root, change, kind, resolved(root, cfg, change, kind))
            d = load(root, change.name)  # an earlier round: this is a new question
            d.pop(kind, None)
            save(root, change.name, d)
    elif done := resolved(root, cfg, change, kind):
        return done
    if yolo(root):
        return _consume(root, change, kind, auto(root, cfg, change, kind))
    open_gate(root, change.name, kind, doc, summary)
    limit = time.time() + 60 * (wait_min if wait_min is not None else float((cfg.get("gates") or {}).get("wait_minutes", 110)))
    poll = float(os.environ.get("SDLC_GATE_POLL", "2"))
    while time.time() < limit:
        r = resolved(root, cfg, change, kind)
        if r:
            return _consume(root, change, kind, r)
        if yolo(root):  # the human switched YOLO on while this waited
            return _consume(root, change, kind, auto(root, cfg, change, kind))
        time.sleep(poll)
    return {"decision": "timeout", "text": "", "by": "", "hint": "nobody decided yet — run the same gate.py ask again"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ask")
    s.add_argument("change"); s.add_argument("kind")
    s.add_argument("--doc", default=""); s.add_argument("--summary", default=""); s.add_argument("--wait", type=float)
    s = sub.add_parser("decide")
    s.add_argument("change"); s.add_argument("kind"); s.add_argument("action", choices=ACTIONS)
    s.add_argument("text", nargs="?", default=""); s.add_argument("--by", default="")
    s = sub.add_parser("status"); s.add_argument("change", nargs="?")
    s = sub.add_parser("yolo"); s.add_argument("state", nargs="?", choices=("on", "off"))
    s = sub.add_parser("models"); s.add_argument("set", nargs="*", metavar="PHASE=MODEL")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("no .sdlc/config.json here — run /sdlc:init first")
    cfg = load_config(root)
    human_only = a.cmd in ("yolo", "models") and (getattr(a, "state", None) or getattr(a, "set", None))
    if (human_only or a.cmd == "decide") and os.environ.get("SDLC_AGENT"):
        sys.exit("only the human decides gates and switches YOLO or models (on the board, or in their own terminal)")
    if a.cmd == "ask":
        out = ask(root, cfg, resolve(root, cfg, a.change), a.kind, a.doc, a.summary, a.wait)
    elif a.cmd == "decide":
        out = decide(root, cfg, resolve(root, cfg, a.change), a.kind, a.action, a.text, a.by)
    elif a.cmd == "status":
        names = [resolve(root, cfg, a.change).name] if a.change else [c.name for c in changes(root, cfg)]
        out = {"yolo": yolo(root), "waiting": {n: w for n in names if (w := waiting(root, n))}}
    elif a.cmd == "yolo":
        if a.state:
            write_controls(root, yolo=a.state == "on")
        out = {"yolo": yolo(root)}
    else:
        if a.set:
            pairs = dict(x.split("=", 1) for x in a.set if "=" in x)
            if "all" in pairs:
                pairs = {p: pairs["all"] for p in PHASE_KEYS} | {k: v for k, v in pairs.items() if k != "all"}
            bad = [p for p in pairs if p not in PHASE_KEYS]
            if bad:
                sys.exit(f"unknown phase {bad}; phases: {', '.join(PHASE_KEYS)} or all")
            write_controls(root, models=pairs)
        out = {m["phase"]: m["model"] for m in phase_models(root)}
    print(json.dumps(out, indent=2))
    if isinstance(out, dict) and out.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
