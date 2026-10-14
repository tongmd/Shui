"""Protocol tests use fake executables only. No inference or network requests."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from shui import agents
from shui.agents import AgentError, RunRequest, run_agent

FAKE_CLI = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

mode = os.environ.get("SHUI_FAKE_MODE", "ok")
capture = Path(os.environ["SHUI_FAKE_CAPTURE"])

def log(data):
    with capture.open("a") as stream:
        stream.write(json.dumps(data) + "\n")

def out(data):
    print(json.dumps(data), flush=True)

log({"argv": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd(),
     "proxy": {k: os.environ.get(k) for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"]}})

def hang():
    if mode == "child":
        child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
        log({"child": child.pid})
    while True:
        time.sleep(0.1)

if "app-server" in sys.argv:
    initialized = False
    sid = "codex-native-1"
    for line in sys.stdin:
        msg = json.loads(line)
        log(msg)
        method = msg.get("method")
        if method == "initialize":
            if mode == "malformed":
                print("not-json", flush=True); hang()
            if mode == "oversized":
                print("x" * 3000000, flush=True); hang()
            if mode == "handshake_timeout":
                hang()
            out({"id":msg["id"], "result":{"userAgent":"fake"}})
        elif method == "initialized":
            initialized = True
        elif method in ["thread/start", "thread/resume"]:
            assert initialized
            if method == "thread/resume":
                sid = msg["params"]["threadId"]
            if mode == "resume_error":
                out({"id":msg["id"],"error":{"code":-1,"message":"Cannot resume " + os.environ.get("FAKE_API_KEY", "")}})
                continue
            if mode == "approval":
                out({"id":msg["id"], "method":"item/commandExecution/requestApproval", "params":{}})
                continue
            out({"method":"thread/started", "params":{"thread":{"id":sid}}})
            out({"id":msg["id"],"result":{"thread":{"id":"unexpected-copy" if mode == "mismatch" else sid,
                 "sessionId":"different-tree-root", "ephemeral":False,
                 "status":{"type":"active" if mode == "active" else "idle"}}}})
        elif method == "turn/start":
            p = {"threadId":sid,"turnId":"turn-1"}
            out({"method":"turn/started", "params":{"threadId":sid,"turn":{"id":"turn-1","status":"inProgress"}}})
            if mode != "early_complete":
                out({"id":msg["id"],"result":{"turn":{"id":"turn-1","items":[],"status":"inProgress"}}})
            if mode in ["hang", "child"]:
                out({"method":"item/agentMessage/delta","params":dict(p,itemId="m1",delta="started")})
                hang()
            if mode == "eof":
                sys.exit(0)
            if mode == "retry":
                out({"method":"error", "params":dict(p,willRetry=True,error={"message":"transient"})})
            if mode == "terminal_error":
                out({"method":"error", "params":dict(p,willRetry=False,error={"message":"failed"})})
                hang()
            if mode in ["large_image", "large_image_extension"]:
                image = {"id":"image1", "type":"imageGeneration", "status":"completed",
                         "result":"iVBORw0KGgo" + "A" * (3 * 1024 * 1024),
                         "savedPath":"/workspace/generated-image.png"}
                if mode == "large_image_extension":
                    image.update(type="Extension", kind="image_gen.generation")
                out({"method":"item/completed", "params":dict(p,item=image)})
            out({"method":"item/agentMessage/delta","params":dict(p,threadId="other-thread",itemId="alien",delta="ignore me")})
            out({"method":"item/started","params":dict(p,item={"id":"cmd1","type":"commandExecution","command":"echo test"})})
            out({"method":"item/commandExecution/outputDelta","params":dict(p,itemId="cmd1",delta="tool output")})
            chunks = ["Hello", " world"]
            if mode == "secret":
                value = os.environ["FAKE_API_KEY"]
                chunks = ["Value " + value[:8], value[8:] + " done"]
            if mode == "bearer":
                chunks = ["Bearer ", "abcdefghijklmno"]
            if mode == "text_limit":
                chunks = ["a"*300]
            for chunk in chunks:
                out({"method":"item/agentMessage/delta","params":dict(p,itemId="m1",delta=chunk)})
            item = {"id":"m1","type":"agentMessage","text":"".join(chunks)}
            out({"method":"item/completed","params":dict(p,item=item)})
            if mode == "failed":
                turn = {"id":"turn-1","items":[item],"status":"failed","error":{"message":"simulated failure"}}
            else:
                turn = {"id":"turn-1","items":[item],"status":"completed"}
            out({"method":"turn/completed","params":{"threadId":sid,"turn":turn}})
            if mode == "early_complete":
                out({"id":msg["id"],"result":{"turn":{"id":"turn-1","items":[],"status":"inProgress"}}})
        elif method == "turn/interrupt":
            out({"id":msg["id"],"result":{}})
else:
    args = sys.argv[1:]
    flag = "--resume" if "--resume" in args else "--session-id"
    sid = args[args.index(flag)+1]
    prompt = sys.stdin.read()
    log({"prompt":prompt})
    if mode == "resume_error":
        print("credential: " + os.environ.get("FAKE_API_KEY", ""), file=sys.stderr)
        sys.exit(1)
    if mode == "mismatch":
        sid = "00000000-0000-0000-0000-000000000000"
    out({"type":"system","subtype":"init","session_id":sid})
    if mode in ["hang", "child"]:
        hang()
    if mode == "approval":
        out({"type":"control_request","request":{"subtype":"can_use_tool"}});hang()
    if mode == "eof":
        sys.exit(0)
    def stream(event):
        out({"type":"stream_event","session_id":sid,"event":event})
    if mode == "thinking_snapshot":
        stream({"type":"message_start","message":{"id":"msg-thinking"}})
        stream({"type":"content_block_start","index":0,"content_block":{"type":"thinking"}})
        out({"type":"assistant","session_id":sid,"message":{"id":"msg-thinking","content":[{"type":"thinking","thinking":"private"}]}})
        stream({"type":"content_block_stop","index":0})
        for index, text in [(1,"First answer"),(2,"Second answer")]:
            stream({"type":"content_block_start","index":index,"content_block":{"type":"text","text":""}})
            stream({"type":"content_block_delta","index":index,"delta":{"type":"text_delta","text":text}})
            out({"type":"assistant","session_id":sid,"message":{"id":"msg-thinking","content":[{"type":"text","text":text}]}})
            stream({"type":"content_block_stop","index":index})
        out({"type":"result","subtype":"success","session_id":sid,"result":"Second answer"})
        sys.exit(0)
    stream({"type":"message_start","message":{"id":"msg-1"}})
    stream({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}})
    stream({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}})
    stream({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}})
    stream({"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool-1","name":"Bash","input":{}}})
    stream({"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"command\":\"pwd\"}"}})
    out({"type":"assistant","session_id":sid,"message":{"id":"msg-1","content":[{"type":"text","text":"Hello world"}, {"type":"tool_use","id":"tool-1","name":"Bash","input":{"command":"pwd"}}]}})
    out({"type":"user","session_id":sid,"message":{"content":[{"type":"tool_result","tool_use_id":"tool-1","content":"tool result"}]}})
    out({"type":"result","subtype":"error_max_turns" if mode == "failed" else "success", "is_error":mode == "failed",
         "session_id":sid,"result":"Hello world", "permission_denials":[{"tool_name":"Bash"}] if mode == "denied" else []})
    sys.exit(1 if mode == "bad_exit" else 0)
"""


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    executable = tmp_path / "fake_agent"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    executable.chmod(0o755)
    capture = tmp_path / "protocol.jsonl"
    monkeypatch.setenv("SHUI_CODEX_BIN", str(executable))
    monkeypatch.setenv("SHUI_CLAUDE_BIN", str(executable))
    monkeypatch.setenv("SHUI_PROXY_URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("SHUI_FAKE_CAPTURE", str(capture))
    monkeypatch.setenv("SHUI_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("SHUI_AGENT_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("SHUI_AGENT_IDLE_TIMEOUT_SECONDS", "3")
    monkeypatch.delenv("SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT", raising=False)
    monkeypatch.setattr(agents, "STOP_GRACE_SECONDS", 0.1)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace, capture


def records(capture):
    return [json.loads(line) for line in capture.read_text().splitlines()]


async def collect_run(
    workspace, provider="codex", session_id=None, emit=None, cancel=None
):
    events = []

    async def collect(kind, payload):
        events.append((kind, payload))
        if emit:
            await emit(kind, payload)

    result = await run_agent(
        RunRequest(
            provider, workspace, "hello from shared project", session_id, "chosen-model"
        ),
        collect,
        cancel or asyncio.Event(),
    )
    return result, events


@pytest.mark.parametrize("compact_limit", [None, "120000", "250000"])
async def test_codex_handshake_stream_and_resume(fake_cli, monkeypatch, compact_limit):
    workspace, capture = fake_cli
    if compact_limit is not None:
        monkeypatch.setenv("SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT", compact_limit)
    expected_config = {"model_auto_compact_token_limit": int(compact_limit or 200000)}

    async def before_generation(kind, payload):
        if kind == "session":
            assert not any(r.get("method") == "turn/start" for r in records(capture))

    result, events = await collect_run(workspace, emit=before_generation)
    assert result.session_id == "codex-native-1"  # Not sessionId of its tree.
    assert result.text == "Hello world"
    assert "".join(p["text"] for k, p in events if k == "delta") == result.text
    assert any(k == "tool" and p["content"] == "tool output" for k, p in events)
    rows = records(capture)
    calls = [r for r in rows if "method" in r]
    assert [r["method"] for r in calls[:4]] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    start, turn = calls[2]["params"], calls[3]["params"]
    assert start["sandbox"] == "danger-full-access"
    assert start["approvalPolicy"] == turn["approvalPolicy"] == "never"
    assert turn["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert start["ephemeral"] is False
    assert start["model"] == turn["model"] == "chosen-model"
    assert start["config"] == expected_config
    resumed, _ = await collect_run(workspace, session_id=result.session_id)
    assert resumed.session_id == result.session_id
    calls = [r for r in records(capture) if "method" in r]
    assert [r["method"] for r in calls].count("thread/start") == 1
    resume = next(r["params"] for r in calls if r["method"] == "thread/resume")
    assert resume["threadId"] == result.session_id and resume["excludeTurns"] is True
    assert resume["config"] == expected_config
    assert not any("fork" in r["method"] for r in calls)


@pytest.mark.parametrize("project_model,environment_model,expected", [
    (None, None, "gpt-6.1-sol"),
    (None, "environment-model", "environment-model"),
    ("project-model", "environment-model", "project-model"),
])
async def test_codex_model_default_and_overrides_reach_new_and_resumed_turns(
    fake_cli, monkeypatch, project_model, environment_model, expected
):
    workspace, capture = fake_cli
    if environment_model is None:
        monkeypatch.delenv("SHUI_CODEX_MODEL", raising=False)
    else:
        monkeypatch.setenv("SHUI_CODEX_MODEL", environment_model)

    async def emit(kind, payload):
        pass

    session_id = None
    for _ in range(2):
        result = await run_agent(
            RunRequest("codex", workspace, "Use the configured model", session_id, project_model),
            emit,
            asyncio.Event(),
        )
        assert result.session_id == "codex-native-1"
        session_id = result.session_id
    calls = [row for row in records(capture) if row.get("method") in {
        "thread/start", "thread/resume", "turn/start",
    }]
    assert [call["method"] for call in calls] == [
        "thread/start", "turn/start", "thread/resume", "turn/start",
    ]
    assert all(call["params"]["model"] == expected for call in calls)
    assert calls[2]["params"]["threadId"] == "codex-native-1"
    assert all(call["params"]["threadId"] == "codex-native-1" for call in calls if call["method"] == "turn/start")


@pytest.mark.parametrize("session_id", [None, "existing-thread"])
@pytest.mark.parametrize("compact_limit", ["", "0", "-1", "1.5", "nan", "1e5", "invalid"])
async def test_codex_invalid_compaction_limit_fails_before_launch(
    fake_cli, monkeypatch, session_id, compact_limit
):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT", compact_limit)
    with pytest.raises(
        AgentError, match="SHUI_CODEX_AUTO_COMPACT_TOKEN_LIMIT must be a positive integer"
    ):
        await collect_run(workspace, session_id=session_id)
    assert not capture.exists()
    assert not agents._ACTIVE


async def test_claude_stream_same_uuid_resume_and_prelaunch_bind(fake_cli):
    workspace, capture = fake_cli

    async def before_generation(kind, payload):
        if kind == "session":
            assert not capture.exists()

    first, events = await collect_run(workspace, "claude", emit=before_generation)
    assert first.text == "Hello world"
    assert "".join(p["text"] for k, p in events if k == "delta") == first.text
    assert any(
        k == "tool" and p["name"] == "Bash" and p["status"] == "completed"
        for k, p in events
    )
    second, _ = await collect_run(workspace, "claude", first.session_id)
    assert first.session_id == second.session_id
    commands = [r["argv"] for r in records(capture) if "argv" in r]
    assert "--session-id" in commands[0] and "--resume" in commands[1]
    assert (
        "--fork-session" not in commands[1]
        and "--continue" not in commands[1]
        and "--bg" not in commands[1]
    )
    assert commands[1][commands[1].index("--resume") + 1] == first.session_id
    for args in commands:
        for flag in [
            "--print",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]:
            assert flag in args
        assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
        assert args[args.index("--permission-prompts") + 1] == "none"


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_explicit_proxy_in_every_case(fake_cli, provider):
    workspace, capture = fake_cli
    await collect_run(workspace, provider)
    env = next(r["proxy"] for r in records(capture) if "proxy" in r)
    for k in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        assert env[k] == "http://127.0.0.1:7890"
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        "",
        "localhost:7890",
        "http://host",
        "http://host:bad",
        "http://host:123/?key=secret",
    ],
)
async def test_missing_or_invalid_proxy_never_launches(fake_cli, monkeypatch, proxy):
    workspace, capture = fake_cli
    if proxy is None:
        monkeypatch.delenv("SHUI_PROXY_URL")
    else:
        monkeypatch.setenv("SHUI_PROXY_URL", proxy)
    with pytest.raises(AgentError, match="SHUI_PROXY_URL"):
        await collect_run(workspace)
    assert not capture.exists()


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_resume_failure_does_not_fork_or_leak_secret(
    fake_cli, monkeypatch, provider
):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "resume_error")
    monkeypatch.setenv("FAKE_API_KEY", "secret-api-credential-value")
    session = "5c0802b1-44c7-46a4-ac07-621d1c7b41c6"
    with pytest.raises(AgentError) as exc:
        await collect_run(workspace, provider, session)
    assert "secret-api-credential-value" not in str(exc.value)
    rows = records(capture)
    assert len([r for r in rows if "argv" in r]) == 1
    assert not any(r.get("method") == "thread/start" for r in rows)
    assert not any(r.get("method") == "turn/start" for r in rows)


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["mismatch", "approval", "failed", "eof"])
async def test_native_errors_are_explicit(fake_cli, monkeypatch, provider, mode):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    session = "5c0802b1-44c7-46a4-ac07-621d1c7b41c6" if mode == "mismatch" else None
    with pytest.raises(AgentError):
        await collect_run(workspace, provider, session)


@pytest.mark.parametrize("mode", ["retry", "early_complete"])
async def test_codex_interleaved_notifications(fake_cli, monkeypatch, mode):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    result, _ = await collect_run(workspace)
    assert result.status == "complete" and result.text == "Hello world"


@pytest.mark.parametrize("mode", ["active", "terminal_error", "malformed", "oversized"])
async def test_codex_invalid_and_active_protocol(fake_cli, monkeypatch, mode):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    if mode == "oversized":
        monkeypatch.setattr(agents, "MAX_FRAME_BYTES", 256 * 1024)
        original_close = agents._Run.close

        async def close_and_check_pipe(run):
            await original_close(run)
            assert run.proc.stdout.at_eof()

        monkeypatch.setattr(agents._Run, "close", close_and_check_pipe)
    with pytest.raises(AgentError, match="oversized" if mode == "oversized" else None):
        await collect_run(workspace)


@pytest.mark.parametrize("mode", ["large_image", "large_image_extension"])
async def test_large_image_frame_finishes_with_bounded_browser_output(
    fake_cli, monkeypatch, mode
):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    result, events = await collect_run(workspace)
    assert result.status == "complete" and result.text == "Hello world"
    image_event = next(p for k, p in events if k == "tool" and "savedPath" in p["content"])
    assert "/workspace/generated-image.png" in image_event["content"]
    assert "generated image data omitted" in image_event["content"]
    assert "iVBORw0KGgo" not in image_event["content"]
    assert len(image_event["content"]) < agents.MAX_TOOL_EVENT_CHARS


@pytest.mark.parametrize("mode", ["denied", "bad_exit"])
async def test_claude_terminal_failure(fake_cli, monkeypatch, mode):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    with pytest.raises(AgentError):
        await collect_run(workspace, "claude")


def alive(pid):
    try:
        os.kill(pid, 0)
        # The process can be reaped between kill(0) and reading procfs.
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (ProcessLookupError, FileNotFoundError):
        return False
    # Orphaned killed grandchildren can briefly be zombies on Linux containers.
    return stat.split()[2] != "Z"


async def wait_for_record(capture, key):
    for _ in range(100):
        if capture.exists():
            found = [r[key] for r in records(capture) if key in r]
            if found:
                return found[-1]
        await asyncio.sleep(0.02)
    raise AssertionError(f"fake CLI never wrote {key}")


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_cancel_kills_owned_process_group(fake_cli, monkeypatch, provider):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "child")
    cancel = asyncio.Event()
    task = asyncio.create_task(collect_run(workspace, provider, cancel=cancel))
    child = await wait_for_record(capture, "child")
    parent = next(r["pid"] for r in records(capture) if "pid" in r)
    # Give the child time to install its SIGTERM handler, then exercise escalation.
    await asyncio.sleep(0.05)
    cancel.set()
    result, _ = await task
    assert result.status == "cancelled"
    for _ in range(50):
        if not alive(child):
            break
        await asyncio.sleep(0.02)
    assert not alive(parent) and not alive(child)


async def test_caller_cancellation_also_cleans_up(fake_cli, monkeypatch):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "hang")
    task = asyncio.create_task(collect_run(workspace))
    pid = await wait_for_record(capture, "pid")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(pid)


@pytest.mark.parametrize(
    "setting,mode",
    [
        ("SHUI_AGENT_TIMEOUT_SECONDS", "hang"),
        ("SHUI_AGENT_IDLE_TIMEOUT_SECONDS", "hang"),
        ("SHUI_AGENT_HANDSHAKE_TIMEOUT_SECONDS", "handshake_timeout"),
    ],
)
async def test_timeout_stops_cli(fake_cli, monkeypatch, setting, mode):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", mode)
    monkeypatch.setenv(setting, "1")
    monkeypatch.setattr(agents, "WAIT_STATUS_SECONDS", 0.03)
    with pytest.raises(AgentError, match="time"):
        await collect_run(workspace)
    pid = next(r["pid"] for r in records(capture) if "pid" in r)
    assert not alive(pid)


class TrackingReader(asyncio.StreamReader):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.finished = asyncio.Event()

    async def readline(self):
        self.calls += 1
        try:
            return await super().readline()
        finally:
            self.finished.set()


def reader_run(workspace, emit):
    run = agents._Run(RunRequest("codex", workspace, "test"), emit, agents._environment())
    reader = TrackingReader()
    run.proc = SimpleNamespace(stdout=reader)
    return run, reader


async def test_wait_status_preserves_pending_read(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.setattr(agents, "WAIT_STATUS_SECONDS", 0.01)
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))
        if len(events) == 2:
            reader.feed_data(b'{"ok": true}\n')

    run, reader = reader_run(workspace, emit)
    assert await run.read() == {"ok": True}
    assert reader.calls == 1 and reader.finished.is_set()
    assert all(k == "status" and "Waiting for native agent output" in p["text"] for k, p in events)


@pytest.mark.parametrize("slow_emit", [False, True])
async def test_wait_status_never_extends_configured_idle_timeout(
    fake_cli, monkeypatch, slow_emit
):
    workspace, _ = fake_cli
    monkeypatch.setattr(agents, "WAIT_STATUS_SECONDS", 0.01)
    monkeypatch.setenv("SHUI_AGENT_IDLE_TIMEOUT_SECONDS", "0.1")
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))
        if slow_emit:
            await asyncio.Event().wait()

    run, reader = reader_run(workspace, emit)
    started = asyncio.get_running_loop().time()
    with pytest.raises(AgentError, match="idle timeout after 0.1 seconds.*session.*retained"):
        await run.read()
    elapsed = asyncio.get_running_loop().time() - started
    assert 0.08 <= elapsed < 0.5
    assert events
    assert reader.calls == 1 and reader.finished.is_set()


async def test_cancel_during_wait_status_leaves_no_pending_reader(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.setattr(agents, "WAIT_STATUS_SECONDS", 0.01)
    waiting = asyncio.Event()

    async def emit(kind, payload):
        waiting.set()

    run, reader = reader_run(workspace, emit)
    task = asyncio.create_task(run.read())
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.finished.is_set()
    reader.feed_data(b'{"ok": true}\n')
    assert await run.read() == {"ok": True}


async def test_default_idle_timeout_allows_long_native_prefill(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.delenv("SHUI_AGENT_IDLE_TIMEOUT_SECONDS")

    async def emit(kind, payload):
        pass

    run, _ = reader_run(workspace, emit)
    assert run.idle_timeout == 1800


async def test_duplicate_native_session_is_rejected(fake_cli, monkeypatch):
    workspace, capture = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "hang")
    cancel = asyncio.Event()
    task = asyncio.create_task(
        collect_run(workspace, session_id="existing", cancel=cancel)
    )
    await wait_for_record(capture, "pid")
    other = workspace.parent / "other"
    other.mkdir()
    try:
        with pytest.raises(AgentError, match="active|in use"):
            await collect_run(other, session_id="existing")
    finally:
        cancel.set()
        await task


async def test_persist_session_failure_prevents_generation(fake_cli):
    workspace, capture = fake_cli

    async def fail_binding(kind, payload):
        if kind == "session":
            raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await collect_run(workspace, emit=fail_binding)
    assert not any(r.get("method") == "turn/start" for r in records(capture))


async def test_secret_split_across_deltas_is_redacted(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    secret = "credential-abcdefghijklmnop"
    monkeypatch.setenv("FAKE_API_KEY", secret)
    monkeypatch.setenv("SHUI_FAKE_MODE", "secret")
    result, events = await collect_run(workspace)
    assert result.text == "Value [redacted] done"
    assert secret not in json.dumps(events)


async def test_bearer_prefix_split_across_deltas_is_redacted(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "bearer")
    result, events = await collect_run(workspace)
    assert "Bearer [redacted]" in result.text
    assert "Bearer abcdefghijklmno" not in json.dumps(events)


async def test_token_prefix_is_redacted_from_output():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    out = agents._Output(
        emit, agents._Redactor({"SHUI_PROXY_URL": "http://127.0.0.1:7890"})
    )
    await out.delta("please use token:secret-token-abcdefghijklmnop", final=True)
    assert "token:[redacted]" in events[0][1]["delta"]
    assert "secret-token-abcdefghijklmnop" not in json.dumps(events)


async def test_structured_tool_secret_is_redacted_before_json_encoding():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    secret = 'quoted"credential\\abcdef'
    redactor = agents._Redactor(
        {"SHUI_PROXY_URL": "http://127.0.0.1:7890", "TEST_API_KEY": secret}
    )
    out = agents._Output(emit, redactor)
    await out.tool("tool", "completed", {"token": secret})
    assert secret not in json.dumps(events)
    assert "redacted" in events[0][1]["content"]


async def test_text_limit_fails_explicitly(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "text_limit")
    monkeypatch.setattr(agents, "MAX_TEXT_CHARS", 200)
    with pytest.raises(AgentError, match="output limit"):
        await collect_run(workspace)


async def test_tool_output_is_bounded():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    out = agents._Output(
        emit, agents._Redactor({"SHUI_PROXY_URL": "http://127.0.0.1:7890"})
    )
    await out.tool("Bash", "running", "x" * (agents.MAX_TOOL_EVENT_CHARS * 2))
    assert len(events[0][1]["content"]) < agents.MAX_TOOL_EVENT_CHARS + 100
    assert "truncated" in events[0][1]["content"]


async def test_inline_image_display_omits_bytes_and_preserves_results_and_paths():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    out = agents._Output(
        emit, agents._Redactor({"SHUI_PROXY_URL": "http://127.0.0.1:7890"})
    )
    data = "data:image/png;base64,iVBORw0KGgo" + "A" * (agents.MAX_TOOL_EVENT_CHARS * 2)
    await out.tool("image", "completed", {"image": {"url": data}, "result": "done", "path": "/tmp/image.png"})
    await out.tool("image", "completed", f"{data}\nSaved /tmp/another.png")
    assert out.tool_chars < 1000
    assert "inline image/png data omitted" in events[0][1]["content"]
    assert '"result": "done"' in events[0][1]["content"]
    assert "/tmp/image.png" in events[0][1]["content"]
    assert "Saved /tmp/another.png" in events[1][1]["content"]
    assert "iVBORw0KGgo" not in json.dumps(events)


async def test_claude_snapshot_without_api_index_after_thinking(fake_cli, monkeypatch):
    workspace, _ = fake_cli
    monkeypatch.setenv("SHUI_FAKE_MODE", "thinking_snapshot")
    result, events = await collect_run(workspace, provider="claude")
    assert result.text == "First answer\n\nSecond answer"
    assert "private" not in json.dumps(events)


def test_provider_status_only_claims_installation(fake_cli):
    values = agents.provider_status()
    assert {v["id"] for v in values} == {"codex", "claude"}
    assert all(set(v) == {"id", "name", "installed"} and v["installed"] for v in values)
