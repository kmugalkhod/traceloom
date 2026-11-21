#!/usr/bin/env python3
"""Deliberate-bug checks: prove the tests catch the bugs they should, before trusting a green run.

  python .sdlc/bin/mutate.py            # every defect in sdlc/defects.json, against the current checkout
  python .sdlc/bin/mutate.py --json

Each defect is injected into the (clean) checkout, the verify `test` command runs, and the file is restored.
  caught        the tests went red — good
  ESCAPED       the tests stayed green: this kind of bug could merge unnoticed
  not injected  its `find` snippet is not in the file exactly once (fix the anchor)
The tests must pass with no defect first, otherwise nothing can be learned. Results go to
.sdlc/state/mutations/<commit>.json. Exit 1 when any defect escaped or the baseline is red.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, find_root, git, load_config, portable_cmd, state_path, utcnow  # noqa: E402

DEFECTS = "sdlc/defects.json"


def _test(root: Path, cmd: str, timeout: int) -> int:
    try:  # no bytecode written: a cached .pyc of one injected version must never stand in for another
        return subprocess.run(portable_cmd(cmd), shell=True, cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).returncode
    except subprocess.TimeoutExpired:
        return 124  # a defect that hangs the tests is caught too


def load_defects(root: Path) -> list:
    p = root / DEFECTS
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    return [d for d in (data.get("defects", []) if isinstance(data, dict) else data) if isinstance(d, dict)]


def run_defects(root: Path, cfg: dict) -> dict:
    test = (cfg.get("verify") or {}).get("test")
    head = git(root, "rev-parse", "HEAD")
    rep = {"head": head, "at": utcnow(), "results": [], "escaped": [], "error": ""}
    defs = load_defects(root)
    if not test:
        rep["error"] = "no verify.test command configured"
    elif not defs:
        rep["error"] = f"no deliberate bugs defined ({DEFECTS})"
    elif git(root, "status", "--porcelain", "--untracked-files=no"):
        rep["error"] = "the checkout has uncommitted changes"
    if rep["error"]:
        return save(root, rep)
    timeout = int((cfg.get("checks") or {}).get("mutation_timeout", 600))
    if _test(root, test, timeout):
        rep["error"] = "the tests already fail with no defect injected"
        return save(root, rep)
    for d in defs:
        f, find, repl = root / str(d.get("file", "")), str(d.get("find", "")), str(d.get("replace", ""))
        r = {"id": d.get("id") or d.get("file"), "why": d.get("why", "")}
        try:
            original = f.read_bytes()
        except OSError:
            rep["results"].append({**r, "result": "not injected", "detail": f"{d.get('file')} not found"})
            continue
        text = original.decode("utf-8", errors="surrogateescape")
        n = text.count(find) if find else 0
        if n != 1:
            rep["results"].append({**r, "result": "not injected", "detail": f"'find' appears {n} times"})
            continue
        st = f.stat()
        try:
            f.write_bytes(text.replace(find, repl, 1).encode("utf-8", errors="surrogateescape"))
            bump = st.st_mtime + 2 * (len(rep["results"]) + 1)  # a timestamp no cache has seen
            os.utime(f, (bump, bump))
            code = _test(root, test, timeout)
        finally:
            f.write_bytes(original)  # always restore, even on Ctrl+C — content and timestamp
            os.utime(f, (st.st_atime, st.st_mtime))
        r["result"] = "caught" if code else "ESCAPED"
        rep["results"].append(r)
        if not code:
            rep["escaped"].append(r["id"])
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        rep["error"] = "the checkout was not restored cleanly — check git status"
    return save(root, rep)


def save(root: Path, rep: dict) -> dict:
    p = state_path(root, f"mutations/{(rep.get('head') or 'none')[:12]}.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    audit(root, "mutations", head=rep.get("head"), escaped=rep["escaped"], error=rep["error"],
          caught=sum(r.get("result") == "caught" for r in rep["results"]))
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    rep = run_defects(root, load_config(root))
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        for r in rep["results"]:
            print(f"{r['result']:13} {r['id']}" + (f"  — {r.get('why')}" if r["result"] == "ESCAPED" else "")
                  + (f"  ({r['detail']})" if r.get("detail") else ""))
        if rep["error"]:
            print(f"! {rep['error']}")
        caught = sum(r["result"] == "caught" for r in rep["results"])
        print(f"\n{caught} caught · {len(rep['escaped'])} escaped · "
              f"{sum(r['result'] == 'not injected' for r in rep['results'])} not injected")
    sys.exit(1 if rep["escaped"] or rep["error"] else 0)


if __name__ == "__main__":
    main()
