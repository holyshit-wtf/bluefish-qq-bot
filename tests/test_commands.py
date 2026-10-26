from __future__ import annotations

import time

import pytest

from services.application import IncomingMessage


def incoming(
    content: str,
    *,
    group_id: int | None = 10001,
    user_id: int = 20001,
    admin: bool = False,
    owner: bool = False,
    message_id: int = 81001,
    to_me: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        user_id=user_id,
        nickname="命令测试",
        content=content,
        group_id=group_id,
        onebot_message_id=message_id,
        to_me=to_me,
        is_reply_to_bot=False,
        is_group_admin=admin,
        is_owner=owner,
    )


@pytest.mark.asyncio
async def test_player_commands_via_message_pipeline(application, repository):
    app, sent = application
    await repository.enable_group(10001)
    await app.process(incoming("/鱼 签到"))
    assert "签到成功" in sent[-1].text
    await app.process(incoming("/鱼 状态", message_id=81002))
    assert "好感" in sent[-1].text and "Token 3" in sent[-1].text
    await app.process(incoming("/鱼 排行", message_id=81003))
    assert "排行" in sent[-1].text and "命令测试" in sent[-1].text


@pytest.mark.asyncio
async def test_group_admin_blocked_term_commands(application, repository):
    app, sent = application
    await repository.enable_group(10001)
    await app.process(
        incoming("/鱼 禁忌 添加 剧透", admin=True, message_id=81101)
    )
    assert "已添加" in sent[-1].text
    await app.process(
        incoming("/鱼 禁忌 列表", admin=True, message_id=81102)
    )
    assert "剧透" in sent[-1].text
    await app.process(
        incoming("/鱼 禁忌 删除 剧透", admin=True, message_id=81103)
    )
    assert "已删除" in sent[-1].text
    assert not await repository.list_blocked_terms(10001)


@pytest.mark.asyncio
async def test_owner_status_view_and_affection_command(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.game_status(
        10001, 20001, "2026-10-25", time.time()
    )
    await repository.upsert_profile(
        10001,
        20001,
        "命令测试",
        "摘要",
        "偏好",
        "风格",
        "2026-10-25",
        time.time(),
    )
    await app.process(
        incoming(
            "/鱼 系统 状态",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81201,
        )
    )
    assert "运行" in sent[-1].text and "发送计数" in sent[-1].text
    await app.process(
        incoming(
            "/鱼 用户 查看 10001 20001",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81202,
        )
    )
    assert "20001" in sent[-1].text
    assert "命令测试" in sent[-1].text
    await app.process(
        incoming(
            "/鱼 用户 好感 10001 20001 +15",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81203,
        )
    )
    assert "15" in sent[-1].text
    assert (
        await repository.game_status(
            10001, 20001, "2026-10-25", time.time()
        )
    )["affection"] == 15


@pytest.mark.asyncio
async def test_global_pause_blocks_group_and_owner_can_resume(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    await app.process(
        incoming(
            "/鱼 全局 暂停",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81301,
        )
    )
    assert await repository.is_globally_paused()
    paused = await app.process(
        incoming("@鱼 暂停时不应回复", message_id=81302)
    )
    assert not paused.handled
    await app.process(
        incoming(
            "/鱼 全局 恢复",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81303,
        )
    )
    resumed = await app.process(
        incoming("@鱼 恢复后回复", message_id=81304, to_me=True)
    )
    assert resumed.replied


@pytest.mark.asyncio
async def test_owner_audit_pages_keep_twenty_records(
    application, repository
):
    app, sent = application
    for index in range(25):
        await repository.audit(
            f"event{index:02d}", "success", timestamp=time.time() + index
        )
    await app.process(
        incoming(
            "/鱼 审计 1",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81401,
        )
    )
    assert "(25)" in sent[-1].text
    assert sent[-1].text.count(";") == 19
    await app.process(
        incoming(
            "/鱼 审计 2",
            group_id=None,
            user_id=9999,
            owner=True,
            message_id=81402,
        )
    )
    assert sent[-1].text.count(";") == 4


@pytest.mark.asyncio
async def test_games_switch_disables_player_data_commands(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    await app.process(
        incoming("/鱼 配置 养成 关", admin=True, message_id=81501)
    )
    assert "已设置为 关" in sent[-1].text
    await app.process(incoming("/鱼 状态", message_id=81502))
    assert "养成模块已关闭" in sent[-1].text
    await app.process(incoming("/鱼 帮助", message_id=81503))
    assert "玩家命令" in sent[-1].text


@pytest.mark.asyncio
async def test_owner_numeric_commands_reject_nonfinite_and_zero(
    application, repository
):
    app, sent = application
    for index, content in enumerate(
        (
            "/鱼 用户 好感 1 2 nan",
            "/鱼 用户 查看 0 2",
            "/鱼 群 启用 0",
        )
    ):
        await app.process(
            incoming(
                content,
                group_id=None,
                user_id=9999,
                owner=True,
                message_id=81601 + index,
            )
        )
        assert "格式" in sent[-1].text or "大于 0" in sent[-1].text


@pytest.mark.asyncio
async def test_private_help_uses_bound_group_persona(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.set_group_config(10001, "persona", "安静鲸娘")
    await repository.touch_binding(20001, 10001, 1, "命令测试")
    await app.process(
        incoming(
            "/鱼 帮助",
            group_id=None,
            message_id=81701,
        )
    )
    assert "安静鲸娘" in sent[-1].text


@pytest.mark.asyncio
async def test_private_status_advances_shared_group_state(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.db.execute(
        """
        UPDATE group_state
        SET hunger=80, mood=90, updated_at=?
        WHERE group_id=10001
        """,
        (time.time() - 2 * 3600,),
    )
    await repository.touch_binding(20001, 10001, 1, "命令测试")
    await app.process(
        incoming(
            "/鱼 状态",
            group_id=None,
            message_id=81801,
        )
    )
    assert "饥饿 76" in sent[-1].text
    assert "心情 88" in sent[-1].text
