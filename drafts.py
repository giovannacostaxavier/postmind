"""Saved posts ("rascunhos"), stored in the same SQLite file as the accounts.

The owner is the account email, or "device:<id>" for visitors without an account
(so their drafts survive a reload and move to their account when they sign in).
"""

import sqlite3
from datetime import datetime, timezone

from accounts import DB_PATH

MAX_MEDIA_BYTES = 20_000_000  # bigger files (long videos) are not kept in drafts


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            post_text TEXT NOT NULL,
            media BLOB,
            media_mime TEXT,
            media_name TEXT,
            media_source TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    return connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(owner: str, draft_id: int | None, post_text: str, media: dict | None) -> int:
    """Create or update a draft; returns its id."""
    if media and len(media["data"]) > MAX_MEDIA_BYTES:
        media = None
    values = (
        post_text,
        media["data"] if media else None,
        media["mime"] if media else None,
        media["name"] if media else None,
        media["source"] if media else None,
        _now(),
    )
    with _connect() as connection:
        if draft_id is not None:
            updated = connection.execute(
                "UPDATE drafts SET post_text = ?, media = ?, media_mime = ?, media_name = ?, "
                "media_source = ?, updated_at = ? WHERE id = ? AND owner = ?",
                (*values, draft_id, owner),
            )
            if updated.rowcount:
                return draft_id
        cursor = connection.execute(
            "INSERT INTO drafts (post_text, media, media_mime, media_name, media_source, updated_at, owner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*values, owner),
        )
        return cursor.lastrowid


def recent(owner: str, limit: int = 6) -> list[dict]:
    """Latest drafts (without the media bytes): [{'id', 'post_text', 'updated_at', 'has_media'}]."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, post_text, updated_at, media IS NOT NULL AS has_media FROM drafts "
            "WHERE owner = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (owner, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def load(owner: str, draft_id: int | None = None) -> dict | None:
    """One draft with its media, or the most recent one when draft_id is None."""
    query = "SELECT * FROM drafts WHERE owner = ? "
    params: tuple = (owner,)
    if draft_id is None:
        query += "ORDER BY updated_at DESC, id DESC LIMIT 1"
    else:
        query += "AND id = ?"
        params += (draft_id,)
    with _connect() as connection:
        row = connection.execute(query, params).fetchone()
    if row is None:
        return None
    media = None
    if row["media"] is not None:
        media = {
            "data": row["media"],
            "mime": row["media_mime"],
            "name": row["media_name"],
            "source": row["media_source"],
        }
    return {"id": row["id"], "post_text": row["post_text"], "media": media}


def delete(owner: str, draft_id: int) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM drafts WHERE id = ? AND owner = ?", (draft_id, owner))


def move_owner(old_owner: str, new_owner: str) -> None:
    """Give a visitor's drafts to their account after they sign in."""
    with _connect() as connection:
        connection.execute("UPDATE drafts SET owner = ? WHERE owner = ?", (new_owner, old_owner))
