import http.client
import json
import threading

import pytest

from afser_data.authentication import TokenRegistry
from afser_data.config import BridgeConfig, private_write
from afser_data.http_bridge import BridgeServer, RateLimiter
from afser_data.models import Chapter, RawRecord, Record, SourceSnapshot
from afser_data.store import Store


@pytest.fixture
def bridge(tmp_path):
    store = Store(tmp_path)
    record = Record("42", "sending", "BER", True, "open", "https://www.afser.de/ereignis-liste/avtproject/42.html", latitude=52.51, longitude=13.41)
    store.activate(SourceSnapshot([Chapter("BER", "Berlin")], [RawRecord("42", "sending", {"name": "Private Name", "address": "Hidden address"}, record)]))
    token = TokenRegistry(tmp_path).create("test", 1)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><title>Map</title>")
    (tmp_path / "private.html").write_text("Private secret")
    (frontend / "symlink.html").symlink_to(tmp_path / "private.html")
    (frontend / ".hidden.html").write_text("Private hidden secret")
    server = BridgeServer(store, BridgeConfig(), port=0, frontend_dir=frontend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], token
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(bridge, path, token=None, origin=None, method="GET", extra=None):
    port, _ = bridge
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {**(extra or {})}
    if token:
        headers["Authorization"] = "Bearer " + token
    if origin:
        headers["Origin"] = origin
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read().decode()
    result = response.status, dict(response.getheaders()), json.loads(body) if body else {}
    conn.close()
    return result


def test_unauthenticated_requests_cannot_read_any_record(bridge):
    assert request(bridge, "/api/records?kind=sending&chapter=all")[0] == 401
    assert request(bridge, "/api/chapters")[0] == 401
    assert request(bridge, "/api/places?q=Berlin")[0] == 401
    _, _, health = request(bridge, "/api/status")
    assert health == {"service": "afser-private-map", "authenticationRequired": True}


def test_allowed_origin_authenticated_anonymous_data(bridge):
    _, token = bridge
    status, headers, data = request(bridge, "/api/records?kind=sending&chapter=BER", token, "http://localhost:5173")
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert headers["Cache-Control"] == "no-store, private"
    assert len(data["records"]) == 1
    assert "Private Name" not in json.dumps(data) and "Hidden address" not in json.dumps(data)


def test_wrong_origin_and_dns_rebinding_blocked(bridge):
    _, token = bridge
    assert request(bridge, "/api/chapters", token, "https://evil.example")[0] == 403
    assert request(bridge, "/api/chapters", token, extra={"Host": "evil.example"})[0] == 403


def test_browser_preflight_and_private_network_access(bridge):
    status, headers, _ = request(bridge, "/api/chapters", origin="http://localhost:5173", method="OPTIONS", extra={"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert status == 200
    assert headers["Access-Control-Allow-Private-Network"] == "true"
    assert headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"


def test_no_mutations_tokens_in_urls_or_default_all_scope(bridge):
    _, token = bridge
    assert request(bridge, "/api/sync", token, method="POST")[0] == 405
    assert request(bridge, "/api/chapters?token=private", token)[0] == 400
    assert request(bridge, "/api/records?kind=sending", token)[0] == 400


def test_static_frontend_cannot_expose_hidden_files_symlinks_or_traversal(bridge):
    port, token = bridge
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/")
    response = conn.getresponse()
    assert response.status == 200
    assert b"<title>Map</title>" in response.read()
    conn.close()
    for path in ("/../private.html", "/%2e%2e/private.html", "/.hidden.html", "/symlink.html", "/afser.sqlite3"):
        assert request(bridge, path)[0] == 404
    origin = f"http://127.0.0.1:{port}"
    assert request(bridge, "/api/chapters", token, origin)[0] == 200


def test_rate_limiter_bounds_memory_and_burst():
    limiter = RateLimiter(rate_per_minute=1, capacity=2, maximum_keys=3)
    assert limiter.allow("one")
    assert limiter.allow("one")
    assert not limiter.allow("one")
    for i in range(20):
        limiter.allow(str(i))
    assert len(limiter.items) == 3


def test_rotating_invalid_bearer_values_cannot_evade_throttling(tmp_path):
    server = BridgeServer(Store(tmp_path), BridgeConfig(), port=0)
    server.limiter = RateLimiter(rate_per_minute=1, capacity=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = (server.server_address[1], "unused")
    try:
        for i in range(2):
            assert request(client, "/api/chapters", str(i) * 40)[0] == 401
        assert request(client, "/api/chapters", "new-random-token" * 3)[0] == 429
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_token_expiry_and_revocation(tmp_path):
    registry = TokenRegistry(tmp_path)
    token = registry.create("volunteer", 1)
    assert registry.verify(token)
    assert not registry.verify("incorrect" * 5)
    assert token not in registry.path.read_text()
    entries = json.loads(registry.path.read_text())
    entries[0]["expiresAt"] = 1
    private_write(registry.path, json.dumps(entries))
    assert not registry.verify(token)
    token = registry.create("new", 1)
    registry.revoke_all()
    assert not registry.verify(token)
