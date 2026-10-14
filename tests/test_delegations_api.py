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
    assert assign(collab).status_code == 201  # Repeat mentions share one open task.
    for index in range(8):
        member = app.state.store.create_user(f"limit-member-{index}", "Limit member", "test-password")
        assert clients["owner"].post(f"/api/projects/{pid}/members", json={"username": member["username"]}).status_code == 200
        response = assign(collab, client_id=f"limit-member-{index}", assignee_ids=[member["id"]])
        assert response.status_code == (201 if index < 7 else 429)
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
    child_messages = clients["assignee"].get(f"/api/projects/{task['child_project_id']}/messages").json()["messages"]
    assignment = next(message for message in child_messages if message["role"] == "user")
    assert assignment["delegation_request"]["context"] == "Only this explicit context"
    assert assignment["attachments"][0]["download_url"] == uploaded["download_url"]
    assert assignment["delegation_id"] == task["id"]
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


def test_deleting_project_cascades_private_delegation_credentials(collab):
    app, clients, users, pid = collab
    made = assign(
        collab, content="Use token:synthetic-message-9Wp2",
        title="Use token:synthetic-title-8Xq3",
        context="Use token:synthetic-context-7Yr4",
    )
    assert made.status_code == 201
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM message_secrets").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM delegation_request_secrets").fetchone()[0] == 2
    response = clients["owner"].request("DELETE", f"/api/projects/{pid}", json={"confirm_name": "Parent"})
    assert response.status_code == 200
    with app.state.store.connect() as c:
        for table in ("message_secrets", "delegation_request_secrets", "delegation_requests", "projects"):
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


def test_member_session_reuses_open_task_and_retains_each_request_brief(collab):
    app, clients, users, pid = collab
    first = assign(collab).json()
    task = first["delegation"]
    child = task["child_project_id"]
    with app.state.store.connect() as c:
        c.execute("UPDATE projects SET native_session_id='member-native-123' WHERE id=?", (child,))
    second = assign(collab, actor="reader", client_id="second-mention-01", title="Another request", content="A different task", context="Second brief only").json()
    assert second["delegation"]["id"] == task["id"]
    assert second["delegation"]["child_project_id"] == child
    assert second["turn"]["id"] != first["turn"]["id"]
    assert second["message"]["id"] != first["message"]["id"]
    assert second["message"]["delegation_request"]["title"] == "Another request"
    assert set(second["delegation"]["creator_ids"]) == {users["creator"]["id"], users["reader"]["id"]}
    assert second["delegation"]["request_count"] == 2
    assert clients["reader"].get(f"/api/projects/{child}").json()["project"]["role"] == "editor"
    with app.state.store.connect() as c:
        assert c.execute("SELECT native_session_id FROM projects WHERE id=?", (child,)).fetchone()[0] == "member-native-123"
        assert c.execute("SELECT COUNT(*) FROM collaboration_sessions").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert app.state.store.delegation_request_for_turn(c, first["turn"]["id"])["context"] == "Only this explicit context"
        assert app.state.store.delegation_request_for_turn(c, second["turn"]["id"])["context"] == "Second brief only"
    events = clients["viewer"].get(f"/api/projects/{child}/event-batch").json()["events"]
    assert [e for e in events if e["type"] == "delegation.updated"][-1]["payload"]["delegation"]["request_count"] == 2
    details = clients["creator"].get(f"/api/projects/{pid}/delegations").json()
    assert len(details["sessions"]) == 1 and details["sessions"][0]["assignee_id"] == users["assignee"]["id"]


def test_closed_task_new_assignment_reuses_native_without_mutating_old_result(collab):
    app, clients, users, pid = collab
    first = assign(collab).json()
    old = first["delegation"]
    child = old["child_project_id"]
    finish(app, old, "First task immutable result")
    merged = clients["creator"].post(merge_url(pid, old), json={"client_id": "first-task-merge01"}).json()
    with app.state.store.connect() as c:
        c.execute("UPDATE projects SET native_session_id='reused-native-123' WHERE id=?", (child,))
        c.execute("UPDATE turns SET status='failed' WHERE id=?", (first["turn"]["id"],))
    second = assign(collab, client_id="second-cycle-001", content="New cycle").json()
    current = second["delegation"]
    assert current["id"] != old["id"] and current["child_project_id"] == child
    assert current["status"] == "open"
    assert clients["owner"].get(f"/api/projects/{child}").json()["project"]["native_session_id"] == "reused-native-123"
    # Repeating old merge is idempotent even while the new task is active.
    replay = clients["creator"].post(merge_url(pid, old), json={"client_id": "replay-old-merge01"}).json()
    assert replay["turn"]["id"] == merged["turn"]["id"]
    assert replay["delegation"]["summary"] == "First task immutable result"
    assert replay["delegation"]["queued_count"] == 0
    assert clients["creator"].post(f"/api/projects/{child}/turns/{first['turn']['id']}/retry", json={"client_id": "closed-task-retry"}).status_code == 409
    # A failed second task must not use the first task's final answer as its result.
    with app.state.store.connect() as c:
        c.execute("UPDATE turns SET status='failed' WHERE id=?", (second["turn"]["id"],))
        c.execute("UPDATE messages SET status='failed' WHERE turn_id=?", (second["turn"]["id"],))
    assert clients["creator"].post(merge_url(pid, current), json={"client_id": "empty-new-merge01"}).status_code == 400
    history = clients["creator"].get(f"/api/projects/{pid}/delegations").json()
    assert len(history["delegations"]) == 2 and len(history["sessions"]) == 1
    assert history["sessions"][0]["delegation"]["id"] == current["id"]


def test_multi_member_batch_is_distinct_sessions_atomic_and_idempotent(collab):
    app, clients, users, pid = collab
    members = [users["assignee"]["id"], users["reader"]["id"]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: assign(collab, assignee_ids=members), range(3)))
    assert all(r.status_code == 201 for r in responses)
    first = responses[0].json()
    assert len(first["assignments"]) == 2
    assert first["delegation"] == first["assignments"][0]["delegation"]
    children = {item["delegation"]["child_project_id"] for item in first["assignments"]}
    assert len(children) == 2
    assert all({item["delegation"]["child_project_id"] for item in r.json()["assignments"]} == children for r in responses)
    by_member = {item["delegation"]["session_assignee"]["id"]: item for item in first["assignments"]}
    own = by_member[users["assignee"]["id"]]["delegation"]["child_project_id"]
    other = by_member[users["reader"]["id"]]["delegation"]["child_project_id"]
    assert clients["assignee"].get(f"/api/projects/{own}").json()["project"]["role"] == "editor"
    assert clients["assignee"].get(f"/api/projects/{other}").json()["project"]["role"] == "viewer"
    assert assign(collab, assignee_ids=[members[0]]).status_code == 409
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM collaboration_sessions").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM delegation_requests").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM turns WHERE project_id=?", (pid,)).fetchone()[0] == 0


def test_batch_failure_does_not_partially_assign_or_remove_existing_workspace(collab, monkeypatch):
    app, clients, users, pid = collab
    existing = assign(collab).json()["delegation"]
    root = app.state.store.workspace(existing["child_project_id"])
    (root / "keep.txt").write_text("Existing work")
    original = app.state.store.enqueue_turn
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("Injected batch failure")
        return result

    monkeypatch.setattr(app.state.store, "enqueue_turn", fail_second)
    with pytest.raises(RuntimeError, match="Injected batch"):
        assign(collab, client_id="fail-whole-batch", assignee_ids=[users["assignee"]["id"], users["reader"]["id"]])
    assert (root / "keep.txt").read_text() == "Existing work"
    with app.state.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM collaboration_sessions").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM delegation_requests").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
    assert len(list((app.state.store.root / "projects").iterdir())) == 2


def test_cancelled_old_task_cannot_cancel_new_task_in_reused_session(collab):
    app, clients, users, pid = collab
    old = assign(collab).json()["delegation"]
    endpoint = merge_url(pid, old).replace("/merge", "/cancel")
    assert clients["creator"].post(endpoint).status_code == 200
    new = assign(collab, client_id="after-cancel-new").json()
    assert new["delegation"]["child_project_id"] == old["child_project_id"]
    assert new["delegation"]["id"] != old["id"]
    assert clients["creator"].post(endpoint).status_code == 200
    with app.state.store.connect() as c:
        assert c.execute("SELECT status FROM turns WHERE id=?", (new["turn"]["id"],)).fetchone()[0] == "queued"
        assert app.state.store.delegation_for_turn(c, new["turn"]["id"])["id"] == new["delegation"]["id"]


def test_legacy_unique_child_migration_preserves_results_references_and_unrelated_fk(collab):
    import sqlite3
    from shui.store import Store

    app, clients, users, pid = collab
    first = assign(collab).json()["delegation"]
    finish(app, first, "Legacy result must survive")
    merged = clients["creator"].post(merge_url(pid, first), json={"client_id": "legacy-merge-001"}).json()
    # Build the old one-window-per-task shape, including duplicate member sessions.
    with app.state.store.connect() as c:
        c.execute("DELETE FROM collaboration_sessions")
    second = assign(collab, client_id="legacy-second01").json()["delegation"]
    shared = assign(collab, client_id="legacy-shared01", assignee_ids=[users["reader"]["id"]]).json()["delegation"]
    with app.state.store.connect() as c:
        c.execute("INSERT INTO delegation_assignees VALUES(?,?)", (shared["id"], users["creator"]["id"]))
    c = sqlite3.connect(app.state.store.path)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("DROP TABLE delegation_turns")
    c.execute("DROP TABLE delegation_requests")
    c.execute("DROP TABLE collaboration_sessions")
    schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='delegations'").fetchone()[0]
    schema = "CREATE TABLE legacy_delegations " + schema[schema.index("("):]
    schema = schema.replace("child_project_id TEXT NOT NULL REFERENCES", "child_project_id TEXT NOT NULL UNIQUE REFERENCES")
    c.execute(schema)
    c.execute("INSERT INTO legacy_delegations SELECT * FROM delegations")
    c.execute("DROP TABLE delegations")
    c.execute("ALTER TABLE legacy_delegations RENAME TO delegations")
    # This pre-existing unrelated author defect must neither block startup nor be changed.
    c.execute("UPDATE projects SET created_by='legacy-missing-author' WHERE id=?", (pid,))
    c.commit()
    baseline = set(c.execute("PRAGMA foreign_key_check").fetchall())
    c.close()
    restarted = Store(app.state.store.root)
    with restarted.connect() as c:
        assert {tuple(row) for row in c.execute("PRAGMA foreign_key_check")} == baseline
        sessions = c.execute("SELECT * FROM collaboration_sessions").fetchall()
        assert len(sessions) == 1
        assert sessions[0]["child_project_id"] == second["child_project_id"]
        assert c.execute("SELECT summary FROM delegations WHERE id=?", (first["id"],)).fetchone()[0] == "Legacy result must survive"
        assert c.execute("SELECT turn_id FROM delegation_merge_attempts WHERE delegation_id=?", (first["id"],)).fetchone()[0] == merged["turn"]["id"]
        assert c.execute("SELECT COUNT(*) FROM delegation_assignees WHERE delegation_id=?", (shared["id"],)).fetchone()[0] == 2
        assert c.execute("SELECT status FROM delegations WHERE id=?", (shared["id"],)).fetchone()[0] == "cancelled"
        assert c.execute("SELECT status FROM turns WHERE project_id=?", (shared["child_project_id"],)).fetchone()[0] == "cancelled"
        assert c.execute("SELECT status FROM projects WHERE id=?", (shared["child_project_id"],)).fetchone()[0] == "idle"
        assert restarted.project_role(c, shared["child_project_id"], users["owner"]) == "viewer"
        assert restarted.delegation_request_for_turn(c, c.execute("SELECT id FROM turns WHERE project_id=?", (second["child_project_id"],)).fetchone()[0])
    # A second startup is idempotent and does not duplicate mappings/requests.
    again = Store(app.state.store.root)
    with again.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM collaboration_sessions").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM delegation_requests").fetchone()[0] == 2
