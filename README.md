# Shui!

<img src="web/icon.png" alt="Shui!" width="64" height="64">

One paper, one project, one shared Codex or Claude Code conversation.

Team deployment: <http://192.0.2.10/shui/> redirects to the current HTTPS
service. See [public deployment details](docs/PUBLIC_DEPLOYMENT.md).

Shui gives a research team a shared transcript, file uploads, live agent output,
and an ordered task queue. Each message records its author. Every collaborator
continues the project's existing native agent conversation. Projects run in
parallel; turns within a project run in order.

To delegate, type **@** in the main conversation, choose project members, and
send the assignment. Shui opens the independent collaboration session in a
separate browser window, keeping the main conversation in its original window.
The assigned people and a dedicated agent can discuss and work there.
Every current project member can view that session from its task card or the
**Tasks** panel. The main agent keeps its own conversation; child dialogue is
not appended to its context. Add optional focused background when assigning.

When the work is ready, choose **完成并汇总** in the child session, review the
result summary, and submit it. The summary and artifact references enter the
main agent's queue for integration. The original main window receives the
result live, and the child window keeps the completed transcript open.
Each window has its own shareable session URL and restores the same session
on refresh. If the browser blocks a new window, use **在新窗口打开** on the
task card to open it explicitly.
Each task uses its own workspace and native session; later child messages
resume that same session. Child agents are instructed to treat parent files
as read-only references and place proposed changes in their own workspace.
As with other Shui agents, this is not an operating-system sandbox.

Paste text or screenshots into the message box with **Ctrl+V** (**Command+V**
on macOS). Pasted images upload as attachments; accompanying text stays in the
message box, and nothing is sent until you press Send. Use a message's **Copy**
button to copy its full text. Image previews also offer **Copy image** and
**Ctrl+C / Command+C** when no text is selected. Normal text selection and
copy/paste shortcuts continue to work.

Each project also has a **Research** panel. It lists experiments, the claim and
validation target for each one, current results, and paper figures. PNG, JPEG,
WebP, GIF, SVG, and PDF figures produced in the project workspace appear as
soon as the files are stable and open in an inline preview. The agent maintains
`research.json` in the workspace root to connect figures to experiments and
record their claims, validation methods, and findings. Existing entries are
updated by stable key; numeric result values remain exact strings.

## Start locally

Requires Python 3.11+ on macOS or Linux, and at least one installed, signed-in
`codex` or `claude` CLI. Use the same host account for CLI login and Shui.

```sh
git clone git@github.com:tongmd/Shui.git
cd Shui
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/shui init-owner --username owner --display-name Owner --generate-password
.venv/bin/shui serve
```

The committed `uv.lock` records the tested dependency resolution. With `uv`
installed, `uv sync --frozen --extra dev` can replace the virtual-environment
creation and pip installation steps above.

Open <http://127.0.0.1:18940>. The bootstrap command prints the location of a
private `owner-login.txt` file, readable only by the host account. It never
prints the generated password. Alternatively, omit `--generate-password` to
choose a password interactively. `SHUI_OWNER_PASSWORD` can supply a password
for unattended bootstrap and is removed from the CLI environment after reading.

The owner has access to every project and can create accounts, assign project
members, configure models, and cancel any project turn. Members can be editors
or viewers. An editor can submit tasks and uploads; a viewer can read and download.

New team accounts start with their username as the initial password. Their
first sign-in opens a mandatory password-change page. Project data and actions
remain unavailable until they set a new password. Password changes rotate the
current login and revoke every earlier login for that account.

The **Team accounts** screen manages each person's status, role, and project
assignments in one place. An owner can reset another member's password to their
username, requiring another password change. Disabling an account signs it out;
removing task permissions cancels its pending work and requests cancellation
of its current task. The primary owner cannot be disabled or demoted there.

Each project stores one GitHub repository link and one Overleaf project link,
editable in project settings and available directly in the project header.
Those resources are also included in the shared agent's task context. Existing
projects can add their links when ready. Team account roles manage access
inside Shui; external GitHub and Overleaf membership follows those services'
own permissions.

## Shared sessions and execution

- Create one project per paper, then add the collaborators to that project.
- Choose Codex or Claude Code. An optional existing native session ID binds the
  project to that conversation. Otherwise the first message creates the native
  conversation and saves its ID before submitting the task.
- All project agents run with full host-account permissions. Codex uses
  `danger-full-access` and no approvals. Claude uses `bypassPermissions`.
  Agents can edit files and run experiments using the host's available tools,
  credentials and SSH configuration.
- File uploads only store files. A later, explicit task can ask the agent to
  read or use an attachment. Uploading does not execute it.
- Failed or interrupted turns pause the remaining queue. A new request or
  explicit retry resumes processing. Retry is never automatic because an
  interrupted experiment may already have made changes.
- A session resume failure stays visible. Shui never silently creates a
  replacement conversation or switches providers. A bound session cannot be
  reassigned to another project.

Full access means the permissions of the operating-system account running Shui.
It does not add root privileges. Project membership limits the web interface;
it is not filesystem isolation between trusted collaborators' agent tasks.
Native CLI histories stay on the host. When binding an existing conversation,
the agent retains that context; Shui's transcript starts with messages sent
through Shui. Do not simultaneously send turns to a bound session from another
CLI or application.

## Configuration

Environment variables are read from the process environment. `.env.example`
documents the settings; Shui does not automatically load `.env` files.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHUI_DATA_DIR` | `~/.local/share/shui` | SQLite database and project workspaces |
| `SHUI_HOST` | `127.0.0.1` | Web listen address |
| `SHUI_PORT` | `18940` | Web port |
| `SHUI_PUBLIC_ORIGIN` | `http://127.0.0.1:18940` | Exact browser origin, including scheme and port |
| `SHUI_PROXY_URL` | `http://127.0.0.1:7890` with `shui serve` | Required proxy for agent child processes |
| `SHUI_CODEX_MODEL` | `gpt-5.6-sol` | Default Codex model; project settings take precedence |
| `SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT` | `200000` | Positive integer native compaction threshold, passed explicitly on thread start/resume |
| `SHUI_CODEX_BIN` | `codex` | Executable name or absolute path |
| `SHUI_CLAUDE_BIN` | `claude` | Executable name or absolute path |
| `SHUI_AGENT_TIMEOUT_SECONDS` | `3600` | Maximum time for one agent turn, up to 86400 |
| `SHUI_AGENT_IDLE_TIMEOUT_SECONDS` | `1800` | Maximum silence between CLI protocol messages |
| `SHUI_PROJECT_UPLOAD_QUOTA` | `5368709120` | Total uploaded bytes per project |

Agent subprocesses inherit the proxy through upper- and lower-case
`HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY`. Only loopback is in `NO_PROXY`.
The service fails an agent launch if the proxy setting is missing or invalid.
Environment variables configure routing; software that ignores them needs a
host-level network rule when enforced proxy routing is required.

Each upload is limited to 50 MiB. Project artifacts and PDFs are available from
the files panel. Authentication uses password hashes and HttpOnly cookies;
mutating requests require a CSRF token. Use HTTPS when exposing Shui remotely.
Native CLI credentials remain on the host. Keep its data directory and CLI
configuration out of Git.

## Administration

```sh
# Run with another data directory and port.
SHUI_PUBLIC_ORIGIN=http://127.0.0.1:18941 \
  .venv/bin/shui --data-dir /path/on/ssd/shui serve --port 18941

# Password recovery also signs that account out of every browser.
.venv/bin/shui reset-password owner

# Run the regression tests without real model calls.
.venv/bin/python -m pytest -q
```

Run one web worker per data directory. Different projects can still execute
concurrently. Persistent process and session locks reject competing Shui
workers. Back up the database, workspaces, and native CLI histories together
while the service is stopped.

See [deployment](docs/DEPLOYMENT.md), [API contract](docs/API_CONTRACT.md), and
[agent protocol details](docs/AGENT_PROTOCOLS.md).

The interface uses the bundled colorful brain icon.
