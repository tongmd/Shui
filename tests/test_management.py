"""Project resources and centralized membership changes remain owner scoped."""

import pytest
from fastapi.testclient import TestClient

from shui.app import create_app
from shui.store import verify_password


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    primary = store.create_user("owner", "Owner", "owner-password", "owner")
    member = store.create_user("editor", "Editor", "member-password")
    admin = store.create_user("admin", "Admin", "admin-password", "owner")
    clients = []

    def login(user, password):
        client = TestClient(app)
        clients.append(client)
        response = client.post(
            "/api/login", json={"username": user, "password": password}
        )
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf"]
        return client

    owner = login("owner", "owner-password")
    editor = login("editor", "member-password")
    other = login("admin", "admin-password")
    one = owner.post("/api/projects", json={"name": "First paper"}).json()
    two = owner.post("/api/projects", json={"name": "Second paper"}).json()
    yield app, owner, editor, other, primary, member, admin, one, two
    for client in clients:
        client.close()


def test_project_resources_validated_persisted_and_owner_editable(team):
    app, owner, editor, _, _, member, _, one, _ = team
    url = f"/api/projects/{one['id']}"
    links = {
        "github_url": "https://github.com/tongmd/Shui.git/",
        "overleaf_url": "https://overleaf.com/project/0123456789abcdef01234567/",
    }
    response = owner.patch(url, json=links)
    assert response.status_code == 200
    assert response.json()["github_url"] == "https://github.com/tongmd/Shui"
    assert (
        response.json()["overleaf_url"]
        == "https://www.overleaf.com/project/0123456789abcdef01234567"
    )
    assert (
        owner.post(
            url + "/members", json={"username": "editor", "role": "editor"}
        ).status_code
        == 200
    )
    assert (
        editor.get(url).json()["project"]["github_url"]
        == "https://github.com/tongmd/Shui"
    )
    assert editor.patch(url, json={"github_url": ""}).status_code == 403
    assert (
        owner.patch(url, json={"github_url": "", "overleaf_url": None}).status_code
        == 200
    )
    saved = owner.get(url).json()["project"]
    assert saved["github_url"] == saved["overleaf_url"] == ""
    new = owner.post("/api/projects", json={"name": "Linked paper", **links})
    assert (
        new.status_code == 201
        and new.json()["github_url"] == "https://github.com/tongmd/Shui"
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("github_url", "https://evil.example/owner/Shui"),
        ("github_url", "http://github.com/tongmd/Shui"),
        ("github_url", "https://token@github.com/owner/Shui"),
        ("github_url", "https://github.com/tongmd/Shui?token=secret"),
        ("github_url", "https://github.com/tongmd/Shui#fragment"),
        ("github_url", "https://github.com:444/owner/Shui"),
        ("github_url", "https://github.com/tongmd/Shui/tree/main"),
        ("github_url", "https://github.com/tongmd/.."),
        ("overleaf_url", "https://evil.example/project/0123456789abcdef01234567"),
        ("overleaf_url", "javascript:alert(1)"),
        ("overleaf_url", "https://www.overleaf.com/project/short"),
        (
            "overleaf_url",
            "https://www.overleaf.com/project/0123456789abcdef01234567?secret=x",
        ),
        ("github_url", []),
        ("overleaf_url", {}),
    ],
)
def test_bad_resource_link_rejected_without_mutation(team, key, value):
    _, owner, _, _, _, _, _, one, _ = team
    assert (
        owner.patch(f"/api/projects/{one['id']}", json={key: value}).status_code == 400
    )
    assert owner.get(f"/api/projects/{one['id']}").json()["project"][key] == ""


def test_central_membership_assignment_is_atomic_and_revokes_access(team):
    app, owner, editor, other, primary, member, admin, one, two = team
    path = f"/api/users/{member['id']}/memberships"
    assert editor.get(path).status_code == 403
    assert editor.put(path, json={"memberships": []}).status_code == 403
    initial = owner.get(path).json()["memberships"]
    assert len(initial) == 2 and all(row["role"] is None for row in initial)
    assignments = [
        {"project_id": one["id"], "role": "editor"},
        {"project_id": two["id"], "role": "viewer"},
    ]
    response = owner.put(path, json={"memberships": assignments})
    assert response.status_code == 200
    assert {p["id"] for p in editor.get("/api/projects").json()["projects"]} == {
        one["id"],
        two["id"],
    }
    assert (
        editor.get(f"/api/projects/{two['id']}").json()["project"]["role"] == "viewer"
    )
    for invalid in [
        assignments + [assignments[0]],
        [{"project_id": "missing", "role": "editor"}],
        [{"project_id": one["id"], "role": "owner"}],
        {},
        [{"project_id": [], "role": "editor"}],
    ]:
        assert owner.put(path, json={"memberships": invalid}).status_code == 400
        assert len(editor.get("/api/projects").json()["projects"]) == 2
    ownpath = f"/api/users/{primary['id']}/memberships"
    assert owner.put(ownpath, json={"memberships": []}).status_code == 200
    assert all(
        row["role"] == "owner" for row in owner.get(ownpath).json()["memberships"]
    )
    turn = editor.post(
        f"/api/projects/{one['id']}/messages",
        json={"content": "queued", "client_id": "management-queued"},
    ).json()["turn"]
    assert (
        owner.put(
            path, json={"memberships": [{"project_id": two["id"], "role": "viewer"}]}
        ).status_code
        == 200
    )
    assert editor.get(f"/api/projects/{one['id']}").status_code == 404
    with app.state.store.connect() as c:
        assert (
            c.execute("SELECT status FROM turns WHERE id=?", (turn["id"],)).fetchone()[
                0
            ]
            == "cancelled"
        )


def test_account_disable_and_reset_revoke_sessions_without_losing_history(team):
    app, owner, editor, other, primary, member, admin, one, two = team
    path = f"/api/users/{member['id']}"
    assert editor.patch(path, json={"role": "owner"}).status_code == 403
    assert editor.post(path + "/reset-password").status_code == 403
    assert (
        owner.patch(path, json={"display_name": "New name"}).json()["display_name"]
        == "New name"
    )
    assert owner.patch(path, json={"is_active": "false"}).status_code == 400
    assert owner.patch(path, json={"username": "someone"}).status_code == 400
    assert owner.patch(path, json={"is_active": False}).json()["is_active"] is False
    assert editor.get("/api/session").json()["authenticated"] is False
    assert (
        editor.post(
            "/api/login", json={"username": "editor", "password": "member-password"}
        ).status_code
        == 401
    )
    assert owner.patch(path, json={"is_active": True}).status_code == 200
    assert (
        editor.post(
            "/api/login", json={"username": "editor", "password": "member-password"}
        ).status_code
        == 200
    )
    reset = owner.post(path + "/reset-password")
    assert reset.status_code == 200 and reset.json()["must_change_password"] is True
    assert editor.get("/api/projects").status_code == 401
    logged = editor.post(
        "/api/login", json={"username": "editor", "password": "editor"}
    )
    assert (
        logged.status_code == 200
        and logged.json()["user"]["must_change_password"] is True
    )
    assert editor.get("/api/projects").status_code == 403
    with app.state.store.connect() as c:
        assert verify_password(
            "editor",
            c.execute(
                "SELECT password_hash FROM users WHERE id=?", (member["id"],)
            ).fetchone()[0],
        )


def test_primary_owner_and_self_management_are_protected(team):
    _, owner, _, other, primary, _, admin, _, _ = team
    for actor, user in [(owner, primary), (other, admin), (other, primary)]:
        path = f"/api/users/{user['id']}"
        assert actor.patch(path, json={"role": "member"}).status_code == 409
        assert actor.patch(path, json={"is_active": False}).status_code == 409
        assert actor.post(path + "/reset-password").status_code == 409
    assert (
        owner.patch(f"/api/users/{admin['id']}", json={"role": "member"}).status_code
        == 200
    )
    assert other.get("/api/users").status_code == 403


def test_owner_can_delete_project_only_with_exact_name_and_workspace_is_removed(team):
    app, owner, editor, _, _, _, _, one, two = team
    pid = one["id"]
    assert (
        owner.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": "wrong"}
        ).status_code
        == 400
    )
    assert owner.get(f"/api/projects/{pid}").status_code == 200
    assert editor.get(f"/api/projects/{pid}").status_code == 404
    queued = owner.post(
        f"/api/projects/{pid}/messages",
        json={"content": "queued", "client_id": "delete-queued"},
    ).json()["turn"]
    response = owner.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": one["name"]}
    )
    assert response.status_code == 200, response.text
    assert owner.get(f"/api/projects/{pid}").status_code == 404
    assert not app.state.store.workspace(pid).exists()
    with app.state.store.connect() as c:
        assert c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone() is None
        assert (
            c.execute("SELECT 1 FROM turns WHERE id=?", (queued["id"],)).fetchone()
            is None
        )
    assert owner.get(f"/api/projects/{two['id']}").status_code == 200
