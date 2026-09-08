from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SESSION_SECONDS = 12 * 60 * 60
PBKDF2_ITERATIONS = 310_000
DEFAULT_CUSTOMER_ID = "customer-001"


class AuthenticationError(ValueError):
    pass


class AuthService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def password_hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return salt.hex(), digest.hex()

    @classmethod
    def verify_password(cls, password: str, salt_hex: str, digest_hex: str) -> bool:
        _salt, actual = cls.password_hash(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(actual, digest_hex)

    def init_schema(self) -> tuple[str, str] | None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL REFERENCES customers(id),
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'customer_admin',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO customers(id, name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_CUSTOMER_ID, "Addvalue", time.time()),
            )
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return None
            username = os.environ.get("DASHBOARD_INITIAL_USERNAME", "customer").strip() or "customer"
            password = os.environ.get("DASHBOARD_INITIAL_PASSWORD") or secrets.token_urlsafe(12)
            salt, digest = self.password_hash(password)
            db.execute(
                """
                INSERT INTO users(id, customer_id, username, display_name, password_salt,
                    password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'customer_admin', ?)
                """,
                ("user-001", DEFAULT_CUSTOMER_ID, username, "Addvalue", salt, digest, time.time()),
            )
            return username, password

    def login(self, username: str, password: str) -> tuple[str, dict[str, object]]:
        username = username.strip()
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE AND enabled = 1", (username,)
            ).fetchone()
            if row is None or not self.verify_password(password, row["password_salt"], row["password_hash"]):
                raise AuthenticationError("Invalid username or password")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            now = time.time()
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, row["id"], now, now + SESSION_SECONDS),
            )
            return token, self._public_user(row)

    def user_for_token(self, token: str | None) -> dict[str, object] | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.enabled = 1
                """,
                (token_hash, time.time()),
            ).fetchone()
        return self._public_user(row) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    @staticmethod
    def _public_user(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "customer_id": row["customer_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
        }
