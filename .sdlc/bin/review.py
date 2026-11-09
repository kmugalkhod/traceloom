"""Review room backend: what the agents produced for the current change, each document's versions, a chat with the
agent that wrote it, and the human's approve / changes / resume decisions. Served by board_server.py under /review.

Agents answer in a headless session that resumes its own context (one per document). Only the architect may edit,
and only its own files while they are still proposed; accepted documents are question-and-answer only. Design
changes and escalations go through the board's existing design / resume flow, so a running team picks them up.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import changes, git, header_field, load_config, stage_of, yolo  # noqa: E402
import gate  # noqa: E402

AGENT_MODEL = {"architect": "opus"}  # others: team.models.<agent> or sonnet
LOCK = threading.Lock()
RUNNING: dict[str, threading.Thread] = {}


def _slug(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


class Review:
    def __init__(self, root: Path, cfg: dict, board):
        self.root, self.cfg, self.board = Path(root), cfg or {}, board
        gate._BOARD = board
        self.state = self.root / ".sdlc" / "state" / "review"
        self._cache = (0.0, None)

    # ---------- which change, which artifacts ----------
    def current_change(self) -> str:
        """The run the board shows live: the team's change while it still has work, else the change most recently
        worked on (so a new /sdlc:loop shows up from its intent, long before any plan)."""
        m = self.board.meta() or {}
        base = self.root / self.cfg.get("changes_dir", "sdlc/changes")
        items = [i for i in self.board.items() if i["type"] in ("task", "design")] if m.get("change") else []
        if m.get("change") and (base / m["change"]).is_dir() and items and not all(i["status"] == "done" for i in items):
            return m["change"]
        best, when = "", -1.0
        for c in (base.iterdir() if base.is_dir() else []):
            if c.is_dir():
                t = self._touched(c)
                if t > when:
                    best, when = c.name, t
        return best or m.get("change", "")

    def board_change(self) -> str:
        return (self.board.meta() or {}).get("change", "")

    def change_dir(self, want: str | None = None) -> Path | None:
        """The change being looked at: the one asked for, else the live one."""
        base = self.root / self.cfg.get("changes_dir", "sdlc/changes")
        for name in (want, self.current_change()):
            if name and "/" not in name and "\\" not in name and ".." not in name and (base / name).is_dir():
                return base / name
        dirs = sorted((d for d in base.glob("*") if d.is_dir()), key=lambda d: d.stat().st_mtime) if base.is_dir() else []
        return dirs[-1] if dirs else None

    def worktree(self, name: str) -> Path:
        return self.root / ((self.cfg.get("team") or {}).get("worktree_dir") or ".sdlc/worktrees") / name

    def artifacts(self, fresh: bool = False, change: str | None = None) -> list[dict]:
        ch = self.change_dir(change)
        t, key, cached = self._cache if len(self._cache) == 3 else (0.0, None, None)
        if cached is not None and not fresh and key == (ch.name if ch else "") and time.time() - t < 3:
            return cached
        out = []
        live = bool(ch) and ch.name == self.board_change()  # the board's items belong to this change
        gates = gate.load(self.root, ch.name) if ch else {}

        def add(id_, name, path, agent, status, kind="doc", needs=False, extra=None):
            out.append({"id": id_, "name": name, "path": path, "agent": agent, "status": status or "", "kind": kind,
                        "needs": needs, "change": ch.name if ch else "", **(extra or {})})

        def file_status(p: Path) -> str:
            return (header_field(p, "Status") or "").strip().lower() if p.is_file() else ""

        if ch:
            rel = ch.relative_to(self.root).as_posix()
            for fname, name, agent in (("intent.md", "Intent", "lead"), ("spec.md", "Spec", "lead"),
                                       ("plan.md", "Plan notes", "planner"), ("review.md", "Code review", "lead")):
                p, g = ch / fname, gates.get(fname[:-3]) or {}
                if p.is_file():
                    st = g.get("status") if g.get("status") in ("waiting", "approved", "changes") else file_status(p)
                    add(f"file:{rel}/{fname}", name, f"{rel}/{fname}", agent, st, kind=fname[:-3],
                        needs=g.get("status") == "waiting")
            esc = ch / "ESCALATION.md"
            if esc.is_file():
                add(f"file:{rel}/ESCALATION.md", "Escalation", f"{rel}/ESCALATION.md", "lead", "waiting", needs=True)
        arch = self.root / "sdlc" / "ARCHITECTURE.md"
        if arch.is_file():
            st = file_status(arch)
            revising = False
            if ch and st == "proposed":  # the human asked for changes; the architect revises in the team run
                try:
                    revising = bool(json.loads((self.root / ".sdlc/state/architecture" / f"{ch.name}.json")
                                               .read_text(encoding="utf-8")).get("feedback_pending"))
                except (OSError, ValueError):
                    pass
            add("file:sdlc/ARCHITECTURE.md", "Architecture", "sdlc/ARCHITECTURE.md", "architect",
                "revising" if revising else st, kind="architecture", needs=st == "proposed" and not revising)
        for adr in sorted((self.root / "sdlc" / "adr").glob("*.md")):
            title = adr.stem.split("-", 1)
            add(f"file:sdlc/adr/{adr.name}", f"ADR-{title[0]} {title[1].replace('-', ' ') if len(title) > 1 else ''}".strip(),
                f"sdlc/adr/{adr.name}", "architect", file_status(adr), kind="adr")
        for it in (self.board.items("design") if live else []):
            waiting = it["status"] == "review" and not str(it.get("qa_by") or "").startswith("approved")
            st = "waiting" if waiting else "approved" if it["status"] in ("review", "done") else it["status"]
            add(f"design:{it['key']}", f"Design {it['key']}", f"design/{it['key']}", "designer", st, kind="design",
                needs=waiting, extra={"key": it["key"], "title": it.get("title", "")})
        for key in ((self.board.meta() or {}).get("escalated", []) or [] if live else []):
            it = self.board.item(key) or {}
            add(f"escalated:{key}", f"Escalated {key}", f"task/{key}", "lead", "waiting", kind="escalation",
                needs=True, extra={"key": key, "title": it.get("title", "")})
        if ch and (ch / "breakdown.json").is_file():
            add("plan", "Plan", f"{ch.relative_to(self.root).as_posix()}/breakdown.json", "planner", "planned", kind="plan")
        self._cache = (time.time(), ch.name if ch else "", out)
        return out

    def count(self, change: str | None = None) -> int:
        try:
            return sum(1 for a in self.artifacts(change=change) if a["needs"])
        except Exception:
            return 0

    def find(self, id_: str, change: str | None = None) -> dict | None:
        return next((a for a in self.artifacts(fresh=True, change=change) if a["id"] == id_), None)

    # ---------- runs and stages (what the board shows before any task exists) ----------
    def runs(self) -> list[dict]:
        """Every change in the project, newest first, with its stage and whether the board has a saved snapshot."""
        c = getattr(self, "_runs_cache", None)
        if c and time.time() - c[0] < 5:
            return c[1]
        hist = self.root / ".sdlc" / "state" / "history"
        cur = self.current_change()
        out = []
        for c in sorted(changes(self.root, self.cfg), key=lambda d: (d.name != cur, -self._touched(d))):
            st = self.stages(c.name)
            label = next((x["label"] + (" — needs you" if x["state"] == "waiting" else "") for x in st["stages"]
                          if x["key"] == st["stage"]), "") if st.get("stages") else stage_of(c)
            out.append({"change": c.name, "stage": label or "done", "live": c.name == cur,
                        "snapshot": (hist / f"{c.name}.json").is_file()})
        self._runs_cache = (time.time(), out)
        return out

    @staticmethod
    def _touched(c: Path) -> float:
        return max([c.stat().st_mtime] + [f.stat().st_mtime for f in c.iterdir() if f.is_file()])

    def stages(self, change: str | None = None) -> dict:
        """The loop's stages for a change, each done / waiting (needs you) / active / pending / skipped."""
        ch = self.change_dir(change)
        if not ch:
            return {"change": "", "stages": [], "yolo": yolo(self.root)}
        live = ch.name == self.board_change()
        g = gate.load(self.root, ch.name)
        fs = lambda f: (header_field(ch / f, "Status") or "").lower() if (ch / f).is_file() else ""
        ok = ("accepted", "approved", "implemented", "done", "in-progress")
        arch = self.root / "sdlc" / "ARCHITECTURE.md"
        ast = (header_field(arch, "Status") or "").lower() if arch.is_file() else ""
        items = self.board.items() if live else self._snapshot_items(ch.name)
        tasks = [i for i in items if i["type"] == "task"]
        designs = [i for i in items if i["type"] == "design"]
        done_t = sum(t["status"] == "done" for t in tasks)
        st = []

        def add(key, label, state, note=""):
            st.append({"key": key, "label": label, "state": state, "note": note})

        def doc(key, label, f):
            gs = (g.get(key) or {}).get("status")
            if gs == "waiting":
                return add(key, label, "waiting", "needs your approval")
            if fs(f) in ok or gs == "approved":
                return add(key, label, "done")
            return add(key, label, "active" if (ch / f).is_file() else "pending")

        doc("intent", "Intent", "intent.md")
        doc("spec", "Spec", "spec.md")
        later = (ch / "plan.md").is_file() or (ch / "breakdown.json").is_file() or bool(tasks)
        if ast == "proposed":
            add("architecture", "Architecture", "waiting", "needs your approval")
        elif ast in ("approved", "accepted"):
            add("architecture", "Architecture", "done")
        else:
            add("architecture", "Architecture", "skipped" if later else "pending", "unchanged" if later else "")
        if (ch / "breakdown.json").is_file() or tasks:
            add("plan", "Plan", "done", f"{len(tasks)} tasks" if tasks else "")
        else:
            doc("plan", "Plan", "plan.md")
        if designs:
            w = any(d["status"] == "review" and not str(d.get("qa_by") or "").startswith("approved") for d in designs)
            dn = all(d["status"] == "done" or str(d.get("qa_by") or "").startswith("approved") for d in designs)
            add("design", "Design", "waiting" if w else "done" if dn else "active", "needs your approval" if w else "")
        else:
            add("design", "Design", "skipped" if tasks else "pending")
        if tasks:
            add("build", "Build", "done" if done_t == len(tasks) else "active", f"{done_t}/{len(tasks)}")
            inq = sum(t["status"] == "in_qa" for t in tasks)
            add("qa", "QA", "done" if done_t == len(tasks) else "active" if inq or done_t else "pending",
                f"{inq} testing" if inq else "")
        else:
            add("build", "Build", "active" if fs("plan.md") in ok else "pending")
            add("qa", "QA", "pending")
        doc("review", "Review", "review.md")
        if (g.get("review") or {}).get("status") == "approved" or fs("review.md") in ok:
            add("done", "Done", "done")
        else:
            add("done", "Done", "pending")
        if live and (self.board.meta() or {}).get("escalated"):
            for x in st:
                if x["key"] == "build":
                    x["state"], x["note"] = "waiting", f"{len(self.board.meta()['escalated'])} task(s) need you"
        cur = next((x["key"] for x in st if x["state"] in ("waiting", "active")), None) or \
            next((x["key"] for x in st if x["state"] == "pending"), "done")
        if cur == "done" and not any(x["key"] == "done" and x["state"] == "done" for x in st):
            cur = ""
        return {"change": ch.name, "stage": cur, "stages": st, "live": ch.name == self.current_change(), "yolo": yolo(self.root),
                "waiting": sum(x["state"] == "waiting" for x in st)}

    def _snapshot_items(self, change: str) -> list[dict]:
        try:
            return json.loads((self.root / ".sdlc/state/history" / f"{change}.json").read_text(encoding="utf-8")).get("items", [])
        except (OSError, ValueError):
            return []

    def snapshot(self, change: str) -> dict | None:
        """A run's board: the live board when it still holds that change, else the copy saved when it moved on."""
        if "/" in change or "\\" in change or ".." in change:
            return None
        if change == self.board_change():
            return self.board.snapshot()
        try:
            return json.loads((self.root / ".sdlc/state/history" / f"{change}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---------- documents and versions ----------
    def _design_notes(self, key: str, change: str | None = None) -> tuple[str, Path | None]:
        ch = self.change_dir(change)
        if not ch:
            return "", None
        rel = ch.relative_to(self.root) / "design"
        for base in (self.worktree("designer"), self.worktree("integration"), self.root):
            for name in ("design-notes.md", "DESIGN.md"):
                p = base / rel / name
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace"), p
        return "", None

    def _plan_md(self, path: str) -> str:
        try:
            bd = json.loads((self.root / path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "# Plan\n\nNo plan yet."
        lines = ["# Plan", "", f"{sum(len(s.get('tasks', [])) for s in bd.get('stories', []))} tasks in "
                 f"{len(bd.get('stories', []))} stories.", ""]
        for i, s in enumerate(bd.get("stories", []), 1):
            lines += [f"## S-{i} {s.get('title', '')}", ""]
            for t in s.get("tasks", []):
                after = t.get("depends_on") or t.get("after") or []
                lines.append(f"- **{t.get('title', '')}**" + (f" — after {', '.join(map(str, after))}" if after else ""))
            lines.append("")
        return "\n".join(lines)

    def versions(self, path: str) -> list[dict]:
        log = git(self.root, "log", "-n", "12", "--format=%H%x09%ad%x09%s", "--date=format:%d %b %H:%M", "--", path)
        vers = [{"id": h, "label": s[:60], "date": d} for h, d, s in (ln.split("\t", 2) for ln in log.splitlines() if ln.count("\t") >= 2)]
        vers.reverse()
        cur = self.root / path
        if cur.is_file():
            head = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=self.root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            if head.returncode != 0 or head.stdout != cur.read_text(encoding="utf-8", errors="replace"):
                vers.append({"id": "working", "label": "latest (not committed yet)", "date": "now"})
        return vers

    def _content_at(self, path: str, ver: str) -> str:
        if ver == "working" or not ver:
            p = self.root / path
            return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        if not re.fullmatch(r"[0-9a-f]{7,40}", ver):
            return ""
        r = subprocess.run(["git", "show", f"{ver}:{path}"], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.stdout if r.returncode == 0 else ""

    def doc(self, id_: str, ver: str = "", change: str | None = None) -> dict | None:
        a = self.find(id_, change)
        if not a:
            return None
        res = {**a, "content": "", "versions": [], "version": "", "added": [], "editable": self.editable(a),
               "diagram": None, "mockup": None}
        if id_.startswith("file:"):
            path = a["path"]
            vers = self.versions(path)
            res["versions"] = vers
            ver = ver or (vers[-1]["id"] if vers else "working")
            res["version"] = ver
            res["content"] = self._content_at(path, ver)
            ids = [v["id"] for v in vers]
            if ver in ids and ids.index(ver) > 0:  # what this version added over the one before
                prev = self._content_at(path, ids[ids.index(ver) - 1]).splitlines()
                cur = res["content"].splitlines()
                sm = difflib.SequenceMatcher(a=prev, b=cur, autojunk=False)
                res["added"] = [j for tag, _, _, j1, j2 in sm.get_opcodes() if tag in ("insert", "replace")
                                for j in range(j1, j2)]
            if a["kind"] == "architecture":
                for img in ("sdlc/architecture.png", "sdlc/architecture.svg"):
                    if (self.root / img).is_file():
                        res["diagram"] = img
                        break
        elif a["kind"] == "design":
            res["content"], _ = self._design_notes(a["key"], a["change"])
            res["content"] = res["content"] or f"# Design {a['key']}\n\n{a.get('title', '')}\n\nThe designer is still working."
            res["mockup"] = f"design/{a['key']}/"
        elif a["kind"] == "escalation":
            ch = self.change_dir(a["change"])
            esc = ch / "ESCALATION.md" if ch else None
            reason = ""
            try:
                reason = self.board.escalation_reason(a["key"]) or ""
            except Exception:
                pass
            res["content"] = (f"# {a['key']} is waiting for you\n\n**{a.get('title', '')}**\n\n" + (reason or "")
                              + ("\n\n" + esc.read_text(encoding="utf-8", errors="replace") if esc and esc.is_file() else ""))
        elif a["kind"] == "plan":
            res["content"] = self._plan_md(a["path"])
        return res

    def file(self, rel: str) -> Path | None:
        """Images the documents show (diagram); only image types inside sdlc/."""
        p = (self.root / rel).resolve()
        base = (self.root / "sdlc").resolve()
        if not str(p).startswith(str(base) + os.sep) or p.suffix.lower() not in (".png", ".svg", ".jpg", ".jpeg", ".webp"):
            return None
        return p if p.is_file() else None

    # ---------- who may change what ----------
    def editable(self, a: dict) -> bool:
        return a["kind"] in ("architecture", "adr") and (header_field(self.root / "sdlc" / "ARCHITECTURE.md", "Status")
                                                         or "").lower() == "proposed"

    # ---------- chat ----------
    def _chat_file(self, id_: str) -> Path:
        return self.state / f"chat-{_slug(id_)}.json"

    def chat(self, id_: str) -> dict:
        f = self._chat_file(id_)
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {"messages": [], "session": None}
        d["pending"] = id_ in RUNNING and RUNNING[id_].is_alive()
        return d

    def _save_chat(self, id_: str, d: dict) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        f = self._chat_file(id_)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in d.items() if k != "pending"}, indent=1), encoding="utf-8")
        os.replace(tmp, f)

    def post(self, id_: str, text: str, quote: str = "", section: str = "", change: str | None = None) -> dict:
        a = self.find(id_, change)
        if not a:
            return {"error": "unknown document"}
        with LOCK:
            if id_ in RUNNING and RUNNING[id_].is_alive():
                return {"error": "the agent is still answering your last message"}
            d = self.chat(id_)
            d["messages"].append({"who": "you", "text": text, "quote": quote[:600], "section": section[:120],
                                  "ts": time.time()})
            self._save_chat(id_, d)
            th = threading.Thread(target=self._answer, args=(a, text, quote, section), daemon=True)
            RUNNING[id_] = th
            th.start()
        return {"ok": True}

    def _cmd(self) -> list[str]:
        return json.loads(os.environ["SDLC_CLAUDE_CMD"]) if os.environ.get("SDLC_CLAUDE_CMD") else [shutil.which("claude") or "claude"]

    def _prompt(self, a: dict, text: str, quote: str, section: str, editable: bool) -> str:
        who = {"architect": "the architect", "designer": "the designer", "planner": "the planner"}.get(a["agent"], "the team lead")
        ch = self.change_dir(a.get("change"))
        ctx = f"The change folder is {ch.relative_to(self.root).as_posix()}." if ch else ""
        doc = a["path"] if a["kind"] not in ("design", "escalation") else {"design": "the design notes and mockup",
                                                                             "escalation": "the escalated task"}[a["kind"]]
        rules = [
            f"You are {who} on this project's AI-native SDLC team. {ctx}",
            f"The human is reviewing {doc} (status: {a['status'] or 'n/a'}) in the Review room and wrote to you.",
            "Answer directly and briefly (under 150 words), in plain text with short lists if useful. Name the section "
            "you mean. Read the files you need first; never guess.",
        ]
        if editable:
            rules.append(
                f"If they ask for a change, make it yourself now in {a['path']} (and the ADRs in sdlc/adr/ if a decision "
                "changes). Keep 'Status: proposed'; never set approved; only the human approves. After changing "
                "ARCHITECTURE.md run `python3 .sdlc/bin/arch_export.py` (use `python` on Windows) so the diagram matches. "
                "End your reply with one line: `CHANGED: <what you changed>`. If the request conflicts with the intent, "
                "spec or sdlc/MISSION.md, explain that instead of changing it.")
        elif a["kind"] == "design":
            rules.append("Do not edit files here. If they want changes, tell them to press 'Request changes' so the "
                         "designer revises the mockup in the team run.")
        else:
            rules.append(f"This is {a['status'] or 'decided'}: do not edit any file. If they want it changed, say how "
                         "(for example a new change, or which command to run).")
        msg = (f'Quoted from the document{" (section: " + section + ")" if section else ""}:\n"""{quote}"""\n\n'
               if quote else "") + f"Their message:\n{text}"
        return "\n".join(rules) + "\n\n" + msg

    def _answer(self, a: dict, text: str, quote: str, section: str) -> None:
        id_ = a["id"]
        d = self.chat(id_)
        editable = self.editable(a)
        cfg = load_config(self.root)  # fresh: the human may have switched models on the board
        model = ((cfg.get("team") or {}).get("models") or {}).get(a["agent"]) or \
            (cfg.get("models") or {}).get("spec" if a["agent"] == "lead" else "", "") or AGENT_MODEL.get(a["agent"], "sonnet")
        tools = ["Read", "Grep", "Glob", "Bash(git log*)", "Bash(git show*)", "Bash(git diff*)", "Bash(ls*)"]
        if editable:
            tools += ["Edit(sdlc/ARCHITECTURE.md)", "Write(sdlc/ARCHITECTURE.md)", "Edit(sdlc/adr/**)", "Write(sdlc/adr/**)",
                      "Bash(python3 .sdlc/bin/arch_export.py*)", "Bash(python .sdlc/bin/arch_export.py*)"]
        cwd = self.root
        if a["kind"] == "design" and self.worktree("designer").is_dir():
            cwd = self.worktree("designer")
        cmd = [*self._cmd(), "-p", "--output-format", "json", "--model", model, "--allowedTools", ",".join(tools)]
        if d.get("session"):
            cmd += ["--resume", d["session"]]
        before = self._hash(a)
        reply, cost, session, err = "", 0.0, d.get("session"), ""
        try:
            r = subprocess.run(cmd, cwd=cwd, input=self._prompt(a, text, quote, section, editable), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=int((cfg.get("team") or {}).get("review_timeout_s", 900)),
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            for line in reversed((r.stdout or "").strip().splitlines()):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ("result" in ev or ev.get("type") == "result"):
                    reply, cost = ev.get("result") or "", ev.get("total_cost_usd") or 0.0
                    session = ev.get("session_id") or session
                    if ev.get("is_error"):
                        err = reply or "the agent reported an error"
                    break
            if not reply and not err:
                err = (r.stderr or r.stdout or "no answer")[-400:]
        except subprocess.TimeoutExpired:
            err = "the agent took too long to answer"
        except OSError as e:
            err = f"could not start claude: {e}"
        changed = self._hash(a) != before
        d = self.chat(id_)
        d["session"] = session
        m = re.search(r"^CHANGED:\s*(.+)$", reply or "", re.M)
        d["messages"].append({"who": a["agent"], "text": (re.sub(r"^CHANGED:.*$", "", reply, flags=re.M).strip()
                                                          if reply else f"⚠ {err}"), "ts": time.time(),
                              "cost": round(cost, 4), "error": bool(err)})
        if changed:
            d["messages"].append({"who": "system", "text": f"{a['name']} updated" + (f": {m.group(1).strip()}" if m else ""),
                                  "ts": time.time(), "changed": True})
            try:
                self.board.event(None, a["agent"], "design", f"{a['name']} revised from the Review room"
                                 + (f": {m.group(1).strip()}" if m else ""))
            except Exception:
                pass
        self._save_chat(id_, d)
        self._cache = (0.0, None)

    def _hash(self, a: dict) -> str:
        h = hashlib.sha1()
        paths = [self.root / a["path"]] if a["id"].startswith("file:") else []
        if a["kind"] in ("architecture", "adr"):
            paths = [self.root / "sdlc" / "ARCHITECTURE.md", *sorted((self.root / "sdlc" / "adr").glob("*.md"))]
        for p in paths:
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()

    # ---------- decisions ----------
    def decide(self, id_: str, action: str, text: str = "", change: str | None = None, by: str = "") -> dict:
        """Approve / request changes / resume, through the same gate the loop and the team lead wait on."""
        a = self.find(id_, change)
        if not a:
            return {"error": "unknown document"}
        kind = {"architecture": "architecture", "adr": "architecture", "design": f"design:{a.get('key')}",
                "escalation": f"escalated:{a.get('key')}"}.get(a["kind"], a["kind"])
        if kind not in gate.DOC_KINDS and kind != "architecture" and ":" not in kind:
            return {"error": "nothing to decide on this document"}
        if kind in gate.DOC_KINDS and not a["needs"]:
            return {"error": f"the {kind} is not waiting for approval"}
        ch = self.change_dir(a["change"] or None)
        if kind == "architecture":  # the architecture names its change; fall back to the one on screen
            doc = self.root / "sdlc" / "ARCHITECTURE.md"
            m = re.search(r"^\W*Change:\W*([^\s`*]+)", "\n".join(doc.read_text(encoding="utf-8", errors="replace").splitlines()[:15]), re.M | re.I)
            named = m.group(1).rstrip("/").split("/")[-1] if m else ""
            ch = self.change_dir(named) if named and self.change_dir(named).name == named else ch
        if not ch:
            return {"error": "cannot tell which change this belongs to"}
        r = gate.decide(self.root, self.cfg, ch, kind, action, text, by)
        if r.get("error"):
            return r
        d = self.chat(id_)
        label = {"approve": "✓ Approved", "changes": "Changes requested", "resume": "Resumed with guidance",
                 "stop": "Stopped"}[r["decision"]]
        d["messages"].append({"who": "system", "text": label + (f": {text.strip()}" if text.strip() else ""),
                              "ts": time.time(), "decision": r["decision"]})
        self._save_chat(id_, d)
        self._cache = (0.0, None)
        return {"ok": True}
