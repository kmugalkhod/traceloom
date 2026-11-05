"""Jira / Linear sync for the team board (hybrid mode).

The internal board (board.py) is always the source of truth and always on. When .sdlc/config.json
sets tracker.type to "jira" or "linear", the orchestrator mirrors the board into it through the
tracker's API — cheap and deterministic — while agents get the tracker's MCP server to read tickets
and comment. Nothing here ever deletes anything in the tracker.

What gets mirrored:
  epic  -> Jira Epic            | Linear Project (or a parent issue: linear.epic_as = "issue")
  story -> Jira Story (parent=epic) | Linear issue in the project
  task  -> Jira Sub-task (parent=story) | Linear sub-issue of the story
  assignee (an agent) -> label "agent-dev-1" (+ optional real assignee account for everything)
  status -> workflow transition by name (configurable mapping)
  QA results, hand-offs and merges -> comments

Credentials come from the environment only:
  Jira:   JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
  Linear: LINEAR_API_KEY
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

DEFAULT_JIRA = {
    "base_url": "", "project_key": "",
    "issue_types": {"epic": "Epic", "story": "Story", "task": "Subtask"},
    "transitions": {"todo": "To Do", "in_progress": "In Progress", "blocked": "In Progress",
                    "in_qa": "In Review", "done": "Done"},
    "assignee_account_id": "", "label_prefix": "agent-",
}
DEFAULT_LINEAR = {
    "team_key": "", "epic_as": "project",
    "states": {"todo": "Todo", "in_progress": "In Progress", "blocked": "In Progress",
               "in_qa": "In Review", "done": "Done"},
    "assignee_id": "", "label_prefix": "agent-",
}


class TrackerError(Exception):
    pass


def _http(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise TrackerError(f"{method} {url} -> {e.code}: {e.read()[:400]!r}") from None
    except urllib.error.URLError as e:
        raise TrackerError(f"{method} {url} -> {e.reason}") from None


def _merge(defaults: dict, over: dict | None) -> dict:
    out = json.loads(json.dumps(defaults))
    for k, v in (over or {}).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# ====================================================================== Jira (REST v3)
class Jira:
    name = "jira"

    def __init__(self, cfg: dict):
        self.c = _merge(DEFAULT_JIRA, cfg)
        self.base = (os.environ.get("JIRA_BASE_URL") or self.c["base_url"]).rstrip("/")
        email, token = os.environ.get("JIRA_EMAIL", ""), os.environ.get("JIRA_API_TOKEN", "")
        if not (self.base and self.c["project_key"] and email and token):
            raise TrackerError("Jira needs JIRA_BASE_URL (or tracker.jira.base_url), tracker.jira.project_key, "
                               "JIRA_EMAIL and JIRA_API_TOKEN")
        self.h = {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}

    @staticmethod
    def adf(text: str) -> dict:
        paras = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.split("\n") if line]
        return {"type": "doc", "version": 1, "content": paras or [{"type": "paragraph", "content": []}]}

    def create(self, item: dict, parent_ext: str | None) -> tuple[str, str]:
        desc = item.get("description") or ""
        if item.get("acceptance"):
            desc += "\n\nAcceptance criteria:\n" + "\n".join(f"- {a}" for a in item["acceptance"])
        fields = {"project": {"key": self.c["project_key"]}, "summary": f"[{item['key']}] {item['title']}"[:250],
                  "issuetype": {"name": self.c["issue_types"][item["type"]]}, "description": self.adf(desc),
                  "labels": ["ai-sdlc"]}
        if parent_ext:
            fields["parent"] = {"key": parent_ext}
        r = _http("POST", f"{self.base}/rest/api/3/issue", self.h, {"fields": fields})
        return r["key"], f"{self.base}/browse/{r['key']}"

    def assign(self, ext: str, agent: str) -> None:
        _http("PUT", f"{self.base}/rest/api/3/issue/{ext}", self.h,
              {"update": {"labels": [{"add": self.c["label_prefix"] + agent}]}})
        if self.c.get("assignee_account_id"):
            _http("PUT", f"{self.base}/rest/api/3/issue/{ext}/assignee", self.h,
                  {"accountId": self.c["assignee_account_id"]})

    def transition(self, ext: str, status: str) -> None:
        want = (self.c["transitions"].get(status) or "").lower()
        if not want:
            return
        ts = _http("GET", f"{self.base}/rest/api/3/issue/{ext}/transitions", self.h).get("transitions", [])
        hit = next((t for t in ts if t.get("name", "").lower() == want
                    or (t.get("to") or {}).get("name", "").lower() == want), None)
        if not hit:
            raise TrackerError(f"{ext}: no transition to '{want}' (available: {[t.get('name') for t in ts]})")
        _http("POST", f"{self.base}/rest/api/3/issue/{ext}/transitions", self.h, {"transition": {"id": hit["id"]}})

    def comment(self, ext: str, text: str) -> None:
        _http("POST", f"{self.base}/rest/api/3/issue/{ext}/comment", self.h, {"body": self.adf(text)})

    def mcp_config(self) -> dict:
        # Atlassian Rovo MCP. Token auth must be enabled by an org admin (Rovo MCP server -> Authentication).
        return {"tracker": {"type": "http", "url": "https://mcp.atlassian.com/v1/mcp",
                            "headers": {"Authorization": "Basic ${SDLC_JIRA_BASIC}"}}}

    def mcp_env(self) -> dict:
        return {"SDLC_JIRA_BASIC": self.h["Authorization"].split(" ", 1)[1]}


# ====================================================================== Linear (GraphQL)
class Linear:
    name = "linear"
    URL = "https://api.linear.app/graphql"

    def __init__(self, cfg: dict):
        self.c = _merge(DEFAULT_LINEAR, cfg)
        key = os.environ.get("LINEAR_API_KEY", "")
        if not (key and self.c["team_key"]):
            raise TrackerError("Linear needs LINEAR_API_KEY and tracker.linear.team_key")
        self.url = os.environ.get("LINEAR_API_URL", self.URL)
        self.h = {"Authorization": key}
        self._team = None
        self._labels: dict[str, str] = {}

    def q(self, query: str, variables: dict | None = None) -> dict:
        r = _http("POST", self.url, self.h, {"query": query, "variables": variables or {}})
        if r.get("errors"):
            raise TrackerError(f"Linear: {r['errors'][0].get('message')}")
        return r.get("data") or {}

    def team(self) -> dict:
        if not self._team:
            d = self.q("query($k:String!){teams(filter:{key:{eq:$k}}){nodes{id key states{nodes{id name}}}}}",
                       {"k": self.c["team_key"]})
            nodes = d.get("teams", {}).get("nodes", [])
            if not nodes:
                raise TrackerError(f"Linear team '{self.c['team_key']}' not found")
            self._team = nodes[0]
        return self._team

    def create(self, item: dict, parent_ext: str | None) -> tuple[str, str]:
        desc = item.get("description") or ""
        if item.get("acceptance"):
            desc += "\n\n**Acceptance criteria**\n" + "\n".join(f"- [ ] {a}" for a in item["acceptance"])
        title = f"[{item['key']}] {item['title']}"
        t = self.team()
        if item["type"] == "epic" and self.c["epic_as"] == "project":
            d = self.q("mutation($i:ProjectCreateInput!){projectCreate(input:$i){success project{id url}}}",
                       {"i": {"name": title[:80], "description": desc[:255], "teamIds": [t["id"]]}})
            p = d["projectCreate"]["project"]
            return f"project:{p['id']}", p["url"]
        inp = {"teamId": t["id"], "title": title, "description": desc}
        if parent_ext and parent_ext.startswith("project:"):
            inp["projectId"] = parent_ext.split(":", 1)[1]
        elif parent_ext:
            inp["parentId"] = parent_ext
            proj = getattr(self, "_proj_of", {}).get(parent_ext)
            if proj:
                inp["projectId"] = proj
        d = self.q("mutation($i:IssueCreateInput!){issueCreate(input:$i){success issue{id identifier url}}}", {"i": inp})
        iss = d["issueCreate"]["issue"]
        if inp.get("projectId"):
            self._proj_of = {**getattr(self, "_proj_of", {}), iss["id"]: inp["projectId"]}
        return iss["id"], iss["url"]

    def _label(self, name: str) -> str:
        if name not in self._labels:
            d = self.q("query($n:String!){issueLabels(filter:{name:{eq:$n}}){nodes{id}}}", {"n": name})
            nodes = d.get("issueLabels", {}).get("nodes", [])
            if nodes:
                self._labels[name] = nodes[0]["id"]
            else:
                d = self.q("mutation($i:IssueLabelCreateInput!){issueLabelCreate(input:$i){issueLabel{id}}}",
                           {"i": {"name": name, "teamId": self.team()["id"]}})
                self._labels[name] = d["issueLabelCreate"]["issueLabel"]["id"]
        return self._labels[name]

    def assign(self, ext: str, agent: str) -> None:
        if ext.startswith("project:"):
            return
        self.q("mutation($id:String!,$l:String!){issueAddLabel(id:$id,labelId:$l){success}}",
               {"id": ext, "l": self._label(self.c["label_prefix"] + agent)})
        if self.c.get("assignee_id"):
            self.q("mutation($id:String!,$i:IssueUpdateInput!){issueUpdate(id:$id,input:$i){success}}",
                   {"id": ext, "i": {"assigneeId": self.c["assignee_id"]}})

    def transition(self, ext: str, status: str) -> None:
        if ext.startswith("project:"):
            return
        want = (self.c["states"].get(status) or "").lower()
        st = next((s for s in self.team()["states"]["nodes"] if s["name"].lower() == want), None)
        if not st:
            raise TrackerError(f"Linear: no workflow state '{want}'")
        self.q("mutation($id:String!,$i:IssueUpdateInput!){issueUpdate(id:$id,input:$i){success}}",
               {"id": ext, "i": {"stateId": st["id"]}})

    def comment(self, ext: str, text: str) -> None:
        if ext.startswith("project:"):
            return
        self.q("mutation($i:CommentCreateInput!){commentCreate(input:$i){success}}",
               {"i": {"issueId": ext, "body": text}})

    def mcp_config(self) -> dict:
        return {"tracker": {"type": "http", "url": "https://mcp.linear.app/mcp",
                            "headers": {"Authorization": "Bearer ${LINEAR_API_KEY}"}}}

    def mcp_env(self) -> dict:
        return {}


def make(cfg: dict):
    """Return a tracker client, or None for the internal board only."""
    t = (cfg.get("tracker") or {}).get("type", "internal")
    if t == "jira":
        return Jira(cfg["tracker"].get("jira", {}))
    if t == "linear":
        return Linear(cfg["tracker"].get("linear", {}))
    return None


# ====================================================================== sync engine
class Sync:
    """Idempotent mirror of the board into the tracker. Safe to call every few seconds."""

    COMMENT_KINDS = {"qa", "merge", "help"}

    def __init__(self, board, client):
        self.b, self.t = board, client

    def run(self) -> list[str]:
        if not self.t:
            return []
        errors = []
        state = self.b.meta().get("sync_state", {"items": {}, "last_event": 0})
        order = {"epic": 0, "story": 1, "task": 2}
        for it in sorted(self.b.items(), key=lambda i: order[i["type"]]):
            s = state["items"].setdefault(it["key"], {})
            try:
                if not it.get("ext_key"):
                    parent = self.b.item(it["parent_key"]) if it.get("parent_key") else None
                    if parent and not parent.get("ext_key"):
                        continue  # parent first, next round
                    ext, url = self.t.create(it, parent.get("ext_key") if parent else None)
                    self.b.update(it["key"], "sync", ext_key=ext, ext_url=url)
                    self.b.event(it["key"], "sync", "sync", f"created in {self.t.name}: {url}")
                    it["ext_key"] = ext
                if it.get("assignee") and s.get("assignee") != it["assignee"]:
                    self.t.assign(it["ext_key"], it["assignee"])
                    s["assignee"] = it["assignee"]
                if it["type"] == "task" and s.get("status") != it["status"]:
                    self.t.transition(it["ext_key"], it["status"])
                    s["status"] = it["status"]
                elif it["type"] != "task" and s.get("status") != it["status"]:
                    try:
                        self.t.transition(it["ext_key"], it["status"])
                    except TrackerError:
                        pass  # epics/stories often have their own workflow; not fatal
                    s["status"] = it["status"]
            except TrackerError as e:
                errors.append(str(e))
        last = state.get("last_event", 0)
        for ev in self.b.events(since=last, limit=500):
            last = ev["id"]
            agentish = ev["kind"] == "status" and ev["actor"] not in ("lead", "sync") and ":" in ev["text"]
            if ev["item_key"] and (ev["kind"] in self.COMMENT_KINDS or agentish):
                it = self.b.item(ev["item_key"])
                if it and it.get("ext_key"):
                    try:
                        self.t.comment(it["ext_key"], f"[{ev['actor']}] {ev['text']}")
                    except TrackerError as e:
                        errors.append(str(e))
        state["last_event"] = last
        self.b.set_meta(sync_state=state)
        seen = set(state.get("errors", []))
        for e in errors[:5]:
            if e not in seen:  # log each distinct error once, not every tick
                self.b.event(None, "sync", "sync", f"{self.t.name} sync error: {e}")
        state["errors"] = list(seen | set(errors))[-50:]
        self.b.set_meta(sync_state=state)
        return errors
