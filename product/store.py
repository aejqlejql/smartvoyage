"""SQLite application storage, deliberately separate from legacy supplier demo MySQL.

All monetary values are integer cents. BEGIN IMMEDIATE serializes inventory writes
across processes; every object lookup includes its owner. No real payment details
or identity documents are collected in this sandbox.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Problem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS auth_sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS auth_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 memory TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL,
 busy_until INTEGER NOT NULL DEFAULT 0, lease TEXT);
CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(user_id,created_at);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 conversation_id TEXT NOT NULL REFERENCES conversations(id), status TEXT NOT NULL,
 mode TEXT NOT NULL, trace TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS run_owner ON runs(user_id,conversation_id,created_at);
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 request TEXT NOT NULL, quote TEXT NOT NULL, version INTEGER NOT NULL,
 expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS plan_owner ON plans(user_id,created_at);
CREATE TABLE IF NOT EXISTS inventory(
 product_id TEXT NOT NULL, service_date TEXT NOT NULL, available INTEGER NOT NULL CHECK(available>=0),
 PRIMARY KEY(product_id,service_date));
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 plan_id TEXT UNIQUE NOT NULL REFERENCES plans(id), quote TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), status TEXT NOT NULL,
 expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS order_owner ON orders(user_id,created_at);
CREATE TABLE IF NOT EXISTS idempotency(
 user_id TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 order_id TEXT NOT NULL REFERENCES orders(id), PRIMARY KEY(user_id,key));
CREATE TABLE IF NOT EXISTS audit_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
 action TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS audit_owner ON audit_events(user_id,id);
CREATE TABLE IF NOT EXISTS rate_limits(
 key TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path=None):
        default = Path(__file__).resolve().parents[1] / "data" / "smartvoyage.sqlite3"
        self.path = str(path or os.getenv("SMARTVOYAGE_APP_DB", default))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db, user_id, action, object_id, details=None):
        db.execute(
            "INSERT INTO audit_events(user_id,action,object_id,details,created_at) VALUES(?,?,?,?,?)",
            (user_id, action, object_id, dumps(details or {}), int(time.time())),
        )

    def limit(self, key, maximum, seconds=60):
        now = int(time.time())
        window = now // seconds
        with self.connection(write=True) as db:
            db.execute("DELETE FROM rate_limits WHERE window < ?", (now // seconds - 2,))
            row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row and row["window"] == window and row["count"] >= maximum:
                raise Problem("rate_limited", "请求过于频繁，请稍后重试", 429)
            db.execute(
                "INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET "
                "count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,window=excluded.window",
                (key, window),
            )

    def create_conversation(self, user_id, title):
        with self.connection(write=True) as db:
            count = db.execute("SELECT count(*) FROM conversations WHERE user_id=?", (user_id,)).fetchone()[0]
            if count >= 100:
                raise Problem("conversation_limit", "最多保留100个会话")
            cid = uid("conv")
            db.execute(
                "INSERT INTO conversations(id,user_id,title,created_at) VALUES(?,?,?,?)",
                (cid, user_id, title, int(time.time())),
            )
            return {"id": cid, "title": title}

    @staticmethod
    def owned(db, table, object_id, user_id):
        if table not in {"conversations", "plans", "orders", "runs"}:
            raise ValueError("unsupported table")
        row = db.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (object_id, user_id)).fetchone()
        if row is None:
            raise Problem("not_found", "记录不存在或无权访问", 404)
        return dict(row)

    def acquire_conversation(self, cid, user_id):
        now = int(time.time())
        with self.connection(write=True) as db:
            row = self.owned(db, "conversations", cid, user_id)
            if row["busy_until"] > now:
                raise Problem("conversation_busy", "该会话正在处理上一条消息，请稍后再试", 409)
            lease = secrets.token_hex(16)
            db.execute("UPDATE conversations SET busy_until=?,lease=? WHERE id=?", (now + 180, lease, cid))
            return row, lease

    def release_conversation(self, cid, lease):
        with self.connection(write=True) as db:
            db.execute("UPDATE conversations SET busy_until=0,lease=NULL WHERE id=? AND lease=?", (cid, lease))


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password, encoded):
    _, salt, _ = encoded.split("$")
    return secrets.compare_digest(password_hash(password, salt), encoded)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
