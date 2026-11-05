#!/usr/bin/env python3
"""The team board: one shared store for epics, stories, tasks, the agent team and their conversation.

Every developer and QA agent works in its own git worktree, but they all read and write ONE board
(SQLite, WAL mode) in the main repository: .sdlc/state/board.db (override: SDLC_BOARD_DB).
The orchestrator (team.py), the live web page (board_server.py) and the Jira/Linear sync all use it.

Agents use this CLI (their name and role come from SDLC_AGENT / SDLC_ROLE, set by the orchestrator):

  board.py show [--json]                     the whole board, grouped by status
  board.py task <KEY>                        one item with its history and messages
  board.py comment <KEY> "text"              note progress on a task
  board.py submit <KEY> "what I did"         developer: hand the task to QA
  board.py qa-pass <KEY> "evidence"          QA: accept the task (it gets merged)
  board.py qa-fail <KEY> "bug report"        QA: send it back to its developer
  board.py blocked <KEY> "what I need"       ask the team for help; an idle teammate is sent to help
  board.py ask <agent|developer|qa|lead> "question" [--task KEY]
  board.py reply <MSG_ID> "answer"
  board.py inbox                             unread messages for me (marks them read)
  board.py team                              who is doing what
  board.py design-ready <KEY> "summary"      designer: the mockup is ready for the human's review
  board.py design-approve <KEY> ["note"]     HUMAN only: approve the design (UI tasks unlock)
  board.py design-changes <KEY> "feedback"   HUMAN only: send the design back to the designer
Humans can run every command too (actor "human").
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys

# Windows consoles default to cp1252, which cannot print the arrows and dashes these tools use
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timezone
from pathlib import Path

TASK_STATUSES = ["todo", "in_progress", "blocked", "review", "in_qa", "done"]
STATUS_LABEL = {"todo": "To Do", "in_progress": "In Progress", "blocked": "Blocked", "review": "Needs approval",
                "in_qa": "In QA", "done": "Done"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS items (
  key TEXT PRIMARY KEY, type TEXT NOT NULL, parent_key TEXT, title TEXT NOT NULL,
  description TEXT DEFAULT '', acceptance TEXT DEFAULT '[]', files TEXT DEFAULT '[]',
  depends_on TEXT DEFAULT '[]', status TEXT NOT NULL DEFAULT 'todo', assignee TEXT, qa_by TEXT,
  qa_rounds INTEGER DEFAULT 0, branch TEXT, ext_key TEXT, ext_url TEXT, sort INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, item_key TEXT, actor TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, sender TEXT, recipient TEXT, item_key TEXT,
  text TEXT, reply_to INTEGER, read INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY, role TEXT, status TEXT, item_key TEXT, activity TEXT, worktree TEXT,
  runs INTEGER DEFAULT 0, cost REAL DEFAULT 0, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_key);
CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(recipient, read);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main_repo_root(start: Path | None = None) -> Path:
    """The main checkout, even when called from inside a linked worktree."""
    cwd = str(start or Path.cwd())
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout.strip()
        if common:
            return Path(common).parent
    except Exception:
        pass
    return Path(cwd)


def db_path() -> Path:
    if os.environ.get("SDLC_BOARD_DB"):
        return Path(os.environ["SDLC_BOARD_DB"])
    return main_repo_root() / ".sdlc" / "state" / "board.db"


class Board:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    # ------------------------------------------------------------ meta
    def set_meta(self, **kv) -> None:
        with self.conn() as c:
            for k, v in kv.items():
                c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, json.dumps(v)))

    def meta(self) -> dict:
        with self.conn() as c:
            return {r["k"]: json.loads(r["v"]) for r in c.execute("SELECT k,v FROM meta")}

    # ------------------------------------------------------------ items
    def next_key(self, prefix: str) -> str:
        with self.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM items WHERE key LIKE ?", (f"{prefix}-%",)).fetchone()[0]
        return f"{prefix}-{n + 1}"

    def add_item(self, type_: str, title: str, parent_key: str | None = None, **f) -> str:
        prefix = {"epic": "E", "story": "S", "task": "T", "design": "D"}[type_]
        key = f.pop("key", None) or self.next_key(prefix)
        ts = now()
        with self.conn() as c:
            sort = c.execute("SELECT COALESCE(MAX(sort),0)+1 FROM items").fetchone()[0]
            c.execute("""INSERT INTO items(key,type,parent_key,title,description,acceptance,files,depends_on,
                         status,sort,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (key, type_, parent_key, title, f.get("description", ""),
                       json.dumps(f.get("acceptance", [])), json.dumps(f.get("files", [])),
                       json.dumps(f.get("depends_on", [])), f.get("status", "todo"), sort, ts, ts))
        self.event(key, "lead", "created", f"{type_} created: {title}")
        return key

    def item(self, key: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def items(self, type_: str | None = None, parent_key: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM items WHERE 1=1", []
        if type_:
            q += " AND type=?"; a.append(type_)
        if parent_key:
            q += " AND parent_key=?"; a.append(parent_key)
        with self.conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY sort", a)]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("acceptance", "files", "depends_on"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except json.JSONDecodeError:
                d[k] = []
        return d

    def update(self, key: str, actor: str = "lead", note: str | None = None, **fields) -> None:
        if not fields:
            return
        old = self.item(key) or {}
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE items SET {sets} WHERE key=?", (*fields.values(), key))
        if "status" in fields and fields["status"] != old.get("status"):
            self.event(key, actor, "status", f"{STATUS_LABEL.get(old.get('status'), old.get('status'))} → "
                                             f"{STATUS_LABEL.get(fields['status'], fields['status'])}"
                                             + (f": {note}" if note else ""))
        elif note:
            self.event(key, actor, "comment", note)
        self.rollup(key)

    def rollup(self, key: str) -> None:
        """Stories and epics take their status from their children."""
        it = self.item(key)
        parent = it and it.get("parent_key")
        while parent:
            kids = self.items(parent_key=parent)
            sts = {k["status"] for k in kids}
            new = "done" if sts and sts == {"done"} else ("todo" if sts <= {"todo"} else "in_progress")
            p = self.item(parent)
            if p and p["status"] != new:
                with self.conn() as c:
                    c.execute("UPDATE items SET status=?, updated_at=? WHERE key=?", (new, now(), parent))
                self.event(parent, "lead", "status", f"{p['type']} is now {STATUS_LABEL.get(new, new)}")
            parent = p and p.get("parent_key")

    # ------------------------------------------------------------ events and messages
    def event(self, item_key: str | None, actor: str, kind: str, text: str) -> int:
        with self.conn() as c:
            cur = c.execute("INSERT INTO events(ts,item_key,actor,kind,text) VALUES(?,?,?,?,?)",
                            (now(), item_key, actor, kind, text[:4000]))
            return cur.lastrowid

    def events(self, since: int = 0, item_key: str | None = None, limit: int = 300) -> list[dict]:
        q, a = "SELECT * FROM events WHERE id>?", [since]
        if item_key:
            q += " AND item_key=?"; a.append(item_key)
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit))]
        return list(reversed(rows))

    def send(self, sender: str, recipient: str, text: str, item_key: str | None = None,
             reply_to: int | None = None) -> int:
        with self.conn() as c:
            mid = c.execute("INSERT INTO messages(ts,sender,recipient,item_key,text,reply_to) VALUES(?,?,?,?,?,?)",
                            (now(), sender, recipient, item_key, text[:4000], reply_to)).lastrowid
        self.event(item_key, sender, "message", f"@{recipient}: {text}")
        return mid

    def inbox(self, agent: str, role: str | None, mark_read: bool = True) -> list[dict]:
        who = [agent] + ([role] if role else []) + ["all"]
        q = f"SELECT * FROM messages WHERE read=0 AND recipient IN ({','.join('?' * len(who))}) AND sender<>? ORDER BY id"
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(q, (*who, agent))]
            if mark_read and rows:
                c.execute(f"UPDATE messages SET read=1 WHERE id IN ({','.join(str(r['id']) for r in rows)})")
        return rows

    def messages(self, item_key: str | None = None, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM messages", []
        if item_key:
            q += " WHERE item_key=?"; a.append(item_key)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit))][::-1]

    # ------------------------------------------------------------ agents
    def upsert_agent(self, name: str, **f) -> None:
        f["updated_at"] = now()
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO agents(name) VALUES(?)", (name,))
            c.execute(f"UPDATE agents SET {', '.join(f'{k}=?' for k in f)} WHERE name=?", (*f.values(), name))

    def agents(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM agents ORDER BY role, name")]

    def add_cost(self, name: str, cost: float) -> None:
        with self.conn() as c:
            c.execute("UPDATE agents SET cost=cost+?, runs=runs+1 WHERE name=?", (cost or 0, name))

    # ------------------------------------------------------------ design approval (human)
    def approve_design(self, key: str, note: str = "", actor: str = "human") -> None:
        self.event(key, actor, "design", f"APPROVED{': ' + note if note else ''}")
        self.update(key, actor, qa_by=f"approved:{actor}")  # the lead merges it, then marks it done

    def request_design_changes(self, key: str, text: str, actor: str = "human") -> None:
        it = self.item(key) or {}
        self.event(key, actor, "design", f"CHANGES: {text}")
        self.update(key, actor, note="changes requested", status="in_progress",
                    qa_rounds=(it.get("qa_rounds") or 0) + 1, qa_by=f"changes:{actor}")
        self.send(actor, "designer", f"Changes requested on {key}: {text}", key)

    def clear(self) -> None:
        """Empty every table in place (works while the board server has the file open, incl. on Windows)."""
        with self.conn() as c:
            for t in ("events", "messages", "agents", "items", "meta"):
                c.execute(f"DELETE FROM {t}")

    def resume(self, key: str, text: str, actor: str = "human") -> None:
        """Human sends an escalated task back to the team with guidance; counters start over."""
        it = self.item(key) or {}
        esc_keys = [k for k in self.meta().get("escalated", []) if k != key]
        self.set_meta(escalated=esc_keys)
        self.event(key, actor, "comment", f"RESUMED by {actor}: {text}")
        back = "in_progress" if it.get("branch") else "todo"
        self.update(key, actor, note=f"resumed by {actor}", status=back, qa_rounds=0, qa_by=None)
        if it.get("assignee"):
            self.send(actor, it["assignee"], f"{key} is back with you. Guidance from {actor}: {text}", key)

    # ------------------------------------------------------------ snapshot for the web page
    def snapshot(self, since: int = 0) -> dict:
        return {"meta": self.meta(), "items": self.items(), "agents": self.agents(),
                "events": self.events(since=since, limit=400), "messages": self.messages(limit=300)}


# ====================================================================== CLI
def me() -> tuple[str, str | None]:
    return os.environ.get("SDLC_AGENT", "human"), os.environ.get("SDLC_ROLE")


def need(b: Board, key: str) -> dict:
    it = b.item(key)
    if not it:
        sys.exit(f"no such item: {key}")
    return it


def guard(cond: bool, msg: str) -> None:
    if not cond:
        sys.exit(f"refused: {msg}")


def take_identity(argv: list[str]) -> list[str]:
    """Agent-team teammates share one session environment, so they say who they are: --as dev-1 --role developer.
    A named agent is never the human, so human-only actions (design approval, resume) stay refused."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--as", "--role") and i + 1 < len(argv):
            os.environ["SDLC_AGENT" if argv[i] == "--as" else "SDLC_ROLE"] = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    if os.environ.get("SDLC_AGENT") == "human":  # nobody can claim to be the human through --as
        os.environ.pop("SDLC_AGENT")
        sys.exit("--as human is not allowed; humans run board.py without --as")
    return out


def main() -> None:
    sys.argv[1:] = take_identity(sys.argv[1:])
    ap = argparse.ArgumentParser(prog="board", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("task"); s.add_argument("key")
    for name in ("comment", "submit", "qa-pass", "qa-fail", "blocked"):
        s = sub.add_parser(name); s.add_argument("key"); s.add_argument("text")
    s = sub.add_parser("design-ready"); s.add_argument("key"); s.add_argument("text")
    s = sub.add_parser("design-approve"); s.add_argument("key"); s.add_argument("text", nargs="?", default="")
    s = sub.add_parser("design-changes"); s.add_argument("key"); s.add_argument("text")
    s = sub.add_parser("resume"); s.add_argument("key"); s.add_argument("text")
    s = sub.add_parser("ask"); s.add_argument("to"); s.add_argument("text"); s.add_argument("--task")
    s = sub.add_parser("reply"); s.add_argument("msg_id", type=int); s.add_argument("text")
    sub.add_parser("inbox")
    sub.add_parser("team")
    a = ap.parse_args()
    b = Board()
    agent, role = me()
    human = agent == "human"

    if a.cmd == "show":
        items = b.items()
        if a.json:
            print(json.dumps(items, indent=2)); return
        for st in TASK_STATUSES:
            ts = [i for i in items if i["type"] == "task" and i["status"] == st]
            if ts:
                print(f"\n{STATUS_LABEL[st]} ({len(ts)})")
                for t in ts:
                    print(f"  {t['key']:5} {t['title'][:70]:70} {t['assignee'] or '-':8} story {t['parent_key']}")
        return

    if a.cmd == "task":
        it = need(b, a.key)
        print(json.dumps({k: v for k, v in it.items() if v not in (None, "", [])}, indent=2))
        kids = b.items(parent_key=a.key)
        if kids:
            print("\nchildren:")
            for k in kids:
                print(f"  {k['key']} [{k['status']}] {k['title']}")
        print("\nhistory:")
        for e in b.events(item_key=a.key, limit=60):
            print(f"  {e['ts'][11:19]} {e['actor']:8} {e['kind']:8} {e['text'][:300]}")
        return

    if a.cmd == "comment":
        need(b, a.key); b.event(a.key, agent, "comment", a.text); print("ok"); return

    if a.cmd == "submit":
        it = need(b, a.key)
        guard(human or (role == "developer" and it["assignee"] == agent), f"{a.key} is assigned to {it['assignee']}")
        guard(it["status"] in ("in_progress", "blocked"), f"{a.key} is {it['status']}, not in progress")
        b.update(a.key, agent, note=a.text, status="in_qa", qa_by=None)
        print(f"{a.key} handed to QA"); return

    if a.cmd in ("qa-pass", "qa-fail"):
        it = need(b, a.key)
        guard(human or role == "qa", "only a QA agent can pass or fail a task")
        guard(it["status"] == "in_qa", f"{a.key} is {it['status']}, not in QA")
        if a.cmd == "qa-pass":
            b.event(a.key, agent, "qa", f"PASS: {a.text}")
            b.update(a.key, agent, qa_by=f"pass:{agent}")  # the orchestrator merges, then marks it done
            print(f"{a.key} passed QA — the lead merges it now")
        else:
            b.event(a.key, agent, "qa", f"FAIL: {a.text}")
            b.update(a.key, agent, note="QA found a problem", status="in_progress",
                     qa_rounds=(it["qa_rounds"] or 0) + 1, qa_by=f"fail:{agent}")
            if it["assignee"]:
                b.send(agent, it["assignee"], f"QA failed {a.key}: {a.text}", a.key)
            print(f"{a.key} sent back to {it['assignee']}")
        return

    if a.cmd == "blocked":
        it = need(b, a.key)
        b.update(a.key, agent, note=a.text, status="blocked")
        b.send(agent, "developer", f"I'm blocked on {a.key} ({it['title']}): {a.text}", a.key)
        print(f"{a.key} marked blocked; the lead will send a teammate to help"); return

    if a.cmd == "design-ready":
        it = need(b, a.key)
        guard(human or role == "designer", "only the designer can submit a design")
        guard(it["type"] == "design" and it["status"] in ("in_progress", "todo"), f"{a.key} is not a design in progress")
        b.update(a.key, agent, note=a.text, status="review", qa_by=None)
        b.send(agent, "human", f"Design {a.key} is ready for your review: {a.text}", a.key)
        print(f"{a.key} sent to the human for approval"); return

    if a.cmd in ("design-approve", "design-changes"):
        it = need(b, a.key)
        guard(human, "only a human approves or rejects a design")
        guard(it["type"] == "design" and it["status"] == "review", f"{a.key} is not waiting for approval")
        if a.cmd == "design-approve":
            b.approve_design(a.key, a.text)
            print(f"{a.key} approved — the lead merges it and unlocks the UI tasks")
        else:
            b.request_design_changes(a.key, a.text)
            print(f"{a.key} sent back to the designer")
        return

    if a.cmd == "resume":
        it = need(b, a.key)
        guard(human, "only a human resumes an escalated task")
        guard(a.key in b.meta().get("escalated", []), f"{a.key} is not escalated")
        b.resume(a.key, a.text)
        print(f"{a.key} resumed — back with {it['assignee'] or 'the team'}; re-run team.py run if the team has stopped")
        return

    if a.cmd == "ask":
        mid = b.send(agent, a.to, a.text, a.task)
        print(f"message {mid} sent to {a.to}"); return

    if a.cmd == "reply":
        with b.conn() as c:
            m = c.execute("SELECT * FROM messages WHERE id=?", (a.msg_id,)).fetchone()
        if not m:
            sys.exit(f"no message {a.msg_id}")
        b.send(agent, m["sender"], a.text, m["item_key"], reply_to=a.msg_id)
        print("ok"); return

    if a.cmd == "inbox":
        msgs = b.inbox(agent, role)
        if not msgs:
            print("no new messages"); return
        for m in msgs:
            print(f"[{m['id']}] {m['ts'][11:19]} {m['sender']} → {m['recipient']}"
                  f"{' (' + m['item_key'] + ')' if m['item_key'] else ''}: {m['text']}")
        return

    if a.cmd == "team":
        for ag in b.agents():
            print(f"{ag['name']:8} {ag['role']:9} {ag['status']:8} {ag['item_key'] or '-':5} {ag['activity'] or ''}")


if __name__ == "__main__":
    main()
