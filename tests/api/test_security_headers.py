import pytest


@pytest.mark.parametrize("path", ["/api/public/health", "/does-not-exist"])
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
