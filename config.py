from __future__ import annotations

import os
from dataclasses import dataclass


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    """Zentrale Konfiguration des Bots, aus Umgebungsvariablen geladen."""

    token: str
    guild_id: int | None = None
    log_channel_id: int | None = None
    database_path: str = "moderation.db"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("DISCORD_TOKEN") or os.environ.get("BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "Kein Discord-Token gefunden. Setze die Umgebungsvariable "
                "DISCORD_TOKEN (oder BOT_TOKEN) in Railway unter Variables."
            )
        return cls(
            token=token,
            guild_id=_int_or_none(os.environ.get("GUILD_ID")),
            log_channel_id=_int_or_none(os.environ.get("LOG_CHANNEL_ID")),
            database_path=os.environ.get("DATABASE_PATH", "moderation.db"),
        )
