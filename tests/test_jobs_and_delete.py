from __future__ import annotations

import time
from datetime import datetime

import pytest

from jobs.scheduler import Scheduler
from services.memory import MemoryService
from services.safety import fts_query
from storage.repository import anonymous_user_key


@pytest.mark.asyncio
async def test_daily_distillation_is_idempotent(
    settings, repository, fake_model
):
    await repository.enable_group(10001)
    memory = MemoryService(repository)
    start = datetime(2026, 10, 25, 9, 0, tzinfo=settings.timezone).timestamp()
    for index, content in enumerate(("我爱喝茶", "茶很好喝", "明天继续喝茶")):
        await memory.remember_group(
            10001,
            20001,
            "甲",
            "user",
            content,
            8000 + index,
            created_at=start + index,
        )
    user_key = anonymous_user_key(10001, 20001)
    fake_model.extract_payload = {
        "memes": [
            {"text": "群里流行喝茶", "evidence_message_ids": [1]},
            {"text": "群里流行喝茶", "evidence_message_ids": [2]},
        ],
        "profiles": [
            {
                "user_key": user_key,
                "summary": "喜欢喝茶",
                "preferences": "茶",
                "interaction_style": "简短",
            }
        ],
    }
    scheduler = Scheduler(settings, repository, fake_model)  # type: ignore[arg-type]
    noon = datetime(2026, 10, 25, 12, 0, tzinfo=settings.timezone)
    await scheduler.tick(noon)
    job = await repository.get_job(10001, "2026-10-25", "distill")
    assert job["status"] == "success"
    assert len(await repository.list_memes(10001)) == 1
    assert await repository.get_profile(10001, 20001)
    calls = len(fake_model.extract_calls)
    await scheduler.tick(noon)
    assert len(fake_model.extract_calls) == calls


@pytest.mark.asyncio
async def test_daily_distillation_skips_group_without_messages(
    settings, repository, fake_model
):
    await repository.enable_group(10001)
    scheduler = Scheduler(settings, repository, fake_model)  # type: ignore[arg-type]
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 0, tzinfo=settings.timezone)
    )
    job = await repository.get_job(10001, "2026-10-25", "distill")
    assert job["status"] == "skipped"
    assert not fake_model.extract_calls


@pytest.mark.asyncio
async def test_backup_is_openable_and_retention(
    settings, repository
):
    scheduler = Scheduler(settings, repository, object())  # type: ignore[arg-type]
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 10, tzinfo=settings.timezone)
    )
    backups = list(settings.backup_dir.glob("*.db"))
    assert len(backups) == 1
    job = await repository.get_job(0, "2026-10-25", "backup")
    assert job["status"] == "success"
    for index in range(9):
        path = settings.backup_dir / f"bluefish-extra-{index}.db"
        path.write_bytes(b"test")
    scheduler._retain_backups(7)
    assert len(list(settings.backup_dir.glob("bluefish-*.db"))) == 7


@pytest.mark.asyncio
async def test_failed_distillation_retries_on_next_day(
    settings, repository, fake_model
):
    await repository.enable_group(10001)
    memory = MemoryService(repository)
    start = datetime(2026, 10, 25, 9, 0, tzinfo=settings.timezone).timestamp()
    for index in range(3):
        await memory.remember_group(
            10001,
            20001,
            "甲",
            "user",
            "重复的有效消息",
            9000 + index,
            created_at=start + index,
        )
    user_key = anonymous_user_key(10001, 20001)
    fake_model.extract_payload = {
        "memes": [],
        "profiles": [
            {
                "user_key": user_key,
                "summary": "摘要",
                "preferences": "偏好",
                "interaction_style": "风格",
            }
        ],
    }
    scheduler = Scheduler(settings, repository, fake_model)  # type: ignore[arg-type]
    fake_model.fail = True
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 0, tzinfo=settings.timezone)
    )
    failed = await repository.get_job(10001, "2026-10-25", "distill")
    assert failed["status"] == "failed"
    fake_model.fail = False
    await scheduler.tick(
        datetime(2026, 10, 26, 12, 0, tzinfo=settings.timezone)
    )
    retried = await repository.get_job(10001, "2026-10-25", "distill")
    assert retried["status"] == "success"


@pytest.mark.asyncio
async def test_backup_failure_retries_once_after_ten_minutes(
    settings, repository, monkeypatch
):
    scheduler = Scheduler(settings, repository, object())  # type: ignore[arg-type]
    original_backup = repository.db.backup_to

    async def failing_backup(destination):
        raise OSError("模拟磁盘错误")

    monkeypatch.setattr(repository.db, "backup_to", failing_backup)
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 10, tzinfo=settings.timezone)
    )
    first = await repository.get_job(0, "2026-10-25", "backup")
    assert first["status"] == "failed"
    assert not list(settings.backup_dir.glob("*.db"))
    await repository.db.execute(
        """
        UPDATE job_runs SET ended_at=?
        WHERE group_id=0 AND run_date='2026-10-25' AND job_type='backup'
        """,
        (
            datetime(
                2026, 10, 25, 12, 10, tzinfo=settings.timezone
            ).timestamp(),
        ),
    )

    monkeypatch.setattr(repository.db, "backup_to", original_backup)
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 20, tzinfo=settings.timezone)
    )
    retry = await repository.get_job(0, "2026-10-25", "backup_retry")
    assert retry["status"] == "success"
    assert len(list(settings.backup_dir.glob("*.db"))) == 1


@pytest.mark.asyncio
async def test_daily_maintenance_prunes_audit_and_api_usage(
    settings, repository
):
    now = time.time()
    await repository.audit("old_audit", "success", timestamp=now - 31 * 86400)
    await repository.audit("recent_audit", "success", timestamp=now)
    await repository.record_api_usage(
        "deepseek-chat", 1, 1, 0.01, "old", True, now - 91 * 86400
    )
    await repository.record_api_usage(
        "deepseek-chat", 1, 1, 0.01, "recent", True, now
    )
    scheduler = Scheduler(settings, repository, object())  # type: ignore[arg-type]
    await scheduler.tick(
        datetime(2026, 10, 25, 12, 10, tzinfo=settings.timezone)
    )
    actions = await repository.db.fetchall("SELECT action FROM audit_log")
    action_names = [row["action"] for row in actions]
    assert "recent_audit" in action_names
    assert "old_audit" not in action_names
    calls = await repository.db.fetchall("SELECT call_type FROM api_usage")
    assert [row["call_type"] for row in calls] == ["recent"]


@pytest.mark.asyncio
async def test_owner_delete_only_affects_one_group(repository):
    memory = MemoryService(repository)
    await repository.enable_group(10001)
    await repository.enable_group(10002)
    now = time.time()
    day = "2026-10-25"
    for group_id in (10001, 10002):
        await repository.feed(group_id, 20001, "rice", day, now)
        await memory.remember_group(
            group_id, 20001, "甲", "user", "群消息", group_id, created_at=now
        )
        await memory.remember_private(
            group_id, 20001, "user", "私聊", created_at=now
        )
        await repository.upsert_profile(
            group_id,
            20001,
            "甲",
            "摘要",
            "偏好",
            "风格",
            day,
            now,
        )
    await repository.touch_binding(20001, 10001, 10001, "甲")
    await repository.audit("manual_marker", "success", actor="owner")
    assert await repository.delete_user_data(10001, 20001, now + 1)
    assert not await repository.get_user_full(10001, 20001)
    assert await repository.get_user_full(10002, 20001)
    assert not await repository.get_profile(10001, 20001)
    assert await repository.get_profile(10002, 20001)
    assert await repository.recent_private_messages(10002, 20001)
    assert (await repository.get_binding(20001))["group_id"] == 10002
    assert not await repository.search_group_messages(
        10001, fts_query("群消息")
    )
    assert await repository.search_group_messages(
        10002, fts_query("群消息")
    )
    audit = await repository.audit_page(1)
    assert any(row["action"] == "manual_marker" for row in audit[0])

