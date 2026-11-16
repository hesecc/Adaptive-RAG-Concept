"""Persistent browser accounts and sessions behind one local store interface."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from .config import Principal
from .domain import ConflictError, ForbiddenError, NotFoundError, RagError, ValidationError, identifier


COOKIE_NAME = "an_rag_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
ADMIN_USERNAME = "ragadmin"
_ADMIN_PASSWORD = "ragadmin123"
_PASSWORD_ROUNDS = 120_000


class AuthenticationError(RagError):
    code = "unauthorized"
    status_code = 401


@dataclass(frozen=True)
class Account:
    username: str
    principal: Principal
    created_at: str

    def public(self) -> dict:
        return {"username": self.username, "admin": self.principal.admin,
                "created_at": self.created_at}


def _username(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("用户名须为 3–32 位字母、数字、下划线或连字符。")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", value):
        raise ValidationError("用户名须为 3–32 位字母、数字、下划线或连字符。")
    return value


def _password(value: str) -> str:
    if not isinstance(value, str) or not 6 <= len(value) <= 128 or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationError("密码须为 6–128 位，且不能包含控制字符。")
    return value


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ROUNDS).hex()


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountStore:
    """Own account/session transactions; deleted users never reuse subject IDs."""

    def __init__(self, data_dir: Path, tenant_id: str):
        self.tenant_id = identifier(tenant_id, "browser_tenant")
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "accounts.sqlite3"
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL, admin INTEGER NOT NULL CHECK(admin IN (0,1)),
                    password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            # The fixed administrator is seeded once. Concurrent startup uses
            # the same unique username rather than creating multiple admins.
            existing = connection.execute("SELECT tenant_id FROM accounts WHERE username=?", (ADMIN_USERNAME,)).fetchone()
            if existing is None:
                self._insert(connection, ADMIN_USERNAME, _ADMIN_PASSWORD, admin=True)
            elif existing["tenant_id"] != self.tenant_id:
                raise ValidationError("browser_tenant differs from the existing account database")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(row["username"], Principal(row["tenant_id"], row["account_id"], admin=bool(row["admin"])), row["created_at"])

    def _insert(self, connection, username, password, *, admin=False):
        salt = secrets.token_bytes(16)
        created = datetime.now(timezone.utc).isoformat()
        account_id = "user-" + uuid.uuid4().hex
        connection.execute(
            "INSERT OR IGNORE INTO accounts VALUES (?,?,?,?,?,?,?)",
            (account_id, username, self.tenant_id, int(admin), salt.hex(), _password_hash(password, salt), created),
        )
        return connection.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()

    def login(self, username: str, password: str) -> tuple[str, Account]:
        try:
            username, password = _username(username), _password(password)
        except ValidationError:
            raise AuthenticationError("账号或密码不正确。") from None
        with self._connection() as connection:
            # Keep password verification and session insertion in one write
            # transaction so a concurrent deletion cannot issue a live session.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            if row is None or not hmac.compare_digest(row["password_hash"], _password_hash(password, bytes.fromhex(row["password_salt"]))):
                raise AuthenticationError("账号或密码不正确。")
            token = secrets.token_urlsafe(32)
            now = time.time()
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?,?)", (_session_hash(token), row["account_id"], now + SESSION_SECONDS))
            return token, self._account(row)

    def session(self, token: str | None) -> Account | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT accounts.* FROM accounts JOIN sessions USING(account_id) WHERE session_hash=? AND expires_at>?",
                (_session_hash(token), time.time()),
            ).fetchone()
            return self._account(row) if row else None

    def logout(self, token: str | None) -> None:
        if isinstance(token, str) and 20 <= len(token) <= 128:
            with self._connection() as connection:
                connection.execute("DELETE FROM sessions WHERE session_hash=?", (_session_hash(token),))

    def _manage(self, principal: Principal):
        if not principal.admin or principal.tenant_id != self.tenant_id:
            raise ForbiddenError("需要当前知识库的管理员身份。")

    def users(self, principal: Principal) -> list[dict]:
        self._manage(principal)
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY admin DESC, username").fetchall()
            return [self._account(row).public() for row in rows]

    def create(self, principal: Principal, username: str, password: str) -> dict:
        self._manage(principal)
        username, password = _username(username), _password(password)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise ConflictError("该账号已存在。")
            return self._account(self._insert(connection, username, password)).public()

    def delete(self, principal: Principal, username: str) -> None:
        self._manage(principal)
        username = _username(username)
        if username == ADMIN_USERNAME:
            raise ConflictError("固定管理员账号不能删除。")
        with self._connection() as connection:
            result = connection.execute("DELETE FROM accounts WHERE username=? AND admin=0", (username,))
            if not result.rowcount:
                raise NotFoundError("账号不存在。")
