"""Independent authenticated restart, hierarchy and credential privacy checks."""
import json
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from shui.app import create_app
from shui.governance import install_governance
from shui.vault import install_vault


def installed(root):
    app=create_app(root,start_worker=False)
    endpoint=next(r.endpoint for r in app.routes if r.path=='/api/projects/{pid}' and 'GET' in r.methods)
    cells=dict(zip(endpoint.__code__.co_freevars,[x.cell_contents for x in endpoint.__closure__]))
    access=cells['access']; acells=dict(zip(access.__code__.co_freevars,[x.cell_contents for x in access.__closure__]))
    if not any(r.path.endswith('/governance') for r in app.routes):
        install_governance(app,app.state.store,acells['auth'],access,app.state.coordinator)
    if not any(r.path.endswith('/credentials') for r in app.routes):
        install_vault(app,app.state.store,acells['auth'],access)
    return app


def login(app,name):
    c=TestClient(app)
    r=c.post('/api/login',json={'username':name,'password':'test-password'})
    assert r.status_code==200,r.text
    c.headers['x-csrf-token']=r.json()['csrf']
    return c


def test_real_auth_restart_resource_conservation_and_project_isolation(tmp_path,monkeypatch):
    monkeypatch.setenv('SHUI_PUBLIC_ORIGIN','http://testserver')
    monkeypatch.delenv('SHUI_ORIGIN_FILE',raising=False)
    root=tmp_path/'data';app=installed(root)
    for name in ('owner','editor','reader','outsider'):
        app.state.store.create_user(name,name,'test-password','owner' if name=='owner' else 'member')
    with login(app,'owner') as o, login(app,'editor') as e, login(app,'reader') as r, login(app,'outsider') as x:
        pid=o.post('/api/projects',json={'name':'integrity','provider':'codex'}).json()['id'];b='/api/projects/'+pid
        for name,role in [('editor','editor'),('reader','viewer')]:
            assert o.post(b+'/members',json={'username':name,'role':role}).status_code==200
        idea=e.post(b+'/ideas',json={'title':'Measured GPU stage','body':'Persist this exact revision'}).json()
        pool=o.post(b+'/resource-pools',json={'kind':'gpu','unit':'hours','capacity':'1.000001'}).json()
        def ask(n,parent=None):
            z=e.post(b+'/resource-requests',json={'idea_id':idea['id'],'kind':'gpu','unit':'hours','amount':n,'parent_id':parent,'reason':'Actual stage'})
            assert z.status_code==201,z.text
            return z.json()
        parent=ask('1.000001');a=o.post(b+f"/resource-requests/{parent['id']}/decision",json={'decision':'approve'}).json()
        child=ask('0.333333',parent['id']);d=o.post(b+f"/resource-requests/{child['id']}/decision",json={'decision':'approve'}).json()
        aid=d['allocation_id']
        assert e.post(b+f'/allocations/{aid}/receipts',json={'amount':'0.111111','evidence':'actual scheduler record'}).status_code==201
        assert e.post(b+f"/resource-requests/{child['id']}/cancel",json={}).status_code==200
        assert e.post(b+f"/resource-requests/{parent['id']}/cancel",json={}).status_code==200
        assert e.post(b+f'/allocations/{aid}/receipts',json={'amount':'0.000001','evidence':'after cancel'}).status_code==409
        before=o.get(b+'/governance').json();p=before['pools'][0]
        assert Decimal(p['reserved'])==0 and Decimal(p['consumed'])==Decimal('0.111111')
        assert Decimal(p['capacity'])==Decimal('1.000001')
        secret='sk-independent-roundtrip-secret-12345'
        cr=o.post(b+'/credentials',json={'name':'Research model','provider':'deepseek','secret':secret})
        assert cr.status_code==200,cr.text
        cid=cr.json()['id']
        view=r.get(b+'/credentials');assert view.status_code==200 and secret not in view.text
        assert view.json()['credentials'][0]['id']==cid
        assert r.post(b+'/credentials',json={'secret':'private'}).status_code==403
        assert e.post(b+'/credentials/'+cid+'/rotate',json={'secret':'private'}).status_code==403
        assert x.get(b+'/credentials').status_code==404
        assert x.get(b+'/governance').status_code==404
        assert secret.encode() not in app.state.store.path.read_bytes()
    restarted=installed(root)
    with login(restarted,'owner') as c:
        after=c.get(b+'/governance').json()
        assert after['pools']==before['pools'] and after['ledger']==before['ledger']
        assert c.get(b+'/credentials').json()['credentials'][0]['id']==cid
        with restarted.state.store.connect() as db:
            encoded=db.execute('SELECT secret FROM vault_credentials WHERE id=?',(cid,)).fetchone()[0]
            assert restarted.state.vault.decrypt_secret(encoded)==secret
        assert c.post(b+'/credentials/'+cid+'/revoke',json={}).status_code==200
        assert c.post(b+'/credentials/'+cid+'/rotate',json={'secret':'retry'}).status_code==409


def test_import_failure_is_atomic_and_assist_correlates_to_actual_turn(tmp_path,monkeypatch):
    monkeypatch.setenv('SHUI_PUBLIC_ORIGIN','http://testserver')
    monkeypatch.delenv('SHUI_ORIGIN_FILE',raising=False)
    app=installed(tmp_path/'data');app.state.store.create_user('owner','Owner','test-password','owner')
    kicked=[];monkeypatch.setattr(app.state.coordinator,'kick',lambda pid:kicked.append(pid))
    with login(app,'owner') as c:
        pid=c.post('/api/projects',json={'name':'Correlation','provider':'codex'}).json()['id'];b='/api/projects/'+pid
        bad=c.post(b+'/ideas/import',json={'ideas':[{'id':'good','title':'Valid'},{'id':'bad','title':''}]})
        assert bad.status_code==400
        assert c.get(b+'/governance').json()['ideas']==[]
        secret='sk-private-integration-secret-92345'
        assert c.post(b+'/credentials',json={'name':'Model','secret':secret}).status_code==200
        idea=c.post(b+'/ideas',json={'title':'Investigate','body':'Use '+secret+' only through credential references'}).json()
        queued=c.post(b+f"/ideas/{idea['id']}/assist",json={'kind':'check'})
        assert queued.status_code==201,queued.text
        data=queued.json();tid=data['execution']['turn_id']
        assert data['turn']['id']==tid and kicked==[pid]
        with app.state.store.connect() as db:
            msg=db.execute("SELECT content FROM messages WHERE turn_id=? AND role='user'",(tid,)).fetchone()[0]
            assert secret not in msg
            db.execute("UPDATE turns SET status='failed' WHERE id=?",(tid,))
            db.execute("UPDATE messages SET error='Actual provider failure' WHERE turn_id=? AND role='assistant'",(tid,))
        response=c.get(b+'/governance')
        assert secret not in response.text
        actual=response.json()['executions'][0]
        assert actual['status']=='failed' and actual['error']=='Actual provider failure'
        assert actual['result']==''


def test_legacy_secret_migration_erases_plaintext_pages_and_survives_restart(tmp_path,monkeypatch):
    from shui.store import Store
    monkeypatch.setenv('SHUI_PUBLIC_ORIGIN','http://testserver')
    app=installed(tmp_path/'data');app.state.store.create_user('owner','Owner','test-password','owner')
    with login(app,'owner') as c:
        pid=c.post('/api/projects',json={'name':'Legacy','provider':'codex'}).json()['id']
        with app.state.store.connect() as db:
            user=dict(db.execute("SELECT * FROM users WHERE username='owner'").fetchone())
            result=app.state.store.enqueue_turn(db,pid,user,'token:old-integration-plaintext-938376',[], 'legacy')
            mid=result['turn']['user_message_id']
            # Model the actual old persisted row, without depending on old Store code.
            db.execute('UPDATE message_secrets SET value=? WHERE message_id=?',('token:old-integration-plaintext-938376',mid))
    assert b'old-integration-plaintext-938376' in app.state.store.path.read_bytes()
    migrated=Store(tmp_path/'data')
    for file in migrated.root.glob('shui.sqlite*'):
        assert b'old-integration-plaintext-938376' not in file.read_bytes(),file
    with migrated.connect() as db:
        row=db.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
        assert migrated.message_content(db,row)=='token:old-integration-plaintext-938376'
    Store(tmp_path/'data')


@pytest.mark.parametrize('provider',['codex','claude'])
def test_readonly_adapter_protocol_is_server_enforced(tmp_path,monkeypatch,provider):
    import asyncio
    import sys
    from shui.agents import RunRequest,run_agent
    from test_agents import FAKE_CLI,records
    cli=tmp_path/'fake-agent';cli.write_text('#!'+sys.executable+'\n'+FAKE_CLI);cli.chmod(0o755)
    capture=tmp_path/'protocol.jsonl';workspace=tmp_path/'workspace';workspace.mkdir()
    for name,value in {'SHUI_PROXY_URL':'http://127.0.0.1:7890','SHUI_CODEX_BIN':str(cli),'SHUI_CLAUDE_BIN':str(cli),'SHUI_FAKE_CAPTURE':str(capture),'SHUI_LOCK_DIR':str(tmp_path/'locks'),'SHUI_AGENT_TIMEOUT_SECONDS':'10','SHUI_AGENT_IDLE_TIMEOUT_SECONDS':'5'}.items():monkeypatch.setenv(name,value)
    async def go():
        async def emit(*args):pass
        await run_agent(RunRequest(provider,workspace,'Assess only',None,'chosen-model',read_only=True),emit,asyncio.Event())
    asyncio.run(go());r=records(capture);argv=r[0]['argv']
    if provider=='codex':
        assert 'sandbox_mode="read-only"' in argv and 'sandbox_mode="danger-full-access"' not in argv
        thread=next(x for x in r if x.get('method')=='thread/start')['params']
        turn=next(x for x in r if x.get('method')=='turn/start')['params']
        assert thread['sandbox']=='read-only' and turn['sandboxPolicy']=={'type':'readOnly'}
    else:
        assert '--dangerously-skip-permissions' not in argv
        assert argv[argv.index('--permission-mode')+1]=='plan'
        assert argv[argv.index('--tools')+1]==''
