"""Queue integration tests use in-memory fake agent behavior, never inference."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from shui.agents import RunResult
from shui.app import create_app
from shui.worker import Coordinator


@pytest.fixture
def queued_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path / "data", start_worker=False)
    app.state.store.create_user("owner", "Owner", "test-password", "owner")
    with TestClient(app) as client:
        response = client.post(
            "/api/login", json={"username": "owner", "password": "test-password"}
        )
        client.headers["x-csrf-token"] = response.json()["csrf"]
        pid = client.post("/api/projects", json={"name": "Paper"}).json()["id"]
        yield app, client, pid


def queue(client, pid, content):
    result = client.post(
        f"/api/projects/{pid}/messages",
        json={"content": content, "client_id": f"request-{content}"},
    )
    assert result.status_code == 201
    return result.json()["turn"]


def rows(store, query, values=()):
    with store.connect() as c:
        return [dict(row) for row in c.execute(query, values)]


async def finish(coordinator):
    if coordinator.tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(coordinator.tasks.values())), timeout=3
        )


@pytest.mark.asyncio
async def test_fifo_and_durable_shared_native_session(queued_app):
    app, client, pid = queued_app
    store = app.state.store
    first, second = queue(client, pid, "first"), queue(client, pid, "second")
    seen = []
    active = 0

    async def runner(request, emit, cancel):
        nonlocal active
        active += 1
        assert active == 1
        seen.append(request)
        assert request.workspace == store.workspace(pid)
        assert "(@owner)" in request.prompt
        assert "host account permissions" in request.prompt
        await emit("session", {"session_id": "shared-session-1"})
        assert (
            rows(store, "SELECT native_session_id FROM projects WHERE id=?", (pid,))[0][
                "native_session_id"
            ]
            == "shared-session-1"
        )
        await emit("delta", {"text": "Hello "})
        await emit("delta", {"text": "world"})
        await emit("tool", {"name": "Bash", "status": "complete", "content": "done"})
        await asyncio.sleep(0)
        active -= 1
        return RunResult("shared-session-1", "Hello world")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        coordinator.kick(pid)
        await finish(coordinator)
        assert [req.prompt.rsplit("\n\n", 1)[-1] for req in seen] == ["first", "second"]
        assert [req.session_id for req in seen] == [None, "shared-session-1"]
        turns = rows(store, "SELECT * FROM turns ORDER BY created_at,id")
        assert [turn["id"] for turn in turns] == [first["id"], second["id"]]
        assert [turn["status"] for turn in turns] == ["complete", "complete"]
        assert turns[0]["finished_at"] <= turns[1]["started_at"]
        assert [
            row["content"]
            for row in rows(
                store, "SELECT content FROM messages WHERE role='assistant'"
            )
        ] == ["Hello world", "Hello world"]
        assert len(rows(store, "SELECT * FROM events WHERE type='agent.delta'")) == 4
        assert len(rows(store, "SELECT * FROM events WHERE type='agent.tool'")) == 2
        assert rows(store, "SELECT status FROM projects")[0]["status"] == "idle"
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_distinct_projects_execute_concurrently(queued_app):
    app, client, pid = queued_app
    second_pid = client.post("/api/projects", json={"name": "Second paper"}).json()[
        "id"
    ]
    queue(client, pid, "first")
    queue(client, second_pid, "second")
    started = set()
    both_started = asyncio.Event()

    async def runner(request, emit, cancel):
        started.add(request.project_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        sid = "native-" + request.project_id
        await emit("session", {"session_id": sid})
        return RunResult(sid, "Complete")

    coordinator = Coordinator(app.state.store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert started == {pid, second_pid}
        assert {
            row["status"] for row in rows(app.state.store, "SELECT status FROM turns")
        } == {"complete"}
    finally:
        await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "binding-change"])
async def test_failure_pauses_queue_and_retains_bound_session(queued_app, failure):
    app, client, pid = queued_app
    store = app.state.store
    client.patch(f"/api/projects/{pid}", json={"native_session_id": "original-session"})
    first, _second = queue(client, pid, "first"), queue(client, pid, "second")
    attempts = []

    async def runner(request, emit, cancel):
        attempts.append(request)
        await emit("delta", {"text": "Partial output"})
        if failure == "binding-change":
            await emit("session", {"session_id": "unexpected-new-session"})
        raise RuntimeError("Agent failed")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert len(attempts) == 1
        assert attempts[0].session_id == "original-session"
        turns = rows(store, "SELECT * FROM turns ORDER BY created_at,id")
        assert [turn["status"] for turn in turns] == ["failed", "queued"]
        assert rows(store, "SELECT native_session_id,status FROM projects")[0] == {
            "native_session_id": "original-session",
            "status": "error",
        }
        assert (
            rows(
                store,
                "SELECT content FROM messages WHERE id=?",
                (first["assistant_message_id"],),
            )[0]["content"]
            == "Partial output"
        )
        coordinator.kick(pid)
        await finish(coordinator)
        assert len(attempts) == 1
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_cancel_running_turn_and_continue_queue(queued_app):
    app, client, pid = queued_app
    first, _second = queue(client, pid, "first"), queue(client, pid, "second")
    started = asyncio.Event()
    calls = []

    async def runner(request, emit, cancel):
        calls.append(request)
        await emit("session", {"session_id": "shared-session"})
        if len(calls) == 1:
            started.set()
            await asyncio.wait_for(cancel.wait(), timeout=2)
            return RunResult(
                "shared-session", "Cancelled after partial work", "cancelled"
            )
        return RunResult("shared-session", "Second complete")

    coordinator = app.state.coordinator
    coordinator.runner = runner
    await coordinator.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert (
            client.post(f"/api/projects/{pid}/turns/{first['id']}/cancel").status_code
            == 200
        )
        await finish(coordinator)
        assert [
            turn["status"]
            for turn in rows(
                app.state.store, "SELECT status FROM turns ORDER BY created_at,id"
            )
        ] == ["cancelled", "complete"]
        assert len(calls) == 2
        assert calls[1].session_id == "shared-session"
        assert coordinator.cancels == {}
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_restart_marks_active_turn_interrupted_without_replaying_queue(
    queued_app,
):
    app, client, pid = queued_app
    store = app.state.store
    first, _second = queue(client, pid, "first"), queue(client, pid, "second")
    with store.connect() as c:
        c.execute("UPDATE turns SET status='running' WHERE id=?", (first["id"],))
        c.execute(
            "UPDATE messages SET status='running' WHERE turn_id=?", (first["id"],)
        )
        c.execute(
            "UPDATE projects SET status='running',native_session_id='prior-session' WHERE id=?",
            (pid,),
        )
    called = []

    async def runner(*args):
        called.append(args)
        return RunResult("prior-session", "Unexpected")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        assert called == []
        assert [
            turn["status"]
            for turn in rows(store, "SELECT status FROM turns ORDER BY created_at,id")
        ] == ["interrupted", "queued"]
        assert (
            rows(store, "SELECT native_session_id FROM projects")[0][
                "native_session_id"
            ]
            == "prior-session"
        )
        retried = client.post(
            f"/api/projects/{pid}/turns/{first['id']}/retry",
            json={"client_id": "explicit-retry"},
        )
        assert retried.status_code == 201
        coordinator.kick(pid)
        await finish(coordinator)
        assert len(called) == 2
        assert all(args[0].session_id == "prior-session" for args in called)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_only_one_worker_can_own_data_directory(queued_app):
    app, _, _ = queued_app
    first, second = Coordinator(app.state.store), Coordinator(app.state.store)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="Another Shui worker"):
            await second.start()
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_agent_research_manifest_is_upserted_with_stable_ids(queued_app):
    app, client, pid = queued_app
    store = app.state.store
    queue(client, pid, "record-experiment")
    queue(client, pid, "update-experiment")
    figure_dir = store.workspace(pid) / "figures"
    figure_dir.mkdir()
    (figure_dir / "main.png").write_bytes(b"\x89PNG\r\n\x1a\nplot")
    calls = 0

    async def runner(request, emit, cancel):
        nonlocal calls
        calls += 1
        assert "research.json" in request.prompt
        value = "0.8100000000000001" if calls == 1 else "0.8200000000000001"
        (request.workspace / "research.json").write_text(
            json.dumps(
                {
                    "experiments": [
                        {
                            "key": "main-evaluation",
                            "name": "Main evaluation",
                            "objective": "Test a claim",
                            "claim": "Models track the target",
                            "validation": "Compare against controls",
                            "status": "completed",
                            "result_summary": f"Observed {value}",
                        }
                    ],
                    "figures": [
                        {
                            "key": "main-figure",
                            "title": "Main result",
                            "path": "figures/main.png",
                            "experiment": "Main evaluation",
                            "caption": "Comparison",
                            "claim": "Models track the target",
                        }
                    ],
                    "claims": [
                        {
                            "key": "tracking-claim",
                            "statement": "Models track the target",
                            "status": "supported",
                            "evidence": "Main evaluation",
                            "experiment": "Main evaluation",
                        }
                    ],
                    "results": [
                        {
                            "key": "main-score",
                            "experiment": "Main evaluation",
                            "metric": "accuracy",
                            "value": value,
                            "unit": "score",
                            "model": "Model A",
                            "summary": f"Observed {value}",
                            "status": "supports",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return RunResult("research-session", "Updated research")

    coordinator = Coordinator(store, runner)
    await coordinator.start()
    try:
        await finish(coordinator)
        data = client.get(f"/api/projects/{pid}/research").json()["insights"]
        assert data["summary"]["experiment_count"] == 1
        assert data["summary"]["figure_count"] == 1
        assert data["summary"]["claim_count"] == 1
        assert data["summary"]["result_count"] == 1
        assert data["results"][0]["value"] == "0.8200000000000001"
        assert data["figures"][0]["download_url"].endswith(
            "path=figures/main.png&inline=1"
        )
        events = [
            json.loads(row["payload"])
            for row in rows(
                store,
                "SELECT payload FROM events WHERE project_id=? AND type='research.updated' ORDER BY id",
                (pid,),
            )
        ]
        assert len(events) == 2
        assert events[0]["insights"]["experiments"][0]["id"] == data["experiments"][0]["id"]
        assert events[1]["insights"]["experiments"][0]["id"] == data["experiments"][0]["id"]
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_generated_figure_emits_artifact_event_while_turn_is_running(queued_app):
    app, _, pid = queued_app
    store = app.state.store
    root = store.workspace(pid)
    user_id = rows(store, "SELECT id FROM users WHERE username='owner'")[0]["id"]
    stop = asyncio.Event()
    coordinator = Coordinator(store)
    watcher = asyncio.create_task(coordinator._watch_artifacts(pid, root, user_id, stop))
    try:
        await asyncio.sleep(0)
        figure_dir = root / "figures"
        figure_dir.mkdir()
        data = b"\x89PNG\r\n\x1a\nplot-bytes"
        (figure_dir / "live.png").write_bytes(data)
        await asyncio.sleep(3.4)
        events = rows(store, "SELECT payload FROM events WHERE project_id=? AND type='artifact.changed'", (pid,))
        assert len(events) == 1
        event = json.loads(events[0]["payload"])
        assert event["path"] == "figures/live.png"
        assert event["mime_type"] == "image/png"
        assert event["size"] == len(data)
        assert len(event["sha256"]) == 64
        assert rows(store, "SELECT path FROM figures WHERE project_id=?", (pid,)) == [
            {"path": "figures/live.png"}
        ]
        (root / "research.json").write_text(
            json.dumps({"figures": [{"key": "paper-fig-1", "title": "Main result", "path": "figures/live.png", "claim": "The effect persists"}]}),
            encoding="utf-8",
        )
        store.sync_research_manifest(pid, user_id)
        figures = rows(store, "SELECT path,title,claim FROM figures WHERE project_id=?", (pid,))
        assert figures == [{"path": "figures/live.png", "title": "Main result", "claim": "The effect persists"}]
    finally:
        stop.set()
        await watcher


def test_research_manifest_rejects_unsafe_paths_atomically(queued_app):
    app, _, pid = queued_app
    store = app.state.store
    store.workspace(pid).joinpath("research.json").write_text(
        json.dumps(
            {
                "experiments": [{"name": "Unsafe figure"}],
                "figures": [{"title": "Escape", "path": "../outside.png"}],
            }
        ),
        encoding="utf-8",
    )
    assert store.sync_research_manifest(pid, None) is None
    assert rows(store, "SELECT id FROM experiments WHERE project_id=?", (pid,)) == []
