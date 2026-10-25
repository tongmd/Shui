# Shui! API contract

JSON API under `/api`; all errors `{detail: string}`. Auth cookies are HttpOnly; mutating requests after login require `X-CSRF-Token` from `/api/session`. Server checks Origin. One project owns one active native Codex/Claude session. Turns from all members are serialized and persisted. Authentication is invite-only; there is no public registration.

- `GET /api/session` -> `{authenticated,user:{id,username,display_name,role:'owner'|'member'},csrf,providers:[{id,name,installed}],version}`. Unauthenticated user/csrf null.
- `POST /api/login {username,password}` same response; `POST /api/logout`.
- `GET /api/projects` -> `{projects:[Project]}`. Lists top-level projects; child collaboration sessions are listed through their parent task API.
- `POST /api/projects {name,description?,provider:'codex'|'claude',model?,native_session_id?,github_url?,overleaf_url?}` -> Project. Owner only. Workspace allocated server-side.
- `GET /api/projects/{id}` -> `{project:Project,messages:[Message],files:[File],members:[Member],events_cursor:number}`. All shared transcript paginated separately for older content.
- `GET /api/projects/{id}/meta` -> `{project:Project,members:[Member]}`. Same current membership checks, roles and task metadata as project detail, without messages, files or the research catalog; use for status polling.
- `PATCH /api/projects/{id} {name?,description?,model?,native_session_id?,github_url?,overleaf_url?}` -> Project. Owner only, session/model changes require idle project. Provider immutable. Explicit native binding must be unique, never create separate session per member.
- `DELETE /api/projects/{id} {confirm_name}` -> `{ok:true,deleted:id}`. Platform Owner only. The exact project name is required. Shui discussions, uploads, memberships, events and queued turns are deleted after any active task stops; external GitHub/Overleaf resources and native provider history are not deleted.
- `GET /api/projects/{id}/messages?before={created_at}&limit=100` -> `{messages:[Message]}`.
- `POST /api/projects/{id}/messages {content,attachment_ids:[],client_id:uuid}` -> `{message:Message,turn:Turn}`. Owner/editor. Idempotent client_id per project, returns existing request after network retry. User attribution comes from auth, not body.
- `POST /api/projects/{id}/turns/{turn_id}/cancel` -> `{ok:true}`. Owner/editor. Cancels own queued requests; project owner can cancel any turn. Current running turn can be interrupted by owner or original author.
- `POST /api/projects/{id}/turns/{turn_id}/retry {client_id:uuid}` -> `{message,turn}` only failed/interrupted turns; never automatic retry after restart.
- `GET /api/projects/{id}/events?after=cursor` SSE, cookie auth; each event has SSE id and JSON `{id,type,project_id,payload,created_at}`. Durable replay supports `Last-Event-ID`; periodic keepalive. Types include `message.created`, `message.updated`, `turn.updated`, `agent.delta`, `agent.tool`, `project.updated`, `file.created`, `artifact.changed`, `research.updated`, `member.updated`, `presence`. Payload contains affected `message`/`turn`/`project` objects; agent.delta `{message_id,delta}`, agent.tool `{turn_id,name,status,content}`.
- `GET /api/projects/{id}/files` -> `{files:[File]}`.
- `POST /api/projects/{id}/files` multipart field `file` -> File. Store bytes in project workspace `uploads/{file_id}/{safe_name}`; max 50MiB, sha256 recorded.
- `GET|HEAD /api/projects/{id}/files/{file_id}` authenticated download attachment; HEAD checks availability and headers without downloading the body.
- `GET /api/projects/{id}/workspace?path=` -> `{path,entries:[{name,path,type:'file'|'directory',size}]}` read-only artifact browser. No traversal/symlinks/outside root or .env/auth/git metadata.
- `GET|HEAD /api/projects/{id}/artifact?path=` -> authenticated download; safe PDF/image/SVG artifacts can preview via browser download URL `&inline=1`. Paths may be workspace-relative or absolute inside this project. Native `file:///` links are accepted only inside the same workspace; `sandbox:/mnt/data/` maps to the workspace root. Traversal, hidden files, symlinks and paths outside the project remain blocked. The message renderer converts native file links to this endpoint and checks HEAD before starting a browser download. Deliverables made outside the workspace must be copied into it before sharing.
- `GET /api/projects/{id}/members` -> `{members:[Member]}`.
- `POST /api/projects/{id}/members {username,role:'editor'|'viewer'}` owner only.
- `DELETE /api/projects/{id}/members/{user_id}` owner only.
- `GET /api/users` -> `{users:[User]}` platform owner only.
- `POST /api/users {username,display_name?,role:'owner'|'member'}` -> User, platform owner only. Initial password always equals username; a supplied legacy `password` field is not used. `must_change_password` starts true.
- `PATCH /api/users/{id} {display_name?,role?,is_active?}` -> User, platform owner only. Primary owner and caller's own owner access cannot be disabled or demoted. Disabled accounts lose existing logins.
- `POST /api/users/{id}/reset-password` -> User, platform owner only, excluding self and primary owner. Resets to username, marks mandatory change, revokes old logins.
- `GET /api/users/{id}/memberships` -> `{memberships:[{project_id,name,role,effective_role}]}`, platform owner only. Lists all projects; role is the direct assignment or null, effective_role includes global Owner access.
- `PUT /api/users/{id}/memberships {memberships:[{project_id,role:'editor'|'viewer'}]}` -> same list. Atomic replacement of direct non-owner assignments; existing project ownership is retained. Invalid or duplicate project entries are rejected.
- `POST /api/password {current_password,new_password,confirm_password}` -> new session payload and rotated cookie/CSRF. New password is 8–256 characters, differs from username ignoring case and from current password, and matches confirmation. Success clears mandatory change and revokes all previous sessions.
- `POST /api/projects/{id}/presence` -> `{ok:true}` auth, refresh user heartbeat; SSE presence lists currently active members (real browser heartbeats).
- `GET /api/projects/{id}/activity?after=0` -> `{events:[Event]}` durable audit event list.
- `GET /api/projects/{id}/research` -> `{insights:{summary,experiments,figures,claims,results}}`; the UI uses each experiment's `claim`, `validation`, and `result_summary`, and each figure's safe workspace `path` or `download_url` for inline preview.
- `POST /api/projects/{id}/research` -> owner/editor update for structured experiments, figures, claims, and results; emits `research.updated`.
- `GET /api/projects/{id}/event-batch?after=cursor` -> `{events:[Event]}` up to 200 ordered durable events, including deltas. The browser uses this when a public proxy buffers SSE; both transports deduplicate by event ID and enforce the same project membership.
- `GET /health` non-sensitive liveness.

Project `{id,name,description,provider,model,native_session_id,status:'idle'|'running'|'waiting'|'error',created_at,updated_at,role:'owner'|'editor'|'viewer',active_turn:Turn|null,queued_count,permission_mode:'full-access'}`. Workspace path should not be displayed in main user flow.
Message `{id,project_id,role:'user'|'assistant'|'system',content,author:{id,username,display_name}|null,status:'queued'|'running'|'complete'|'failed'|'cancelled'|'interrupted',turn_id,attachments:[File],created_at,error?}`.
Turn `{id,project_id,user_message_id,assistant_message_id,status,author_id,created_at,started_at,finished_at,error}`.
File `{id,project_id,name,size,sha256,created_at,uploaded_by,download_url}`.
Member `{id,username,display_name,role}`; global owner implicitly owns all projects.

User responses additionally include booleans `must_change_password` and
`is_active`. A pending password change permits only session inspection, logout,
and password change. Other authenticated endpoints return HTTP 403 with
`{detail:{code:'password_change_required',message:...}}`; the browser shows the
mandatory form before loading projects. Session provider lists are empty until
the password change is complete. Existing custom passwords survive migration;
legacy accounts still using their username as password become restricted.

Project responses include `github_url` and `overleaf_url` strings. Empty links
are allowed for existing/unconfigured projects. GitHub accepts a canonical HTTPS
repository URL and normalizes `.git`/trailing slashes. Overleaf accepts an HTTPS
`/project/<24-hex-ID>` URL. Links with credentials, query strings, fragments,
unexpected hosts, or extra path components are rejected. These are project
resource associations, not external membership provisioning.

The project detail response also includes an `insights` aggregate, and `GET /api/projects/{id}/research` returns the same payload. `POST /api/projects/{id}/research` lets owners and editors upsert experiments, figures, claims and results. Figure paths must refer to existing image, SVG or PDF files inside the project workspace. Each item can reference an experiment in that project, and writes emit `research.updated`. Result values are stored as text to preserve their original precision.

Research update events are compact invalidations (`{refresh:true}`), rather than
full research catalogs. SSE, event-batch and activity also project old stored
research snapshots to invalidations while preserving event IDs and order. The
original historical rows remain intact. Clients fetch `/research` when the
research panel is visible, coalescing multiple notifications into one request.

## Collaboration tasks

Project editors and owners can assign work to up to eight active project
editors/owners. Each parent/member pair has one durable child project,
workspace, FIFO queue, and native conversation. Repeated assignments to that
member append work to the current open task. After completion or cancellation,
a new task record reuses the same child session and native history; earlier
summaries and integration attempts remain associated with their original task.
A parent can have at most eight open tasks. Tasks cannot be nested; externally
bridged projects cannot delegate.

- `GET /api/projects/{parent}/delegations` -> `{delegations:[Delegation],assignable_members:[Member],sessions:[{parent_project_id,assignee_id,child_project_id,created_at,delegation}]}`. Sessions contain each member's current task; delegations also retains past tasks.
- `POST /api/projects/{parent}/delegations {content,title?,context?,assignee_ids:[id],attachment_ids:[],client_id}` -> `{delegation,message,turn,assignments:[{delegation,message,turn}]}` (201). Atomically appends one child turn and parent task-card message per assignee, creating the member's session only when needed. Top-level fields identify the first assignment for compatibility. Does not enqueue a parent turn. Retries with the same creator/client ID return the original assignment batch. Plain text mentions in the ordinary message API do not assign work.
- `GET /api/projects/{parent}/delegations/{id}` -> `{delegation}`.
- `POST /api/projects/{parent}/delegations/{id}/merge {summary?,client_id}` -> `{delegation,message,turn}`. Requires the child queue to be idle. Uses the supplied summary or the last completed assistant response belonging to that task, bounded to 12,000 characters, plus workspace/artifact references. Atomically closes the task and enqueues one parent integration turn; repeated submission returns that turn. Failure/retry follows the normal explicit turn API and updates that task's integration status even if a newer task has since opened in the child.
- `POST /api/projects/{parent}/delegations/{id}/cancel` -> `{delegation}`. Closes the task and cancels its queued/running child work, preserving the transcript.

`Delegation` includes `id,parent_project_id,child_project_id,parent_project_name,
title,instructions,context,status,created_by,creator,assignees,created_at,
updated_at,completed_at,summary,merge_turn_id,merge_turn,session_status,
active_turn,queued_count,can_manage`. Status is `open`, `completed`, or
`cancelled`; completed means submitted to the parent, not successful integration.
`merge_turn.status` describes integration progress. `can_manage` is caller
specific and is omitted from shared events.

Existing project detail, message, file, workspace, research, and event APIs
also work for the child ID. Child project details add `parent_project_id` and
`delegation`; parent card messages add `delegation` and `delegation_request`,
which identifies the individual assignment and preserves its title/content.
Integration messages add
`merge_delegation`, including in their first creation event, so the UI displays
and copies the result summary while the runner receives its execution context.
Valid local artifact links in the submitted summary are bound to their source
project; missing or unsafe local links become plain text. Child read access follows
current parent membership dynamically. Only the task creator, assignees, and
parent owner with current editing rights can write, complete, or cancel it.
Closed tasks remain read-only; a subsequent assignment opens a new task in the
same child conversation. Membership/settings are managed on the parent;
deleting the parent stops and deletes its child tasks too.

Parent `delegation.created` and `delegation.updated` events carry task metadata,
progress, and the submitted summary. Task updates also refresh the existing
child window when new work opens there. Child dialogue and native tool events stay
on the child event channel. Native histories are neither copied nor forked.

The sidebar and task cards open child sessions using
`/?project={parent_id}&session={child_id}`; parent windows use
`/?project={parent_id}`. URL state takes precedence over per-window session
storage. Separate browser tabs retain their independent session URLs.
These links contain no credentials and retain the normal login and membership
checks. Submitting a summary leaves the child session in read-only mode;
the parent receives the integration turn through its existing event stream.
Each parent/member pair appears once in the sidebar; later assignments reuse
that child session and its native history.
The composer resolves complete known project usernames in typed mentions,
including mentions adjacent to Chinese text, and submits them through the
delegation endpoint. It displays resolved recipients before sending. Email,
URL, code, and escaped `@` text remain ordinary text. While the member list is
unavailable, mention-bearing drafts cannot silently fall back to a normal turn.

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

Adapters MUST preserve returned native session id; error/timeout must never silently fork or fall back to an independent model. Full-access execution is an explicit owner deployment preference. Only one worker per project can send turns. CLI login credentials remain on the host. Separately saved project credential values are encrypted in SQLite and never returned by browser APIs. Child network calls inherit explicit SHUI_PROXY_URL through HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, NO_PROXY only loopback. Stdout deltas and tool events streamed; avoid full unbounded process output. Any native approval request must be surfaced as an error or denied, never silently hang. Cancellation terminates owned process group, never unrelated CLI processes. Native histories survive through session id.

## Ideas, resource approvals and credential inventory

All routes below inherit project membership, CSRF and Origin checks. Owners manage pools, credentials and the approval hierarchy. Registered project
editors can decide assigned requests using their funded upstream allocation;
only an owner can fund a top-level approval directly from a pool. Editors can propose ideas, submit
feedback and request resources. Mutations verify the existing governance history
before appending an event. Amounts are positive decimal values with at most six
decimal places. Capacity, reserved, consumed and remaining are returned as strings.

| Method and project-relative route | Request / result |
| --- | --- |
| `GET governance` | Ideas, revisions, feedback, resource_requests, pools, allocations, receipts, executions, approvers, events and ledger verification |
| `POST ideas` | `{title,body?,parent_id?}`; creates idea and revision 1 |
| `POST ideas/{id}/revisions` | `{title,body?}`; appends a revision |
| `POST ideas/{id}/feedback` | `{content,reply_to?}`; reply must reference feedback on this idea |
| `POST ideas/import` | `{ideas:[{id,title,body?,summary?,reviews?,resources?,events?,tags?}]}`; up to 200 CircuIdea records, deduplicated by source ID |
| `POST resource-approvers` | `{user_id,parent_id?}`; owner registers an active editor and immutable upstream approver; omitted parent leads to an owner |
| `POST resource-pools` | `{kind,unit,capacity,credential_id?}`; unique resource kind/unit in project |
| `POST resource-pools/{id}/capacity` | `{amount}`; adds recorded capacity |
| `POST resource-requests` | `{idea_id,kind,unit,amount,reason,approver_id?,parent_id?}`; binds current revision |
| `POST resource-requests/{id}/decision` | `{decision:'approve'|'reject',amount?,note?}`; partial approval uses a smaller amount |
| `POST resource-requests/{id}/escalate` | `{approver_id,reason,amount?}`; creates and links an upstream pending request |
| `POST resource-requests/{id}/cancel` | Releases unused allocation; active executions or child allocations must close first |
| `POST allocations/{id}/receipts` | `{amount,evidence,turn_id?,outcome?,client_id?}`; reported usage, idempotent by key/content |
| `POST ideas/{id}/assist` | `{kind:'check'|'draft'|'decision',content?,client_id?}`; queues a read-only native CLI turn |
| `POST ideas/{id}/execute` | `{allocation_id,content?,client_id?}`; queues execution bound to an active allocation and the approved revision |
| `GET credentials` | Metadata only: name/provider/category/purpose/endpoint/scopes/expiry/fingerprint/status/revision |
| `POST credentials` | `{secret,name?,provider?,category?,purpose?,endpoint?,scopes?,expires_at?}` |
| `POST credentials/{id}/rotate` | `{secret}`; rotates an active credential |
| `POST credentials/{id}/revoke` | Erases the saved value and marks it revoked |

Prefix every project-relative route with `/api/projects/{project_id}/`.
Approval authority is scoped to the project. Escalation follows the registered
upstream relationship; registration cannot silently reparent existing approvals.
A resource request's kind and unit must match its parent and pool. Top-level
approval reserves pool capacity; downstream approval transfers available parent
allocation. Usage reduces remaining/reserved and increases consumed. Cancellation
returns unused capacity upstream. No receipt implies no measured usage: the API
does not infer tokens or GPU hours from a completed turn.

Credentials captured from explicit token submissions are encrypted before storage
and replaced with references in shared text. Category/provider summaries are local
metadata, not remote key validation. A credential resolved for approved execution
is passed internally to the subprocess, not included in API responses or ledger
payloads. Governance hashes cover ordered events and current object state; verification
detects inconsistencies with that stored chain, not an independent external anchor.
