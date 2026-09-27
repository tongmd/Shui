"""Durable shared state. One SQLite database; each project is one paper/session."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    )


def uid():
    return secrets.token_hex(16)


def password_hash(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return salt + ":" + key


def verify_password(password, encoded):
    try:
        return secrets.compare_digest(
            password_hash(password, encoded.split(":", 1)[0]), encoded
        )
    except (ValueError, TypeError):
        return False


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "shui.sqlite"
        (self.root / "projects").mkdir(exist_ok=True, mode=0o700)
        self.initialize()

    @contextlib.contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=20000")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def initialize(self):
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,display_name TEXT NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('owner','member')),created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts(ip TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',provider TEXT NOT NULL CHECK(provider IN ('codex','claude','bridge')),model TEXT,native_session_id TEXT,status TEXT NOT NULL DEFAULT 'idle',created_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS project_native_session ON projects(provider,native_session_id) WHERE native_session_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS members(project_id TEXT NOT NULL REFERENCES projects(id),user_id TEXT NOT NULL REFERENCES users(id),role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),PRIMARY KEY(project_id,user_id));
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),role TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',author_id TEXT REFERENCES users(id),status TEXT NOT NULL,turn_id TEXT,attachments TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,error TEXT,client_id TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS request_idempotency ON messages(project_id,client_id) WHERE client_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS message_project_date ON messages(project_id,created_at);
            CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),user_message_id TEXT NOT NULL REFERENCES messages(id),assistant_message_id TEXT NOT NULL REFERENCES messages(id),author_id TEXT NOT NULL REFERENCES users(id),status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error TEXT);
            CREATE INDEX IF NOT EXISTS turn_queue ON turns(project_id,status,created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_turn ON turns(project_id) WHERE status='running';
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,uploaded_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,relative_path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL REFERENCES projects(id),type TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_project ON events(project_id,id);
            CREATE TABLE IF NOT EXISTS presence(project_id TEXT NOT NULL REFERENCES projects(id),user_id TEXT NOT NULL REFERENCES users(id),seen REAL NOT NULL,PRIMARY KEY(project_id,user_id));
            CREATE TABLE IF NOT EXISTS bridge_state(project_id TEXT PRIMARY KEY REFERENCES projects(id),session_id TEXT NOT NULL,last_seen REAL NOT NULL,phase TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS migrations(source_key TEXT PRIMARY KEY,project_id TEXT NOT NULL,created_at TEXT NOT NULL);
            """)
        os.chmod(self.path, 0o600)

    def workspace(self, project_id):
        # Only database-created IDs are passed here, never a client-supplied path.
        if len(project_id) != 32 or any(
            x not in "0123456789abcdef" for x in project_id
        ):
            raise ValueError("Invalid project ID")
        return self.root / "projects" / project_id

    @staticmethod
    def event(c, project_id, kind, payload):
        cur = c.execute(
            "INSERT INTO events(project_id,type,payload,created_at) VALUES(?,?,?,?)",
            (project_id, kind, json.dumps(payload, ensure_ascii=False), now()),
        )
        return cur.lastrowid

    @staticmethod
    def user(row):
        return {k: row[k] for k in ("id", "username", "display_name", "role")}

    def create_user(self, username, display_name, password, role="member"):
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,39}", username):
            raise ValueError(
                "Username must be 2–40 letters, digits, dots, underscores or hyphens"
            )
        if not 8 <= len(password) <= 256:
            raise ValueError("Password must be 8–256 characters")
        if (
            role not in ("owner", "member")
            or not display_name.strip()
            or len(display_name) > 80
        ):
            raise ValueError("Invalid user settings")
        with self.connect() as c:
            id_ = uid()
            c.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)",
                (id_, username, display_name, password_hash(password), role, now()),
            )
            return self.user(
                c.execute("SELECT * FROM users WHERE id=?", (id_,)).fetchone()
            )

    def project_role(self, c, project_id, user):
        if not c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            return None
        if user["role"] == "owner":
            return "owner"
        r = c.execute(
            "SELECT role FROM members WHERE project_id=? AND user_id=?",
            (project_id, user["id"]),
        ).fetchone()
        return r["role"] if r else None

    def file(self, row):
        return {
            **{
                k: row[k]
                for k in (
                    "id",
                    "project_id",
                    "name",
                    "size",
                    "sha256",
                    "uploaded_by",
                    "created_at",
                )
            },
            "download_url": f"/api/projects/{row['project_id']}/files/{row['id']}",
        }

    def message(self, c, row):
        out = {
            k: row[k]
            for k in (
                "id",
                "project_id",
                "role",
                "content",
                "status",
                "turn_id",
                "created_at",
                "error",
            )
        }
        author = (
            c.execute("SELECT * FROM users WHERE id=?", (row["author_id"],)).fetchone()
            if row["author_id"]
            else None
        )
        out["author"] = self.user(author) if author else None
        out["attachments"] = []
        for id_ in json.loads(row["attachments"]):
            f = c.execute(
                "SELECT * FROM files WHERE id=? AND project_id=?",
                (id_, row["project_id"]),
            ).fetchone()
            if f:
                out["attachments"].append(self.file(f))
        return out

    def project(self, c, row, user):
        out = dict(row)
        out.pop("created_by", None)
        out["role"] = self.project_role(c, row["id"], user)
        t = c.execute(
            "SELECT * FROM turns WHERE project_id=? AND status='running'", (row["id"],)
        ).fetchone()
        out["active_turn"] = dict(t) if t else None
        out["queued_count"] = c.execute(
            "SELECT COUNT(*) FROM turns WHERE project_id=? AND status='queued'",
            (row["id"],),
        ).fetchone()[0]
        out["permission_mode"] = "full-access"
        if row["provider"] == "bridge":
            import time

            b = c.execute(
                "SELECT * FROM bridge_state WHERE project_id=?", (row["id"],)
            ).fetchone()
            out["bridge_online"] = bool(
                b and time.time() - b["last_seen"] < 90 and b["phase"] != "offline"
            )
        return out

    def create_project(
        self,
        user,
        name,
        description="",
        provider="codex",
        model=None,
        native_session_id=None,
    ):
        id_, at = uid(), now()
        workspace = self.workspace(id_)
        workspace.mkdir(mode=0o700)
        (workspace / "uploads").mkdir(mode=0o700)
        with self.connect() as c:
            c.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    id_,
                    name,
                    description,
                    provider,
                    model,
                    native_session_id,
                    "idle",
                    user["id"],
                    at,
                    at,
                ),
            )
            c.execute("INSERT INTO members VALUES(?,?,?)", (id_, user["id"], "owner"))
            self.event(
                c,
                id_,
                "project.created",
                {"name": name, "actor": user["username"], "provider": provider},
            )
            return self.project(
                c,
                c.execute("SELECT * FROM projects WHERE id=?", (id_,)).fetchone(),
                user,
            )
