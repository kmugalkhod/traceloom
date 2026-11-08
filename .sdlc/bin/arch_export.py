#!/usr/bin/env python3
"""Turn the Mermaid diagram in sdlc/ARCHITECTURE.md into a picture people can read and a diagram they can edit:

  architecture.svg     — the reviewed picture: a title, colour by role, a legend; opens in any browser
  architecture.png     — the same picture as PNG (needs Python Playwright, or falls back to the Mermaid CLI)
  architecture.drawio  — the same layout, editable in diagrams.net / the draw.io VS Code extension

  python .sdlc/bin/arch_export.py [ARCHITECTURE.md] [--out DIR] [--no-png] [--layout auto|dot|builtin]

Layout: Graphviz `dot` when it is installed (best: few crossings, routed edges — `winget install graphviz`,
`brew install graphviz`, `apt install graphviz`), otherwise a built-in layered layout.

Supported Mermaid (what the architect agent is told to use): `flowchart LR|TD|TB|RL|BT`, `subgraph id [Title]` ...
`end` (nested too), nodes id[box] id(rounded) id[(store)] id[/queue/] id((external)) id{decision} id{{job}}
id>flag], edges --> --- -.-> ==> with labels `-- text -->` or `-->|text|`, chains `a --> b --> c` and `a & b --> c`.
Other lines (classDef, style, click, %% comments) are ignored. Unknown syntax is reported, never guessed.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SHAPES = [  # (open, close, kind) — longest delimiters first
    ("[(", ")]", "store"), ("((", "))", "circle"), ("{{", "}}", "hex"), ("[/", "/]", "queue"),
    ("[[", "]]", "box"), ("[", "]", "box"), ("(", ")", "round"), ("{", "}", "decision"), (">", "]", "flag"),
]
# role → (fill, stroke, legend text). One quiet palette: the colour says what kind of thing it is.
ROLE = {
    "circle": ("#F1F5F9", "#64748B", "User or external system"),
    "box": ("#EFF6FF", "#2563EB", "Service / component"),
    "round": ("#EFF6FF", "#2563EB", "Service / component"),
    "hex": ("#ECFDF5", "#059669", "Background job"),
    "store": ("#FFFBEB", "#D97706", "Data store"),
    "queue": ("#F5F3FF", "#7C3AED", "Queue / stream"),
    "decision": ("#FEF2F2", "#DC2626", "Decision"),
    "flag": ("#F8FAFC", "#475569", "Note"),
}
DRAWIO_SHAPE = {
    "box": "rounded=1;arcSize=8;", "round": "rounded=1;arcSize=20;", "circle": "rounded=1;arcSize=50;",
    "store": "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=10;",
    "queue": "shape=parallelogram;perimeter=parallelogramPerimeter;fixedSize=1;size=14;",
    "decision": "rhombus;", "hex": "shape=hexagon;perimeter=hexagonPerimeter2;fixedSize=1;size=14;",
    "flag": "shape=step;perimeter=stepPerimeter;fixedSize=1;size=12;",
}
INK, MUTED, EDGE, FONT = "#0F172A", "#475569", "#64748B", "Inter, Segoe UI, Helvetica, Arial, sans-serif"
ID = r"[A-Za-z_][\w-]*"
ARROW = re.compile(r"\s*(?:--\s*\"?([^\"|>-][^\"|>]*?)\"?\s*(-->|---)|-\.\s*\"?([^\"|>.][^\"|>]*?)\"?\s*\.->|"
                   r"==\s*\"?([^\"|>=][^\"|>]*?)\"?\s*==>|(-\.->|-->|---|==>))(?:\|\"?([^|\"]*)\"?\|)?\s*")
WRAP, CHAR_W, LINE_H = 20, 7.6, 17  # label wrap width (chars), px per char at 13px, px per line


def mermaid_block(md: str) -> str:
    m = re.search(r"```mermaid\s*\n(.*?)```", md, re.S)
    if not m:
        raise SystemExit("no ```mermaid block found")
    return m.group(1)


def node_at(s: str, i: int, nodes: dict, group: str | None) -> tuple[str, int]:
    """Parse `id`, `id[label]` etc. starting at s[i]; register it; return (id, new index)."""
    m = re.compile(ID).match(s, i)
    if not m:
        raise ValueError(f"expected a node id at: {s[i:i + 30]!r}")
    nid, i = m.group(0), m.end()
    if group and nid in nodes and not nodes[nid]["group"]:
        nodes[nid]["group"] = group  # like Mermaid: a node mentioned inside a subgraph belongs to it
    for op, cl, kind in SHAPES:
        if s.startswith(op, i):
            j = s.find(cl, i + len(op))
            if j < 0:
                raise ValueError(f"unclosed {op} for {nid}")
            label = s[i + len(op):j].strip().strip('"')
            n = nodes.setdefault(nid, {"label": nid, "kind": "box", "group": group})
            n.update(label=label, kind=kind)
            return nid, j + len(cl)
    nodes.setdefault(nid, {"label": nid, "kind": "box", "group": group})
    return nid, i


def node_list(s: str, i: int, nodes: dict, group: str | None) -> tuple[list, int]:
    ids = []
    while True:
        nid, i = node_at(s, i, nodes, group)
        ids.append(nid)
        m = re.compile(r"\s*&\s*").match(s, i)
        if not m:
            return ids, i
        i = m.end()


def parse(src: str) -> dict:
    nodes, edges, groups, stack, problems = {}, [], {}, [], []
    direction = "LR"
    for ln, raw in enumerate(src.splitlines(), 1):
        line = raw.split("%%")[0].strip().rstrip(";")
        if not line:
            continue
        if m := re.match(r"^(?:flowchart|graph)\s+(LR|RL|TD|TB|BT)\b", line):
            direction = m.group(1)
            continue
        if re.match(r"^(classDef|class|style|linkStyle|click|direction)\b", line):
            continue
        if m := re.match(rf"^subgraph\s+({ID})?\s*(?:\[\"?(.*?)\"?\]|\"(.*?)\"|(.*))?$", line):
            gid = m.group(1) or f"g{len(groups)}"
            title = (m.group(2) or m.group(3) or m.group(4) or gid).strip() or gid
            groups[gid] = {"title": title, "parent": stack[-1] if stack else None}
            stack.append(gid)
            continue
        if line == "end":
            if stack:
                stack.pop()
            continue
        try:
            group = stack[-1] if stack else None
            left, i = node_list(line, 0, nodes, group)
            while i < len(line):
                m = ARROW.match(line, i)
                if not m:
                    raise ValueError(f"unexpected text {line[i:i + 30]!r}")
                kind = m.group(2) or (m.group(3) is not None and "-.->") or (m.group(4) is not None and "==>") \
                    or m.group(5)
                label = (m.group(1) or m.group(3) or m.group(4) or m.group(6) or "").strip()
                right, i = node_list(line, m.end(), nodes, group)
                edges += [(a, b, kind, label) for a in left for b in right]
                left = right
        except ValueError as e:
            problems.append(f"line {ln}: {e}")
    return {"direction": direction, "nodes": nodes, "edges": edges, "groups": groups, "problems": problems}


# ---------------------------------------------------------------- sizes
def wrap(label: str) -> list:
    words, lines, cur = label.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > WRAP:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur or not lines else lines


def size(n: dict) -> tuple:
    lines = wrap(n["label"])
    w = max(124, max(len(x) for x in lines) * CHAR_W + 40)
    h = max(54, len(lines) * LINE_H + 30)
    k = n["kind"]
    if k == "store":
        h += 16
    elif k in ("queue", "hex", "flag"):
        w += 28
    elif k == "circle":
        w += 12
    elif k == "decision":
        w, h = w * 1.35, h * 1.5
    return round(w), round(h)


def chain(g: dict, gid: str | None) -> list:
    """Outermost-first list of the group and its ancestors."""
    out = []
    while gid:
        out.insert(0, gid)
        gid = g["groups"].get(gid, {}).get("parent")
    return out


def depth(g: dict, gid: str) -> int:
    return len(chain(g, gid)) - 1


# ---------------------------------------------------------------- layout: Graphviz
def layout_dot(g: dict, dims: dict) -> dict | None:
    dot = shutil.which("dot")
    if not dot:
        return None
    q = lambda s: '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
    rank = {"LR": "LR", "RL": "RL", "TD": "TB", "TB": "TB", "BT": "BT"}[g["direction"]]
    out = [f"digraph G {{ rankdir={rank}; nodesep=0.5; ranksep=0.85; splines=spline; newrank=true;",
           'node [shape=box, fixedsize=true, label=""]; edge [fontname="Helvetica", fontsize=11, arrowsize=0.8];']

    def emit(gid: str | None, ind: str) -> None:
        for n, v in g["nodes"].items():
            if v["group"] == gid:
                out.append(f"{ind}{q(n)} [width={dims[n][0] / 72:.3f}, height={dims[n][1] / 72:.3f}];")
        for k, v in g["groups"].items():
            if v["parent"] == gid:
                out.append(f'{ind}subgraph {q("cluster_" + k)} {{ label={q(v["title"])}; fontname="Helvetica"; '
                           f'fontsize=13; labeljust=l; margin=20;')
                emit(k, ind + "  ")
                out.append(ind + "}")

    emit(None, "  ")
    for k, (a, b, kind, label) in enumerate(g["edges"]):
        attrs = [f'id="e{k}"'] + ([f"label={q(label)}"] if label else []) + (["dir=none"] if kind == "---" else [])
        out.append(f"  {q(a)} -> {q(b)} [{', '.join(attrs)}];")
    out.append("}")
    try:
        r = subprocess.run([dot, "-Tjson"], input="\n".join(out), capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    x1, y1, x2, y2 = map(float, data["bb"].split(","))
    fy = lambda y: y2 - float(y)
    pt = lambda s: (float(s.split(",")[0]) - x1, fy(s.split(",")[1]))
    objs = data.get("objects", [])
    L = {"w": x2 - x1, "h": y2 - y1, "nodes": {}, "groups": {}, "edges": [], "engine": "Graphviz"}
    for o in objs:
        if o["name"].startswith("cluster_") and "bb" in o:
            a, b, c, d = map(float, o["bb"].split(","))
            L["groups"][o["name"][8:]] = (a - x1, fy(d), c - a, d - b)
        elif o["name"] in g["nodes"] and "pos" in o:
            cx, cy = pt(o["pos"])
            w, h = dims[o["name"]]
            L["nodes"][o["name"]] = (cx - w / 2, cy - h / 2, w, h)
    found = {e.get("id"): e for e in data.get("edges", [])}
    if len(found) != len(g["edges"]):
        return None
    for k, (a, b, kind, label) in enumerate(g["edges"]):
        e = found.get(f"e{k}")
        if e is None:
            return None
        toks = e.get("pos", "").split(";")[0].split()
        end = start = None
        pts = []
        for t in toks:
            if t.startswith("e,"):
                end = pt(t[2:])
            elif t.startswith("s,"):
                start = pt(t[2:])
            else:
                pts.append(pt(t))
        L["edges"].append({"kind": kind, "label": label, "bezier": pts, "start": start, "end": end,
                           "lp": pt(e["lp"]) if e.get("lp") else None})
    return L


# ---------------------------------------------------------------- layout: built-in (no Graphviz)
def ranks(g: dict) -> dict:
    """Longest-path layering; cycles broken by visiting order."""
    rank, state = {}, {}
    preds = {n: [a for a, b, *_ in g["edges"] if b == n and a != n] for n in g["nodes"]}

    def visit(n: str) -> int:
        if n in rank:
            return rank[n]
        state[n] = 1
        r = 0
        for p in preds[n]:
            if state.get(p) != 1:
                r = max(r, visit(p) + 1)
        state[n] = 2
        rank[n] = r
        return r

    for n in g["nodes"]:
        visit(n)
    base = {}
    for n in g["nodes"]:  # a source sits just before the first thing it feeds, not always in column 0
        succ = [rank[b] for a, b, *_ in g["edges"] if a == n and b != n]
        if not preds[n] and succ:
            base[n] = max(0, min(succ) - 1)
    rank, state = {}, {}

    def visit2(n: str) -> int:
        if n in rank:
            return rank[n]
        state[n] = 1
        r = base.get(n, 0)
        for p in preds[n]:
            if state.get(p) != 1:
                r = max(r, visit2(p) + 1)
        state[n] = 2
        rank[n] = r
        return r

    for n in g["nodes"]:
        visit2(n)
    return rank


def layout_builtin(g: dict, dims: dict) -> dict:
    rank, order = ranks(g), list(g["nodes"])
    path = lambda n: tuple(chain(g, g["nodes"][n]["group"]))
    bands = sorted({path(n) for n in order if path(n)})  # one band of rows per group; loose nodes come after
    slot = {n: i for i, n in enumerate(order)}
    nb = {n: [b for a, b, *_ in g["edges"] if a == n] + [a for a, b, *_ in g["edges"] if b == n] for n in order}
    for _ in range(8):  # barycenter sweeps: fewer crossings inside each band
        for n in sorted(order, key=lambda n: rank[n]):
            if nb[n]:
                slot[n] = sum(slot[m] for m in nb[n]) / len(nb[n]) * 0.6 + slot[n] * 0.4
    horizontal = g["direction"] in ("LR", "RL")
    along = lambda n: dims[n][0] if horizontal else dims[n][1]
    across = lambda n: dims[n][1] if horizontal else dims[n][0]
    step_a = max(along(n) for n in order) + 110
    step_c = max(across(n) for n in order) + 46
    xy = lambda a, c, n: (a, c, *dims[n]) if horizontal else (c, a, *dims[n])
    col_at = lambda n: rank[n] * step_a + (step_a - 110 - along(n)) / 2
    L = {"nodes": {}, "groups": {}, "edges": [], "engine": "built-in"}
    cross = 0.0
    for p in bands:
        members = [n for n in order if path(n) == p]
        rows = 0
        for r in sorted({rank[n] for n in members}):
            col = sorted([n for n in members if rank[n] == r], key=lambda n: slot[n])
            for k, n in enumerate(col):
                L["nodes"][n] = xy(col_at(n), cross + 44 * len(p) + k * step_c + (step_c - 46 - across(n)) / 2, n)
            rows = max(rows, len(col))
        cross += rows * step_c + 44 * len(p) + 70
    for gid in sorted(g["groups"], key=lambda k: -depth(g, k)):  # innermost first
        boxes = [L["nodes"][n] for n, v in g["nodes"].items() if v["group"] == gid]
        boxes += [L["groups"][k] for k, v in g["groups"].items() if v["parent"] == gid and k in L["groups"]]
        if boxes:
            x, y = min(b[0] for b in boxes) - 22, min(b[1] for b in boxes) - 40
            L["groups"][gid] = (x, y, max(b[0] + b[2] for b in boxes) + 22 - x, max(b[1] + b[3] for b in boxes) + 22 - y)
    # loose nodes (users, external systems, shared stores): in their column, as close as possible to what they
    # connect to, and never inside a group they don't belong to
    hit = lambda b, o, m=18: not (b[0] + b[2] + m <= o[0] or o[0] + o[2] + m <= b[0] or b[1] + b[3] + m <= o[1]
                                  or o[1] + o[3] + m <= b[1])
    mid_c = lambda b: (b[1] + b[3] / 2) if horizontal else (b[0] + b[2] / 2)
    for n in sorted([n for n in order if not path(n)], key=lambda n: (rank[n], slot[n])):
        near = [mid_c(L["nodes"][m]) for m in nb[n] if m in L["nodes"]]
        want = (sum(near) / len(near) if near else 0) - across(n) / 2
        for d in sorted(range(-60, 61), key=abs):
            b = xy(col_at(n), want + d * step_c / 4, n)
            if not any(hit(b, o) for o in list(L["nodes"].values()) + list(L["groups"].values())):
                L["nodes"][n] = b
                break
    if g["direction"] in ("RL", "BT"):
        allb = list(L["nodes"].values()) + list(L["groups"].values())
        far = max((b[0] + b[2]) if horizontal else (b[1] + b[3]) for b in allb)
        flip = lambda b: (far - b[0] - b[2], b[1], b[2], b[3]) if horizontal else (b[0], far - b[1] - b[3], b[2], b[3])
        L["nodes"] = {n: flip(b) for n, b in L["nodes"].items()}
        L["groups"] = {k: flip(b) for k, b in L["groups"].items()}
    allb = list(L["nodes"].values()) + list(L["groups"].values())
    ox, oy = -min(b[0] for b in allb), -min(b[1] for b in allb)
    move = lambda b: (b[0] + ox, b[1] + oy, b[2], b[3])
    L["nodes"] = {n: move(b) for n, b in L["nodes"].items()}
    L["groups"] = {k: move(b) for k, b in L["groups"].items()}
    L["w"] = max(b[0] + b[2] for b in allb) + ox
    L["h"] = max(b[1] + b[3] for b in allb) + oy
    for a, b, kind, label in g["edges"]:
        ax, ay, aw, ah = L["nodes"][a]
        bx, by, bw, bh = L["nodes"][b]
        if horizontal:
            fwd = bx >= ax + aw
            s = (ax + aw, ay + ah / 2) if fwd else (ax + aw / 2, ay + ah)
            e = (bx, by + bh / 2) if fwd else (bx + bw / 2, by + bh)
            d = max(40, abs(e[0] - s[0]) / 2)
            c1, c2 = ((s[0] + d, s[1]), (e[0] - d, e[1])) if fwd else ((s[0], s[1] + 60), (e[0], e[1] + 60))
        else:
            fwd = by >= ay + ah
            s = (ax + aw / 2, ay + ah) if fwd else (ax + aw, ay + ah / 2)
            e = (bx + bw / 2, by) if fwd else (bx + bw, by + bh / 2)
            d = max(30, abs(e[1] - s[1]) / 2)
            c1, c2 = ((s[0], s[1] + d), (e[0], e[1] - d)) if fwd else ((s[0] + 60, s[1]), (e[0] + 60, e[1]))
        mid = tuple(0.125 * s[i] + 0.375 * c1[i] + 0.375 * c2[i] + 0.125 * e[i] for i in (0, 1))
        L["edges"].append({"kind": kind, "label": label, "bezier": [s, c1, c2, e], "start": None,
                           "end": None, "lp": mid})
    return L


# ---------------------------------------------------------------- SVG (the reviewed picture)
def node_svg(n: dict, box: tuple) -> str:
    x, y, w, h = box
    fill, stroke, _ = ROLE[n["kind"]]
    k, a = n["kind"], f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"'
    if k == "store":
        ry = 8
        shape = (f'<path d="M{x},{y + ry} a{w / 2},{ry} 0 0 0 {w},0 v{h - 2 * ry} a{w / 2},{ry} 0 0 1 {-w},0 z" {a}/>'
                 f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" {a}/>')
        ty = y + ry + (h - ry) / 2
    else:
        s = 14
        poly = {"queue": [(x + s, y), (x + w, y), (x + w - s, y + h), (x, y + h)],
                "hex": [(x + s, y), (x + w - s, y), (x + w, y + h / 2), (x + w - s, y + h), (x + s, y + h), (x, y + h / 2)],
                "decision": [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)],
                "flag": [(x, y), (x + w - s, y), (x + w, y + h / 2), (x + w - s, y + h), (x, y + h)]}.get(k)
        if poly:
            shape = f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in poly)}" {a}/>'
        else:
            r = {"circle": h / 2, "round": 14}.get(k, 8)
            shape = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" {a}/>'
        ty = y + h / 2
    lines = wrap(n["label"])
    t0 = ty - (len(lines) - 1) * LINE_H / 2
    text = "".join(f'<text x="{x + w / 2}" y="{t0 + i * LINE_H}" class="n">{html.escape(t)}</text>'
                   for i, t in enumerate(lines))
    return f'<g filter="url(#sh)">{shape}</g>{text}'


def edge_path(e: dict) -> str:
    p = e["bezier"]
    if not p:
        return ""
    d = f"M{p[0][0]:.1f},{p[0][1]:.1f}"
    for i in range(1, len(p) - 2, 3):
        d += " C" + " ".join(f"{x:.1f},{y:.1f}" for x, y in p[i:i + 3])
    if e.get("end"):
        d += f" L{e['end'][0]:.1f},{e['end'][1]:.1f}"
    return d


def svg(g: dict, L: dict, title: str, caption: str) -> tuple:
    M, TOP = 36, 78
    legend = []
    for k in ROLE:
        if any(v["kind"] == k for v in g["nodes"].values()) and ROLE[k][2] not in [x[1] for x in legend]:
            legend.append((k, ROLE[k][2]))
    kinds = {e[2] for e in g["edges"]}
    lines = [(k, t) for k, t in (("-->", "Request / call"), ("-.->", "Async or optional"), ("==>", "Main data flow"))
             if k in kinds]
    W = max(L["w"] + 2 * M, 640)
    items, lx, ly = [], M, TOP + L["h"] + 40
    for kind, text, is_line in [(k, t, False) for k, t in legend] + [(k, t, True) for k, t in lines]:
        need = (34 if is_line else 30) + len(text) * 6.8
        if lx + need > W - M and lx > M:
            lx, ly = M, ly + 28
        items.append((kind, text, is_line, lx, ly))
        lx += need + 26
    H = ly + 34 + M
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">',
         f'<style>text{{font-family:{FONT}}} .n{{font-size:13px;font-weight:600;fill:{INK};text-anchor:middle;'
         'dominant-baseline:central}} .g{font-size:12px;font-weight:700;letter-spacing:.06em;fill:#475569} '
         f'.l{{font-size:11px;fill:{MUTED};text-anchor:middle;dominant-baseline:central}} '
         f'.k{{font-size:12px;fill:{MUTED};dominant-baseline:central}}</style>',
         '<defs><filter id="sh" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="1.5" '
         'stdDeviation="1.5" flood-color="#0F172A" flood-opacity=".10"/></filter>',
         f'<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
         f'<path d="M0,0 L10,5 L0,10 z" fill="{EDGE}"/></marker></defs>',
         f'<rect width="100%" height="100%" fill="#FFFFFF"/>',
         f'<text x="{M}" y="38" style="font-size:20px;font-weight:700;fill:{INK}">{html.escape(title)}</text>',
         f'<text x="{M}" y="60" style="font-size:12px;fill:{MUTED}">{html.escape(caption)}</text>',
         f'<g transform="translate({M},{TOP})">']
    for gid in sorted(L["groups"], key=lambda k: depth(g, k)):
        x, y, w, h = L["groups"][gid]
        top = depth(g, gid) == 0
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{14 if top else 10}" '
                 f'fill="{"#F8FAFC" if top else "#FFFFFF"}" stroke="#CBD5E1" stroke-width="1.2"'
                 f'{"" if top else " stroke-dasharray=\"5 4\""}/>')
    titles = []
    for gid in sorted(L["groups"], key=lambda k: depth(g, k)):
        x, y, w, h = L["groups"][gid]
        t = g["groups"][gid]["title"].upper()
        titles.append(f'<rect x="{x + 8}" y="{y + 7}" width="{len(t) * 8.2 + 14:.0f}" height="22" rx="6" '
                      f'fill="{"#F8FAFC" if depth(g, gid) == 0 else "#FFFFFF"}"/>'
                      f'<text x="{x + 15}" y="{y + 18}" class="g" dominant-baseline="central">{html.escape(t)}</text>')
    for e in L["edges"]:
        dash = ' stroke-dasharray="6 5"' if e["kind"] == "-.->" else ""
        wdt = 2.8 if e["kind"] == "==>" else 1.6
        mk = "" if e["kind"] == "---" else ' marker-end="url(#ar)"'
        o.append(f'<path d="{edge_path(e)}" fill="none" stroke="{EDGE}" stroke-width="{wdt}"{dash}{mk}/>')
    for e in L["edges"]:
        if e["label"] and e["lp"]:
            x, y = e["lp"]
            tw = len(e["label"]) * 6.4 + 14
            o.append(f'<rect x="{x - tw / 2:.1f}" y="{y - 10:.1f}" width="{tw:.1f}" height="20" rx="10" fill="#FFFFFF" '
                     f'stroke="#E2E8F0"/><text x="{x:.1f}" y="{y:.1f}" class="l">{html.escape(e["label"])}</text>')
    o += titles
    for n, v in g["nodes"].items():
        o.append(node_svg(v, L["nodes"][n]))
    o.append("</g>")
    for k, text, is_line, lx, ly in items:
        if is_line:
            dash = ' stroke-dasharray="6 5"' if k == "-.->" else ""
            o.append(f'<path d="M{lx},{ly} h26" stroke="{EDGE}" stroke-width="{2.8 if k == "==>" else 1.6}"{dash} '
                     f'marker-end="url(#ar)"/><text x="{lx + 34}" y="{ly}" class="k">{html.escape(text)}</text>')
        else:
            fill, stroke, _ = ROLE[k]
            o.append(f'<rect x="{lx}" y="{ly - 8}" width="22" height="16" rx="{8 if k == "circle" else 3}" '
                     f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>'
                     f'<text x="{lx + 30}" y="{ly}" class="k">{html.escape(text)}</text>')
    o.append("</svg>")
    return "\n".join(o), W, H


# ---------------------------------------------------------------- draw.io (same layout, editable)
def drawio(g: dict, L: dict, title: str) -> str:
    esc = lambda s: html.escape(s, quote=True)
    OX, OY = 20, 70
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>',
             f'<mxCell id="title" value="{esc(title)}" style="text;html=1;fontSize=20;fontStyle=1;fontColor={INK};'
             'align=left;verticalAlign=middle;" vertex="1" parent="1"><mxGeometry x="20" y="16" width="600" '
             'height="34" as="geometry"/></mxCell>']
    absb = {}
    for gid in sorted(L["groups"], key=lambda k: depth(g, k)):  # parents first: draw.io needs the parent cell first
        x, y, w, h = L["groups"][gid]
        par = g["groups"][gid]["parent"]
        px, py = absb.get(par, (-OX, -OY))[:2]
        absb[gid] = (x, y)
        top = depth(g, gid) == 0
        cells.append(f'<mxCell id="grp_{esc(gid)}" value="{esc(g["groups"][gid]["title"].upper())}" style="rounded=1;'
                     f'arcSize={4 if top else 6};container=1;collapsible=0;html=1;whiteSpace=wrap;'
                     f'fillColor={"#F8FAFC" if top else "#FFFFFF"};strokeColor=#CBD5E1;{"" if top else "dashed=1;"}'
                     'verticalAlign=top;align=left;spacingLeft=12;spacingTop=4;fontStyle=1;fontSize=12;'
                     f'fontColor=#475569;" vertex="1" parent="{"grp_" + esc(par) if par else "1"}">'
                     f'<mxGeometry x="{x - px:.0f}" y="{y - py:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/>'
                     '</mxCell>')
    for n, v in g["nodes"].items():
        x, y, w, h = L["nodes"][n]
        par = v["group"] if v["group"] in absb else None
        px, py = absb[par] if par else (-OX, -OY)
        fill, stroke, _ = ROLE[v["kind"]]
        cells.append(f'<mxCell id="n_{esc(n)}" value="{esc(v["label"])}" style="{DRAWIO_SHAPE[v["kind"]]}'
                     f'whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth=1.5;fontColor={INK};'
                     f'fontStyle=1;fontSize=13;shadow=1;" vertex="1" parent="{"grp_" + esc(par) if par else "1"}">'
                     f'<mxGeometry x="{x - px:.0f}" y="{y - py:.0f}" width="{w:.0f}" height="{h:.0f}" as="geometry"/>'
                     '</mxCell>')
    for k, (e, (a, b, kind, label)) in enumerate(zip(L["edges"], g["edges"])):
        arrow = "endArrow=none;" if kind == "---" else "endArrow=block;endFill=1;"
        extra = {"-.->": "dashed=1;", "==>": "strokeWidth=3;"}.get(kind, "strokeWidth=1.5;")
        route = "edgeStyle=orthogonalEdgeStyle;rounded=1;"
        pts = ""
        if L["engine"] == "Graphviz" and len(e["bezier"]) > 4:  # keep Graphviz's route
            route = "curved=1;"
            way = e["bezier"][3:-1:3]
            pts = '<Array as="points">' + "".join(f'<mxPoint x="{x + OX:.0f}" y="{y + OY:.0f}"/>' for x, y in way) + \
                  "</Array>"
        cells.append(f'<mxCell id="e{k}" value="{esc(label)}" style="{route}html=1;{arrow}{extra}strokeColor={EDGE};'
                     f'fontSize=11;fontColor={MUTED};labelBackgroundColor=#FFFFFF;" edge="1" parent="1" '
                     f'source="n_{esc(a)}" target="n_{esc(b)}"><mxGeometry relative="1" as="geometry">{pts}'
                     '</mxGeometry></mxCell>')
    return ('<mxfile host="sdlc-arch-export"><diagram id="architecture" name="Architecture"><mxGraphModel grid="1" '
            'gridSize="10" page="0" background="#FFFFFF"><root>' + "".join(cells) +
            "</root></mxGraphModel></diagram></mxfile>\n")


# ---------------------------------------------------------------- PNG
def png(svg_file: Path, out: Path, src: str, w: float, h: float) -> str:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": math.ceil(w), "height": math.ceil(h)}, device_scale_factor=2)
            pg.goto(svg_file.resolve().as_uri())
            pg.screenshot(path=str(out))  # viewport = picture size (full_page hangs on bare SVG documents)
            b.close()
        return f"wrote {out}"
    except Exception as e:  # no Playwright / no browser: fall back to the Mermaid CLI's own rendering
        why = f"Playwright unavailable ({type(e).__name__})"
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return f"no PNG: {why} and no Mermaid CLI — open {svg_file.name} in a browser instead"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.mmd"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([mmdc, "-i", str(f), "-o", str(out), "-b", "white", "-s", "2"], capture_output=True,
                           text=True)
    return f"wrote {out} (Mermaid CLI; {why})" if r.returncode == 0 else f"no PNG: mmdc failed: {r.stderr[-300:]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default="sdlc/ARCHITECTURE.md")
    ap.add_argument("--out", help="folder for the exports (default: next to the file)")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--layout", choices=["auto", "dot", "builtin"], default="auto")
    a = ap.parse_args()
    f = Path(a.file)
    if not f.is_file():
        sys.exit(f"{f} not found")
    md = f.read_text(encoding="utf-8")
    src = mermaid_block(md)
    g = parse(src)
    if g["problems"]:
        print("Unsupported Mermaid (simplify these lines):\n  " + "\n  ".join(g["problems"]))
        sys.exit(2)
    if not g["nodes"]:
        sys.exit("the diagram has no nodes")
    dims = {n: size(v) for n, v in g["nodes"].items()}
    L = None if a.layout == "builtin" else layout_dot(g, dims)
    if L is None:
        if a.layout == "dot":
            sys.exit("Graphviz `dot` not found (winget install graphviz / brew install graphviz / apt install graphviz)")
        L = layout_builtin(g, dims)
    h1 = re.search(r"^#\s+(.+)$", md, re.M)
    title = re.sub(r"^(Architecture\s*[—:-]\s*)", "", h1.group(1).strip()) if h1 else "Architecture"
    title = title if "architecture" in title.lower() else f"{title} — architecture"
    status = re.search(r"^\s*Status:\s*[*`]*([\w-]+)", md, re.M | re.I)
    caption = (f"Status: {status.group(1)}  ·  " if status else "") + f"from {f.name}  ·  layout: {L['engine']}"
    out = Path(a.out) if a.out else f.parent
    out.mkdir(parents=True, exist_ok=True)
    pic, W, H = svg(g, L, title, caption)
    (out / "architecture.svg").write_text(pic, encoding="utf-8")
    (out / "architecture.drawio").write_text(drawio(g, L, title), encoding="utf-8")
    print(f"wrote {out / 'architecture.svg'} and architecture.drawio ({len(g['nodes'])} nodes, {len(g['edges'])} "
          f"edges, {len(g['groups'])} groups; layout: {L['engine']})")
    if not a.no_png:
        print(png(out / "architecture.svg", out / "architecture.png", src, W, H))


if __name__ == "__main__":
    main()
