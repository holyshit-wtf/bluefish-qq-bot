from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from core.config import Settings
from core.time import day_key, day_start_timestamp
from services.commands import (
    CommandContext,
    CommandPlan,
    CommandService,
    RuntimeState,
)
from services.llm import DeepSeekClient, ModelBusyError, ModelFailureError
from services.memory import MemoryService
from services.personality import (
    affection_level,
    build_group_system_prompt,
    build_private_system_prompt,
    build_search_unavailable_reply,
    growth_stage,
    persona_probability_factor,
)
from services.safety import check_group_message, sanitize_reply
from services.search import (
    SearchIntent,
    SearchResult,
    SearchService,
    build_search_query,
    classify_search_intent,
    filter_fresh_results,
    format_current_time,
    format_search_evidence,
)
from storage.repository import Repository, anonymous_user_key

logger = logging.getLogger(__name__)

SendCallback = Callable[
    [str, int | None, int | None, int | None], Awaitable[int | None]
]


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    user_id: int
    nickname: str
    content: str
    group_id: int | None
    onebot_message_id: int
    to_me: bool
    is_reply_to_bot: bool
    is_group_admin: bool
    is_owner: bool
    has_image: bool = False


@dataclass(frozen=True, slots=True)
class ProcessResult:
    handled: bool
    replied: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    requested: bool
    intent: SearchIntent = SearchIntent()
    results: tuple[SearchResult, ...] = ()
    status: str = "not_needed"

    @property
    def max_length(self) -> int:
        return 500 if self.requested else 300

    @property
    def prompt_mode(self) -> str:
        if self.results:
            return "grounded"
        if self.requested:
            return "fallback"
        return "none"

    @property
    def use_unavailable_reply(self) -> bool:
        return (
            self.requested
            and not self.results
            and self.intent.time_sensitive
        )


class BlueFishApplication:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        memory: MemoryService,
        model: DeepSeekClient,
        runtime: RuntimeState,
        send: SendCallback,
        *,
        search: SearchService | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.memory = memory
        self.model = model
        self.runtime = runtime
        self.send = send
        self.search = search
        self.commands = CommandService(repository, settings, runtime)
        self.rng = rng or random.Random()
        self._pending_private: dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._group_locks_guard = asyncio.Lock()
        self._group_pending: dict[int, int] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._budget_lock = asyncio.Lock()

    async def process(self, incoming: IncomingMessage) -> ProcessResult:
        if incoming.group_id is not None:
            queue_full = False
            async with self._group_locks_guard:
                pending = self._group_pending.get(incoming.group_id, 0)
                if pending >= self.settings.model_queue_limit:
                    queue_full = True
                else:
                    self._group_pending[incoming.group_id] = pending + 1
                    lock = self._group_locks.setdefault(
                        incoming.group_id, asyncio.Lock()
                    )
            if queue_full:
                await self.repository.audit(
                    "group_queue_full",
                    "dropped",
                    group_id=incoming.group_id,
                    user_id=incoming.user_id,
                )
                return ProcessResult(False, reason="group_queue_full")
            try:
                async with lock:
                    return await self._process_group(incoming)
            finally:
                async with self._group_locks_guard:
                    current = self._group_pending.get(incoming.group_id, 1)
                    if current <= 1:
                        self._group_pending.pop(incoming.group_id, None)
                    else:
                        self._group_pending[incoming.group_id] = current - 1
        return await self._process_private(incoming)

    async def stop(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _process_group(self, incoming: IncomingMessage) -> ProcessResult:
        group_id = incoming.group_id
        assert group_id is not None
        group = await self.repository.get_group(group_id)
        if not group or not group["enabled"]:
            return ProcessResult(False, reason="group_not_enabled")
        if await self.repository.is_globally_paused():
            return ProcessResult(False, reason="globally_paused")

        now_ts = time.time()
        now = datetime.fromtimestamp(now_ts, self.settings.timezone)
        context = CommandContext(
            user_id=incoming.user_id,
            nickname=incoming.nickname,
            group_id=group_id,
            is_group_admin=incoming.is_group_admin,
            is_owner=incoming.is_owner,
        )
        plan = self.commands.prepare(incoming.content, context)
        terms = await self.repository.list_blocked_terms(group_id)
        normalized_terms = [self._normalized_term(term, group_id) for term in terms]
        safety = check_group_message(incoming.content, normalized_terms)
        delete_blocked_term = bool(
            plan
            and plan.kind == "blocked_删除"
            and incoming.is_group_admin
        )
        if not safety.allowed and not delete_blocked_term:
            await self.repository.audit(
                "blocked_message",
                "rejected",
                group_id=group_id,
                user_id=incoming.user_id,
                metadata={"rule": "blocked_term"},
                timestamp=now_ts,
            )
            return ProcessResult(False, reason="blocked_term")

        hunger, mood = await self.repository.evolve_group_state(
            group_id, now_ts
        )
        group = dict(group)
        group["hunger"] = hunger
        group["mood"] = mood

        await self.repository.touch_binding(
            incoming.user_id,
            group_id,
            incoming.onebot_message_id,
            incoming.nickname,
        )

        if plan is not None:
            return await self._execute_command(
                plan, context, group_id=group_id,
                quote_message_id=incoming.onebot_message_id,
            )

        remembered_content = incoming.content.strip()
        if not remembered_content and incoming.has_image:
            remembered_content = "[图片]"
        if remembered_content:
            await self.memory.remember_group(
                group_id,
                incoming.user_id,
                incoming.nickname,
                "user",
                remembered_content,
                incoming.onebot_message_id,
                created_at=now_ts,
            )

        if group["games_enabled"]:
            await self.repository.reward_chat(
                group_id,
                incoming.user_id,
                day_key(now, self.settings.timezone),
                now_ts,
            )

        must_answer = bool(
            group["must_answer"]
            and (incoming.to_me or incoming.is_reply_to_bot)
        )
        if not must_answer and not await self._should_randomly_insert(
            group, now_ts
        ):
            return ProcessResult(True, reason="not_triggered")
        if not remembered_content:
            if must_answer:
                await self.repository.audit(
                    "empty_required_message",
                    "skipped",
                    group_id=group_id,
                    user_id=incoming.user_id,
                )
            return ProcessResult(True, reason="empty_content")

        reservation = await self._reserve(
            "group_chat", group_id, now_ts, now
        )
        if reservation is None:
            return ProcessResult(False, reason="send_limit")
        send_finished = False
        try:
            recent, merged = await self.memory.context_for_group(
                group_id, incoming.content
            )
            search_outcome = await self._collect_search(
                remembered_content,
                [
                    str(row["content"])
                    for row in recent
                    if row["role"] == "user"
                ],
                now=now,
                group_id=group_id,
                user_id=incoming.user_id,
                allowed=must_answer,
            )
            status = await self.repository.game_status(
                group_id,
                incoming.user_id,
                day_key(now, self.settings.timezone),
                now_ts,
            )
            profile = await self.repository.get_profile(
                group_id, incoming.user_id
            )
            memes = await self.repository.list_memes(group_id, limit=10)
            system_prompt = build_group_system_prompt(
                str(group["persona"]),
                affection=float(status.get("affection", 0)),
                exp=int(status.get("exp", 0)),
                hunger=float(status.get("hunger", group["hunger"])),
                mood=float(status.get("mood", group["mood"])),
                current_time=format_current_time(
                    now, self.settings.timezone_name
                ),
                max_length=search_outcome.max_length,
                search_mode=search_outcome.prompt_mode,
                market_search=search_outcome.intent.market,
                time_sensitive_search=search_outcome.intent.time_sensitive,
            )
            search_evidence = (
                format_search_evidence(search_outcome.results, now=now)
                if search_outcome.results
                else None
            )
            user_prompt = self._group_user_prompt(
                incoming,
                group,
                recent,
                merged,
                profile,
                memes,
                remembered_content,
                search_evidence,
            )
            if search_outcome.use_unavailable_reply:
                raw_reply = build_search_unavailable_reply(
                    str(group["persona"]),
                    market=search_outcome.intent.market,
                )
            else:
                result = await self.model.chat(
                    system_prompt,
                    user_prompt,
                    call_type="group_chat",
                )
                raw_reply = result.content
            safe_text = await self._prepare_reply_text(
                raw_reply, search_outcome.max_length
            )
            if search_outcome.intent.market:
                safe_text = self._ensure_market_notice(
                    safe_text, search_outcome.max_length
                )
            if not safe_text:
                raise ModelFailureError("模型输出无有效正文")
            if not check_group_message(safe_text, normalized_terms).allowed:
                await self.repository.audit(
                    "blocked_output",
                    "rejected",
                    group_id=group_id,
                    user_id=incoming.user_id,
                    metadata={"rule": "blocked_term"},
                )
                raise ModelFailureError("模型输出命中禁忌词")
            sent_id = await self._deliver(
                safe_text, group_id, None, incoming.onebot_message_id
            )
            await self.repository.finish_send(
                reservation,
                success=True,
                onebot_message_id=sent_id,
                sent_at=time.time(),
            )
            send_finished = True
            await self.memory.remember_group(
                group_id,
                None,
                "蓝色大肥鱼",
                "bot",
                safe_text,
                sent_id,
                created_at=time.time(),
            )
            logger.info(
                "群聊消息已发送 | group=%s scene=group_chat",
                group_id,
            )
            self._schedule_budget_check()
            return ProcessResult(True, replied=True)
        except Exception as exc:
            if not send_finished:
                await self.repository.finish_send(reservation, success=False)
            await self.repository.audit(
                "group_chat",
                "failed",
                group_id=group_id,
                user_id=incoming.user_id,
                metadata={"error": type(exc).__name__},
            )
            logger.exception("群聊生成或发送失败", extra={"group_id": group_id})
            return ProcessResult(False, reason="model_or_send_failure")

    async def _process_private(
        self, incoming: IncomingMessage
    ) -> ProcessResult:
        async with self._pending_lock:
            pending = self._pending_private.get(incoming.user_id, 0)
            if pending >= 3:
                await self.repository.audit(
                    "private_queue_full",
                    "dropped",
                    user_id=incoming.user_id,
                )
                return ProcessResult(False, reason="private_queue_full")
            self._pending_private[incoming.user_id] = pending + 1
        try:
            return await self._process_private_inner(incoming)
        finally:
            async with self._pending_lock:
                current = self._pending_private.get(incoming.user_id, 1)
                if current <= 1:
                    self._pending_private.pop(incoming.user_id, None)
                else:
                    self._pending_private[incoming.user_id] = current - 1

    async def _process_private_inner(
        self, incoming: IncomingMessage
    ) -> ProcessResult:
        now_ts = time.time()
        now = datetime.fromtimestamp(now_ts, self.settings.timezone)
        binding = await self.repository.get_binding(incoming.user_id)
        group_id = int(binding["group_id"]) if binding else None
        private_group: dict[str, object] | None = None
        if group_id is not None:
            private_group = await self.repository.get_group(group_id)
            if private_group is not None:
                hunger, mood = await self.repository.evolve_group_state(
                    group_id, now_ts
                )
                private_group = dict(private_group)
                private_group["hunger"] = hunger
                private_group["mood"] = mood
        context = CommandContext(
            user_id=incoming.user_id,
            nickname=incoming.nickname,
            group_id=None,
            is_group_admin=False,
            is_owner=incoming.is_owner,
        )
        plan = self.commands.prepare(incoming.content, context)
        paused = await self.repository.is_globally_paused()
        if paused and not (
            incoming.is_owner and plan and plan.kind.startswith("owner_")
        ):
            return ProcessResult(False, reason="globally_paused")

        normalized_terms: list[str] = []
        if group_id is not None:
            terms = await self.repository.list_blocked_terms(group_id)
            normalized_terms = [self._normalized_term(term, group_id) for term in terms]
            safety = check_group_message(incoming.content, normalized_terms)
            if not safety.allowed:
                await self.repository.audit(
                    "blocked_private_message",
                    "rejected",
                    group_id=group_id,
                    user_id=incoming.user_id,
                    metadata={"rule": "blocked_term"},
                    timestamp=now_ts,
                )
                return ProcessResult(False, reason="blocked_term")

        if plan is not None:
            return await self._execute_command(
                plan,
                context,
                group_id=None,
                quote_message_id=None,
                private=True,
            )

        if not incoming.content.strip():
            return ProcessResult(True, reason="empty_private_content")
        reservation = await self._reserve(
            "private_chat", group_id, now_ts, now
        )
        if reservation is None:
            return ProcessResult(False, reason="send_limit")
        send_finished = False
        try:
            history: list[dict[str, str]] = []
            if group_id is not None:
                group = private_group
                if not group:
                    await self.repository.finish_send(reservation, success=False)
                    return ProcessResult(False, reason="binding_group_missing")
                history = await self.memory.private_history(
                    group_id, incoming.user_id
                )
                await self.memory.remember_private(
                    group_id,
                    incoming.user_id,
                    "user",
                    incoming.content,
                    created_at=now_ts,
                )
                status = await self.repository.game_status(
                    group_id,
                    incoming.user_id,
                    day_key(now, self.settings.timezone),
                    now_ts,
                )
                profile = await self.repository.get_profile(
                    group_id, incoming.user_id
                )
                memes = await self.repository.list_memes(group_id, limit=10)
            else:
                group = None

            search_outcome = await self._collect_search(
                incoming.content,
                [
                    item["content"]
                    for item in history
                    if item.get("role") == "user"
                ],
                now=now,
                group_id=group_id,
                user_id=incoming.user_id,
                allowed=True,
            )
            search_evidence = (
                format_search_evidence(search_outcome.results, now=now)
                if search_outcome.results
                else None
            )
            current_time = format_current_time(
                now, self.settings.timezone_name
            )
            if group is not None:
                system_prompt = build_private_system_prompt(
                    str(group["persona"]),
                    affection=float(status.get("affection", 0)),
                    exp=int(status.get("exp", 0)),
                    hunger=float(status.get("hunger", group["hunger"])),
                    mood=float(status.get("mood", group["mood"])),
                    current_time=current_time,
                    max_length=search_outcome.max_length,
                    search_mode=search_outcome.prompt_mode,
                    market_search=search_outcome.intent.market,
                    time_sensitive_search=search_outcome.intent.time_sensitive,
                )
                user_prompt = self._private_user_prompt(
                    incoming,
                    group,
                    group_id,
                    profile,
                    memes,
                    search_evidence,
                )
            else:
                system_prompt = (
                    build_private_system_prompt(
                        "标准贪吃鲸娘",
                        affection=0,
                        exp=0,
                        hunger=80,
                        mood=60,
                        current_time=current_time,
                        max_length=search_outcome.max_length,
                        search_mode=search_outcome.prompt_mode,
                        market_search=search_outcome.intent.market,
                        time_sensitive_search=search_outcome.intent.time_sensitive,
                    )
                    + "\n用户尚未绑定群。本次只能做不持久化的普通寒暄，"
                    "不要假装记得对方，也不要创建养成档案。"
                )
                user_prompt = incoming.content[:4000]
                if search_evidence:
                    user_prompt = f"{user_prompt}\n\n{search_evidence}"
            if search_outcome.use_unavailable_reply:
                raw_reply = build_search_unavailable_reply(
                    str(group["persona"]) if group else "标准贪吃鲸娘",
                    market=search_outcome.intent.market,
                )
            else:
                result = await self.model.chat(
                    system_prompt,
                    user_prompt,
                    history=history,
                    call_type="private_chat",
                )
                raw_reply = result.content
            safe_text = await self._prepare_reply_text(
                raw_reply, search_outcome.max_length
            )
            if search_outcome.intent.market:
                safe_text = self._ensure_market_notice(
                    safe_text, search_outcome.max_length
                )
            if not safe_text:
                raise ModelFailureError("模型输出无有效正文")
            if not check_group_message(safe_text, normalized_terms).allowed:
                await self.repository.audit(
                    "blocked_output",
                    "rejected",
                    group_id=group_id,
                    user_id=incoming.user_id,
                    metadata={"rule": "blocked_term"},
                )
                raise ModelFailureError("模型输出命中禁忌词")
            if group_id is None:
                reminder = "先在已启用的群里发消息，就能绑定群并查看养成状态啦。"
                market_notice = (
                    "公开行情可能延迟，也不构成投资建议。"
                    if search_outcome.intent.market
                    else ""
                )
                if market_notice and market_notice in safe_text:
                    safe_text = safe_text.replace(market_notice, "").rstrip()
                reserved = len(reminder) + 1
                if market_notice:
                    reserved += len(market_notice) + 1
                model_budget = search_outcome.max_length - reserved
                safe_text = safe_text[:model_budget].rstrip()
                safe_text = (
                    f"{safe_text} {reminder}".strip()
                    if safe_text
                    else reminder
                )
                if market_notice:
                    safe_text = f"{safe_text} {market_notice}".strip()
                safe_text = sanitize_reply(
                    safe_text, search_outcome.max_length
                )
            sent_id = await self._deliver(
                safe_text, None, incoming.user_id, None
            )
            await self.repository.finish_send(
                reservation,
                success=True,
                onebot_message_id=sent_id,
                sent_at=time.time(),
            )
            send_finished = True
            if group_id is not None:
                await self.memory.remember_private(
                    group_id,
                    incoming.user_id,
                    "bot",
                    safe_text,
                    created_at=time.time(),
                )
            logger.info(
                "私聊消息已发送 | scene=private_chat"
            )
            self._schedule_budget_check()
            return ProcessResult(True, replied=True)
        except Exception as exc:
            if not send_finished:
                await self.repository.finish_send(reservation, success=False)
            await self.repository.audit(
                "private_chat",
                "failed",
                group_id=group_id,
                user_id=incoming.user_id,
                metadata={"error": type(exc).__name__},
            )
            logger.exception("私聊生成或发送失败")
            return ProcessResult(False, reason="model_or_send_failure")

    async def _execute_command(
        self,
        plan: CommandPlan,
        context: CommandContext,
        *,
        group_id: int | None,
        quote_message_id: int | None,
        private: bool = False,
    ) -> ProcessResult:
        now_ts = time.time()
        now = datetime.fromtimestamp(now_ts, self.settings.timezone)
        if plan.kind == "permission_error":
            await self.repository.audit(
                "permission_denied",
                "denied",
                group_id=group_id,
                user_id=context.user_id,
                metadata={"command": plan.kind},
            )
            logger.warning(
                "命令权限拒绝 | group=%s command=%s",
                group_id,
                plan.kind,
            )
        reservation = await self._reserve(
            "private_command" if private else "group_command",
            group_id,
            now_ts,
            now,
        )
        if reservation is None:
            return ProcessResult(False, reason="send_limit")
        try:
            reply = sanitize_reply(await self.commands.execute(plan, context), 300)
            if not reply:
                raise RuntimeError("命令回复为空")
            sent_id = await self._deliver(
                reply,
                group_id,
                context.user_id if private else None,
                quote_message_id,
            )
            await self.repository.finish_send(
                reservation,
                success=True,
                onebot_message_id=sent_id,
                sent_at=time.time(),
            )
            logger.info(
                "命令回执已发送 | group=%s command=%s",
                group_id,
                plan.kind,
            )
            return ProcessResult(True, replied=True)
        except Exception as exc:
            await self.repository.finish_send(reservation, success=False)
            await self.repository.audit(
                "command",
                "failed",
                group_id=group_id,
                user_id=context.user_id,
                metadata={"command": plan.kind, "error": type(exc).__name__},
            )
            logger.exception("命令执行失败", extra={"command": plan.kind})
            return ProcessResult(False, reason="command_failure")

    async def _should_randomly_insert(
        self, group: dict[str, object], now: float
    ) -> bool:
        last = await self.repository.last_group_send_at(int(group["group_id"]))
        if last is not None and now - last <= int(group["cooldown_seconds"]):
            return False
        probability = float(group["base_probability"]) / 100
        probability += (float(group["mood"]) - 50) / 50 * 0.05
        probability = min(1.0, max(0.0, probability))
        probability *= persona_probability_factor(str(group["persona"]))
        probability = min(1.0, probability)
        return self.rng.random() < probability

    async def _reserve(
        self,
        scene: str,
        group_id: int | None,
        now_ts: float,
        now: datetime,
    ) -> int | None:
        reservation = await self.repository.reserve_send(
            scene,
            group_id,
            now_ts,
            day_start_timestamp(now, self.settings.timezone),
            per_minute=self.settings.send_limit_per_minute,
            per_hour=self.settings.send_limit_per_hour,
            per_day=self.settings.send_limit_per_day,
        )
        if reservation is None:
            await self.repository.audit(
                "send_limit",
                "rejected",
                group_id=group_id,
                metadata={"scene": scene},
                timestamp=now_ts,
            )
            logger.warning(
                "发送限流 | scene=%s group=%s",
                scene,
                group_id,
            )
        return reservation

    async def _deliver(
        self,
        text: str,
        group_id: int | None,
        user_id: int | None,
        quote_message_id: int | None,
    ) -> int | None:
        return await self.send(text, group_id, user_id, quote_message_id)

    async def _prepare_reply_text(
        self, raw: str, max_length: int = 300
    ) -> str:
        safe = sanitize_reply(raw, 10_000)
        if len(safe) > max_length:
            try:
                compressed = await self.model.compress(safe, max_length)
                safe = sanitize_reply(compressed.content, max_length)
            except (ModelBusyError, ModelFailureError):
                safe = sanitize_reply(safe, max_length)
        return sanitize_reply(safe, max_length)

    def _ensure_market_notice(self, text: str, max_length: int) -> str:
        notice = "公开行情可能延迟，也不构成投资建议。"
        if notice in text:
            return text
        budget = max_length - len(notice) - 1
        body = text[:budget].rstrip() if budget > 0 else ""
        return sanitize_reply(
            f"{body} {notice}".strip(),
            max_length,
        )

    async def _collect_search(
        self,
        content: str,
        prior_user_messages: list[str],
        *,
        now: datetime,
        group_id: int | None,
        user_id: int,
        allowed: bool,
    ) -> SearchOutcome:
        if (
            not allowed
            or not self.settings.deepseek_search_enabled
            or self.search is None
        ):
            return SearchOutcome(requested=False)
        intent = classify_search_intent(content)
        if not intent.needed:
            return SearchOutcome(requested=False, intent=intent)
        primary_query = build_search_query(
            content,
            prior_user_messages,
            now=now,
        )
        if not primary_query:
            return SearchOutcome(requested=False, intent=intent)
        compact_query = build_search_query(
            content,
            prior_user_messages,
            now=now,
            compact=True,
        )
        queries = [primary_query]
        if compact_query and compact_query != primary_query:
            queries.append(compact_query)
        attempts = 0
        raw_count = 0
        try:
            results: list[SearchResult] = []
            for query in queries:
                attempts += 1
                raw_results = await self.search.search(
                    query,
                    ori_question=content[:128],
                )
                raw_count += len(raw_results)
                results = filter_fresh_results(
                    raw_results,
                    now=now,
                    intent=intent,
                )
                if results:
                    break
        except Exception as exc:
            await self.repository.audit(
                "web_search",
                "failed",
                group_id=group_id,
                user_id=user_id,
                metadata={
                    "intent": "market" if intent.market else "knowledge",
                    "error": type(exc).__name__,
                    "attempts": attempts,
                },
                timestamp=now.timestamp(),
            )
            logger.warning(
                "搜索调用失败 | group=%s intent=%s error=%s",
                group_id,
                "market" if intent.market else "knowledge",
                type(exc).__name__,
            )
            return SearchOutcome(
                requested=True,
                intent=intent,
                status="failed",
            )
        status = "success" if results else "empty"
        await self.repository.audit(
            "web_search",
            status,
            group_id=group_id,
            user_id=user_id,
            metadata={
                "intent": "market" if intent.market else "knowledge",
                "results": len(results),
                    "raw_results": raw_count,
                "attempts": attempts,
            },
            timestamp=now.timestamp(),
        )
        logger.info(
            "搜索调用完成 | group=%s intent=%s results=%d raw=%d attempts=%d",
            group_id,
            "market" if intent.market else "knowledge",
            len(results),
            raw_count,
            attempts,
        )
        return SearchOutcome(
            requested=True,
            intent=intent,
            results=tuple(results),
            status=status,
        )

    def _group_user_prompt(
        self,
        incoming: IncomingMessage,
        group: dict[str, object],
        recent: list[dict[str, object]],
        merged: list[dict[str, object]],
        profile: dict[str, object] | None,
        memes: list[dict[str, object]],
        current_content: str,
        search_evidence: str | None = None,
    ) -> str:
        recent_lines = []
        for row in recent[-40:]:
            role = str(row["role"])
            speaker = str(row["nickname"]) if role == "user" else "你"
            recent_lines.append(f"{speaker}: {row['content']}")
        memory_lines = []
        recent_ids = {int(row["id"]) for row in recent}
        for row in merged:
            if int(row["id"]) in recent_ids:
                continue
            memory_lines.append(
                f"{row['nickname']}: {row['content']}"
            )
        profile_line = "暂无群友画像。"
        if profile:
            profile_line = (
                f"偏好：{profile.get('preferences') or '暂无'}；"
                f"互动风格：{profile.get('interaction_style') or '暂无'}；"
                f"摘要：{profile.get('summary') or '暂无'}"
            )
        meme_line = "；".join(str(row["text"]) for row in memes) or "暂无"
        return "\n".join(
            (
                f"当前用户匿名键：{anonymous_user_key(int(group['group_id']), incoming.user_id)}",
                f"当前发言人昵称：{incoming.nickname}",
                f"群配置：人格 {group['persona']}，基础插话概率 "
                f"{float(group['base_probability']):.0f}%，随机冷却 "
                f"{int(group['cooldown_seconds'])} 秒，"
                f"必答 {'开' if group['must_answer'] else '关'}，"
                f"养成 {'开' if group['games_enabled'] else '关'}。",
                "群友画像：" + profile_line,
                "相关群梗：" + meme_line,
                "最近群聊：",
                "\n".join(recent_lines) or "暂无",
                "检索补充记忆：",
                "\n".join(memory_lines) or "暂无",
                search_evidence or "",
                f"请回复当前发言人这条消息：{current_content[:4000]}",
            )
        )

    def _private_user_prompt(
        self,
        incoming: IncomingMessage,
        group: dict[str, object],
        group_id: int,
        profile: dict[str, object] | None,
        memes: list[dict[str, object]],
        search_evidence: str | None = None,
    ) -> str:
        profile_text = "暂无画像"
        if profile:
            level = affection_level(float(profile.get("affection", 0)))
            stage = growth_stage(int(profile.get("exp", 0)))
            profile_text = (
                f"{profile.get('summary') or '暂无'}；"
                f"偏好 {profile.get('preferences') or '暂无'}；"
                f"互动风格 {profile.get('interaction_style') or '暂无'}；"
                f"印象 {level}，成长 {stage.level} 级"
            )
        meme_text = "；".join(str(row["text"]) for row in memes) or "暂无"
        evidence_text = f"{search_evidence}\n" if search_evidence else ""
        return (
            f"当前用户匿名键：{anonymous_user_key(group_id, incoming.user_id)}\n"
            f"私聊用户昵称：{incoming.nickname}；群昵称："
            f"{profile.get('nickname') if profile else incoming.nickname}\n"
            f"绑定群配置：人格 {group['persona']}，"
            f"养成 {'开' if group['games_enabled'] else '关'}\n"
            f"绑定群画像：{profile_text}\n绑定群群梗：{meme_text}\n"
            f"{evidence_text}"
            f"私聊消息：{incoming.content[:4000]}"
        )

    async def maybe_send_budget_alert(self) -> None:
        async with self._budget_lock:
            await self._maybe_send_budget_alert_locked()

    async def _maybe_send_budget_alert_locked(self) -> None:
        now = datetime.now(self.settings.timezone)
        month_key = now.strftime("%Y-%m")
        already = await self.repository.get_setting("budget_alert_month", "")
        if already == month_key:
            return
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        usage = await self.repository.usage_summary(month_start)
        if float(usage["cost"]) < self.settings.monthly_budget_cny:
            return
        reservation = await self.repository.reserve_send(
            "budget_alert",
            None,
            time.time(),
            day_start_timestamp(now, self.settings.timezone),
            per_minute=self.settings.send_limit_per_minute,
            per_hour=self.settings.send_limit_per_hour,
            per_day=self.settings.send_limit_per_day,
        )
        if reservation is None:
            await self.repository.audit("budget_alert", "send_limit", actor="system")
            return
        text = (
            f"本月模型成本估算 ¥{float(usage['cost']):.2f}，"
            f"已达到提醒值 ¥{self.settings.monthly_budget_cny:.2f}。"
            "服务不会自动停止。"
        )
        try:
            sent_id = await self._deliver(
                text, None, self.settings.owner_qq, None
            )
            await self.repository.finish_send(
                reservation,
                success=True,
                onebot_message_id=sent_id,
                sent_at=time.time(),
            )
            await self.repository.set_setting("budget_alert_month", month_key)
            await self.repository.audit("budget_alert", "sent", actor="system")
        except Exception:
            await self.repository.finish_send(reservation, success=False)
            await self.repository.audit("budget_alert", "failed", actor="system")
            logger.exception("预算提醒发送失败")

    def _schedule_budget_check(self) -> None:
        task = asyncio.create_task(self._run_budget_check())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_budget_check(self) -> None:
        try:
            await self.maybe_send_budget_alert()
        except Exception:
            logger.exception("预算提醒检查失败")

    def _normalized_term(self, term: str, group_id: int) -> str:
        from services.safety import normalize_blocked_term

        return normalize_blocked_term(term)

