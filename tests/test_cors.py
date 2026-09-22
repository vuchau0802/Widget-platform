"""CORS tests for the public submission API (per the brief).

Cross-origin POST /submissions must both answer preflights and carry an
allow-origin header on the actual response.
"""

def _preflight_headers():
    return {
        "Origin": "http://example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }


def test_preflight_authorizes_submission_endpoint(client):
    r = client.options("/submissions", headers=_preflight_headers())
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert any(m.strip() == "POST" for m in r.headers["access-control-allow-methods"].split(","))


def test_preflight_without_origin_is_not_cors_handled(client):
    r = client.options("/submissions", headers={"Access-Control-Request-Method": "POST"})
    assert r.status_code == 405
    assert "access-control-allow-origin" not in r.headers