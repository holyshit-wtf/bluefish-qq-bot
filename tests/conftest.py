from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.config import Settings
from services.application import BlueFishApplication
from services.commands import RuntimeState
from services.llm import ModelFailureError, ModelResult
from services.memory import MemoryService
from services.search import SearchFailureError, SearchResult
from storage.database import Database
from storage.repository import Repository


@dataclass
class SentMessage:
    text: str
    group_id: int | None
    user_id: int | None
    quote_id: int | None
    message_id: int


class FakeModel:
    def __init__(self) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.extract_calls: list[dict[str, Any]] = []
        self.fail = False
        self.reply = "今天也要好好吃饭呀。"
        self.extract_payload: dict[str, Any] = {"memes": [], "profiles": []}

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        call_type: str = "chat",
        history: list[dict[str, str]] | None = None,
        max_tokens: int = 320,
    ) -> ModelResult:
        if self.fail:
            raise ModelFailureError("模拟模型故障")
        self.chat_calls.append(
            {
                "system": system_prompt,
                "user": user_prompt,
                "call_type": call_type,
                "history": history or [],
            }
        )
        return ModelResult(self.reply, 20, 10)

    async def compress(self, original: str, max_length: int = 300) -> ModelResult:
        if self.fail:
            raise ModelFailureError("模拟压缩故障")
        return ModelResult(original[: max_length - 10], 10, 10)

    async def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        call_type: str = "daily_extract",
    ) -> dict[str, Any]:
        if self.fail:
            raise ModelFailureError("模拟提炼故障")
        self.extract_calls.append(
            {"system": system_prompt, "user": user_prompt, "call_type": call_type}
        )
        return self.extract_payload


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.results: list[SearchResult] = []
        self.result_batches: list[list[SearchResult]] = []

    async def search(
        self,
        query: str,
        *,
        ori_question: str | None = None,
        count: int = 5,
    ) -> list[SearchResult]:
        self.calls.append(
            {
                "query": query,
                "ori_question": ori_question,
                "count": count,
            }
        )
        if self.fail:
            raise SearchFailureError("模拟搜索故障")
        if self.result_batches:
            return self.result_batches.pop(0)
        return list(self.results)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        deepseek_api_key="test-key",
        owner_qq=9999,
        napcat_ws_url="ws://127.0.0.1:3001",
        database_path=tmp_path / "data" / "test.db",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "data" / "backups",
        log_dir=tmp_path / "logs",
    )


@pytest_asyncio.fixture
async def db(settings: Settings):
    database = Database(settings.database_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def repository(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def fake_search() -> FakeSearch:
    return FakeSearch()


@pytest.fixture
def application(
    settings: Settings,
    repository: Repository,
    fake_model: FakeModel,
    fake_search: FakeSearch,
) -> tuple[BlueFishApplication, list[SentMessage]]:
    sent: list[SentMessage] = []

    async def send(
        text: str,
        group_id: int | None,
        user_id: int | None,
        quote_id: int | None,
    ) -> int:
        message_id = 50_000 + len(sent)
        sent.append(
            SentMessage(text, group_id, user_id, quote_id, message_id)
        )
        return message_id

    app = BlueFishApplication(
        settings,
        repository,
        MemoryService(
            repository,
            group_retention_limit=settings.group_history_retention,
            group_context_messages=settings.group_context_messages,
            group_context_fts_limit=settings.group_context_fts_limit,
        ),
        fake_model,  # type: ignore[arg-type]
        RuntimeState(started_at=time.time()),
        send,
        search=fake_search,
        rng=random.Random(7),
    )
    return app, sent

