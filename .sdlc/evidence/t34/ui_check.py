import os
import re

os.environ["DATABASE_URL"] = "sqlite:///./.qa-tmp/t34.db"
os.environ["TRACELOOM_BLOB_DIR"] = "./.qa-tmp/blobs"
from fastapi.testclient import TestClient

from traceloom.main import app

P = "ec6dca56e3754ebeb0c7f3ff1081b194"
H = {"Accept": "text/html"}
with TestClient(app, follow_redirects=True) as c:
    c.get("/api/public/health")
    H["X-CSRF-Token"] = next(v for k, v in c.cookies.items() if "csrf" in k.lower())
    r = c.post(
        "/login", data={"email": "demo@example.com", "password": "demo-password-123"}, headers=H
    )
    print("login", r.status_code, r.url)
    for path, needle in [
        ("settings/scores", "helpfulness"),
        ("queues", "Demo review queue"),
        ("datasets", "demo-support-questions"),
    ]:
        r = c.get(f"/project/{P}/{path}", headers=H)
        print(path, r.status_code, r.text.count(needle))
    r = c.get(f"/project/{P}/datasets", headers=H)
    m = re.search(r"/datasets/[0-9a-f]{32}", r.text)
    print(m and m.group(0))
    r = c.get(f"/project/{P}{m.group(0)}", headers=H)
    print(r.status_code, [r.text.count(x) for x in ("demo-run-baseline", "demo-run-candidate")])
    r = c.get(f"/project/{P}{m.group(0)}?tab=runs", headers=H)
    print(
        "runs",
        r.status_code,
        [r.text.count(x) for x in ("demo-run-baseline", "demo-run-candidate")],
    )
