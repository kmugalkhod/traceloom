from traceloom import __version__


def test_health_ok(client):
    r = client.get("/api/public/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_health_deep_reports_registered_checks(app, client):
    app.state.container.health_checks["extra"] = lambda: True
    body = client.get("/api/public/health?deep=1").json()
    assert {k: body["checks"][k] for k in ("db", "blob", "extra")} == {
        "db": "ok",
        "blob": "ok",
        "extra": "ok",
    }


def test_health_deep_failure_is_503(app, client):
    app.state.container.health_checks["queue"] = lambda: False
    r = client.get("/api/public/health?deep=1")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
