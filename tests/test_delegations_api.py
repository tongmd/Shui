"""Collaboration boundaries, durable queue/idempotency, and bounded integration."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from shui.app import create_app


@pytest.fixture
def collab(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    users, clients = {}, {}
    for name in ("owner", "creator", "assignee", "reader", "viewer", "outside"):
        users[name] = app.state.store.create_user(name, name.title(), "test-password", "owner" if name == "owner" else "member")
        client = clients[name] = TestClient(app)
        session = client.post("/api/login", json={"username": name, "password": "test-password"}).json()
        client.headers["x-csrf-token"] = session["csrf"]
    pid = clients["owner"].post("/api/projects", json={"name": "Parent", "provider": "codex", "model": "test-model", "native_session_id": "parent-session-123"}).json()["id"]
    for name in ("creator", "assignee", "reader", "viewer"):
        assert clients["owner"].post(f"/api/projects/{pid}/members", json={"username": name, "role": "viewer" if name == "viewer" else "editor"}).status_code == 200
    yield app, clients, users, pid
    for client in clients.values():
        client.close()


def assign(collab, actor="creator", client_id="assign-request-0001", **overrides):
    app, clients, users, pid = collab
    data = {"content": "Verify the requested implementation", "context": "Only this explicit context", "assignee_ids": [users["assignee"]["id"]], "client_id": client_id, **overrides}
    return clients[actor].post(f"/api/projects/{pid}/delegations", json=data)


def finish(app, task, answer="A bounded final result"):
    with app.state.store.connect() as c:
        c.execute("UPDATE turns SET status='complete' WHERE project_id=?", (task["child_project_id"],))
        c.execute("UPDATE messages SET status='complete',content=CASE WHEN role='assistant' THEN ? ELSE content END WHERE project_id=?", (answer, task["child_project_id"]))
        c.execute("UPDATE projects SET status='idle' WHERE id=?", (task["child_project_id"],))


def merge_url(pid, task):
    return f"/api/projects/{pid}/delegations/{task['id']}/merge"


def test_assignment_has_separate_context_workspace_and_current_permissions(collab):
    app, clients, users, pid = collab
    made = assign(collab)
    assert made.status_code == 201, made.text
    payload = made.json()
    task, turn = payload["delegation"], payload["turn"]
    child = task["child_project_id"]
    assert turn["project_id"] == child and turn["status"] == "queued"
    assert task["can_manage"] and task["assignees"][0]["id"] == users["assignee"]["id"]
    assert payload["message"]["turn_id"] is None
    assert payload["message"]["delegation"]["id"] == task["id"]
    assert app.state.store.workspace(child).is_dir()
    assert app.state.store.workspace(child) != app.state.store.workspace(pid)
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM turns WHERE project_id=?", (pid,)).fetchone()[0] == 0
        assert c.execute("SELECT native_session_id FROM projects WHERE id=?", (pid,)).fetchone()[0] == "parent-session-123"
        assert c.execute("SELECT native_session_id FROM projects WHERE id=?", (child,)).fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM members WHERE project_id=?", (child,)).fetchone()[0] == 0
    for name in ("owner", "creator", "assignee", "reader", "viewer"):
        detail = clients[name].get(f"/api/projects/{child}")
        assert detail.status_code == 200
        project = detail.json()["project"]
        assert project["parent_project_id"] == pid
        assert project["model"] == "test-model"
        assert project["role"] == ("owner" if name == "owner" else "editor" if name in ("creator", "assignee") else "viewer")
        assert {m["id"] for m in detail.json()["members"]} == {users[n]["id"] for n in ("owner", "creator", "assignee", "reader", "viewer")}
        assert all(p["id"] != child for p in clients[name].get("/api/projects").json()["projects"])
    assert clients["outside"].get(f"/api/projects/{child}").status_code == 404
    for name, status in (("assignee", 201), ("reader", 403), ("viewer", 403), ("outside", 404)):
        assert clients[name].post(f"/api/projects/{child}/messages", json={"content": "Follow-up", "client_id": f"discussion-{name}"}).status_code == status
    for name in ("reader", "viewer"):
        listed = clients[name].get(f"/api/projects/{pid}/delegations").json()
        assert not listed["delegations"][0]["can_manage"]
        assert {m["id"] for m in listed["assignable_members"]} == {users[n]["id"] for n in ("owner", "creator", "assignee", "reader")}
    events = clients["viewer"].get(f"/api/projects/{pid}/event-batch").json()["events"]
    assert all("can_manage" not in json.dumps(e["payload"]) for e in events)
    assert "Follow-up" not in json.dumps(events)


def test_idempotency_is_atomic_cross_user_and_cross_endpoint(collab):
    app, clients, users, pid = collab
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: assign(collab), range(4)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()["delegation"]["id"] for r in results}) == 1
    assert assign(collab, actor="assignee").status_code == 409
    assert assign(collab, content="Different task with reused id").status_code == 409
    assert clients["creator"].post(f"/api/projects/{pid}/messages", json={"content": "Message", "client_id": "assign-request-0001"}).status_code == 409
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert len(list((app.state.store.root / "projects").iterdir())) == 2


def test_validation_csrf_bridge_scope_and_cap(collab):
    app, clients, users, pid = collab
    for override in ({"assignee_ids": []}, {"assignee_ids": [users["outside"]["id"]]}, {"assignee_ids": [users["viewer"]["id"]]}, {"assignee_ids": [users["assignee"]["id"]] * 2}, {"assignee_ids": "no"}, {"assignee_ids": [{}]}, {"attachment_ids": [{}]}, {"attachment_ids": ["0" * 32]}, {"content": " "}, {"content": "x" * 12001}, {"context": "x" * 8001}, {"client_id": "short"}, {"unexpected": True}):
        assert assign(collab, **override).status_code == 400
    assert assign(collab, actor="viewer").status_code == 403
    assert assign(collab, actor="outside").status_code == 404
    response = clients["creator"].post(f"/api/projects/{pid}/delegations", headers={"x-csrf-token": ""}, json={})
    assert response.status_code == 403
    with app.state.store.connect() as c:
        c.execute("UPDATE projects SET provider='bridge' WHERE id=?", (pid,))
    assert assign(collab).status_code == 409
    with app.state.store.connect() as c:
        c.execute("UPDATE projects SET provider='codex' WHERE id=?", (pid,))
    for i in range(8):
        assert assign(collab, client_id=f"task-limit-{i:02d}").status_code == 201
    assert assign(collab).status_code == 429
    assert assign(collab, client_id="task-limit-00").status_code == 201


def test_child_cannot_be_administered_as_independent_project_or_nest(collab):
    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    child = task["child_project_id"]
    base = f"/api/projects/{child}"
    owner = clients["owner"]
    assert owner.patch(base, json={"name": "Separate ownership"}).status_code == 409
    assert owner.post(base + "/members", json={"username": "outside"}).status_code == 409
    assert owner.delete(base + "/members/" + users["assignee"]["id"]).status_code == 409
    assert owner.request("DELETE", base, json={"confirm_name": task["title"]}).status_code == 409
    assert owner.post(base + "/delegations", json={"content": "Nested", "assignee_ids": [users["assignee"]["id"]], "client_id": "nested-task-0001"}).status_code == 409
    assert owner.put(f"/api/users/{users['outside']['id']}/memberships", json={"memberships": [{"project_id": child, "role": "editor"}]}).status_code == 409
    other = owner.post("/api/projects", json={"name": "Unrelated"}).json()["id"]
    assert owner.get(f"/api/projects/{other}/delegations/{task['id']}").status_code == 404


def test_attachment_is_validated_reference_no_implicit_copy(collab):
    app, clients, users, pid = collab
    owner = clients["owner"]
    uploaded = owner.post(f"/api/projects/{pid}/files", files={"file": ("plan.txt", b"Explicit input")}).json()
    task = assign(collab, attachment_ids=[uploaded["id"]]).json()["delegation"]
    assert task["attachments"][0]["project_id"] == pid
    assert task["attachment_ids"] == [uploaded["id"]]
    assert list((app.state.store.workspace(task["child_project_id"]) / "uploads").iterdir()) == []
    other = owner.post("/api/projects", json={"name": "Unrelated"}).json()["id"]
    foreign = owner.post(f"/api/projects/{other}/files", files={"file": ("private.txt", b"Do not share")}).json()
    assert assign(collab, client_id="foreign-files-01", attachment_ids=[foreign["id"]]).status_code == 400
    assert clients["assignee"].post(f"/api/projects/{task['child_project_id']}/messages", json={"content": "No cross-project attachment shortcut", "client_id": "child-attach-01", "attachment_ids": [uploaded["id"]]}).status_code == 400


def test_revocation_is_dynamic_and_stops_author_queue(collab):
    app, clients, users, pid = collab
    task = assign(collab, actor="assignee").json()["delegation"]
    child = task["child_project_id"]
    owner = clients["owner"]
    assert owner.post(f"/api/projects/{pid}/members", json={"username": "assignee", "role": "viewer"}).status_code == 200
    assert clients["assignee"].get(f"/api/projects/{child}").json()["project"]["role"] == "viewer"
    assert clients["assignee"].post(merge_url(pid, task), json={"summary": "No access", "client_id": "denied-merge-01"}).status_code == 403
    with app.state.store.connect() as c:
        assert c.execute("SELECT status FROM turns WHERE project_id=?", (child,)).fetchone()[0] == "cancelled"
    assert owner.delete(f"/api/projects/{pid}/members/{users['assignee']['id']}").status_code == 200
    for suffix in ("", "/messages", "/files", "/workspace", "/event-batch", "/members"):
        assert clients["assignee"].get(f"/api/projects/{child}" + suffix).status_code == 404
    # A stale user object or an existing assignee row cannot bypass inactive-account checks.
    with app.state.store.connect() as c:
        c.execute("UPDATE users SET is_active=0 WHERE id=?", (users["creator"]["id"],))
        assert app.state.store.project_role(c, child, users["creator"]) is None


def test_merge_is_bounded_once_and_closes_child(collab):
    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    child, endpoint = task["child_project_id"], merge_url(pid, task)
    assert clients["creator"].post(endpoint, json={"client_id": "merge-request-0001"}).status_code == 409
    finish(app, task, "Final result. " + "x" * 18000)
    with app.state.store.connect() as c:
        c.execute("UPDATE messages SET content=? WHERE project_id=? AND role='user'", ("PRIVATE CHILD DISCUSSION NEVER REPLAY", child))
    assert clients["reader"].post(endpoint, json={"client_id": "merge-denied-0001"}).status_code == 403
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda i: clients["assignee"].post(endpoint, json={"client_id": f"merge-request-{i:04d}"}), range(4)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()["turn"]["id"] for r in responses}) == 1
    merged = responses[0].json()
    assert merged["delegation"]["status"] == "completed"
    assert merged["delegation"]["merge_turn"]["status"] == "queued"
    assert len(merged["delegation"]["summary"]) == 12000
    assert merged["message"]["merge_delegation"]["id"] == task["id"]
    events = clients["viewer"].get(f"/api/projects/{pid}/event-batch").json()["events"]
    published = [e["payload"]["message"] for e in events if e["type"] == "message.created" and e["payload"]["message"]["id"] == merged["message"]["id"]]
    assert len(published) == 1 and published[0]["merge_delegation"]["id"] == task["id"]
    assert "can_manage" not in published[0]["merge_delegation"]
    assert "PRIVATE CHILD DISCUSSION" not in merged["message"]["content"]
    assert str(app.state.store.workspace(child)) in merged["message"]["content"]
    assert len(merged["message"]["content"]) < 14000
    assert clients["creator"].post(f"/api/projects/{child}/messages", json={"content": "After finish", "client_id": "after-closed-01"}).status_code == 409
    assert clients["assignee"].post(f"/api/projects/{child}/files", files={"file": ("late.txt", b"late")}).status_code == 409
    assert clients["creator"].post(endpoint.replace("/merge", "/cancel")).status_code == 409
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM turns WHERE project_id=?", (pid,)).fetchone()[0] == 1
    parent_messages = clients["owner"].get(f"/api/projects/{pid}/messages").json()["messages"]
    assert parent_messages[0]["delegation"]["status"] == "completed"


def test_retry_merge_updates_task_and_blocks_old_attempt(collab):
    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    finish(app, task)
    original = clients["creator"].post(merge_url(pid, task), json={"client_id": "merge-original-01"}).json()["turn"]
    with app.state.store.connect() as c:
        c.execute("UPDATE turns SET status='failed' WHERE id=?", (original["id"],))
        c.execute("UPDATE messages SET status='failed' WHERE turn_id=?", (original["id"],))
    retry = clients["creator"].post(f"/api/projects/{pid}/turns/{original['id']}/retry", json={"client_id": "merge-retry-0001"})
    assert retry.status_code == 201, retry.text
    assert retry.json()["message"]["merge_delegation"]["id"] == task["id"]
    current = clients["creator"].post(merge_url(pid, task), json={"client_id": "merge-repeated-01"}).json()
    assert current["turn"]["id"] == retry.json()["turn"]["id"]
    assert current["delegation"]["merge_turn_id"] == retry.json()["turn"]["id"]
    assert clients["creator"].post(f"/api/projects/{pid}/turns/{original['id']}/retry", json={"client_id": "merge-retry-0002"}).status_code == 409
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM delegation_merge_attempts").fetchone()[0] == 2


def test_cancel_preserves_transcript_and_parent_queue_then_delete_cleans_tree(collab):
    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    child = task["child_project_id"]
    parent_turn = clients["owner"].post(f"/api/projects/{pid}/messages", json={"content": "Main task", "client_id": "parent-queue-01"}).json()["turn"]
    endpoint = merge_url(pid, task).replace("/merge", "/cancel")
    assert clients["reader"].post(endpoint).status_code == 403
    assert clients["assignee"].post(endpoint).status_code == 200
    assert clients["creator"].post(endpoint).status_code == 200
    assert clients["viewer"].get(f"/api/projects/{child}/messages").status_code == 200
    assert clients["creator"].post(merge_url(pid, task), json={"summary": "No merge", "client_id": "cancel-merge-01"}).status_code == 409
    with app.state.store.connect() as c:
        assert c.execute("SELECT status FROM turns WHERE id=?", (parent_turn["id"],)).fetchone()[0] == "queued"
        assert c.execute("SELECT status FROM turns WHERE project_id=?", (child,)).fetchone()[0] == "cancelled"
    second = assign(collab, client_id="second-task-0001").json()["delegation"]
    finish(app, second)
    clients["creator"].post(merge_url(pid, second), json={"client_id": "delete-merged-01"})
    assert clients["owner"].request("DELETE", f"/api/projects/{pid}", json={"confirm_name": "Parent"}).status_code == 200
    for child_id in (child, second["child_project_id"], pid):
        assert not app.state.store.workspace(child_id).exists()
        assert clients["owner"].get(f"/api/projects/{child_id}").status_code == 404
    with app.state.store.connect() as c:
        for table in ("delegations", "delegation_assignees", "delegation_merge_attempts", "projects", "messages", "turns", "events"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merged_artifact_links_keep_child_scope_and_download(collab, tmp_path):
    from urllib.parse import quote

    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    child = task["child_project_id"]
    root = app.state.store.workspace(child)
    target = root / "result (final).pdf"
    target.write_bytes(b"%PDF-1.4 child result")
    (root / "simple.pdf").write_bytes(b"child relative artifact")
    (app.state.store.workspace(pid) / "simple.pdf").write_bytes(b"different parent artifact")
    (app.state.store.workspace(pid) / "missing-child.pdf").write_bytes(b"must not return this stale parent artifact")
    (tmp_path / "private.txt").write_text("private")
    (root / "unsafe.txt").symlink_to(tmp_path / "private.txt")
    summary = (
        f"[Relative](simple.pdf)\n[Absolute](<{target}>)\n"
        f"[File URL](<{target.as_uri()}>)\n[Line](simple.pdf:12)\n"
        "[External](https://example.com/report.pdf)\n"
        "`[Code](simple.pdf)`\n[Unsafe](unsafe.txt)\n[Missing](missing-child.pdf)"
    )
    finish(app, task, summary)
    result = clients["creator"].post(merge_url(pid, task), json={"client_id": "artifact-merge-01"}).json()
    content = result["message"]["content"]
    simple = f"/api/projects/{child}/artifact?path=simple.pdf"
    escaped = f"/api/projects/{child}/artifact?path={quote(target.name, safe='')}"
    assert f"[Relative]({simple})" in content
    assert f"[Absolute]({escaped})" in content
    assert f"[File URL]({escaped})" in content
    assert f"[Line]({simple})" in content
    assert "[External](https://example.com/report.pdf)" in content
    assert "`[Code](simple.pdf)`" in content
    assert f"/api/projects/{child}/artifact?path=unsafe.txt" not in content
    assert "[Missing](missing-child.pdf)" not in content
    assert "[Unsafe](unsafe.txt)" not in content
    assert "unavailable collaboration artifact" in content
    assert clients["viewer"].get(simple).content == b"child relative artifact"
    assert clients["viewer"].get(escaped).content == target.read_bytes()
    assert clients["outside"].get(escaped).status_code == 404


def test_failed_creation_rolls_back_workspace_and_database(collab, monkeypatch):
    app, clients, users, pid = collab
    original = app.state.store.enqueue_turn

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Injected failure before transaction commit")

    monkeypatch.setattr(app.state.store, "enqueue_turn", fail_after_insert)
    with pytest.raises(RuntimeError, match="Injected failure"):
        assign(collab)
    assert [p.name for p in (app.state.store.root / "projects").iterdir()] == [pid]
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        for table in ("delegations", "delegation_assignees", "turns", "messages"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["create", "merge", "delete", "research"])
async def test_permissions_are_rechecked_after_streamed_body(collab, operation):
    import httpx
    from shui.app import COOKIE

    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"] if operation in ("merge", "research") else None
    actor = "owner" if operation == "delete" else "creator"
    payload = {"content": "New task", "assignee_ids": [users["assignee"]["id"]], "client_id": "slow-body-task-01"}
    method, path = "POST", f"/api/projects/{pid}/delegations"
    if operation == "merge":
        finish(app, task)
        payload, path = {"summary": "Result", "client_id": "slow-body-merge01"}, merge_url(pid, task)
    elif operation == "delete":
        method, path, payload = "DELETE", f"/api/projects/{pid}", {"confirm_name": "Parent"}
    elif operation == "research":
        path = f"/api/projects/{task['child_project_id']}/research"
        payload = {"experiments": [{"name": "Late edit"}]}
    encoded = json.dumps(payload).encode()

    async def chunks():
        yield encoded[:10]
        with app.state.store.connect() as c:
            if operation == "research":
                c.execute("UPDATE delegations SET status='cancelled' WHERE id=?", (task["id"],))
            elif operation == "delete":
                c.execute("UPDATE users SET is_active=0 WHERE id=?", (users[actor]["id"],))
            else:
                c.execute("DELETE FROM sessions WHERE user_id=?", (users[actor]["id"],))
        yield encoded[10:]

    headers = {"content-type": "application/json", "x-csrf-token": clients[actor].headers["x-csrf-token"], "cookie": f"{COOKIE}={clients[actor].cookies.get(COOKIE)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.request(method, path, content=chunks(), headers=headers)
    assert response.status_code == (409 if operation == "research" else 401), response.text
    with app.state.store.connect() as c:
        assert c.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
        assert c.execute("SELECT COUNT(*) FROM turns WHERE project_id=?", (pid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 0


@pytest.mark.parametrize("via_membership", [False, True])
def test_cancelled_integration_status_is_published_to_task_card(collab, via_membership):
    app, clients, users, pid = collab
    task = assign(collab).json()["delegation"]
    finish(app, task)
    turn = clients["creator"].post(merge_url(pid, task), json={"client_id": "cancel-merge-01"}).json()["turn"]
    if via_membership:
        response = clients["owner"].post(f"/api/projects/{pid}/members", json={"username": "creator", "role": "viewer"})
    else:
        response = clients["creator"].post(f"/api/projects/{pid}/turns/{turn['id']}/cancel")
    assert response.status_code == 200
    events = clients["viewer"].get(f"/api/projects/{pid}/event-batch").json()["events"]
    last = [event for event in events if event["type"] == "delegation.updated"][-1]
    assert last["payload"]["delegation"]["merge_turn"]["status"] == "cancelled"
    assert last["payload"]["delegation"]["status"] == "completed"
