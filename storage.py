from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class WarningRecord:
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    created_at: str  # ISO-8601, kompatibel mit discord.utils.parse_time


class ModerationStore:
    """Persistiert Verwarnungen in einer lokalen SQLite-Datenbank."""

    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def add_warning(
        self,
        *,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
    ) -> WarningRecord:
        created_at = datetime.now(timezone.utc).isoformat()
        cursor = self._connection.execute(
            """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, reason, created_at),
        )
        self._connection.commit()
        return WarningRecord(
            id=cursor.lastrowid,
            guild_id=guild_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason=reason,
            created_at=created_at,
        )

    def get_warnings(self, *, guild_id: int, user_id: int) -> list[WarningRecord]:
        cursor = self._connection.execute(
            """
            SELECT id, guild_id, user_id, moderator_id, reason, created_at
            FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC
            """,
            (guild_id, user_id),
        )
        return [WarningRecord(**dict(row)) for row in cursor.fetchall()]

    def delete_warnings(self, *, guild_id: int, user_id: int) -> int:
        cursor = self._connection.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self._connection.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()
