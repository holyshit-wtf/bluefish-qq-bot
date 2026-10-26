from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.config import ConfigError, Settings, load_settings
from core.instance_lock import InstanceAlreadyRunningError, InstanceLock


def test_missing_secrets_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        load_settings(
            tmp_path,
            {
                "OWNER_QQ": "123",
                "NAPCAT_WS_URL": "ws://127.0.0.1:3001",
            },
        )


def test_settings_paths_are_absolute(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path,
        deepseek_api_key="key",
        owner_qq=1,
        napcat_ws_url="ws://127.0.0.1:3001",
    )
    assert settings.database_path == tmp_path / "data" / "bluefish.db"
    assert settings.timezone_name == "Asia/Shanghai"


def test_invalid_model_price_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="单价"):
        Settings(
            project_root=tmp_path,
            deepseek_api_key="key",
            owner_qq=1,
            napcat_ws_url="ws://127.0.0.1:3001",
            deepseek_input_price_per_million=-1,
        ).validate_secrets()


def test_send_limits_are_configurable_and_default_to_twenty(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        tmp_path,
        {
            "DEEPSEEK_API_KEY": "key",
            "OWNER_QQ": "123",
            "NAPCAT_WS_URL": "ws://127.0.0.1:3001",
        },
    )
    assert settings.send_limit_per_minute == 20
    assert settings.send_limit_per_hour == 30
    assert settings.send_limit_per_day == 200
    assert settings.deepseek_search_enabled is True
    assert settings.deepseek_search_timeout_seconds == 8
    assert settings.deepseek_search_price_cny_per_request == 0.01
    assert settings.group_history_retention == 400
    assert settings.group_context_messages == 80
    assert settings.group_context_fts_limit == 16


def test_search_config_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="DEEPSEEK_SEARCH_ENABLED"):
        load_settings(
            tmp_path,
            {
                "DEEPSEEK_API_KEY": "key",
                "OWNER_QQ": "123",
                "NAPCAT_WS_URL": "ws://127.0.0.1:3001",
                "DEEPSEEK_SEARCH_ENABLED": "maybe",
            },
        )


def test_group_memory_config_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="GROUP_HISTORY_RETENTION"):
        load_settings(
            tmp_path,
            {
                "DEEPSEEK_API_KEY": "key",
                "OWNER_QQ": "123",
                "NAPCAT_WS_URL": "ws://127.0.0.1:3001",
                "GROUP_HISTORY_RETENTION": "40",
                "GROUP_CONTEXT_MESSAGES": "80",
            },
        )


def test_single_instance_lock(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "bot.lock")
    second = InstanceLock(tmp_path / "bot.lock")
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@pytest.mark.asyncio
async def test_database_schema_version(settings: Settings) -> None:
    from storage.database import Database

    database = Database(settings.database_path)
    await database.initialize()
    try:
        version = await database.scalar("PRAGMA user_version")
        assert version == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_v1_database_migrates_nickname_table(settings: Settings) -> None:
    from storage.database import Database

    database = Database(settings.database_path)
    await database.initialize()
    await database.close()
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("DROP TABLE user_nicknames")
        connection.execute("PRAGMA user_version=1")
        connection.commit()

    migrated = Database(settings.database_path)
    await migrated.initialize()
    try:
        version = await migrated.scalar("PRAGMA user_version")
        table = await migrated.scalar(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='user_nicknames'"
        )
        assert version == 2
        assert table == 1
    finally:
        await migrated.close()

