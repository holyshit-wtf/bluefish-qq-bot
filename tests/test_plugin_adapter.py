from __future__ import annotations

import importlib
import os
import time
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_onebot_group_event_adapter_path(
    settings, repository, application, fake_model
):
    app, sent = application
    await repository.enable_group(10001)

    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter, GroupMessageEvent

    os.environ.setdefault("DRIVER", "~websockets")
    os.environ.setdefault("ONEBOT_WS_URLS", "[]")
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    driver = nonebot.get_driver()
    if not driver._adapters:
        driver.register_adapter(Adapter)
    plugin = importlib.import_module("plugins.bluefish")

    reservation = await repository.reserve_send(
        "preload",
        10001,
        time.time(),
        time.time() - 10_000,
    )
    assert reservation is not None
    await repository.finish_send(
        reservation, success=True, onebot_message_id=88001
    )

    event = GroupMessageEvent.model_validate(
        {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": int(time.time()),
            "self_id": 10000,
            "user_id": 20001,
            "message_id": 77001,
            "message": [
                {"type": "reply", "data": {"id": "88001"}},
                {"type": "text", "data": {"text": "接上一条"}},
            ],
            "raw_message": "接上一条",
            "font": 0,
            "sender": {
                "user_id": 20001,
                "nickname": "适配器测试",
                "role": "admin",
            },
            "group_id": 10001,
            "to_me": False,
        }
    )
    old_runtime = plugin._runtime
    plugin._runtime = SimpleNamespace(
        application=app,
        repository=repository,
        settings=settings,
    )
    try:
        await plugin._handle_message(object(), event)
    finally:
        plugin._runtime = old_runtime

    assert len(sent) == 1
    assert sent[0].quote_id == 77001
    assert fake_model.chat_calls
    assert "接上一条" in fake_model.chat_calls[-1]["user"]

    command_event = GroupMessageEvent.model_validate(
        {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "time": int(time.time()),
            "self_id": 10000,
            "user_id": 20001,
            "message_id": 77002,
            "message": [
                {
                    "type": "text",
                    "data": {"text": "/鱼 配置 人格 活力鲸娘"},
                }
            ],
            "raw_message": "/鱼 配置 人格 活力鲸娘",
            "font": 0,
            "sender": {
                "user_id": 20001,
                "nickname": "适配器测试",
                "role": "admin",
            },
            "group_id": 10001,
            "to_me": False,
        }
    )
    plugin._runtime = SimpleNamespace(
        application=app,
        repository=repository,
        settings=settings,
    )
    try:
        await plugin._handle_message(object(), command_event)
    finally:
        plugin._runtime = old_runtime
    assert (await repository.get_group(10001))["persona"] == "活力鲸娘"
    assert len(sent) == 2

    plugin._runtime = None
    await plugin._bot_disconnect(object())
    await plugin._bot_connect(object())
