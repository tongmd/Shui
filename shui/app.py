from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import sysconfig
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .agents import provider_status
from .store import Store, now, password_hash, uid, verify_password
from .worker import Coordinator

COOKIE = "shui_session"
MAX_UPLOAD = 50 * 1024 * 1024
HTTP_SLOW_REQUEST_MS = 1000
http_logger = logging.getLogger("shui.http")
PUBLIC_ASSETS = frozenset({"app.js", "lucide-icons.js", "styles.css", "icon.svg", "icon.png", "favicon.ico", "governance.js", "governance.css", "idea-transfer.js"})
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
SVG_TAGS = {
    "svg", "g", "defs", "symbol", "title", "desc", "path", "rect", "circle",
    "ellipse", "line", "polyline", "polygon", "text", "tspan", "clipPath",
    "mask", "linearGradient", "radialGradient", "stop", "pattern", "marker",
}
SVG_ATTRIBUTES = {
    "id", "class", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2",
    "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "transform",
    "preserveAspectRatio", "fill", "fill-opacity", "fill-rule", "stroke",
    "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray",
    "stroke-dashoffset", "stroke-opacity", "opacity", "clip-path", "clip-rule",
    "mask", "marker-start", "marker-mid", "marker-end", "gradientUnits",
    "gradientTransform", "spreadMethod", "offset", "stop-color", "stop-opacity",
    "font-family", "font-size", "font-weight", "text-anchor", "dominant-baseline",
    "dx", "dy", "letter-spacing", "role", "aria-label",
}
SVG_LOCAL_URL = re.compile(r"url\(\s*['\"]?#[-A-Za-z0-9_.:]+['\"]?\s*\)\Z", re.I)


def sanitize_svg(data: bytes) -> bytes | None:
    if len(data) > 5 * 1024 * 1024:
        return None
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return None
    try:
        root = ET.fromstring(data)
        nodes = list(root.iter())
    except (ET.ParseError, RecursionError, ValueError):
        return None
    if len(nodes) > 50000:
        return None

    def local_name(name):
        return name.rsplit("}", 1)[-1].split(":")[-1]

    if local_name(root.tag) != "svg":
        return None

    def clean(source, depth=0):
        if depth > 100:
            return None
        tag = local_name(source.tag)
        if tag not in SVG_TAGS:
            return None
        target = ET.Element(tag)
        for raw_name, raw_value in source.attrib.items():
            name = local_name(raw_name)
            value = raw_value.strip()
            if name not in SVG_ATTRIBUTES or len(value) > 10000:
                continue
            lowered = value.lower()
            if any(token in lowered for token in ("javascript:", "data:", "file:", "http:", "https:", "expression(", "@import")):
                continue
            if "url(" in lowered and not SVG_LOCAL_URL.fullmatch(value):
                continue
            if name == "id" and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                continue
            target.set(name, value)
        if tag == "svg":
            target.set("xmlns", "http://www.w3.org/2000/svg")
        target.text = source.text
        for child in source:
            safe_child = clean(child, depth + 1)
            if safe_child is not None:
                target.append(safe_child)
        target.tail = source.tail
        return target

    try:
        sanitized = clean(root)
        return ET.tostring(sanitized, encoding="utf-8", xml_declaration=True) if sanitized is not None else None
    except (ET.ParseError, RecursionError, ValueError):
        return None


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


def artifact_link_path(root: Path, value: str) -> str:
    """Map native-agent file links into this project's existing path boundary."""
    if len(value) > 4096 or "\x00" in value:
        raise HTTPException(400, "Invalid project path")
    if value.startswith("sandbox:"):
        value = value.removeprefix("sandbox:")
        if value.startswith("/mnt/data/"):
            value = value.removeprefix("/mnt/data/")
    elif value.startswith("file:"):
        try:
            parts = urlsplit(value)
        except ValueError:
            raise HTTPException(400, "Invalid project path") from None
        if parts.netloc not in ("", "localhost") or parts.query or parts.fragment:
            raise HTTPException(400, "Invalid project path")
        value = unquote(parts.path)
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise HTTPException(400, "Invalid project path")
    if "\x00" in value:
        raise HTTPException(400, "Invalid project path")
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            raise HTTPException(400, "Path is outside this project") from None
    return str(path)


def safe_artifact(root: Path, relative: str, directory=False):
    if root.is_symlink():
        raise HTTPException(403, "Symbolic links are not served")
    path = Path(relative or ".")
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(p in FORBIDDEN_NAMES or p.startswith(".") for p in path.parts)
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
    asset_hashes = {}
    asset_link = re.compile(
        r"(?P<attribute>\b(?:src|href)=)(?P<quote>[\"'])(?P<url>/(?:"
        + "|".join(re.escape(name) for name in sorted(PUBLIC_ASSETS))
        + r")(?:[?#][^\"']*)?)(?P=quote)"
    )

    def asset_fingerprint(asset):
        path = web / asset
        stat = path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = asset_hashes.get(asset)
        if not cached or cached[0] != signature:
            cached = (signature, hashlib.sha256(path.read_bytes()).hexdigest())
            asset_hashes[asset] = cached
        return path, stat, cached[1]

    def version_asset_link(match):
        parts = urlsplit(match.group("url"))
        try:
            _, _, digest = asset_fingerprint(parts.path.removeprefix("/"))
        except OSError:
            return match.group(0)
        href = parts.path + "?v=" + digest + ("#" + parts.fragment if parts.fragment else "")
        return match.group("attribute") + match.group("quote") + href + match.group("quote")

    @asynccontextmanager
    async def life(app):
        with store.keep_wal_open():
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
    deleting_projects = set()

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

    def log_request(request, status, started, first_byte):
        route = getattr(request.scope.get("route"), "path", "<unmatched>")
        finished = time.monotonic()
        first_ms = (first_byte - started) * 1000
        total_ms = (finished - started) * 1000
        # Long-lived event streams are expected; their initial response can still be slow.
        slow = first_ms >= HTTP_SLOW_REQUEST_MS or (not route.endswith("/events") and total_ms >= HTTP_SLOW_REQUEST_MS)
        if status >= 500 or slow:
            http_logger.warning(json.dumps({
                "route": route, "method": request.method, "status": status,
                "ttfb_ms": round(first_ms, 2), "total_ms": round(total_ms, 2),
            }))

    @app.middleware("http")
    async def headers(request, call_next):
        started = time.monotonic()
        length = request.headers.get("content-length")
        if length:
            try:
                too_large = int(length) > MAX_UPLOAD + 1024 * 1024
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, 400)
            if too_large:
                return JSONResponse({"detail": "Upload exceeds 50 MiB"}, 413)
        try:
            response = await call_next(request)
        except Exception:
            log_request(request, 500, started, time.monotonic())
            raise
        first_byte = time.monotonic()
        chunks = response.body_iterator

        async def observed_body():
            try:
                async for chunk in chunks:
                    yield chunk
            finally:
                log_request(request, response.status_code, started, first_byte)

        response.body_iterator = observed_body()
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'",
        )
        response.headers.setdefault("Cache-Control", "private, no-store" if request.url.path.startswith("/api/") else "no-store")
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

    def access(request, pid, write=False, owner=False, allow_deleting=False):
        user = auth(request, write)
        with store.connect() as c:
            role = store.project_role(c, pid, user)
            task = store.delegation_for_child(c, pid)
        if role is None:
            raise HTTPException(404, "Project not found")
        if write and not allow_deleting and (pid in deleting_projects or (task and task["parent_project_id"] in deleting_projects)):
            raise HTTPException(409, "This project is being deleted")
        if write and task and task["status"] != "open":
            raise HTTPException(409, "This collaboration task is closed")
        if owner and role != "owner":
            raise HTTPException(403, "Project owner permission required")
        if write and role not in ("owner", "editor"):
            raise HTTPException(403, "This project is read-only")
        return user, role

    def root_project(c, pid):
        if store.delegation_for_child(c, pid):
            raise HTTPException(409, "Manage this collaboration through its parent project")

    def writable(c, pid, user):
        role = store.project_role(c, pid, user)
        if role is None:
            raise HTTPException(404, "Project not found")
        task = store.delegation_for_child(c, pid)
        if pid in deleting_projects or (task and task["parent_project_id"] in deleting_projects):
            raise HTTPException(409, "This project is being deleted")
        if task and task["status"] != "open":
            raise HTTPException(409, "This collaboration task is closed")
        if role not in ("owner", "editor"):
            raise HTTPException(403, "This project is read-only")

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

    def turn_delegation_changed(c, project_id, turn_id):
        child_task = store.delegation_for_turn(c, turn_id)
        if child_task:
            store.delegation_changed(c, project_id, child_task["id"])
        task = c.execute("SELECT id,child_project_id FROM delegations WHERE merge_turn_id=?", (turn_id,)).fetchone()
        if task:
            store.delegation_changed(c, task["child_project_id"], task["id"])

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
                turn_delegation_changed(c, turn["project_id"], turn["id"])

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
            "SELECT id,name FROM projects WHERE id NOT IN (SELECT child_project_id FROM delegations) ORDER BY name,id"
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
                root_project(c, pid)
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
                "SELECT * FROM projects WHERE id NOT IN (SELECT child_project_id FROM delegations) ORDER BY updated_at DESC"
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
            members = store.project_members(c, pid)
            cursor = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE project_id=?", (pid,)
            ).fetchone()[0]
            return {
                "project": store.project(c, row, u),
                "messages": [store.message(c, m) for m in reversed(msgs)],
                "files": [store.file(x) for x in files],
                "members": [dict(x) for x in members],
                "events_cursor": cursor,
                "insights": store.research(c, pid),
            }

    @app.get("/api/projects/{pid}/meta")
    def project_meta(pid: str, request: Request):
        user, _ = access(request, pid)
        with store.connect() as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                raise HTTPException(404, "Project not found")
            return {"project": store.project(c, row, user), "members": store.project_members(c, pid)}

    def qualify_summary_links(summary, child, parent):
        # Match the same inline-link syntax as the renderer, leaving code and web URLs intact.
        pattern = re.compile(
            r"```[\s\S]*?(?:```|$)|`[^`\n]+`|"
            r"(?P<prefix>\[[^\]\n]+\]\()"
            r"(?P<target><[^>\n]+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+)"
            r'(?P<suffix>(?:\s+"[^"\n]*")?\))'
        )

        def replace(match):
            raw = match.group("target")
            if not raw:
                return match.group(0)
            path = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
            path = re.sub(r"\\([\\()[\]<> ])", r"\1", path)
            if path.startswith(("/api/", "//", "#", "?")) or re.match(r"https?://", path, re.I):
                return match.group(0)
            path = re.sub(r"(?::\d+(?::\d+)?|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$", "", path)
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path) and not path.startswith(("file:", "sandbox:")):
                return match.group(0)
            projects = (child, parent) if path.startswith(("/", "file:")) else (child,)
            for project_id in projects:
                try:
                    relative = artifact_link_path(store.workspace(project_id), unquote(path))
                    safe_artifact(store.workspace(project_id), relative)
                except HTTPException:
                    continue
                href = f"/api/projects/{project_id}/artifact?path={quote(relative, safe='')}"
                return match.group("prefix") + href + match.group("suffix")
            # A bare relative link would silently resolve against the parent workspace.
            label = match.group("prefix")[1:-2]
            return label + " (unavailable collaboration artifact: `" + raw.replace("`", "") + "`)"

        return pattern.sub(replace, summary)[:12000]

    def delegation_request_id(data):
        value = textfield(data, "client_id", 100)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{8,100}", value):
            raise HTTPException(400, "Invalid request ID")
        return value

    def delegation_row(c, pid, did, user, manage=False):
        row = c.execute("SELECT * FROM delegations WHERE id=? AND parent_project_id=?", (did, pid)).fetchone()
        if not row:
            raise HTTPException(404, "Collaboration task not found")
        if manage and not store.can_manage_delegation(c, row, user):
            raise HTTPException(403, "Only the task creator, assignees or project owner can manage this task")
        return row

    def delegation_response(c, task, user, request_row=None):
        if request_row is None:
            request_row = c.execute("SELECT * FROM delegation_requests WHERE delegation_id=? ORDER BY created_at,id LIMIT 1", (task["id"],)).fetchone()
        message = c.execute("SELECT * FROM messages WHERE id=?", (request_row["parent_message_id"] if request_row else task["parent_message_id"],)).fetchone()
        turn = c.execute("SELECT * FROM turns WHERE id=?", (request_row["child_turn_id"],)).fetchone() if request_row else c.execute("SELECT t.* FROM turns t JOIN delegation_turns d ON d.turn_id=t.id WHERE d.delegation_id=? ORDER BY t.created_at,t.id LIMIT 1", (task["id"],)).fetchone()
        return {"delegation": store.delegation(c, task, user), "message": store.message(c, message), "turn": dict(turn)}

    @app.get("/api/projects/{pid}/delegations")
    async def delegation_list(pid: str, request: Request):
        user, _ = access(request, pid)
        with store.connect() as c:
            root_project(c, pid)
            return {
                "delegations": [store.delegation(c, row, user) for row in c.execute(
                    "SELECT * FROM delegations WHERE parent_project_id=? ORDER BY created_at DESC,id DESC", (pid,)
                )],
                "assignable_members": [member for member in store.project_members(c, pid) if member["role"] in ("owner", "editor")],
                "sessions": [{**dict(session), "delegation": store.delegation(c, task, user) if (task := store.delegation_for_child(c, session["child_project_id"])) else None} for session in c.execute("SELECT * FROM collaboration_sessions WHERE parent_project_id=? ORDER BY created_at,assignee_id", (pid,))],
            }

    @app.get("/api/projects/{pid}/delegations/{did}")
    async def delegation_get(pid: str, did: str, request: Request):
        user, _ = access(request, pid)
        with store.connect() as c:
            return {"delegation": store.delegation(c, delegation_row(c, pid, did, user), user)}

    @app.post("/api/projects/{pid}/delegations", status_code=201)
    async def delegation_create(pid: str, request: Request):
        user, _ = access(request, pid, True)
        data = await body(request)
        user, _ = access(request, pid, True)
        if set(data) - {"content", "title", "context", "assignee_ids", "attachment_ids", "client_id"}:
            raise HTTPException(400, "Unsupported collaboration field")
        content = textfield(data, "content", 12000)
        if not content:
            raise HTTPException(400, "Describe the task to assign")
        title = textfield(data, "title", 120) or content.splitlines()[0][:120]
        context = textfield(data, "context", 8000)
        visible_content, _content_secrets = store.redact_message_tokens(content)
        visible_title, title_secrets = store.redact_message_tokens(title)
        visible_context, context_secrets = store.redact_message_tokens(context)
        client_id = delegation_request_id(data)
        assignees = data.get("assignee_ids")
        attachments = data.get("attachment_ids", [])
        for values, maximum, label in ((assignees, 8, "assignees"), (attachments, 12, "attachments")):
            if not isinstance(values, list) or len(values) > maximum or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in values) or len(values) != len(set(values)):
                raise HTTPException(400, f"Invalid {label}")
        if not assignees:
            raise HTTPException(400, "Choose at least one project member")
        workspaces, children = [], []
        try:
            with store.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                root_project(c, pid)
                writable(c, pid, user)
                parent = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
                if parent["provider"] == "bridge":
                    raise HTTPException(409, "Collaboration tasks need a native Codex or Claude project")
                previous = c.execute("SELECT * FROM delegation_requests WHERE parent_project_id=? AND client_id=? ORDER BY created_at,id", (pid, client_id)).fetchall()
                if previous:
                    if {r["assignee_id"] for r in previous} != set(assignees) or any(
                        r["created_by"] != user["id"] or r["content"] != visible_content or r["title"] != visible_title or r["context"] != visible_context or json.loads(r["attachment_ids"]) != attachments for r in previous
                    ):
                        raise HTTPException(409, "Request ID already belongs to another task assignment")
                    assignments = [delegation_response(c, delegation_row(c, pid, r["delegation_id"], user), user, r) for r in previous]
                    return {**assignments[0], "assignments": assignments}
                if c.execute("SELECT 1 FROM messages WHERE project_id=? AND client_id=?", (pid, client_id)).fetchone():
                    raise HTTPException(409, "Request ID already belongs to a message")
                members = {m["id"]: m for m in store.project_members(c, pid) if m["role"] in ("owner", "editor")}
                if any(user_id not in members for user_id in assignees):
                    raise HTTPException(400, "Assignees must be active editors or owners of this project")
                sessions = {row["assignee_id"]: row for row in c.execute("SELECT * FROM collaboration_sessions WHERE parent_project_id=?", (pid,))}
                opened = {row["child_project_id"] for row in c.execute("SELECT child_project_id FROM delegations WHERE parent_project_id=? AND status='open'", (pid,))}
                needed = sum(1 for assignee in assignees if assignee not in sessions or sessions[assignee]["child_project_id"] not in opened)
                if len(opened) + needed > 8:
                    raise HTTPException(429, "At most 8 collaboration tasks can be open in one project")
                for fid in attachments:
                    file = c.execute("SELECT * FROM files WHERE id=? AND project_id=?", (fid, pid)).fetchone()
                    if not file:
                        raise HTTPException(400, "Attachment is not in this project")
                    safe_artifact(store.workspace(pid), file["relative_path"])
                assignments = []
                for index, assignee in enumerate(assignees):
                    rid, mid, at = uid(), uid(), now()
                    session = sessions.get(assignee)
                    if session:
                        child = session["child_project_id"]
                    else:
                        child = uid()
                        workspace = store.workspace(child)
                        workspace.mkdir(mode=0o700)
                        workspaces.append(workspace)
                        (workspace / "uploads").mkdir(mode=0o700)
                        name = f"{parent['name']} · @{members[assignee]['username']}"[:120]
                        c.execute("INSERT INTO projects(id,name,description,provider,model,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (
                            child, name, "Member collaboration session", parent["provider"], parent["model"], "idle", user["id"], at, at,
                        ))
                        c.execute("INSERT INTO collaboration_sessions VALUES(?,?,?,?)", (pid, assignee, child, at))
                        store.event(c, child, "project.created", {"name": name, "provider": parent["provider"], "actor": user["username"]})
                    if c.execute("SELECT COUNT(*) FROM turns WHERE project_id=? AND status='queued'", (child,)).fetchone()[0] >= 100:
                        raise HTTPException(429, "The member's collaboration queue is full")
                    task = c.execute("SELECT * FROM delegations WHERE child_project_id=? AND status='open'", (child,)).fetchone()
                    new_task = task is None
                    if new_task and c.execute("SELECT 1 FROM turns WHERE project_id=? AND status='running'", (child,)).fetchone():
                        raise HTTPException(409, "The previous collaboration task is still stopping; try again shortly")
                    card = "@" + members[assignee]["username"] + "\n\n" + visible_content
                    card_client = client_id if index == 0 else "delegation.card." + rid
                    c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, pid, "user", card, user["id"], "complete", None, json.dumps(attachments), at, None, card_client))
                    if new_task:
                        did = uid()
                        c.execute("INSERT INTO delegations(id,parent_project_id,child_project_id,parent_message_id,created_by,title,instructions,context,attachment_ids,client_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                            did, pid, child, mid, user["id"], visible_title, visible_content, visible_context, json.dumps(attachments), "delegation.task." + did, at, at,
                        ))
                        c.execute("INSERT INTO delegation_assignees VALUES(?,?)", (did, assignee))
                    else:
                        did = task["id"]
                    queued = store.enqueue_turn(c, child, user, content, [], "delegation.request." + rid, emit=False)
                    c.execute("INSERT INTO delegation_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        rid, did, pid, child, mid, queued["turn"]["id"], user["id"], assignee, client_id, visible_title, visible_content, visible_context, json.dumps(attachments), at,
                    ))
                    for field, values in (("title", title_secrets), ("context", context_secrets)):
                        c.executemany("INSERT INTO delegation_request_secrets VALUES(?,?,?,?)", (
                            (rid, field, position, store.vault.encrypt_secret(value)) for position, value in enumerate(values)
                        ))
                    c.execute("UPDATE delegation_turns SET request_id=? WHERE turn_id=?", (rid, queued["turn"]["id"]))
                    store.publish_enqueued_turn(c, queued["turn"])
                    task = delegation_row(c, pid, did, user)
                    request_row = c.execute("SELECT * FROM delegation_requests WHERE id=?", (rid,)).fetchone()
                    result = delegation_response(c, task, user, request_row)
                    assignments.append(result)
                    children.append(child)
                    store.event(c, pid, "message.created", {"message": result["message"]})
                    if new_task:
                        store.event(c, pid, "delegation.created", {"delegation": store.delegation(c, task)})
                c.execute("UPDATE projects SET updated_at=? WHERE id=?", (now(), pid))
        except BaseException:
            for workspace in workspaces:
                shutil.rmtree(workspace, ignore_errors=True)
            raise
        if start_worker:
            for child in children:
                coordinator.kick(child)
        return {**assignments[0], "assignments": assignments}

    @app.post("/api/projects/{pid}/delegations/{did}/merge")
    async def delegation_merge(pid: str, did: str, request: Request):
        user, _ = access(request, pid, True)
        data = await body(request)
        user, _ = access(request, pid, True)
        if set(data) - {"summary", "client_id"}:
            raise HTTPException(400, "Unsupported merge field")
        client_id = delegation_request_id(data)
        summary = textfield(data, "summary", 12000)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            writable(c, pid, user)
            task = delegation_row(c, pid, did, user, True)
            if task["status"] == "cancelled":
                raise HTTPException(409, "This collaboration task was cancelled")
            if task["merge_turn_id"]:
                turn = c.execute("SELECT * FROM turns WHERE id=?", (task["merge_turn_id"],)).fetchone()
                message = c.execute("SELECT * FROM messages WHERE id=?", (turn["user_message_id"],)).fetchone()
                return {"delegation": store.delegation(c, task, user), "message": store.message(c, message), "turn": dict(turn)}
            child = task["child_project_id"]
            if c.execute("SELECT 1 FROM turns WHERE project_id=? AND status IN ('queued','running')", (child,)).fetchone():
                raise HTTPException(409, "Wait for the collaboration agent to finish or stop its pending turns before merging")
            if not summary:
                last = c.execute("SELECT m.content FROM messages m JOIN delegation_turns d ON d.turn_id=m.turn_id WHERE d.delegation_id=? AND m.role='assistant' AND m.status='complete' AND m.content!='' ORDER BY m.created_at DESC,m.id DESC LIMIT 1", (did,)).fetchone()
                if not last:
                    raise HTTPException(400, "Write a result summary before merging")
                summary = last["content"][:12000]
            summary = qualify_summary_links(summary, child, pid)
            if c.execute("SELECT 1 FROM messages WHERE project_id=? AND client_id=?", (pid, client_id)).fetchone():
                raise HTTPException(409, "Request ID already belongs to a message")
            if c.execute("SELECT COUNT(*) FROM turns WHERE project_id=? AND status='queued'", (pid,)).fetchone()[0] >= 100:
                raise HTTPException(429, "Project queue is full")
            # Only final summary and bounded artifact references enter the parent native session.
            output_prefix = f"tasks/{did}/%"
            files = [f"- {row['name']}: {row['relative_path']}" for row in c.execute("SELECT name,relative_path FROM files WHERE project_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 12", (child, task["created_at"]))]
            files += [f"- {row['title'][:180]}: {row['path'][:500]}" for row in c.execute("SELECT title,path FROM figures WHERE project_id=? AND (path LIKE ? OR (path NOT LIKE 'tasks/%' AND created_at>=?)) ORDER BY updated_at DESC LIMIT 12", (child, output_prefix, task["created_at"]))]
            content = (
                f"Collaboration result: {task['title']}\n\n{summary}\n\n"
                f"Source collaboration session: {child}\nChild workspace: {store.workspace(child)}\n"
                f"Current task output directory: {store.workspace(child) / 'tasks' / did}\n"
                "Review these results and integrate the relevant findings into the parent task. "
                "Inspect child artifacts before copying or replacing any parent files. "
                "The separate discussion remains available in the collaboration session.\n"
                + ("Artifact references:\n" + "\n".join(files) if files else "")
            )
            result = store.enqueue_turn(c, pid, user, content, [], client_id, emit=False)
            c.execute("UPDATE delegations SET status='completed',summary=?,merge_turn_id=?,updated_at=?,completed_at=? WHERE id=?", (summary, result["turn"]["id"], now(), now(), did))
            c.execute("INSERT INTO delegation_merge_attempts VALUES(?,?)", (did, result["turn"]["id"]))
            result = store.publish_enqueued_turn(c, result["turn"])
            store.delegation_changed(c, child, did)
            result["delegation"] = store.delegation(c, delegation_row(c, pid, did, user), user)
        if start_worker:
            coordinator.kick(pid)
        return result

    @app.post("/api/projects/{pid}/delegations/{did}/cancel")
    async def delegation_cancel(pid: str, did: str, request: Request):
        user, _ = access(request, pid, True)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            writable(c, pid, user)
            task = delegation_row(c, pid, did, user, True)
            if task["status"] == "completed":
                raise HTTPException(409, "The result was already submitted; manage its integration turn in the parent session")
            if task["status"] == "cancelled":
                return {"delegation": store.delegation(c, task, user)}
            child = task["child_project_id"]
            c.execute("UPDATE delegations SET status='cancelled',updated_at=? WHERE id=?", (now(), did))
            for turn in c.execute("SELECT t.* FROM turns t JOIN delegation_turns d ON d.turn_id=t.id WHERE d.delegation_id=? AND t.status IN ('queued','running')", (did,)).fetchall():
                if turn["status"] == "running":
                    event = coordinator.cancels.get(turn["id"])
                    if event:
                        event.set()
                else:
                    c.execute("UPDATE turns SET status='cancelled',finished_at=? WHERE id=?", (now(), turn["id"]))
                    c.execute("UPDATE messages SET status='cancelled' WHERE turn_id=?", (turn["id"],))
                    store.event(c, child, "turn.updated", {"turn": dict(c.execute("SELECT * FROM turns WHERE id=?", (turn["id"],)).fetchone())})
            if not c.execute("SELECT 1 FROM turns WHERE project_id=? AND status='running'", (child,)).fetchone():
                c.execute("UPDATE projects SET status='idle',updated_at=? WHERE id=?", (now(), child))
            store.delegation_changed(c, child, did)
            store.event(c, child, "delegation.closed", {"status": "cancelled"})
            return {"delegation": store.delegation(c, delegation_row(c, pid, did, user), user)}

    def research_text(data, key, limit, default=""):
        value = data.get(key, default)
        if value is None:
            return default
        if not isinstance(value, str) or len(value) > limit:
            raise HTTPException(400, f"Invalid {key}")
        return value.strip()

    def research_json(data, key):
        value = data.get(key, {})
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise HTTPException(400, f"Invalid {key}")
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid {key}")
        if len(encoded) > 20000:
            raise HTTPException(400, f"{key} is too large")
        return value

    def research_id(value, label):
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise HTTPException(400, f"Invalid {label}")
        return value

    def research_list(data, key, limit=200):
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > limit:
            raise HTTPException(400, f"Invalid {key}")
        if not all(isinstance(item, dict) for item in value):
            raise HTTPException(400, f"Invalid {key}")
        return value

    @app.get("/api/projects/{pid}/research")
    async def research_get(pid: str, request: Request):
        access(request, pid)
        with store.connect() as c:
            return {"insights": store.research(c, pid)}

    @app.post("/api/projects/{pid}/research")
    async def research_update(pid: str, request: Request):
        u, _ = access(request, pid, True)
        data = await body(request)
        experiments = research_list(data, "experiments")
        figures = research_list(data, "figures")
        claims = research_list(data, "claims")
        results = research_list(data, "results")
        allowed = {"experiments", "figures", "claims", "results"}
        if set(data) - allowed:
            raise HTTPException(400, "Unsupported research field")
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            writable(c, pid, u)
            existing_experiments = {
                row["id"]
                for row in c.execute(
                    "SELECT id FROM experiments WHERE project_id=?", (pid,)
                )
            }
            incoming_experiments = set()
            for item in experiments:
                item_id = research_id(item.get("id"), "experiment id")
                if item_id and item_id not in existing_experiments:
                    raise HTTPException(404, "Experiment not found")
                eid = item_id or uid()
                incoming_experiments.add(eid)
                name = research_text(item, "name", 180) or research_text(item, "title", 180)
                if not name:
                    raise HTTPException(400, "Experiment name is required")
                status = research_text(item, "status", 30, "planned")
                if status not in ("planned", "running", "completed", "failed", "blocked"):
                    raise HTTPException(400, "Invalid experiment status")
                at = now()
                values = (
                    eid,
                    pid,
                    name,
                    research_text(item, "description", 4000),
                    research_text(item, "objective", 4000),
                    research_text(item, "claim", 4000),
                    research_text(item, "validation", 4000),
                    status,
                    research_text(item, "result_summary", 4000)
                    or research_text(item, "result", 4000),
                    json.dumps(research_json(item, "metadata"), ensure_ascii=False),
                    u["id"],
                    at,
                    at,
                )
                c.execute(
                    "INSERT INTO experiments(id,project_id,name,description,objective,claim,validation,status,result_summary,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,objective=excluded.objective,claim=excluded.claim,validation=excluded.validation,status=excluded.status,result_summary=excluded.result_summary,metadata=excluded.metadata,updated_at=excluded.updated_at",
                    values,
                )
            existing_figures = {
                row["id"]
                for row in c.execute(
                    "SELECT id FROM figures WHERE project_id=?", (pid,)
                )
            }
            for item in figures:
                item_id = research_id(item.get("id"), "figure id")
                if item_id and item_id not in existing_figures:
                    raise HTTPException(404, "Figure not found")
                fid = item_id or uid()
                eid = research_id(item.get("experiment_id"), "experiment id")
                if eid and eid not in incoming_experiments and not c.execute(
                    "SELECT 1 FROM experiments WHERE id=? AND project_id=?", (eid, pid)
                ).fetchone():
                    raise HTTPException(404, "Experiment not found")
                path = research_text(item, "path", 500)
                if not path or Path(path).is_absolute() or ".." in Path(path).parts:
                    raise HTTPException(400, "Invalid figure path")
                figure_path = safe_artifact(store.workspace(pid), path)
                suffix = figure_path.suffix.lower()
                mime = mimetypes.guess_type(figure_path.name)[0] or ""
                if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf"}:
                    raise HTTPException(400, "Figure path must be an image, SVG, or PDF")
                if figure_path.stat().st_size > 100 * 1024 * 1024:
                    raise HTTPException(413, "Figure exceeds 100 MiB")
                digest = hashlib.sha256(figure_path.read_bytes()).hexdigest()
                at = now()
                values = (
                    fid,
                    pid,
                    eid,
                    research_text(item, "title", 180) or figure_path.name,
                    path,
                    research_text(item, "caption", 4000),
                    research_text(item, "claim", 4000) or research_text(item, "purpose", 4000),
                    research_text(item, "validation", 4000),
                    research_text(item, "kind", 60, "figure"),
                    mime,
                    figure_path.stat().st_size,
                    digest,
                    u["id"],
                    at,
                    at,
                )
                c.execute(
                    "INSERT INTO figures(id,project_id,experiment_id,title,path,caption,claim,validation,kind,mime_type,size,sha256,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,title=excluded.title,path=excluded.path,caption=excluded.caption,claim=excluded.claim,validation=excluded.validation,kind=excluded.kind,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at",
                    values,
                )
            for item in claims:
                item_id = research_id(item.get("id"), "claim id")
                if item_id and not c.execute(
                    "SELECT 1 FROM claims WHERE id=? AND project_id=?", (item_id, pid)
                ).fetchone():
                    raise HTTPException(404, "Claim not found")
                cid = item_id or uid()
                eid = research_id(item.get("experiment_id"), "experiment id")
                if eid and not c.execute(
                    "SELECT 1 FROM experiments WHERE id=? AND project_id=?", (eid, pid)
                ).fetchone():
                    raise HTTPException(404, "Experiment not found")
                statement = research_text(item, "statement", 6000)
                if not statement:
                    raise HTTPException(400, "Claim statement is required")
                status = research_text(item, "status", 30, "proposed")
                if status not in ("proposed", "supported", "partial", "refuted", "unverified"):
                    raise HTTPException(400, "Invalid claim status")
                at = now()
                c.execute(
                    "INSERT INTO claims(id,project_id,experiment_id,statement,status,evidence,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,statement=excluded.statement,status=excluded.status,evidence=excluded.evidence,metadata=excluded.metadata,updated_at=excluded.updated_at",
                    (
                        cid, pid, eid, statement, status,
                        research_text(item, "evidence", 6000),
                        json.dumps(research_json(item, "metadata"), ensure_ascii=False),
                        u["id"], at, at,
                    ),
                )
            for item in results:
                item_id = research_id(item.get("id"), "result id")
                if item_id and not c.execute(
                    "SELECT 1 FROM results WHERE id=? AND project_id=?", (item_id, pid)
                ).fetchone():
                    raise HTTPException(404, "Result not found")
                rid = item_id or uid()
                eid = research_id(item.get("experiment_id"), "experiment id")
                if eid and not c.execute(
                    "SELECT 1 FROM experiments WHERE id=? AND project_id=?", (eid, pid)
                ).fetchone():
                    raise HTTPException(404, "Experiment not found")
                status = research_text(item, "status", 30, "observed")
                if status not in ("observed", "supports", "partial", "contradicts", "inconclusive"):
                    raise HTTPException(400, "Invalid result status")
                at = now()
                c.execute(
                    "INSERT INTO results(id,project_id,experiment_id,metric,value,unit,model,summary,status,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,metric=excluded.metric,value=excluded.value,unit=excluded.unit,model=excluded.model,summary=excluded.summary,status=excluded.status,metadata=excluded.metadata,updated_at=excluded.updated_at",
                    (
                        rid, pid, eid,
                        research_text(item, "metric", 180),
                        research_text(item, "value", 500),
                        research_text(item, "unit", 80),
                        research_text(item, "model", 180),
                        research_text(item, "summary", 4000) or research_text(item, "result", 4000),
                        status,
                        json.dumps(research_json(item, "metadata"), ensure_ascii=False),
                        u["id"], at, at,
                    ),
                )
            insights = store.research(c, pid)
            store.event(c, pid, "research.updated", {"insights": insights, "actor": u["username"]})
            return {"insights": insights}

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
            root_project(c, pid)
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

    @app.delete("/api/projects/{pid}")
    async def project_delete(pid: str, request: Request):
        actor, _ = access(request, pid, True, True, allow_deleting=True)
        data = await body(request)
        actor, _ = access(request, pid, True, True, allow_deleting=True)
        confirmation = textfield(data, "confirm_name", 120)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            root_project(c, pid)
            project = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not project:
                raise HTTPException(404, "Project not found")
            if confirmation != project["name"]:
                raise HTTPException(400, "Type the exact project name to delete it")
            project_ids = [r[0] for r in c.execute("SELECT DISTINCT child_project_id FROM delegations WHERE parent_project_id=?", (pid,))] + [pid]
            deleting_projects.update(project_ids)
            c.execute("UPDATE delegations SET status='cancelled',updated_at=? WHERE parent_project_id=? AND status='open'", (now(), pid))
            pending = []
            for project_id in project_ids:
                c.execute("UPDATE projects SET status='error',updated_at=? WHERE id=?", (now(), project_id))
                c.execute("UPDATE turns SET status='cancelled',finished_at=? WHERE project_id=? AND status='queued'", (now(), project_id))
                c.execute("UPDATE messages SET status='cancelled' WHERE turn_id IN (SELECT id FROM turns WHERE project_id=? AND status='cancelled')", (project_id,))
                for turn in c.execute("SELECT id FROM turns WHERE project_id=? AND status='running'", (project_id,)):
                    event = coordinator.cancels.get(turn["id"])
                    if event:
                        event.set()
                if task := coordinator.tasks.get(project_id):
                    pending.append(task)
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*(asyncio.shield(task) for task in pending)), timeout=20)
            except asyncio.TimeoutError:
                raise HTTPException(409, "The active task is still stopping; try again shortly")
        actor, _ = access(request, pid, True, True, allow_deleting=True)
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Remove references before the underlying child/parent messages and turns.
            c.execute("DELETE FROM delegations WHERE parent_project_id=?", (pid,))
            c.execute("DELETE FROM collaboration_sessions WHERE parent_project_id=?", (pid,))
            for project_id in project_ids:
                for table in ("results", "claims", "figures", "experiments", "presence", "bridge_state", "events", "files", "turns", "messages", "members"):
                    c.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
                c.execute("DELETE FROM migrations WHERE project_id=?", (project_id,))
                c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        for project_id in project_ids:
            shutil.rmtree(store.workspace(project_id), ignore_errors=True)
            deleting_projects.discard(project_id)
        return {"ok": True, "deleted": pid, "actor": actor["username"]}

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
            writable(c, pid, u)
            previous = c.execute(
                "SELECT * FROM messages WHERE project_id=? AND client_id=?",
                (pid, client_id),
            ).fetchone()
            if previous:
                if previous["author_id"] != u["id"]:
                    raise HTTPException(409, "Request ID belongs to another member")
                if not previous["turn_id"]:
                    raise HTTPException(409, "Request ID belongs to a collaboration task")
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
            merging = None
            if retry_of:
                child_task = store.delegation_for_turn(c, retry_of)
                if child_task and child_task["status"] != "open":
                    raise HTTPException(409, "This turn belongs to a closed collaboration task; send the new task in its current session")
                merging = c.execute(
                    "SELECT d.* FROM delegations d JOIN delegation_merge_attempts a ON a.delegation_id=d.id WHERE a.turn_id=?",
                    (retry_of,),
                ).fetchone()
                if merging and merging["merge_turn_id"] != retry_of:
                    raise HTTPException(409, "A newer integration attempt already exists; retry that turn instead")
            result = store.enqueue_turn(c, pid, u, content, attachments, client_id, retry_of, emit=not bool(merging))
            if merging:
                c.execute("UPDATE delegations SET merge_turn_id=?,updated_at=? WHERE id=?", (result["turn"]["id"], now(), merging["id"]))
                c.execute("INSERT INTO delegation_merge_attempts VALUES(?,?)", (merging["id"], result["turn"]["id"]))
                result = store.publish_enqueued_turn(c, result["turn"], retry_of)
                store.delegation_changed(c, merging["child_project_id"], merging["id"])
            msg, turn = result["message"], result["turn"]
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
            content = store.message_content(c, msg)
        return enqueue(
            pid,
            u,
            content,
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
            turn_delegation_changed(c, pid, tid)
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
        suffix = path.suffix.lower()
        media_by_suffix = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
        }
        media = media_by_suffix.get(suffix, "application/octet-stream")
        allowed_inline = False
        if inline and suffix in media_by_suffix:
            if suffix == ".svg":
                if path.stat().st_size <= 5 * 1024 * 1024:
                    safe_svg = sanitize_svg(path.read_bytes())
                    if safe_svg is not None:
                        return Response(
                            safe_svg,
                            media_type="image/svg+xml",
                            headers={
                                "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
                                "X-Content-Type-Options": "nosniff",
                            },
                        )
            else:
                with path.open("rb") as stream:
                    head = stream.read(4096)
                if suffix == ".pdf":
                    allowed_inline = head.startswith(b"%PDF-")
                elif suffix == ".png":
                    allowed_inline = head.startswith(b"\x89PNG\r\n\x1a\n")
                elif suffix in (".jpg", ".jpeg"):
                    allowed_inline = head.startswith(b"\xff\xd8\xff")
                elif suffix == ".gif":
                    allowed_inline = head.startswith((b"GIF87a", b"GIF89a"))
                elif suffix == ".webp":
                    allowed_inline = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        return FileResponse(
            path,
            filename=name,
            media_type=media if allowed_inline else "application/octet-stream",
            content_disposition_type="inline" if allowed_inline else "attachment",
            headers={
                "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.api_route("/api/projects/{pid}/files/{fid}", methods=["GET", "HEAD"])
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

    @app.api_route("/api/projects/{pid}/artifact", methods=["GET", "HEAD"])
    async def artifact(pid: str, request: Request, path: str, inline: int = 0):
        access(request, pid)
        root = store.workspace(pid)
        p = safe_artifact(root, artifact_link_path(root, path))
        return download(p, p.name, inline)

    @app.get("/api/projects/{pid}/members")
    async def members(pid: str, request: Request):
        access(request, pid)
        with store.connect() as c:
            return {"members": store.project_members(c, pid)}

    @app.post("/api/projects/{pid}/members")
    async def member_add(pid: str, request: Request):
        u, _ = access(request, pid, True, True)
        data = await body(request)
        u, _ = access(request, pid, True, True)
        role = data.get("role", "editor")
        if role not in ("editor", "viewer"):
            raise HTTPException(400, "Choose editor or viewer")
        with store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            root_project(c, pid)
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
            c.execute("BEGIN IMMEDIATE")
            root_project(c, pid)
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
    def activity(pid: str, request: Request, after: int = 0):
        access(request, pid)
        with store.connect() as c:
            return {"events": store.read_events(c, pid, after, 150, activity=True)}

    @app.get("/api/projects/{pid}/event-batch")
    def event_batch(pid: str, request: Request, after: int = 0):
        access(request, pid)
        with store.connect() as c:
            return {"events": store.read_events(c, pid, after)}

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
                    rows = store.read_events(c, pid, cursor)
                for data in rows:
                    cursor = data["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if time.monotonic() - tick > 15:
                    # EventSource hides comments from JavaScript. A separate named
                    # heartbeat proves the stream is still delivering during idle
                    # periods without changing the durable replay cursor.
                    yield ": keepalive\n\nevent: heartbeat\ndata: {}\n\n"
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
        # HTML always discovers the current bytes; asset URLs change automatically
        # on edits/deploys, so immutable cached scripts cannot hide a new release.
        html = (web / "index.html").read_text(encoding="utf-8")
        return Response(asset_link.sub(version_asset_link, html), media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.api_route("/{asset}", methods=["GET", "HEAD"])
    async def static(asset: str, request: Request, v: str = ""):
        if asset not in PUBLIC_ASSETS:
            raise HTTPException(404, "Not found")
        try:
            path, stat, digest = asset_fingerprint(asset)
        except OSError:
            raise HTTPException(404, "Not found") from None
        cache = "public, max-age=31536000, immutable" if v == digest else "public, no-cache"
        etag = '"' + digest + '"'
        headers = {"Cache-Control": cache, "ETag": etag}
        supplied = request.headers.get("if-none-match", "")
        if any(tag.strip().removeprefix("W/") in (etag, "*") for tag in supplied.split(",")):
            return Response(status_code=304, headers=headers)
        return FileResponse(path, stat_result=stat, headers=headers)

    from .vault import install_vault
    from .governance import install_governance
    install_vault(app, store, auth, access)
    install_governance(app, store, auth, access, coordinator)
    return app
