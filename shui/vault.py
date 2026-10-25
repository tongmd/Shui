"""Project credential inventory. Secrets never leave the server through HTTP APIs."""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import re
import secrets
import stat
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from .store import now, uid

PREFIX = 'shui:vault:v1:'
TOKEN = re.compile(r"(?i)\btoken:([^\s`'\"<>]{1,4096})")


def classify(provider='', purpose=''):
    value = (provider + ' ' + purpose).lower()
    if any(x in value for x in ('ssh', 'gpu', 'nvidia', 'cuda', '显卡')):
        return 'gpu' if 'ssh' not in value else 'ssh'
    return 'llm' if any(x in value for x in ('openai', 'anthropic', 'deepseek', 'qwen', 'aliyun', 'token', '模型')) else 'other'


def capture_context(text, token_start, secret):
    """Infer only local labels from nearby explicit text; never inspect a remote service."""
    surrounding = TOKEN.sub('token:[redacted]', text[max(0,token_start-240):token_start+240])
    endpoint = ''
    for candidate in re.findall(r"(?:https?|ssh)://[^\s<>`\"']+", surrounding):
        try:
            parts = urlsplit(candidate.rstrip('.,;，。；)'))
        except ValueError:
            continue
        if not parts.hostname or parts.username or parts.password or secret in candidate:
            continue
        endpoint = parts._replace(query='', fragment='').geturl()
        break
    labels = {'aliyun':('aliyun','dashscope','阿里云'), 'openai':('openai',),
              'anthropic':('anthropic','claude'), 'deepseek':('deepseek',),
              'qwen':('qwen',), 'nvidia':('nvidia','cuda'), 'ssh':('ssh',)}
    lowered = surrounding.lower()
    provider = next((name for name, terms in labels.items() if any(term in lowered for term in terms)), '')
    category = classify(provider, surrounding)
    return {'name': (provider + ' token').strip() if provider else 'Saved token',
            'provider': provider, 'category': category, 'purpose':'Saved from an explicit token submission', 'endpoint':endpoint}


class Vault:
    def __init__(self, store):
        self.store = store
        directory = store.root / 'vault'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise RuntimeError('Unsafe vault directory')
        os.chmod(directory, 0o700)
        key = directory / 'master.key'
        if key.is_symlink():
            raise RuntimeError('Unsafe vault key')
        with store.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS vault_credentials(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,created_by TEXT NOT NULL,name TEXT NOT NULL,provider TEXT NOT NULL,category TEXT NOT NULL,purpose TEXT NOT NULL,endpoint TEXT NOT NULL,scopes TEXT NOT NULL,expires_at TEXT,secret TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vault_audit(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,credential_id TEXT NOT NULL,actor_id TEXT NOT NULL,action TEXT NOT NULL,at TEXT NOT NULL);''')
            existing = c.execute('SELECT 1 FROM vault_credentials LIMIT 1').fetchone()
            encrypted_messages = c.execute('SELECT 1 FROM message_secrets WHERE value LIKE ? LIMIT 1', (PREFIX+'%',)).fetchone()
            encrypted_delegations = c.execute('SELECT 1 FROM delegation_request_secrets WHERE value LIKE ? LIMIT 1', (PREFIX+'%',)).fetchone()
        if not key.exists():
            if existing or encrypted_messages or encrypted_delegations:
                raise RuntimeError('Vault key missing; restore the original key')
            try:
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(Fernet.generate_key()); out.flush(); os.fsync(out.fileno())
            except FileExistsError:
                pass
        if key.is_symlink() or not stat.S_ISREG(key.stat().st_mode):
            raise RuntimeError('Unsafe vault key')
        os.chmod(key, 0o600)
        self.fernet = Fernet(key.read_bytes())

    def encrypt_secret(self, value):
        return PREFIX + self.fernet.encrypt(value.encode()).decode()

    def decrypt_secret(self, value):
        if not value.startswith(PREFIX):
            raise RuntimeError('Unencrypted credential refused')
        try:
            return self.fernet.decrypt(value[len(PREFIX):].encode()).decode()
        except (InvalidToken, UnicodeError, ValueError):
            raise RuntimeError('Credential integrity check failed') from None

    @staticmethod
    def metadata(row):
        return {k: row[k] for k in ('id','project_id','name','provider','category','purpose','endpoint','scopes','expires_at','fingerprint','status','revision','created_at','updated_at')} | {'scopes': json.loads(row['scopes']), 'secret_saved': row['status'] == 'active'}

    def audit(self, c, pid, cid, actor, action):
        c.execute('INSERT INTO vault_audit VALUES(?,?,?,?,?,?)', (uid(),pid,cid,actor,action,now()))

    def create(self, pid, actor, data, connection=None):
        allowed = {'name','provider','category','purpose','endpoint','scopes','expires_at','secret'}
        if not isinstance(data, dict) or set(data)-allowed:
            raise HTTPException(400, 'Invalid credential fields')
        secret = data.get('secret')
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384:
            raise HTTPException(400, 'A credential value is required')
        fields = {k: data.get(k, '') for k in ('name','provider','purpose','endpoint')}
        if any(not isinstance(v,str) or len(v)>500 for v in fields.values()):
            raise HTTPException(400, 'Invalid credential metadata')
        # Never permit submitted secrets in public metadata, including endpoint credentials.
        if any(secret in v for v in fields.values()):
            raise HTTPException(400, 'Do not include credential values in metadata')
        if fields['endpoint']:
            try:
                parts = urlsplit(fields['endpoint'])
            except ValueError:
                raise HTTPException(400, 'Invalid endpoint') from None
            if parts.scheme not in ('http','https','ssh') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise HTTPException(400, 'Use an endpoint without credentials or query parameters')
        scopes = data.get('scopes', [])
        if not isinstance(scopes,list) or len(scopes)>32 or any(not isinstance(x,str) or len(x)>100 or secret in x for x in scopes):
            raise HTTPException(400, 'Invalid scopes')
        expiry = data.get('expires_at') or None
        if expiry:
            try: datetime.datetime.fromisoformat(expiry.replace('Z','+00:00'))
            except (ValueError, AttributeError): raise HTTPException(400,'Invalid expiry') from None
        category = data.get('category') or classify(fields['provider'], fields['purpose'])
        if category not in ('gpu','llm','ssh','other'): raise HTTPException(400,'Invalid category')
        cid = uid(); timestamp=now()
        import contextlib
        with (contextlib.nullcontext(connection) if connection is not None else self.store.connect()) as c:
            c.execute('INSERT INTO vault_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (cid,pid,actor,fields['name'] or 'Credential',fields['provider'],category,fields['purpose'],fields['endpoint'],json.dumps(scopes),expiry,self.encrypt_secret(secret),hashlib.sha256(secret.encode()).hexdigest()[:12],'active',1,timestamp,timestamp))
            self.audit(c,pid,cid,actor,'created')
            return self.metadata(c.execute('SELECT * FROM vault_credentials WHERE id=?',(cid,)).fetchone())

    def resolve_for_execution(self, c, pid, cid, actor_id):
        """Internal only. Caller must additionally prove the exact approved allocation.

        Returns (secret, metadata), never an HTTP response. Does not grant authority,
        make network requests, or serialize a secret into project events.
        """
        actor = c.execute('SELECT * FROM users WHERE id=?', (actor_id,)).fetchone()
        if not actor or self.store.project_role(c, pid, actor) not in ('owner','editor'):
            raise HTTPException(403, 'Resource execution permission required')
        row = c.execute('SELECT * FROM vault_credentials WHERE id=? AND project_id=?', (cid,pid)).fetchone()
        if not row:
            raise HTTPException(404, 'Credential not found')
        if row['status'] != 'active':
            raise HTTPException(409, 'Credential is revoked')
        if row['expires_at']:
            try:
                expiry = datetime.datetime.fromisoformat(row['expires_at'].replace('Z','+00:00'))
                if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=datetime.timezone.utc)
            except (ValueError, AttributeError):
                raise HTTPException(409, 'Credential expiry is invalid') from None
            if expiry <= datetime.datetime.now(datetime.timezone.utc):
                raise HTTPException(409, 'Credential is expired')
        value = self.decrypt_secret(row['secret'])
        self.audit(c,pid,cid,actor_id,'execution_used')
        return value, self.metadata(row)

    def project_values(self, connection, pid):
        """Runtime redaction only: never serialize these values through an API."""
        return tuple(self.decrypt_secret(row[0]) for row in connection.execute(
            "SELECT secret FROM vault_credentials WHERE project_id=? AND status='active'", (pid,)))

    def capture_tokens(self, pid, actor, text, connection=None):
        refs=[]
        def capture(match):
            if match.group(1) == '[redacted]': return match.group(0)
            import contextlib
            with (contextlib.nullcontext(connection) if connection is not None else self.store.connect()) as c:
                fingerprint = hashlib.sha256(match.group(1).encode()).hexdigest()[:12]
                existing = c.execute("SELECT id,secret FROM vault_credentials WHERE project_id=? AND fingerprint=? AND status='active'", (pid,fingerprint)).fetchall()
                found = next((r['id'] for r in existing if secrets.compare_digest(self.decrypt_secret(r['secret']), match.group(1))), None)
                row = {'id':found} if found else self.create(pid,actor,{'secret':match.group(1),**capture_context(text, match.start(), match.group(1))}, connection=c)
            if row['id'] not in refs: refs.append(row['id'])
            return 'token:[redacted]'
        return TOKEN.sub(capture,text), refs


def install_vault(app, store, auth, access):
    vault=getattr(store, "vault", None) or Vault(store); app.state.vault=vault

    @app.get('/api/projects/{pid}/credentials')
    def inventory(pid: str, request: Request):
        access(request,pid)
        with store.connect() as c:
            return {'credentials':[vault.metadata(r) for r in c.execute('SELECT * FROM vault_credentials WHERE project_id=? ORDER BY created_at',(pid,))]}

    @app.post('/api/projects/{pid}/credentials')
    async def create(pid: str, request: Request):
        actor,_=access(request,pid,True,True)
        return vault.create(pid,actor['id'],await request.json())

    @app.post('/api/projects/{pid}/credentials/{cid}/rotate')
    async def rotate(pid: str, cid: str, request: Request):
        actor,_=access(request,pid,True,True); data=await request.json()
        value=data.get('secret') if isinstance(data,dict) else None
        if not isinstance(value,str) or not 1<=len(value)<=16384: raise HTTPException(400,'A credential value is required')
        with store.connect() as c:
            row=c.execute('SELECT * FROM vault_credentials WHERE id=? AND project_id=?',(cid,pid)).fetchone()
            if not row: raise HTTPException(404,'Credential not found')
            if row['status']!='active': raise HTTPException(409,'Credential is revoked')
            if any(value in row[k] for k in ('name','provider','purpose','endpoint','scopes')): raise HTTPException(400,'Credential value conflicts with metadata')
            c.execute('UPDATE vault_credentials SET secret=?,fingerprint=?,revision=revision+1,updated_at=? WHERE id=?',(vault.encrypt_secret(value),hashlib.sha256(value.encode()).hexdigest()[:12],now(),cid))
            vault.audit(c,pid,cid,actor['id'],'rotated')
            return vault.metadata(c.execute('SELECT * FROM vault_credentials WHERE id=?',(cid,)).fetchone())

    @app.post('/api/projects/{pid}/credentials/{cid}/revoke')
    def revoke(pid: str, cid: str, request: Request):
        actor,_=access(request,pid,True,True)
        with store.connect() as c:
            row=c.execute('SELECT * FROM vault_credentials WHERE id=? AND project_id=?',(cid,pid)).fetchone()
            if not row: raise HTTPException(404,'Credential not found')
            if row['status']=='active':
                c.execute("UPDATE vault_credentials SET status='revoked',secret='',updated_at=? WHERE id=?",(now(),cid)); vault.audit(c,pid,cid,actor['id'],'revoked')
            return vault.metadata(c.execute('SELECT * FROM vault_credentials WHERE id=?',(cid,)).fetchone())
    return vault
