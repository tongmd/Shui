"""Research replay stays bounded, metadata polling stays small, and logs stay private."""
import asyncio
import importlib
import json
import logging

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from shui.app import COOKIE, create_app
from shui.store import now


@pytest.fixture
def perf_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    owner = app.state.store.create_user("owner", "Owner", "test-password", "owner")
    other = app.state.store.create_user("outside", "Outside", "test-password")
    clients = []
    for user in (owner, other):
        client = TestClient(app, raise_server_exceptions=False)
        login = client.post("/api/login", json={"username": user["username"], "password": "test-password"})
        client.headers["x-csrf-token"] = login.json()["csrf"]
        clients.append(client)
    pid = clients[0].post("/api/projects", json={"name": "Research"}).json()["id"]
    yield app, clients[0], clients[1], pid
    for client in clients:
        client.close()


def test_new_research_notifications_do_not_duplicate_snapshot(perf_app):
    app, owner, _, pid = perf_app
    changed = owner.post(f"/api/projects/{pid}/research", json={"experiments": [{"name": "Real experiment", "description": "Evidence " * 400}]})
    assert changed.status_code == 200
    insights = changed.json()["insights"]
    assert insights["experiments"][0]["description"] == ("Evidence " * 400).strip()
    with app.state.store.connect() as c:
        row = c.execute("SELECT payload FROM events WHERE project_id=? AND type='research.updated' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    saved = json.loads(row["payload"])
    assert saved["refresh"] is True
    assert saved["summary"]["experiment_count"] == 1
    assert "insights" not in saved and len(row["payload"]) < 1000
    assert owner.get(f"/api/projects/{pid}/research").json()["insights"] == insights


@pytest.mark.asyncio
async def test_legacy_snapshot_replay_preserves_cursors_without_transferring_catalog(perf_app):
    app, owner, outside, pid = perf_app
    huge = {"insights": {"figures": [{"caption": "legacy evidence " * 50000}], "summary": {"figure_count": 1}}, "actor": "owner"}
    encoded = json.dumps(huge)
    with app.state.store.connect() as c:
        before = app.state.store.event(c, pid, "agent.tool", {"name": "before"})
        ids = [c.execute("INSERT INTO events(project_id,type,payload,created_at) VALUES(?,?,?,?)", (pid, "research.updated", encoded, now())).lastrowid for _ in range(3)]
        after = app.state.store.event(c, pid, "agent.tool", {"name": "after"})
    for endpoint in ("event-batch", "activity"):
        response = owner.get(f"/api/projects/{pid}/{endpoint}", params={"after": before})
        assert response.status_code == 200 and len(response.content) < 2000
        events = response.json()["events"]
        assert [event["id"] for event in events] == ids + [after]
        assert all(event["payload"] == {"refresh": True} for event in events[:3])
        assert events[-1]["payload"] == {"name": "after"}
        assert outside.get(f"/api/projects/{pid}/{endpoint}").status_code == 404
    # The stream uses the same projection; direct iteration avoids an infinite SSE response.
    route = next(route for route in app.routes if route.path == "/api/projects/{pid}/events")
    request = Request({"type": "http", "method": "GET", "path": f"/api/projects/{pid}/events", "headers": [(b"cookie", f"{COOKIE}={owner.cookies.get(COOKIE)}".encode())]})

    async def connected():
        return False

    request.is_disconnected = connected
    response = await route.endpoint(pid, request, after=before)
    try:
        chunk = await anext(response.body_iterator)
        assert len(chunk) < 500
        event = json.loads(chunk.split("data: ", 1)[1])
        assert event["id"] == ids[0] and event["payload"] == {"refresh": True}
    finally:
        await response.body_iterator.aclose()
    with app.state.store.connect() as c:
        # The output optimization must not rewrite/delete the durable legacy evidence.
        assert c.execute("SELECT payload FROM events WHERE id=?", (ids[0],)).fetchone()[0] == encoded


def test_meta_poll_omits_transcripts_and_research_but_preserves_permissions(perf_app):
    app, owner, outside, pid = perf_app
    owner.post(f"/api/projects/{pid}/messages", json={"content": "A large message " * 5000, "client_id": "metadata-test-01"})
    owner.post(f"/api/projects/{pid}/research", json={"experiments": [{"name": "Research", "description": "evidence " * 400}]})
    response = owner.get(f"/api/projects/{pid}/meta")
    assert response.status_code == 200 and len(response.content) < 3000
    assert set(response.json()) == {"project", "members"}
    assert response.json()["project"]["queued_count"] == 1
    assert response.json()["project"]["role"] == "owner"
    assert outside.get(f"/api/projects/{pid}/meta").status_code == 404
    assert owner.get(f"/api/projects/{pid}").json()["messages"][0]["content"].startswith("A large message")


def test_slow_stream_and_errors_log_only_safe_template_and_timing(perf_app, monkeypatch, caplog):
    app, owner, _, pid = perf_app
    module = importlib.import_module("shui.app")
    monkeypatch.setattr(module, "HTTP_SLOW_REQUEST_MS", 10)

    @app.get("/test/slow/{private_path}")
    async def slow(private_path: str):
        async def pieces():
            yield b"first"
            await asyncio.sleep(0.03)
            yield b"last"
        return StreamingResponse(pieces())

    @app.post("/test/failure/{private_path}")
    async def failure(private_path: str):
        raise RuntimeError("internal-token-do-not-log")

    with caplog.at_level(logging.WARNING, logger="shui.http"):
        response = owner.get("/test/slow/private-name?token=query-secret", headers={"authorization": "Bearer header-secret"})
        assert response.content == b"firstlast"
        assert owner.post("/test/failure/private-name?token=query-secret", json={"password": "body-secret"}).status_code == 500
    records = [json.loads(record.message) for record in caplog.records if record.name == "shui.http"]
    slow_log = next(record for record in records if record["route"] == "/test/slow/{private_path}")
    assert slow_log["total_ms"] >= 20 and slow_log["total_ms"] > slow_log["ttfb_ms"]
    failed = next(record for record in records if record["status"] == 500)
    assert failed["route"] == "/test/failure/{private_path}" and failed["method"] == "POST"
    assert all(set(record) == {"route", "method", "status", "ttfb_ms", "total_ms"} for record in records)
    for secret in ("private-name", "query-secret", "header-secret", "body-secret", "internal-token-do-not-log", owner.cookies.get(COOKIE)):
        assert secret not in json.dumps(records)


@pytest.mark.asyncio
async def test_idle_stream_heartbeat_has_no_cursor_or_persisted_event(perf_app, monkeypatch):
    import itertools
    import time as real_time
    from types import SimpleNamespace

    app, owner, _, pid = perf_app
    module = importlib.import_module("shui.app")
    # Simulate a genuinely idle stream reaching its 15-second heartbeat deadline.
    ticks = itertools.chain((0.0, 16.0), itertools.repeat(16.0))
    monkeypatch.setattr(module, "time", SimpleNamespace(time=real_time.time, monotonic=lambda: next(ticks)))
    with app.state.store.connect() as c:
        cursor = c.execute("SELECT MAX(id) FROM events WHERE project_id=?", (pid,)).fetchone()[0]
        count = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    route = next(route for route in app.routes if route.path == "/api/projects/{pid}/events")
    request = Request({"type": "http", "method": "GET", "path": f"/api/projects/{pid}/events", "headers": [(b"cookie", f"{COOKIE}={owner.cookies.get(COOKIE)}".encode())]})

    async def connected():
        return False

    request.is_disconnected = connected
    response = await route.endpoint(pid, request, after=cursor)
    try:
        heartbeat = await anext(response.body_iterator)
        assert heartbeat == ": keepalive\n\nevent: heartbeat\ndata: {}\n\n"
        assert "id:" not in heartbeat and pid not in heartbeat
        with app.state.store.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == count
            next_id = app.state.store.event(c, pid, "agent.tool", {"name": "after idle"})
        replay = await anext(response.body_iterator)
        event = json.loads(replay.split("data: ", 1)[1])
        assert event["id"] == next_id and event["payload"] == {"name": "after idle"}
        # Heartbeats must not bypass the stream's ongoing session/access checks.
        with app.state.store.connect() as c:
            c.execute("DELETE FROM sessions")
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
    finally:
        await response.body_iterator.aclose()


def test_static_assets_are_content_versioned_and_revalidated_safely(tmp_path, monkeypatch):
    import hashlib
    import os
    import re

    web = tmp_path / "web"
    web.mkdir()
    assets = {
        "app.js": b"window.shui = 'one';",
        "lucide-icons.js": b"window.icons = {};",
        "styles.css": b"body { color: black; }",
        "icon.png": b"public-icon",
        "favicon.ico": b"public-favicon",
        "icon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    }
    for name, content in assets.items():
        (web / name).write_bytes(content)
    (web / "index.html").write_text('''<!doctype html><html><head>
<link rel="stylesheet" href="/styles.css"><script defer src="/lucide-icons.js"></script>
<script defer src="/app.js"></script><link rel="icon" href="/icon.png?v=old-manual-version">
</head><body><img src="/icon.png"><a href="">Reload</a></body></html>''')
    monkeypatch.setenv("SHUI_WEB_DIR", str(web))
    app = create_app(tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        html = client.get("/")
        assert html.status_code == 200 and html.headers["cache-control"] == "no-store"
        assert "old-manual-version" not in html.text
        for name in ("app.js", "lucide-icons.js", "styles.css", "icon.png"):
            digest = hashlib.sha256(assets[name]).hexdigest()
            path = f"/{name}?v={digest}"
            assert path in html.text
            asset = client.get(path)
            assert asset.content == assets[name]
            assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
            assert asset.headers["etag"] == f'"{digest}"'
            head = client.head(path)
            assert head.content == b"" and head.headers["etag"] == asset.headers["etag"]
            fresh = client.get(path, headers={"if-none-match": asset.headers["etag"]})
            assert fresh.status_code == 304 and not fresh.content
            unversioned = client.get(f"/{name}")
            assert unversioned.headers["cache-control"] == "public, no-cache"
            assert client.get(f"/{name}", headers={"if-none-match": f'W/"{digest}"'}).status_code == 304
        old_hash = hashlib.sha256(assets["app.js"]).hexdigest()
        previous_stat = (web / "app.js").stat()
        replacement = b"window.shui = 'two';"  # Same size; also preserve mtime like a sync/deploy can.
        (web / "app.js").write_bytes(replacement)
        os.utime(web / "app.js", ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        changed_html = client.get("/index.html")
        new_hash = hashlib.sha256(replacement).hexdigest()
        assert f"/app.js?v={new_hash}" in changed_html.text
        assert f"/app.js?v={old_hash}" not in changed_html.text
        stale = client.get(f"/app.js?v={old_hash}", headers={"if-none-match": f'"{old_hash}"'})
        assert stale.status_code == 200 and stale.content == replacement
        assert stale.headers["cache-control"] == "public, no-cache"
        assert client.get("/api/session").headers["cache-control"] == "private, no-store"
        missing = client.get("/credentials.json?v=" + new_hash)
        assert missing.status_code == 404 and "immutable" not in missing.headers["cache-control"]
        csp = changed_html.headers["content-security-policy"]
        assert "script-src 'self';" in csp
        assert re.search(r"script-src[^;]*unsafe-inline", csp) is None


def test_authenticated_files_and_metadata_never_receive_public_cache_policy(perf_app):
    app, owner, _, pid = perf_app
    uploaded = owner.post(f"/api/projects/{pid}/files", files={"file": ("private.txt", b"private research")}).json()
    for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/meta", uploaded["download_url"]):
        response = owner.get(path, params={"v": "pretend-public-version"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "public" not in response.headers["cache-control"]
