from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from core.config import Settings
from storage.repository import Repository

logger = logging.getLogger(__name__)


class ModelBusyError(RuntimeError):
    pass


class ModelFailureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelResult:
    content: str
    input_tokens: int
    output_tokens: int


class DeepSeekClient:
    def __init__(self, settings: Settings, repository: Repository) -> None:
        self.settings = settings
        self.repository = repository
        self._semaphore = asyncio.Semaphore(settings.max_model_concurrency)
        self._waiting = 0
        self._waiting_lock = asyncio.Lock()

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        call_type: str = "chat",
        history: list[dict[str, str]] | None = None,
        max_tokens: int = 320,
    ) -> ModelResult:
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-40:])
        messages.append({"role": "user", "content": user_prompt})
        return await self._request(
            messages,
            call_type=call_type,
            max_tokens=max_tokens,
            json_mode=False,
        )

    async def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        call_type: str = "daily_extract",
    ) -> dict[str, Any]:
        result = await self._request(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            call_type=call_type,
            max_tokens=1800,
            json_mode=True,
        )
        try:
            value = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise ModelFailureError("模型返回的 JSON 无法解析") from exc
        if not isinstance(value, dict):
            raise ModelFailureError("模型 JSON 顶层必须是对象")
        return value

    async def compress(self, original: str, max_length: int = 300) -> ModelResult:
        return await self.chat(
            "把给定群聊回复压缩成自然、完整的纯文本，保留原意，"
            f"最多 {max_length} 个字符。"
            "只输出压缩后的正文，不使用格式或链接。",
            original,
            call_type="compress",
            max_tokens=max(220, min(500, max_length + 20)),
        )

    async def _request(
        self,
        messages: list[dict[str, str]],
        *,
        call_type: str,
        max_tokens: int,
        json_mode: bool,
    ) -> ModelResult:
        async with self._waiting_lock:
            if self._waiting >= self.settings.model_queue_limit:
                raise ModelBusyError("模型请求等待队列已满")
            self._waiting += 1
        acquired = False
        try:
            await self._semaphore.acquire()
            acquired = True
            async with self._waiting_lock:
                self._waiting -= 1
            return await self._request_with_retry(
                messages,
                call_type=call_type,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        finally:
            if acquired:
                self._semaphore.release()
            else:
                async with self._waiting_lock:
                    self._waiting -= 1

    async def _request_with_retry(
        self,
        messages: list[dict[str, str]],
        *,
        call_type: str,
        max_tokens: int,
        json_mode: bool,
    ) -> ModelResult:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await self._request_once(
                    messages,
                    call_type=call_type,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    retryable=True,
                )
            except _RetryableModelError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                break
            except _NonRetryableModelError as exc:
                last_error = exc
                break
        await self.repository.record_api_usage(
            self.settings.model,
            0,
            0,
            0,
            call_type,
            False,
            time.time(),
        )
        logger.warning(
            "模型调用最终失败 | type=%s",
            call_type,
        )
        raise ModelFailureError(str(last_error or "模型调用失败"))

    async def _request_once(
        self,
        messages: list[dict[str, str]],
        *,
        call_type: str,
        max_tokens: int,
        json_mode: bool,
        retryable: bool,
    ) -> ModelResult:
        del retryable
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.8,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.settings.deepseek_base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise _RetryableModelError(type(exc).__name__) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableModelError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise _NonRetryableModelError(f"HTTP {response.status_code}")
        try:
            body = response.json()
            content = str(body["choices"][0]["message"]["content"]).strip()
            usage = body.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _RetryableModelError("响应结构异常") from exc
        if not content:
            raise _RetryableModelError("模型返回空正文")
        cost = (
            input_tokens
            * self.settings.deepseek_input_price_per_million
            / 1_000_000
            + output_tokens
            * self.settings.deepseek_output_price_per_million
            / 1_000_000
        )
        await self.repository.record_api_usage(
            self.settings.model,
            input_tokens,
            output_tokens,
            cost,
            call_type,
            True,
            time.time(),
        )
        logger.info(
            "模型调用成功 | type=%s input=%d output=%d",
            call_type,
            input_tokens,
            output_tokens,
        )
        return ModelResult(content, input_tokens, output_tokens)


class _RetryableModelError(RuntimeError):
    pass


class _NonRetryableModelError(RuntimeError):
    pass

