from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

from storage.migrations import apply_migrations
from storage.schema import SCHEMA_VERSION


class Database:
    """Serialized SQLite access with WAL and explicit transactions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("数据库尚未初始化")
        return self._connection

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.execute("PRAGMA busy_timeout=5000")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await apply_migrations(self._connection)
        await self._connection.execute(
            """
            INSERT INTO system_settings(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )
        await self._connection.execute(
            "UPDATE send_events SET status='failed' WHERE status='reserved'"
        )
        await self._connection.execute(
            "UPDATE job_runs SET status='failed' WHERE status='running'"
        )
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._lock:
            await self.connection.execute(sql, params)
            await self.connection.commit()

    async def executemany(
        self, sql: str, rows: Iterable[Sequence[Any]]
    ) -> None:
        async with self._lock:
            await self.connection.executemany(sql, rows)
            await self.connection.commit()

    async def fetchone(
        self, sql: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            return await cursor.fetchone()

    async def fetchall(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.connection.execute(sql, params)
            return list(await cursor.fetchall())

    async def scalar(
        self, sql: str, params: Sequence[Any] = (), default: Any = None
    ) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None else default

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = self.connection
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            target = await aiosqlite.connect(destination)
            try:
                await self.connection.backup(target)
            finally:
                await target.close()

