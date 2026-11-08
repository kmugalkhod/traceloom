#!/usr/bin/env python3
"""Run the out-of-the-loop factory on a timer, on an always-on machine — not a laptop that sleeps.

  python .sdlc/bin/schedule.py install [--every 5] [--manager-hours 3]   # writes the timer files, shows them
  python .sdlc/bin/schedule.py install --apply                           # …and registers the timer
  python .sdlc/bin/schedule.py remove [--apply]
  python .sdlc/bin/schedule.py status
  python .sdlc/bin/schedule.py fire        # what the timer runs: one loop tick, plus the manager when it is due
  python .sdlc/bin/schedule.py halt | unhalt   # stop / allow new ticks without touching the timer

Linux: a systemd user service + timer (run `loginctl enable-linger $USER` so it keeps running after logout).
macOS: a launchd agent. Windows: a Task Scheduler task running pythonw (no console window).
Secrets (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, tracker and notification tokens) go in ~/.sdlc-env as
KEY=value lines, mode 600 — never in the repo or the timer files. A tick that finds the previous one still running
is skipped, so long ticks never pile up. Each tick's output is appended to .sdlc/state/schedule.log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sdlc_core import audit, find_root, load_config, read_state, state_path, utcnow  # noqa: E402

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("SDLC_ENV_FILE") or Path.home() / ".sdlc-env")


def name_for(root: Path) -> str:
    return "sdlc-" + "".join(c if c.isalnum() else "-" for c in root.name.lower())[:30] + "-" + \
        hashlib.sha1(str(root).encode()).hexdigest()[:6]


def load_env_file() -> dict:
    out = {}
    if ENV_FILE.is_file():
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def units(root: Path, every: int) -> dict:
    py, fire = sys.executable, str(root / ".sdlc" / "bin" / "schedule.py")
    n = name_for(root)
    path = os.environ.get("PATH", "")
    if sys.platform.startswith("linux"):
        return {f"{n}.service": f"""[Unit]
Description=SDLC factory tick for {root}

[Service]
Type=oneshot
WorkingDirectory={root}
Environment=PATH={path}
Environment=IS_SANDBOX=1
EnvironmentFile=-{ENV_FILE}
ExecStart={py} {fire} fire
""", f"{n}.timer": f"""[Unit]
Description=Every {every} min: SDLC factory tick for {root}

[Timer]
OnBootSec=2min
OnUnitActiveSec={every}min
Persistent=true

[Install]
WantedBy=timers.target
"""}
    if sys.platform == "darwin":
        return {f"com.{n}.plist": f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.{n}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{fire}</string><string>fire</string></array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StartInterval</key><integer>{every * 60}</integer>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>StandardOutPath</key><string>{root}/.sdlc/state/schedule.log</string>
  <key>StandardErrorPath</key><string>{root}/.sdlc/state/schedule.log</string>
</dict></plist>
"""}
    pyw = str(Path(py).with_name("pythonw.exe")) if Path(py).with_name("pythonw.exe").exists() else py
    return {f"{n}.cmd": f'schtasks /Create /F /TN "{n}" /SC MINUTE /MO {every} /TR "\\"{pyw}\\" \\"{fire}\\" fire"\n'}


def install(root: Path, every: int, apply: bool) -> None:
    out = state_path(root, "schedule")
    out.mkdir(parents=True, exist_ok=True)
    u = units(root, every)
    for f, text in u.items():
        (out / f).write_text(text, encoding="utf-8")
        print(f"--- {out / f}\n{text}")
    cfg = read_state(root, "schedule.json") or {}
    cfg.update(every=every, installed=utcnow(), name=name_for(root))
    state_path(root, "schedule.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if not apply:
        print("Not registered yet. Check the files above, then run again with --apply.")
        return
    if sys.platform.startswith("linux"):
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        for f in u:
            shutil.copy(out / f, d / f)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", f"{name_for(root)}.timer"])
        print("timer registered" if r.returncode == 0 else "systemctl failed: is this a systemd user session?")
        print("Keep it running after logout:  loginctl enable-linger $USER")
    elif sys.platform == "darwin":
        dst = Path.home() / "Library" / "LaunchAgents" / f"com.{name_for(root)}.plist"
        shutil.copy(out / f"com.{name_for(root)}.plist", dst)
        subprocess.run(["launchctl", "load", "-w", str(dst)], check=False)
        print(f"launchd agent loaded: {dst}")
    else:
        r = subprocess.run((out / f"{name_for(root)}.cmd").read_text(encoding="utf-8"), shell=True)
        print("task registered (runs while you are logged on)" if r.returncode == 0 else "schtasks failed")
    if not ENV_FILE.is_file():
        print(f"Put secrets in {ENV_FILE} (KEY=value lines, chmod 600), e.g. CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.")


def remove(root: Path, apply: bool) -> None:
    n = name_for(root)
    if not apply:
        print(f"Would remove the timer {n}. Run again with --apply.")
        return
    if sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{n}.timer"], check=False)
        for f in (f"{n}.service", f"{n}.timer"):
            (Path.home() / ".config" / "systemd" / "user" / f).unlink(missing_ok=True)
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "LaunchAgents" / f"com.{n}.plist"
        subprocess.run(["launchctl", "unload", "-w", str(p)], check=False)
        p.unlink(missing_ok=True)
    else:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", n], check=False)
    print(f"timer {n} removed")


def _alive(pid: int) -> bool:
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
            return str(pid) in r.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def fire(root: Path, cfg: dict) -> int:
    """One tick. Skipped when halted or when the previous tick is still running."""
    log = state_path(root, "schedule.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    say = lambda m: open(log, "a", encoding="utf-8").write(f"[{utcnow()}] {m}\n")
    if state_path(root, "halt").exists():
        say("halted: tick skipped (schedule.py unhalt to resume)")
        return 0
    lock = state_path(root, "schedule.lock")
    if lock.exists():
        try:
            if _alive(int(lock.read_text().strip() or 0)):
                say("previous tick still running: skipped")
                return 0
        except ValueError:
            pass
    lock.write_text(str(os.getpid()))
    try:
        env = {**os.environ, **load_env_file(), "PYTHONIOENCODING": "utf-8"}
        st = read_state(root, "schedule.json") or {}
        say("tick: loop.py --once")
        with open(log, "a", encoding="utf-8") as fh:
            r = subprocess.run([sys.executable, str(HERE / "loop.py"), "--once"], cwd=root, env=env,
                               stdout=fh, stderr=subprocess.STDOUT)
        hours = float(st.get("manager_hours", (cfg.get("manager") or {}).get("every_hours", 3)))
        if time.time() - float(st.get("manager_at", 0)) > hours * 3600:
            say("manager: due")
            with open(log, "a", encoding="utf-8") as fh:
                subprocess.run([sys.executable, str(HERE / "manager.py"), "--fix"], cwd=root, env=env,
                               stdout=fh, stderr=subprocess.STDOUT)
            st["manager_at"] = time.time()
            state_path(root, "schedule.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
        audit(root, "schedule-tick", exit=r.returncode)
        return r.returncode
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install")
    s.add_argument("--every", type=int, default=5, help="minutes between ticks")
    s.add_argument("--manager-hours", type=float, default=3)
    s.add_argument("--apply", action="store_true")
    s = sub.add_parser("remove")
    s.add_argument("--apply", action="store_true")
    for c in ("status", "fire", "halt", "unhalt"):
        sub.add_parser(c)
    a = ap.parse_args()
    root = find_root()
    if not root:
        sys.exit("not an SDLC repo: run /sdlc:init first")
    cfg = load_config(root)
    if a.cmd == "install":
        install(root, a.every, a.apply)
        st = read_state(root, "schedule.json") or {}
        st["manager_hours"] = a.manager_hours
        state_path(root, "schedule.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
    elif a.cmd == "remove":
        remove(root, a.apply)
    elif a.cmd == "fire":
        sys.exit(fire(root, cfg))
    elif a.cmd in ("halt", "unhalt"):
        p = state_path(root, "halt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(utcnow()) if a.cmd == "halt" else p.unlink(missing_ok=True)
        print("halted: ticks do nothing until unhalt" if a.cmd == "halt" else "ticks run again")
    else:
        st = read_state(root, "schedule.json") or {}
        hb = read_state(root, "loop-heartbeat.json") or {}
        print(f"timer {st.get('name', name_for(root))}: every {st.get('every', '?')} min, installed {st.get('installed', 'no')}")
        print(f"last loop tick: {hb.get('at', 'never')} · halted: {state_path(root, 'halt').exists()}")
        lg = state_path(root, "schedule.log")
        if lg.is_file():
            print("".join(lg.read_text(encoding="utf-8", errors="replace").splitlines(True)[-8:]))


if __name__ == "__main__":
    main()
