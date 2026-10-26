from __future__ import annotations

import os
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from scripts.migration import (
    DEPLOYMENT_DOCUMENT,
    MigrationError,
    assert_bot_stopped,
    create_deployment_archive,
    create_snapshot_archive,
    import_snapshot_archive,
    inspect_database,
    sha256_file,
    verify_archive,
)
from services.memory import MemoryService
from storage.database import Database
from storage.repository import Repository


async def _create_working_database(path: Path) -> None:
    database = Database(path)
    await database.initialize()
    try:
        repository = Repository(database)
        await repository.enable_group(10001)
        memory = MemoryService(repository)
        await memory.remember_group(
            10001,
            20001,
            "测试用户",
            "user",
            "迁移后仍能搜索到蓝鱼记忆",
            8001,
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_snapshot_roundtrip_preserves_database_and_fts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    await _create_working_database(source)

    archive_path, checksum_path = create_snapshot_archive(
        project_root=tmp_path,
        database_path=source,
        output_directory=tmp_path / "exports",
    )
    assert checksum_path.is_file()
    assert checksum_path.read_text(encoding="utf-8").startswith(
        sha256_file(archive_path)
    )
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == {
            "snapshot/bluefish.db",
            "manifest.json",
            "snapshot.sha256",
        }

    verified = verify_archive(archive_path, expected_type="snapshot")
    assert verified["manifest"]["contains_secret"] is False
    assert verified["database"]["schema_version"] == 2

    target = tmp_path / "target" / "data" / "bluefish.db"
    backup_dir = tmp_path / "target" / "data" / "backups"
    old_database = Database(target)
    await old_database.initialize()
    try:
        await Repository(old_database).enable_group(20002)
    finally:
        await old_database.close()

    result = import_snapshot_archive(
        archive_path=archive_path,
        database_path=target,
        backup_directory=backup_dir,
        clear_lock=False,
    )
    assert result["backup"] is not None
    assert Path(result["backup"]).is_file()
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT group_id FROM groups ORDER BY group_id"
        ).fetchall() == [(10001,)]
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            ("蓝鱼",),
        ).fetchone()[0] == 1
    with closing(sqlite3.connect(result["backup"])) as connection:
        assert connection.execute(
            "SELECT group_id FROM groups"
        ).fetchone() == (20002,)


@pytest.mark.asyncio
async def test_snapshot_rejects_modified_database(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    await _create_working_database(source)
    archive_path, _ = create_snapshot_archive(
        project_root=tmp_path,
        database_path=source,
        output_directory=tmp_path / "exports",
    )
    modified = tmp_path / "modified.zip"
    with zipfile.ZipFile(archive_path) as original:
        with zipfile.ZipFile(modified, "w") as changed:
            for info in original.infolist():
                content = original.read(info)
                if info.filename == "snapshot/bluefish.db":
                    content = content[:200] + b"tampered-data"
                changed.writestr(info, content)
    with pytest.raises(MigrationError, match="SHA-256"):
        verify_archive(modified, expected_type="snapshot")


@pytest.mark.asyncio
async def test_failed_import_does_not_replace_existing_database(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    await _create_working_database(source)
    archive_path, _ = create_snapshot_archive(
        project_root=tmp_path,
        database_path=source,
        output_directory=tmp_path / "exports",
    )
    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(archive_path) as original:
        with zipfile.ZipFile(corrupted, "w") as changed:
            for info in original.infolist():
                content = original.read(info)
                if info.filename == "snapshot/bluefish.db":
                    content = b"not-a-sqlite-database"
                changed.writestr(info, content)

    target = tmp_path / "target" / "data" / "bluefish.db"
    database = Database(target)
    await database.initialize()
    try:
        await Repository(database).enable_group(20002)
    finally:
        await database.close()

    with pytest.raises(MigrationError, match="SHA-256"):
        import_snapshot_archive(
            archive_path=corrupted,
            database_path=target,
            backup_directory=tmp_path / "target" / "data" / "backups",
            clear_lock=False,
        )
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute(
            "SELECT group_id FROM groups"
        ).fetchone() == (20002,)


def test_running_instance_and_newer_schema_are_rejected(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "bot.lock"
    lock_path.write_text(str(os.getpid()), encoding="ascii")
    with pytest.raises(MigrationError, match="仍在运行"):
        assert_bot_stopped(lock_path)

    database_path = tmp_path / "future.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE groups(group_id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE system_settings(key TEXT PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5(content)"
        )
        connection.execute("PRAGMA user_version=999")
        connection.commit()
    with pytest.raises(MigrationError, match="高于当前程序支持"):
        inspect_database(database_path)


@pytest.mark.asyncio
async def test_deployment_archive_contains_secrets_but_excludes_runtime_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "bluefish"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "DEEPSEEK_API_KEY=test\n",
        encoding="utf-8",
    )
    (project / "bot.py").write_text("print('bot')\n", encoding="utf-8")
    (project / "logs").mkdir()
    (project / "logs" / "bluefish.log").write_text(
        "secret log\n",
        encoding="utf-8",
    )
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "bot.pyc").write_bytes(b"cache")
    (project / "data").mkdir()
    (project / "data" / "bot.lock").write_text("123", encoding="ascii")
    (project / "data" / "bluefish.db-wal").write_bytes(b"wal")
    document = project / DEPLOYMENT_DOCUMENT
    document.parent.mkdir(parents=True)
    document.write_text("# 部署\n", encoding="utf-8")
    source_database = project / "source.db"
    await _create_working_database(source_database)

    archive_path, _ = create_deployment_archive(
        project_root=project,
        database_path=source_database,
        output_directory=tmp_path / "exports",
    )
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "蓝色大肥鱼/.env" in names
    assert "蓝色大肥鱼/bot.py" in names
    assert "蓝色大肥鱼/data/bluefish.db" in names
    assert "公司电脑部署说明.md" in names
    assert "manifest.json" in names
    assert "package.sha256" in names
    assert not any("__pycache__" in name for name in names)
    assert not any("bluefish.log" in name for name in names)
    assert not any(name.endswith((".pyc", ".lock", ".db-wal")) for name in names)
    assert "蓝色大肥鱼/source.db" not in names

    verified = verify_archive(archive_path, expected_type="deployment")
    assert verified["manifest"]["contains_secret"] is True
    assert verified["manifest"]["app_version"] == "1.2.3"
