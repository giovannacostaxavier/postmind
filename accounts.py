"""Email + password accounts, stored in a local SQLite file (users.db).

Passwords are never stored: only a salted scrypt hash of each one.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "users.db"
MIN_PASSWORD_LENGTH = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return connection


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)


def create_account(name: str, email: str, password: str, confirm: str) -> str | None:
    """Create a new account. Returns an error message (pt-PT), or None on success."""
    name, email = name.strip(), email.strip().lower()
    if not name:
        return "Escreve o teu nome."
    if not EMAIL_PATTERN.match(email):
        return "Esse email não parece válido."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"A palavra-passe precisa de pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    if password != confirm:
        return "As palavras-passe não coincidem."

    salt = secrets.token_bytes(16)
    with _connect() as connection:
        try:
            connection.execute(
                "INSERT INTO users (email, name, salt, password_hash) VALUES (?, ?, ?, ?)",
                (email, name, salt, _hash_password(password, salt)),
            )
        except sqlite3.IntegrityError:
            return "Já existe uma conta com esse email. Usa Entrar."
    return None


def check_login(email: str, password: str) -> dict | None:
    """Return the account ({'name', 'email'}) if the password is right, else None."""
    email = email.strip().lower()
    with _connect() as connection:
        row = connection.execute(
            "SELECT name, salt, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
    if row is None:
        return None
    name, salt, stored_hash = row
    if not hmac.compare_digest(_hash_password(password, salt), stored_hash):
        return None
    return {"name": name, "email": email}


# --- Sessions: keep an email login alive across page reloads (cookie "pm_session") ---

SESSION_DAYS = 30


def _sessions() -> sqlite3.Connection:
    connection = _connect()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    return connection


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(email: str) -> str:
    """Start a session for this account; returns the token to keep in the browser cookie."""
    token = secrets.token_urlsafe(32)
    with _sessions() as connection:
        connection.execute(
            "INSERT INTO sessions (token_hash, email, expires_at) "
            f"VALUES (?, ?, datetime('now', '+{SESSION_DAYS} days'))",
            (_token_hash(token), email.strip().lower()),
        )
    return token


def account_for_session(token: str | None) -> dict | None:
    """The account ({'name', 'email'}) behind a valid, unexpired session token."""
    if not token:
        return None
    with _sessions() as connection:
        row = connection.execute(
            "SELECT users.name, users.email FROM sessions JOIN users ON users.email = sessions.email "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > datetime('now')",
            (_token_hash(token),),
        ).fetchone()
    return {"name": row[0], "email": row[1]} if row else None


def end_session(token: str | None) -> None:
    if token:
        with _sessions() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
