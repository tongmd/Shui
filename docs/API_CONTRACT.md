# Shui! API contract

JSON API under `/api`; all errors `{detail: string}`. Auth cookies are HttpOnly; mutating requests after login require `X-CSRF-Token` from `/api/session`. Server checks Origin. One project owns one active native Codex/Claude session. Turns from all members are serialized and persisted. Authentication is invite-only; there is no public registration.

- `GET /api/session` -> `{authenticated,user:{id,username,display_name,role:'owner'|'member'},csrf,providers:[{id,name,installed}],version}`. Unauthenticated user/csrf null.
- `POST /api/login {username,password}` same response; `POST /api/logout`.
- `GET /api/projects` -> `{projects:[Project]}`.
- `POST /api/projects {name,description?,provider:'codex'|'claude',model?,native_session_id?}` -> Project. Owner only. Workspace allocated server-side.
- `GET /api/projects/{id}` -> `{project:Project,messages:[Message],files:[File],members:[Member],events_cursor:number}`. All shared transcript paginated separately for older content.
- `PATCH /api/projects/{id} {name?,description?,model?,native_session_id?}` -> Project. Owner only, session/model changes require idle project. Provider immutable. Explicit native binding must be unique, never create separate session per member.
- `GET /api/projects/{id}/messages?before={created_at}&limit=100` -> `{messages:[Message]}`.
- `POST /api/projects/{id}/messages {content,attachment_ids:[],client_id:uuid}` -> `{message:Message,turn:Turn}`. Owner/editor. Idempotent client_id per project, returns existing request after network retry. User attribution comes from auth, not body.
- `POST /api/projects/{id}/turns/{turn_id}/cancel` -> `{ok:true}`. Owner/editor. Cancels own queued requests; project owner can cancel any turn. Current running turn can be interrupted by owner or original author.
- `POST /api/projects/{id}/turns/{turn_id}/retry {client_id:uuid}` -> `{message,turn}` only failed/interrupted turns; never automatic retry after restart.
- `GET /api/projects/{id}/events?after=cursor` SSE, cookie auth; each event has SSE id and JSON `{id,type,project_id,payload,created_at}`. Durable replay supports `Last-Event-ID`; periodic keepalive. Types `message.created`, `message.updated`, `turn.updated`, `agent.delta`, `agent.tool`, `project.updated`, `file.created`, `member.updated`, `presence`. Payload contains affected `message`/`turn`/`project` objects; agent.delta `{message_id,delta}`, agent.tool `{turn_id,name,status,content}`.
- `GET /api/projects/{id}/files` -> `{files:[File]}`.
- `POST /api/projects/{id}/files` multipart field `file` -> File. Store bytes in project workspace `uploads/{file_id}/{safe_name}`; max 50MiB, sha256 recorded.
- `GET /api/projects/{id}/files/{file_id}` authenticated download attachment.
- `GET /api/projects/{id}/workspace?path=` -> `{path,entries:[{name,path,type:'file'|'directory',size}]}` read-only artifact browser. No traversal/symlinks/outside root or .env/auth/git metadata.
- `GET /api/projects/{id}/artifact?path=` -> authenticated download; safe PDF can preview via browser download URL `&inline=1`.
- `GET /api/projects/{id}/members` -> `{members:[Member]}`.
- `POST /api/projects/{id}/members {username,role:'editor'|'viewer'}` owner only.
- `DELETE /api/projects/{id}/members/{user_id}` owner only.
- `GET /api/users` -> `{users:[User]}` platform owner only.
- `POST /api/users {username,display_name?,password,role:'owner'|'member'}` -> User platform owner only.
- `POST /api/projects/{id}/presence` -> `{ok:true}` auth, refresh user heartbeat; SSE presence lists currently active members (real browser heartbeats).
- `GET /api/projects/{id}/activity?after=0` -> `{events:[Event]}` durable audit event list.
- `GET /api/projects/{id}/event-batch?after=cursor` -> `{events:[Event]}` up to 200 ordered durable events, including deltas. The browser uses this when a public proxy buffers SSE; both transports deduplicate by event ID and enforce the same project membership.
- `GET /health` non-sensitive liveness.

Project `{id,name,description,provider,model,native_session_id,status:'idle'|'running'|'waiting'|'error',created_at,updated_at,role:'owner'|'editor'|'viewer',active_turn:Turn|null,queued_count,permission_mode:'full-access'}`. Workspace path should not be displayed in main user flow.
Message `{id,project_id,role:'user'|'assistant'|'system',content,author:{id,username,display_name}|null,status:'queued'|'running'|'complete'|'failed'|'cancelled'|'interrupted',turn_id,attachments:[File],created_at,error?}`.
Turn `{id,project_id,user_message_id,assistant_message_id,status,author_id,created_at,started_at,finished_at,error}`.
File `{id,project_id,name,size,sha256,created_at,uploaded_by,download_url}`.
Member `{id,username,display_name,role}`; global owner implicitly owns all projects.

## Adapter interface (`shui/agents.py`)

```python
@dataclass
class RunRequest:
    provider: str
    workspace: Path
    prompt: str
    session_id: str | None = None
    model: str | None = None
    project_id: str = ''

@dataclass
class RunResult:
    session_id: str | None
    text: str
    status: str = 'complete'  # complete | cancelled

async def run_agent(request: RunRequest, emit, cancel: asyncio.Event) -> RunResult: ...
# emit is async (kind:str, payload:dict) -> None
# kinds: session {session_id}, delta {text}, tool {name,status,content}, status {text}
def provider_status() -> list[dict]: ...
```

Adapters MUST preserve returned native session id; error/timeout must never silently fork or fall back to an independent model. Full-access execution is an explicit owner deployment preference. Only one worker per project can send turns. Credentials are host CLI credentials, never sent to browsers or stored in SQLite. Child network calls inherit explicit SHUI_PROXY_URL through HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, NO_PROXY only loopback. Stdout deltas and tool events streamed; avoid full unbounded process output. Any native approval request must be surfaced as an error or denied, never silently hang. Cancellation terminates owned process group, never unrelated CLI processes. Native histories survive through session id.
