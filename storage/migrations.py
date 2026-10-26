from __future__ import annotations

from typing import Protocol

from storage.schema import SCHEMA_SQL, SCHEMA_VERSION


class MigrationConnection(Protocol):
    async def execute(self, sql: str, parameters=()): ...

    async def executescript(self, sql: str): ...


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_SQL),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS user_nicknames (
            group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(group_id, user_id)
        );
        """,
    ),
)


async def apply_migrations(connection: MigrationConnection) -> int:
    cursor = await connection.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = int(row[0]) if row else 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        await connection.executescript(sql)
        await connection.execute(f"PRAGMA user_version={version}")
    return max(current, SCHEMA_VERSION)

