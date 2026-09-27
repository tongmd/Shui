"""First-login credentials must never grant access to a shared agent workspace."""

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import shui.app as app_module
from shui.app import COOKIE, create_app
from shui.store import Store, now, password_hash, verify_password

INITIAL_USERNAME = "newperson"
NEW_PASSWORD = "new-private-password-2937"


def sign_in(app, username, password):
    client = TestClient(app)
    result = client.post(
        "/api/login", json={"username": username, "password": password}
    )
    assert result.status_code == 200, result.text
    client.headers["x-csrf-token"] = result.json()["csrf"]
    client.headers["origin"] = "http://testserver"
    return client, result.json()


def assert_password_gate(response):
    assert response.status_code == 403, response.text
    payload = response.json()
    detail = payload.get("detail")
    code = payload.get("code")
    if isinstance(detail, dict):
        code = code or detail.get("code")
    assert code == "password_change_required", payload


def change_password(client, **overrides):
    body = {
        "current_password": INITIAL_USERNAME,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    body.update(overrides)
    return client.post("/api/password", json=body)


@pytest.fixture
def first_login(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    app.state.store.create_user(
        "owner", "Owner", "owner-custom-password", "owner"
    )
    owner_client, _ = sign_in(app, "owner", "owner-custom-password")
    response = owner_client.post(
        "/api/users", json={"username": INITIAL_USERNAME, "display_name": "New Person"}
    )
    assert response.status_code == 201, response.text
    user = response.json()
    project = owner_client.post("/api/projects", json={"name": "Shared paper"})
    assert project.status_code == 201, project.text
    pid = project.json()["id"]
    assert owner_client.post(
        f"/api/projects/{pid}/members",
        json={"username": INITIAL_USERNAME, "role": "editor"},
    ).status_code == 200
    client, login = sign_in(app, INITIAL_USERNAME, INITIAL_USERNAME)
    yield app, owner_client, client, user, pid, login
    owner_client.close()
    client.close()


@pytest.mark.parametrize("username", ["xy", "editor"])
def test_owner_creates_username_password_without_password_field(first_login, username):
    app, owner, _, _, _, _ = first_login
    created = owner.post("/api/users", json={"username": username})
    assert created.status_code == 201, created.text
    assert created.json()["must_change_password"] is True
    assert created.json()["role"] == "member"
    assert "password" not in created.json()
    assert "password_hash" not in created.json()
    client, result = sign_in(app, username, username)
    try:
        assert result["user"]["must_change_password"] is True
        assert result["providers"] == []
    finally:
        client.close()
    with app.state.store.connect() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        assert row["password_hash"] != username
        assert verify_password(username, row["password_hash"])


def test_owner_supplied_password_is_not_used_for_new_member(first_login):
    app, owner, _, _, _, _ = first_login
    response = owner.post(
        "/api/users",
        json={"username": "colleague", "password": "do-not-use-this-password"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["must_change_password"] is True
    with TestClient(app) as client:
        rejected = client.post(
            "/api/login",
            json={"username": "colleague", "password": "do-not-use-this-password"},
        )
        assert rejected.status_code == 401
    client, _ = sign_in(app, "colleague", "colleague")
    client.close()


def test_pending_login_and_session_only_expose_password_change_state(first_login):
    _, owner, client, user, _, login = first_login
    assert user["must_change_password"] is True
    for payload in (login, client.get("/api/session").json()):
        assert payload["authenticated"] is True
        assert payload["user"]["must_change_password"] is True
        assert payload["providers"] == []
        assert "password_hash" not in json.dumps(payload)
    owner_session = owner.get("/api/session").json()
    assert owner_session["user"]["role"] == "owner"
    assert owner_session["user"]["must_change_password"] is False
    assert {p["id"] for p in owner_session["providers"]} == {"codex", "claude"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/users",
        "/api/users/nonexistent/memberships",
        "/api/projects",
        "/api/projects/{pid}",
        "/api/projects/{pid}/messages",
        "/api/projects/{pid}/members",
        "/api/projects/{pid}/files",
        "/api/projects/{pid}/files/nonexistent",
        "/api/projects/{pid}/workspace",
        "/api/projects/{pid}/artifact?path=paper.tex",
        "/api/projects/{pid}/activity",
        "/api/projects/{pid}/event-batch",
    ],
)
def test_pending_user_cannot_read_project_or_admin_apis(first_login, path):
    _, _, client, _, pid, _ = first_login
    assert_password_gate(client.get(path.format(pid=pid)))


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/users", {"username": "another"}),
        ("patch", "/api/users/nonexistent", {"is_active": False}),
        ("post", "/api/users/nonexistent/reset-password", {}),
        ("put", "/api/users/nonexistent/memberships", {"memberships": []}),
        ("post", "/api/projects", {"name": "Should not run"}),
        ("patch", "/api/projects/{pid}", {"name": "Cannot change"}),
        (
            "post",
            "/api/projects/{pid}/messages",
            {"content": "Run an experiment", "client_id": "password-gate-01"},
        ),
        ("post", "/api/projects/{pid}/members", {"username": "owner", "role": "editor"}),
        ("delete", "/api/projects/{pid}/members/nonexistent", {}),
        ("post", "/api/projects/{pid}/turns/nonexistent/cancel", {}),
        (
            "post",
            "/api/projects/{pid}/turns/nonexistent/retry",
            {"client_id": "password-retry-01"},
        ),
        ("post", "/api/projects/{pid}/presence", {}),
    ],
)
def test_pending_user_cannot_mutate_project_or_admin_apis(first_login, method, path, body):
    app, _, client, _, pid, _ = first_login
    assert_password_gate(client.request(method, path.format(pid=pid), json=body))
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_pending_user_cannot_upload_files(first_login):
    app, _, client, _, pid, _ = first_login
    assert_password_gate(client.post(
        f"/api/projects/{pid}/files", files={"file": ("paper.txt", b"not allowed")}
    ))
    assert list((app.state.store.workspace(pid) / "uploads").iterdir()) == []


@pytest.mark.asyncio
async def test_pending_user_cannot_open_event_stream(first_login):
    app, _, client, _, pid, _ = first_login
    route = next(r for r in app.routes if r.path == "/api/projects/{pid}/events")
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/projects/{pid}/events",
        "headers": [(b"cookie", f"{COOKIE}={client.cookies.get(COOKIE)}".encode())],
    })
    with pytest.raises(HTTPException) as exc:
        await route.endpoint(pid, request, after=0)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("field,value", [
    ("current_password", "wrong-password"),
    ("current_password", None),
    ("new_password", "short"),
    ("new_password", "x" * 257),
    ("new_password", INITIAL_USERNAME),
    ("new_password", INITIAL_USERNAME.upper()),
    ("new_password", None),
    ("confirm_password", "does-not-match"),
    ("confirm_password", None),
])
def test_invalid_password_change_does_not_unlock_account(first_login, field, value):
    app, _, client, user, _, _ = first_login
    data = {field: value}
    if field == "new_password":
        data["confirm_password"] = value
    response = change_password(client, **data)
    assert response.status_code in (400, 401, 422), response.text
    assert_password_gate(client.get("/api/projects"))
    with app.state.store.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        assert row["must_change_password"]
        assert verify_password(INITIAL_USERNAME, row["password_hash"])


@pytest.mark.parametrize("missing", ["current_password", "new_password", "confirm_password"])
def test_password_change_requires_all_fields(first_login, missing):
    _, _, client, _, _, _ = first_login
    data = {
        "current_password": INITIAL_USERNAME,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }
    del data[missing]
    response = client.post("/api/password", json=data)
    assert response.status_code in (400, 401, 422), response.text
    assert_password_gate(client.get("/api/projects"))


@pytest.mark.parametrize("headers", [
    {"x-csrf-token": ""},
    {"x-csrf-token": "invalid"},
    {"origin": "https://another-site.example"},
])
def test_password_change_requires_csrf_and_same_origin(first_login, headers):
    _, _, client, _, _, _ = first_login
    response = client.post(
        "/api/password",
        json={
            "current_password": INITIAL_USERNAME,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
        headers=headers,
    )
    assert response.status_code == 403, response.text
    assert_password_gate(client.get("/api/projects"))


def test_password_change_unlocks_rotates_session_and_invalidates_all_old_sessions(first_login):
    app, _, client, user, pid, _ = first_login
    other, _ = sign_in(app, INITIAL_USERNAME, INITIAL_USERNAME)
    old_cookie = client.cookies.get(COOKIE)
    old_csrf = client.headers["x-csrf-token"]
    other_cookie = other.cookies.get(COOKIE)
    response = change_password(client)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["user"]["must_change_password"] is False
    assert payload["user"]["id"] == user["id"]
    assert payload["csrf"] != old_csrf
    assert client.cookies.get(COOKIE) not in (old_cookie, other_cookie)
    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert other.get("/api/projects").status_code == 401
    assert other.get("/api/session").json()["authenticated"] is False
    other.close()
    with TestClient(app) as stolen:
        stolen.cookies.set(COOKIE, old_cookie)
        assert stolen.get("/api/projects").status_code == 401
    assert client.post(f"/api/projects/{pid}/presence").status_code == 403
    client.headers["x-csrf-token"] = payload["csrf"]
    assert client.post(f"/api/projects/{pid}/presence").status_code == 200
    with app.state.store.connect() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        assert row["must_change_password"] == 0
        assert verify_password(NEW_PASSWORD, row["password_hash"])
        assert not verify_password(INITIAL_USERNAME, row["password_hash"])
        sessions = c.execute("SELECT * FROM sessions WHERE user_id=?", (user["id"],)).fetchall()
        assert len(sessions) == 1
        assert sessions[0]["token_hash"] == hashlib.sha256(client.cookies.get(COOKIE).encode()).hexdigest()
    for path in app.state.store.root.glob("shui.sqlite*"):
        assert NEW_PASSWORD.encode() not in path.read_bytes()
    with TestClient(app) as fresh:
        assert fresh.post("/api/login", json={
            "username": INITIAL_USERNAME, "password": INITIAL_USERNAME,
        }).status_code == 401
        signed_in = fresh.post("/api/login", json={
            "username": INITIAL_USERNAME, "password": NEW_PASSWORD,
        })
        assert signed_in.status_code == 200
        assert signed_in.json()["user"]["must_change_password"] is False


@pytest.mark.parametrize("length", [8, 256])
def test_new_password_accepts_documented_length_bounds(first_login, length):
    _, _, client, _, _, _ = first_login
    password = "P" * length
    response = change_password(client, new_password=password, confirm_password=password)
    assert response.status_code == 200, response.text


def test_established_password_cannot_be_reused(first_login):
    _, owner, _, _, _, _ = first_login
    response = change_password(
        owner,
        current_password="owner-custom-password",
        new_password="owner-custom-password",
        confirm_password="owner-custom-password",
    )
    assert response.status_code == 400, response.text
    assert owner.get("/api/projects").status_code == 200


def test_concurrent_changes_cannot_both_consume_the_initial_password(first_login):
    app, _, client, user, _, _ = first_login
    other, _ = sign_in(app, INITIAL_USERNAME, INITIAL_USERNAME)
    barrier = threading.Barrier(2)
    passwords = ["first-private-password", "second-private-password"]

    def submit(session, password):
        barrier.wait(timeout=5)
        return change_password(
            session, new_password=password, confirm_password=password
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, session, password)
                for session, password in zip((client, other), passwords)
            ]
            responses = [f.result(timeout=15) for f in futures]
        assert sum(r.status_code == 200 for r in responses) == 1
        assert all(r.status_code in (200, 400, 401, 409) for r in responses)
        winner = next(i for i, r in enumerate(responses) if r.status_code == 200)
        with app.state.store.connect() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            assert verify_password(passwords[winner], row["password_hash"])
            assert not verify_password(passwords[1 - winner], row["password_hash"])
            assert c.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?", (user["id"],)
            ).fetchone()[0] == 1
    finally:
        other.close()


def test_password_change_requires_a_signed_in_session(first_login):
    app, _, _, _, _, _ = first_login
    with TestClient(app) as anonymous:
        assert change_password(anonymous).status_code == 401


@pytest.mark.parametrize("revocation", [{"role": "member"}, {"is_active": False}])
def test_owner_revoked_during_password_hash_cannot_reset_another_account(
    first_login, monkeypatch, revocation
):
    app, owner, _, target, _, _ = first_login
    second_owner = app.state.store.create_user(
        "secondowner", "Second Owner", "second-owner-password", "owner"
    )
    actor, _ = sign_in(app, "secondowner", "second-owner-password")
    with app.state.store.connect() as c:
        old_hash = c.execute(
            "SELECT password_hash FROM users WHERE id=?", (target["id"],)
        ).fetchone()[0]
    entered = threading.Event()
    release = threading.Event()
    original_hash = app_module.password_hash

    def blocked_hash(password, *args, **kwargs):
        if password == target["username"]:
            entered.set()
            assert release.wait(timeout=5), "Password hash was not released"
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(app_module, "password_hash", blocked_hash)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                actor.post, f"/api/users/{target['id']}/reset-password"
            )
            try:
                assert entered.wait(timeout=5), "Password reset did not start"
                changed = owner.patch(
                    f"/api/users/{second_owner['id']}", json=revocation
                )
                assert changed.status_code == 200, changed.text
            finally:
                release.set()
            result = future.result(timeout=10)
        assert result.status_code in (401, 403), result.text
        with app.state.store.connect() as c:
            assert c.execute(
                "SELECT password_hash FROM users WHERE id=?", (target["id"],)
            ).fetchone()[0] == old_hash
    finally:
        actor.close()


def test_pending_user_can_log_out(first_login):
    _, _, client, _, _, _ = first_login
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/session").json()["authenticated"] is False


def test_current_database_flag_is_checked_for_existing_session(first_login):
    app, owner, _, _, _, _ = first_login
    assert owner.get("/api/projects").status_code == 200
    with app.state.store.connect() as c:
        c.execute("UPDATE users SET must_change_password=1 WHERE username='owner'")
    assert_password_gate(owner.get("/api/projects"))
    assert owner.get("/api/session").json()["user"]["must_change_password"] is True


def test_migration_preserves_custom_owner_and_restricts_legacy_initial_passwords(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    root = tmp_path / "legacy"
    root.mkdir()
    legacy_cookie = "legacy-session-token"
    owner_hash = password_hash("unchanged-owner-password")
    initial_hash = password_hash("editor")
    with sqlite3.connect(root / "shui.sqlite") as c:
        c.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('owner','member')),created_at TEXT NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
        """)
        c.executemany("INSERT INTO users VALUES(?,?,?,?,?,?)", [
            ("owner-id", "owner", "Owner", owner_hash, "owner", now()),
            ("member-id", "editor", "Editor", initial_hash, "member", now()),
        ])
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (
            hashlib.sha256(legacy_cookie.encode()).hexdigest(),
            "member-id", "legacy-csrf-token", time.time() + 3600,
        ))
    app = create_app(root, start_worker=False)
    with app.state.store.connect() as c:
        owner = c.execute("SELECT * FROM users WHERE username='owner'").fetchone()
        member = c.execute("SELECT * FROM users WHERE username='editor'").fetchone()
        assert owner["password_hash"] == owner_hash
        assert owner["must_change_password"] == 0
        assert member["password_hash"] == initial_hash
        assert member["must_change_password"] == 1
    # A session issued before deployment must respect the migrated flag too.
    with TestClient(app) as legacy:
        legacy.cookies.set(COOKIE, legacy_cookie)
        legacy.headers["x-csrf-token"] = "legacy-csrf-token"
        legacy.headers["origin"] = "http://testserver"
        assert_password_gate(legacy.get("/api/projects"))
        assert legacy.get("/api/session").json()["user"]["must_change_password"] is True
        response = change_password(legacy, current_password="editor")
        assert response.status_code == 200, response.text
    owner_client, owner_session = sign_in(app, "owner", "unchanged-owner-password")
    assert owner_session["user"]["role"] == "owner"
    assert owner_session["user"]["must_change_password"] is False
    owner_client.close()
    # Restarting cannot reset the completed password or re-enable the old one.
    restarted = Store(root)
    with restarted.connect() as c:
        row = c.execute("SELECT * FROM users WHERE username='editor'").fetchone()
        assert row["must_change_password"] == 0
        assert verify_password(NEW_PASSWORD, row["password_hash"])
        assert not verify_password("editor", row["password_hash"])
