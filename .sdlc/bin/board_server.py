#!/usr/bin/env python3
"""Live team board in the browser: kanban, epic → story → task tree, search, agent status, team chat.

  python .sdlc/bin/board_server.py [--port 8765] [--host 127.0.0.1] [--db PATH]

Reads the same board the agents write (board.py). The page polls every 1.5 s, so moves, QA verdicts,
merges and agent activity show up live. You can message any agent (or the whole team) from the page;
agents read it with `board.py inbox`. Binds to localhost by default — the page has no login.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board import Board  # noqa: E402

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team Board</title>
<style>
:root{
  --bg:#0a0c10;--panel:#10131a;--panel2:#151922;--raise:#1a1f2a;--line:#212633;--line2:#2b3141;
  --text:#e8ebf1;--muted:#97a0b2;--faint:#5f6879;
  --blue:#6ea8fe;--green:#4ade80;--amber:#fbbf24;--red:#f87171;--violet:#a78bfa;--pink:#f472b6;--teal:#2dd4bf;
  --on:#0a0c10;--shadow:0 12px 32px rgba(0,0,0,.45);--r:12px;
}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){
  --bg:#f4f5f8;--panel:#ffffff;--panel2:#f7f8fa;--raise:#eef0f4;--line:#e3e6ec;--line2:#d4d9e2;
  --text:#141821;--muted:#5a6273;--faint:#8b93a3;
  --blue:#1f6feb;--green:#16a34a;--amber:#b7791f;--red:#dc2626;--violet:#6d4fd8;--pink:#d6337f;--teal:#0f8a7a;
  --on:#ffffff;--shadow:0 12px 32px rgba(20,30,50,.14);}}
:root[data-theme="light"]{
  --bg:#f4f5f8;--panel:#ffffff;--panel2:#f7f8fa;--raise:#eef0f4;--line:#e3e6ec;--line2:#d4d9e2;
  --text:#141821;--muted:#5a6273;--faint:#8b93a3;
  --blue:#1f6feb;--green:#16a34a;--amber:#b7791f;--red:#dc2626;--violet:#6d4fd8;--pink:#d6337f;--teal:#0f8a7a;
  --on:#ffffff;--shadow:0 12px 32px rgba(20,30,50,.14);}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:13.5px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;overflow:hidden}
button,input,select,textarea{font:inherit;color:inherit}
a{color:var(--blue);text-decoration:none} a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px;border-radius:6px}
.mono{font-family:ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace;font-size:.93em}
.sub{color:var(--muted);font-size:12px}
.grow{flex:1}
::-webkit-scrollbar{width:10px;height:10px} ::-webkit-scrollbar-thumb{background:var(--line2);border-radius:99px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}
.scroll{overflow:auto;scrollbar-width:thin;scrollbar-color:var(--line2) transparent}

/* ---------- header ---------- */
header{flex:none;display:flex;align-items:center;gap:14px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{width:32px;height:32px;border-radius:9px;flex:none;display:grid;place-items:center;color:#fff;font-weight:800;
  background:linear-gradient(140deg,var(--blue),var(--violet));box-shadow:inset 0 0 0 1px rgba(255,255,255,.12)}
h1{margin:0;font-size:15px;font-weight:700;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;height:26px;padding:0 10px;border-radius:99px;border:1px solid var(--line);
  font-size:12px;color:var(--muted);background:var(--panel2);white-space:nowrap}
.pill b{color:var(--text);font-weight:600}
.pill.alert{color:var(--pink);border-color:color-mix(in srgb,var(--pink) 45%,var(--line));background:color-mix(in srgb,var(--pink) 10%,var(--panel2));cursor:pointer}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.dot.running,.dot.working{background:var(--green);animation:pulse 1.4s infinite}
.dot.helping{background:var(--violet);animation:pulse 1.4s infinite}
.dot.designing{background:var(--pink);animation:pulse 1.4s infinite}
.dot.done{background:var(--blue)} .dot.needs-human{background:var(--red)}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,currentColor 0%,transparent)}50%{opacity:.5}}
.prog{display:flex;align-items:center;gap:10px;min-width:210px}
.prog .bar{flex:1}
.bar{height:6px;border-radius:99px;background:var(--raise);overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--teal),var(--green));transition:width .5s}
.prog b{font-size:12.5px;font-variant-numeric:tabular-nums}
.search{display:flex;align-items:center;gap:8px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:0 10px;width:280px;height:34px;color:var(--muted)}
.search:focus-within{border-color:color-mix(in srgb,var(--blue) 60%,var(--line))}
.search input{border:0;background:transparent;outline:none;width:100%;color:var(--text)}
.search kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--faint)}
.icon-btn{width:34px;height:34px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);cursor:pointer;color:var(--muted);display:grid;place-items:center;flex:none}
.icon-btn:hover{color:var(--text)}

/* ---------- banner ---------- */
#banner:empty{display:none}
.now{flex:none;display:flex;align-items:center;gap:10px;margin:10px 18px 0;padding:9px 14px;border-radius:10px;
  background:var(--panel);border:1px solid var(--line);font-size:13px;min-height:40px;overflow:hidden}
.now:empty{display:none}
.now .lbl{font-size:10.5px;font-weight:800;letter-spacing:.08em;color:var(--faint);text-transform:uppercase;flex:none}
.now .msg{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.now .t{color:var(--faint);font-size:11.5px;flex:none;margin-left:auto}
.now.flash{animation:nowflash 1.2s ease-out}
@keyframes nowflash{0%{border-color:var(--green);box-shadow:0 0 0 3px color-mix(in srgb,var(--green) 25%,transparent)}100%{}}
.banner{flex:none;margin:12px 18px 0;padding:12px 14px;border-radius:var(--r);display:flex;gap:14px;align-items:center;
  border:1px solid color-mix(in srgb,var(--pink) 40%,var(--line));background:linear-gradient(90deg,color-mix(in srgb,var(--pink) 12%,var(--panel)),var(--panel))}
.banner .ic{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;background:color-mix(in srgb,var(--pink) 20%,transparent);font-size:18px;flex:none}
.banner b{font-size:13.5px}
.banner .sub{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.btn{border:0;border-radius:9px;height:32px;padding:0 13px;font-weight:650;cursor:pointer;background:var(--blue);color:var(--on);white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.btn.ok{background:var(--green)} .btn.warn{background:var(--amber)}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line2)}
.btn:hover{filter:brightness(1.07)}

/* ---------- shell ---------- */
.shell{flex:1;min-height:0;display:grid;grid-template-columns:264px minmax(0,1fr) 330px;grid-template-rows:minmax(0,1fr)}
.pane{min-height:0;display:flex;flex-direction:column;border-right:1px solid var(--line)}
.pane:last-child{border-right:0;border-left:1px solid var(--line)}
.center{border-right:0}
.ph{flex:none;display:flex;align-items:center;gap:8px;padding:12px 14px 10px;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
.ph .n{margin-left:auto;font-weight:600;letter-spacing:0;text-transform:none}

/* ---------- sidebar: epics & stories ---------- */
.side{background:var(--panel)}
.nav{flex:1;min-height:0;padding:0 10px 14px}
.navall{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:9px;cursor:pointer;color:var(--muted);margin-bottom:6px}
.navall:hover,.srow:hover{background:var(--panel2)}
.navall.on,.srow.on{background:color-mix(in srgb,var(--blue) 12%,var(--panel2));color:var(--text);box-shadow:inset 2px 0 0 var(--blue)}
.epic{border:1px solid var(--line);border-radius:var(--r);background:var(--panel2);margin-bottom:10px;overflow:hidden}
.ehd{display:flex;gap:10px;align-items:center;padding:11px 12px;cursor:pointer}
.ring{--p:0;width:34px;height:34px;border-radius:50%;flex:none;display:grid;place-items:center;
  background:conic-gradient(var(--green) calc(var(--p)*1%),var(--raise) 0)}
.ring span{width:26px;height:26px;border-radius:50%;background:var(--panel2);display:grid;place-items:center;font-size:9.5px;font-weight:700;color:var(--muted)}
.ehd .t{font-weight:650;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.chev{color:var(--faint);transition:transform .15s;flex:none}
.epic.closed .chev{transform:rotate(-90deg)}
.epic.closed .stories{display:none}
.stories{border-top:1px solid var(--line);padding:6px}
.srow{display:grid;grid-template-columns:8px 1fr auto;grid-template-rows:auto auto;column-gap:9px;row-gap:5px;align-items:center;padding:8px 9px;border-radius:9px;cursor:pointer}
.srow .sd{width:8px;height:8px;border-radius:3px}
.srow .st{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.srow .sc{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.srow .bar{grid-column:2 / 4;height:4px}
.srow.design .st{color:var(--pink)}

/* ---------- center ---------- */
.toolbar{flex:none;display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line)}
.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{border:0;background:transparent;color:var(--muted);height:28px;padding:0 12px;border-radius:7px;cursor:pointer;font-weight:600;display:inline-flex;gap:6px;align-items:center}
.seg button[aria-selected="true"]{background:var(--raise);color:var(--text);box-shadow:0 1px 0 rgba(0,0,0,.2)}
.crumb{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.crumb .sub{font-weight:500}
select{background:var(--panel2);border:1px solid var(--line);border-radius:9px;height:32px;padding:0 9px;max-width:180px}
.clear{border:0;background:transparent;color:var(--blue);cursor:pointer;font-weight:600;padding:0 4px}
.views{flex:1;min-height:0;position:relative}
.kanban{position:absolute;inset:0;display:grid;grid-template-columns:repeat(5,minmax(156px,1fr));grid-template-rows:minmax(0,1fr);gap:10px;padding:14px 16px;overflow-x:auto}
.col{min-height:0;display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.colh{flex:none;display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);font-weight:650;font-size:12.5px;
  box-shadow:inset 0 2px 0 var(--c)}
.colh .sw{width:8px;height:8px;border-radius:50%;background:var(--c)}
.colh .n{margin-left:auto;color:var(--muted);background:var(--raise);border-radius:99px;padding:0 8px;font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
.cards{flex:1;min-height:0;padding:9px;display:flex;flex-direction:column;gap:8px}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 11px 10px 13px;cursor:pointer;position:relative;
  transition:border-color .15s,transform .15s,box-shadow .15s}
.card::before{content:"";position:absolute;left:0;top:9px;bottom:9px;width:3px;border-radius:0 3px 3px 0;background:var(--sc,var(--line2))}
.card:hover{border-color:var(--line2);transform:translateY(-1px);box-shadow:0 6px 16px rgba(0,0,0,.18)}
.card.live{border-color:color-mix(in srgb,var(--green) 55%,var(--line))}
.card.design{border-style:dashed;border-color:color-mix(in srgb,var(--pink) 50%,var(--line));--sc:var(--pink)}
.card .top{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--faint)}
.card .top .grow{min-width:0} .card .top .mono{white-space:nowrap}
.card .ttl{margin:5px 0 9px;font-weight:600;line-height:1.35;color:var(--text)}
.card .foot{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.livebar.wait{color:var(--muted)}
.livebar{margin-top:8px;padding-top:7px;border-top:1px dashed var(--line);font-size:11.5px;color:var(--green);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.livebar.pink{color:var(--pink)}
.prog-card{margin-top:8px}.prog-card .bar{height:5px}
.prog-meta{display:flex;gap:8px;font-size:11px;color:var(--muted);margin-top:5px;font-variant-numeric:tabular-nums}
.prog-meta .ok{color:var(--green)}
.tag{font-size:11px;height:20px;display:inline-flex;align-items:center;padding:0 7px;border-radius:6px;white-space:nowrap;max-width:140px;overflow:hidden;text-overflow:ellipsis;
  background:color-mix(in srgb,var(--sc) 14%,transparent);color:var(--sc);border:1px solid color-mix(in srgb,var(--sc) 30%,transparent)}
.chip{font-size:11px;height:20px;display:inline-flex;align-items:center;gap:3px;padding:0 6px;border-radius:6px;color:var(--muted);border:1px solid var(--line);white-space:nowrap}
.chip.red{color:var(--red);border-color:color-mix(in srgb,var(--red) 40%,var(--line))}
.chip.amber{color:var(--amber);border-color:color-mix(in srgb,var(--amber) 40%,var(--line))}
.chip.pink{color:var(--pink);border-color:color-mix(in srgb,var(--pink) 40%,var(--line))}
.av{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-weight:800;font-size:11px;color:#0a0c10;flex:none}
.av.developer{background:var(--blue)} .av.qa{background:var(--green)} .av.planner,.av.helper{background:var(--violet)}
.av.human,.av.lead{background:var(--amber)} .av.designer{background:var(--pink)} .av.sync{background:var(--teal)}
.av.mini{width:20px;height:20px;border-radius:6px;font-size:9px}
.empty{color:var(--faint);font-size:12px;text-align:center;padding:22px 8px}
.empty.big{padding:60px 20px;font-size:13px}

/* list view */
.list{position:absolute;inset:0;padding:14px 16px}
.grp{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);margin-bottom:12px;overflow:hidden}
.gh{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--line);cursor:pointer}
.gh .t{font-weight:650}
.gh .bar{width:110px}
.grp.closed .rows{display:none} .grp.closed .chev{transform:rotate(-90deg)} .grp.closed .gh{border-bottom:0}
.row{display:grid;grid-template-columns:58px minmax(0,1fr) 118px 64px 64px 110px;gap:12px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.row:last-child{border-bottom:0}
.row:hover{background:var(--panel2)} .row>.badge{justify-self:start}
.row .ti{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:550}
.rowh{cursor:default;color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;background:var(--panel2)}
.rowh:hover{background:var(--panel2)}
.badge{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:99px;font-size:11.5px;font-weight:600;white-space:nowrap;
  color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--c)}
.s-todo{--c:var(--muted)} .s-in_progress{--c:var(--blue)} .s-blocked{--c:var(--red)} .s-in_qa{--c:var(--amber)} .s-done{--c:var(--green)} .s-review{--c:var(--pink)}

/* ---------- right: team + activity ---------- */
.right{background:var(--panel)}
.agents{flex:none;max-height:38%;padding:0 10px 8px}
.ag{display:flex;gap:10px;align-items:center;padding:8px;border-radius:10px}
.ag:hover{background:var(--panel2)}
.ag .nm{display:flex;align-items:center;gap:6px;font-weight:650}
.ag .nm .sub{font-weight:500}
.ag .act{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ag .cost{margin-left:auto;text-align:right;font-size:11.5px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}
.divider{height:1px;background:var(--line);flex:none}
.feedh{display:flex;align-items:center}
.feedh label{margin-left:auto;display:flex;gap:6px;align-items:center;text-transform:none;letter-spacing:0;font-weight:500;color:var(--muted);cursor:pointer}
.feed{flex:1;min-height:0;padding:2px 6px 8px}
.ev{display:flex;gap:9px;padding:7px 8px;font-size:12.5px;border-radius:9px}
.ev .av{width:24px;height:24px;border-radius:7px;font-size:9px}
.ev .who{font-weight:650}
.ev .when{color:var(--faint);font-size:11px;margin-left:5px}
.ev .txt{color:var(--muted);word-break:break-word;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.ev.msg .txt{color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:4px 10px 10px 10px;padding:7px 9px;margin-top:3px;-webkit-line-clamp:8}
.ev.qa-pass .txt{color:var(--green)} .ev.qa-fail .txt{color:var(--red)} .ev.merge .txt{color:var(--teal)}
.compose{flex:none;display:flex;gap:6px;padding:10px;border-top:1px solid var(--line)}
.compose select{max-width:110px}
.compose input{flex:1;min-width:0;background:var(--panel2);border:1px solid var(--line);border-radius:9px;height:32px;padding:0 10px;outline:none}
.compose input:focus{border-color:color-mix(in srgb,var(--blue) 60%,var(--line))}

/* ---------- dialog ---------- */
dialog{border:1px solid var(--line2);background:var(--panel);color:var(--text);border-radius:16px;padding:0;width:min(780px,94vw);max-height:88vh;box-shadow:var(--shadow)}
dialog::backdrop{background:rgba(5,7,10,.62);backdrop-filter:blur(2px)}
.dh{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.db{padding:16px 18px;overflow-y:auto;max-height:calc(88vh - 82px)}
.kv{display:grid;grid-template-columns:120px 1fr;gap:8px 14px;font-size:13px;margin:0 0 6px}
.kv dt{color:var(--muted)} .kv dd{margin:0}
.sec{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint);font-weight:700;margin:18px 0 8px}
ul.ac{margin:0;padding-left:18px} ul.ac li{margin:3px 0}
pre.desc{white-space:pre-wrap;margin:0;font:inherit;color:var(--muted)}
.kid{display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line);cursor:pointer}

/* ---------- responsive ---------- */
@media (max-width:1400px){.shell{grid-template-columns:236px minmax(0,1fr) 296px}.search{width:220px}.prog{min-width:150px}}
@media (max-width:1060px){
  body{overflow:auto;height:auto} html{height:auto}
  .shell{display:flex;flex-direction:column}
  .pane{border:0;border-bottom:1px solid var(--line)} .pane:last-child{border-left:0}
  .nav{max-height:280px} .views{height:auto}
  .kanban{position:static;grid-template-rows:auto;grid-template-columns:repeat(5,minmax(240px,1fr))} .col{max-height:70vh}
  .list{position:static} .agents{max-height:none} .feed{max-height:60vh}
  header{flex-wrap:wrap} .search{width:100%;order:9} .prog{min-width:160px}
}
@media (max-width:640px){
  header,.toolbar,.kanban,.list{padding-left:16px;padding-right:16px}
  .banner{margin:12px 16px 0;flex-wrap:wrap} .banner .grow{flex-basis:calc(100% - 52px)} .banner .btn{flex:1;justify-content:center}
  .row{grid-template-columns:52px minmax(0,1fr) 104px}.row .hide-s{display:none}
  .pills .pill:not(:first-child){display:none}
}
</style>
</head>
<body>
<header>
  <div class="brand"><div class="logo" aria-hidden="true">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5 9.5 18 20 6.5"/></svg></div>
    <div style="min-width:0"><h1 id="title">Team Board</h1><div class="sub" id="subtitle">loading…</div></div></div>
  <div class="pills">
    <span class="pill"><span class="dot" id="rundot"></span><b id="runstate">—</b></span>
    <span class="pill">tracker <b id="tracker">internal</b></span>
    <span class="pill">branch <b class="mono" id="branch">—</b></span>
    <span class="pill alert" id="needs" hidden></span>
  </div>
  <div class="grow"></div>
  <div class="prog" title="Tasks done"><div class="bar"><i id="progbar" style="width:0"></i></div><b id="progtxt">0/0</b></div>
  <label class="search"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="q" placeholder="Search tasks, stories, agents…" aria-label="Search"><kbd>/</kbd></label>
  <button class="icon-btn" id="theme" title="Toggle light / dark" aria-label="Toggle light or dark theme">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg></button>
</header>
<div id="banner"></div>
<div id="now" class="now" aria-live="polite"></div>
<div class="shell">
  <aside class="pane side" aria-label="Epics and stories">
    <div class="ph">Epics &amp; stories <span class="n" id="sidecount"></span></div>
    <nav class="nav scroll" id="nav"></nav>
  </aside>
  <section class="pane center" aria-label="Work">
    <div class="toolbar">
      <div class="seg" role="tablist">
        <button role="tab" data-view="board" aria-selected="true">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="10" y="4" width="5" height="11" rx="1.5"/><rect x="17" y="4" width="4" height="7" rx="1.5"/></svg>Board</button>
        <button role="tab" data-view="list" aria-selected="false">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>List</button>
      </div>
      <div class="crumb" id="crumb"></div>
      <div class="grow"></div>
      <select id="fwho" aria-label="Filter by agent"><option value="">Everyone</option></select>
    </div>
    <div class="views">
      <div id="board" class="kanban"></div>
      <div id="list" class="list scroll" hidden></div>
    </div>
  </section>
  <aside class="pane right" aria-label="Team and activity">
    <div class="ph">Team <span class="n" id="teamcount"></span></div>
    <div class="agents scroll" id="team"></div>
    <div class="divider"></div>
    <div class="ph feedh">Activity <label><input type="checkbox" id="onlymsg"> messages only</label></div>
    <div class="feed scroll" id="feed" aria-live="polite"></div>
    <form class="compose" id="compose">
      <select id="to" aria-label="Recipient"><option value="all">@all</option></select>
      <input id="msg" placeholder="Message the team…" autocomplete="off" aria-label="Message">
      <button class="btn" type="submit">Send</button>
    </form>
  </aside>
</div>
<dialog id="dlg"><div class="dh"><div style="min-width:0"><div class="sub mono" id="dkey"></div><h2 id="dtitle" style="margin:4px 0 0;font-size:18px;letter-spacing:-.01em"></h2></div>
  <button class="icon-btn" id="dclose" aria-label="Close">✕</button></div><div class="db" id="dbody"></div></dialog>
<script>
const COLS=[["todo","To Do","var(--muted)"],["in_progress","In Progress","var(--blue)"],["blocked","Blocked","var(--red)"],["in_qa","Review & QA","var(--amber)"],["done","Done","var(--green)"]];
const LABEL=Object.assign(Object.fromEntries(COLS.map(c=>[c[0],c[1]])),{in_qa:"In QA",review:"Needs approval"});
const COLOF=st=>st==="review"?"in_qa":st;
const STORY_COLORS=["var(--violet)","var(--teal)","var(--amber)","var(--blue)","var(--pink)","var(--green)","var(--red)"];
let S={items:[],agents:[],events:[],messages:[],meta:{},progress:{},live:{},now:0},since=0,view="board",openKey=null,fstory="",lastSig="";
const closedEpics=new Set(),closedGroups=new Set();
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ini=n=>{n=n||"?";const m=n.match(/^([a-z])[a-z]*-(\d+)$/i);return m?(m[1]+m[2]).toUpperCase():n.replace(/[^a-z0-9]/gi,"").slice(0,2).toUpperCase()||"?"};
const roleOf=n=>{const a=S.agents.find(x=>x.name===n);return a?a.role:(n==="human"?"human":n==="sync"?"sync":n==="lead"?"lead":"developer")};
const ago=ts=>{if(!ts)return"";const s=Math.max(0,(Date.now()-Date.parse(ts))/1000);return s<60?Math.floor(s)+"s":s<3600?Math.floor(s/60)+"m":s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d"};
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
const badge=st=>`<span class="badge s-${esc(st)}">${esc(LABEL[st]||st)}</span>`;
const items=t=>S.items.filter(i=>i.type===t);
function storyColor(k){const i=items("story").findIndex(s=>s.key===k);return STORY_COLORS[(i<0?0:i)%STORY_COLORS.length]}
function pct(list){const d=list.filter(t=>t.status==="done").length;return list.length?Math.round(100*d/list.length):0}
function match(t,q){if(!q)return true;const story=S.items.find(i=>i.key===t.parent_key)||{};
  return [t.key,t.title,t.description,t.assignee,t.qa_by,LABEL[t.status],story.title,story.key,(t.files||[]).join(" ")].join(" ").toLowerCase().includes(q)}
function work(){const q=$("q").value.trim().toLowerCase(),fw=$("fwho").value;
  return S.items.filter(i=>(i.type==="task"||i.type==="design")&&match(i,q)
    &&(!fstory||(fstory==="design"?i.type==="design":i.parent_key===fstory))
    &&(!fw||i.assignee===fw||(i.qa_by||"").endsWith(fw)))}
const BUSY=["working","helping","designing"];
function curKey(a){const j=((S.meta.tl||{}).jobs||{})[a.name];if(!j||!(j.keys||[]).length)return a.item_key;
  if(j.kind!=="developer")return j.keys[0];
  return j.keys.find(k=>(S.items.find(i=>i.key===k)||{}).status==="in_progress")||j.keys[j.keys.length-1]}
function liveOn(t){const key=t.key,jobs=(S.meta.tl||{}).jobs;
  if(jobs){for(const [name,j] of Object.entries(jobs)){if(!(j.keys||[]).includes(key))continue;
      const a=S.agents.find(x=>x.name===name)||{name,status:"working",activity:""};
      if(j.kind==="developer"){if(t.status!=="in_progress")continue;  // handed in: the developer is on its next task
        const cur=j.keys.find(k=>(S.items.find(i=>i.key===k)||{}).status==="in_progress");if(cur===key)return a}
      else return a}
    return null}
  return S.agents.find(a=>a.item_key===key&&BUSY.includes(a.status)&&!(t.status==="in_qa"&&roleOf(a.name)==="developer"))}
const dur=sec=>sec==null?"":sec<60?`${Math.floor(sec)}s`:sec<3600?`${Math.floor(sec/60)}m`:`${Math.floor(sec/3600)}h ${Math.floor(sec%3600/60)}m`;
function jobOf(name){return ((S.meta.tl||{}).jobs||{})[name]}
function lastSaid(name){const e=[...S.events].reverse().find(x=>x.actor===name&&(x.kind==="comment"||x.kind==="message"));
  return e?{text:e.text.slice(0,120),age:S.now-Date.parse(e.ts)/1000}:null}
function liveText(a,key){let c;const j=jobOf(a.name);const p=key?(S.progress||{})[key]:null;const bits=[];
  const lv=(S.live||{})[a.name];const lvAge=lv?S.now-Date.parse(lv.ts)/1000:null;
  if(lv&&lvAge<900)bits.push(`${lv.text} (${dur(lvAge)} ago)`);
  else if((c=lastSaid(a.name))&&c.age<900)bits.push(`💬 ${c.text} (${dur(c.age)} ago)`);
  else if(p&&p.last_file)bits.push(`editing ${p.last_file.split("/").slice(-2).join("/")} (${dur(S.now-p.last_change)} ago)`);
  else if(a.activity&&!/^assigned:/.test(a.activity))bits.push(a.activity);
  if(j&&j.started)bits.push(`${dur(S.now-j.started)} on it`);
  return bits.join(" · ")||a.activity||a.status}
function keepScroll(fn){const ids=["nav","list","team","feed"];const pos={};
  ids.forEach(id=>{const e=$(id);if(e)pos[id]=e.scrollTop});
  const cols={};document.querySelectorAll(".cards[data-col]").forEach(c=>cols[c.dataset.col]=c.scrollTop);
  const bx=$("board").scrollLeft;const db=document.querySelector(".db");const dbs=db?db.scrollTop:0;
  fn();
  ids.forEach(id=>{if(id!=="feed"&&$(id))$(id).scrollTop=pos[id]||0});
  document.querySelectorAll(".cards[data-col]").forEach(c=>c.scrollTop=cols[c.dataset.col]||0);
  $("board").scrollLeft=bx;const db2=document.querySelector(".db");if(db2)db2.scrollTop=dbs}

/* header + banner */
function renderHeader(){const m=S.meta,ts=items("task"),d=ts.filter(t=>t.status==="done").length;
  $("title").textContent=m.project?`${m.project} · team board`:"Team Board";
  const cost=S.agents.reduce((s,a)=>s+(a.cost||0),0);
  $("subtitle").textContent=(m.change||"no change loaded")+(cost?` · $${cost.toFixed(2)} est.`:"");
  $("runstate").textContent=m.status||"idle";$("rundot").className="dot "+(m.status||"");
  $("tracker").textContent=m.tracker||"internal";$("branch").textContent=m.integration_branch||"—";
  $("progtxt").textContent=`${d}/${ts.length}`;$("progbar").style.width=(ts.length?100*d/ts.length:0)+"%";
  const esc_=(m.escalated||[]).length,rev=S.items.filter(i=>i.type==="design"&&i.status==="review").length,n=esc_+rev;
  $("needs").hidden=!n;$("needs").textContent=n?`● ${n} need${n===1?"s":""} you`:"";document.title=(n?`(${n}) `:"")+"Team Board"}
function renderBanner(){const d=S.items.find(i=>i.type==="design"&&i.status==="review");
  if(!d){$("banner").innerHTML="";return}
  const last=[...S.events].reverse().find(e=>e.item_key===d.key&&e.actor==="designer"&&e.kind==="status");
  $("banner").innerHTML=`<div class="banner" role="region" aria-label="Design approval"><div class="ic" aria-hidden="true">🎨</div>
   <div class="grow" style="min-width:0"><b>Design ${esc(d.key)} is ready for your review</b><div class="sub">${esc(last?last.text.split(": ").slice(1).join(": "):d.title)}</div></div>
   <a class="btn ghost" href="design/${esc(d.key)}" target="_blank" rel="noopener">Open mockup ↗</a>
   <button class="btn ok" data-design="approve" data-dkey="${esc(d.key)}">Approve</button>
   <button class="btn warn" data-design="changes" data-dkey="${esc(d.key)}">Request changes</button></div>`}

/* sidebar */
function renderNav(){const epics=items("epic"),stories=items("story"),tasks=items("task");
  $("sidecount").textContent=epics.length?`${epics.length} epic${epics.length>1?"s":""} · ${stories.length} stories`:"";
  if(!epics.length){$("nav").innerHTML=`<div class="empty">No epics yet.<br>Run <span class="mono">team.py plan</span></div>`;return}
  let h=`<div class="navall${fstory?"":" on"}" data-story="" role="button" tabindex="0">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
    <span class="grow">All work</span><span class="sub">${tasks.length} tasks</span></div>`;
  for(const e of epics){const ss=stories.filter(s=>s.parent_key===e.key);const all=tasks.filter(t=>ss.some(s=>s.key===t.parent_key));const p=pct(all);
    const ds=S.items.filter(i=>i.type==="design"&&i.parent_key===e.key);
    h+=`<div class="epic${closedEpics.has(e.key)?" closed":""}"><div class="ehd" data-epic="${esc(e.key)}" role="button" tabindex="0" aria-expanded="${!closedEpics.has(e.key)}">
      <div class="ring" style="--p:${p}"><span>${p}%</span></div>
      <div style="min-width:0;flex:1"><div class="t">${esc(e.title)}</div><div class="sub"><span class="mono">${esc(e.key)}</span> · ${ss.length} stories · ${all.length} tasks${e.ext_url?` · <a href="${esc(e.ext_url)}" target="_blank" rel="noopener">${esc(S.meta.tracker)} ↗</a>`:""}</div></div>
      <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></div>
      <div class="stories">
      ${ds.map(d=>`<div class="srow design${fstory==="design"?" on":""}" data-story="design" role="button" tabindex="0">
        <span class="sd" style="background:var(--pink)"></span><span class="st">🎨 ${esc(d.key)} · Design</span><span class="sc">${esc(LABEL[d.status]||d.status)}</span></div>`).join("")}
      ${ss.map(s=>{const st=tasks.filter(t=>t.parent_key===s.key),dn=st.filter(t=>t.status==="done").length;
        return `<div class="srow${fstory===s.key?" on":""}" data-story="${esc(s.key)}" role="button" tabindex="0" title="${esc(s.title)}">
        <span class="sd" style="background:${storyColor(s.key)}"></span><span class="st">${esc(s.title)}</span><span class="sc">${dn}/${st.length}</span>
        <span class="bar"><i style="width:${pct(st)}%"></i></span></div>`}).join("")}
      </div></div>`}
  $("nav").innerHTML=h}

/* crumb */
function renderCrumb(){if(!fstory){$("crumb").innerHTML=`All work <span class="sub">· ${(n=>n+(n===1?" item":" items"))(work().length)}</span>`;return}
  const s=fstory==="design"?{key:"Design",title:"UI design"}:S.items.find(i=>i.key===fstory)||{};
  $("crumb").innerHTML=`<span class="tag" style="--sc:${fstory==="design"?"var(--pink)":storyColor(fstory)}">${esc(s.key)}</span> ${esc(s.title||"")} <span class="sub">· ${(n=>n+(n===1?" item":" items"))(work().length)}</span> <button class="clear" data-story="">Clear</button>`}

/* board */
function card(t){const live=liveOn(t);
  if(t.type==="design"){const dm=(S.meta.design||{});
    return `<div class="card design${live?" live":""}" data-key="${esc(t.key)}" tabindex="0" role="button" aria-label="${esc(t.key+" "+t.title)}">
     <div class="top"><span class="mono">🎨 ${esc(t.key)}</span><span class="grow"></span><span class="chip pink">${esc(dm.mode||"design")}</span></div>
     <div class="ttl">${esc(t.title)}</div>
     <div class="foot"><span class="av mini designer" title="designer">DS</span>
      ${t.status==="review"?`<span class="chip pink">Needs your approval</span>`:""}
      ${t.qa_rounds?`<span class="chip amber">changes ×${t.qa_rounds}</span>`:""}
      ${t.status!=="todo"?`<a class="chip" href="design/${esc(t.key)}" target="_blank" rel="noopener">mockup ↗</a>`:""}</div>
     ${live?`<div class="livebar pink">● designer · ${esc(live.activity||"designing")}</div>`:""}</div>`}
  const story=S.items.find(i=>i.key===t.parent_key)||{};
  const waiting=(t.depends_on||[]).filter(d=>(S.items.find(i=>i.key===d)||{}).status!=="done");
  const qa=t.qa_by||"",qaName=qa.includes(":")?qa.split(":")[1]:qa,escd=(S.meta.escalated||[]).includes(t.key);
  return `<div class="card${live?" live":""}" style="--sc:${storyColor(t.parent_key)}" data-key="${esc(t.key)}" tabindex="0" role="button" aria-label="${esc(t.key+" "+t.title)}">
   <div class="top"><span class="mono">${esc(t.key)}</span><span class="grow"></span>
     ${t.ext_url?`<a href="${esc(t.ext_url)}" target="_blank" rel="noopener">${esc(t.ext_key||S.meta.tracker||"link")} ↗</a>`:""}</div>
   <div class="ttl">${esc(t.title)}</div>
   <div class="foot">
     <span class="tag" style="--sc:${storyColor(t.parent_key)}" title="${esc(story.title)}">${esc(story.key)}</span>
     ${t.assignee?`<span class="av mini ${roleOf(t.assignee)}" title="developer ${esc(t.assignee)}">${ini(t.assignee)}</span>`:""}
     ${qaName&&t.status!=="todo"&&!qaName.startsWith("approved")?`<span class="av mini qa" title="QA ${esc(qaName)}">${ini(qaName)}</span>`:""}
     ${t.qa_rounds?`<span class="chip red" title="sent back by QA">↺ ${t.qa_rounds}</span>`:""}
     ${escd?`<span class="chip red" title="escalated to you">⚑ needs you</span>`:""}
     ${waiting.length&&t.status==="todo"?`<span class="chip amber" title="waiting on">🔒 ${esc(waiting.join(", "))}</span>`:""}
   </div>
   ${progressBlock(t)}
   ${live?`<div class="livebar">● ${esc(live.name)} · ${esc(liveText(live,t.key))}</div>`:
     t.status==="in_qa"&&!(t.qa_by||"").startsWith("pass:")?`<div class="livebar wait">⏳ ${t.qa_by?"QA "+esc(t.qa_by.replace(/^fail:/,"")):"waiting for QA"}</div>`:
     t.status==="in_qa"?`<div class="livebar wait">✓ passed QA · merges with the rest of its story</div>`:""}
  </div>`}
function progressBlock(t){const p=(S.progress||{})[t.key];if(!p||t.status!=="in_progress")return"";
  const tot=Math.max(p.files_total,1),pc=Math.round(100*Math.min(p.files_done,tot)/tot)*(p.committed?1:0.9);
  const age=p.last_change?Math.max(0,S.now-p.last_change):null;
  const ago=age===null?"no changes yet":age<60?`${Math.floor(age)}s ago`:age<3600?`${Math.floor(age/60)}m ago`:`${Math.floor(age/3600)}h ago`;
  return `<div class="prog-card"><div class="bar"><i style="width:${p.committed?100:Math.max(4,pc)}%"></i></div>
    <div class="prog-meta"><span>${p.files_total?`${p.files_done}/${p.files_total} files`:`${p.changed} files changed`}</span>
    ${p.committed?`<span class="ok">✓ committed</span>`:""}<span class="grow"></span><span title="last change in the worktree">${esc(ago)}</span></div></div>`}
function renderBoard(){const ts=work();
  $("board").innerHTML=COLS.map(([k,l,c])=>{const cs=ts.filter(t=>COLOF(t.status)===k);
    return `<section class="col" style="--c:${c}" aria-label="${l}"><div class="colh"><span class="sw"></span>${l}<span class="n">${cs.length}</span></div>
      <div class="cards scroll" data-col="${k}">${cs.map(card).join("")||`<div class="empty">Nothing here</div>`}</div></section>`}).join("")}

/* list */
function row(t){const qa=t.qa_by||"",qaName=qa.includes(":")?qa.split(":")[1]:"";
  const waiting=(t.depends_on||[]).filter(d=>(S.items.find(i=>i.key===d)||{}).status!=="done");
  return `<div class="row" data-key="${esc(t.key)}" tabindex="0" role="button"><span class="mono sub">${t.type==="design"?"🎨 ":""}${esc(t.key)}</span>
   <span class="ti">${esc(t.title)}</span>${badge(t.status)}
   <span class="hide-s">${t.assignee?`<span class="av mini ${roleOf(t.assignee)}" title="${esc(t.assignee)}">${ini(t.assignee)}</span>`:`<span class="sub">—</span>`}</span>
   <span class="hide-s">${qaName&&!qa.startsWith("approved")?`<span class="av mini qa" title="${esc(qaName)}">${ini(qaName)}</span>`:`<span class="sub">—</span>`}</span>
   <span class="hide-s sub">${t.qa_rounds?`<span class="chip red">↺ ${t.qa_rounds}</span> `:""}${waiting.length&&t.status==="todo"?`🔒 ${esc(waiting.join(", "))}`:""}</span></div>`}
function renderList(){const ts=work();const groups=[];
  const ds=ts.filter(t=>t.type==="design");if(ds.length)groups.push({key:"design",title:"UI design",color:"var(--pink)",rows:ds,all:items("design")});
  for(const s of items("story")){const r=ts.filter(t=>t.parent_key===s.key);if(r.length)groups.push({key:s.key,title:s.title,color:storyColor(s.key),rows:r,all:items("task").filter(t=>t.parent_key===s.key),s})}
  if(!groups.length){$("list").innerHTML=`<div class="empty big">No matching work</div>`;return}
  $("list").innerHTML=groups.map(g=>`<div class="grp${closedGroups.has(g.key)?" closed":""}"><div class="gh" data-group="${esc(g.key)}" role="button" tabindex="0" aria-expanded="${!closedGroups.has(g.key)}">
     <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
     <span class="tag" style="--sc:${g.color}">${esc(g.key==="design"?"Design":g.key)}</span><span class="t grow">${esc(g.title)}</span>
     ${g.s?badge(g.s.status):""}<span class="sub">${g.all.filter(t=>t.status==="done").length}/${g.all.length}</span><span class="bar"><i style="width:${pct(g.all)}%"></i></span></div>
     <div class="rows"><div class="row rowh"><span>Key</span><span>Title</span><span>Status</span><span class="hide-s">Dev</span><span class="hide-s">QA</span><span class="hide-s">Notes</span></div>${g.rows.map(row).join("")}</div></div>`).join("")}

/* team + feed */
function renderTeam(){const ag=S.agents.filter(a=>a.role!=="planner"||a.status==="working");
  const busy=ag.filter(a=>["working","helping","designing"].includes(a.status)).length;
  $("teamcount").textContent=ag.length?`${busy} active · ${ag.length}`:"";
  $("team").innerHTML=ag.map(a=>{const ck=curKey(a);const t=S.items.find(i=>i.key===ck);
    return `<div class="ag"><div class="av ${esc(a.role)}">${ini(a.name)}</div><div style="min-width:0;flex:1">
     <div class="nm">${esc(a.name)} <span class="dot ${esc(a.status)}" title="${esc(a.status)}"></span><span class="sub">${esc(a.role)}</span></div>
     <div class="act" title="${esc(a.activity)}">${["working","helping","designing"].includes(a.status)?
        `${t?`<a href="#" data-key="${esc(t.key)}" class="mono">${esc(t.key)}</a> `:""}${esc(liveText(a,ck))}`:
        `<span class="sub">idle</span>${a.activity?` · ${esc(a.activity)}`:""}`}</div></div>
     <div class="cost">${a.runs||a.cost?`${a.runs||0} runs<br>$${(a.cost||0).toFixed(2)}`:""}</div></div>`}).join("")||`<div class="empty">No agents yet</div>`;
  const opts=["all","developer","qa",...S.agents.map(a=>a.name)];const cur=$("to").value;
  $("to").innerHTML=opts.map(o=>`<option value="${esc(o)}">@${esc(o)}</option>`).join("");$("to").value=opts.includes(cur)?cur:"all";
  const fw=$("fwho").value;$("fwho").innerHTML=`<option value="">Everyone</option>`+S.agents.filter(a=>a.role!=="planner").map(a=>`<option>${esc(a.name)}</option>`).join("");$("fwho").value=fw}
let lastNowId=0;
function renderNow(){const want=["comment","qa","merge","message","help","assign","design"];
  const e=[...S.events].reverse().find(x=>want.includes(x.kind)&&!(x.kind==="assign"&&/^task created|^assigned to/.test(x.text)));
  if(!e){$("now").innerHTML="";return}
  const icon={qa:e.text.startsWith("PASS")?"✅":"❌",merge:"🔀",comment:"💬",message:"✉️",help:"🤝",assign:"▶️",design:"🎨"}[e.kind]||"•";
  $("now").innerHTML=`<span class="lbl">Now</span><span aria-hidden="true">${icon}</span><span class="msg"><b>${esc(e.actor)}</b>${e.item_key?` <a href="#" data-key="${esc(e.item_key)}" class="mono">${esc(e.item_key)}</a>`:""} · ${esc(e.text)}</span><span class="t">${ago(e.ts)} ago</span>`;
  if(e.id!==lastNowId){if(lastNowId){$("now").classList.remove("flash");void $("now").offsetWidth;$("now").classList.add("flash")}lastNowId=e.id}}
function evClass(e){if(e.kind==="message"||e.kind==="comment")return"msg";if(e.kind==="qa")return e.text.startsWith("PASS")?"qa-pass":"qa-fail";if(e.kind==="merge")return"merge";return""}
function renderFeed(){const only=$("onlymsg").checked,q=$("q").value.trim().toLowerCase();
  const evs=S.events.filter(e=>(!only||["message","qa","help","comment"].includes(e.kind))&&(!q||(e.text+" "+e.actor+" "+(e.item_key||"")).toLowerCase().includes(q))).slice(-200);
  const f=$("feed"),atBottom=f.scrollHeight-f.scrollTop-f.clientHeight<40,prev=f.scrollTop;
  f.innerHTML=evs.map(e=>`<div class="ev ${evClass(e)}"><div class="av ${esc(roleOf(e.actor))}">${ini(e.actor)}</div><div style="min-width:0;flex:1">
    <div><span class="who">${esc(e.actor)}</span>${e.item_key?` <a href="#" data-key="${esc(e.item_key)}" class="mono">${esc(e.item_key)}</a>`:""}<span class="when">${ago(e.ts)}</span></div>
    <div class="txt" title="${esc(e.text)}">${esc(e.text)}</div></div></div>`).join("")||`<div class="empty">Quiet so far</div>`;
  f.scrollTop=atBottom?f.scrollHeight:prev}

/* detail dialog */
function detail(key){const t=S.items.find(i=>i.key===key);if(!t)return;openKey=key;
  const story=S.items.find(i=>i.key===t.parent_key)||{},kids=S.items.filter(i=>i.parent_key===key),evs=S.events.filter(e=>e.item_key===key);
  $("dkey").textContent=`${t.type.toUpperCase()} · ${t.key}`;$("dtitle").textContent=t.title;
  $("dbody").innerHTML=`<dl class="kv"><dt>Status</dt><dd>${badge(t.status)}${t.qa_rounds?` <span class="chip red">↺ QA ${t.qa_rounds}</span>`:""}</dd>
   ${t.parent_key?`<dt>Parent</dt><dd><a href="#" data-key="${esc(story.key)}">${esc(story.key)}</a> ${esc(story.title)}</dd>`:""}
   ${t.assignee?`<dt>Developer</dt><dd>${esc(t.assignee)}</dd>`:""}${t.qa_by?`<dt>QA</dt><dd>${esc(t.qa_by)}</dd>`:""}
   ${t.branch?`<dt>Branch</dt><dd class="mono">${esc(t.branch)}</dd>`:""}
   ${(t.depends_on||[]).length?`<dt>Depends on</dt><dd>${t.depends_on.map(d=>`<a href="#" data-key="${esc(d)}">${esc(d)}</a>`).join(", ")}</dd>`:""}
   ${(t.files||[]).length?`<dt>Files</dt><dd class="mono" style="font-size:12px">${t.files.map(esc).join("<br>")}</dd>`:""}
   ${t.ext_url?`<dt>${esc(S.meta.tracker)}</dt><dd><a href="${esc(t.ext_url)}" target="_blank" rel="noopener">${esc(t.ext_key)} ↗</a></dd>`:""}
   ${t.type==="design"&&t.status!=="todo"?`<dt>Mockup</dt><dd><a href="design/${esc(t.key)}" target="_blank" rel="noopener">open mockup ↗</a> · <a href="design/${esc(t.key)}/notes" target="_blank" rel="noopener">design notes</a></dd>`:""}
   ${t.type==="design"&&t.status==="review"?`<dt>Decision</dt><dd><button class="btn ok" data-design="approve" data-dkey="${esc(t.key)}">Approve</button> <button class="btn warn" data-design="changes" data-dkey="${esc(t.key)}">Request changes</button></dd>`:""}
   ${(S.meta.escalated||[]).includes(t.key)?`<dt>Escalated</dt><dd><button class="btn warn" data-resume="${esc(t.key)}">Resume with guidance</button></dd>`:""}</dl>
   ${t.description?`<div class="sec">Description</div><pre class="desc">${esc(t.description)}</pre>`:""}
   ${(t.acceptance||[]).length?`<div class="sec">Acceptance criteria</div><ul class="ac">${t.acceptance.map(a=>`<li>${esc(a)}</li>`).join("")}</ul>`:""}
   ${kids.length?`<div class="sec">${t.type==="epic"?"Stories":"Tasks"}</div>${kids.map(k=>`<div class="kid" data-key="${esc(k.key)}"><span class="mono sub">${esc(k.key)}</span><span class="grow">${esc(k.title)}</span>${badge(k.status)}</div>`).join("")}`:""}
   <div class="sec">History</div>${evs.map(e=>`<div class="ev ${evClass(e)}" style="padding:6px 0"><div class="av ${esc(roleOf(e.actor))}">${ini(e.actor)}</div><div style="min-width:0"><span class="who">${esc(e.actor)}</span><span class="when">${esc((e.ts||"").slice(11,19))}</span><div class="txt" style="-webkit-line-clamp:unset">${esc(e.text)}</div></div></div>`).join("")||`<div class="sub">No history yet</div>`}`;
  if(!$("dlg").open)$("dlg").showModal()}

function render(){keepScroll(()=>{renderHeader();renderBanner();renderNow();renderNav();renderCrumb();renderTeam();
  view==="board"?renderBoard():renderList();renderFeed();if($("dlg").open&&openKey)detail(openKey)})}
async function poll(){try{const r=await fetch("api/board?since="+since);const d=await r.json();
    S.items=d.items;S.agents=d.agents;S.meta=d.meta;S.messages=d.messages;S.progress=d.progress||{};S.live=d.live||{};S.now=d.now||Date.now()/1000;
    if(d.events.length){S.events=since?S.events.concat(d.events).slice(-800):d.events;since=d.events[d.events.length-1].id}
    const sig=JSON.stringify([d.items,d.agents,d.meta,since,d.progress,d.live]);if(sig!==lastSig){lastSig=sig;render()}
  }catch(e){$("runstate").textContent="offline";$("rundot").className="dot needs-human"}finally{setTimeout(poll,1500)}}

/* events */
function setView(v){view=v;document.querySelectorAll(".seg button").forEach(b=>b.setAttribute("aria-selected",b.dataset.view===v));
  $("board").hidden=v!=="board";$("list").hidden=v!=="list";store.set("sdlc-board-view",v);render()}
document.addEventListener("click",e=>{
  const da=e.target.closest("[data-design]");if(da){designAction(da.dataset.dkey,da.dataset.design);return}
  const rs=e.target.closest("[data-resume]");if(rs){resumeTask(rs.dataset.resume);return}
  const sv=e.target.closest(".seg button");if(sv){setView(sv.dataset.view);return}
  const ep=e.target.closest("[data-epic]");if(ep&&!e.target.closest("a")){const k=ep.dataset.epic;closedEpics.has(k)?closedEpics.delete(k):closedEpics.add(k);render();return}
  const gp=e.target.closest("[data-group]");if(gp){const k=gp.dataset.group;closedGroups.has(k)?closedGroups.delete(k):closedGroups.add(k);render();return}
  const st=e.target.closest("[data-story]");if(st){fstory=st.dataset.story;render();return}
  if(e.target.closest("#needs")){const d=S.items.find(i=>i.type==="design"&&i.status==="review");detail(d?d.key:(S.meta.escalated||[])[0]);return}
  const k=e.target.closest("[data-key]");if(k&&!e.target.closest("a[target]")){e.preventDefault();detail(k.dataset.key)}});
document.addEventListener("keydown",e=>{
  if(e.key==="/"&&document.activeElement.tagName!=="INPUT"){e.preventDefault();$("q").focus();return}
  if(e.key==="Escape"&&document.activeElement===$("q")){$("q").value="";render();$("q").blur();return}
  if(e.key==="Enter"||e.key===" "){const t=e.target;if(!t.closest)return;
    const x=t.closest("[data-epic],[data-group],[data-story],[data-key][role=button]");if(x&&x===t){e.preventDefault();x.click()}}});
async function designAction(key,action){let text="";
  if(action==="changes"){text=prompt("What should the designer change?");if(!text)return}
  const r=await fetch("api/design",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,action,text})});
  if(!r.ok){alert((await r.json()).error||"failed")}lastSig=""}
async function resumeTask(key){const text=prompt(`Guidance for the team on ${key}:`);if(!text)return;
  const r=await fetch("api/resume",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,text})});
  if(!r.ok){alert((await r.json()).error||"failed")}lastSig=""}
$("dclose").onclick=()=>{openKey=null;$("dlg").close()};$("dlg").addEventListener("close",()=>openKey=null);
["q","fwho","onlymsg"].forEach(id=>$(id).addEventListener("input",render));
$("compose").onsubmit=async e=>{e.preventDefault();const text=$("msg").value.trim();if(!text)return;$("msg").value="";
  await fetch("api/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({to:$("to").value,text,task:openKey})});lastSig=""};
(function theme(){const saved=store.get("sdlc-board-theme");if(saved)document.documentElement.dataset.theme=saved;
  $("theme").onclick=()=>{const cur=document.documentElement.dataset.theme||(matchMedia("(prefers-color-scheme: light)").matches?"light":"dark");
    const nx=cur==="light"?"dark":"light";document.documentElement.dataset.theme=nx;store.set("sdlc-board-theme",nx)}})();
if(store.get("sdlc-board-view")==="list")setView("list");
poll();
</script>
</body>
</html>
"""


_PROGRESS = {"at": 0.0, "data": {}}


def _git(cwd: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=10).stdout
    except Exception:
        return ""


def live_progress(board: Board, root: Path, cfg: dict) -> dict:
    """Per in-progress task: planned files written so far, whether it is committed, and when its worktree last
    changed — read straight from the developer's git worktree, so the board moves while the work happens."""
    if time.time() - _PROGRESS["at"] < 4:
        return _PROGRESS["data"]
    out: dict = {}
    try:
        meta = board.meta()
        integ = meta.get("integration_branch") or ""
        wt_root = root / ((cfg.get("team") or {}).get("worktree_dir") or ".sdlc/worktrees")
        tasks = [t for t in board.items(type_="task") if t["status"] in ("in_progress", "blocked") and t.get("assignee")]
        by_wt: dict = {}
        for t in tasks:
            by_wt.setdefault(t["assignee"], []).append(t)
        for agent, ts in by_wt.items():
            wt = wt_root / agent
            if not (wt / ".git").exists() or not integ:
                continue
            changed = set(_git(wt, "diff", "--name-only", f"{integ}...HEAD").split())
            for line in _git(wt, "status", "--porcelain", "-uall").splitlines():
                path = line[3:].strip().strip('"')
                if " -> " in path:
                    path = path.split(" -> ")[-1]
                if path:
                    changed.add(path.replace("\\", "/"))
            changed = {c for c in changed if not c.startswith(".sdlc/")}
            subjects = _git(wt, "log", "--format=%s", f"{integ}..HEAD").splitlines()
            newest, newest_file = 0.0, ""
            for c in changed:
                try:
                    m = (wt / c).stat().st_mtime
                except OSError:
                    continue
                if m > newest:
                    newest, newest_file = m, c
            for t in ts:
                planned = [f.replace("\\", "/") for f in (t.get("files") or [])]
                done = [f for f in planned if f in changed or any(c.startswith(f.rstrip("/") + "/") for c in changed)]
                out[t["key"]] = {"files_done": len(done), "files_total": len(planned), "changed": len(changed),
                                 "committed": any(sub.startswith(t["key"] + ":") for sub in subjects),
                                 "last_change": newest, "last_file": newest_file}
    except Exception:
        pass
    _PROGRESS.update(at=time.time(), data=out)
    return out


_LIVE = {"at": 0.0, "data": {}}
_AGENT_RE = re.compile(r"assignments[\\/]+((?:dev|qa)-\d+|designer)-")


def _describe(block: dict, root: str) -> str | None:
    if block.get("type") == "text" and (block.get("text") or "").strip():
        return "💭 " + " ".join(block["text"].split())[:110]
    if block.get("type") != "tool_use":
        return None
    n, i = block.get("name", ""), block.get("input") or {}
    rel = lambda f: re.split(r"worktrees[\\/][^\\/]+[\\/]", str(f or ""))[-1].replace("\\", "/")
    if n == "Bash":
        cmd = re.sub(r'^\s*cd\s+("[^"]*"|\S+)\s*&&\s*', "", " ".join((i.get("command") or "").split()))
        return "$ " + cmd[:110]
    if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{'write' if n == 'Write' else 'edit'} {rel(i.get('file_path'))}"
    if n == "Read":
        return f"read {rel(i.get('file_path'))}"
    if n in ("Grep", "Glob"):
        return f"search {str(i.get('pattern') or '')[:60]}"
    if n.startswith("mcp__playwright__"):
        return f"browser: {n.split('__')[-1].replace('browser_', '')} {str(i.get('url') or i.get('element') or '')[:60]}"
    if n == "Skill":
        return f"skill: {i.get('skill', '')}"
    if n == "SendMessage":
        msg = str(i.get("message") or "")
        return ("✓ " + msg[:100]) if msg.lower().startswith("done") else f"message → {i.get('to', '')}: {msg[:70]}"
    if n == "SubagentHandback":
        return "✓ " + str(i.get("message") or "finished")[:100]
    if n in ("TaskUpdate", "TaskCreate", "TaskList", "TaskGet"):
        return None
    return n


def _first_prompt_agent(head: str) -> str | None:
    for line in head.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        c = (d.get("message") or {}).get("content")
        text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict)
                                                    and x.get("type") == "text")
        if not text.strip():
            continue
        m = _AGENT_RE.search(text) or re.search(r"\byou are ((?:dev|qa)-\d+|designer)\b", text, re.I)
        return m.group(1).lower() if m else None
    return None


def live_activity(root: Path) -> dict:
    """What every agent-team teammate is doing right now, read from Claude Code's own transcripts
    (~/.claude/projects/*/<session>/subagents/agent-*.jsonl) — the same data its agent panel shows. No hooks."""
    if time.time() - _LIVE["at"] < 2.5:
        return _LIVE["data"]
    out: dict = {}
    try:
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        want = str(root).replace("\\", "/").rstrip("/").lower()
        cutoff = time.time() - 6 * 3600
        for f in base.glob("*/**/*.jsonl"):  # every recent transcript; a teammate's starts with its assignment
            try:
                st = f.stat()
                if st.st_mtime < cutoff:
                    continue
                with open(f, "rb") as fh:
                    head = fh.read(16384).decode("utf-8", "replace")
                    fh.seek(max(0, st.st_size - 65536))
                    tail = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
            cwd = json.loads('"' + m.group(1) + '"').replace("\\", "/").rstrip("/").lower() if m else ""
            if cwd != want and not cwd.startswith(want + "/"):
                continue
            agent = _first_prompt_agent(head)  # only the first prompt names the teammate (the lead's own
            if not agent:                      # transcript mentions every assignment, so never read names elsewhere)
                continue
            last, ts = None, ""
            for line in reversed(tail.splitlines()):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                for blk in reversed((d.get("message") or {}).get("content") or []):
                    last = _describe(blk, str(root))
                    if last:
                        break
                if last:
                    ts = d.get("timestamp") or ""
                    break
            if last and (agent not in out or ts > out[agent]["ts"]):
                out[agent] = {"text": last, "ts": ts}
    except Exception:
        pass
    _LIVE.update(at=time.time(), data=out)
    return out


class Handler(BaseHTTPRequestHandler):
    board: Board = None  # set in main
    root: Path = None
    cfg: dict = {}

    def design_file(self, name: str) -> Path | None:
        """The design file from the designer's worktree (latest) or the integration branch (approved)."""
        m = self.board.meta()
        change = m.get("change")
        if not change or not self.root:
            return None
        rel = Path(self.cfg.get("changes_dir", "sdlc/changes")) / change / "design" / name
        wt = self.root / (self.cfg.get("team", {}).get("worktree_dir") or ".sdlc/worktrees")
        for base in (wt / "designer", wt / "integration", self.root):
            p = (base / rel).resolve()
            if p.is_file() and str(p).startswith(str(self.root.resolve())):
                return p
        return None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path.startswith("/design/"):
            parts = u.path.strip("/").split("/")
            name = "design-notes.md" if len(parts) > 2 and parts[2] == "notes" else "mockup.html"
            p = self.design_file(name)
            if not p:
                return self._json({"error": "no design yet"}, 404)
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/board":
            since = int((parse_qs(u.query).get("since") or ["0"])[0] or 0)
            snap = self.board.snapshot(since=since)
            snap["progress"] = live_progress(self.board, self.root, self.cfg) if self.root else {}
            snap["live"] = live_activity(self.root) if self.root else {}
            snap["now"] = time.time()
            self._json(snap)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/design":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            it = self.board.item(str(d.get("key") or ""))
            if not it or it["type"] != "design" or it["status"] != "review":
                return self._json({"error": "that design is not waiting for approval"}, 409)
            if d.get("action") == "approve":
                self.board.approve_design(it["key"], str(d.get("text") or "")[:1000])
            elif d.get("action") == "changes" and str(d.get("text") or "").strip():
                self.board.request_design_changes(it["key"], str(d["text"]).strip()[:2000])
            else:
                return self._json({"error": "action must be approve, or changes with text"}, 400)
            return self._json({"ok": True})
        if path == "/api/resume":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            key, text = str(d.get("key") or ""), str(d.get("text") or "").strip()[:2000]
            if key not in self.board.meta().get("escalated", []):
                return self._json({"error": "that task is not escalated"}, 409)
            if not text:
                return self._json({"error": "give the team some guidance"}, 400)
            self.board.resume(key, text)
            return self._json({"ok": True})
        if path != "/api/message":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        text = str(d.get("text") or "").strip()[:2000]
        if not text:
            return self._json({"error": "empty"}, 400)
        mid = self.board.send("human", str(d.get("to") or "all")[:40], text, d.get("task") or None)
        self._json({"id": mid})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int)
    ap.add_argument("--db")
    a = ap.parse_args()
    port = a.port
    if not port:
        try:
            from sdlc_core import find_root, load_config
            root = find_root()
            port = load_config(root)["team"]["board_port"] if root else 8765
        except Exception:
            port = 8765
    Handler.board = Board(Path(a.db) if a.db else None)
    try:
        from sdlc_core import find_root, load_config
        Handler.root = find_root() or Handler.board.path.parent.parent.parent
        Handler.cfg = load_config(Handler.root) if (Handler.root / ".sdlc" / "config.json").is_file() else {}
    except Exception:
        Handler.root = Handler.board.path.parent.parent.parent
    srv = ThreadingHTTPServer((a.host, port), Handler)
    print(f"team board: http://{a.host}:{port}   (db {Handler.board.path})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
