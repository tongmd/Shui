"""Exercise authentication, project boundaries, files and durable API behavior."""

import hashlib
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from shui.app import COOKIE, create_app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    users = {}
    clients = {}
    for name, role in [
        ("owner", "owner"),
        ("editor", "member"),
        ("viewer", "member"),
        ("outside", "member"),
    ]:
        users[name] = app.state.store.create_user(
            name, name.title(), "test-password", role
        )
        client = TestClient(app)
        response = client.post(
            "/api/login", json={"username": name, "password": "test-password"}
        )
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf"]
        clients[name] = client
    response = clients["owner"].post(
        "/api/projects", json={"name": "Shared paper", "provider": "codex"}
    )
    assert response.status_code == 201
    pid = response.json()["id"]
    for name in ("editor", "viewer"):
        assert (
            clients["owner"]
            .post(f"/api/projects/{pid}/members", json={"username": name, "role": name})
            .status_code
            == 200
        )
    yield app, clients, users, pid
    for client in clients.values():
        client.close()


def send(client, pid, content="Review the paper", client_id="request-0001", **extra):
    return client.post(
        f"/api/projects/{pid}/messages",
        json={"content": content, "client_id": client_id, **extra},
    )


def test_owner_permissions_and_session_security(workspace):
    app, clients, users, pid = workspace
    owner, member = clients["owner"], clients["editor"]
    session = owner.get("/api/session").json()
    assert session["user"]["role"] == "owner"
    assert "password_hash" not in json.dumps(session)
    assert {p["id"] for p in session["providers"]} == {"codex", "claude"}
    assert member.get("/api/users").status_code == 403
    assert (
        member.post(
            "/api/users", json={"username": "new-user", "password": "test-password"}
        ).status_code
        == 403
    )
    assert (
        member.post("/api/projects", json={"name": "Not permitted"}).status_code == 403
    )
    response = owner.post(
        "/api/users",
        json={
            "username": "new-user",
            "display_name": "New User",
            "password": "test-password",
        },
    )
    assert response.status_code == 201
    assert response.json()["role"] == "member"
    assert "password_hash" not in response.json()
    assert (
        owner.post(
            "/api/projects", json={"name": "Unsupported", "provider": "bridge"}
        ).status_code
        == 400
    )
    assert (
        owner.post(
            "/api/projects", json={"name": "Missing CSRF"}, headers={"x-csrf-token": ""}
        ).status_code
        == 403
    )
    assert (
        owner.post(
            "/api/projects",
            json={"name": "Wrong origin"},
            headers={"origin": "https://other.example"},
        ).status_code
        == 403
    )
    token = owner.cookies.get(COOKIE)
    with app.state.store.connect() as c:
        session_row = c.execute(
            "SELECT token_hash FROM sessions WHERE user_id=?", (users["owner"]["id"],)
        ).fetchone()
    assert session_row[0] == hashlib.sha256(token.encode()).hexdigest()
    assert owner.post("/api/logout").status_code == 200
    assert owner.get("/api/projects").status_code == 401


def test_project_isolation_and_member_roles(workspace):
    app, clients, users, pid = workspace
    owner, editor, viewer, outside = (
        clients[n] for n in ("owner", "editor", "viewer", "outside")
    )
    root = f"/api/projects/{pid}"
    assert outside.get("/api/projects").json()["projects"] == []
    for suffix in ("", "/messages", "/members", "/files", "/workspace", "/activity"):
        assert outside.get(root + suffix).status_code == 404
    assert send(outside, pid).status_code == 404
    assert viewer.get(root).status_code == 200
    assert send(viewer, pid).status_code == 403
    assert viewer.post(root + "/presence").status_code == 200
    assert (
        viewer.post(
            root + "/files", files={"file": ("note.txt", b"content")}
        ).status_code
        == 403
    )
    assert send(editor, pid).status_code == 201
    assert editor.patch(root, json={"name": "Renamed"}).status_code == 403
    assert (
        editor.post(
            root + "/members", json={"username": "outside", "role": "editor"}
        ).status_code
        == 403
    )
    assert (
        owner.post(
            root + "/members", json={"username": "owner", "role": "viewer"}
        ).status_code
        == 409
    )
    assert owner.delete(root + "/members/" + users["owner"]["id"]).status_code == 409
    assert owner.delete(root + "/members/" + users["editor"]["id"]).status_code == 200
    assert editor.get(root).status_code == 404
    assert owner.get(root).json()["project"]["role"] == "owner"


def test_idempotent_messages_and_queue_order(workspace):
    app, clients, _, pid = workspace
    owner, editor = clients["owner"], clients["editor"]
    first = send(editor, pid).json()
    repeated = send(editor, pid).json()
    assert repeated == first
    assert send(owner, pid).status_code == 409
    second = send(owner, pid, "Next task", "request-0002").json()
    with app.state.store.connect() as c:
        turns = c.execute(
            "SELECT * FROM turns WHERE project_id=? ORDER BY created_at,id", (pid,)
        ).fetchall()
        message_count = c.execute(
            "SELECT COUNT(*) FROM messages WHERE project_id=?", (pid,)
        ).fetchone()[0]
    assert [row["id"] for row in turns] == [first["turn"]["id"], second["turn"]["id"]]
    assert message_count == 4
    project = owner.get(f"/api/projects/{pid}").json()["project"]
    assert project["queued_count"] == 2
    assert project["permission_mode"] == "full-access"
    assert (
        owner.patch(
            f"/api/projects/{pid}",
            json={"native_session_id": "existing-native-session"},
        ).status_code
        == 409
    )


def test_session_binding_is_unique_and_cannot_be_replaced(workspace):
    _, clients, _, pid = workspace
    owner = clients["owner"]
    root = f"/api/projects/{pid}"
    assert (
        owner.patch(
            root, json={"native_session_id": "shared-native-session"}
        ).status_code
        == 200
    )
    assert (
        owner.patch(
            root, json={"native_session_id": "other-native-session"}
        ).status_code
        == 409
    )
    assert owner.patch(root, json={"native_session_id": None}).status_code == 409
    assert (
        owner.post(
            "/api/projects",
            json={
                "name": "Second paper",
                "provider": "codex",
                "native_session_id": "shared-native-session",
            },
        ).status_code
        == 409
    )


def test_cancel_and_retry_require_author_or_owner(workspace):
    app, clients, _, pid = workspace
    owner, editor = clients["owner"], clients["editor"]
    turn = send(owner, pid).json()["turn"]
    root = f"/api/projects/{pid}/turns/{turn['id']}"
    assert editor.post(root + "/cancel").status_code == 403
    assert (
        editor.post(root + "/retry", json={"client_id": "retry-0001"}).status_code
        == 403
    )
    assert (
        owner.post(root + "/retry", json={"client_id": "retry-0001"}).status_code == 409
    )
    assert owner.post(root + "/cancel").status_code == 200
    retried = owner.post(root + "/retry", json={"client_id": "retry-0001"})
    assert retried.status_code == 201
    assert retried.json()["turn"]["id"] != turn["id"]
    assert retried.json()["message"]["content"] == "Review the paper"
    with app.state.store.connect() as c:
        assert (
            c.execute("SELECT status FROM turns WHERE id=?", (turn["id"],)).fetchone()[
                0
            ]
            == "cancelled"
        )
        events = [
            json.loads(row[0])
            for row in c.execute("SELECT payload FROM events WHERE type='turn.updated'")
        ]
    assert any(event.get("retry_of") == turn["id"] for event in events)


def test_upload_download_and_attachment_isolation(workspace):
    app, clients, _, pid = workspace
    owner = clients["owner"]
    root = f"/api/projects/{pid}"
    payload = b"<script>throw Error('never execute')</script>"
    response = owner.post(
        root + "/files", files={"file": ("../../note.html", payload, "text/html")}
    )
    assert response.status_code == 201
    uploaded = response.json()
    assert uploaded["name"] == "note.html"
    assert uploaded["sha256"] == hashlib.sha256(payload).hexdigest()
    assert uploaded["size"] == len(payload)
    downloaded = owner.get(uploaded["download_url"], params={"inline": 1})
    assert downloaded.content == payload
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in downloaded.headers["content-security-policy"]
    assert (
        send(clients["editor"], pid, attachment_ids=[uploaded["id"]]).status_code == 201
    )
    other = owner.post("/api/projects", json={"name": "Other paper"}).json()["id"]
    assert owner.get(f"/api/projects/{other}/files/{uploaded['id']}").status_code == 404
    assert send(owner, other, attachment_ids=[uploaded["id"]]).status_code == 400
    with app.state.store.connect() as c:
        row = c.execute(
            "SELECT relative_path FROM files WHERE id=?", (uploaded["id"],)
        ).fetchone()
    assert (app.state.store.workspace(pid) / row[0]).read_bytes() == payload


def test_upload_quota_failure_removes_partial_file(workspace, monkeypatch):
    app, clients, _, pid = workspace
    monkeypatch.setenv("SHUI_PROJECT_UPLOAD_QUOTA", "3")
    response = clients["owner"].post(
        f"/api/projects/{pid}/files", files={"file": ("paper.txt", b"four")}
    )
    assert response.status_code == 413
    assert list((app.state.store.workspace(pid) / "uploads").iterdir()) == []
    assert clients["owner"].get(f"/api/projects/{pid}/files").json()["files"] == []


@pytest.mark.parametrize(
    "name", ["credentials.json", ".env", ".env.production", "id_rsa"]
)
def test_sensitive_upload_names_are_rejected(workspace, name):
    _, clients, _, pid = workspace
    response = clients["owner"].post(
        f"/api/projects/{pid}/files", files={"file": (name, b"secret")}
    )
    assert response.status_code == 400


def test_workspace_traversal_and_symlinks_are_rejected(workspace, tmp_path):
    app, clients, _, pid = workspace
    owner = clients["owner"]
    root = app.state.store.workspace(pid)
    (root / "paper.tex").write_text("Paper")
    (root / ".env").write_text("secret")
    external = tmp_path / "private.txt"
    external.write_text("private")
    (root / "linked.txt").symlink_to(external)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    endpoint = f"/api/projects/{pid}/artifact"
    for path in ("../private.txt", str(external), ".env", "nested/../../private.txt"):
        assert owner.get(endpoint, params={"path": path}).status_code == 400
    for path in ("linked.txt", "linked-dir/private.txt"):
        assert owner.get(endpoint, params={"path": path}).status_code == 403
    entries = owner.get(f"/api/projects/{pid}/workspace").json()["entries"]
    assert {entry["name"] for entry in entries} == {"paper.tex", "uploads"}
    assert owner.get(endpoint, params={"path": "paper.tex"}).content == b"Paper"


def test_upload_directory_symlink_cannot_write_outside_project(workspace, tmp_path):
    app, clients, _, pid = workspace
    uploads = app.state.store.workspace(pid) / "uploads"
    uploads.rmdir()
    outside = tmp_path / "outside-uploads"
    outside.mkdir()
    uploads.symlink_to(outside, target_is_directory=True)
    response = clients["owner"].post(
        f"/api/projects/{pid}/files", files={"file": ("paper.txt", b"content")}
    )
    assert response.status_code in (400, 403)
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_sse_replays_only_later_project_events_and_checks_membership(workspace):
    app, clients, users, pid = workspace
    owner = clients["owner"]
    other = owner.post("/api/projects", json={"name": "Other paper"}).json()["id"]
    with app.state.store.connect() as c:
        prior = app.state.store.event(c, pid, "test.prior", {})
        app.state.store.event(c, other, "test.other-project", {})
        later = app.state.store.event(c, pid, "test.later", {"text": "replay"})
    route = next(
        route for route in app.routes if route.path == "/api/projects/{pid}/events"
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/projects/{pid}/events",
            "headers": [
                (
                    b"cookie",
                    f"{COOKIE}={clients['editor'].cookies.get(COOKIE)}".encode(),
                ),
                (b"last-event-id", str(prior).encode()),
            ],
        },
        receive=receive,
    )
    response = await route.endpoint(pid, request, after=0)
    chunk = await anext(response.body_iterator)
    assert chunk.startswith(f"id: {later}\n")
    assert '"type": "test.later"' in chunk
    assert "test.other-project" not in chunk
    with app.state.store.connect() as c:
        c.execute(
            "DELETE FROM members WHERE project_id=? AND user_id=?",
            (pid, users["editor"]["id"]),
        )
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


@pytest.mark.asyncio
async def test_sse_invalid_cursor_is_rejected(workspace):
    app, clients, _, pid = workspace
    route = next(
        route for route in app.routes if route.path == "/api/projects/{pid}/events"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (
                    b"cookie",
                    f"{COOKIE}={clients['owner'].cookies.get(COOKIE)}".encode(),
                ),
                (b"last-event-id", b"not-an-integer"),
            ],
        }
    )
    with pytest.raises(HTTPException) as exc:
        await route.endpoint(pid, request)
    assert exc.value.status_code == 400


def test_event_batch_replays_deltas_without_cross_project_leaks(workspace):
    app, clients, users, pid = workspace
    owner, editor = clients["owner"], clients["editor"]
    other = owner.post("/api/projects", json={"name": "Other paper"}).json()["id"]
    with app.state.store.connect() as c:
        cursor = app.state.store.event(c, pid, "prior", {})
        app.state.store.event(c, other, "agent.delta", {"delta": "private"})
        next_id = app.state.store.event(c, pid, "agent.delta", {"delta": "shared"})
    path = f"/api/projects/{pid}/event-batch"
    result = editor.get(path, params={"after": cursor})
    assert result.status_code == 200
    assert [(e["id"], e["payload"]["delta"]) for e in result.json()["events"]] == [
        (next_id, "shared")
    ]
    assert editor.get(path, params={"after": next_id}).json()["events"] == []
    with app.state.store.connect() as c:
        c.execute(
            "DELETE FROM members WHERE project_id=? AND user_id=?",
            (pid, users["editor"]["id"]),
        )
    assert editor.get(path).status_code == 404
