from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sysconfig
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agents import provider_status
from .store import Store, now, uid, verify_password
from .worker import Coordinator

COOKIE = "shui_session"
MAX_UPLOAD = 50 * 1024 * 1024
ACTIVE = ("queued", "running")
FORBIDDEN_NAMES = {
    ".git",
    ".env",
    ".ssh",
    ".codex",
    ".claude",
    "auth.json",
    "credentials.json",
    "credentials",
    "id_rsa",
    "id_ed25519",
    ".shui",
}


def safe_artifact(root: Path, relative: str, directory=False):
    if root.is_symlink():
        raise HTTPException(403, "Symbolic links are not served")
    path = Path(relative or ".")
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(p in FORBIDDEN_NAMES or p.startswith(".env") for p in path.parts)
    ):
        raise HTTPException(400, "Invalid project path")
    target = root / path
    current = root
    for part in path.parts:
        if part == ".":
            continue
        current = current / part
        if current.is_symlink():
            raise HTTPException(403, "Symbolic links are not served")
    if not target.resolve().is_relative_to(root.resolve()):
        raise HTTPException(403, "Path is outside this project")
    if not target.exists():
        raise HTTPException(404, "File not found")
    if directory and not target.is_dir():
        raise HTTPException(400, "Not a directory")
    if not directory and not target.is_file():
        raise HTTPException(400, "Not a regular file")
    return target


def create_app(data_dir: Path | None = None, runner=None, start_worker=True):
    store = Store(
        data_dir
        or Path(os.environ.get("SHUI_DATA_DIR", "~/.local/share/shui")).expanduser()
    )
    coordinator = Coordinator(store, runner)
    source_web = Path(__file__).resolve().parent.parent / "web"
    default_web = (
        source_web
        if source_web.is_dir()
        else Path(sysconfig.get_path("data")) / "share" / "shui" / "web"
    )
    web = Path(os.environ.get("SHUI_WEB_DIR", str(default_web))).resolve()

    @asynccontextmanager
    async def life(app):
        if start_worker:
            await coordinator.start()
        try:
            yield
        finally:
            if start_worker:
                await coordinator.close()

    app = FastAPI(
        title="Shui!",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=life,
    )
    app.state.store = store
    app.state.coordinator = coordinator

    def origin():
        file = os.environ.get("SHUI_ORIGIN_FILE")
        if file:
            try:
                return Path(file).read_text().strip().rstrip("/")
            except OSError:
                pass
        return os.environ.get("SHUI_PUBLIC_ORIGIN", "http://127.0.0.1:18940").rstrip(
            "/"
        )

    @app.middleware("http")
    async def headers(request, call_next):
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > MAX_UPLOAD + 1024 * 1024
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, 400)
            if too_large:
                return JSONResponse({"detail": "Upload exceeds 50 MiB"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(req, exc):
        return JSONResponse(
            {"detail": "This username, session binding or request already exists."}, 409
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(req, exc):
        return JSONResponse({"detail": "The request is incomplete or malformed."}, 422)

    def auth(request, write=False):
        token = request.cookies.get(COOKIE, "")
        with store.connect() as c:
            row = c.execute(
                "SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if not row:
            raise HTTPException(401, "Please sign in")
        if write:
            if (
                request.headers.get("origin")
                and request.headers["origin"].rstrip("/") != origin()
            ):
                raise HTTPException(403, "Origin does not match this site")
            if not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), row["csrf"]
            ):
                raise HTTPException(403, "Refresh the page and try again")
        return dict(row)

    def access(request, pid, write=False, owner=False):
        user = auth(request, write)
        with store.connect() as c:
            role = store.project_role(c, pid, user)
        if role is None:
            raise HTTPException(404, "Project not found")
        if owner and role != "owner":
            raise HTTPException(403, "Project owner permission required")
        if write and role not in ("owner", "editor"):
            raise HTTPException(403, "This project is read-only")
        return user, role

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "JSON required")
        raw = await request.body()
        if len(raw) > 150000:
            raise HTTPException(413, "Request too large")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON object required")
        return data

    def textfield(data, key, limit, default=""):
        value = data.get(key, default)
        if not isinstance(value, str) or len(value) > limit:
            raise HTTPException(400, f"Invalid {key}")
        return value.strip()

    def session_payload(user):
        return {
            "authenticated": bool(user),
            "user": store.user(user) if user else None,
            "csrf": user["csrf"] if user else None,
            "providers": provider_status() if user else [],
            "version": "0.1.0",
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "Shui!", "version": "0.1.0"}

    @app.get("/api/session")
    async def session(request: Request):
        try:
            user = auth(request)
        except HTTPException:
            user = None
        return session_payload(user)

    @app.post("/api/login")
    async def login(request: Request):
        if (
            request.headers.get("origin")
            and request.headers["origin"].rstrip("/") != origin()
        ):
            raise HTTPException(403, "Origin does not match this site")
        data = await body(request)
        username = textfield(data, "username", 80)
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 256:
            raise HTTPException(400, "Invalid password")
        ip = request.client.host if request.client else "unknown"
        with store.connect() as c:
            c.execute("DELETE FROM login_attempts WHERE at<?", (time.time() - 900,))
            if (
                c.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)
                ).fetchone()[0]
                >= 20
            ):
                raise HTTPException(429, "Too many sign-in attempts. Try again later.")
            c.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, time.time()))
            row = c.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
        encoded = row["password_hash"] if row else "00" * 16 + ":" + "00" * 64
        valid = await asyncio.to_thread(verify_password, password, encoded)
        if not row or not valid:
            raise HTTPException(401, "Incorrect username or password")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect() as c:
            c.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
            c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    row["id"],
                    csrf,
                    time.time() + 86400 * 7,
                ),
            )
        user = dict(row)
        user["csrf"] = csrf
        response = JSONResponse(session_payload(user))
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=origin().startswith("https://"),
            samesite="strict",
            max_age=86400 * 7,
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        auth(request, True)
        with store.connect() as c:
            c.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest(),),
            )
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.get("/api/users")
    async def users(request: Request):
        u = auth(request)
        if u["role"] != "owner":
            raise HTTPException(403, "Platform owner permission required")
        with store.connect() as c:
            return {
                "users": [
                    store.user(x)
                    for x in c.execute("SELECT * FROM users ORDER BY created_at")
                ]
            }

    @app.post("/api/users", status_code=201)
    async def user_create(request: Request):
        u = auth(request, True)
        if u["role"] != "owner":
            raise HTTPException(403, "Platform owner permission required")
        data = await body(request)
        try:
            return store.create_user(
                textfield(data, "username", 40),
                textfield(data, "display_name", 80) or textfield(data, "username", 40),
                data.get("password", ""),
                data.get("role", "member"),
            )
        except (ValueError, TypeError):
            raise HTTPException(
                400, "Use a valid username and a password of at least eight characters"
            )

    @app.get("/api/projects")
    async def projects(request: Request):
        u = auth(request)
        with store.connect() as c:
            rows = c.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
            return {
                "projects": [
                    store.project(c, r, u)
                    for r in rows
                    if store.project_role(c, r["id"], u)
                ]
            }

    @app.post("/api/projects", status_code=201)
    async def project_create(request: Request):
        u = auth(request, True)
        if u["role"] != "owner":
            raise HTTPException(403, "Platform owner permission required")
        data = await body(request)
        name = textfield(data, "name", 120)
        description = textfield(data, "description", 3000)
        provider = data.get("provider", "codex")
        model = textfield(data, "model", 120) or None
        sid = textfield(data, "native_session_id", 160) or None
        if not name or provider not in ("codex", "claude"):
            raise HTTPException(400, "Choose a project name and provider")
        if sid and not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", sid):
            raise HTTPException(400, "Invalid native session ID")
        return store.create_project(u, name, description, provider, model, sid)

    @app.get("/api/projects/{pid}")
    async def project_get(pid: str, request: Request):
        u, _ = access(request, pid)
        with store.connect() as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            msgs = c.execute(
                "SELECT * FROM messages WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
                (pid,),
            ).fetchall()
            files = c.execute(
                "SELECT * FROM files WHERE project_id=? ORDER BY created_at DESC",
                (pid,),
            ).fetchall()
            members = c.execute(
                "SELECT u.id,u.username,u.display_name,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE project_id=?",
                (pid,),
            ).fetchall()
            cursor = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE project_id=?", (pid,)
            ).fetchone()[0]
            return {
                "project": store.project(c, row, u),
                "messages": [store.message(c, m) for m in reversed(msgs)],
                "files": [store.file(x) for x in files],
                "members": [dict(x) for x in members],
                "events_cursor": cursor,
            }

    @app.patch("/api/projects/{pid}")
    async def project_patch(pid: str, request: Request):
        u, _ = access(request, pid, True, True)
        data = await body(request)
        if set(data) - {"name", "description", "model", "native_session_id"}:
            raise HTTPException(400, "Unsupported project setting")
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            p = dict(c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
            if {"model", "native_session_id"} & set(data) and c.execute(
                "SELECT 1 FROM turns WHERE project_id=? AND status IN ('queued','running')",
                (pid,),
            ).fetchone():
                raise HTTPException(
                    409, "Wait for queued and running turns before changing the session"
                )
            for key, limit in [
                ("name", 120),
                ("description", 3000),
                ("model", 120),
                ("native_session_id", 160),
            ]:
                if key not in data:
                    continue
                value = textfield(data, key, limit) if data[key] is not None else ""
                if key == "name" and not value:
                    raise HTTPException(400, "Project name is required")
                if key == "native_session_id":
                    if p[key] and value != p[key]:
                        raise HTTPException(
                            409,
                            "This project is already bound. Create a new project to use a different session.",
                        )
                    if value and not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", value):
                        raise HTTPException(400, "Invalid native session ID")
                p[key] = value or (
                    None if key in ("model", "native_session_id") else ""
                )
            c.execute(
                "UPDATE projects SET name=?,description=?,model=?,native_session_id=?,updated_at=? WHERE id=?",
                (
                    p["name"],
                    p["description"],
                    p["model"],
                    p["native_session_id"],
                    now(),
                    pid,
                ),
            )
            out = store.project(
                c, c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone(), u
            )
            store.event(
                c, pid, "project.updated", {"project": out, "actor": u["username"]}
            )
            return out

    @app.get("/api/projects/{pid}/messages")
    async def messages(
        pid: str, request: Request, before: str | None = None, limit: int = 100
    ):
        access(request, pid)
        limit = max(1, min(limit, 200))
        with store.connect() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE project_id=? AND (? IS NULL OR created_at<?) ORDER BY created_at DESC,id DESC LIMIT ?",
                (pid, before, before, limit),
            ).fetchall()
            return {"messages": [store.message(c, r) for r in reversed(rows)]}

    def enqueue(pid, u, content, attachments, client_id, retry_of=None):
        if not content.strip() and not attachments:
            raise HTTPException(400, "Write a message or attach a file")
        if len(content) > 100000 or not re.fullmatch(
            r"[A-Za-z0-9_.-]{8,100}", client_id
        ):
            raise HTTPException(400, "Invalid message or request ID")
        if len(attachments) > 12 or not all(isinstance(x, str) for x in attachments):
            raise HTTPException(400, "Too many attachments")
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute(
                "SELECT * FROM messages WHERE project_id=? AND client_id=?",
                (pid, client_id),
            ).fetchone()
            if previous:
                if previous["author_id"] != u["id"]:
                    raise HTTPException(409, "Request ID belongs to another member")
                return {
                    "message": store.message(c, previous),
                    "turn": dict(
                        c.execute(
                            "SELECT * FROM turns WHERE id=?", (previous["turn_id"],)
                        ).fetchone()
                    ),
                }
            if (
                c.execute(
                    "SELECT COUNT(*) FROM turns WHERE project_id=? AND status='queued'",
                    (pid,),
                ).fetchone()[0]
                >= 100
            ):
                raise HTTPException(429, "Project queue is full")
            for id_ in attachments:
                if not c.execute(
                    "SELECT 1 FROM files WHERE id=? AND project_id=?", (id_, pid)
                ).fetchone():
                    raise HTTPException(400, "Attachment is not in this project")
            tid, mid, aid = uid(), uid(), uid()
            at = now()
            c.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    pid,
                    "user",
                    content,
                    u["id"],
                    "queued",
                    tid,
                    json.dumps(attachments),
                    at,
                    None,
                    client_id,
                ),
            )
            c.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    pid,
                    "assistant",
                    "",
                    None,
                    "queued",
                    tid,
                    "[]",
                    now(),
                    None,
                    None,
                ),
            )
            c.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tid, pid, mid, aid, u["id"], "queued", at, None, None, None),
            )
            c.execute(
                "UPDATE projects SET status=CASE WHEN status='running' THEN status ELSE 'waiting' END,updated_at=? WHERE id=?",
                (at, pid),
            )
            msg = store.message(
                c, c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            )
            turn = dict(c.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone())
            store.event(c, pid, "message.created", {"message": msg})
            store.event(
                c,
                pid,
                "message.created",
                {
                    "message": store.message(
                        c,
                        c.execute(
                            "SELECT * FROM messages WHERE id=?", (aid,)
                        ).fetchone(),
                    )
                },
            )
            store.event(c, pid, "turn.updated", {"turn": turn, "retry_of": retry_of})
        if start_worker:
            coordinator.kick(pid)
        return {"message": msg, "turn": turn}

    @app.post("/api/projects/{pid}/messages", status_code=201)
    async def message_create(pid: str, request: Request):
        u, _ = access(request, pid, True)
        data = await body(request)
        ids = data.get("attachment_ids", [])
        if not isinstance(ids, list):
            raise HTTPException(400, "Attachments must be a list")
        return enqueue(
            pid,
            u,
            textfield(data, "content", 100000),
            ids,
            textfield(data, "client_id", 100),
        )

    @app.post("/api/projects/{pid}/turns/{tid}/retry", status_code=201)
    async def retry(pid: str, tid: str, request: Request):
        u, role = access(request, pid, True)
        data = await body(request)
        with store.connect() as c:
            t = c.execute(
                "SELECT * FROM turns WHERE id=? AND project_id=?", (tid, pid)
            ).fetchone()
            if not t:
                raise HTTPException(404, "Turn not found")
            if t["author_id"] != u["id"] and role != "owner":
                raise HTTPException(403, "Only the author or project owner can retry")
            if t["status"] not in ("failed", "interrupted", "cancelled"):
                raise HTTPException(409, "This turn is not retryable")
            msg = c.execute(
                "SELECT * FROM messages WHERE id=?", (t["user_message_id"],)
            ).fetchone()
        return enqueue(
            pid,
            u,
            msg["content"],
            json.loads(msg["attachments"]),
            textfield(data, "client_id", 100),
            tid,
        )

    @app.post("/api/projects/{pid}/turns/{tid}/cancel")
    async def cancel(pid: str, tid: str, request: Request):
        u, role = access(request, pid, True)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            t = c.execute(
                "SELECT * FROM turns WHERE id=? AND project_id=?", (tid, pid)
            ).fetchone()
            if not t:
                raise HTTPException(404, "Turn not found")
            if t["author_id"] != u["id"] and role != "owner":
                raise HTTPException(403, "Only the author or project owner can cancel")
            if t["status"] == "queued":
                c.execute(
                    "UPDATE turns SET status='cancelled',finished_at=? WHERE id=?",
                    (now(), tid),
                )
                c.execute(
                    "UPDATE messages SET status='cancelled' WHERE turn_id=?", (tid,)
                )
                store.event(
                    c,
                    pid,
                    "turn.updated",
                    {
                        "turn": dict(
                            c.execute(
                                "SELECT * FROM turns WHERE id=?", (tid,)
                            ).fetchone()
                        )
                    },
                )
                if not c.execute(
                    "SELECT 1 FROM turns WHERE project_id=? AND status IN ('running','queued')",
                    (pid,),
                ).fetchone():
                    c.execute("UPDATE projects SET status='idle' WHERE id=?", (pid,))
            elif t["status"] == "running":
                if tid in coordinator.cancels:
                    coordinator.cancels[tid].set()
                else:
                    raise HTTPException(
                        409, "This turn belongs to the external session bridge"
                    )
            store.event(
                c,
                pid,
                "turn.cancel_requested",
                {"turn_id": tid, "actor": u["username"]},
            )
        return {"ok": True}

    @app.get("/api/projects/{pid}/files")
    async def files(pid: str, request: Request):
        access(request, pid)
        with store.connect() as c:
            return {
                "files": [
                    store.file(x)
                    for x in c.execute(
                        "SELECT * FROM files WHERE project_id=? ORDER BY created_at DESC",
                        (pid,),
                    )
                ]
            }

    @app.post("/api/projects/{pid}/files", status_code=201)
    async def upload(pid: str, request: Request, file: UploadFile = File(...)):
        u, _ = access(request, pid, True)
        name = unicodedata.normalize("NFKC", file.filename or "attachment")
        name = Path(name.replace("\\", "/")).name
        if name in FORBIDDEN_NAMES or name.startswith(".env"):
            raise HTTPException(400, "Choose a different filename")
        name = re.sub(r"[^\w.() -]", "_", name)[:180].strip(". ") or "attachment"
        if name in FORBIDDEN_NAMES:
            raise HTTPException(400, "Choose a different filename")
        root = store.workspace(pid)
        if root.is_symlink():
            raise HTTPException(403, "Symbolic links are not served")
        upload_root = safe_artifact(root, "uploads", True)
        fid = uid()
        folder = upload_root / fid
        folder.mkdir(mode=0o700)
        target = folder / name
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("xb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "File exceeds 50 MiB")
                    digest.update(chunk)
                    stream.write(chunk)
            with store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                quota = int(
                    os.environ.get("SHUI_PROJECT_UPLOAD_QUOTA", str(5 * 1024**3))
                )
                used = c.execute(
                    "SELECT COALESCE(SUM(size),0) FROM files WHERE project_id=?", (pid,)
                ).fetchone()[0]
                if used + size > quota:
                    raise HTTPException(413, "Project upload quota reached")
                c.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        pid,
                        name,
                        size,
                        digest.hexdigest(),
                        u["id"],
                        now(),
                        str(target.relative_to(store.workspace(pid))),
                    ),
                )
                out = store.file(
                    c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
                )
                store.event(
                    c, pid, "file.created", {"file": out, "actor": u["username"]}
                )
                return out
        except BaseException:
            target.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()
            raise
        finally:
            await file.close()

    def download(path, name, inline):
        with path.open("rb") as stream:
            is_pdf = path.suffix.lower() == ".pdf" and stream.read(5) == b"%PDF-"
        media = "application/pdf" if is_pdf else "application/octet-stream"
        return FileResponse(
            path,
            filename=name,
            media_type=media,
            content_disposition_type="inline" if inline and is_pdf else "attachment",
            headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
        )

    @app.get("/api/projects/{pid}/files/{fid}")
    async def file_download(pid: str, fid: str, request: Request, inline: int = 0):
        access(request, pid)
        with store.connect() as c:
            f = c.execute(
                "SELECT * FROM files WHERE id=? AND project_id=?", (fid, pid)
            ).fetchone()
        if not f:
            raise HTTPException(404, "File not found")
        path = safe_artifact(store.workspace(pid), f["relative_path"])
        return download(path, f["name"], inline)

    @app.get("/api/projects/{pid}/workspace")
    async def workspace(pid: str, request: Request, path: str = ""):
        access(request, pid)
        root = store.workspace(pid)
        target = safe_artifact(root, path, True)
        entries = []
        for f in sorted(
            target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())
        ):
            if f.is_symlink() or f.name in FORBIDDEN_NAMES or f.name.startswith("."):
                continue
            entries.append(
                {
                    "name": f.name,
                    "path": str(f.relative_to(root)),
                    "type": "directory" if f.is_dir() else "file",
                    "size": f.stat().st_size if f.is_file() else None,
                }
            )
            if len(entries) >= 500:
                break
        return {
            "path": "" if target == root else str(target.relative_to(root)),
            "entries": entries,
        }

    @app.get("/api/projects/{pid}/artifact")
    async def artifact(pid: str, request: Request, path: str, inline: int = 0):
        access(request, pid)
        p = safe_artifact(store.workspace(pid), path)
        return download(p, p.name, inline)

    @app.get("/api/projects/{pid}/members")
    async def members(pid: str, request: Request):
        access(request, pid)
        with store.connect() as c:
            return {
                "members": [
                    dict(x)
                    for x in c.execute(
                        "SELECT u.id,u.username,u.display_name,m.role FROM members m JOIN users u ON u.id=m.user_id WHERE project_id=?",
                        (pid,),
                    )
                ]
            }

    @app.post("/api/projects/{pid}/members")
    async def member_add(pid: str, request: Request):
        u, _ = access(request, pid, True, True)
        data = await body(request)
        role = data.get("role", "editor")
        if role not in ("editor", "viewer"):
            raise HTTPException(400, "Choose editor or viewer")
        with store.connect() as c:
            member = c.execute(
                "SELECT * FROM users WHERE username=?",
                (textfield(data, "username", 40),),
            ).fetchone()
            if not member:
                raise HTTPException(404, "Create this account first")
            existing = c.execute(
                "SELECT role FROM members WHERE project_id=? AND user_id=?",
                (pid, member["id"]),
            ).fetchone()
            if existing and existing["role"] == "owner":
                raise HTTPException(409, "The project owner cannot be demoted here")
            c.execute(
                "INSERT INTO members VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role",
                (pid, member["id"], role),
            )
            store.event(
                c,
                pid,
                "member.updated",
                {"user_id": member["id"], "role": role, "actor": u["username"]},
            )
        return {"ok": True}

    @app.delete("/api/projects/{pid}/members/{user_id}")
    async def member_remove(pid: str, user_id: str, request: Request):
        u, _ = access(request, pid, True, True)
        with store.connect() as c:
            m = c.execute(
                "SELECT role FROM members WHERE project_id=? AND user_id=?",
                (pid, user_id),
            ).fetchone()
            if m and m["role"] == "owner":
                raise HTTPException(409, "The project owner cannot be removed")
            c.execute(
                "DELETE FROM members WHERE project_id=? AND user_id=?", (pid, user_id)
            )
            c.execute(
                "DELETE FROM presence WHERE project_id=? AND user_id=?", (pid, user_id)
            )
            store.event(
                c,
                pid,
                "member.updated",
                {"user_id": user_id, "removed": True, "actor": u["username"]},
            )
        return {"ok": True}

    @app.post("/api/projects/{pid}/presence")
    async def presence(pid: str, request: Request):
        # Viewers can emit presence, but still require CSRF.
        u = auth(request, True)
        with store.connect() as c:
            if not store.project_role(c, pid, u):
                raise HTTPException(404, "Project not found")
            c.execute(
                "INSERT INTO presence VALUES(?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET seen=excluded.seen",
                (pid, u["id"], time.time()),
            )
            online = [
                dict(x)
                for x in c.execute(
                    "SELECT u.id,u.username,u.display_name FROM presence p JOIN users u ON u.id=p.user_id WHERE p.project_id=? AND p.seen>?",
                    (pid, time.time() - 65),
                )
            ]
            store.event(c, pid, "presence", {"members": online})
        return {"ok": True}

    @app.get("/api/projects/{pid}/activity")
    async def activity(pid: str, request: Request, after: int = 0):
        access(request, pid)
        with store.connect() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE project_id=? AND id>? AND type NOT IN ('agent.delta','presence') ORDER BY id DESC LIMIT 150",
                (pid, max(0, after)),
            ).fetchall()
            return {
                "events": [
                    {**dict(x), "payload": json.loads(x["payload"])}
                    for x in reversed(rows)
                ]
            }

    @app.get("/api/projects/{pid}/events")
    async def events(pid: str, request: Request, after: int = 0):
        access(request, pid)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor")

        async def stream():
            nonlocal cursor
            tick = time.monotonic()
            while not await request.is_disconnected():
                try:
                    access(request, pid)
                except HTTPException:
                    return
                with store.connect() as c:
                    rows = c.execute(
                        "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT 200",
                        (pid, cursor),
                    ).fetchall()
                for r in rows:
                    data = {**dict(r), "payload": json.loads(r["payload"])}
                    cursor = r["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if time.monotonic() - tick > 15:
                    yield ": keepalive\n\n"
                    tick = time.monotonic()
                await asyncio.sleep(0.3 if rows else 0.8)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/")
    @app.get("/index.html")
    async def index():
        return FileResponse(web / "index.html", media_type="text/html")

    @app.get("/{asset}")
    async def static(asset: str):
        if asset not in {
            "app.js",
            "lucide-icons.js",
            "styles.css",
            "icon.svg",
            "icon.png",
            "favicon.ico",
        }:
            raise HTTPException(404, "Not found")
        return FileResponse(web / asset)

    return app
