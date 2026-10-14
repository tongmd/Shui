# Native agent adapters

`shui/agents.py` implements the interface in [API_CONTRACT.md](API_CONTRACT.md). Each project records one native conversation identifier. A fresh operating-system process may serve each turn, but subsequent turns resume that same conversation and its native persisted history. Browser members do not receive independent assistants.

The implementation targets the locally inspected Codex 0.153.4 app-server schema and Claude Code 2.1.261 print interface. Provider discovery reports only whether an executable is installed. It does not claim that credentials, quota, models, or service connectivity work.

Each parent/member pair owns one child project and native conversation. Its
first assignment starts a fresh native thread; later assignments and messages
resume that child thread, including after a previous task was completed.
Each turn records its exact task and assignment brief, so retrying a turn or
integrating an older result cannot pick up a newer task's context.
Parent and child use separate workspace and
session locks and can run concurrently. A focused task brief supplies the
assignment, optional background, selected attachment references, and parent
resource links. It never copies or forks the parent's transcript/native history.

Child agents receive instructions to read parent resources only as needed and
write new proposals under `tasks/{delegation_id}/` in their persistent workspace,
preserving earlier task deliverables. This is a task constraint under the
existing full-access execution model, not a new filesystem security boundary.
The parent receives task status events without child dialogue. Only an explicit
completion request sends a bounded result summary and artifact references to
the parent's existing queue/session; it does not automatically overwrite files.
Cancellation, closure, and revocation of the turn author's access stop child
execution. Restart marks interrupted child turns visibly, without replaying
them. Closed child transcripts remain readable by current parent members.

## Execution and host setup

The owner has selected full-access execution. Children run as the host account that runs Shui, with that account's existing CLI authentication and filesystem rights. Shui does not grant root, use sudo, or mint service credentials. Codex uses `approvalPolicy: "never"` and `danger-full-access`; Claude uses `bypassPermissions` and `--dangerously-skip-permissions`.

`SHUI_PROXY_URL` is mandatory and must have a scheme, host, and explicit port. Accepted schemes are `http`, `https`, `socks5`, and `socks5h`; the selected CLI must itself support the configured proxy scheme. Both uppercase and lowercase `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` are replaced with this value for child processes. `NO_PROXY` and `no_proxy` contain only `localhost,127.0.0.1,::1`. Existing host credentials remain in the child environment and are never included in provider status or protocol initialization sent to browsers.

This is proxy environment configuration, not an OS egress firewall. Full-access tools can launch software that ignores proxy variables or changes its environment. Deployments that require enforced egress routing must enforce it at the host or network layer.

| Setting | Default | Purpose |
|---|---|---|
| `SHUI_PROXY_URL` | Required | Explicit child-process proxy |
| `SHUI_CODEX_BIN` | `codex` on PATH | Codex executable path, not a shell command |
| `SHUI_CLAUDE_BIN` | `claude` on PATH | Claude executable path, not a shell command |
| `SHUI_CODEX_MODEL` | `gpt-5.6-sol` | Codex model when a project does not specify one |
| `SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT` | `200000` | Positive integer token threshold for native Codex compaction on new and resumed threads |
| `SHUI_AGENT_TIMEOUT_SECONDS` | `3600` | Maximum turn wall time |
| `SHUI_AGENT_IDLE_TIMEOUT_SECONDS` | `1800` | Maximum wait between native protocol records |
| `SHUI_AGENT_HANDSHAKE_TIMEOUT_SECONDS` | `60` | Codex initialization and thread binding |
| `SHUI_LOCK_DIR` | `~/.cache/shui/session-locks` | Shared advisory-lock directory for Shui workers |

A project's explicit model overrides the Codex fallback. Claude preserves its native configured model if the project leaves model unset. No fallback provider or fallback model is selected after an inference error.

During a silent native wait, Shui emits a browser status every 60 seconds with the elapsed wait. These local status messages do not reset the native idle deadline or the 3600-second total turn limit. The longer default idle allowance accommodates large multimodal history uploads and model prefill; an explicitly configured lower idle limit still applies. An idle timeout reports the actual wait and retains the native session identifier and persisted history.

## Codex app-server

Shui launches `codex app-server --listen stdio://` using a private stdin/stdout pipe, with an independent stderr drain. The JSON protocol is newline-delimited. The sequence is:

1. Send `initialize` with `clientInfo`, await its response, then send `initialized`.
2. Use `thread/start` for a new project, with `ephemeral: false`, or `thread/resume` with the previously stored `threadId`. Both requests explicitly pass `config.model_auto_compact_token_limit` from `SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT` (default `200000`). Thread binding reloads configuration, so a process-level `-c` override alone is insufficient. Invalid values fail before launching Codex; they do not silently fall back. Resume requests use `excludeTurns: true` so large historical transcripts are not returned into the adapter buffer. This does not discard model history.
3. Check the returned `thread.id` and reject a changed identifier, an ephemeral thread, or an active thread. Await `emit("session", {"session_id": thread.id})` before sending user input. The application must durably save this binding before its emitter returns.
4. Send `turn/start` with the same `threadId`, user input, workspace, model, `approvalPolicy: "never"`, and `sandboxPolicy: {"type": "dangerFullAccess"}`.
5. Read interleaved responses and notifications until a matching `turn/completed` reports `completed`, `interrupted`, or `failed`.

The adapter's `session_id` field stores the native **thread ID**, which is the value accepted by `thread/resume`. Codex's separate `thread.sessionId` can refer to a shared session-tree root and must not replace that thread ID.

The installed schema uses `sandbox: "danger-full-access"` on thread start/resume and `sandboxPolicy: {"type": "dangerFullAccess"}` on turn start. These are different wire formats. Generated schemas take precedence over stale examples on the documentation page.

`item/agentMessage/delta` streams answer text. Completed agent-message items provide a fallback or suffix without duplicating already streamed text. Command, file-change, MCP, and other tool lifecycle events become `tool` events. Reasoning content is not forwarded. Notifications for other threads or other turn IDs are ignored. A completion arriving before the `turn/start` response is retained. Retryable native errors surface as status messages; terminal errors fail the turn without starting another session.

Messages carrying both `method` and `id` are server requests, not responses to Shui's RPCs. Interactive approvals or user-input requests receive a protocol error and stop the turn. They never sit unanswered indefinitely.

## Claude Code print mode

Shui launches Claude with:

```text
--print --output-format stream-json --verbose --include-partial-messages
--permission-mode bypassPermissions --dangerously-skip-permissions
--permission-prompts none
```

For a new conversation Shui allocates a UUID, durably emits it, then launches with `--session-id UUID`. The prompt is sent over stdin, not placed in the process command line. Later turns use `--resume UUID`. Shui never uses `--continue`, `--fork-session`, `--bg`, or `--no-session-persistence`. In particular, Claude's background resume mode may copy a busy session and is unsuitable here.

Every native event that supplies a session identifier must match the assigned UUID. `system/init` confirms it before a successful final result is accepted. A startup failure may leave a reserved UUID before Claude has saved native history. A later resume then fails explicitly; recovery is an owner action, never a hidden new session.

Partial `stream_event` text deltas are keyed by message ID and content-block index. Final assistant frames use Claude's top-level `apiBlockIndex` when present. Print-mode snapshots can omit that index, so a single-block snapshot uses the matching current streamed block, including after a thinking block. This prevents duplicating output without dropping later text blocks. Tool-use starts, streamed arguments, and tool results use native tool IDs for their bounded buffers and become tool events. A successful `result` plus a zero exit status is required. Permission denials, mismatched identifiers, unexpected EOF, or non-success result types fail the turn. Raw CLI stderr and unstructured failure text are not copied into browser events.

## Cancellation, limits, and collision handling

Every child gets a new process group. Cancellation, timeout, parsing failure, or normal teardown terminates that owned group with SIGTERM and escalates to SIGKILL. The adapter never sends signals to unrelated CLI processes. For an identified unfinished Codex turn it also sends `turn/interrupt` before terminating its connection. Persisted native history and its identifier remain available after cancellation; the caller must not automatically retry a possibly executed prompt.

Shui uses in-process reservations and nonblocking OS advisory locks for workspace and provider/session identity. All Shui workers sharing an account must use the same lock directory. The database must additionally reject binding the same native conversation to two projects. These locks do not coordinate unrelated CLI clients that ignore Shui's locks; do not concurrently drive a bound native session from a separate terminal. A returned active Codex thread or changed native identifier fails explicitly.

Protocol frames are limited to 32 MiB to accommodate native image results, assistant text to 1,048,576 characters, and a turn to 100,000 events and 4,096 message/tool items. Tool display content is capped at 16 KiB per event and 2 MiB per turn; truncation is labeled. Inline image data URIs and recognized base64 image-generation results become display placeholders, preserving saved paths and ordinary tool results. This changes browser display only; native image history remains intact. CLI stderr is continuously drained without retaining an unbounded buffer. Known secret environment values and proxy passwords are redacted from text/tool output and protocol error details, including known secrets split across assistant deltas. This is not a general secret-classification system for arbitrary files an agent can read.

## Verification

Run the protocol suite with the project's development dependencies installed:

```sh
.venv/bin/python -m pytest tests/test_agents.py -q
```

The tests launch executable fake CLIs. They cover initialization order, same-ID resume, default and overridden compaction config on both thread methods, invalid compaction values rejected before launch, pre-inference binding, full-access parameters, proxy propagation, interleaved notifications, text deduplication, tool streaming, large image frames, timeout/error paths, output bounds, secret redaction, native session collision, and cancellation of an owned child process group. Reader tests additionally verify that wait statuses retain a single pending read, respect the real idle deadline even when status delivery stalls, and leave no pending read on cancellation. They never request model inference. A real authenticated two-turn smoke test is a separate deployment check.

Official Codex reference: <https://developers.openai.com/codex/app-server/>. Inspect the installed release with `codex app-server generate-json-schema --out <directory>` and Claude with `claude --help` before upgrading either CLI.

For a real deployment check, inspect the native thread's runtime compaction
limit after an inference turn on both a new thread and a same-ID resume.
`config/read` alone verifies process configuration and is not evidence that a
resumed thread received the override. Native sampling logs expose
`model_auto_compact_token_limit` and `auto_compact_scope_limit` for this check.
