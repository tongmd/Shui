"""Independent collaboration sessions, using fake native agents only."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from shui.agents import RunResult
from shui.app import create_app
from shui.store import now
from shui.worker import Coordinator


@pytest.fixture
def collaboration_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    store = app.state.store
    owner = store.create_user("owner", "Owner", "test-password", "owner")
    assignee = store.create_user("alice", "Alice", "test-password")
    with TestClient(app) as client:
        login(client, "owner")
        parent = client.post("/api/projects", json={
            "name": "Parent paper", "description": "Test failure prediction",
            "github_url": "https://github.com/example/paper",
        }).json()["id"]
        assert client.post(f"/api/projects/{parent}/members", json={
            "username": "alice", "role": "editor",
        }).status_code == 200
        assert client.patch(f"/api/projects/{parent}", json={
            "native_session_id": "parent-session",
        }).status_code == 200
        yield app, client, parent, owner, assignee


def login(client, username):
    response = client.post("/api/login", json={
        "username": username, "password": "test-password",
    })
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf"]


def rows(store, sql, values=()):
    with store.connect() as c:
        return [dict(row) for row in c.execute(sql, values)]


def delegate(client, parent, assignee, client_id="task-request-1", attachments=None, **overrides):
    response = client.post(f"/api/projects/{parent}/delegations", json={
        "content": "Verify the failure predictor and prepare a figure",
        "title": "Verify failure predictor",
        "context": "Explicit short background only",
        "assignee_ids": [assignee["id"]],
        "attachment_ids": attachments or [],
        "client_id": client_id,
        **overrides,
    })
    assert response.status_code == 201, response.text
    return response.json()


def queue(client, project_id, content, client_id="child-followup-1"):
    response = client.post(f"/api/projects/{project_id}/messages", json={
        "content": content, "client_id": client_id,
    })
    assert response.status_code == 201, response.text
    return response.json()["turn"]


async def finish(coordinator):
    if coordinator.tasks:
        await asyncio.wait_for(asyncio.gather(*list(coordinator.tasks.values())), timeout=5)


@pytest.mark.asyncio
async def test_child_starts_independently_resumes_and_keeps_research_local(collaboration_app):
    app, client, parent, owner, assignee = collaboration_app
    store = app.state.store
    # Prior history and the parent native binding must never be copied to a child.
    with store.connect() as c:
        c.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("prior-history", parent, "assistant", "PARENT_TRANSCRIPT_NOT_TO_COPY",
             None, "complete", None, "[]", now(), None, None),
        )
    parent_manifest = '{"experiments": [{"name": "Parent experiment"}]}'
    (store.workspace(parent) / "research.json").write_text(parent_manifest)
    created = delegate(client, parent, assignee)
    child = created["delegation"]["child_project_id"]
    output_path = "tasks/" + created["delegation"]["id"] + "/result.png"
    seen = []

    async def runner(request, emit, cancel):
        seen.append(request)
        assert request.project_id == child
        assert request.workspace == store.workspace(child)
        assert request.workspace != store.workspace(parent)
        assert "PARENT_TRANSCRIPT_NOT_TO_COPY" not in request.prompt
        assert "Explicit short background only" in request.prompt
        assert "Test failure prediction" in request.prompt
        assert "https://github.com/example/paper" in request.prompt
        assert '"username": "alice"' in request.prompt
        assert "READ-ONLY" in request.prompt
        assert str(store.workspace(parent)) in request.prompt
        assert "Do not edit the parent workspace" in request.prompt
        assert "tasks/" + created["delegation"]["id"] in request.prompt
        assert (request.workspace / output_path).parent.is_dir()
        await emit("session", {"session_id": "dedicated-child-session"})
        assert rows(store, "SELECT native_session_id FROM projects WHERE id=?", (child,))[0][
            "native_session_id"
        ] == "dedicated-child-session"
        # Binding is durable before fake inference produces any content.
        await emit("delta", {"text": "CHILD_DISCUSSION_ONLY"})
        (request.workspace / output_path).write_bytes(b"\x89PNG\r\n\x1a\nfigure")
        (request.workspace / "research.json").write_text(json.dumps({
            "experiments": [{"key": "child-check", "name": "Child validation"}],
            "figures": [{"key": "child-result", "title": "Result", "path": output_path}],
        }))
        return RunResult("dedicated-child-session", f"CHILD_DISCUSSION_ONLY\n[result]({output_path})")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert len(seen) == 1
        assert seen[0].session_id is None
        assert rows(store, "SELECT id FROM turns WHERE project_id=?", (parent,)) == []
        queue(client, child, "Please check the calibration too")
        coordinator.kick(child)
        await finish(coordinator)
        assert len(seen) == 2
        assert seen[1].session_id == "dedicated-child-session"
        assert "Please check the calibration too" in seen[1].prompt
        assert rows(store, "SELECT native_session_id FROM projects WHERE id=?", (parent,))[0][
            "native_session_id"
        ] == "parent-session"
        assert (store.workspace(parent) / "research.json").read_text() == parent_manifest
        assert rows(store, "SELECT name FROM experiments WHERE project_id=?", (child,)) == [
            {"name": "Child validation"}
        ]
        assert rows(store, "SELECT id FROM experiments WHERE project_id=?", (parent,)) == []
        assert rows(store, "SELECT path FROM figures WHERE project_id=?", (child,)) == [
            {"path": output_path}
        ]
        parent_events = rows(store, "SELECT type,payload FROM events WHERE project_id=?", (parent,))
        assert all("CHILD_DISCUSSION_ONLY" not in event["payload"] for event in parent_events)
        assert not {"agent.delta", "agent.tool", "research.updated", "artifact.changed"} & {
            event["type"] for event in parent_events
        }
        task_events = [json.loads(e["payload"])["delegation"] for e in parent_events
                       if e["type"] == "delegation.updated"]
        assert any(task["session_status"] == "running" for task in task_events)
        assert task_events[-1]["session_status"] == "idle"
        assert task_events[-1]["status"] == "open"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_parent_and_multiple_children_run_concurrently(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    first = delegate(client, parent, assignee)["delegation"]["child_project_id"]
    second_assignee = store.create_user("bob", "Bob", "test-password")
    assert client.post(f"/api/projects/{parent}/members", json={
        "username": "bob", "role": "editor",
    }).status_code == 200
    second = delegate(client, parent, second_assignee, "task-request-2")["delegation"]["child_project_id"]
    queue(client, parent, "Continue the parent work", "parent-request-1")
    running, active = set(), set()
    all_started = asyncio.Event()

    async def runner(request, emit, cancel):
        assert request.project_id not in active
        active.add(request.project_id)
        running.add(request.project_id)
        if len(running) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=2)
        sid = "parent-session" if request.project_id == parent else "native-" + request.project_id
        await emit("session", {"session_id": sid})
        active.remove(request.project_id)
        return RunResult(sid, "Complete")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert running == {parent, first, second}
        assert {t["status"] for t in rows(store, "SELECT status FROM turns")} == {"complete"}
        assert len({r["native_session_id"] for r in rows(store, "SELECT native_session_id FROM projects")}) == 3
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_only_bounded_final_result_is_merged_into_parent(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    task = delegate(client, parent, assignee)["delegation"]
    child, task_id = task["child_project_id"], task["id"]
    seen = []

    async def runner(request, emit, cancel):
        seen.append(request)
        if request.project_id == child:
            await emit("session", {"session_id": "child-session"})
            # The first full reply must not be replayed during integration.
            text = "EARLIER_CHILD_DISCUSSION" if len(seen) == 1 else "Final result " + "x" * 15000
            return RunResult("child-session", text)
        assert request.project_id == parent
        assert request.session_id == "parent-session"
        assert "Final result " in request.prompt
        assert "EARLIER_CHILD_DISCUSSION" not in request.prompt
        assert "Explicit short background only" not in request.prompt
        assert len(request.prompt) < 18000
        return RunResult("parent-session", "Integrated the reviewed result")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        queue(client, child, "Summarize the final result")
        coordinator.kick(child)
        await finish(coordinator)
        assert len(seen) == 2
        merged = client.post(f"/api/projects/{parent}/delegations/{task_id}/merge", json={
            "client_id": "merge-request-1",
        })
        assert merged.status_code in (200, 201), merged.text
        summary = merged.json()["delegation"]["summary"]
        assert len(summary) <= 12000
        coordinator.kick(parent)
        await finish(coordinator)
        assert len(seen) == 3
        duplicate = client.post(f"/api/projects/{parent}/delegations/{task_id}/merge", json={
            "client_id": "merge-request-1",
        })
        assert duplicate.status_code in (200, 201), duplicate.text
        assert duplicate.json()["turn"]["id"] == merged.json()["turn"]["id"]
        metadata = [json.loads(event["payload"])["delegation"] for event in rows(
            store, "SELECT payload FROM events WHERE project_id=? AND type='delegation.updated' ORDER BY id", (parent,)
        )]
        assert any(t["merge_turn"] and t["merge_turn"]["status"] == "running" for t in metadata)
        assert metadata[-1]["merge_turn"]["status"] == "complete"
        assert rows(store, "SELECT native_session_id FROM projects WHERE id=?", (parent,))[0][
            "native_session_id"
        ] == "parent-session"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_cancelling_task_stops_its_queue_without_stopping_parent(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    task = delegate(client, parent, assignee)["delegation"]
    child = task["child_project_id"]
    queue(client, child, "Second child request")
    queue(client, parent, "Independent parent work", "parent-request-1")
    child_started, parent_release = asyncio.Event(), asyncio.Event()
    child_calls = []

    async def runner(request, emit, cancel):
        if request.project_id == parent:
            await parent_release.wait()
            assert not cancel.is_set()
            return RunResult("parent-session", "Parent work complete")
        child_calls.append(request)
        await emit("session", {"session_id": "child-session"})
        child_started.set()
        await asyncio.wait_for(cancel.wait(), timeout=3)
        return RunResult("child-session", "Partial child work", "cancelled")

    coordinator = app.state.coordinator
    coordinator.runner = runner
    await coordinator.start()
    try:
        await asyncio.wait_for(child_started.wait(), timeout=2)
        response = client.post(f"/api/projects/{parent}/delegations/{task['id']}/cancel", json={})
        assert response.status_code == 200, response.text
        parent_release.set()
        await finish(coordinator)
        assert len(child_calls) == 1
        assert {t["status"] for t in rows(store, "SELECT status FROM turns WHERE project_id=?", (child,))} == {"cancelled"}
        assert rows(store, "SELECT status FROM turns WHERE project_id=?", (parent,)) == [{"status": "complete"}]
        assert rows(store, "SELECT native_session_id FROM projects WHERE id=?", (child,))[0]["native_session_id"] == "child-session"
    finally:
        parent_release.set()
        await coordinator.close()


@pytest.mark.asyncio
async def test_restart_preserves_child_session_and_exposes_interruption(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    task = delegate(client, parent, assignee)
    child, first = task["delegation"]["child_project_id"], task["turn"]
    queue(client, child, "Pending followup")
    with store.connect() as c:
        c.execute("UPDATE turns SET status='running' WHERE id=?", (first["id"],))
        c.execute("UPDATE messages SET status='running' WHERE turn_id=?", (first["id"],))
        c.execute("UPDATE projects SET status='running',native_session_id='child-before-restart' WHERE id=?", (child,))
    calls = []

    async def runner(request, emit, cancel):
        calls.append(request)
        assert request.session_id == "child-before-restart"
        return RunResult(request.session_id, "Explicitly resumed")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert calls == []
        assert [t["status"] for t in rows(store, "SELECT status FROM turns WHERE project_id=? ORDER BY created_at,id", (child,))] == ["interrupted", "queued"]
        latest = json.loads(rows(store,
            "SELECT payload FROM events WHERE project_id=? AND type='delegation.updated' ORDER BY id DESC LIMIT 1", (parent,)
        )[0]["payload"])["delegation"]
        assert latest["session_status"] == "error"
        assert latest["queued_count"] == 1
        retry = client.post(f"/api/projects/{child}/turns/{first['id']}/retry", json={
            "client_id": "explicit-retry-1",
        })
        assert retry.status_code == 201, retry.text
        coordinator.kick(child)
        await finish(coordinator)
        assert len(calls) == 2
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["removed", "viewer", "inactive", "password"])
async def test_revoked_author_cannot_start_queued_child(collaboration_app, revocation):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    login(client, "alice")
    child = delegate(client, parent, assignee)["delegation"]["child_project_id"]
    queue(client, child, "Next child request")
    with store.connect() as c:
        if revocation == "removed":
            c.execute("DELETE FROM members WHERE project_id=? AND user_id=?", (parent, assignee["id"]))
        elif revocation == "viewer":
            c.execute("UPDATE members SET role='viewer' WHERE project_id=? AND user_id=?", (parent, assignee["id"]))
        elif revocation == "inactive":
            c.execute("UPDATE users SET is_active=0 WHERE id=?", (assignee["id"],))
        else:
            c.execute("UPDATE users SET must_change_password=1 WHERE id=?", (assignee["id"],))
    calls = []

    async def runner(*args):
        calls.append(args)
        return RunResult("must-not-exist", "Unauthorized")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert calls == []
        assert {t["status"] for t in rows(store, "SELECT status FROM turns WHERE project_id=?", (child,))} == {"cancelled"}
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_running_child_stops_when_parent_membership_is_removed(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    login(client, "alice")
    child = delegate(client, parent, assignee)["delegation"]["child_project_id"]
    started = asyncio.Event()

    async def runner(request, emit, cancel):
        await emit("session", {"session_id": "retained-child-session"})
        started.set()
        await asyncio.wait_for(cancel.wait(), timeout=3)
        return RunResult("retained-child-session", "Work stopped", "cancelled")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # Bypass the HTTP revocation helper deliberately: the worker must enforce
        # dynamic inherited access itself during long-running child inference.
        with store.connect() as c:
            c.execute("DELETE FROM members WHERE project_id=? AND user_id=?", (parent, assignee["id"]))
        await finish(coordinator)
        assert rows(store, "SELECT status FROM turns WHERE project_id=?", (child,)) == [{"status": "cancelled"}]
        assert rows(store, "SELECT native_session_id FROM projects WHERE id=?", (child,))[0]["native_session_id"] == "retained-child-session"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", ["completed", "cancelled"])
async def test_closed_task_never_executes_leftover_queue(collaboration_app, task_status):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    task = delegate(client, parent, assignee)["delegation"]
    child = task["child_project_id"]
    queue(client, child, "Leftover queued request")
    with store.connect() as c:
        c.execute("UPDATE delegations SET status=? WHERE id=?", (task_status, task["id"]))
        c.execute("UPDATE projects SET status='error' WHERE id=?", (child,))
    calls = []

    async def runner(*args):
        calls.append(args)
        return RunResult("unexpected-session", "Unexpected execution")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert calls == []
        assert {t["status"] for t in rows(store, "SELECT status FROM turns WHERE project_id=?", (child,))} == {"cancelled"}
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_parent_attachment_references_reject_symlink_escape(collaboration_app, tmp_path):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    uploads = []
    for name in ("reference.txt", "escape.txt"):
        response = client.post(f"/api/projects/{parent}/files", files={
            "file": (name, b"source", "text/plain"),
        })
        assert response.status_code == 201, response.text
        uploads.append(response.json())
    task = delegate(client, parent, assignee, attachments=[f["id"] for f in uploads])["delegation"]
    escape = rows(store, "SELECT relative_path FROM files WHERE id=?", (uploads[1]["id"],))[0]["relative_path"]
    outside = tmp_path / "outside-private-reference.txt"
    outside.write_text("Do not expose this file")
    target = store.workspace(parent) / escape
    target.unlink()
    target.symlink_to(outside)
    seen = []

    async def runner(request, emit, cancel):
        seen.append(request)
        assert "reference.txt" in request.prompt
        assert "escape.txt" not in request.prompt
        assert str(outside) not in request.prompt
        await emit("session", {"session_id": "attachment-child-session"})
        return RunResult("attachment-child-session", "Read the safe reference")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert len(seen) == 1
        assert seen[0].project_id == task["child_project_id"]
    finally:
        await coordinator.close()


def focused_brief(request):
    return json.loads(request.prompt.split("Focused task brief (JSON):\n", 1)[1].split(
        "\n\nCurrent collaborator message:\n", 1
    )[0])


@pytest.mark.asyncio
async def test_repeated_member_requests_and_retry_keep_their_exact_briefs(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    first = delegate(client, parent, assignee, content="FIRST instructions", context="FIRST context")
    second = delegate(client, parent, assignee, "task-request-2", content="SECOND instructions", context="SECOND context")
    child = first["delegation"]["child_project_id"]
    assert second["delegation"]["child_project_id"] == child
    assert second["delegation"]["id"] == first["delegation"]["id"]
    assert second["turn"]["id"] != first["turn"]["id"]
    calls = []

    async def runner(request, emit, cancel):
        brief = focused_brief(request)
        calls.append((request, brief))
        await emit("session", {"session_id": "member-session"})
        if len(calls) == 1:
            raise RuntimeError("First attempt failed after binding")
        return RunResult("member-session", "Completed " + brief["instructions"])

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert len(calls) == 1
        assert calls[0][1]["instructions"] == "FIRST instructions"
        retry = client.post(f"/api/projects/{child}/turns/{first['turn']['id']}/retry", json={
            "client_id": "retry-first-request",
        })
        assert retry.status_code == 201, retry.text
        coordinator.kick(child)
        await finish(coordinator)
        queue(client, child, "Follow up on the most recent assignment")
        coordinator.kick(child)
        await finish(coordinator)
        assert [brief["instructions"] for _, brief in calls] == [
            "FIRST instructions", "SECOND instructions", "FIRST instructions", "SECOND instructions",
        ]
        assert [brief["context"] for _, brief in calls] == [
            "FIRST context", "SECOND context", "FIRST context", "SECOND context",
        ]
        assert [request.session_id for request, _ in calls] == [None] + ["member-session"] * 3
        assert {brief["task_id"] for _, brief in calls} == {first["delegation"]["id"]}
        assert rows(store, "SELECT id FROM turns WHERE project_id=?", (parent,)) == []
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_new_task_reuses_member_session_without_overwriting_old_results_or_merge(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    first = delegate(client, parent, assignee, content="FIRST task instructions")["delegation"]
    child = first["child_project_id"]
    calls = []
    old_output = store.workspace(child) / "tasks" / first["id"] / "result.txt"

    async def runner(request, emit, cancel):
        calls.append(request)
        if request.project_id == parent:
            assert request.session_id == "parent-session"
            assert "FIRST verified result" in request.prompt
            assert "SECOND task instructions" not in request.prompt
            return RunResult("parent-session", "Integrated the first task")
        brief = focused_brief(request)
        output = request.workspace / brief["output_directory"] / "result.txt"
        assert output.parent.is_dir()
        await emit("session", {"session_id": "persistent-member-session"})
        if brief["task_id"] == first["id"]:
            output.write_text("FIRST result must remain unchanged")
            return RunResult("persistent-member-session", f"FIRST verified result [file]({brief['output_directory']}/result.txt)")
        assert request.session_id == "persistent-member-session"
        assert brief["instructions"] == "SECOND task instructions"
        assert old_output.read_text() == "FIRST result must remain unchanged"
        output.write_text("SECOND result in its own task directory")
        return RunResult("persistent-member-session", f"SECOND verified result [file]({brief['output_directory']}/result.txt)")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        merge = client.post(f"/api/projects/{parent}/delegations/{first['id']}/merge", json={
            "client_id": "merge-first-result",
        })
        assert merge.status_code == 200, merge.text
        second = delegate(client, parent, assignee, "new-task-request-2", content="SECOND task instructions")["delegation"]
        assert second["id"] != first["id"]
        assert second["child_project_id"] == child
        coordinator.kick(child)
        coordinator.kick(parent)
        await finish(coordinator)
        assert old_output.read_text() == "FIRST result must remain unchanged"
        assert (store.workspace(child) / "tasks" / second["id"] / "result.txt").read_text() == "SECOND result in its own task directory"
        states = {row["id"]: row for row in rows(store, "SELECT * FROM delegations WHERE child_project_id=?", (child,))}
        assert states[first["id"]]["merge_turn_id"] == merge.json()["turn"]["id"]
        assert states[second["id"]]["merge_turn_id"] is None
        events = [json.loads(row["payload"])["delegation"] for row in rows(
            store, "SELECT payload FROM events WHERE project_id=? AND type='delegation.updated' ORDER BY id", (parent,)
        )]
        first_events = [event for event in events if event["id"] == first["id"]]
        assert first_events[-1]["merge_turn"]["status"] == "complete"
        assert all(event["merge_turn"] is None for event in events if event["id"] == second["id"])
        child_calls = [request for request in calls if request.project_id == child]
        assert [request.session_id for request in child_calls] == [None, "persistent-member-session"]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_old_task_stops_before_a_new_task_reuses_the_member_session(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    first = delegate(client, parent, assignee, content="Old task")
    child = first["delegation"]["child_project_id"]
    queue(client, child, "Old queued followup")
    started = asyncio.Event()
    calls = []

    async def runner(request, emit, cancel):
        brief = focused_brief(request)
        calls.append(brief)
        await emit("session", {"session_id": "member-session"})
        if brief["task_id"] == first["delegation"]["id"]:
            started.set()
            await asyncio.wait_for(cancel.wait(), timeout=3)
            return RunResult("member-session", "Old task cancelled", "cancelled")
        assert request.session_id == "member-session"
        return RunResult("member-session", "New task completed")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # Do not set the coordinator's cancellation event: the worker must notice
        # closure itself. A new task cannot race the old task's native shutdown.
        with store.connect() as c:
            c.execute("UPDATE delegations SET status='cancelled' WHERE id=?", (first["delegation"]["id"],))
        blocked = client.post(f"/api/projects/{parent}/delegations", json={
            "content": "New task", "assignee_ids": [assignee["id"]],
            "client_id": "new-task-request-2",
        })
        assert blocked.status_code == 409, blocked.text
        await finish(coordinator)
        second = delegate(client, parent, assignee, "new-task-request-2", content="New task")
        assert second["delegation"]["child_project_id"] == child
        coordinator.kick(child)
        await finish(coordinator)
        assert [brief["instructions"] for brief in calls] == ["Old task", "New task"]
        turns = rows(store, "SELECT status FROM turns WHERE project_id=? ORDER BY created_at,id", (child,))
        assert [turn["status"] for turn in turns] == ["cancelled", "cancelled", "complete"]
        retry = client.post(f"/api/projects/{child}/turns/{first['turn']['id']}/retry", json={
            "client_id": "must-not-retry-old",
        })
        assert retry.status_code == 409, retry.text
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_unmapped_child_turn_does_not_inherit_the_latest_task(collaboration_app):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    first = delegate(client, parent, assignee)
    child = first["delegation"]["child_project_id"]
    with store.connect() as c:
        c.execute("DELETE FROM delegation_turns WHERE turn_id=?", (first["turn"]["id"],))
    calls = []

    async def runner(*args):
        calls.append(args)
        return RunResult("unexpected-session", "Unexpected execution")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert calls == []
        assert rows(store, "SELECT status FROM turns WHERE project_id=?", (child,)) == [{"status": "cancelled"}]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_task_output_symlink_fails_before_inference(collaboration_app, tmp_path):
    app, client, parent, _, assignee = collaboration_app
    store = app.state.store
    task = delegate(client, parent, assignee)["delegation"]
    child = task["child_project_id"]
    outside = tmp_path / "outside-output"
    outside.mkdir()
    (store.workspace(child) / "tasks").symlink_to(outside, target_is_directory=True)
    calls = []

    async def runner(*args):
        calls.append(args)
        return RunResult("unexpected-session", "Unexpected execution")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert calls == []
        assert rows(store, "SELECT status FROM turns WHERE project_id=?", (child,)) == [{"status": "failed"}]
        assert list(outside.iterdir()) == []
    finally:
        await coordinator.close()
