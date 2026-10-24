"""Opaque, revocable cookie sessions; only token hashes are stored on disk."""

import secrets
import sqlite3
import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import Problem, Store, password_hash, token_hash, uid, verify_password


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if value.count("@") != 1 or "." not in value.split("@")[-1] or any(c.isspace() for c in value):
            raise ValueError("请输入有效邮箱")
        return value


class Registration(Credentials):
    name: str = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError("昵称不能为空")
        return value.strip()


class AuthService:
    def __init__(self, store: Store):
        self.store = store
        self.dummy = password_hash("not-a-user-password")

    def register(self, request: Registration):
        hashed = password_hash(request.password)
        user_id = uid("user")
        try:
            with self.store.connection(write=True) as db:
                db.execute(
                    "INSERT INTO users VALUES(?,?,?,?,?)",
                    (user_id, request.email, request.name, hashed, int(time.time())),
                )
                self.store.audit(db, user_id, "account.registered", user_id)
        except sqlite3.IntegrityError:
            raise Problem("email_taken", "该邮箱已注册，请直接登录", 409) from None
        return self.issue(user_id)

    def login(self, request: Credentials):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (request.email,)).fetchone()
        valid = verify_password(request.password, row["password_hash"] if row else self.dummy)
        if not row or not valid:
            raise Problem("invalid_credentials", "邮箱或密码不正确", 401)
        return self.issue(row["id"])

    def issue(self, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        now = int(time.time())
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
            # Bound simultaneous browser sessions while preserving the newest ten.
            db.execute(
                "DELETE FROM auth_sessions WHERE token_hash IN (SELECT token_hash FROM auth_sessions "
                "WHERE user_id=? ORDER BY expires_at DESC LIMIT -1 OFFSET 9)",
                (user_id,),
            )
            db.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (token_hash(token), user_id, csrf, now + 86400))
            self.store.audit(db, user_id, "account.logged_in", user_id)
            user = dict(db.execute("SELECT id,email,name FROM users WHERE id=?", (user_id,)).fetchone())
        return token, {"user": user, "csrf_token": csrf}

    def authenticate(self, token):
        if not token:
            raise Problem("authentication_required", "请先登录", 401)
        with self.store.connection() as db:
            row = db.execute(
                "SELECT u.id,u.email,u.name,a.csrf FROM auth_sessions a JOIN users u "
                "ON u.id=a.user_id WHERE a.token_hash=? AND a.expires_at>?",
                (token_hash(token), int(time.time())),
            ).fetchone()
        if not row:
            raise Problem("authentication_required", "登录已过期，请重新登录", 401)
        return dict(row)

    def logout(self, token, user_id):
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token),))
            self.store.audit(db, user_id, "account.logged_out", user_id)
