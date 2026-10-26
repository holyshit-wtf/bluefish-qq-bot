from __future__ import annotations

import asyncio

import httpx
import pytest

from services.llm import DeepSeekClient, ModelFailureError


@pytest.mark.asyncio
async def test_deepseek_retries_5xx_once_and_records_usage(
    settings, repository, monkeypatch
):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "重试成功"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekClient(settings, repository)
    result = await client.chat("系统", "用户")
    assert result.content == "重试成功"
    assert attempts == 2
    row = await repository.db.fetchone(
        "SELECT input_tokens, output_tokens, success FROM api_usage"
    )
    assert row["input_tokens"] == 20
    assert row["output_tokens"] == 10
    assert row["success"] == 1


@pytest.mark.asyncio
async def test_deepseek_final_network_failure_is_silent_and_audited(
    settings, repository, monkeypatch
):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("模拟断网", request=request)

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekClient(settings, repository)
    with pytest.raises(ModelFailureError):
        await client.chat("系统", "用户")
    assert attempts == 2
    row = await repository.db.fetchone(
        "SELECT success FROM api_usage ORDER BY id DESC LIMIT 1"
    )
    assert row["success"] == 0


@pytest.mark.asyncio
async def test_model_concurrency_is_bounded_to_two(
    settings, repository, monkeypatch
):
    active = 0
    maximum_active = 0
    guard = asyncio.Lock()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        async with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.03)
        async with guard:
            active -= 1
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "并发成功"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekClient(settings, repository)
    results = await asyncio.gather(
        *(client.chat("系统", f"用户{index}") for index in range(6))
    )
    assert len(results) == 6
    assert maximum_active == 2
