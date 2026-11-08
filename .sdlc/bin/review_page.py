"""The Review room page (served by board_server.py at /review, shown in the board's Review tab)."""

REVIEW_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review</title>
<style>
:root{--bg:#0a0c10;--panel:#10131a;--panel2:#151922;--line:#212633;--line2:#2b3141;--ink:#e8ebf1;--ink2:#97a0b2;--ink3:#5f6879;
  --accent:#6ea8fe;--ok:#4ade80;--okbg:rgba(74,222,128,.12);--wait:#fbbf24;--waitbg:rgba(251,191,36,.12);--bad:#f87171;
  --add:rgba(74,222,128,.12);--code:#0d1017;--sel:rgba(110,168,254,.25);--me:#3b5bdb}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f4f5f8;--panel:#fff;--panel2:#f7f8fa;--line:#e3e6ec;--line2:#d4d9e2;
  --ink:#141821;--ink2:#5a6273;--ink3:#8b93a3;--accent:#1f6feb;--ok:#16a34a;--okbg:rgba(22,163,74,.1);--wait:#b7791f;--waitbg:rgba(183,121,31,.1);
  --add:rgba(22,163,74,.1);--code:#f3f5f8;--sel:rgba(31,111,235,.15);--me:#1f6feb}}
:root[data-theme="light"]{--bg:#f4f5f8;--panel:#fff;--panel2:#f7f8fa;--line:#e3e6ec;--line2:#d4d9e2;--ink:#141821;--ink2:#5a6273;
  --ink3:#8b93a3;--accent:#1f6feb;--ok:#16a34a;--okbg:rgba(22,163,74,.1);--wait:#b7791f;--waitbg:rgba(183,121,31,.1);--add:rgba(22,163,74,.1);
  --code:#f3f5f8;--sel:rgba(31,111,235,.15);--me:#1f6feb}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);font:13.5px/1.5 Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;overflow:hidden}
::selection{background:var(--sel)}
button,textarea{font:inherit;color:inherit} button{background:none;border:0;cursor:pointer}
.app{display:grid;grid-template-columns:240px minmax(0,1fr) 400px;height:100vh}
.app>*{min-width:0}
/* rail */
.rail{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:12px 10px}
.rail h4{font-size:11px;letter-spacing:.08em;color:var(--ink3);text-transform:uppercase;margin:12px 8px 6px}
.art{width:100%;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:8px;border-radius:8px;text-align:left;color:var(--ink2)}
.art:hover{background:var(--panel2)} .art.on{background:var(--panel2);color:var(--ink);box-shadow:inset 2px 0 0 var(--accent)}
.art b{display:block;font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.art small{display:block;font-size:11px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{font-size:10.5px;padding:2px 7px;border-radius:99px;font-weight:700;white-space:nowrap}
.badge.ok{background:var(--okbg);color:var(--ok)} .badge.wait{background:var(--waitbg);color:var(--wait)} .badge.none{color:var(--ink3);border:1px solid var(--line2)}
.empty{color:var(--ink3);padding:30px 16px;text-align:center}
/* document */
.doc{display:grid;grid-template-rows:auto 1fr;min-height:0}
.dochead{display:flex;align-items:center;gap:10px;padding:10px 20px;border-bottom:1px solid var(--line);background:var(--panel);min-width:0}
.path{font:12px ui-monospace,"JetBrains Mono",Menlo,monospace;color:var(--ink3);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vers{display:flex;gap:4px;flex-wrap:wrap}
.ver{font-size:12px;padding:3px 9px;border-radius:6px;color:var(--ink3);border:1px solid transparent;white-space:nowrap}
.ver.on{color:var(--ink);border-color:var(--line2);background:var(--panel2)}
.grow{flex:1}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;border:1px solid var(--line2);font-size:12px;color:var(--ink2);white-space:nowrap}
.chip i{width:7px;height:7px;border-radius:50%;background:var(--wait)} .chip.ok i{background:var(--ok)} .chip.none i{background:var(--ink3)}
.scroll{overflow:auto;position:relative}
.paper{max-width:860px;margin:0 auto;padding:28px 40px 120px}
.md h1{font-size:26px;line-height:1.25;margin:0 0 10px;letter-spacing:-.01em}
.md h2{font-size:18px;margin:28px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
.md h3{font-size:15px;margin:18px 0 6px}
.md p{margin:8px 0} .md ul,.md ol{margin:8px 0 8px 22px} .md li{margin:3px 0}
.md code{font:12.5px ui-monospace,"JetBrains Mono",Menlo,monospace;background:var(--code);border:1px solid var(--line);padding:1px 5px;border-radius:5px}
.md pre{background:var(--code);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:auto;margin:10px 0}
.md pre code{border:0;padding:0;background:none}
.md .tw{overflow:auto;margin:12px 0} .md table{width:100%;border-collapse:collapse;font-size:13px}
.md .meta{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 14px} .md .meta span{font-size:12px;color:var(--ink2);background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:2px 10px} .md .meta b{color:var(--ink);font-weight:600}
.md th,.md td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.md th{color:var(--ink2);background:var(--panel2)}
.md blockquote{border-left:3px solid var(--accent);padding:4px 14px;color:var(--ink2);margin:10px 0;background:var(--panel2);border-radius:0 8px 8px 0}
.md img{max-width:100%;border-radius:12px;border:1px solid var(--line);background:#fff;margin:8px 0}
.md .task{list-style:none;margin-left:-18px} .md .task input{margin-right:8px}
.md a{color:var(--accent)}
.md .added{background:var(--add);box-shadow:inset 3px 0 0 var(--ok);border-radius:4px;padding-left:12px;margin-left:-12px}
.md details{margin:6px 0;color:var(--ink3);font-size:12px}
.mock{width:100%;height:520px;border:1px solid var(--line);border-radius:12px;background:#fff;margin:6px 0 16px}
.ask{position:absolute;display:none;z-index:5;background:var(--accent);color:#fff;border-radius:8px;padding:6px 10px;font-size:12px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,.3)}
/* chat */
.chat{display:grid;grid-template-rows:auto auto 1fr auto;border-left:1px solid var(--line);background:var(--panel);min-height:0}
.chathead{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.av{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff;flex:none}
.av-architect{background:linear-gradient(135deg,#db2777,#f472b6)} .av-designer{background:linear-gradient(135deg,#7c3aed,#a78bfa)}
.av-planner{background:linear-gradient(135deg,#0284c7,#38bdf8)} .av-lead{background:linear-gradient(135deg,#475569,#94a3b8)} .av-you{background:#334155}
.chathead b{font-size:14px} .chathead small{display:block;font-size:12px;color:var(--ink3)}
.gate{padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel2)}
.gate p{font-size:12.5px;color:var(--ink2);margin-bottom:10px} .gate p b{color:var(--ink)}
.gate textarea{width:100%;min-height:54px;resize:vertical;background:var(--panel);border:1px solid var(--line2);border-radius:8px;padding:8px;margin-bottom:8px;color:var(--ink)}
.row{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:7px 12px;border-radius:8px;font-weight:700;font-size:13px;border:1px solid var(--line2)}
.btn.primary{background:var(--ok);color:#052e1f;border-color:transparent} .btn:disabled{opacity:.5;cursor:default}
.gate.done{background:var(--okbg)} .gate.err p{color:var(--bad)}
.msgs{overflow:auto;padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.msg{display:flex;gap:10px} .msg.me{flex-direction:row-reverse}
.bubble{background:var(--panel2);border:1px solid var(--line);border-radius:4px 14px 14px 14px;padding:9px 12px;font-size:13.5px;line-height:1.55;max-width:310px;white-space:pre-wrap;overflow-wrap:anywhere}
.msg.me .bubble{background:var(--me);color:#fff;border-color:transparent;border-radius:14px 4px 14px 14px}
.bubble.err{border-color:var(--bad)}
.who{font-size:11px;color:var(--ink3);margin-bottom:3px} .msg.me .who{text-align:right}
.quote{font-size:12px;border-left:2px solid rgba(255,255,255,.6);padding-left:8px;margin-bottom:6px;opacity:.9;font-style:italic}
.sys{align-self:center;font-size:12px;color:var(--ink3);background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:4px 12px;text-align:center}
.sys.ok{color:var(--ok);background:var(--okbg);border-color:transparent} .sys.changed{color:var(--accent);cursor:pointer}
.typing span{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--ink3);margin-right:3px;animation:b 1.2s infinite}
.typing span:nth-child(2){animation-delay:.2s} .typing span:nth-child(3){animation-delay:.4s}
@keyframes b{0%,80%,100%{opacity:.25}40%{opacity:1}}
.suggest{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 10px}
.sg{font-size:12px;padding:5px 10px;border-radius:99px;border:1px solid var(--line2);color:var(--ink2);background:var(--panel2)} .sg:hover{color:var(--ink);border-color:var(--accent)}
.composer{border-top:1px solid var(--line);padding:10px 12px 12px}
.ctx{display:none;font-size:12px;color:var(--ink2);background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin-bottom:8px}
.ctx b{color:var(--accent)} .ctx button{float:right;color:var(--ink3)}
.box{display:flex;gap:8px;align-items:flex-end;background:var(--panel2);border:1px solid var(--line2);border-radius:12px;padding:8px}
.box:focus-within{border-color:var(--accent)}
.box textarea{flex:1;resize:none;background:none;border:0;outline:0;max-height:140px;min-height:22px;color:var(--ink)}
.send{width:34px;height:34px;border-radius:9px;background:var(--accent);color:#fff;display:grid;place-items:center;font-size:16px}
.hint{font-size:11px;color:var(--ink3);margin-top:6px}
@media (max-width:1200px){.app{grid-template-columns:200px minmax(0,1fr) 340px}}
@media (max-width:860px){body{overflow-x:hidden;overflow-y:auto}.app{display:block;height:auto}
  .rail{display:flex;overflow-x:auto;gap:6px;border-right:0;border-bottom:1px solid var(--line)} .rail h4{display:none} .art{width:auto;flex:none}
  .scroll{overflow:visible} .paper{padding:20px 16px 40px} .chat{border-left:0;border-top:1px solid var(--line);height:80vh}}
</style>
</head>
<body>
<div class="app">
  <nav class="rail" id="rail"><div class="empty">Loading…</div></nav>
  <section class="doc">
    <div class="dochead"><span class="path" id="path"></span><div class="vers" id="vers"></div><div class="grow"></div>
      <span class="chip none" id="status"><i></i><span></span></span></div>
    <div class="scroll" id="scroll"><article class="paper md" id="paper"></article><button class="ask" id="ask">💬 Ask about this</button></div>
  </section>
  <aside class="chat">
    <div class="chathead"><div class="av av-lead" id="av">—</div><div><b id="agentname">—</b><small id="agentsub"></small></div></div>
    <div class="gate" id="gate" hidden></div>
    <div class="msgs" id="msgs"></div>
    <div><div class="suggest" id="suggest"></div>
      <div class="composer">
        <div class="ctx" id="ctx"><button id="ctxx" title="Remove quote">✕</button>Quoting <b id="ctxsec"></b>: <span id="ctxtext"></span></div>
        <div class="box"><textarea id="input" rows="1" placeholder="Ask, or say what to change…"></textarea><button class="send" id="send" title="Send">↑</button></div>
        <div class="hint" id="hint">Select text in the document to quote it.</div>
      </div></div>
  </aside>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const S = { list: [], cur: null, doc: null, ver: "", chatN: -1, pending: false, quote: null, sending: false };
const AG = { architect: ["Architect", "AR"], designer: ["Designer", "DE"], planner: ["Planner", "PL"], lead: ["Team lead", "TL"] };
const SUGGEST = {
  architecture: ["What are the riskiest decisions here?", "Will this run on my laptop?", "Explain the key flows simply"],
  adr: ["Why this option over the others?", "When should we revisit this?"],
  design: ["Walk me through the main screen", "What did you change from the brief?"],
  escalation: ["What exactly are you stuck on?", "What are my options?"],
  plan: ["Which tasks can run in parallel?", "What is the riskiest task?"],
  doc: ["Summarise this in 3 lines", "What is still unclear?"],
};

/* theme follows the board around it */
function syncTheme() { try { const t = parent.document.documentElement.dataset.theme; if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme; } catch (e) {} }
setInterval(syncTheme, 1000); syncTheme();

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const d = await r.json().catch(() => ({ error: "bad response" }));
  if (!r.ok && !d.error) d.error = "request failed (" + r.status + ")";
  return d;
}

/* ---------- safe Markdown: everything is escaped first; links and images are checked ---------- */
function safeUrl(u) { u = u.trim(); return /^(https?:\/\/|#|\.{0,2}\/|[\w-]+[\w./-]*$)/i.test(u) && !/^\s*(javascript|data|vbscript):/i.test(u) ? u : "#"; }
function imgSrc(u) { if (/^https?:/i.test(u)) return null; const base = (S.doc && S.doc.path || "").split("/").slice(0, -1).join("/");
  const parts = (base ? base + "/" + u : u).split("/"), out = []; for (const p of parts) { if (p === "..") out.pop(); else if (p && p !== ".") out.push(p); }
  return "api/review/file?path=" + encodeURIComponent(out.join("/")); }
function inline(s) {
  s = esc(s);
  return s.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m, a, u) => { const src = imgSrc(u.replace(/&amp;/g, "&")); return src ? `<img alt="${a}" src="${src}">` : `<a href="${safeUrl(u)}">${a}</a>`; })
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, a, u) => `<a href="${safeUrl(u)}" target="_blank" rel="noopener">${a}</a>`)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/~~([^~]+)~~/g, "<s>$1</s>").replace(/(^|\W)\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
}
function md(src, added, diagram) {
  const L = src.split("\n"), out = [], add = new Set(added || []);
  let i = 0, sec = "";
  const cls = (a, b) => { for (let k = a; k < b; k++) if (add.has(k)) return ' class="added"'; return ""; };
  while (i < L.length) {
    const l = L[i], start = i;
    let m;
    if (/^```/.test(l)) { const lang = l.slice(3).trim(); const b = []; i++; while (i < L.length && !/^```/.test(L[i])) b.push(L[i++]); i++;
      if (lang === "mermaid" && diagram) out.push(`<div${cls(start, i)} data-sec="${esc(sec)}"><img alt="diagram" src="api/review/file?path=${encodeURIComponent(diagram)}&t=${Date.now() >> 14}"><details><summary>Diagram source (Mermaid)</summary><pre><code>${esc(b.join("\n"))}</code></pre></details></div>`);
      else out.push(`<pre${cls(start, i)}><code>${esc(b.join("\n"))}</code></pre>`); continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { const lv = Math.min(3, m[1].length); if (lv === 2) sec = m[2]; out.push(`<h${lv}${cls(i, i + 1)} data-sec="${esc(m[2])}">${inline(m[2])}</h${lv}>`); i++; continue; }
    if (/^\s*\|/.test(l)) { const rows = []; while (i < L.length && /^\s*\|/.test(L[i])) rows.push(L[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
      const head = cells(rows[0]), body = rows.slice(/^\s*\|?\s*:?-/.test(rows[1] || "") ? 2 : 1).map(cells);
      out.push(`<div class="tw"><table${cls(start, i)} data-sec="${esc(sec)}"><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${body.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>`); continue; }
    if (/^\s*[-*+] /.test(l) || /^\s*\d+[.)] /.test(l)) { const ordered = /^\s*\d/.test(l), items = [];
      while (i < L.length && (/^\s*[-*+] /.test(L[i]) || /^\s*\d+[.)] /.test(L[i]))) { items.push([i, L[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "")]); i++; }
      const li = items.map(([k, t]) => { const tm = t.match(/^\[( |x|X)\]\s+(.*)/);
        return tm ? `<li class="task${add.has(k) ? " added" : ""}"><input type="checkbox" ${tm[1] !== " " ? "checked" : ""} disabled>${inline(tm[2])}</li>` : `<li${add.has(k) ? ' class="added"' : ""}>${inline(t)}</li>`; });
      out.push(`<${ordered ? "ol" : "ul"} data-sec="${esc(sec)}">${li.join("")}</${ordered ? "ol" : "ul"}>`); continue; }
    if (/^>\s?/.test(l)) { const q = []; while (i < L.length && /^>\s?/.test(L[i])) q.push(L[i++].replace(/^>\s?/, "")); out.push(`<blockquote${cls(start, i)}>${inline(q.join(" "))}</blockquote>`); continue; }
    if (l.trim()) { const p = []; while (i < L.length && L[i].trim() && !/^(#|```|\s*\||\s*[-*+] |\s*\d+[.)] |>)/.test(L[i])) p.push(L[i++]);
      const kv = /^[A-Z][\w ]{0,24}:\s+\S/;
      if (p.every(x => kv.test(x))) { out.push(`<div class="meta${cls(start, i) ? " added" : ""}">${p.map(x => { const k = x.indexOf(":");
        return `<span>${esc(x.slice(0, k))}: <b>${inline(x.slice(k + 1).trim().replace(/\s+$/, ""))}</b></span>`; }).join("")}</div>`); continue; }
      out.push(`<p${cls(start, i)} data-sec="${esc(sec)}">${inline(p.join(" "))}</p>`); continue; }
    i++;
  }
  return out.join("\n");
}

/* ---------- list ---------- */
function badge(a) {
  const s = (a.status || "").toLowerCase();
  if (a.needs) return `<span class="badge wait">${s === "proposed" ? "review" : "you"}</span>`;
  if (/accepted|approved|done|planned/.test(s)) return `<span class="badge ok">${esc(s)}</span>`;
  return s ? `<span class="badge none">${esc(s)}</span>` : "";
}
function renderRail() {
  const g = [["Needs you", S.list.filter(a => a.needs)], ["Decided", S.list.filter(a => !a.needs && /accepted|approved|planned|done/.test(a.status))],
             ["Other", S.list.filter(a => !a.needs && !/accepted|approved|planned|done/.test(a.status))]];
  $("rail").innerHTML = S.list.length ? g.filter(x => x[1].length).map(([t, xs]) => `<h4>${t}</h4>` + xs.map(a =>
    `<button class="art ${S.cur === a.id ? "on" : ""}" data-id="${esc(a.id)}"><span><b>${esc(a.name)}</b><small>${esc(a.path)}</small></span>${badge(a)}</button>`).join("")).join("")
    : '<div class="empty">Nothing to review yet. Documents appear here as the team writes them.</div>';
  $("rail").querySelectorAll(".art").forEach(b => b.onclick = () => open(b.dataset.id));
}
async function loadList() {
  const d = await api("api/review"); if (d.error) return;
  S.list = d.artifacts || []; renderRail();
  if (!S.cur && S.list.length) { const want = decodeURIComponent(location.hash.slice(1)); open(S.list.find(a => a.id === want) ? want : (S.list.find(a => a.needs) || S.list[0]).id); }
}

/* ---------- document ---------- */
async function open(id, ver) {
  if (S.cur !== id) { S.chatN = -1; $("msgs").innerHTML = ""; clearQuote(); }
  S.cur = id; S.ver = ver || ""; history.replaceState(null, "", "#" + encodeURIComponent(id));
  const d = await api("api/review/doc?id=" + encodeURIComponent(id) + (ver ? "&ver=" + encodeURIComponent(ver) : ""));
  if (d.error) { $("paper").innerHTML = `<p>${esc(d.error)}</p>`; return; }
  const keep = S.doc && S.doc.id === id ? $("scroll").scrollTop : 0;
  S.doc = d; renderRail();
  $("path").textContent = d.path;
  $("vers").innerHTML = (d.versions || []).length > 1 ? d.versions.map((v, i) => `<button class="ver ${v.id === d.version ? "on" : ""}" data-v="${esc(v.id)}" title="${esc(v.label)} · ${esc(v.date)}">v${i + 1}${v.id === "working" ? " · latest" : ""}</button>`).join("") : "";
  $("vers").querySelectorAll(".ver").forEach(b => b.onclick = () => open(id, b.dataset.v));
  const st = $("status"), s = (d.status || "").toLowerCase();
  st.className = "chip " + (d.needs ? "" : /accepted|approved|done|planned/.test(s) ? "ok" : "none");
  st.querySelector("span").textContent = d.needs ? "Waiting for you" : (s ? s[0].toUpperCase() + s.slice(1) : "—");
  $("paper").innerHTML = (d.mockup ? `<iframe class="mock" sandbox="allow-scripts" src="${esc(d.mockup)}" title="Mockup"></iframe>` : "") + md(d.content || "", d.added, d.diagram);
  $("scroll").scrollTop = keep;
  const A = AG[d.agent] || AG.lead;
  $("av").className = "av av-" + (AG[d.agent] ? d.agent : "lead"); $("av").textContent = A[1]; $("agentname").textContent = A[0];
  $("agentsub").textContent = d.editable ? "can revise this while it waits for you" : "answers questions about this document";
  $("hint").textContent = d.editable ? "Select text to quote it. Ask for a change and the architect edits it; compare versions above." : "Select text in the document to quote it.";
  renderGate(); renderSuggest(); pollChat(true);
}
function renderGate() {
  const d = S.doc, g = $("gate"); g.className = "gate"; g.hidden = !d.needs;
  if (!d.needs) return;
  if (d.kind === "architecture" || d.kind === "adr") g.innerHTML = `<p><b>Approve this architecture?</b> Nothing is planned until you decide. Ask the architect below — it can revise this now — or send changes for the team run.</p>
    <textarea id="gtext" placeholder="Note, or the changes you want (needed for Request changes)"></textarea>
    <div class="row"><button class="btn primary" data-a="approve">✓ Approve</button><button class="btn" data-a="changes">Request changes</button></div>`;
  else if (d.kind === "design") g.innerHTML = `<p><b>Approve design ${esc(d.key)}?</b> Developers start on screens after you approve.</p>
    <textarea id="gtext" placeholder="Note, or the changes you want (needed for Request changes)"></textarea>
    <div class="row"><button class="btn primary" data-a="approve">✓ Approve</button><button class="btn" data-a="changes">Request changes</button></div>`;
  else if (d.kind === "escalation") g.innerHTML = `<p><b>${esc(d.key)} needs your guidance.</b> Tell the team how to continue; the task resumes right away.</p>
    <textarea id="gtext" placeholder="Your guidance for the team"></textarea><div class="row"><button class="btn primary" data-a="resume">Resume with this guidance</button></div>`;
  else { g.hidden = true; return; }
  g.querySelectorAll("[data-a]").forEach(b => b.onclick = () => decide(b.dataset.a));
}
async function decide(action) {
  const t = ($("gtext") && $("gtext").value.trim()) || "";
  if ((action === "changes" || action === "resume") && !t) { $("gtext").focus(); return; }
  $("gate").querySelectorAll("button").forEach(b => b.disabled = true);
  const r = await api("api/review/decide", { id: S.cur, action, text: t });
  if (r.error) { $("gate").classList.add("err"); $("gate").querySelector("p").textContent = r.error; $("gate").querySelectorAll("button").forEach(b => b.disabled = false); return; }
  $("gate").className = "gate done"; $("gate").innerHTML = `<p><b>${action === "approve" ? "✓ Approved." : action === "resume" ? "✓ Resumed." : "✓ Sent to the agent."}</b> The board updates in a moment.</p>`;
  setTimeout(async () => { await loadList(); open(S.cur); }, 900);
}
function renderSuggest() { const k = S.doc.kind in SUGGEST ? S.doc.kind : "doc";
  $("suggest").innerHTML = SUGGEST[k].map(s => `<button class="sg">${esc(s)}</button>`).join("");
  $("suggest").querySelectorAll(".sg").forEach(b => b.onclick = () => send(b.textContent)); }

/* ---------- chat ---------- */
function msgHTML(m) {
  if (m.who === "system") return `<div class="sys ${m.decision === "approve" || m.decision === "resume" ? "ok" : ""} ${m.changed ? "changed" : ""}" ${m.changed ? 'data-latest="1"' : ""}>${esc(m.text)}${m.changed ? " · show changes" : ""}</div>`;
  const me = m.who === "you", A = AG[m.who] || AG.lead;
  return `<div class="msg ${me ? "me" : ""}"><div class="av av-${me ? "you" : (AG[m.who] ? m.who : "lead")}">${me ? "YOU" : A[1]}</div>
    <div><div class="who">${me ? "You" : A[0]} · ${new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}${m.cost ? " · $" + m.cost.toFixed(2) : ""}</div>
    <div class="bubble ${m.error ? "err" : ""}">${m.quote ? `<div class="quote">“${esc(m.quote.slice(0, 160))}${m.quote.length > 160 ? "…" : ""}”${m.section ? " — " + esc(m.section) : ""}</div>` : ""}${esc(m.text)}</div></div></div>`;
}
async function pollChat(force) {
  if (!S.cur) return;
  const id = S.cur, d = await api("api/review/chat?id=" + encodeURIComponent(id)); if (d.error || id !== S.cur) return;
  const n = d.messages.length + (d.pending ? 1 : 0);
  if (force || n !== S.chatN || d.pending !== S.pending) {
    const grew = d.messages.length > Math.max(0, S.chatN - (S.pending ? 1 : 0)), hadPending = S.pending;
    S.chatN = n; S.pending = d.pending;
    $("msgs").innerHTML = (d.messages.length ? "" : `<div class="sys">Ask the ${esc((AG[S.doc.agent] || AG.lead)[0].toLowerCase())} anything about this document.</div>`)
      + d.messages.map(msgHTML).join("") + (d.pending ? `<div class="msg"><div class="av av-${AG[S.doc.agent] ? S.doc.agent : "lead"}">${(AG[S.doc.agent] || AG.lead)[1]}</div><div class="bubble"><span class="typing"><span></span><span></span><span></span></span></div></div>` : "");
    $("msgs").querySelectorAll("[data-latest]").forEach(x => x.onclick = () => open(S.cur));
    $("msgs").scrollTop = 1e9;
    if (hadPending && !d.pending && d.messages.some(m => m.changed)) { loadList(); open(S.cur); }
    $("send").disabled = d.pending;
  }
}
async function send(text) {
  text = (text ?? $("input").value).trim(); if (!text || S.sending || S.pending) return;
  S.sending = true; const q = S.quote;
  const r = await api("api/review/chat", { id: S.cur, text, quote: q ? q.text : "", section: q ? q.sec : "" });
  S.sending = false;
  if (r.error) { $("hint").textContent = r.error; return; }
  $("input").value = ""; $("input").style.height = "auto"; clearQuote(); pollChat(true);
}
function clearQuote() { S.quote = null; $("ctx").style.display = "none"; }

/* ---------- select text → ask about it ---------- */
$("scroll").addEventListener("mouseup", () => {
  const sel = window.getSelection(), txt = sel.toString().trim(), ask = $("ask");
  if (!txt || txt.length < 3 || !sel.rangeCount) { ask.style.display = "none"; return; }
  const r = sel.getRangeAt(0).getBoundingClientRect(), box = $("scroll").getBoundingClientRect();
  ask.style.left = Math.max(8, r.left - box.left + r.width / 2 - 60) + "px"; ask.style.top = (r.top - box.top + $("scroll").scrollTop - 40) + "px"; ask.style.display = "block";
  let n = sel.anchorNode; n = n && (n.nodeType === 3 ? n.parentElement : n); const h = n && n.closest("[data-sec]");
  ask.onclick = () => { S.quote = { text: txt.slice(0, 600), sec: h ? h.dataset.sec : "" }; $("ctxsec").textContent = S.quote.sec || "document";
    $("ctxtext").textContent = "“" + txt.slice(0, 90) + (txt.length > 90 ? "…" : "") + "”"; $("ctx").style.display = "block"; ask.style.display = "none";
    sel.removeAllRanges(); $("input").focus(); };
});
$("ctxx").onclick = clearQuote;
$("send").onclick = () => send();
$("input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
$("input").addEventListener("input", e => { e.target.style.height = "auto"; e.target.style.height = Math.min(140, e.target.scrollHeight) + "px"; });
window.addEventListener("hashchange", () => { const id = decodeURIComponent(location.hash.slice(1)); if (id && id !== S.cur) open(id); });

loadList(); setInterval(loadList, 4000); setInterval(() => pollChat(false), 1500);
</script>
</body>
</html>
"""
