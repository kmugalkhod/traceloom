import sys, tempfile, pathlib
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from tests.e2e import live_server as ls
from tests.factories import DEFAULT_PASSWORD
from traceloom.auth.permissions import Role
from traceloom.datasets import service
from traceloom.datasets.service import Actor
from traceloom.db.models import Project, User
from traceloom.config import Settings
from traceloom.main import create_app
import threading, time, uvicorn

tmp = pathlib.Path(tempfile.mkdtemp())
s = Settings(TRACELOOM_ENV="dev", TRACELOOM_MODE="lite", DATABASE_URL=f"sqlite:///{tmp/'e.db'}",
             TRACELOOM_BLOB_DIR=str(tmp/"b"), TRACELOOM_SERVER_SECRET="x"*32)
app = create_app(s); port = ls.free_port()
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started: time.sleep(0.05)
live = ls.Live(f"http://127.0.0.1:{port}", app)
w = ls.make_world(live)
with live.container.session_factory() as db:
    p = db.get(Project, w["project_id"]); u = db.query(User).filter_by(email=w["member"]).one()
    a = Actor.for_user(u, Role.MEMBER)
    ds = service.create_dataset(db, p, a, name="refund-cases", description="Support refund questions")
    it = service.create_item(db, p, a, ds, input={"q": "Where is my refund?"}, expected_output={"a": "5 days"})
    service.update_item(db, p, a, service.get_item(db, p, it.id), input={"q": "Where is my refund?!"}, expected_output={"a": "5 days"}, metadata={})
    db.commit(); dsid, itid = ds.id, it.id
base = f"{live.url}/project/{w['project_id']}/datasets"
out = pathlib.Path(".sdlc/evidence")
with sync_playwright() as pw:
    b = ls._launch(pw)
    for width in (1280, 390):
        ctx = b.new_context(viewport={"width": width, "height": 900}); pg = ctx.new_page()
        ls.sign_in(pg, live, w["member"], DEFAULT_PASSWORD)
        for name, url in (("list", base), ("detail", f"{base}/{dsid}"), ("drawer", f"{base}/{dsid}?item={itid}"), ("history", f"{base}/{dsid}?tab=history")):
            pg.goto(url); pg.wait_for_timeout(300)
            pg.screenshot(path=str(out/f"t18-{name}-{width}.png"), full_page=True)
            print(name, width, "scrollW", pg.evaluate("document.documentElement.scrollWidth"))
        m = ctx.new_page(); m.goto("http://127.0.0.1:8690/integration/sdlc/changes/2026-11-09-phase3-evaluation/design/mockup.html")
        m.wait_for_timeout(500); m.screenshot(path=str(out/f"t18-mockup-{width}.png"), full_page=True)
        ctx.close()
    b.close()
srv.should_exit = True
