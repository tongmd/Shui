import json
import stat
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from shui.store import Store, now
from shui.vault import Vault, install_vault, classify


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'data')
    owner=store.create_user('vault-owner','Owner','test-password','owner')
    with store.connect() as c:
        for pid in ('one','two'):
            c.execute('INSERT INTO projects(id,name,provider,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)',(pid,pid,'codex',owner['id'],now(),now()))
    app=FastAPI()
    def access(request,pid,write=False,owner=False):
        role=request.headers.get('role')
        if role is None: raise HTTPException(401)
        if role=='outsider': raise HTTPException(404)
        if owner and role!='owner': raise HTTPException(403)
        return {'id':'actor'},role
    vault=install_vault(app,store,None,access)
    return store,vault,TestClient(app)


def test_api_scope_rotation_and_audit(setup):
    store,vault,client=setup
    path='/api/projects/one/credentials'
    value='sk-real-test-secret-that-never-leaks'
    assert client.get(path).status_code==401
    assert client.get(path,headers={'role':'outsider'}).status_code==404
    assert client.post(path,headers={'role':'editor'},json={'secret':value}).status_code==403
    response=client.post(path,headers={'role':'owner'},json={'secret':value,'name':'Inference','provider':'deepseek','scopes':['generation']})
    assert response.status_code==200,response.text
    row=response.json(); assert row['category']=='llm' and value not in response.text
    cid=row['id']; headers={'role':'owner'}
    assert not client.get('/api/projects/two/credentials',headers=headers).json()['credentials']
    assert client.post(f'/api/projects/two/credentials/{cid}/rotate',headers=headers,json={'secret':'another-private-value'}).status_code==404
    with store.connect() as c:
        stored=c.execute('SELECT secret FROM vault_credentials').fetchone()[0]
        assert value not in stored and vault.decrypt_secret(stored)==value
    assert client.post(path+'/'+cid+'/rotate',headers=headers,json={'secret':'another-private-value'}).json()['revision']==2
    assert client.post(path+'/'+cid+'/revoke',headers=headers).json()['status']=='revoked'
    assert client.post(path+'/'+cid+'/rotate',headers=headers,json={'secret':'nope'}).status_code==409
    with store.connect() as c:
        assert [r[0] for r in c.execute('SELECT action FROM vault_audit ORDER BY rowid')]==['created','rotated','revoked']
        assert c.execute('SELECT secret FROM vault_credentials').fetchone()[0]==''


def test_key_integrity_and_loss(setup):
    store,vault,_=setup
    encoded=vault.encrypt_secret('private-credential')
    assert vault.decrypt_secret(encoded)=='private-credential'
    with pytest.raises(RuntimeError): vault.decrypt_secret(encoded[:-6]+'abcdef')
    with pytest.raises(RuntimeError): vault.decrypt_secret('plaintext')
    assert stat.S_IMODE((store.root/'vault/master.key').stat().st_mode)==0o600
    vault.create('one','actor',{'secret':'test-private-secret'})
    (store.root/'vault/master.key').unlink()
    with pytest.raises(RuntimeError,match='key missing'): Vault(store)


def test_capture_atomic_and_metadata_safety(setup):
    store,vault,_=setup
    with store.connect() as c:
        redacted,refs=vault.capture_tokens('one','actor','Use token:sk-long-new-private-token for this.',connection=c)
        assert redacted=='Use token:[redacted] for this.' and len(refs)==1
        assert c.execute('SELECT COUNT(*) FROM vault_credentials').fetchone()[0]==1
        again, refs2 = vault.capture_tokens('one','actor','token:sk-long-new-private-token',connection=c)
        assert refs2 == refs and c.execute('SELECT COUNT(*) FROM vault_credentials').fetchone()[0]==1
    assert classify('nvidia','GPU')=='gpu'
    assert classify('host','SSH')=='ssh'
    for data in ({'secret':'private-value','endpoint':'https://x.test?api_key=abc'}, {'secret':'private-value','name':'private-value'}):
        with pytest.raises(HTTPException): vault.create('one','actor',data)


def test_execution_resolution_requires_active_scope_and_unexpired(setup):
    store,vault,client=setup
    with store.connect() as c:
        actor=c.execute("SELECT id FROM users WHERE username='vault-owner'").fetchone()[0]
    record=vault.create('one',actor,{'secret':'exec-private-value'})
    with store.connect() as c:
        value,metadata=vault.resolve_for_execution(c,'one',record['id'],actor)
        assert value=='exec-private-value' and 'secret' not in metadata
        assert c.execute("SELECT action FROM vault_audit ORDER BY rowid DESC LIMIT 1").fetchone()[0]=='execution_used'
        for pid,who,code in [('two',actor,404),('one','unknown-user',403)]:
            with pytest.raises(HTTPException) as error: vault.resolve_for_execution(c,pid,record['id'],who)
            assert error.value.status_code==code
        c.execute("UPDATE vault_credentials SET expires_at='2020-01-01T00:00:00Z' WHERE id=?",(record['id'],))
        with pytest.raises(HTTPException) as error: vault.resolve_for_execution(c,'one',record['id'],actor)
        assert error.value.detail=='Credential is expired'
        c.execute("UPDATE vault_credentials SET expires_at=NULL,status='revoked' WHERE id=?",(record['id'],))
        with pytest.raises(HTTPException) as error: vault.resolve_for_execution(c,'one',record['id'],actor)
        assert error.value.detail=='Credential is revoked'
    assert client.get('/api/projects/one/credentials',headers={'role':'owner'}).status_code==200
    assert 'exec-private-value' not in client.get('/api/projects/one/credentials',headers={'role':'owner'}).text


def test_context_classification_is_local_and_sanitized(setup):
    store,vault,_=setup
    text='Use Aliyun endpoint https://dashscope.aliyuncs.com/compatible-mode/v1?key=other for token:sk-context-test-secret'
    _,refs=vault.capture_tokens('one','actor',text)
    with store.connect() as c:
        row=c.execute('SELECT * FROM vault_credentials WHERE id=?',(refs[0],)).fetchone()
        assert row['provider']=='aliyun' and row['category']=='llm'
        assert row['endpoint']=='https://dashscope.aliyuncs.com/compatible-mode/v1'
        assert 'other' not in row['endpoint'] and 'sk-context-test-secret' not in json.dumps(vault.metadata(row))
