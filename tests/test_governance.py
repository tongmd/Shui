"""Real authenticated HTTP checks of resource conservation and idea provenance."""
import pytest
from fastapi.testclient import TestClient
from shui.app import create_app
from shui.governance import install_governance

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SHUI_PUBLIC_ORIGIN','http://testserver')
    monkeypatch.delenv('SHUI_ORIGIN_FILE',raising=False)
    app=create_app(tmp_path/'data',start_worker=False)
    if not any(r.path.endswith('/governance') for r in app.routes):
        endpoint=next(r.endpoint for r in app.routes if r.path=='/api/projects/{pid}' and 'GET' in r.methods)
        cells=dict(zip(endpoint.__code__.co_freevars,[x.cell_contents for x in endpoint.__closure__]))
        access=cells['access'];acells=dict(zip(access.__code__.co_freevars,[x.cell_contents for x in access.__closure__]))
        install_governance(app,app.state.store,acells['auth'],access,app.state.coordinator)
    clients={}
    for name in ('owner','member','viewer','outside'):
        app.state.store.create_user(name,name,'test-password','owner' if name=='owner' else 'member')
        client=clients[name]=TestClient(app)
        login=client.post('/api/login',json={'username':name,'password':'test-password'}).json()
        client.headers['x-csrf-token']=login['csrf']
    p=clients['owner'].post('/api/projects',json={'name':'Test','provider':'codex'}).json()['id']
    for n in ('member','viewer'):
        assert clients['owner'].post(f'/api/projects/{p}/members',json={'username':n,'role':'editor' if n=='member' else 'viewer'}).status_code==200
    yield app,clients,f'/api/projects/{p}',p
    for c in clients.values():c.close()

def test_conservation_hierarchy_receipts_and_cancellation(setup):
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    i=m.post(b+'/ideas',json={'title':'GPU study','body':'Measured experiments'}).json()
    pool=o.post(b+'/resource-pools',json={'kind':'gpu','unit':'hours','capacity':100});assert pool.status_code==201,pool.text
    def req(n,parent=None):return m.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'gpu','unit':'hours','amount':n,'reason':'Stage budget','parent_id':parent}).json()
    r=req(80);assert m.post(b+f"/resource-requests/{r['id']}/decision",json={'decision':'approve'}).status_code==403
    upstream=o.post(b+f"/resource-requests/{r['id']}/decision",json={'decision':'approve'}).json()
    child=req(50,r['id']);a=o.post(b+f"/resource-requests/{child['id']}/decision",json={'decision':'approve'}).json()['allocation_id']
    bad=req(31,r['id']);assert o.post(b+f"/resource-requests/{bad['id']}/decision",json={'decision':'approve'}).status_code==409
    assert m.post(b+f'/allocations/{a}/receipts',json={'amount':20,'evidence':'Scheduler invoice 42','outcome':'negative result'}).status_code==201
    assert m.post(b+f'/allocations/{a}/receipts',json={'amount':31,'evidence':'Overspend'}).status_code==409
    assert m.post(b+f"/resource-requests/{r['id']}/cancel",json={}).status_code==409
    assert m.post(b+f"/resource-requests/{child['id']}/cancel",json={}).status_code==200
    assert m.post(b+f"/resource-requests/{r['id']}/cancel",json={}).status_code==200
    data=o.get(b+'/governance').json();pool=data['pools'][0]
    assert float(pool['reserved'])==0 and float(pool['consumed'])==20 and float(pool['capacity'])==100
    assert data['ledger']['valid']

def test_scope_roles_validation_import_and_tamper(setup):
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    assert cs['outside'].get(b+'/governance').status_code==404
    assert cs['viewer'].post(b+'/ideas',json={'title':'x'}).status_code==403
    i=m.post(b+'/ideas',json={'title':'v1','body':'body'}).json()
    assert m.post(b+f"/ideas/{i['id']}/revisions",json={'title':'v2','body':'new'}).status_code==201
    f=m.post(b+f"/ideas/{i['id']}/feedback",json={'content':'Check evidence'}).json()
    assert f['revision']==2
    assert m.post(b+f"/ideas/{i['id']}/feedback",json={'content':'Reply','reply_to':f['id']}).status_code==201
    assert m.post(b+'/ideas',json={'title':'cycle','parent_id':'unknown'}).status_code==404
    for n in ('NaN','Infinity',-1,True,1.0000001):
        assert o.post(b+'/resource-pools',json={'kind':str(n),'unit':'tokens','capacity':n}).status_code==400
    source={'id':'circu-1','title':'Existing','body':'Preserved','reviews':[{'author':'prior','text':'original'}]}
    first=m.post(b+'/ideas/import',json=source).json()['ideas'][0]
    assert m.post(b+'/ideas/import',json=source).json()['ideas'][0]['id']==first['id']
    assert first['historical']['reviews']==source['reviews']
    # Foreign project IDs cannot be connected or read.
    p2=o.post('/api/projects',json={'name':'Other','provider':'codex'}).json()['id']
    foreign=o.post(f'/api/projects/{p2}/ideas',json={'title':'Private'}).json()['id']
    assert m.post(b+'/ideas',json={'title':'Cross','parent_id':foreign}).status_code==404
    assert m.post(b+f"/ideas/{i['id']}/execute",json={'content':'Do it'}).status_code==400
    with app.state.store.connect() as c:c.execute("UPDATE governance_ledger SET event='{}' WHERE project_id=? AND seq=(SELECT MIN(seq) FROM governance_ledger WHERE project_id=?)",(pid,pid))
    assert o.get(b+'/governance').json()['ledger']['valid'] is False


def test_execution_idempotency_revision_budget_and_assist(setup,monkeypatch):
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    monkeypatch.setattr(app.state.coordinator,'kick',lambda pid:None)
    i=m.post(b+'/ideas',json={'title':'Study','body':'Do actual work'}).json()
    o.post(b+'/resource-pools',json={'kind':'tokens','unit':'tokens','capacity':1000})
    r=m.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'tokens','unit':'tokens','amount':800,'reason':'bounded stage'}).json()
    approved=o.post(b+f"/resource-requests/{r['id']}/decision",json={'decision':'approve'}).json()
    aid=approved['allocation_id'];endpoint=b+f"/ideas/{i['id']}/execute"
    first=m.post(endpoint,json={'allocation_id':aid,'content':'Run stage','client_id':'once'})
    assert first.status_code==201,first.text
    again=m.post(endpoint,json={'allocation_id':aid,'content':'Run stage','client_id':'once'})
    assert first.json()['turn']['id']==again.json()['turn']['id']
    assert m.post(endpoint,json={'allocation_id':aid,'content':'Second stage','client_id':'two'}).status_code==409
    assert m.post(b+f"/resource-requests/{r['id']}/cancel",json={}).status_code==409
    with app.state.store.connect() as c:
        msg=c.execute('SELECT content FROM messages WHERE id=?',(first.json()['turn']['user_message_id'],)).fetchone()[0]
        assert '800' in msg and 'tokens' in msg and 'bounded stage' in msg
        c.execute("UPDATE turns SET status='complete' WHERE id=?",(first.json()['turn']['id'],))
    m.post(b+f"/ideas/{i['id']}/revisions",json={'title':'Changed stage','body':'New work'})
    assert m.post(endpoint,json={'allocation_id':aid,'content':'New stage','client_id':'three'}).status_code==409
    assist=m.post(b+f"/ideas/{i['id']}/assist",json={'kind':'check'})
    assert assist.status_code==201,assist.text
    assert assist.json()['execution']['read_only'] is True
    snap=o.get(b+'/governance').json()
    assert snap['executions'][0]['status']=='complete' and snap['ledger']['valid']

def test_receipt_retry_and_state_corruption_detection(setup):
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    i=m.post(b+'/ideas',json={'title':'One','body':'Stage'}).json()
    o.post(b+'/resource-pools',json={'kind':'gpu','unit':'hours','capacity':10})
    r=m.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'gpu','unit':'hours','amount':8,'reason':'run'}).json()
    a=o.post(b+f"/resource-requests/{r['id']}/decision",json={'decision':'approve'}).json()['allocation_id']
    payload={'amount':3,'evidence':'invoice same receipt'}
    one=m.post(b+f'/allocations/{a}/receipts',json=payload).json()
    two=m.post(b+f'/allocations/{a}/receipts',json=payload).json()
    assert one['id']==two['id']
    snap=o.get(b+'/governance').json();assert snap['pools'][0]['consumed']=='3'
    with app.state.store.connect() as c:c.execute("UPDATE governance_objects SET data=json_set(data,'$.title','CORRUPT') WHERE id=?",(i['id'],))
    assert o.get(b+'/governance').json()['ledger']['valid'] is False

def test_governance_credentials_are_saved_not_exposed(setup):
    app,cs,b,pid=setup;m=cs['member']
    secret='sk-governance-private-1234567890'
    made=m.post(b+'/ideas',json={'title':'Key test','body':'Use token:'+secret})
    assert made.status_code==201,made.text
    assert secret not in made.text
    snap=m.get(b+'/governance')
    assert secret not in snap.text
    with app.state.store.connect() as c:
        raw=' '.join(r[0] for r in c.execute('SELECT data FROM governance_objects'))+' '.join(r[0] for r in c.execute('SELECT event FROM governance_ledger'))
        assert secret not in raw
        assert secret in app.state.store.project_secret_values(c,pid)

def test_escalation_and_concurrent_approval_are_atomic(setup):
    from concurrent.futures import ThreadPoolExecutor
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    with app.state.store.connect() as c:owner_id=c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    i=m.post(b+'/ideas',json={'title':'Stage','body':'Resource chain'}).json()
    o.post(b+'/resource-pools',json={'kind':'tokens','unit':'tokens','capacity':100})
    child=m.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'tokens','unit':'tokens','amount':60,'reason':'Stage'}).json()
    escalated=m.post(b+f"/resource-requests/{child['id']}/escalate",json={'approver_id':owner_id,'reason':'Upstream budget','amount':80})
    assert escalated.status_code==201,escalated.text
    upstream=escalated.json()['upstream']
    assert o.post(b+f"/resource-requests/{child['id']}/decision",json={'decision':'approve'}).status_code==409
    url=b+f"/resource-requests/{upstream['id']}/decision"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:o.post(url,json={'decision':'approve'}).status_code,range(2)))
    assert sorted(results)==[200,409]
    assert o.post(b+f"/resource-requests/{child['id']}/decision",json={'decision':'approve'}).status_code==200
    snap=o.get(b+'/governance').json()
    assert snap['pools'][0]['reserved']=='80' and snap['ledger']['valid']

def test_credential_binding_is_project_scoped_and_execution_only(setup,monkeypatch):
    app,cs,b,pid=setup;o=cs['owner'];m=cs['member']
    monkeypatch.setattr(app.state.coordinator,'kick',lambda pid:None)
    secret='resource-execution-only-secret'
    credential=o.post(b+'/credentials',json={'name':'GPU account','provider':'gpu','category':'gpu','secret':secret}).json()
    assert 'id' in credential,credential
    i=m.post(b+'/ideas',json={'title':'GPU stage','body':'Run permitted experiment'}).json()
    p=o.post(b+'/resource-pools',json={'kind':'gpu','unit':'hours','capacity':10,'credential_id':credential['id']})
    assert p.status_code==201,p.text
    bad=o.post(b+'/resource-pools',json={'kind':'other','unit':'hours','capacity':10,'credential_id':'unknown'})
    assert bad.status_code==400
    r=m.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'gpu','unit':'hours','amount':3,'reason':'Stage'}).json()
    aid=o.post(b+f"/resource-requests/{r['id']}/decision",json={'decision':'approve'}).json()['allocation_id']
    execution=m.post(b+f"/ideas/{i['id']}/execute",json={'allocation_id':aid,'content':'Execute'}).json()
    assert execution['execution']['credential_id']==credential['id']
    assist=m.post(b+f"/ideas/{i['id']}/assist",json={'kind':'check'}).json()
    assert assist['execution']['credential_id'] is None
    assert secret not in m.get(b+'/governance').text

def test_real_manager_chain_requires_upstream_funding(setup):
    app,cs,b,pid=setup;o=cs['owner'];staff=cs['member'];managers={};ids={}
    with app.state.store.connect() as c:ids['owner']=c.execute("SELECT id FROM users WHERE username='owner'").fetchone()[0]
    for name in ('supervisor','director'):
        user=app.state.store.create_user(name,name,'test-password','member');ids[name]=user['id']
        client=TestClient(app);session=client.post('/api/login',json={'username':name,'password':'test-password'}).json();client.headers['x-csrf-token']=session['csrf'];managers[name]=client
        assert o.post(b+'/members',json={'username':name,'role':'editor'}).status_code==200
    try:
        assert o.post(b+'/resource-approvers',json={'user_id':ids['director'],'parent_id':ids['owner']}).status_code==201
        assert o.post(b+'/resource-approvers',json={'user_id':ids['supervisor'],'parent_id':ids['director']}).status_code==201
        assert staff.post(b+'/resource-approvers',json={'user_id':ids['supervisor']}).status_code==403
        assert o.post(b+'/resource-approvers',json={'user_id':ids['director'],'parent_id':ids['supervisor']}).status_code==409
        i=staff.post(b+'/ideas',json={'title':'Multi-level compute','body':'A real approval chain'}).json()
        o.post(b+'/resource-pools',json={'kind':'gpu','unit':'hours','capacity':100})
        child=staff.post(b+'/resource-requests',json={'idea_id':i['id'],'kind':'gpu','unit':'hours','amount':60,'reason':'Study','approver_id':ids['supervisor']}).json()
        path=lambda r:b+f"/resource-requests/{r['id']}"
        assert managers['supervisor'].post(path(child)+'/decision',json={'decision':'approve'}).status_code==409
        assert managers['director'].post(path(child)+'/decision',json={'decision':'approve'}).status_code==403
        assert managers['supervisor'].post(path(child)+'/escalate',json={'approver_id':ids['owner'],'reason':'Skip manager'}).status_code==400
        middle=managers['supervisor'].post(path(child)+'/escalate',json={'approver_id':ids['director'],'reason':'Need team resources'}).json()['upstream']
        assert managers['director'].post(path(middle)+'/decision',json={'decision':'approve'}).status_code==409
        top=managers['director'].post(path(middle)+'/escalate',json={'approver_id':ids['owner'],'reason':'Need organization capacity'}).json()['upstream']
        assert o.post(path(top)+'/decision',json={'decision':'approve'}).status_code==200
        assert managers['director'].post(path(middle)+'/decision',json={'decision':'approve'}).status_code==200
        assert managers['supervisor'].post(path(child)+'/decision',json={'decision':'approve'}).status_code==200
        snap=staff.get(b+'/governance').json()
        assert snap['pools'][0]['reserved']=='60' and snap['ledger']['valid']
        assert {a['id'] for a in snap['approvers']}=={ids['owner'],ids['director'],ids['supervisor']}
    finally:
        for client in managers.values():client.close()
