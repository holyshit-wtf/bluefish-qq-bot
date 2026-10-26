from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)

from core.config import Settings, load_settings
from jobs.scheduler import Scheduler
from services.application import BlueFishApplication, IncomingMessage
from services.commands import RuntimeState
from services.llm import DeepSeekClient
from services.memory import MemoryService
from services.search import DeepSeekSearchClient
from storage.database import Database
from storage.repository import Repository

logger = logging.getLogger(__name__)


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings.database_path)
        self.repository: Repository | None = None
        self.application: BlueFishApplication | None = None
        self.scheduler: Scheduler | None = None
        self.state = RuntimeState(started_at=time.time())

    async def start(self) -> None:
        self.settings.create_runtime_dirs()
        await self.database.initialize()
        self.repository = Repository(self.database)
        model = DeepSeekClient(self.settings, self.repository)
        search = DeepSeekSearchClient(self.settings, self.repository)
        memory = MemoryService(
            self.repository,
            group_retention_limit=self.settings.group_history_retention,
            group_context_messages=self.settings.group_context_messages,
            group_context_fts_limit=self.settings.group_context_fts_limit,
        )
        self.application = BlueFishApplication(
            self.settings,
            self.repository,
            memory,
            model,
            self.state,
            self._send,
            search=search,
        )
        self.scheduler = Scheduler(
            self.settings,
            self.repository,
            model,
            budget_check=self.application.maybe_send_budget_alert,
        )
        await self._record_versions()
        self.scheduler.start()
        logger.info(
            "蓝色大肥鱼已启动",
            extra={"database": str(self.settings.database_path)},
        )

    async def stop(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.application is not None:
            await self.application.stop()
        if self.repository is not None:
            await self.repository.audit("shutdown", "success", actor="system")
        await self.database.close()

    async def mark_connected(self, bot: Bot) -> None:
        try:
            status = await bot.call_api("get_status")
            if not isinstance(status, dict):
                raise RuntimeError("OneBot get_status 返回值异常")
            if status.get("online") is False or status.get("good") is False:
                raise RuntimeError("OneBot 报告账号离线或连接异常")
            login = await bot.call_api("get_login_info")
            if not isinstance(login, dict) or int(login.get("user_id") or 0) <= 0:
                raise RuntimeError("OneBot 未返回有效登录账号")
        except Exception as exc:
            self.state.onebot_connected = False
            if self.repository is not None:
                await self.repository.audit(
                    "onebot_connect",
                    "failed",
                    actor="system",
                    metadata={"error": type(exc).__name__},
                )
            return
        self.state.onebot_connected = True
        try:
            info = await bot.call_api("get_version_info")
            if isinstance(info, dict):
                app_name = info.get("app_name") or info.get("impl") or "OneBot"
                app_version = (
                    info.get("app_version")
                    or info.get("version")
                    or info.get("impl_version")
                    or "unknown"
                )
                self.state.napcat_version = f"{app_name} {app_version}"
        except Exception:
            self.state.napcat_version = "已连接（版本查询失败）"
        if self.repository is not None:
            await self.repository.audit(
                "onebot_connect", "success", actor="system"
            )
        await self._record_versions()

    async def mark_disconnected(self) -> None:
        self.state.onebot_connected = False
        if self.repository is not None:
            await self.repository.audit(
                "onebot_disconnect", "warning", actor="system"
            )

    async def _send(
        self,
        text: str,
        group_id: int | None,
        user_id: int | None,
        quote_message_id: int | None,
    ) -> int | None:
        bots = list(nonebot.get_bots().values())
        if not bots:
            raise RuntimeError("OneBot 当前不在线")
        bot = bots[0]
        if group_id is not None:
            message = Message()
            if quote_message_id is not None:
                message.append(MessageSegment.reply(str(quote_message_id)))
            message.append(MessageSegment.text(text))
            response = await bot.call_api(
                "send_group_msg", group_id=group_id, message=message
            )
        else:
            if user_id is None:
                raise ValueError("私聊发送缺少用户")
            response = await bot.call_api(
                "send_private_msg",
                user_id=user_id,
                message=Message(MessageSegment.text(text)),
            )
        if isinstance(response, dict):
            value = response.get("message_id")
            return int(value) if value is not None else None
        return None

    async def _record_versions(self) -> None:
        if self.repository is None:
            return
        try:
            nonebot_version = importlib.metadata.version("nonebot2")
        except importlib.metadata.PackageNotFoundError:
            nonebot_version = "unknown"
        versions = {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "nonebot2": nonebot_version,
            "napcat": self.state.napcat_version,
            "model": self.settings.model,
            "search": (
                "deepseek"
                if self.settings.deepseek_search_enabled
                else "disabled"
            ),
            "recorded_at": time.time(),
        }
        await self.repository.set_setting(
            "runtime_versions",
            json.dumps(versions, ensure_ascii=False, separators=(",", ":")),
        )


_runtime: BotRuntime | None = None


def _get_runtime() -> BotRuntime:
    if _runtime is None:
        raise RuntimeError("机器人运行时尚未初始化")
    return _runtime


driver = nonebot.get_driver()


@driver.on_startup
async def _startup() -> None:
    global _runtime
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("必须使用 Python 3.12 运行本项目")
    root = Path(__file__).resolve().parents[2]
    settings = load_settings(root)
    _runtime = BotRuntime(settings)
    await _runtime.start()


@driver.on_shutdown
async def _shutdown() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.stop()
        _runtime = None


@driver.on_bot_connect
async def _bot_connect(bot: Bot) -> None:
    if _runtime is None:
        logger.warning("OneBot 连接时机器人运行时尚未初始化，忽略本次回调")
        return
    await _runtime.mark_connected(bot)


@driver.on_bot_disconnect
async def _bot_disconnect(bot: Bot) -> None:
    del bot
    if _runtime is None:
        return
    await _runtime.mark_disconnected()


message_handler = on_message(priority=10, block=False)


@message_handler.handle()
async def _handle_message(bot: Bot, event: MessageEvent) -> None:
    if int(event.get_user_id()) == int(event.self_id):
        return
    runtime = _get_runtime()
    if runtime.application is None:
        return
    group_id = (
        int(event.group_id)
        if isinstance(event, GroupMessageEvent)
        else None
    )
    content = event.get_plaintext()
    has_image = group_id is not None and any(
        segment.type == "image" for segment in event.message
    )
    onebot_message_id = int(event.message_id)
    to_me = isinstance(event, GroupMessageEvent) and event.is_tome()
    is_reply_to_bot = False
    if group_id is not None and isinstance(event, GroupMessageEvent):
        for segment in event.message:
            if segment.type == "reply":
                try:
                    reply_id = int(segment.data["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                is_reply_to_bot = (
                    await runtime.repository.was_bot_message(group_id, reply_id)
                    if runtime.repository is not None
                    else False
                )
                break
    is_group_admin = (
        isinstance(event, GroupMessageEvent)
        and getattr(event.sender, "role", None) in {"admin", "owner"}
    )
    incoming = IncomingMessage(
        user_id=int(event.get_user_id()),
        nickname=(
            getattr(event.sender, "card", None)
            or getattr(event.sender, "nickname", None)
            or str(event.get_user_id())
        ),
        content=content,
        group_id=group_id,
        onebot_message_id=onebot_message_id,
        to_me=to_me,
        is_reply_to_bot=is_reply_to_bot,
        is_group_admin=is_group_admin,
        is_owner=int(event.get_user_id()) == runtime.settings.owner_qq,
        has_image=has_image,
    )
    try:
        await runtime.application.process(incoming)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "处理 QQ 消息失败",
            extra={"group_id": group_id, "message_id": onebot_message_id},
        )

