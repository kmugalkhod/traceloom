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
from sdlc_core import git, header_field  # noqa: E402

AGENT_MODEL = {"architect": "opus"}  # others: team.models.<agent> or sonnet
LOCK = threading.Lock()
RUNNING: dict[str, threading.Thread] = {}


def _slug(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


class Review:
    def __init__(self, root: Path, cfg: dict, board):
        self.root, self.cfg, self.board = Path(root), cfg or {}, board
        self.state = self.root / ".sdlc" / "state" / "review"
        self._cache = (0.0, None)

    # ---------- which change, which artifacts ----------
    def change_dir(self) -> Path | None:
        name = (self.board.meta() or {}).get("change")
        base = self.root / self.cfg.get("changes_dir", "sdlc/changes")
        if name and (base / name).is_dir():
            return base / name
        dirs = sorted((d for d in base.glob("*") if d.is_dir()), key=lambda d: d.stat().st_mtime) if base.is_dir() else []
        return dirs[-1] if dirs else None

    def worktree(self, name: str) -> Path:
        return self.root / ((self.cfg.get("team") or {}).get("worktree_dir") or ".sdlc/worktrees") / name

    def artifacts(self, fresh: bool = False) -> list[dict]:
        t, cached = self._cache
        if cached is not None and not fresh and time.time() - t < 3:
            return cached
        out, ch = [], self.change_dir()

        def add(id_, name, path, agent, status, kind="doc", needs=False, extra=None):
            out.append({"id": id_, "name": name, "path": path, "agent": agent, "status": status or "", "kind": kind,
                        "needs": needs, **(extra or {})})

        def file_status(p: Path) -> str:
            return (header_field(p, "Status") or "").strip().lower() if p.is_file() else ""

        if ch:
            rel = ch.relative_to(self.root).as_posix()
            for fname, name, agent in (("intent.md", "Intent", "lead"), ("spec.md", "Spec", "lead"),
                                       ("plan.md", "Plan notes", "planner")):
                p = ch / fname
                if p.is_file():
                    add(f"file:{rel}/{fname}", name, f"{rel}/{fname}", agent, file_status(p))
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
        for it in self.board.items("design"):
            waiting = it["status"] == "review" and not str(it.get("qa_by") or "").startswith("approved")
            st = "waiting" if waiting else "approved" if it["status"] in ("review", "done") else it["status"]
            add(f"design:{it['key']}", f"Design {it['key']}", f"design/{it['key']}", "designer", st, kind="design",
                needs=waiting, extra={"key": it["key"], "title": it.get("title", "")})
        for key in (self.board.meta() or {}).get("escalated", []) or []:
            it = self.board.item(key) or {}
            add(f"escalated:{key}", f"Escalated {key}", f"task/{key}", "lead", "waiting", kind="escalation",
                needs=True, extra={"key": key, "title": it.get("title", "")})
        if ch and (ch / "breakdown.json").is_file():
            add("plan", "Plan", f"{ch.relative_to(self.root).as_posix()}/breakdown.json", "planner", "planned", kind="plan")
        self._cache = (time.time(), out)
        return out

    def count(self) -> int:
        try:
            return sum(1 for a in self.artifacts() if a["needs"])
        except Exception:
            return 0

    def find(self, id_: str) -> dict | None:
        return next((a for a in self.artifacts(fresh=True) if a["id"] == id_), None)

    # ---------- documents and versions ----------
    def _design_notes(self, key: str) -> tuple[str, Path | None]:
        ch = self.change_dir()
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

    def doc(self, id_: str, ver: str = "") -> dict | None:
        a = self.find(id_)
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
            res["content"], _ = self._design_notes(a["key"])
            res["content"] = res["content"] or f"# Design {a['key']}\n\n{a.get('title', '')}\n\nThe designer is still working."
            res["mockup"] = f"design/{a['key']}/"
        elif a["kind"] == "escalation":
            ch = self.change_dir()
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

    def post(self, id_: str, text: str, quote: str = "", section: str = "") -> dict:
        a = self.find(id_)
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
        ch = self.change_dir()
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
        team = self.cfg.get("team") or {}
        model = (team.get("models") or {}).get(a["agent"]) or AGENT_MODEL.get(a["agent"], "sonnet")
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
                               text=True, encoding="utf-8", errors="replace", timeout=int(team.get("review_timeout_s", 900)),
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
    def decide(self, id_: str, action: str, text: str = "") -> dict:
        a = self.find(id_)
        if not a:
            return {"error": "unknown document"}
        bin_ = self.root / ".sdlc" / "bin" / "team.py"
        if a["kind"] in ("architecture", "adr"):
            doc = self.root / "sdlc" / "ARCHITECTURE.md"
            if (header_field(doc, "Status") or "").lower() != "proposed":
                return {"error": "the architecture is not waiting for approval"}
            m = re.search(r"^\W*Change:\W*([^\s`*]+)", "\n".join(doc.read_text(encoding="utf-8", errors="replace").splitlines()[:15]), re.M | re.I)
            change = (m.group(1).rstrip("/").split("/")[-1] if m else "") or (self.board.meta() or {}).get("change", "")
            if not change:
                return {"error": "cannot tell which change this architecture belongs to"}
            if action == "approve":
                args = ["architecture-approve", change, text[:1000]]
            elif action == "changes" and text.strip():
                args = ["architecture-changes", change, text.strip()[:2000]]
            else:
                return {"error": "action must be approve, or changes with text"}
            r = subprocess.run([sys.executable, str(bin_), *args], cwd=self.root, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return {"error": (r.stderr or r.stdout).strip()[-400:]}
        elif a["kind"] == "design":
            if not a["needs"]:
                return {"error": "that design is not waiting for approval"}
            if action == "approve":
                self.board.approve_design(a["key"], text[:1000])
            elif action == "changes" and text.strip():
                self.board.request_design_changes(a["key"], text.strip()[:2000])
            else:
                return {"error": "action must be approve, or changes with text"}
        elif a["kind"] == "escalation":
            if action != "resume" or not text.strip():
                return {"error": "give the team some guidance to resume"}
            self.board.resume(a["key"], text.strip()[:2000])
        else:
            return {"error": "nothing to decide on this document"}
        d = self.chat(id_)
        label = {"approve": "✓ Approved", "changes": "Changes requested", "resume": "Resumed with guidance"}[action]
        d["messages"].append({"who": "system", "text": label + (f": {text.strip()}" if text.strip() else ""),
                              "ts": time.time(), "decision": action})
        self._save_chat(id_, d)
        self._cache = (0.0, None)
        return {"ok": True}
