from __future__ import annotations

import importlib.util
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from storage.database import Database


@pytest.mark.asyncio
async def test_healthcheck_is_read_only(
    tmp_path: Path, monkeypatch, capsys
):
    database_path = tmp_path / "data" / "health.db"
    database = Database(database_path)
    await database.initialize()
    await database.execute(
        """
        INSERT INTO send_events(created_at, scene, group_id, status)
        VALUES (?, 'test', 1, 'reserved')
        """,
        (time.time(),),
    )
    await database.execute(
        """
        INSERT INTO job_runs(
            group_id, run_date, job_type, status, started_at
        ) VALUES (0, '2026-10-25', 'test', 'running', ?)
        """,
        (time.time(),),
    )
    await database.close()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("OWNER_QQ", "9999")
    monkeypatch.setenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backup"))

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("healthcheck_script", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert "健康检查通过" in output
    assert "机器人进程: 未运行" in output
    assert module.pid_is_running(os.getpid())

    with closing(sqlite3.connect(database_path)) as connection:
        send_status = connection.execute(
            "SELECT status FROM send_events LIMIT 1"
        ).fetchone()[0]
        job_status = connection.execute(
            "SELECT status FROM job_runs LIMIT 1"
        ).fetchone()[0]
    assert send_status == "reserved"
    assert job_status == "running"
