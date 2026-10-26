from __future__ import annotations

import time

import pytest

from services.memory import MemoryService
from services.safety import (
    check_group_message,
    fts_query,
    normalize_blocked_term,
    sanitize_reply,
)
from storage.database import Database
from storage.repository import Repository


@pytest.mark.asyncio
async def test_game_limits_cooldown_and_signin(repository, settings):
    group_id, user_id = 10001, 20001
    await repository.enable_group(group_id)
    now = time.time()
    day = "2026-10-25"
    sign = await repository.signin(group_id, user_id, day, now)
    assert sign.ok
    assert (await repository.signin(group_id, user_id, day, now + 1)).ok is False

    rice = await repository.feed(group_id, user_id, "rice", day, now + 2)
    assert rice.ok
    blocked = await repository.feed(group_id, user_id, "rice", day, now + 3)
    assert not blocked.ok
    assert "冷却" in blocked.message

    await repository.db.execute(
        "UPDATE group_state SET hunger=100 WHERE group_id=?", (group_id,)
    )
    await repository.db.execute(
        "UPDATE tokens SET balance=20 WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    )
    full = await repository.feed(
        group_id, user_id, "token", day, now + 1801
    )
    assert not full.ok
    assert full.token_balance == 20


@pytest.mark.asyncio
async def test_daily_affection_and_growth_caps(repository):
    group_id, user_id = 10001, 20001
    await repository.enable_group(group_id)
    now = time.time()
    day = "2026-10-25"
    await repository.game_status(group_id, user_id, day, now)
    await repository.db.execute(
        "UPDATE tokens SET balance=999 WHERE FALSE"
    )
    for index in range(20):
        await repository.db.execute(
            "UPDATE group_state SET hunger=0 WHERE group_id=?", (group_id,)
        )
        await repository.db.execute(
            "UPDATE tokens SET balance=999 WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        )
        result = await repository.feed(
            group_id, user_id, "token", day, now + index * 3600
        )
        assert result.ok
    status = await repository.game_status(
        group_id, user_id, day, now + 25 * 3600
    )
    assert status["affection"] == 10
    assert status["exp"] == 50
    assert status["affection_daily"] == 10
    assert status["growth_daily"] == 50

    status_next = await repository.game_status(
        group_id, user_id, "2026-10-26", now + 26 * 3600
    )
    assert status_next["affection_daily"] == 0
    assert status_next["growth_daily"] == 0


@pytest.mark.asyncio
async def test_group_state_evolves_hourly(repository):
    group_id = 10001
    await repository.enable_group(group_id)
    group = await repository.get_group(group_id)
    started = float(group["state_updated_at"])
    await repository.db.execute(
        "UPDATE group_state SET mood=90 WHERE group_id=?", (group_id,)
    )
    hunger, mood = await repository.evolve_group_state(
        group_id, started + 3 * 3600
    )
    assert hunger == 74
    assert mood == 87

    await repository.db.execute(
        "UPDATE group_state SET mood=90, hunger=10, updated_at=? WHERE group_id=?",
        (started + 3 * 3600, group_id),
    )
    hunger, mood = await repository.evolve_group_state(
        group_id, started + 35 * 3600
    )
    assert hunger == 0
    assert mood == 60


@pytest.mark.asyncio
async def test_natural_chat_token_rewards_are_rate_limited(repository):
    group_id, user_id = 10001, 20001
    await repository.enable_group(group_id)
    now = time.time()
    day = "2026-10-25"
    assert await repository.reward_chat(group_id, user_id, day, now)
    assert not await repository.reward_chat(
        group_id, user_id, day, now + 899
    )
    for index in range(1, 7):
        assert await repository.reward_chat(
            group_id, user_id, day, now + index * 900
        )
    assert not await repository.reward_chat(
        group_id, user_id, day, now + 7 * 900
    )
    status = await repository.game_status(
        group_id, user_id, day, now + 7 * 900
    )
    assert status["balance"] == 7
    assert status["chat_reward_count"] == 7
    assert await repository.reward_chat(
        group_id, user_id, "2026-10-26", now + 24 * 3600
    )


@pytest.mark.asyncio
async def test_signin_respects_token_balance_cap(repository):
    group_id, user_id = 10001, 20001
    await repository.enable_group(group_id)
    now = time.time()
    await repository.game_status(group_id, user_id, "2026-10-25", now)
    await repository.db.execute(
        "UPDATE tokens SET balance=998 WHERE group_id=? AND user_id=?",
        (group_id, user_id),
    )
    result = await repository.signin(
        group_id, user_id, "2026-10-25", now + 1
    )
    assert result.token_balance == 999
    assert "获得 1 枚" in result.message


@pytest.mark.asyncio
async def test_memory_retention_and_chinese_fts(repository):
    group_id = 10001
    await repository.enable_group(group_id)
    memory = MemoryService(repository)
    base = time.time() - 1000
    for index in range(450):
        await memory.remember_group(
            group_id,
            1,
            "甲",
            "user",
            f"第{index}条 今天讨论蓝色鲸鱼和编程",
            index,
            created_at=base + index,
        )
    recent = await repository.recent_group_messages(group_id, 400)
    assert len(recent) == 400
    result = await repository.search_group_messages(
        group_id, fts_query("蓝色鲸鱼")
    )
    assert result
    recent_context, merged = await memory.context_for_group(
        group_id, "蓝色鲸鱼"
    )
    assert len(recent_context) == 80
    merged_ids = [int(item["id"]) for item in merged]
    assert len(merged_ids) == len(set(merged_ids)) == 96
    merged_times = [float(item["created_at"]) for item in merged]
    assert merged_times == sorted(merged_times)
    query = fts_query("蓝色鲸鱼")
    started = time.perf_counter()
    for _ in range(10):
        await repository.search_group_messages(group_id, query)
    elapsed = (time.perf_counter() - started) / 10
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_private_retention(repository):
    await repository.enable_group(10001)
    memory = MemoryService(repository)
    for index in range(45):
        await memory.remember_private(
            10001, 2, "user", f"私聊第{index}条", created_at=time.time() + index
        )
    history = await repository.recent_private_messages(10001, 2, 100)
    assert len(history) == 40


def test_content_safety_and_output_sanitization():
    term = normalize_blocked_term(" 禁 止 ")
    assert term == "禁止"
    assert not check_group_message("请 禁 止 发言", [term]).allowed
    assert sanitize_reply("**你好** http://evil.example @全体成员") == "你好"
    assert sanitize_reply("加QQ群 123456，电话 13800138000") == "加，"


@pytest.mark.asyncio
async def test_group_isolation(repository):
    await repository.enable_group(10001)
    await repository.enable_group(10002)
    day = "2026-10-25"
    now = time.time()
    await repository.feed(10001, 1, "rice", day, now)
    assert (await repository.game_status(10001, 1, day, now))["affection"] == 1
    assert not await repository.get_user_full(10002, 1)
    await repository.touch_binding(1, 10001, 1)
    await repository.touch_binding(2, 10002, 2)
    assert (await repository.get_binding(1))["group_id"] == 10001
    assert (await repository.get_binding(2))["group_id"] == 10002


@pytest.mark.asyncio
async def test_restart_preserves_config_values_context_and_window(settings):
    database = Database(settings.database_path)
    await database.initialize()
    repository = Repository(database)
    await repository.enable_group(10001)
    await repository.set_group_config(10001, "persona", "活力鲸娘")
    day = "2026-10-25"
    now = time.time()
    await repository.feed(10001, 1, "rice", day, now)
    await MemoryService(repository).remember_group(
        10001, 1, "甲", "user", "重启也要记住的句子", 1001, created_at=now
    )
    for index in range(settings.send_limit_per_minute):
        reservation = await repository.reserve_send(
            "restart", 10001, now - index, now - 10_000
        )
        assert reservation is not None
        await repository.finish_send(
            reservation, success=True, onebot_message_id=1000 + index
        )
    await database.close()

    reopened = Database(settings.database_path)
    await reopened.initialize()
    reopened_repo = Repository(reopened)
    group = await reopened_repo.get_group(10001)
    assert group["persona"] == "活力鲸娘"
    assert (await reopened_repo.game_status(10001, 1, day, now))["affection"] == 1
    assert (await reopened_repo.recent_group_messages(10001))[0]["content"] == (
        "重启也要记住的句子"
    )
    blocked = await reopened_repo.reserve_send(
        "after_restart", 10001, now + 1, now - 10_000
    )
    assert blocked is None
    await reopened.close()

