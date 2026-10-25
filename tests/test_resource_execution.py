"""Approved resource delivery through the actual queue, with no model calls."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from shui.agents import RunResult
from shui.app import create_app
from shui.worker import Coordinator


@pytest.fixture
def resource_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SHUI_PUBLIC_ORIGIN', 'http://testserver')
    monkeypatch.delenv('SHUI_ORIGIN_FILE', raising=False)
    app = create_app(tmp_path / 'data', start_worker=False)
    clients = {}
    for name in ('owner', 'member', 'other'):
        app.state.store.create_user(name, name, 'test-password', 'owner' if name == 'owner' else 'member')
        client = clients[name] = TestClient(app)
        session = client.post('/api/login', json={'username': name, 'password': 'test-password'}).json()
        client.headers['x-csrf-token'] = session['csrf']
    owner, member = clients['owner'], clients['member']
    pid = owner.post('/api/projects', json={'name': 'Resources'}).json()['id']
    base = f'/api/projects/{pid}'
    for name in ('member', 'other'):
        assert owner.post(base + '/members', json={'username': name, 'role': 'editor'}).status_code == 200
    monkeypatch.setattr(app.state.coordinator, 'kick', lambda _: None)
    secret = 'synthetic-execution-credential-91XY'
    credential = owner.post(base + '/credentials', json={'name': 'Compute', 'provider': 'test-provider', 'category': 'llm', 'endpoint': 'https://example.com/api', 'secret': secret}).json()
    idea = member.post(base + '/ideas', json={'title': 'Bounded experiment', 'body': 'Collect real evidence'}).json()
    pool = owner.post(base + '/resource-pools', json={'kind': 'tokens', 'unit': 'tokens', 'capacity': 100, 'credential_id': credential['id']})
    assert pool.status_code == 201, pool.text
    request = member.post(base + '/resource-requests', json={'idea_id': idea['id'], 'kind': 'tokens', 'unit': 'tokens', 'amount': 10, 'reason': 'Pilot'}).json()
    approved = owner.post(base + f"/resource-requests/{request['id']}/decision", json={'decision': 'approve'}).json()
    yield app, clients, base, pid, idea, approved['allocation_id'], credential, secret
    for client in clients.values():
        client.close()


async def run_queue(app, pid, runner):
    coordinator = Coordinator(app.state.store, runner)
    await coordinator.start()
    try:
        coordinator.kick(pid)
        if coordinator.tasks:
            await asyncio.wait_for(asyncio.gather(*list(coordinator.tasks.values())), 5)
    finally:
        await coordinator.close()


def stored(app, table):
    with app.state.store.connect() as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM ' + table)]


@pytest.mark.asyncio
async def test_approved_actor_gets_env_only_and_echoes_are_redacted(resource_setup):
    app, clients, base, pid, idea, aid, credential, secret = resource_setup
    queued = clients['member'].post(base + f"/ideas/{idea['id']}/execute", json={'allocation_id': aid}).json()
    assert secret not in json.dumps(queued)
    seen = []

    async def runner(request, emit, cancel):
        seen.append(request)
        assert request.resource_env == {'SHUI_RESOURCE_TOKEN': secret, 'SHUI_RESOURCE_ENDPOINT': 'https://example.com/api', 'SHUI_RESOURCE_PROVIDER': 'test-provider', 'SHUI_RESOURCE_CREDENTIAL_ID': credential['id']}
        assert secret not in request.prompt
        assert not request.read_only
        await emit('delta', {'text': 'Echo ' + secret})
        await emit('tool', {'name': 'Compute', 'status': 'complete', 'content': secret})
        return RunResult('resource-session', 'Final ' + secret)

    await run_queue(app, pid, runner)
    assert len(seen) == 1
    assert stored(app, 'turns')[0]['status'] == 'complete'
    for table in ('messages', 'turns', 'events', 'vault_audit', 'governance_objects'):
        assert secret not in json.dumps(stored(app, table))
    for endpoint in ('messages', 'governance', 'credentials'):
        assert secret not in clients['member'].get(base + '/' + endpoint).text
    audit = [row for row in stored(app, 'vault_audit') if row['action'] == 'execution_used']
    with app.state.store.connect() as c:
        actor = c.execute("SELECT id FROM users WHERE username='member'").fetchone()[0]
    assert len(audit) == 1 and audit[0]['actor_id'] == actor


@pytest.mark.asyncio
async def test_assistance_never_gets_resource_environment(resource_setup):
    app, clients, base, pid, idea, aid, credential, secret = resource_setup
    result = clients['member'].post(base + f"/ideas/{idea['id']}/assist", json={'kind': 'check', 'allocation_id': aid})
    assert result.status_code == 201, result.text
    seen = []

    async def runner(request, emit, cancel):
        seen.append(request)
        assert request.read_only is True
        assert request.resource_env == {}
        assert secret not in request.prompt
        assert 'authorized to edit project files' not in request.prompt
        return RunResult('assist-session', 'Review complete')

    await run_queue(app, pid, runner)
    assert len(seen) == 1
    assert stored(app, 'turns')[0]['status'] == 'complete'
    assert not [row for row in stored(app, 'vault_audit') if row['action'] == 'execution_used']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidation', ['revoked', 'expired', 'allocation-mismatch'])
async def test_queued_credential_revalidated_before_runner(resource_setup, invalidation):
    app, clients, base, pid, idea, aid, credential, secret = resource_setup
    result = clients['member'].post(base + f"/ideas/{idea['id']}/execute", json={'allocation_id': aid})
    assert result.status_code == 201, result.text
    turn_id = result.json()['turn']['id']
    if invalidation == 'revoked':
        assert clients['owner'].post(base + f"/credentials/{credential['id']}/revoke").status_code == 200
    else:
        with app.state.store.connect() as c:
            if invalidation == 'expired':
                c.execute('UPDATE vault_credentials SET expires_at=? WHERE id=?', ('2000-01-01T00:00:00Z', credential['id']))
            else:
                row = c.execute('SELECT data FROM governance_objects WHERE id=?', (aid,)).fetchone()
                data = json.loads(row[0]); data['credential_id'] = 'different-credential'
                c.execute('UPDATE governance_objects SET data=? WHERE id=?', (json.dumps(data), aid))

    async def forbidden_runner(*args):
        pytest.fail('Invalid resource credentials must not reach the runner')

    await run_queue(app, pid, forbidden_runner)
    turn = next(row for row in stored(app, 'turns') if row['id'] == turn_id)
    assert turn['status'] == 'failed' and turn['finished_at']
    assert turn['error']
    assert secret not in json.dumps(stored(app, 'events'))
    assert not [row for row in stored(app, 'vault_audit') if row['action'] == 'execution_used']


def test_other_editor_cannot_execute_requesters_allocation(resource_setup):
    app, clients, base, pid, idea, aid, credential, secret = resource_setup
    response = clients['other'].post(base + f"/ideas/{idea['id']}/execute", json={'allocation_id': aid})
    assert response.status_code == 403
    assert not stored(app, 'turns')
    assert secret not in response.text
