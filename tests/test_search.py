from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from services.search import (
    DeepSeekSearchClient,
    SearchFailureError,
    SearchIntent,
    SearchResult,
    build_search_query,
    classify_search_intent,
    filter_fresh_results,
)


def result(
    *,
    title: str = "攻略",
    summary: str = "先完成前置任务，再沿右侧道路前进。",
    date: str | None = None,
) -> SearchResult:
    return SearchResult(
        title=title,
        url="https://example.com/guide",
        snippet=summary,
        summary=summary,
        date=date,
        site_name="资料站",
        authority=3,
        rerank_score=2.1,
    )


def test_search_intent_and_follow_up_query():
    assert classify_search_intent("今天有什么科技新闻").needed
    assert classify_search_intent("今天股价多少").market
    assert classify_search_intent("给我一份游戏攻略").needed
    assert not classify_search_intent("你好").needed
    assert not classify_search_intent("今天星期几").needed
    assert not classify_search_intent("你饿吗").needed
    query = build_search_query(
        "第三关怎么打",
        ["最近在玩《星海遗迹》", "前面那关已经过了"],
    )
    assert "《星海遗迹》" in query
    assert "第三关怎么打" in query
    assert len(query) <= 128
    now = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    normalized = build_search_query(
        "赛尔号巅峰之战本周更新了什么内容",
        now=now,
    )
    compact = build_search_query(
        "赛尔号巅峰之战本周更新了什么内容",
        now=now,
        compact=True,
    )
    assert "更新内容" in normalized
    assert "本周" not in normalized
    assert "2026年10月" in normalized
    assert normalized != compact
    assert "2026年" not in compact
    market_query = build_search_query(
        "最近股市行情如何",
        now=now,
    )
    assert market_query == "A股 最新行情 2026年10月25日"
    market_compact = build_search_query(
        "最近股市行情如何",
        now=now,
        compact=True,
    )
    assert market_compact == "A股 最新行情"
    assert classify_search_intent("近期有什么科研咨询").needed
    assert classify_search_intent("聊聊生物医学").needed
    assert classify_search_intent("免疫治疗").needed
    assert (
        build_search_query("近期有什么科研咨询", now=now)
        == "科研资讯 2026年10月"
    )
    assert (
        build_search_query("聊聊生物医学", now=now)
        == "生物医学 最新研究进展 2026年10月"
    )
    assert (
        build_search_query("免疫治疗", now=now)
        == "免疫治疗 最新研究进展 2026年10月"
    )


@pytest.mark.asyncio
async def test_search_client_sends_expected_request_and_records_cost(
    settings, repository, monkeypatch
):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["payload"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {
                        "title": "星海遗迹第三关攻略",
                        "url": "https://example.com/guide",
                        "snippet": "右侧道路有隐藏宝箱。",
                        "summary": "先开启左侧机关，再沿右侧道路前进并取得宝箱。",
                        "site_name": "攻略站",
                        "date": "2026-10-25T10:00:00+08:00",
                        "authority": 3,
                        "rerank_score": 2.4,
                    },
                    {
                        "title": "无摘要",
                        "url": "https://example.com/empty",
                        "snippet": "",
                        "summary": "",
                        "rerank_score": 2.0,
                    },
                    {
                        "title": "低相关",
                        "url": "https://example.com/low",
                        "snippet": "无关内容",
                        "summary": "无关内容",
                        "rerank_score": 0.8,
                    },
                ]
            },
        )

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekSearchClient(settings, repository)
    results = await client.search(
        "星海遗迹 第三关攻略",
        ori_question="第三关怎么打",
    )

    assert len(results) == 1
    assert results[0].site_name == "攻略站"
    assert seen["url"].endswith("/v1/tools/search")
    assert seen["authorization"] == "Bearer test-key"
    payload = json.loads(str(seen["payload"]))
    assert payload["count"] == 5
    assert payload["need_content"] is False
    usage = await repository.db.fetchone(
        "SELECT estimated_cost_cny, success FROM api_usage "
        "WHERE call_type='web_search'"
    )
    assert usage["estimated_cost_cny"] == pytest.approx(0.01)
    assert usage["success"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 503])
async def test_search_retries_transient_status_once(
    settings, repository, monkeypatch, status
):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status, request=request)
        return httpx.Response(
            200,
            request=request,
            json={"results": []},
        )

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekSearchClient(settings, repository)
    results = await client.search("最新新闻")
    assert results == []
    assert attempts == 2
    usage = await repository.db.fetchone(
        "SELECT estimated_cost_cny, success FROM api_usage "
        "WHERE call_type='web_search'"
    )
    assert usage["estimated_cost_cny"] == 0
    assert usage["success"] == 1


@pytest.mark.asyncio
async def test_search_does_not_retry_payment_error(
    settings, repository, monkeypatch
):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(402, request=request)

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekSearchClient(settings, repository)
    with pytest.raises(SearchFailureError):
        await client.search("股价")
    assert attempts == 1
    usage = await repository.db.fetchone(
        "SELECT estimated_cost_cny, success FROM api_usage "
        "WHERE call_type='web_search'"
    )
    assert usage["estimated_cost_cny"] == 0
    assert usage["success"] == 0


@pytest.mark.asyncio
async def test_search_retries_network_timeout_once(
    settings, repository, monkeypatch
):
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("模拟搜索超时", request=request)
        return httpx.Response(
            200,
            request=request,
            json={"results": []},
        )

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    client = DeepSeekSearchClient(settings, repository)
    assert await client.search("今天新闻") == []
    assert attempts == 2


def test_stale_market_result_is_filtered():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    recent = result(date=(now - timedelta(hours=2)).isoformat())
    weekend = result(date=(now - timedelta(days=3)).isoformat())
    stale = result(date=(now - timedelta(days=5)).isoformat())
    undated = result(date=None)
    filtered = filter_fresh_results(
        [recent, weekend, stale, undated],
        now=now,
        intent=SearchIntent(
            needed=True,
            time_sensitive=True,
            market=True,
        ),
    )
    assert filtered == [recent, weekend]


def test_older_knowledge_result_is_kept_for_non_market_question():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    older = result(date=(now - timedelta(days=13)).isoformat())
    filtered = filter_fresh_results(
        [older],
        now=now,
        intent=SearchIntent(needed=True, time_sensitive=True),
    )
    assert filtered == [older]
