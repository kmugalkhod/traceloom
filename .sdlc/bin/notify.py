#!/usr/bin/env python3
"""Tell the human when something needs them — on the phone, with buttons that act.

  python .sdlc/bin/notify.py test                 # send a test message to every configured channel
  python .sdlc/bin/notify.py serve                # answer button presses (Telegram), keep it running
  python .sdlc/bin/notify.py serve --once         # one poll (cron / tests)

Channels (.sdlc/config.json → notify; secrets only in environment variables, never in the config):
  Slack     notify.slack_webhook_env  (default SDLC_SLACK_WEBHOOK): an incoming-webhook URL — alerts with links
  Telegram  notify.telegram.token_env (default SDLC_TELEGRAM_TOKEN) + notify.telegram.chat_id: alerts WITH buttons.
            The bot polls from this machine, so no public URL is needed. Only presses from that chat are obeyed.
Buttons: Approve / Request changes (a design), Answer (an escalated task: your reply resumes it),
Merge anyway (a change the merge gate handed over: hard limits are re-checked, only the agent's doubt is
overruled). A production release is never approved from a button unless notify.telegram.allow_release is true.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, changes, find_root, load_config, read_state, state_path, utcnow  # noqa: E402

DEFAULTS = {"slack_webhook_env": "SDLC_SLACK_WEBHOOK", "events": ["needs_you", "merged", "reverted", "manager"],
            "telegram": {"token_env": "SDLC_TELEGRAM_TOKEN", "chat_id": "", "allow_release": False}}


def ncfg(cfg: dict) -> dict:
    n = {**DEFAULTS, **(cfg.get("notify") or {})}
    n["telegram"] = {**DEFAULTS["telegram"], **((cfg.get("notify") or {}).get("telegram") or {})}
    return n


def _post(url: str, body: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _tg(token: str, method: str, body: dict, timeout: int = 40) -> dict:
    api = os.environ.get("SDLC_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
    return _post(f"{api}/bot{token}/{method}", body, timeout)


def channels(cfg: dict) -> dict:
    n = ncfg(cfg)
    out = {}
    if os.environ.get(n["slack_webhook_env"]):
        out["slack"] = os.environ[n["slack_webhook_env"]]
    tok = os.environ.get(n["telegram"]["token_env"])
    if tok and n["telegram"].get("chat_id"):
        out["telegram"] = (tok, str(n["telegram"]["chat_id"]))
    return out


def send(root: Path, cfg: dict, event: str, title: str, text: str, actions: list | None = None,
         key: str = "") -> list[str]:
    """Best effort and once per (event, key, text): a failing channel never breaks the run."""
    n = ncfg(cfg)
    if event not in n["events"] and event != "test":
        return []
    ch = channels(cfg)
    if not ch:
        return []
    sent_p = state_path(root, "notify.json")
    sent = read_state(root, "notify.json") or {}
    sig = hashlib.sha256(f"{event}|{key}|{title}|{text}".encode()).hexdigest()[:16]
    if sig in sent and event != "test":
        return []
    done = []
    if "slack" in ch:
        try:
            _post(ch["slack"], {"text": f"*{title}*\n{text}" + ("\n_Buttons: on Telegram, or the team board._"
                                                                   if actions else "")})
            done.append("slack")
        except (urllib.error.URLError, OSError):
            pass
    if "telegram" in ch:
        tok, chat = ch["telegram"]
        body = {"chat_id": chat, "text": f"{title}\n\n{text}"[:4000]}
        if actions:
            body["reply_markup"] = {"inline_keyboard": [[{"text": a["label"], "callback_data": a["action"][:64]}
                                                         for a in actions]]}
        try:
            _tg(tok, "sendMessage", body, 15)
            done.append("telegram")
        except (urllib.error.URLError, OSError):
            pass
    if done:
        sent[sig] = utcnow()
        sent_p.parent.mkdir(parents=True, exist_ok=True)
        sent_p.write_text(json.dumps(dict(list(sent.items())[-500:]), indent=1), encoding="utf-8")
        audit(root, "notify", event=event, key=key, channels=done)
    return done


# ------------------------------------------------------------------ what the buttons do
def act(root: Path, cfg: dict, action: str, text: str = "", who: str = "telegram") -> str:
    """Perform a human action asked for from the phone. Returns the reply to show."""
    kind, _, arg = action.partition(":")
    if kind in ("approve", "changes", "answer"):
        from board import Board
        b = Board()
        it = b.item(arg)
        if not it:
            return f"{arg} is not on the board"
        if kind == "approve":
            if it["type"] != "design" or it["status"] != "review":
                return f"{arg} is not waiting for approval"
            b.approve_design(arg, f"approved from {who}")
            return f"✅ {arg} approved — the UI tasks unlock"
        if kind == "changes":
            if not text:
                return "ASK:What should change in the design?"
            b.request_design_changes(arg, text)
            return f"↩️ {arg} sent back to the designer"
        if arg not in b.meta().get("escalated", []):
            return f"{arg} is not waiting for you any more"
        if not text:
            return f"ASK:What should the team do on {arg}?"
        b.resume(arg, text, actor=who[:40])
        return f"▶️ {arg} resumed with your answer"
    if kind == "merge":
        import merge_gate
        hits = [c for c in changes(root, cfg) if c.name == arg]
        if not hits:
            return f"no change {arg}"
        rec = merge_gate.decide(root, cfg, hits[0], "auto", owner=who)
        rec = merge_gate.apply(root, cfg, hits[0], rec)
        return f"🔀 {arg}: {rec.get('result')}" + "".join(f"\n- {r}" for r in rec["reasons"][:4])
    if kind == "release":
        if not ncfg(cfg)["telegram"].get("allow_release"):
            return "Release approval from the phone is off (notify.telegram.allow_release)."
        st = {"approver": who, "reference": arg, "granted_at": utcnow(),
              "ttl_minutes": cfg["prod_gate"].get("approval_ttl_minutes", 120)}
        p = state_path(root, "release-approval.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2), encoding="utf-8")
        audit(root, "release-approved", **st)
        return f"🚀 release {arg} authorised for {st['ttl_minutes']} min"
    return f"unknown action {action}"


def serve(root: Path, cfg: dict, once: bool = False) -> None:
    ch = channels(cfg)
    if "telegram" not in ch:
        sys.exit("Telegram is not configured: set notify.telegram.chat_id and the token environment variable")
    tok, chat = ch["telegram"]
    st = read_state(root, "notify-serve.json") or {"offset": 0, "pending": {}}
    while True:
        try:
            r = _tg(tok, "getUpdates", {"offset": st["offset"], "timeout": 0 if once else 30,
                                        "allowed_updates": ["callback_query", "message"]})
        except (urllib.error.URLError, OSError):
            time.sleep(5)
            continue
        for u in r.get("result") or []:
            st["offset"] = u["update_id"] + 1
            cq, msg = u.get("callback_query"), u.get("message")
            src = (cq or {}).get("message", {}).get("chat", {}) if cq else (msg or {}).get("chat", {})
            if str(src.get("id")) != chat:  # only the owner's chat is obeyed
                audit(root, "notify-refused", chat=src.get("id"))
                continue
            who = "telegram:" + str(((cq or msg).get("from") or {}).get("username") or src.get("id"))
            if cq:
                reply = act(root, cfg, cq.get("data", ""), who=who)
                try:
                    _tg(tok, "answerCallbackQuery", {"callback_query_id": cq["id"]}, 15)
                except (urllib.error.URLError, OSError):
                    pass
                if reply.startswith("ASK:"):
                    st["pending"][chat] = cq.get("data", "")
                    reply = reply[4:] + " (reply with a message)"
            else:
                action = st["pending"].pop(chat, "")
                txt = (msg.get("text") or "").strip()
                reply = act(root, cfg, action, txt, who) if action and txt else \
                    "Use the buttons on a notification, or the team board."
            try:
                _tg(tok, "sendMessage", {"chat_id": chat, "text": reply[:4000]}, 15)
            except (urllib.error.URLError, OSError):
                pass
        p = state_path(root, "notify-serve.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st), encoding="utf-8")
        if once:
            return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test")
    s = sub.add_parser("serve")
    s.add_argument("--once", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    if a.cmd == "test":
        done = send(root, cfg, "test", "SDLC kit", "Notifications work. You'll hear from me when the team needs you.")
        print(f"sent to: {', '.join(done) or 'nothing — no channel configured (see notify.py --help)'}")
    else:
        serve(root, cfg, a.once)


if __name__ == "__main__":
    main()
