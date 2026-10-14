"""WAL stays open without a read transaction or cross-thread connection sharing."""

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from shui.app import create_app
from shui.store import Store


def wal_path(store):
    return store.path.with_name(store.path.name + "-wal")


def add_probe_event(store, project_id, label):
    with store.connect() as connection:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        store.event(connection, project_id, "probe", {"label": label})


def test_wal_lifetime_keeps_commits_durable_without_holding_a_transaction(tmp_path):
    store = Store(tmp_path)
    user = store.create_user("owner", "Owner", "test-password", "owner")
    project = store.create_project(user, "Probe")
    assert not wal_path(store).exists()
    with store.keep_wal_open():
        add_probe_event(store, project["id"], "main thread")
        assert wal_path(store).exists() and wal_path(store).stat().st_size > 0
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(add_probe_event, store, project["id"], "worker thread").result()
        with store.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM events WHERE type='probe'").fetchone()[0] == 2
            # A pinned read transaction would prevent this checkpoint/truncation.
            assert tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (0, 0, 0)
        assert wal_path(store).exists()
    assert not wal_path(store).exists()
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events WHERE type='probe'").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_independent_app_lifespans_share_wal_without_sharing_connections(tmp_path):
    first = create_app(tmp_path, start_worker=False)
    second = create_app(tmp_path, start_worker=False)
    user = first.state.store.create_user("owner", "Owner", "test-password", "owner")
    project = first.state.store.create_project(user, "Probe")
    async with first.router.lifespan_context(first):
        async with second.router.lifespan_context(second):
            add_probe_event(first.state.store, project["id"], "both running")
            assert wal_path(first.state.store).exists()
        assert wal_path(first.state.store).exists()
        add_probe_event(first.state.store, project["id"], "second stopped")
    assert not wal_path(first.state.store).exists()
    # The same application may be started again, as in repeated TestClient use.
    async with first.router.lifespan_context(first):
        add_probe_event(first.state.store, project["id"], "restarted")
    assert not wal_path(first.state.store).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["startup", "shutdown", "body"])
async def test_lifespan_closes_wal_even_when_startup_or_shutdown_fails(tmp_path, monkeypatch, failure):
    app = create_app(tmp_path)
    store = app.state.store
    user = store.create_user("owner", "Owner", "test-password", "owner")
    project = store.create_project(user, "Probe")
    stages = []

    async def start():
        add_probe_event(store, project["id"], "startup")
        stages.append("startup")
        assert wal_path(store).exists()
        if failure == "startup":
            raise RuntimeError("startup failed")

    async def close():
        # The anchor must outlive the worker's final durable state update.
        assert wal_path(store).exists()
        add_probe_event(store, project["id"], "shutdown")
        stages.append("shutdown")
        if failure == "shutdown":
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(app.state.coordinator, "start", start)
    monkeypatch.setattr(app.state.coordinator, "close", close)
    with pytest.raises(RuntimeError, match=failure + " failed"):
        async with app.router.lifespan_context(app):
            if failure == "body":
                raise RuntimeError("body failed")
    assert not wal_path(store).exists()
    assert stages == (["startup"] if failure == "startup" else ["startup", "shutdown"])


@pytest.mark.asyncio
async def test_polling_reads_run_off_event_loop_and_keep_auth_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("SHUI_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.delenv("SHUI_ORIGIN_FILE", raising=False)
    app = create_app(tmp_path, start_worker=False)
    store = app.state.store
    user = store.create_user("owner", "Owner", "test-password", "owner")
    store.create_user("outside", "Outside", "test-password")
    project = store.create_project(user, "Probe")
    event_loop_thread = threading.get_ident()
    observed_threads = []
    original_connect = store.connect

    @contextlib.contextmanager
    def observed_connect():
        observed_threads.append(threading.get_ident())
        with original_connect() as connection:
            yield connection

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as owner, httpx.AsyncClient(transport=transport, base_url="http://testserver") as outside:
            assert (await owner.post("/api/login", json={"username": "owner", "password": "test-password"})).status_code == 200
            assert (await outside.post("/api/login", json={"username": "outside", "password": "test-password"})).status_code == 200
            monkeypatch.setattr(store, "connect", observed_connect)
            paths = [f"/api/projects/{project['id']}/{endpoint}" for endpoint in ("meta", "activity", "event-batch")]
            responses = await asyncio.gather(*(owner.get(path) for path in paths))
            assert all(response.status_code == 200 for response in responses)
            responses = await asyncio.gather(*(outside.get(path) for path in paths))
            assert all(response.status_code == 404 for response in responses)
            assert observed_threads and event_loop_thread not in observed_threads
