# Deployment

## Host

Use a macOS or Linux account with the intended experiment permissions. Install
the two provider CLIs, sign in as that account, and confirm the selected model
is available. A CLI binary being present is not a login or quota check.

Install Shui from the repository into a virtual environment. Keep
`SHUI_DATA_DIR` on persistent local SSD storage, outside the source checkout.
SQLite and the agent session files need a single host with working file locks.
For multiple GPU machines, agents can use the host account's SSH setup; Shui
does not allocate GPUs or distribute experiments automatically.

## Linux service

Adapt `deploy/shui.service` to the host username, checkout and SSD paths. The
service must run as the account with the CLI subscriptions and SSH access.
Initialize the owner using that same account and `SHUI_DATA_DIR` before starting.
The unit intentionally leaves host execution unrestricted, as selected by the
owner. It still keeps its files private with a restrictive umask.

Place environment settings in `/etc/shui.env`, readable only by the service
account and root. Set PATH to include the installed Codex and Claude binaries,
or use their absolute paths in `SHUI_CODEX_BIN` and `SHUI_CLAUDE_BIN`.

```ini
SHUI_DATA_DIR=/var/lib/shui
SHUI_PUBLIC_ORIGIN=https://shui.example.org
SHUI_PROXY_URL=http://127.0.0.1:7890
SHUI_CODEX_BIN=/home/research/.local/bin/codex
SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT=200000
SHUI_CLAUDE_BIN=/home/research/.local/bin/claude
```

`SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT` must be a positive integer. Shui passes it
to both new and resumed Codex threads; a wrapper's process-level configuration
alone does not reliably set a resumed thread's compaction threshold. The default
is `200000`, and invalid values fail before the native process is launched.

```sh
sudo install -m 0644 deploy/shui.service /etc/systemd/system/shui.service
sudo systemctl daemon-reload
sudo systemctl enable --now shui
sudo journalctl -u shui -f
```

## HTTPS and live output

For macOS, adapt `deploy/org.shui.app.plist.example` and place it at
`~/Library/LaunchAgents/org.shui.app.plist`. Create the private data directory
and bootstrap the owner first, then load the agent with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.shui.app.plist`.
It will remain running and start again when the user logs in. Keep the Mac awake
when collaborators need continuous access.

Put an HTTPS reverse proxy in front of `127.0.0.1:18940`. The configured public
origin must exactly match the URL collaborators open. HTTPS origins enable
secure cookies. Do not cache authenticated routes or buffer event streams.

Example Caddy configuration:

```caddyfile
shui.example.org {
    request_body {
        max_size 51MB
    }
    reverse_proxy 127.0.0.1:18940 {
        flush_interval -1
    }
}
```

For Nginx set `client_max_body_size 51m`, `proxy_buffering off`,
`proxy_read_timeout 3600s`, and forward the original Host header. Shui's SSE
endpoint emits a heartbeat every 15 seconds and can replay persisted events.
Use one Uvicorn worker. An additional worker will fail its exclusive data lock.

The browser loads research previews only when the panel and image are visible;
PDFs are opened explicitly. Hidden tabs release their event stream and catch up
from the saved cursor on return. This pauses browser traffic, not agent work.

HTTP failures (5xx) and requests taking at least one second produce structured
`shui.http` warning logs with method, route template, status, time to first byte,
and total response duration. Query strings, credentials, request bodies and
concrete file paths are omitted. Normal long-lived SSE connections are excluded
from the total-duration warning. Compare local and public download timings to
separate server processing from proxy or network delays.

## Updates and recovery

Stop the service before taking a consistent backup. Include the entire data
directory, `~/.codex` and `~/.claude` native histories, and keep the backup
private because it contains research, login sessions and credentials. Provider
authentication may need to be refreshed on a replacement host.

After an abrupt restart, running turns become interrupted and are never
automatically repeated. Inspect the experiment's files/processes before retrying.
Queued work behind a failed project stays paused until a new request or retry.
Changing a bound native session is deliberately rejected; create another
project when starting a separate conversation.

The service includes no public registration. Invite collaborators by creating
their account as Owner and assigning their project role. Password recovery is
available locally with `shui reset-password USERNAME`, which revokes old web
sessions and requires the account to choose a new password at next login.
The Owner web screen also resets another account to its username as its initial
password. Do not commit `.env`, databases, owner-login files, or native credentials.

Schema additions migrate automatically at startup. Back up SQLite before an
upgrade; existing Owner credentials and native project bindings are preserved.

## Resource records and encrypted credentials

Keep `SHUI_DATA_DIR/vault/master.key` with the database when backing up or moving
Shui. The key is created with mode 0600 in a private directory. Never commit it or
publish it with a deployment archive. Restoring encrypted data without its original
key fails closed; do not generate a replacement key over an existing vault.
Stop Shui for a consistent backup and restore the database and key together.

Owners configure project resource pools and may bind a pool to a project
credential. Approved execution resolves that credential internally for the specific
subprocess; browser APIs return metadata only. Rotation affects later resolution;
revocation does not cancel already-running external work or revoke a provider's
actual key. Revoke the key at its provider when required.

Approvals conserve the recorded GPU-hour or token capacity. Consumption receipts
are explicitly reported evidence, not a provider meter. Installed agents still run
with the service account's host permissions: an allocation is not a GPU scheduler,
a process sandbox, or a hard external billing limit. Use the provider's own quotas
and an execution account with the intended machine access.

Governance records and their hash chain persist independently of streamed agent
events. Startup migrations add their tables without changing native session
bindings. Verify ideas, approvals, credential inventory and a normal conversation
after an upgrade, in addition to the existing membership/file/research flows.
