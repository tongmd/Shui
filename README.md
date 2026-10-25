<div align="center">

<img src="web/icon.png" alt="Shui!" width="80" height="80">

# Shui!

A shared workspace for ideas, resources, and research with coding agents.

[Project page](https://stonezhang.com/shui/) · [Quick start](#quick-start) · [Deployment](docs/DEPLOYMENT.md) · [API](docs/API_CONTRACT.md)

</div>

Shui brings your team into the same Codex or Claude Code conversation. Keep one
project per paper, delegate focused tasks to teammates, and bring their results
back into a shared research workflow. Ideas, feedback, resource approvals, execution
evidence, and saved credentials now stay with the same project.

## Why Shui?

- **Shared context.** Every project keeps a persistent native agent session,
  an attributed conversation, files, and live output.
- **Focused collaboration.** Mention a project member with `@username` to
  delegate work. Each member's dedicated session retains its own context across
  tasks; completed summaries return to the main conversation for integration.
- **Predictable execution.** Tasks run in order within each session. Separate
  projects and delegated sessions can work in parallel.
- **Research in view.** Track experiments, claims, validation targets, results,
  and figures in the Research panel. Connect your repository and Overleaf project.
- **Your tools, your host.** Use installed Codex and Claude Code CLIs with the
  files, compute, and tools available to your host account.

## Ideas and resources

CircuIdea is the ideas module inside Shui, sharing its projects, members, and accounts.
Open [Ideas](https://shui-app.stonezhang.com/#ideas) or choose 想法与资源 in a project.
Organize parent and child ideas in the tree, move existing ideas, revise them, collect feedback,
and request GPU hours or model tokens. A request belongs to an exact idea revision;
approvers can approve all or part of it, reject it, or receive an escalated request.
Owners register editor approvers and their upstream authority. Upstream allocations fund downstream approvals without reserving the same capacity
twice. Cancelling releases the unused balance after active work and child
allocations have closed.

Ask the project's installed agent to check evidence, draft a proposal, or prepare
decision context. These assistance turns are read-only. An approved allocation can
start an execution turn; results remain in the existing conversation. Record actual
consumption with evidence and link its receipt to the turn.

Project credentials are encrypted at rest. Explicit token submissions are saved and
classified locally; the interface shows provider, purpose, endpoint, status and a
fingerprint, never the saved value. Owners can add, rotate or revoke credentials.
An allocation may refer to a credential for its approved execution subprocess.

Resource accounts track approvals and reported consumption. They do not provision
physical GPUs, buy provider credit, or independently measure provider billing.
The feedback ledger links events and state hashes; it is an application audit chain,
not a published blockchain or proof that submitted evidence is true.

The workspace is available at [shui-app.stonezhang.com](https://shui-app.stonezhang.com/). Execution results return to the idea for an explicit review; acceptance and requested changes stay with the executed version.

## Quick start

Requires **Python 3.11+**, **uv**, and at least one installed, signed-in **Codex**
or **Claude Code** CLI on macOS or Linux. Sign in to the CLI as the same operating
system user that runs Shui.

Agent processes also require an available HTTP or SOCKS proxy. The default is
`http://127.0.0.1:7890`; set `SHUI_PROXY_URL` before starting Shui if your proxy
uses a different address.

```sh
git clone https://github.com/tongmd/Shui.git
cd Shui
uv sync --frozen --extra dev
uv run shui init-owner --username owner --display-name Owner --generate-password
uv run shui serve
```

Open **<http://127.0.0.1:18940>**. The bootstrap command reports the location of a
private `owner-login.txt` containing your generated login. Create a project,
choose an agent, and send your first instruction.

Configure the model in project settings for your CLI account.

<details>
<summary>Install with pip instead</summary>

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/shui init-owner --username owner --display-name Owner --generate-password
.venv/bin/shui serve
```

</details>

## A research workflow

1. **Start a project.** Add your paper's repository and Overleaf links. Start a
   new agent conversation or bind an existing native session.
2. **Work together.** Invite project editors or viewers. Share instructions,
   upload files, paste screenshots, and follow the same live transcript.
3. **Delegate a question.** Type `@` to select a member. Their task opens in a
   separate workspace and persistent session, with optional focused background.
4. **Bring results back.** Submit a completed task's summary and artifact
   references to the parent session's queue.
5. **Follow the evidence.** The Research panel reads `research.json` and shows
   experiment records alongside figures produced in the workspace.

Native conversation bindings persist across restarts. Failed or interrupted
turns remain visible and pause their queue until a new instruction or explicit
retry; work is never silently repeated.

## Self-hosting

Shui runs one web worker per data directory. SQLite stores project state; native
agent histories remain on the host. Project membership controls web access.
Agents run with the host account's full permissions, so use a trusted team and
a dedicated account with the access you intend to grant.

| Setting | Purpose |
| --- | --- |
| `SHUI_DATA_DIR` | Persistent database and workspaces; defaults to `~/.local/share/shui` |
| `SHUI_PUBLIC_ORIGIN` | Exact browser origin, including scheme and port |
| `SHUI_PROXY_URL` | Proxy used by agent subprocesses |
| `SHUI_CODEX_BIN` / `SHUI_CLAUDE_BIN` | CLI executable names or paths |
| `SHUI_CODEX_MODEL` | Default Codex model; project settings take precedence |

See [`.env.example`](.env.example) for additional settings and
[Deployment](docs/DEPLOYMENT.md) for systemd, launchd, HTTPS, backups, and recovery.
Environment variables are read from the process environment; `.env` files are
not loaded automatically.

## Development

```sh
uv sync --frozen --extra dev
uv run python -m pytest -q
node --test tests/*.js tests/*.cjs
```

The regression suites run without real model calls. The frontend uses plain
JavaScript and CSS in `web/`; no frontend build step is required.

Bug reports and pull requests are welcome. Include reproduction steps for
issues and relevant tests for behavior changes. Protocol and endpoint details
are in [Agent protocols](docs/AGENT_PROTOCOLS.md) and
[API contract](docs/API_CONTRACT.md).

## Origin

在「大水理论」与「快科研」的倡议下，我快速实现了这个项目。
