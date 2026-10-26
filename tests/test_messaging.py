from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

from core.time import day_start_timestamp
from services.application import IncomingMessage
from services.search import SearchResult


def message(
    *,
    group_id: int | None = 10001,
    user_id: int = 20001,
    content: str,
    to_me: bool = True,
    is_reply_to_bot: bool = False,
    admin: bool = False,
    owner: bool = False,
    message_id: int = 7001,
) -> IncomingMessage:
    return IncomingMessage(
        user_id=user_id,
        nickname="测试群友",
        content=content,
        group_id=group_id,
        onebot_message_id=message_id,
        to_me=to_me,
        is_reply_to_bot=is_reply_to_bot,
        is_group_admin=admin,
        is_owner=owner,
    )


@pytest.mark.asyncio
async def test_unenabled_group_is_silent(application, repository, fake_model):
    app, sent = application
    result = await app.process(message(content="@鱼 你好"))
    assert not result.handled
    assert result.reason == "group_not_enabled"
    assert not sent
    assert not fake_model.chat_calls


@pytest.mark.asyncio
async def test_required_reply_is_quoted_and_limited(application, repository, fake_model):
    app, sent = application
    await repository.enable_group(10001)
    result = await app.process(message(content="@鱼 你好"))
    assert result.replied
    assert len(sent) == 1
    assert sent[0].quote_id == 7001
    assert sent[0].group_id == 10001
    assert len(fake_model.chat_calls) == 1
    prompt = fake_model.chat_calls[0]["user"]
    assert "群配置" in prompt
    assert "当前用户匿名键" in prompt
    assert "20001" not in prompt
    assert "F:\\" not in prompt


@pytest.mark.asyncio
async def test_direct_question_uses_search_and_dynamic_time(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.results = [
        SearchResult(
            title="星海遗迹第三关攻略",
            url="https://example.com/guide",
            snippet="右侧道路有隐藏宝箱。",
            summary="先开启左侧机关，再沿右侧道路前进并取得宝箱。",
            date=datetime.now(settings.timezone).isoformat(),
            site_name="攻略站",
            authority=3,
            rerank_score=2.4,
        )
    ]
    result = await app.process(
        message(content="@鱼 星海遗迹第三关怎么打")
    )
    assert result.replied
    assert len(fake_search.calls) == 1
    call = fake_model.chat_calls[-1]
    assert "当前时间：" in call["system"]
    assert "不超过 500 字" in call["system"]
    assert "外部搜索资料" in call["user"]
    assert "先开启左侧机关" in call["user"]
    assert "https://example.com" not in call["user"]
    assert "https://example.com" not in sent[-1].text
    audit = await repository.db.fetchone(
        "SELECT action, result, metadata_json FROM audit_log "
        "WHERE action='web_search'"
    )
    assert audit["result"] == "success"
    assert "星海遗迹" not in audit["metadata_json"]


@pytest.mark.asyncio
async def test_reply_to_bot_triggers_search(
    application, repository, fake_search
):
    app, _ = application
    await repository.enable_group(10001)
    result = await app.process(
        message(
            content="今天有什么新闻",
            to_me=False,
            is_reply_to_bot=True,
        )
    )
    assert result.replied
    assert len(fake_search.calls) == 2


@pytest.mark.asyncio
async def test_random_insert_does_not_search(
    application, repository, fake_search
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.set_group_config(10001, "base_probability", 100)
    result = await app.process(
        message(
            content="今天有什么新闻",
            to_me=False,
        )
    )
    assert result.replied
    assert sent
    assert not fake_search.calls


@pytest.mark.asyncio
async def test_greeting_and_local_time_do_not_search(
    application, repository, fake_search
):
    app, _ = application
    await repository.enable_group(10001)
    await app.process(message(content="@鱼 你好"))
    await app.process(
        message(content="@鱼 今天星期几", message_id=7002)
    )
    assert not fake_search.calls


@pytest.mark.asyncio
async def test_time_sensitive_search_failure_uses_safe_fallback(
    application, repository, fake_model, fake_search
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.fail = True
    result = await app.process(message(content="@鱼 今天有什么新闻"))
    assert result.replied
    assert not fake_model.chat_calls
    assert "没查到" in sent[-1].text
    assert "瞎报" in sent[-1].text
    audit = await repository.db.fetchone(
        "SELECT result, metadata_json FROM audit_log "
        "WHERE action='web_search'"
    )
    assert audit["result"] == "failed"
    assert "今天有什么新闻" not in audit["metadata_json"]


@pytest.mark.asyncio
async def test_stable_search_failure_falls_back_to_model_knowledge(
    application, repository, fake_model, fake_search
):
    app, _ = application
    await repository.enable_group(10001)
    fake_search.fail = True
    result = await app.process(
        message(content="@鱼 星海遗迹第三关怎么打")
    )
    assert result.replied
    call = fake_model.chat_calls[-1]
    assert "没有获得可用的外部搜索资料" in call["system"]
    assert "稳定的攻略或知识问题" in call["system"]
    assert "外部搜索资料" not in call["user"]


@pytest.mark.asyncio
async def test_search_reply_allows_500_chars_and_strips_links(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.results = [
        SearchResult(
            title="攻略",
            url="https://example.com",
            snippet="摘要",
            summary="完整摘要",
            date=datetime.now(settings.timezone).isoformat(),
            site_name="攻略站",
            authority=3,
            rerank_score=2.0,
        )
    ]
    fake_model.reply = "攻" * 600 + " https://evil.example"
    result = await app.process(
        message(content="@鱼 给我一份游戏攻略")
    )
    assert result.replied
    assert 300 < len(sent[-1].text) <= 500
    assert "http" not in sent[-1].text


@pytest.mark.asyncio
async def test_private_market_failure_has_time_and_risk_notice(
    application, repository, fake_model, fake_search
):
    app, sent = application
    fake_search.fail = True
    result = await app.process(
        message(
            group_id=None,
            content="今天股价多少",
            to_me=False,
        )
    )
    assert result.replied
    assert len(fake_search.calls) == 1
    assert not fake_model.chat_calls
    assert "行情可能延迟" in sent[-1].text
    assert "不构成投资建议" in sent[-1].text


@pytest.mark.asyncio
async def test_market_search_always_adds_risk_notice(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.results = [
        SearchResult(
            title="行情摘要",
            url="https://example.com/quote",
            snippet="示例指数上涨。",
            summary="截至今日午间，示例指数上涨。",
            date=datetime.now(settings.timezone).isoformat(),
            site_name="行情站",
            authority=3,
            rerank_score=2.3,
        )
    ]
    fake_model.reply = "示例指数今天午间是上涨的。"
    result = await app.process(message(content="@鱼 今天股市行情怎么样"))
    assert result.replied
    assert "公开行情可能延迟，也不构成投资建议。" in sent[-1].text
    assert "这是行情问题" in fake_model.chat_calls[-1]["system"]


@pytest.mark.asyncio
async def test_long_unbound_market_reply_keeps_reminder_and_notice(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    fake_search.results = [
        SearchResult(
            title="行情摘要",
            url="https://example.com/quote",
            snippet="示例行情。",
            summary="截至今日午间，示例指数上涨。",
            date=datetime.now(settings.timezone).isoformat(),
            site_name="行情站",
            authority=3,
            rerank_score=2.3,
        )
    ]
    fake_model.reply = "涨" * 600
    result = await app.process(
        message(
            group_id=None,
            content="今天股市行情怎么样",
            to_me=False,
        )
    )
    assert result.replied
    assert len(sent[-1].text) <= 500
    assert "先在已启用的群" in sent[-1].text
    assert "公开行情可能延迟，也不构成投资建议。" in sent[-1].text


@pytest.mark.asyncio
async def test_follow_up_search_uses_recent_user_context(
    application, repository, fake_search
):
    app, _ = application
    await repository.enable_group(10001)
    await app.memory.remember_group(
        10001,
        20002,
        "测试群友",
        "user",
        "最近在玩《星海遗迹》",
        7990,
        created_at=time.time() - 2,
    )
    result = await app.process(
        message(content="@鱼 第三关怎么打", message_id=7991)
    )
    assert result.replied
    assert "《星海遗迹》" in fake_search.calls[0]["query"]


@pytest.mark.asyncio
async def test_empty_search_retries_with_compact_query(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.result_batches = [
        [],
        [
            SearchResult(
                title="赛尔号巅峰之战更新",
                url="https://example.com/sail",
                snippet="本周上线新玩法。",
                summary="本周上线新玩法，并开放新的兑换内容。",
                date=datetime.now(settings.timezone).isoformat(),
                site_name="更新站",
                authority=3,
                rerank_score=2.2,
            )
        ],
    ]
    fake_model.reply = "本周开了新玩法，也换了兑换内容。"
    result = await app.process(
        message(content="@鱼 赛尔号巅峰之战本周更新了什么内容")
    )
    assert result.replied
    assert len(fake_search.calls) == 2
    month = datetime.now(settings.timezone).strftime("%Y年%m月")
    assert month in fake_search.calls[0]["query"]
    assert "本周" not in fake_search.calls[0]["query"]
    assert "2026年" not in fake_search.calls[1]["query"]
    assert "本周开了新玩法" in sent[-1].text
    assert "这是时效问题" in fake_model.chat_calls[-1]["system"]
    audit = await repository.db.fetchone(
        "SELECT result, metadata_json FROM audit_log "
        "WHERE action='web_search'"
    )
    assert audit["result"] == "success"
    assert '"attempts":2' in audit["metadata_json"]


@pytest.mark.asyncio
async def test_research_consultation_and_topic_follow_ups_search(
    application, repository, fake_model, fake_search, settings
):
    app, sent = application
    await repository.enable_group(10001)
    fake_search.results = [
        SearchResult(
            title="最新研究进展",
            url="https://example.com/research",
            snippet="近期研究摘要。",
            summary="近期研究在免疫治疗方向取得新进展。",
            date=datetime.now(settings.timezone).isoformat(),
            site_name="研究站",
            authority=3,
            rerank_score=2.4,
        )
    ]
    fake_model.reply = "近期这项研究有了新的研究进展。"
    for index, content in enumerate(
        ("近期有什么科研咨询", "聊聊生物医学", "免疫治疗"),
        start=1,
    ):
        result = await app.process(
            message(content=f"@鱼 {content}", message_id=8100 + index)
        )
        assert result.replied
    assert len(fake_search.calls) == 3
    assert "科研资讯" in fake_search.calls[0]["query"]
    assert "生物医学 最新研究进展" in fake_search.calls[1]["query"]
    assert "免疫治疗 最新研究进展" in fake_search.calls[2]["query"]
    assert all("研究进展" in item.text for item in sent[-3:])


@pytest.mark.asyncio
async def test_random_trigger_cooldown_and_required_bypass(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.set_group_config(10001, "base_probability", 100)
    first = await app.process(
        message(
            content="今天好饿",
            to_me=False,
            message_id=7051,
        )
    )
    assert first.replied
    second = await app.process(
        message(
            content="再来一句",
            to_me=False,
            message_id=7052,
        )
    )
    assert not second.replied
    required = await app.process(
        message(
            content="@鱼 现在必须回答",
            to_me=True,
            message_id=7053,
        )
    )
    assert required.replied
    assert len(sent) == 2
    assert len(fake_model.chat_calls) == 2


@pytest.mark.asyncio
async def test_random_probability_mood_and_persona_modifiers(
    application, repository
):
    app, _ = application
    await repository.enable_group(10001)
    group = await repository.get_group(10001)
    group = dict(group)
    group["base_probability"] = 0
    group["cooldown_seconds"] = 0
    group["mood"] = 100
    group["persona"] = "标准贪吃鲸娘"

    class FixedRandom:
        def __init__(self, value):
            self.value = value

        def random(self):
            return self.value

    app.rng = FixedRandom(0.049)
    assert await app._should_randomly_insert(group, time.time())
    app.rng = FixedRandom(0.051)
    assert not await app._should_randomly_insert(group, time.time())
    group["persona"] = "安静鲸娘"
    app.rng = FixedRandom(0.03)
    assert not await app._should_randomly_insert(group, time.time())


@pytest.mark.asyncio
async def test_send_limit_blocks_model_and_survives_reopen(
    application, repository, db, settings, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    now = datetime.now(settings.timezone)
    now_ts = time.time()
    for index in range(settings.send_limit_per_minute):
        reservation = await repository.reserve_send(
            "preload", 10001, now_ts - index, day_start_timestamp(now, settings.timezone)
        )
        assert reservation is not None
        await repository.finish_send(
            reservation, success=True, onebot_message_id=9000 + index
        )
    result = await app.process(message(content="@鱼 被限流", message_id=7002))
    assert not result.replied
    assert result.reason == "send_limit"
    assert not fake_model.chat_calls
    rows = await repository.db.fetchall(
        "SELECT action FROM audit_log WHERE action='send_limit'"
    )
    assert rows

    await db.close()


@pytest.mark.asyncio
async def test_command_permissions_and_persona(application, repository, fake_model):
    app, sent = application
    await repository.enable_group(10001)

    await app.process(
        message(
            content="/鱼 配置 概率 50",
            admin=False,
            message_id=7101,
        )
    )
    assert "管理员" in sent[-1].text

    await app.process(
        message(
            content="/鱼 配置 人格 安静鲸娘",
            admin=True,
            message_id=7102,
        )
    )
    group = await repository.get_group(10001)
    assert group["persona"] == "安静鲸娘"
    assert "已切换" in sent[-1].text

    await app.process(
        message(
            content="/鱼 全局 暂停",
            admin=True,
            message_id=7103,
        )
    )
    assert "只能由主人私聊" in sent[-1].text
    assert not await repository.is_globally_paused()


@pytest.mark.asyncio
async def test_owner_private_can_enable_group(application, repository):
    app, sent = application
    result = await app.process(
        message(
            group_id=None,
            user_id=9999,
            content="/鱼 群 启用 12345",
            to_me=False,
            owner=True,
        )
    )
    assert result.replied
    assert await repository.group_is_enabled(12345)
    assert sent[-1].user_id == 9999


@pytest.mark.asyncio
async def test_private_binding_does_not_leak_group_context(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.touch_binding(20001, 10001, 1)
    await app.memory.remember_group(
        10001,
        30001,
        "另一个群友",
        "user",
        "只在群里出现的秘密句子",
        2,
    )
    result = await app.process(
        message(
            group_id=None,
            content="私聊问一句",
            to_me=False,
        )
    )
    assert result.replied
    call = fake_model.chat_calls[-1]
    assert "只在群里出现的秘密句子" not in call["user"]
    assert all(
        "只在群里出现的秘密句子" not in item["content"]
        for item in call["history"]
    )
    history = await app.memory.private_history(10001, 20001)
    assert len(history) == 2


@pytest.mark.asyncio
async def test_switching_binding_group_uses_new_private_context(
    application, repository
):
    app, _ = application
    await repository.enable_group(10001)
    await repository.enable_group(10002)
    await repository.set_group_config(10001, "base_probability", 0)
    await repository.set_group_config(10002, "base_probability", 0)
    app.rng = type(
        "NeverRandom",
        (),
        {"random": lambda self: 0.99},
    )()

    await app.process(
        message(
            group_id=10001,
            content="我先在 A 群发言",
            to_me=False,
            message_id=7401,
        )
    )
    await app.process(
        message(
            group_id=None,
            content="A 群私聊内容",
            to_me=False,
            message_id=7402,
        )
    )
    assert (await repository.get_binding(20001))["group_id"] == 10001

    await app.process(
        message(
            group_id=10002,
            content="我再在 B 群发言",
            to_me=False,
            message_id=7403,
        )
    )
    await app.process(
        message(
            group_id=None,
            content="B 群私聊内容",
            to_me=False,
            message_id=7404,
        )
    )
    assert (await repository.get_binding(20001))["group_id"] == 10002
    history_a = await repository.recent_private_messages(10001, 20001)
    history_b = await repository.recent_private_messages(10002, 20001)
    assert len(history_a) == len(history_b) == 2
    assert history_a[0]["content"] == "A 群私聊内容"
    assert history_b[0]["content"] == "B 群私聊内容"


@pytest.mark.asyncio
async def test_private_without_binding_is_stateless(application, repository):
    app, sent = application
    result = await app.process(
        message(
            group_id=None,
            user_id=20002,
            content="你好",
            to_me=False,
        )
    )
    assert result.replied
    assert "先在已启用的群" in sent[-1].text
    assert not await repository.get_binding(20002)


@pytest.mark.asyncio
async def test_private_without_binding_always_includes_prompt(
    application, repository, fake_model
):
    app, sent = application
    fake_model.reply = "长" * 300
    await app.process(
        message(
            group_id=None,
            user_id=20003,
            content="你好",
            to_me=False,
        )
    )
    assert len(sent[-1].text) <= 300
    assert "先在已启用的群" in sent[-1].text


@pytest.mark.asyncio
async def test_image_message_updates_binding_and_group_context(
    application, repository
):
    app, sent = application
    await repository.enable_group(10001)
    result = await app.process(
        IncomingMessage(
            user_id=20009,
            nickname="发图群友",
            content="",
            group_id=10001,
            onebot_message_id=78001,
            to_me=False,
            is_reply_to_bot=False,
            is_group_admin=False,
            is_owner=False,
            has_image=True,
        )
    )
    assert result.handled
    assert (await repository.get_binding(20009))["group_id"] == 10001
    recent = await repository.recent_group_messages(10001)
    assert recent[-1]["content"] == "[图片]"


@pytest.mark.asyncio
async def test_model_failure_is_silent_but_commands_work(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    fake_model.fail = True
    result = await app.process(message(content="@鱼 模型会失败"))
    assert not result.replied
    assert not sent
    fake_model.fail = False
    result = await app.process(
        message(content="/鱼 签到", to_me=False, message_id=7202)
    )
    assert result.replied
    assert "签到成功" in sent[-1].text
    assert not fake_model.chat_calls


@pytest.mark.asyncio
async def test_blocked_term_skips_model_context_and_persistence(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.add_blocked_term(
        10001, "禁止词", "禁止词", created_by=9999
    )
    result = await app.process(message(content="@鱼 这是禁 止 词内容"))
    assert not result.handled
    assert result.reason == "blocked_term"
    assert not sent
    assert not fake_model.chat_calls
    assert not await repository.recent_group_messages(10001)
    rows = await repository.db.fetchall(
        "SELECT metadata_json FROM audit_log WHERE action='blocked_message'"
    )
    assert rows
    assert "禁 止 词内容" not in rows[0]["metadata_json"]


@pytest.mark.asyncio
async def test_model_output_hitting_blocked_term_is_not_sent(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.add_blocked_term(
        10001, "剧透", "剧透", created_by=9999
    )
    fake_model.reply = "这里有一个剧透内容"
    result = await app.process(message(content="@鱼 讲点什么"))
    assert not result.replied
    assert not sent
    row = await repository.db.fetchone(
        "SELECT metadata_json FROM audit_log "
        "WHERE action='blocked_output'"
    )
    assert row and "剧透" not in row["metadata_json"]


@pytest.mark.asyncio
async def test_command_send_limit_prevents_state_change(
    application, repository, settings
):
    app, sent = application
    await repository.enable_group(10001)
    now = datetime.now(settings.timezone)
    now_ts = time.time()
    for index in range(settings.send_limit_per_minute):
        reservation = await repository.reserve_send(
            "preload",
            10001,
            now_ts - index,
            day_start_timestamp(now, settings.timezone),
        )
        await repository.finish_send(
            reservation, success=True, onebot_message_id=9500 + index
        )
    result = await app.process(
        message(content="/鱼 签到", to_me=False, message_id=7301)
    )
    assert not result.replied
    count = await repository.db.scalar(
        "SELECT COUNT(*) FROM tokens WHERE user_id=?", (20001,), 0
    )
    assert count == 0
    assert not sent


@pytest.mark.asyncio
async def test_budget_alert_is_sent_once(application, repository, settings):
    app, sent = application
    await repository.record_api_usage(
        "deepseek-chat",
        1_000_000,
        1_000_000,
        settings.monthly_budget_cny + 1,
        "test",
        True,
        time.time(),
    )
    await asyncio.gather(
        app.maybe_send_budget_alert(),
        app.maybe_send_budget_alert(),
    )
    assert sent
    assert sent[-1].user_id == settings.owner_qq
    await app.maybe_send_budget_alert()
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_private_pending_requests_are_bounded(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    await repository.touch_binding(20001, 10001, 1)
    gate = asyncio.Event()
    original_chat = fake_model.chat

    async def slow_chat(*args, **kwargs):
        await gate.wait()
        return await original_chat(*args, **kwargs)

    fake_model.chat = slow_chat
    tasks = [
        asyncio.create_task(
            app.process(
                message(
                    group_id=None,
                    content=f"并发私聊{index}",
                    message_id=7600 + index,
                )
            )
        )
        for index in range(3)
    ]
    await asyncio.sleep(0.05)
    dropped = await app.process(
        message(
            group_id=None,
            content="第四条",
            message_id=7603,
        )
    )
    assert not dropped.handled
    assert dropped.reason == "private_queue_full"
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(result.replied for result in results)
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_group_messages_are_processed_in_order(
    application, repository, fake_model
):
    app, sent = application
    await repository.enable_group(10001)
    gate = asyncio.Event()
    started_prompts: list[str] = []
    original_chat = fake_model.chat

    async def slow_group_chat(system, user, **kwargs):
        started_prompts.append(user)
        if len(started_prompts) == 1:
            await gate.wait()
        return await original_chat(system, user, **kwargs)

    fake_model.chat = slow_group_chat
    first = asyncio.create_task(
        app.process(message(content="@鱼 第一条", message_id=7901))
    )
    second = asyncio.create_task(
        app.process(message(content="@鱼 第二条", message_id=7902))
    )
    deadline = asyncio.get_running_loop().time() + 2
    while (
        not started_prompts
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.01)
    assert len(started_prompts) == 1
    assert "第一条" in started_prompts[0]
    gate.set()
    results = await asyncio.gather(first, second)
    assert all(result.replied for result in results)
    assert len(started_prompts) == 2
    assert "第二条" in started_prompts[1]
    assert [item.quote_id for item in sent] == [7901, 7902]

