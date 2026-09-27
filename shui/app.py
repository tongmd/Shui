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
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agents import provider_status
from .store import Store, now, password_hash, uid, verify_password
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


def project_link(value, kind):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 500:
        raise HTTPException(400, f"Invalid {kind} link")
    try:
        parts = urlsplit(value.strip())
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.port
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        path = parts.path.rstrip("/")
        if kind == "github":
            if parts.hostname != "github.com" or not re.fullmatch(
                r"/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+", path
            ):
                raise ValueError
            path = path.removesuffix(".git")
            if path.split("/")[-1] in {".", "..", ""}:
                raise ValueError
            return "https://github.com" + path
        if parts.hostname not in {
            "overleaf.com",
            "www.overleaf.com",
        } or not re.fullmatch(r"/project/[0-9a-fA-F]{24}", path):
            raise ValueError
        return "https://www.overleaf.com" + path.lower()
    except ValueError:
        example = (
            "https://github.com/owner/repo"
            if kind == "github"
            else "https://www.overleaf.com/project/PROJECT_ID"
        )
        raise HTTPException(400, f"Use a {kind} project link such as {example}")


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

    def auth(request, write=False, allow_password_change=False):
        token = request.cookies.get(COOKIE, "")
        with store.connect() as c:
            row = c.execute(
                "SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if not row or not row["is_active"]:
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
        if row["must_change_password"] and not allow_password_change:
            raise HTTPException(
                403,
                {
                    "code": "password_change_required",
                    "message": "Please change your initial password before continuing",
                },
            )
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
            "providers": provider_status()
            if user and not user["must_change_password"]
            else [],
            "version": "0.1.0",
        }

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "Shui!", "version": "0.1.0"}

    @app.get("/api/session")
    async def session(request: Request):
        try:
            user = auth(request, allow_password_change=True)
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
        if not row or not valid or not row["is_active"]:
            raise HTTPException(401, "Incorrect username or password")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM users WHERE id=?", (row["id"],)
            ).fetchone()
            if (
                not current
                or not current["is_active"]
                or current["password_hash"] != encoded
            ):
                raise HTTPException(401, "Incorrect username or password")
            row = current
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
        return signed_in_response(row, token, csrf)

    def signed_in_response(row, token, csrf):
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
        auth(request, True, allow_password_change=True)
        with store.connect() as c:
            c.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest(),),
            )
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.post("/api/password")
    async def change_password(request: Request):
        user = auth(request, True, allow_password_change=True)
        data = await body(request)
        old, new, confirm = (
            data.get(key)
            for key in ("current_password", "new_password", "confirm_password")
        )
        if not all(isinstance(v, str) for v in (old, new, confirm)) or len(old) > 256:
            raise HTTPException(400, "Complete all password fields")
        if not 8 <= len(new) <= 256 or not new.strip():
            raise HTTPException(400, "New password must be 8–256 characters")
        if new != confirm:
            raise HTTPException(400, "New passwords do not match")
        if new.casefold() == user["username"].casefold() or new == old:
            raise HTTPException(
                400,
                "Choose a new password different from your username and current password",
            )
        if not await asyncio.to_thread(verify_password, old, user["password_hash"]):
            raise HTTPException(400, "Current password is incorrect")
        encoded = await asyncio.to_thread(password_hash, new)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            session_exists = c.execute(
                "SELECT 1 FROM sessions WHERE token_hash=? AND expires>?",
                (
                    hashlib.sha256(
                        request.cookies.get(COOKIE, "").encode()
                    ).hexdigest(),
                    time.time(),
                ),
            ).fetchone()
            if not current or not current["is_active"] or not session_exists:
                raise HTTPException(401, "Please sign in")
            if current["password_hash"] != user["password_hash"]:
                raise HTTPException(
                    409, "Password changed during this request. Sign in again."
                )
            c.execute(
                "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                (encoded, user["id"]),
            )
            c.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    user["id"],
                    csrf,
                    time.time() + 86400 * 7,
                ),
            )
            updated = c.execute(
                "SELECT * FROM users WHERE id=?", (user["id"],)
            ).fetchone()
        return signed_in_response(updated, token, csrf)

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
        platform_owner(request, True)
        try:
            return store.create_user(
                textfield(data, "username", 40),
                textfield(data, "display_name", 80) or textfield(data, "username", 40),
                None,
                data.get("role", "member"),
                must_change_password=True,
            )
        except (ValueError, TypeError):
            raise HTTPException(400, "Use a valid username, display name and role")

    def platform_owner(request, write=False):
        user = auth(request, write)
        if user["role"] != "owner":
            raise HTTPException(403, "Platform owner permission required")
        return user

    def managed_user(c, user_id):
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Account not found")
        return dict(row)

    def stop_disallowed_turns(c, user):
        for turn in c.execute(
            "SELECT * FROM turns WHERE author_id=? AND status IN ('queued','running')",
            (user["id"],),
        ).fetchall():
            permitted = (
                user["is_active"]
                and not user["must_change_password"]
                and store.project_role(c, turn["project_id"], user)
                in ("owner", "editor")
            )
            if permitted:
                continue
            if turn["status"] == "running":
                if turn["id"] in coordinator.cancels:
                    coordinator.cancels[turn["id"]].set()
            else:
                c.execute(
                    "UPDATE turns SET status='cancelled',finished_at=? WHERE id=?",
                    (now(), turn["id"]),
                )
                c.execute(
                    "UPDATE messages SET status='cancelled' WHERE turn_id=?",
                    (turn["id"],),
                )
                store.event(
                    c,
                    turn["project_id"],
                    "turn.updated",
                    {
                        "turn": dict(
                            c.execute(
                                "SELECT * FROM turns WHERE id=?", (turn["id"],)
                            ).fetchone()
                        )
                    },
                )
                if not c.execute(
                    "SELECT 1 FROM turns WHERE project_id=? AND status IN ('queued','running')",
                    (turn["project_id"],),
                ).fetchone():
                    c.execute(
                        "UPDATE projects SET status='idle' WHERE id=? AND status!='error'",
                        (turn["project_id"],),
                    )

    @app.patch("/api/users/{user_id}")
    async def user_update(user_id: str, request: Request):
        actor = platform_owner(request, True)
        data = await body(request)
        actor = platform_owner(request, True)
        if set(data) - {"display_name", "role", "is_active"}:
            raise HTTPException(400, "Unsupported account setting")
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            target = managed_user(c, user_id)
            if "display_name" in data:
                target["display_name"] = textfield(data, "display_name", 80)
                if not target["display_name"]:
                    raise HTTPException(400, "Display name is required")
            if "role" in data:
                if data["role"] not in ("member", "owner"):
                    raise HTTPException(400, "Choose member or owner")
                target["role"] = data["role"]
            if "is_active" in data:
                if not isinstance(data["is_active"], bool):
                    raise HTTPException(400, "Invalid account status")
                target["is_active"] = data["is_active"]
            protected = c.execute(
                "SELECT id FROM users ORDER BY created_at,id LIMIT 1"
            ).fetchone()["id"]
            if user_id in {actor["id"], protected} and (
                not target["is_active"] or target["role"] != "owner"
            ):
                raise HTTPException(
                    409,
                    "The primary owner and your own owner access cannot be disabled or demoted",
                )
            c.execute(
                "UPDATE users SET display_name=?,role=?,is_active=? WHERE id=?",
                (
                    target["display_name"],
                    target["role"],
                    int(target["is_active"]),
                    user_id,
                ),
            )
            if not target["is_active"]:
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                c.execute("DELETE FROM presence WHERE user_id=?", (user_id,))
            stop_disallowed_turns(c, target)
            for p in c.execute(
                "SELECT project_id FROM members WHERE user_id=?", (user_id,)
            ).fetchall():
                store.event(
                    c,
                    p["project_id"],
                    "member.updated",
                    {"user_id": user_id, "actor": actor["username"]},
                )
            return store.user(target)

    @app.post("/api/users/{user_id}/reset-password")
    async def user_reset_password(user_id: str, request: Request):
        actor = platform_owner(request, True)
        if user_id == actor["id"]:
            raise HTTPException(
                409, "Use your own password-change form for this account"
            )
        with store.connect() as c:
            target = managed_user(c, user_id)
        encoded = await asyncio.to_thread(password_hash, target["username"])
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            actor = platform_owner(request, True)
            protected = c.execute(
                "SELECT id FROM users ORDER BY created_at,id LIMIT 1"
            ).fetchone()["id"]
            if user_id == protected:
                raise HTTPException(
                    409, "The primary owner password must be changed by that owner"
                )
            c.execute(
                "UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?",
                (encoded, user_id),
            )
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM presence WHERE user_id=?", (user_id,))
            target = managed_user(c, user_id)
            stop_disallowed_turns(c, target)
            return store.user(target)

    def membership_payload(c, user):
        result = []
        for project in c.execute(
            "SELECT id,name FROM projects ORDER BY name,id"
        ).fetchall():
            row = c.execute(
                "SELECT role FROM members WHERE project_id=? AND user_id=?",
                (project["id"], user["id"]),
            ).fetchone()
            result.append(
                {
                    "project_id": project["id"],
                    "name": project["name"],
                    "role": row["role"] if row else None,
                    "effective_role": store.project_role(c, project["id"], user)
                    if user["is_active"]
                    else None,
                }
            )
        return {"memberships": result}

    @app.get("/api/users/{user_id}/memberships")
    async def user_memberships(user_id: str, request: Request):
        platform_owner(request)
        with store.connect() as c:
            return membership_payload(c, managed_user(c, user_id))

    @app.put("/api/users/{user_id}/memberships")
    async def user_memberships_update(user_id: str, request: Request):
        actor = platform_owner(request, True)
        data = await body(request)
        assignments = data.get("memberships")
        actor = platform_owner(request, True)
        if not isinstance(assignments, list) or len(assignments) > 1000:
            raise HTTPException(400, "Invalid project memberships")
        wanted = {}
        for item in assignments:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("project_id"), str)
                or item.get("role") not in ("editor", "viewer")
                or item["project_id"] in wanted
            ):
                raise HTTPException(400, "Use one editor or viewer role per project")
            wanted[item["project_id"]] = item["role"]
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            target = managed_user(c, user_id)
            before = {
                r["project_id"]: r["role"]
                for r in c.execute("SELECT * FROM members WHERE user_id=?", (user_id,))
            }
            for pid in wanted:
                if not c.execute(
                    "SELECT 1 FROM projects WHERE id=?", (pid,)
                ).fetchone():
                    raise HTTPException(400, "Project not found")
                if before.get(pid) == "owner":
                    raise HTTPException(
                        409, "Project ownership cannot be replaced here"
                    )
            c.execute(
                "DELETE FROM members WHERE user_id=? AND role!='owner'", (user_id,)
            )
            for pid, role in wanted.items():
                c.execute("INSERT INTO members VALUES(?,?,?)", (pid, user_id, role))
            for pid in set(before) | set(wanted):
                if before.get(pid) == "owner":
                    continue
                if before.get(pid) != wanted.get(pid):
                    c.execute(
                        "DELETE FROM presence WHERE project_id=? AND user_id=?",
                        (pid, user_id),
                    )
                    store.event(
                        c,
                        pid,
                        "member.updated",
                        {
                            "user_id": user_id,
                            "role": wanted.get(pid),
                            "removed": pid not in wanted,
                            "actor": actor["username"],
                        },
                    )
            stop_disallowed_turns(c, target)
            return membership_payload(c, target)

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
        u = platform_owner(request, True)
        name = textfield(data, "name", 120)
        description = textfield(data, "description", 3000)
        provider = data.get("provider", "codex")
        model = textfield(data, "model", 120) or None
        sid = textfield(data, "native_session_id", 160) or None
        if not name or provider not in ("codex", "claude"):
            raise HTTPException(400, "Choose a project name and provider")
        if sid and not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", sid):
            raise HTTPException(400, "Invalid native session ID")
        return store.create_project(
            u,
            name,
            description,
            provider,
            model,
            sid,
            github_url=project_link(data.get("github_url", ""), "github"),
            overleaf_url=project_link(data.get("overleaf_url", ""), "overleaf"),
        )

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
        u, _ = access(request, pid, True, True)
        if set(data) - {
            "name",
            "description",
            "model",
            "native_session_id",
            "github_url",
            "overleaf_url",
        }:
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
            for key, kind in [("github_url", "github"), ("overleaf_url", "overleaf")]:
                if key in data:
                    p[key] = project_link(data[key], kind)
            c.execute(
                "UPDATE projects SET name=?,description=?,model=?,native_session_id=?,github_url=?,overleaf_url=?,updated_at=? WHERE id=?",
                (
                    p["name"],
                    p["description"],
                    p["model"],
                    p["native_session_id"],
                    p["github_url"],
                    p["overleaf_url"],
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
        u, _ = access(request, pid, True)
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
        u, role = access(request, pid, True)
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
                access(request, pid, True)
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
        u, _ = access(request, pid, True, True)
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
            stop_disallowed_turns(c, member)
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
            target = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if target:
                stop_disallowed_turns(c, target)
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

    @app.get("/api/projects/{pid}/event-batch")
    async def event_batch(pid: str, request: Request, after: int = 0):
        access(request, pid)
        with store.connect() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT 200",
                (pid, max(0, after)),
            ).fetchall()
        return {
            "events": [
                {**dict(row), "payload": json.loads(row["payload"])} for row in rows
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
