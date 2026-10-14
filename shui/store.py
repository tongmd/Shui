"""Durable shared state. One SQLite database; each project is one paper/session."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import quote


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

    @contextlib.contextmanager
    def keep_wal_open(self):
        """Avoid checkpointing the WAL after every short-lived request connection.

        This connection belongs only to the calling application lifespan. It does
        no writes and holds no transaction; requests keep their own connections,
        including requests executed in a worker thread. Keep SQLite's FULL commit
        durability and automatic checkpoint policy unchanged.
        """
        c = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        try:
            c.execute("PRAGMA query_only=ON")
            # Opening alone is lazy: finish a read to attach the connection to WAL.
            with contextlib.closing(c.execute("SELECT rootpage FROM sqlite_schema LIMIT 1")) as cursor:
                cursor.fetchall()
            yield
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
            CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',objective TEXT NOT NULL DEFAULT '',claim TEXT NOT NULL DEFAULT '',validation TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','running','completed','failed','blocked')),result_summary TEXT NOT NULL DEFAULT '',metadata TEXT NOT NULL DEFAULT '{}',created_by TEXT REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS experiment_project ON experiments(project_id,updated_at,id);
            CREATE TABLE IF NOT EXISTS figures(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),experiment_id TEXT REFERENCES experiments(id) ON DELETE SET NULL,title TEXT NOT NULL,path TEXT NOT NULL,caption TEXT NOT NULL DEFAULT '',claim TEXT NOT NULL DEFAULT '',validation TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'figure',mime_type TEXT NOT NULL DEFAULT '',size INTEGER,sha256 TEXT,created_by TEXT REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS figure_project ON figures(project_id,updated_at,id);
            CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),experiment_id TEXT REFERENCES experiments(id) ON DELETE SET NULL,statement TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','supported','partial','refuted','unverified')),evidence TEXT NOT NULL DEFAULT '',metadata TEXT NOT NULL DEFAULT '{}',created_by TEXT REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS claim_project ON claims(project_id,updated_at,id);
            CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),experiment_id TEXT REFERENCES experiments(id) ON DELETE SET NULL,metric TEXT NOT NULL DEFAULT '',value TEXT NOT NULL DEFAULT '',unit TEXT NOT NULL DEFAULT '',model TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'observed' CHECK(status IN ('observed','supports','partial','contradicts','inconclusive')),metadata TEXT NOT NULL DEFAULT '{}',created_by TEXT REFERENCES users(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS result_project ON results(project_id,updated_at,id);
            CREATE TABLE IF NOT EXISTS delegations(id TEXT PRIMARY KEY,parent_project_id TEXT NOT NULL REFERENCES projects(id),child_project_id TEXT NOT NULL REFERENCES projects(id),parent_message_id TEXT NOT NULL REFERENCES messages(id),created_by TEXT NOT NULL REFERENCES users(id),title TEXT NOT NULL,instructions TEXT NOT NULL,context TEXT NOT NULL DEFAULT '',attachment_ids TEXT NOT NULL DEFAULT '[]',status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','completed','cancelled')),summary TEXT NOT NULL DEFAULT '',merge_turn_id TEXT REFERENCES turns(id),client_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,UNIQUE(parent_project_id,client_id));
            CREATE INDEX IF NOT EXISTS delegation_parent ON delegations(parent_project_id,created_at);
            CREATE TABLE IF NOT EXISTS delegation_assignees(delegation_id TEXT NOT NULL REFERENCES delegations(id) ON DELETE CASCADE,user_id TEXT NOT NULL REFERENCES users(id),PRIMARY KEY(delegation_id,user_id));
            CREATE TABLE IF NOT EXISTS delegation_merge_attempts(delegation_id TEXT NOT NULL REFERENCES delegations(id) ON DELETE CASCADE,turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),PRIMARY KEY(delegation_id,turn_id));
            CREATE TABLE IF NOT EXISTS collaboration_sessions(parent_project_id TEXT NOT NULL REFERENCES projects(id),assignee_id TEXT NOT NULL REFERENCES users(id),child_project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),created_at TEXT NOT NULL,PRIMARY KEY(parent_project_id,assignee_id));
            CREATE TABLE IF NOT EXISTS delegation_requests(id TEXT PRIMARY KEY,delegation_id TEXT NOT NULL REFERENCES delegations(id) ON DELETE CASCADE,parent_project_id TEXT NOT NULL REFERENCES projects(id),child_project_id TEXT NOT NULL REFERENCES projects(id),parent_message_id TEXT NOT NULL UNIQUE REFERENCES messages(id),child_turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),created_by TEXT NOT NULL REFERENCES users(id),assignee_id TEXT NOT NULL REFERENCES users(id),client_id TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,context TEXT NOT NULL DEFAULT '',attachment_ids TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,UNIQUE(parent_project_id,client_id,assignee_id));
            CREATE TABLE IF NOT EXISTS delegation_turns(delegation_id TEXT NOT NULL REFERENCES delegations(id) ON DELETE CASCADE,turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),request_id TEXT REFERENCES delegation_requests(id) ON DELETE SET NULL,PRIMARY KEY(delegation_id,turn_id));
            """)
            # Preserve unrelated legacy data problems; only reject violations introduced here.
            previous_fk_violations = {tuple(row) for row in c.execute("PRAGMA foreign_key_check")}
            # Rebuilding the legacy UNIQUE(child_project_id) table must not cascade its references.
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute("BEGIN IMMEDIATE")
            user_columns = {row[1] for row in c.execute("PRAGMA table_info(users)")}
            if "must_change_password" not in user_columns:
                c.execute(
                    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
                )
                for user in c.execute(
                    "SELECT id,username,password_hash FROM users"
                ).fetchall():
                    if verify_password(user["username"], user["password_hash"]):
                        c.execute(
                            "UPDATE users SET must_change_password=1 WHERE id=?",
                            (user["id"],),
                        )
            if "is_active" not in user_columns:
                c.execute(
                    "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
                )
            project_columns = {
                row[1] for row in c.execute("PRAGMA table_info(projects)")
            }
            for column in ("github_url", "overleaf_url"):
                if column not in project_columns:
                    c.execute(
                        f"ALTER TABLE projects ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            self._migrate_collaboration_sessions(c)
            introduced = {tuple(row) for row in c.execute("PRAGMA foreign_key_check")} - previous_fk_violations
            if introduced:
                raise sqlite3.IntegrityError("Collaboration migration introduced invalid references")
        os.chmod(self.path, 0o600)

    def _migrate_collaboration_sessions(self, c):
        unique_child = any(
            index["unique"] and not index["partial"] and [col["name"] for col in c.execute(f"PRAGMA index_info('{index['name']}')")] == ["child_project_id"]
            for index in c.execute("PRAGMA index_list(delegations)").fetchall()
        )
        if unique_child:
            schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='delegations'").fetchone()[0]
            schema = "CREATE TABLE delegations_v2 " + schema[schema.index("("):]
            schema = re.sub(r"(child_project_id\s+TEXT\s+NOT\s+NULL)\s+UNIQUE", r"\1", schema, flags=re.I)
            c.execute(schema)
            columns = ",".join(row["name"] for row in c.execute("PRAGMA table_info(delegations)"))
            c.execute(f"INSERT INTO delegations_v2(rowid,{columns}) SELECT rowid,{columns} FROM delegations")
            c.execute("DROP TABLE delegations")
            c.execute("ALTER TABLE delegations_v2 RENAME TO delegations")
            c.execute("CREATE INDEX delegation_parent ON delegations(parent_project_id,created_at)")
            # A legacy shared session stays archived. Only the latest one-person session
            # for each parent/member pair becomes the canonical reusable session.
            for row in c.execute(
                "SELECT d.*,MIN(a.user_id) AS assignee_id FROM delegations d JOIN delegation_assignees a ON a.delegation_id=d.id "
                "GROUP BY d.id HAVING COUNT(a.user_id)=1 ORDER BY d.created_at DESC,d.id DESC"
            ).fetchall():
                c.execute("INSERT OR IGNORE INTO collaboration_sessions VALUES(?,?,?,?)", (row["parent_project_id"], row["assignee_id"], row["child_project_id"], row["created_at"]))
            c.execute("UPDATE delegations SET status='cancelled' WHERE status='open' AND child_project_id NOT IN (SELECT child_project_id FROM collaboration_sessions)")
            c.execute("UPDATE turns SET status='cancelled',finished_at=? WHERE status='queued' AND project_id IN (SELECT child_project_id FROM delegations WHERE child_project_id NOT IN (SELECT child_project_id FROM collaboration_sessions))", (now(),))
            c.execute("UPDATE messages SET status='cancelled' WHERE turn_id IN (SELECT id FROM turns WHERE status='cancelled' AND project_id IN (SELECT child_project_id FROM delegations WHERE child_project_id NOT IN (SELECT child_project_id FROM collaboration_sessions)))")
            c.execute("UPDATE projects SET status='idle' WHERE status IN ('waiting','running') AND id IN (SELECT child_project_id FROM delegations WHERE child_project_id NOT IN (SELECT child_project_id FROM collaboration_sessions)) AND NOT EXISTS (SELECT 1 FROM turns WHERE turns.project_id=projects.id AND turns.status IN ('queued','running'))")
            for task in c.execute("SELECT * FROM delegations").fetchall():
                c.execute("INSERT OR IGNORE INTO delegation_turns(delegation_id,turn_id) SELECT ?,id FROM turns WHERE project_id=?", (task["id"], task["child_project_id"]))
                assignees = c.execute("SELECT user_id FROM delegation_assignees WHERE delegation_id=?", (task["id"],)).fetchall()
                turn = c.execute("SELECT id FROM turns WHERE project_id=? ORDER BY created_at,id LIMIT 1", (task["child_project_id"],)).fetchone()
                if len(assignees) == 1 and turn:
                    rid = uid()
                    c.execute("INSERT INTO delegation_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        rid, task["id"], task["parent_project_id"], task["child_project_id"], task["parent_message_id"], turn["id"], task["created_by"], assignees[0]["user_id"], task["client_id"], task["title"], task["instructions"], task["context"], task["attachment_ids"], task["created_at"],
                    ))
                    c.execute("UPDATE delegation_turns SET request_id=? WHERE delegation_id=?", (rid, task["id"]))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open_delegation_per_child ON delegations(child_project_id) WHERE status='open'")

    def workspace(self, project_id):
        # Only database-created IDs are passed here, never a client-supplied path.
        if len(project_id) != 32 or any(
            x not in "0123456789abcdef" for x in project_id
        ):
            raise ValueError("Invalid project ID")
        return self.root / "projects" / project_id

    @staticmethod
    def event(c, project_id, kind, payload):
        if kind in ("research.updated", "insights.updated"):
            # The authoritative data lives in research tables, not in every event.
            # Repeating the growing figure catalog made a small project accumulate
            # hundreds of MB of snapshots and replay them on every reconnect.
            research = payload.get("insights", payload.get("research", {}))
            summary = research.get("summary", payload.get("summary", {}))
            compact = {"refresh": True}
            if isinstance(summary, dict):
                compact["summary"] = {
                    key: value[:4000] if isinstance(value, str) else value
                    for key, value in summary.items()
                    if key in {"experiment_count", "figure_count", "claim_count", "validation_count", "result_count", "result_summary", "status", "updated_at"}
                    and isinstance(value, (str, int, float, type(None)))
                }
            if isinstance(payload.get("actor"), str):
                compact["actor"] = payload["actor"][:80]
            payload = compact
        cur = c.execute(
            "INSERT INTO events(project_id,type,payload,created_at) VALUES(?,?,?,?)",
            (project_id, kind, json.dumps(payload, ensure_ascii=False), now()),
        )
        return cur.lastrowid

    @staticmethod
    def read_events(c, project_id, after=0, limit=200, activity=False):
        # Project legacy snapshots to an invalidation before fetching the payload.
        # SQLite can then skip their overflow pages instead of reading/parsing MB
        # only to discard them in Python. Keep stored history and cursor IDs intact.
        activity_filter = " AND type NOT IN ('agent.delta','presence')" if activity else ""
        direction = "DESC" if activity else "ASC"
        rows = c.execute(
            "SELECT id,project_id,type,CASE WHEN type IN ('research.updated','insights.updated') "
            "THEN '{\"refresh\":true}' ELSE payload END AS payload,created_at FROM events "
            f"WHERE project_id=? AND id>?{activity_filter} ORDER BY id {direction} LIMIT ?",
            (project_id, max(0, after), limit),
        ).fetchall()
        if activity:
            rows.reverse()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    @staticmethod
    def user(row):
        return {
            **{k: row[k] for k in ("id", "username", "display_name", "role")},
            "must_change_password": bool(row["must_change_password"]),
            "is_active": bool(row["is_active"]),
        }

    def create_user(
        self,
        username,
        display_name,
        password,
        role="member",
        must_change_password=False,
    ):
        import re

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,39}", username):
            raise ValueError(
                "Username must be 2–40 letters, digits, dots, underscores or hyphens"
            )
        if must_change_password:
            password = username
        elif not isinstance(password, str) or not 8 <= len(password) <= 256:
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
                "INSERT INTO users(id,username,display_name,password_hash,role,created_at,must_change_password) VALUES(?,?,?,?,?,?,?)",
                (
                    id_,
                    username,
                    display_name,
                    password_hash(password),
                    role,
                    now(),
                    int(must_change_password),
                ),
            )
            return self.user(
                c.execute("SELECT * FROM users WHERE id=?", (id_,)).fetchone()
            )

    def collaboration_session(self, c, child_project_id):
        return c.execute("SELECT * FROM collaboration_sessions WHERE child_project_id=?", (child_project_id,)).fetchone()

    def delegation_for_child(self, c, child_project_id):
        return c.execute(
            "SELECT * FROM delegations WHERE child_project_id=? ORDER BY (status='open') DESC,created_at DESC,id DESC LIMIT 1", (child_project_id,)
        ).fetchone()

    def delegation_for_turn(self, c, turn_id):
        return c.execute("SELECT d.* FROM delegations d JOIN delegation_turns t ON t.delegation_id=d.id WHERE t.turn_id=?", (turn_id,)).fetchone()

    def delegation_request_for_turn(self, c, turn_id):
        return c.execute("SELECT r.* FROM delegation_requests r JOIN delegation_turns t ON t.request_id=r.id WHERE t.turn_id=?", (turn_id,)).fetchone()

    def project_role(self, c, project_id, user):
        current = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if not current or not current["is_active"]:
            return None
        if not c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            return None
        session = self.collaboration_session(c, project_id)
        task = self.delegation_for_child(c, project_id)
        if session or task:
            parent = session["parent_project_id"] if session else task["parent_project_id"]
            role = self.project_role(c, parent, current)
            if not role:
                return None
            if not session or not task or task["status"] != "open":
                return "viewer"
            if role == "owner":
                return "owner"
            if role == "editor" and (session["assignee_id"] == current["id"] or self.can_manage_delegation(c, task, current)):
                return "editor"
            return "viewer"
        if current["role"] == "owner":
            return "owner"
        r = c.execute("SELECT role FROM members WHERE project_id=? AND user_id=?", (project_id, current["id"])).fetchone()
        return r["role"] if r else None

    def can_manage_delegation(self, c, row, user):
        role = self.project_role(c, row["parent_project_id"], user)
        return bool(role == "owner" or (role == "editor" and (
            row["created_by"] == user["id"] or c.execute(
                "SELECT 1 FROM delegation_assignees WHERE delegation_id=? AND user_id=?", (row["id"], user["id"])
            ).fetchone() or c.execute(
                "SELECT 1 FROM delegation_requests WHERE delegation_id=? AND created_by=?", (row["id"], user["id"])
            ).fetchone()
        )))

    def project_members(self, c, project_id):
        session = self.collaboration_session(c, project_id)
        task = self.delegation_for_child(c, project_id)
        parent = session["parent_project_id"] if session else task["parent_project_id"] if task else project_id
        return [dict(row) for row in c.execute(
            "SELECT u.id,u.username,u.display_name,m.role FROM members m "
            "JOIN users u ON u.id=m.user_id WHERE m.project_id=? AND u.is_active=1 "
            "ORDER BY u.display_name,u.id", (parent,)
        )]

    def delegation(self, c, row, user=None):
        out = dict(row)
        out.pop("client_id", None)
        out["attachment_ids"] = json.loads(row["attachment_ids"])
        out["attachments"] = [self.file(f) for fid in out["attachment_ids"] if (f := c.execute(
            "SELECT * FROM files WHERE id=? AND project_id=?", (fid, row["parent_project_id"])
        ).fetchone())]
        creator = c.execute("SELECT * FROM users WHERE id=?", (row["created_by"],)).fetchone()
        out["creator"] = self.user(creator) if creator else None
        out["creator_ids"] = sorted({row["created_by"]} | {r[0] for r in c.execute("SELECT created_by FROM delegation_requests WHERE delegation_id=?", (row["id"],))})
        out["request_count"] = max(1, c.execute("SELECT COUNT(*) FROM delegation_requests WHERE delegation_id=?", (row["id"],)).fetchone()[0])
        out["assignees"] = [self.user(u) for u in c.execute(
            "SELECT u.* FROM delegation_assignees a JOIN users u ON u.id=a.user_id "
            "WHERE a.delegation_id=? ORDER BY u.display_name,u.id", (row["id"],)
        )]
        assignee = c.execute("SELECT u.* FROM collaboration_sessions s JOIN users u ON u.id=s.assignee_id WHERE s.child_project_id=?", (row["child_project_id"],)).fetchone()
        out["session_assignee"] = self.user(assignee) if assignee else None
        parent = c.execute("SELECT name FROM projects WHERE id=?", (row["parent_project_id"],)).fetchone()
        child = c.execute("SELECT status FROM projects WHERE id=?", (row["child_project_id"],)).fetchone()
        out["parent_project_name"] = parent["name"] if parent else ""
        out["session_status"] = child["status"] if child else "idle"
        active = c.execute("SELECT t.* FROM turns t JOIN delegation_turns d ON d.turn_id=t.id WHERE d.delegation_id=? AND t.status='running'", (row["id"],)).fetchone()
        out["active_turn"] = dict(active) if active else None
        out["queued_count"] = c.execute("SELECT COUNT(*) FROM turns t JOIN delegation_turns d ON d.turn_id=t.id WHERE d.delegation_id=? AND t.status='queued'", (row["id"],)).fetchone()[0]
        merged = c.execute("SELECT * FROM turns WHERE id=?", (row["merge_turn_id"],)).fetchone() if row["merge_turn_id"] else None
        out["merge_turn"] = dict(merged) if merged else None
        if user is not None:
            out["can_manage"] = self.can_manage_delegation(c, row, user)
        return out

    def delegation_changed(self, c, child_project_id, delegation_id=None):
        row = c.execute("SELECT * FROM delegations WHERE id=? AND child_project_id=?", (delegation_id, child_project_id)).fetchone() if delegation_id else self.delegation_for_child(c, child_project_id)
        if not row:
            return None
        c.execute("UPDATE delegations SET updated_at=? WHERE id=?", (now(), row["id"]))
        row = c.execute("SELECT * FROM delegations WHERE id=?", (row["id"],)).fetchone()
        out = self.delegation(c, row)
        self.event(c, row["parent_project_id"], "delegation.updated", {"delegation": out})
        self.event(c, child_project_id, "delegation.updated", {"delegation": out})
        return out

    def enqueue_turn(self, c, project_id, user, content, attachments, client_id, retry_of=None, emit=True):
        """Insert a turn in the caller's transaction; bind child turns to their exact task/brief."""
        tid, mid, aid, at = uid(), uid(), uid(), now()
        c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            mid, project_id, "user", content, user["id"], "queued", tid,
            json.dumps(attachments), at, None, client_id,
        ))
        c.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            aid, project_id, "assistant", "", None, "queued", tid, "[]", now(), None, None,
        ))
        c.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?)", (
            tid, project_id, mid, aid, user["id"], "queued", at, None, None, None,
        ))
        task = self.delegation_for_child(c, project_id)
        if task:
            request = self.delegation_request_for_turn(c, retry_of) if retry_of else c.execute("SELECT * FROM delegation_requests WHERE delegation_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (task["id"],)).fetchone()
            c.execute("INSERT INTO delegation_turns VALUES(?,?,?)", (task["id"], tid, request["id"] if request else None))
        c.execute("UPDATE projects SET status=CASE WHEN status='running' THEN status ELSE 'waiting' END,updated_at=? WHERE id=?", (at, project_id))
        turn = dict(c.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone())
        if emit:
            return self.publish_enqueued_turn(c, turn, retry_of)
        return {"message": self.message(c, c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()), "turn": turn}

    def publish_enqueued_turn(self, c, turn, retry_of=None):
        """Publish only after the caller has attached any task metadata in the transaction."""
        project_id = turn["project_id"]
        message = self.message(c, c.execute("SELECT * FROM messages WHERE id=?", (turn["user_message_id"],)).fetchone())
        assistant = self.message(c, c.execute("SELECT * FROM messages WHERE id=?", (turn["assistant_message_id"],)).fetchone())
        self.event(c, project_id, "message.created", {"message": message})
        self.event(c, project_id, "message.created", {"message": assistant})
        self.event(c, project_id, "turn.updated", {"turn": turn, "retry_of": retry_of})
        task = self.delegation_for_turn(c, turn["id"])
        if task:
            self.delegation_changed(c, project_id, task["id"])
        return {"message": message, "turn": turn}

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
        bound = self.delegation_for_turn(c, row["turn_id"]) if row["turn_id"] else None
        if bound:
            out["delegation_id"] = bound["id"]
        out["attachments"] = []
        for id_ in json.loads(row["attachments"]):
            f = c.execute(
                "SELECT * FROM files WHERE id=? AND project_id=?",
                (id_, row["project_id"]),
            ).fetchone()
            if f:
                out["attachments"].append(self.file(f))
        request = c.execute("SELECT * FROM delegation_requests WHERE parent_message_id=?", (row["id"],)).fetchone()
        task = c.execute("SELECT * FROM delegations WHERE id=?", (request["delegation_id"],)).fetchone() if request else c.execute("SELECT * FROM delegations WHERE parent_message_id=?", (row["id"],)).fetchone()
        if not request and row["role"] == "user" and row["turn_id"]:
            request = c.execute("SELECT * FROM delegation_requests WHERE child_turn_id=?", (row["turn_id"],)).fetchone()
            if request:
                for fid in json.loads(request["attachment_ids"]):
                    reference = c.execute("SELECT * FROM files WHERE id=? AND project_id=?", (fid, request["parent_project_id"])).fetchone()
                    if reference:
                        out["attachments"].append(self.file(reference))
        if request:
            out["delegation_request"] = dict(request)
            out["delegation_request"]["attachment_ids"] = json.loads(request["attachment_ids"])
        if task:
            out["delegation"] = self.delegation(c, task)
        if row["role"] == "user" and row["turn_id"]:
            merged = c.execute(
                "SELECT d.* FROM delegations d JOIN delegation_merge_attempts a ON a.delegation_id=d.id WHERE a.turn_id=?",
                (row["turn_id"],),
            ).fetchone()
            if merged:
                out["merge_delegation"] = self.delegation(c, merged)
        return out

    @staticmethod
    def _json(value, fallback):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return fallback
        return parsed

    def experiment(self, row):
        metadata = self._json(row["metadata"], {})
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "title": row["name"],
            "description": row["description"],
            "objective": row["objective"],
            "claim": row["claim"],
            "validation": row["validation"],
            "status": row["status"],
            "result_summary": row["result_summary"],
            "metadata": metadata,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def figure(self, row):
        out = {
            "id": row["id"],
            "project_id": row["project_id"],
            "experiment_id": row["experiment_id"],
            "title": row["title"],
            "name": row["title"],
            "path": row["path"],
            "caption": row["caption"],
            "claim": row["claim"],
            "validation": row["validation"],
            "purpose": row["claim"] or row["validation"],
            "kind": row["kind"],
            "mime_type": row["mime_type"],
            "size": row["size"],
            "sha256": row["sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        out["download_url"] = (
            f"/api/projects/{row['project_id']}/artifact?path="
            f"{quote(row['path'], safe='/')}&inline=1"
        )
        out["preview_url"] = out["download_url"]
        return out

    def claim(self, row):
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "experiment_id": row["experiment_id"],
            "statement": row["statement"],
            "status": row["status"],
            "evidence": row["evidence"],
            "metadata": self._json(row["metadata"], {}),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def result(self, row):
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "experiment_id": row["experiment_id"],
            "metric": row["metric"],
            "value": row["value"],
            "unit": row["unit"],
            "model": row["model"],
            "summary": row["summary"],
            "result": row["summary"] or row["value"],
            "status": row["status"],
            "metadata": self._json(row["metadata"], {}),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def research(self, c, project_id):
        experiments = [
            self.experiment(row)
            for row in c.execute(
                "SELECT * FROM experiments WHERE project_id=? ORDER BY updated_at DESC,id DESC",
                (project_id,),
            ).fetchall()
        ]
        figures = [
            self.figure(row)
            for row in c.execute(
                "SELECT * FROM figures WHERE project_id=? ORDER BY updated_at DESC,id DESC",
                (project_id,),
            ).fetchall()
        ]
        claims = [
            self.claim(row)
            for row in c.execute(
                "SELECT * FROM claims WHERE project_id=? ORDER BY updated_at DESC,id DESC",
                (project_id,),
            ).fetchall()
        ]
        results = [
            self.result(row)
            for row in c.execute(
                "SELECT * FROM results WHERE project_id=? ORDER BY updated_at DESC,id DESC",
                (project_id,),
            ).fetchall()
        ]
        for experiment in experiments:
            eid = experiment["id"]
            experiment["figure_ids"] = [
                figure["id"] for figure in figures if figure["experiment_id"] == eid
            ]
            linked = [result for result in results if result["experiment_id"] == eid]
            experiment["results"] = linked
            experiment["metric"] = linked[0]["metric"] if linked else ""
            experiment["result"] = (
                (linked[0]["summary"] or linked[0]["value"])
                if linked
                else experiment["result_summary"]
            )
            experiment["findings"] = [
                result["summary"] for result in linked if result["summary"]
            ]
        updated = [
            item["updated_at"]
            for group in (experiments, figures, claims, results)
            for item in group
        ]
        summaries = [result["summary"] for result in results if result["summary"]]
        statuses = {experiment["status"] for experiment in experiments}
        aggregate_status = (
            "completed"
            if statuses and statuses <= {"completed"}
            else (
                "running"
                if "running" in statuses
                else ("planned" if statuses else "empty")
            )
        )
        return {
            "summary": {
                "experiment_count": len(experiments),
                "figure_count": len(figures),
                "claim_count": len(claims),
                "validation_count": sum(
                    1
                    for item in claims
                    if item["status"] in ("supported", "partial", "refuted")
                ),
                "result_count": len(results),
                "result_summary": summaries[0] if summaries else "",
                "status": aggregate_status,
                "updated_at": max(updated) if updated else None,
            },
            "experiments": experiments,
            "figures": figures,
            "claims": claims,
            "results": results,
        }

    @staticmethod
    def _manifest_text(item, key, limit, default=""):
        value = item.get(key, default)
        if value is None:
            return default
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"Invalid research manifest field: {key}")
        return value.strip()

    @staticmethod
    def _stable_research_id(kind, project_id, identity):
        digest = hashlib.sha256(
            f"{kind}\0{project_id}\0{identity}".encode("utf-8")
        ).hexdigest()
        return digest[:32]

    def _manifest_figure(self, project_id, raw_path):
        if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 500:
            raise ValueError("Invalid figure path")
        if "\\" in raw_path or "\x00" in raw_path:
            raise ValueError("Invalid figure path")
        path = Path(raw_path)
        forbidden = {
            ".git", ".env", ".ssh", ".codex", ".claude", ".shui",
            "auth.json", "credentials.json", "credentials", "id_rsa",
            "id_ed25519",
        }
        if (
            path.is_absolute()
            or ".." in path.parts
            or any(
                part in forbidden
                or part.startswith(".env")
                or part.startswith(".")
                for part in path.parts
            )
        ):
            raise ValueError("Invalid figure path")
        root = self.workspace(project_id)
        if root.is_symlink() or (self.root / "projects").is_symlink():
            raise ValueError("Invalid project workspace")
        target = root
        for part in path.parts:
            if part in ("", "."):
                continue
            target = target / part
            if target.is_symlink():
                raise ValueError("Figure paths cannot use symlinks")
        if not target.resolve().is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError("Figure must be a file in the project workspace")
        suffix = target.suffix.lower()
        media = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
        }
        if suffix not in media or target.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("Figure must be an image, SVG, or PDF under 100 MiB")
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return target, media[suffix], digest.hexdigest()

    def sync_research_manifest(self, project_id, created_by):
        manifest = self.workspace(project_id) / "research.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1_000_000:
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(payload, dict) or set(payload) - {
            "experiments", "figures", "claims", "results"
        }:
            return None
        groups = {}
        for key in ("experiments", "figures", "claims", "results"):
            value = payload.get(key, [])
            if not isinstance(value, list) or len(value) > 100 or not all(
                isinstance(item, dict) for item in value
            ):
                return None
            groups[key] = value

        try:
            experiment_ids = {}
            experiments = []
            for item in groups["experiments"]:
                name = self._manifest_text(item, "name", 180) or self._manifest_text(item, "title", 180)
                if not name:
                    raise ValueError("Experiment name is required")
                supplied = item.get("id")
                if supplied is not None and (
                    not isinstance(supplied, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", supplied)
                ):
                    raise ValueError("Invalid experiment id")
                identity = self._manifest_text(item, "key", 180) or name.casefold()
                eid = supplied or self._stable_research_id("experiment", project_id, identity)
                experiment_ids[identity] = eid
                experiment_ids[name] = eid
                experiment_ids[identity.casefold()] = eid
                experiment_ids[name.casefold()] = eid
                status = self._manifest_text(item, "status", 30, "planned")
                if status not in ("planned", "running", "completed", "failed", "blocked"):
                    raise ValueError("Invalid experiment status")
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 20000:
                    raise ValueError("Invalid experiment metadata")
                experiments.append((
                    eid, project_id, name,
                    self._manifest_text(item, "description", 4000),
                    self._manifest_text(item, "objective", 4000),
                    self._manifest_text(item, "claim", 4000),
                    self._manifest_text(item, "validation", 4000),
                    status,
                    self._manifest_text(item, "result_summary", 4000) or self._manifest_text(item, "result", 4000),
                    json.dumps(metadata, ensure_ascii=False), created_by,
                ))

            def linked_experiment(item):
                reference = item.get("experiment_id")
                if reference is None:
                    reference = item.get("experiment")
                if reference is None or reference == "":
                    return None
                if not isinstance(reference, str) or len(reference) > 180:
                    raise ValueError("Invalid experiment reference")
                return experiment_ids.get(reference, experiment_ids.get(reference.casefold(), reference))

            figures = []
            for item in groups["figures"]:
                path_text = self._manifest_text(item, "path", 500)
                target, media, digest = self._manifest_figure(project_id, path_text)
                title = self._manifest_text(item, "title", 180) or target.name
                identity = self._manifest_text(item, "key", 500) or path_text
                supplied = item.get("id")
                if supplied is not None and (
                    not isinstance(supplied, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", supplied)
                ):
                    raise ValueError("Invalid figure id")
                eid = linked_experiment(item)
                figures.append((
                    supplied or self._stable_research_id("figure", project_id, identity),
                    project_id, eid, title, path_text,
                    self._manifest_text(item, "caption", 4000),
                    self._manifest_text(item, "claim", 4000) or self._manifest_text(item, "purpose", 4000),
                    self._manifest_text(item, "validation", 4000),
                    self._manifest_text(item, "kind", 60, "figure"),
                    media, target.stat().st_size, digest, created_by,
                ))

            claims = []
            for item in groups["claims"]:
                statement = self._manifest_text(item, "statement", 6000)
                if not statement:
                    raise ValueError("Claim statement is required")
                status = self._manifest_text(item, "status", 30, "proposed")
                if status not in ("proposed", "supported", "partial", "refuted", "unverified"):
                    raise ValueError("Invalid claim status")
                identity = self._manifest_text(item, "key", 500) or statement.casefold()
                supplied = item.get("id")
                if supplied is not None and (
                    not isinstance(supplied, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", supplied)
                ):
                    raise ValueError("Invalid claim id")
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 20000:
                    raise ValueError("Invalid claim metadata")
                claims.append((
                    supplied or self._stable_research_id("claim", project_id, identity),
                    project_id, linked_experiment(item), statement, status,
                    self._manifest_text(item, "evidence", 6000),
                    json.dumps(metadata, ensure_ascii=False), created_by,
                ))

            results = []
            for item in groups["results"]:
                status = self._manifest_text(item, "status", 30, "observed")
                if status not in ("observed", "supports", "partial", "contradicts", "inconclusive"):
                    raise ValueError("Invalid result status")
                metric = self._manifest_text(item, "metric", 180)
                model = self._manifest_text(item, "model", 180)
                value = self._manifest_text(item, "value", 500)
                summary = self._manifest_text(item, "summary", 4000) or self._manifest_text(item, "result", 4000)
                identity = self._manifest_text(item, "key", 500) or "|".join(
                    (
                        str(item.get("experiment_id", item.get("experiment", ""))),
                        metric.casefold(),
                        model.casefold(),
                        self._manifest_text(item, "unit", 80).casefold(),
                    )
                )
                supplied = item.get("id")
                if supplied is not None and (
                    not isinstance(supplied, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", supplied)
                ):
                    raise ValueError("Invalid result id")
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 20000:
                    raise ValueError("Invalid result metadata")
                results.append((
                    supplied or self._stable_research_id("result", project_id, identity),
                    project_id, linked_experiment(item), metric, value,
                    self._manifest_text(item, "unit", 80), model, summary, status,
                    json.dumps(metadata, ensure_ascii=False), created_by,
                ))
        except (ValueError, TypeError):
            return None

        tables = {
            "experiments": [row[0] for row in experiments],
            "figures": [row[0] for row in figures],
            "claims": [row[0] for row in claims],
            "results": [row[0] for row in results],
        }
        with self.connect() as c:
            if not c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                return None
            existing_experiments = {
                row["id"]
                for row in c.execute(
                    "SELECT id FROM experiments WHERE project_id=?", (project_id,)
                )
            }
            valid_experiments = existing_experiments | set(tables["experiments"])
            if any(
                row[2] and row[2] not in valid_experiments
                for row in figures + claims + results
            ):
                return None
            for table, ids in tables.items():
                if any(
                    c.execute(
                        f"SELECT 1 FROM {table} WHERE id=? AND project_id!=?",
                        (item_id, project_id),
                    ).fetchone()
                    for item_id in ids
                ):
                    return None

        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in experiments:
                c.execute(
                    "INSERT INTO experiments(id,project_id,name,description,objective,claim,validation,status,result_summary,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,description=excluded.description,objective=excluded.objective,claim=excluded.claim,validation=excluded.validation,status=excluded.status,result_summary=excluded.result_summary,metadata=excluded.metadata,updated_at=excluded.updated_at "
                    "WHERE experiments.project_id=excluded.project_id",
                    (*row, now(), now()),
                )
            for row in figures:
                existing = c.execute(
                    "SELECT id FROM figures WHERE project_id=? AND path=?",
                    (project_id, row[4]),
                ).fetchone()
                if existing and existing["id"] != row[0]:
                    row = (existing["id"], *row[1:])
                c.execute(
                    "INSERT INTO figures(id,project_id,experiment_id,title,path,caption,claim,validation,kind,mime_type,size,sha256,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,title=excluded.title,path=excluded.path,caption=excluded.caption,claim=excluded.claim,validation=excluded.validation,kind=excluded.kind,mime_type=excluded.mime_type,size=excluded.size,sha256=excluded.sha256,updated_at=excluded.updated_at WHERE figures.project_id=excluded.project_id",
                    (*row, now(), now()),
                )
            for row in claims:
                c.execute(
                    "INSERT INTO claims(id,project_id,experiment_id,statement,status,evidence,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,statement=excluded.statement,status=excluded.status,evidence=excluded.evidence,metadata=excluded.metadata,updated_at=excluded.updated_at WHERE claims.project_id=excluded.project_id",
                    (*row, now(), now()),
                )
            for row in results:
                c.execute(
                    "INSERT INTO results(id,project_id,experiment_id,metric,value,unit,model,summary,status,metadata,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET experiment_id=excluded.experiment_id,metric=excluded.metric,value=excluded.value,unit=excluded.unit,model=excluded.model,summary=excluded.summary,status=excluded.status,metadata=excluded.metadata,updated_at=excluded.updated_at WHERE results.project_id=excluded.project_id",
                    (*row, now(), now()),
                )
            insights = self.research(c, project_id)
            self.event(c, project_id, "research.updated", {"insights": insights})
            return insights

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
        task = self.delegation_for_child(c, row["id"])
        out["parent_project_id"] = task["parent_project_id"] if task else None
        if task:
            out["delegation"] = self.delegation(c, task, user)
            out["parent_project_name"] = out["delegation"]["parent_project_name"]
            out["parent_role"] = self.project_role(c, task["parent_project_id"], user)
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
        github_url="",
        overleaf_url="",
    ):
        id_, at = uid(), now()
        workspace = self.workspace(id_)
        workspace.mkdir(mode=0o700)
        (workspace / "uploads").mkdir(mode=0o700)
        with self.connect() as c:
            c.execute(
                "INSERT INTO projects(id,name,description,provider,model,native_session_id,status,created_by,created_at,updated_at,github_url,overleaf_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    github_url,
                    overleaf_url,
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
