"""Native, persistent CLI adapters. No provider API credentials leave this module.

The application must serialize turns and durably handle ``session`` before its
emitter returns. A native resume error is terminal, never a request to fork.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit


@dataclass
class RunRequest:
    provider: str
    workspace: Path
    prompt: str
    session_id: str | None = None
    model: str | None = None
    project_id: str = ""


@dataclass
class RunResult:
    session_id: str | None
    text: str
    status: str = "complete"


class AgentError(RuntimeError):
    """A safe-to-display adapter failure; no automatic retry is implied."""


Emit = Callable[[str, dict], Awaitable[None]]
MAX_FRAME_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 1024 * 1024
MAX_TOOL_CHARS = 2 * 1024 * 1024
MAX_TOOL_EVENT_CHARS = 16 * 1024
MAX_EVENTS = 100_000
MAX_ITEMS = 4096
STOP_GRACE_SECONDS = 1.0
WAIT_STATUS_SECONDS = 60.0
_ACTIVE: set[tuple[str, str]] = set()
_IMAGE_DATA_URI = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,[A-Za-z0-9+/]+={0,2}", re.I
)


def _tool_display_text(value: str) -> str:
    # Inline image bytes are native model input, not useful browser tool text.
    return _IMAGE_DATA_URI.sub(
        lambda match: f"[inline {match[1]} data omitted]", value
    )


def _binary(provider: str) -> str | None:
    return shutil.which(os.environ.get(f"SHUI_{provider.upper()}_BIN", provider))


def provider_status() -> list[dict]:
    """Executable discovery only. This does not test login, quota, or service health."""
    return [
        {"id": p, "name": name, "installed": _binary(p) is not None}
        for p, name in (("codex", "Codex"), ("claude", "Claude Code"))
    ]


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    proxy = env.get("SHUI_PROXY_URL", "").strip()
    try:
        parsed = urlsplit(proxy)
        valid = (
            parsed.scheme in {"http", "https", "socks5", "socks5h"}
            and parsed.hostname
            and parsed.port
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
        )
    except ValueError:
        valid = False
    if not valid:
        raise AgentError(
            "SHUI_PROXY_URL must specify an explicit proxy URL with host and port."
        )
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env[key] = proxy
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = "localhost,127.0.0.1,::1"
    return env


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not 0 < value <= 86400:
            raise ValueError
        return value
    except ValueError as exc:
        raise AgentError(
            f"{name} must be a positive number no greater than 86400."
        ) from exc


def _positive_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        if not re.fullmatch(r"[0-9]+", raw):
            raise ValueError
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise AgentError(f"{name} must be a positive integer.") from exc


class _Redactor:
    """Do not forward known environment credentials or proxy passwords."""

    def __init__(self, env: dict[str, str]):
        values = {
            v
            for k, v in env.items()
            if len(v) >= 6
            and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION", k, re.I)
        }
        proxy = urlsplit(env["SHUI_PROXY_URL"])
        if proxy.password:
            values.add(proxy.password)
        if proxy.username or proxy.password:
            values.add(env["SHUI_PROXY_URL"])
        self.values = sorted(values, key=len, reverse=True)

    def clean(self, value: str) -> str:
        for secret in self.values:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]{8,}", r"\1[redacted]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted]", value)
        value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
        return value

    def split_safe(self, text: str) -> tuple[str, str]:
        # Hold suffixes that could continue a known secret or a bearer/basic
        # credential. This keeps a sensitive prefix and its token together when
        # the CLI splits them across two deltas.
        hold = 0
        for secret in self.values:
            for n in range(min(len(text), len(secret) - 1), hold, -1):
                if text.endswith(secret[:n]):
                    hold = n
                    break
        sensitive_prefix = re.search(
            r"(?is)(?:bearer\s+|basic\s+)[A-Za-z0-9._~+/=-]{0,256}$", text
        )
        if sensitive_prefix:
            hold = max(hold, len(text) - sensitive_prefix.start())
        return (text[:-hold], text[-hold:]) if hold else (text, "")


class _Output:
    def __init__(self, emit: Emit, redactor: _Redactor):
        self.emit = emit
        self.redactor = redactor
        self.parts: dict[str, str] = {}
        self.text = ""
        self.pending = ""
        self.raw_chars = 0
        self.tool_chars = 0
        self.tool_truncated = False
        self.tool_pending: dict[str, str] = {}

    async def _publish(self, text: str, final: bool = False) -> None:
        combined = self.redactor.clean(self.pending + text)
        safe, self.pending = (
            (combined, "") if final else self.redactor.split_safe(combined)
        )
        if safe:
            safe = self.redactor.clean(safe)
            self.text += safe
            await self.emit("delta", {"text": safe})

    async def append(self, key: str, delta: str) -> None:
        if not isinstance(delta, str) or not delta:
            return
        if len(self.parts) >= MAX_ITEMS and key not in self.parts:
            raise AgentError("Agent produced too many message items.")
        self.raw_chars += len(delta)
        if self.raw_chars > MAX_TEXT_CHARS:
            raise AgentError("Agent response exceeded the configured output limit.")
        if key not in self.parts and self.parts:
            await self._publish("\n\n", final=True)
        self.parts[key] = self.parts.get(key, "") + delta
        await self._publish(delta)

    async def final_item(self, key: str, text: str) -> None:
        previous = self.parts.get(key, "")
        if text.startswith(previous):
            await self.append(key, text[len(previous) :])
        elif text != previous:
            raise AgentError(
                "Native text stream and final message disagree; response was not duplicated."
            )

    async def finish(self) -> None:
        await self._publish("", final=True)

    async def tool(
        self, name: str, status: str, content: object = "", event_key: str | None = None
    ) -> None:
        if not isinstance(content, str):
            content = self._safe_json(content)
        else:
            content = _tool_display_text(content)
        pending_key = event_key or name
        content = self.redactor.clean(self.tool_pending.pop(pending_key, "") + content)
        if status == "running":
            content, pending = self.redactor.split_safe(content)
            if pending:
                self.tool_pending[pending_key] = pending
        remaining = MAX_TOOL_CHARS - self.tool_chars
        if remaining <= 0:
            if not self.tool_truncated:
                self.tool_truncated = True
                await self.emit(
                    "status",
                    {"text": "Tool output display limit reached; execution continues."},
                )
            return
        cap = min(remaining, MAX_TOOL_EVENT_CHARS)
        shown = content[:cap]
        self.tool_chars += len(shown)
        if len(content) > cap:
            shown += "\n[tool output truncated]"
        await self.emit(
            "tool",
            {
                "name": self.redactor.clean(str(name))[:200],
                "status": status,
                "content": shown,
            },
        )

    def _safe_json(self, value: object) -> str:
        if isinstance(value, str):
            return self.redactor.clean(_tool_display_text(value))
        if isinstance(value, dict):
            return json.dumps(
                {
                    str(k): self._safe_json(v)
                    if isinstance(v, (str, dict, list, tuple))
                    else self.redactor.clean(str(v))
                    for k, v in value.items()
                },
                ensure_ascii=False,
            )
        if isinstance(value, (list, tuple)):
            return json.dumps(
                [
                    self._safe_json(v)
                    if isinstance(v, (str, dict, list, tuple))
                    else self.redactor.clean(str(v))
                    for v in value
                ],
                ensure_ascii=False,
            )
        return self.redactor.clean(str(value))


class _Run:
    def __init__(self, request: RunRequest, emit: Emit, env: dict[str, str]):
        self.request, self.emit, self.env = request, emit, env
        self.redactor = _Redactor(env)
        self.output = _Output(emit, self.redactor)
        self.session_id = request.session_id
        self.turn_id: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_task: asyncio.Task | None = None
        self.locks: list[tuple[tuple[str, str], object]] = []
        self.events = 0
        self.next_id = 1
        self.generation_started = False
        self.completed_status: str | None = None
        self.completed_turn_id: str | None = None
        self.tool_names: dict[str, str] = {}
        self.streamed_message_blocks: set[tuple[str, str]] = set()
        self.idle_timeout = _seconds("SHUI_AGENT_IDLE_TIMEOUT_SECONDS", 1800)

    def lock(self, key: tuple[str, str]) -> None:
        if any(k == key for k, _ in self.locks):
            return
        if key in _ACTIVE:
            raise AgentError(
                "This workspace or native session already has an active turn."
            )
        root = Path(
            os.environ.get(
                "SHUI_LOCK_DIR", str(Path.home() / ".cache" / "shui" / "session-locks")
            )
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        handle = (root / digest).open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise AgentError(
                "This workspace or native session is in use by another Shui worker."
            ) from exc
        _ACTIVE.add(key)
        self.locks.append((key, handle))

    async def bind(self, session_id: object) -> None:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
            raise AgentError("Native CLI returned an invalid session identifier.")
        if self.session_id and session_id != self.session_id:
            raise AgentError(
                "Native CLI changed the requested session identifier; refusing a copied session."
            )
        self.lock((self.request.provider, session_id))
        self.session_id = session_id
        # This awaited callback is the durable binding boundary, before inference.
        await self.emit("session", {"session_id": session_id})

    async def spawn(self, args: list[str]) -> None:
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args,
                cwd=self.request.workspace,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_FRAME_BYTES,
            )
        )
        try:
            self.proc = await asyncio.shield(task)
        except asyncio.CancelledError:
            self.proc = await task
            raise
        except OSError as exc:
            raise AgentError(
                "Could not launch the installed agent executable."
            ) from exc

        async def drain() -> None:
            assert self.proc and self.proc.stderr
            # Do not put CLI debug logs, credentials, or full stderr into SSE/DB.
            while await self.proc.stderr.read(8192):
                pass

        self.stderr_task = asyncio.create_task(drain())

    async def send(self, data: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(data, ensure_ascii=False) + "\n").encode())
        try:
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AgentError("Native CLI closed its input unexpectedly.") from exc

    async def read(self) -> dict | None:
        assert self.proc and self.proc.stdout
        while True:
            loop = asyncio.get_running_loop()
            started = loop.time()
            deadline = started + self.idle_timeout
            pending = asyncio.create_task(self.proc.stdout.readline())
            try:
                async with asyncio.timeout_at(deadline):
                    while True:
                        done, _ = await asyncio.wait(
                            {pending}, timeout=WAIT_STATUS_SECONDS
                        )
                        if pending in done:
                            raw = await pending
                            break
                        await self.emit(
                            "status",
                            {
                                "text": "Waiting for native agent output "
                                f"({loop.time() - started:.0f}s without protocol events)."
                            },
                        )
            except TimeoutError as exc:
                elapsed = loop.time() - started
                raise AgentError(
                    f"Native agent idle timeout after {elapsed:.1f} seconds "
                    "without protocol events; the native session and "
                    "persisted history were retained."
                ) from exc
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise AgentError(
                    "Native agent emitted an oversized protocol frame."
                ) from exc
            finally:
                # A heartbeat must not restart the read or the real idle deadline.
                # Cancellation/timeout must not leave a competing stdout reader.
                pending.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pending
            if len(raw) > MAX_FRAME_BYTES:
                raise AgentError("Native agent emitted an oversized protocol frame.")
            if not raw:
                return None
            self.events += 1
            if self.events > MAX_EVENTS:
                raise AgentError("Native agent exceeded the protocol event limit.")
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError) as exc:
                raise AgentError(
                    "Native agent emitted an invalid JSON protocol frame."
                ) from exc
            return data

    async def close(self) -> None:
        try:
            if self.proc and self.proc.returncode is None:
                if (
                    self.request.provider == "codex"
                    and self.generation_started
                    and self.turn_id
                    and self.completed_status != "complete"
                    and self.proc.returncode is None
                ):
                    with suppress(Exception):
                        await asyncio.wait_for(
                            self.send(
                                {
                                    "id": self.next_id,
                                    "method": "turn/interrupt",
                                    "params": {
                                        "threadId": self.session_id,
                                        "turnId": self.turn_id,
                                    },
                                }
                            ),
                            0.25,
                        )
                # killpg targets only the fresh process group we created. Always send
                # SIGKILL after the grace period, even if its leader exits first.
                with suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), STOP_GRACE_SECONDS)
                except TimeoutError:
                    pass
                with suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 2)
            if self.proc and self.proc.stdout:
                # An oversized frame can leave stdout paused with buffered bytes.
                # Drain after stopping the child so its pipe transport reaches EOF.
                async def discard_stdout() -> None:
                    while await self.proc.stdout.read(8192):
                        pass

                with suppress(TimeoutError):
                    await asyncio.wait_for(discard_stdout(), 2)
        finally:
            if self.stderr_task:
                self.stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.stderr_task
            for key, handle in self.locks:
                _ACTIVE.discard(key)
                with suppress(OSError):
                    handle.close()
            self.locks.clear()

    def error_text(self, value: object) -> str:
        message = value.get("message", "") if isinstance(value, dict) else ""
        return self.redactor.clean(str(message))[:400]


async def _codex_event(run: _Run, data: dict) -> None:
    method = data.get("method", "")
    params = data.get("params") or {}
    if "id" in data and method:
        # Server requests occupy a separate ID space. Never mistake one for the
        # response to our outstanding request, and never hang on native approval.
        await run.send(
            {
                "id": data["id"],
                "error": {
                    "code": -32601,
                    "message": "Interactive native requests are not supported by Shui.",
                },
            }
        )
        await run.emit(
            "status", {"text": "Native agent requested interactive approval or input."}
        )
        raise AgentError(
            "Native agent requested approval or interactive input; the turn was stopped."
        )
    if not run.generation_started:
        return
    if params.get("threadId") not in (None, run.session_id):
        return
    event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
    if run.turn_id and event_turn and event_turn != run.turn_id:
        return
    if method == "turn/started":
        run.turn_id = params.get("turn", {}).get("id") or run.turn_id
    elif method == "item/agentMessage/delta":
        item_id = str(params.get("itemId", "message"))
        await run.output.append(item_id, params.get("delta", ""))
    elif method in {"item/started", "item/completed"}:
        item = params.get("item") or {}
        kind, key = item.get("type", ""), str(item.get("id", "message"))
        if kind == "agentMessage" and method == "item/completed":
            await run.output.final_item(key, item.get("text", ""))
        elif kind not in {"agentMessage", "userMessage", "reasoning", "plan"}:
            name = item.get("tool") or kind or "tool"
            content = {
                k: item[k]
                for k in (
                    "command",
                    "changes",
                    "arguments",
                    "aggregatedOutput",
                    "result",
                    "error",
                    "query",
                    "path",
                    "savedPath",
                )
                if k in item
            }
            result = content.get("result")
            if (
                (kind == "imageGeneration" or item.get("kind") == "image_gen.generation")
                and isinstance(result, str)
                and result.startswith(("iVBORw0KGgo", "/9j/", "R0lGOD", "UklGR"))
                and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", result)
            ):
                content["result"] = "[generated image data omitted]"
            await run.output.tool(
                name,
                "running"
                if method.endswith("started")
                else item.get("status", "completed"),
                content,
                event_key=str(item.get("id", name)),
            )
    elif method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
        await run.output.tool(
            method.split("/")[1],
            "running",
            params.get("delta", ""),
            event_key=str(params.get("itemId", method)),
        )
    elif method == "item/mcpToolCall/progress":
        await run.output.tool(
            "mcpToolCall",
            "running",
            params.get("message", ""),
            event_key=str(params.get("itemId", params.get("callId", method))),
        )
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        for item in turn.get("items", []):
            if item.get("type") == "agentMessage":
                await run.output.final_item(
                    str(item.get("id", "message")), item.get("text", "")
                )
        status = turn.get("status")
        if status == "failed":
            raise AgentError("Codex turn failed. " + run.error_text(turn.get("error")))
        if status not in {"completed", "interrupted"}:
            raise AgentError("Codex returned an unexpected terminal turn status.")
        run.completed_turn_id = str(turn.get("id", "")) or None
        run.completed_status = "complete" if status == "completed" else "cancelled"
    elif method == "error":
        if not params.get("willRetry"):
            raise AgentError(
                "Codex reported a terminal error. "
                + run.error_text(params.get("error"))
            )
        await run.emit(
            "status",
            {"text": "Codex is retrying a transient provider error in the same turn."},
        )


async def _rpc(run: _Run, method: str, params: dict) -> dict:
    request_id = run.next_id
    run.next_id += 1
    await run.send({"id": request_id, "method": method, "params": params})
    while True:
        data = await run.read()
        if data is None:
            raise AgentError(
                f"Codex exited while waiting for {method}; native session was not replaced."
            )
        if data.get("method"):
            await _codex_event(run, data)
        elif data.get("id") == request_id:
            if "error" in data:
                raise AgentError(
                    f"Codex {method} failed; native session was not replaced. "
                    + run.error_text(data["error"])
                )
            result = data.get("result")
            if not isinstance(result, dict):
                raise AgentError(f"Codex returned an invalid {method} response.")
            return result


async def _codex(run: _Run, binary: str) -> None:
    model = run.request.model or os.environ.get("SHUI_CODEX_MODEL") or "gpt-6.1-sol"
    auto_compact_token_limit = _positive_integer(
        "SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT", 200000
    )
    await run.spawn(
        [
            binary,
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="danger-full-access"',
        ]
    )
    async with asyncio.timeout(_seconds("SHUI_AGENT_HANDSHAKE_TIMEOUT_SECONDS", 60)):
        await _rpc(
            run,
            "initialize",
            {"clientInfo": {"name": "shui", "title": "Shui!", "version": "0.1.0"}},
        )
        await run.send({"method": "initialized", "params": {}})
        params: dict = {
            "cwd": str(run.request.workspace.resolve()),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            # Thread binding reloads config; process-level -c overrides alone
            # do not reliably reach the resumed thread's compaction policy.
            "config": {"model_auto_compact_token_limit": auto_compact_token_limit},
        }
        params["model"] = model
        if run.session_id:
            params.update(threadId=run.session_id, excludeTurns=True)
            result = await _rpc(run, "thread/resume", params)
        else:
            params["ephemeral"] = False
            result = await _rpc(run, "thread/start", params)
        thread = result.get("thread") or {}
        if (thread.get("status") or {}).get("type") == "active":
            raise AgentError("The native Codex thread already has an active turn.")
        if thread.get("ephemeral") is True:
            raise AgentError(
                "Codex returned an ephemeral thread; persistent history is required."
            )
        await run.bind(thread.get("id"))
    params = {
        "threadId": run.session_id,
        "input": [{"type": "text", "text": run.request.prompt}],
        "cwd": str(run.request.workspace.resolve()),
        "approvalPolicy": "never",
        "sandboxPolicy": {"type": "dangerFullAccess"},
    }
    params["model"] = model
    run.generation_started = True
    result = await _rpc(run, "turn/start", params)
    turn = result.get("turn") or {}
    if not turn.get("id") or (run.turn_id and run.turn_id != turn["id"]):
        raise AgentError("Codex returned an inconsistent turn identifier.")
    if run.completed_turn_id and run.completed_turn_id != turn["id"]:
        raise AgentError("Codex completion belongs to a different turn identifier.")
    run.turn_id = turn["id"]
    # Completion can race ahead of the turn/start response; retain it.
    while run.completed_status is None:
        data = await run.read()
        if data is None:
            raise AgentError(
                "Codex exited before the turn completed; native session was retained."
            )
        await _codex_event(run, data)


async def _claude(run: _Run, binary: str) -> None:
    existing = bool(run.session_id)
    session_id = run.session_id or str(uuid.uuid4())
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise AgentError(
            "Claude requires an explicit UUID session identifier."
        ) from exc
    # Persist the assigned UUID before a print process can make a model request.
    # CLI startup failure may leave a reserved UUID with no saved native history;
    # subsequent resume fails explicitly rather than creating a replacement.
    await run.bind(session_id)
    args = [
        binary,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--permission-prompts",
        "none",
        "--resume" if existing else "--session-id",
        session_id,
    ]
    if run.request.model:
        args.extend(["--model", run.request.model])
    await run.spawn(args)
    assert run.proc and run.proc.stdin
    run.proc.stdin.write(run.request.prompt.encode())
    await run.proc.stdin.drain()
    run.proc.stdin.close()
    run.generation_started = True
    message_id = ""
    current_block_index = None
    current_block_type = None
    block_tools: dict[int, tuple[str, str]] = {}
    streamed_tools: set[str] = set()
    initialized = False
    while True:
        data = await run.read()
        if data is None:
            raise AgentError(
                "Claude exited without a successful result; native session was not replaced."
            )
        seen_session = data.get("session_id")
        if seen_session and seen_session != session_id:
            raise AgentError(
                "Claude changed the requested session identifier; refusing a copied session."
            )
        kind = data.get("type")
        if kind == "system" and data.get("subtype") == "init":
            if seen_session != session_id:
                raise AgentError(
                    "Claude did not confirm the assigned native session identifier."
                )
            initialized = True
            await run.emit(
                "status", {"text": "Connected to the shared Claude session."}
            )
        elif kind in {"control_request", "permission_request"}:
            raise AgentError(
                "Claude requested interactive approval; the turn was stopped."
            )
        elif kind == "stream_event":
            # Child-agent text is not the parent's answer; tool events still show
            # that activity through ordinary parent tool_use/tool_result records.
            if data.get("parent_tool_use_id"):
                continue
            event = data.get("event") or {}
            subtype = event.get("type")
            if subtype == "message_start":
                message_id = str((event.get("message") or {}).get("id", ""))
                block_tools = {}
                current_block_index = None
                current_block_type = None
            elif subtype == "content_block_start":
                block = event.get("content_block") or {}
                index = event.get("index", 0)
                current_block_index, current_block_type = index, block.get("type")
                if block.get("type") == "tool_use":
                    tid, name = (
                        str(block.get("id", index)),
                        str(block.get("name", "tool")),
                    )
                    block_tools[index] = (tid, name)
                    run.tool_names[tid] = name
                    streamed_tools.add(tid)
                    if len(streamed_tools) > MAX_ITEMS:
                        raise AgentError("Claude produced too many tool items.")
                    await run.output.tool(
                        name, "running", block.get("input", {}), event_key=tid
                    )
                elif block.get("type") == "text" and block.get("text"):
                    await run.output.append(f"{message_id}:{index}", block["text"])
            elif subtype == "content_block_delta":
                delta, index = event.get("delta") or {}, event.get("index", 0)
                if delta.get("type") == "text_delta":
                    if not message_id:
                        raise AgentError(
                            "Claude text stream did not identify its message."
                        )
                    run.streamed_message_blocks.add((message_id, str(index)))
                    await run.output.append(
                        f"{message_id}:{index}", delta.get("text", "")
                    )
                elif delta.get("type") == "input_json_delta":
                    _, name = block_tools.get(index, ("", "tool"))
                    tid, _ = block_tools.get(index, (str(index), name))
                    await run.output.tool(
                        name, "running", delta.get("partial_json", ""), event_key=tid
                    )
        elif kind in {"assistant", "user"}:
            if data.get("parent_tool_use_id"):
                continue
            message = data.get("message") or {}
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if kind == "assistant" and btype == "text":
                    mid = str(message.get("id", message_id))
                    # Print mode can omit apiBlockIndex and emit only the block
                    # that just streamed, including after a thinking block.
                    native_index = data.get("apiBlockIndex")
                    if native_index is None:
                        native_index = (
                            current_block_index
                            if (
                                len(content) == 1
                                and mid == message_id
                                and current_block_type == btype
                                and current_block_index is not None
                            )
                            else index
                        )
                    key = f"{mid}:{native_index}"
                    block_key = (mid, str(native_index))
                    if key in run.output.parts:
                        await run.output.final_item(key, block.get("text", ""))
                    elif block_key not in run.streamed_message_blocks:
                        await run.output.final_item(key, block.get("text", ""))
                elif btype == "tool_use":
                    tid, name = str(block.get("id", "")), str(block.get("name", "tool"))
                    run.tool_names[tid] = name
                    if tid not in streamed_tools:
                        streamed_tools.add(tid)
                        if len(streamed_tools) > MAX_ITEMS:
                            raise AgentError("Claude produced too many tool items.")
                        await run.output.tool(
                            name, "running", block.get("input", {}), event_key=tid
                        )
                elif btype == "tool_result":
                    tool_id = str(block.get("tool_use_id", ""))
                    await run.output.tool(
                        run.tool_names.get(tool_id, "tool"),
                        "failed" if block.get("is_error") else "completed",
                        block.get("content", ""),
                        event_key=tool_id,
                    )
        elif kind == "result":
            if not initialized or seen_session != session_id:
                raise AgentError("Claude result lacks a confirmed native session.")
            if data.get("is_error") or data.get("subtype") != "success":
                subtype = re.sub(
                    r"[^a-zA-Z0-9_]", "", str(data.get("subtype", "error"))
                )[:80]
                raise AgentError(
                    f"Claude turn failed ({subtype}); native session was retained."
                )
            if data.get("permission_denials"):
                raise AgentError(
                    "Claude denied a required native permission; review the host policy."
                )
            if not run.output.parts and isinstance(data.get("result"), str):
                await run.output.append("result", data["result"])
            try:
                exit_code = await asyncio.wait_for(run.proc.wait(), 5)
            except TimeoutError as exc:
                raise AgentError("Claude did not exit after its final result.") from exc
            if exit_code != 0:
                raise AgentError("Claude exited with an error after its final result.")
            run.completed_status = "complete"
            return


async def run_agent(
    request: RunRequest, emit: Emit, cancel: asyncio.Event
) -> RunResult:
    if request.provider not in {"codex", "claude"}:
        raise AgentError("Unknown native agent provider.")
    if cancel.is_set():
        return RunResult(request.session_id, "", "cancelled")
    if (
        not isinstance(request.prompt, str)
        or len(request.prompt.encode()) > MAX_TEXT_CHARS
    ):
        raise AgentError("Agent input exceeded the prompt size limit.")
    if not Path(request.workspace).is_dir():
        raise AgentError("Project workspace does not exist.")
    binary = _binary(request.provider)
    if not binary:
        raise AgentError("The requested native agent CLI is not installed.")
    run = _Run(request, emit, _environment())
    task = watcher = None
    try:
        run.lock(("workspace", str(Path(request.workspace).resolve())))
        if request.session_id:
            run.lock((request.provider, request.session_id))
        task = asyncio.create_task(
            (_codex if request.provider == "codex" else _claude)(run, binary)
        )
        watcher = asyncio.create_task(cancel.wait())
        done, _ = await asyncio.wait(
            {task, watcher},
            timeout=_seconds("SHUI_AGENT_TIMEOUT_SECONDS", 3600),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            try:
                await task
            except TimeoutError as exc:
                raise AgentError(
                    "Native agent handshake timed out; session was not replaced."
                ) from exc
        else:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if watcher not in done:
                raise AgentError(
                    "Agent turn exceeded its time limit; native session was retained."
                )
            run.completed_status = "cancelled"
        await run.output.finish()
        return RunResult(
            run.session_id, run.output.text, run.completed_status or "cancelled"
        )
    finally:
        for pending in (task, watcher):
            if pending and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
        cleanup = asyncio.create_task(run.close())
        # Process-group teardown and lock release must finish even if the caller
        # itself is cancelled while awaiting the agent task.
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            raise
