"""Guards for the shared UI stylesheets: tokens exist and no literal colours creep in."""

import re
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[2] / "traceloom" / "web" / "static" / "ui"
FILES = [
    "base", "traces", "trace-detail", "trace-tree", "trace-io",
    "dashboard", "prompts", "playground", "settings",
]
LITERAL_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def test_all_ui_stylesheets_exist():
    for name in FILES:
        assert (UI_DIR / f"{name}.css").is_file(), name


def test_base_defines_tokens():
    css = (UI_DIR / "base.css").read_text()
    for token, value in {
        "--row-h": "44px",
        "--ctl-h": "36px",
        "--ctl-h-sm": "30px",
        "--panel-head-h": "52px",
    }.items():
        assert re.search(rf"{re.escape(token)}\s*:\s*{value}", css), token


def test_no_literal_colours_in_ui_css():
    offenders = []
    for path in sorted(UI_DIR.glob("*.css")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if LITERAL_COLOUR.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "use colour tokens, not literals:\n" + "\n".join(offenders)


def test_literal_colour_pattern_catches_literals():
    for bad in ("color:#fff", "color: #1a2b3c", "background:rgb(0,0,0)", "x:rgba(1,2,3,.5)", "x:hsl(1,2%,3%)"):
        assert LITERAL_COLOUR.search(bad), bad
    assert not LITERAL_COLOUR.search("color:var(--fg);margin:0")
