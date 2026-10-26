from __future__ import annotations

import time
from typing import Any

from services.safety import fts_query, search_text
from storage.repository import Repository


class MemoryService:
    def __init__(
        self,
        repository: Repository,
        *,
        group_retention_limit: int = 400,
        group_context_messages: int = 80,
        group_context_fts_limit: int = 16,
    ) -> None:
        if group_retention_limit < 1:
            raise ValueError("group_retention_limit 必须大于 0")
        if not 1 <= group_context_messages <= group_retention_limit:
            raise ValueError(
                "group_context_messages 必须在 1 到保留上限之间"
            )
        if group_context_fts_limit < 0:
            raise ValueError("group_context_fts_limit 不能为负数")
        self.repository = repository
        self.group_retention_limit = group_retention_limit
        self.group_context_messages = group_context_messages
        self.group_context_fts_limit = group_context_fts_limit

    async def remember_group(
        self,
        group_id: int,
        user_id: int | None,
        nickname: str,
        role: str,
        content: str,
        onebot_message_id: int | None,
        *,
        created_at: float | None = None,
    ) -> int:
        return await self.repository.add_group_message(
            group_id,
            user_id,
            nickname,
            role,
            content,
            search_text(content),
            onebot_message_id,
            created_at or time.time(),
            retention_limit=self.group_retention_limit,
        )

    async def context_for_group(
        self, group_id: int, current_text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        recent = await self.repository.recent_group_messages(
            group_id, self.group_context_messages
        )
        recent_ids = {int(item["id"]) for item in recent}
        matches = await self.repository.search_group_messages(
            group_id,
            fts_query(current_text),
            limit=self.group_context_fts_limit,
            exclude_ids=recent_ids,
        )
        combined = sorted(
            recent + matches,
            key=lambda row: (float(row["created_at"]), int(row["id"])),
        )
        # The near-term window stays intact; FTS adds older candidates only.
        seen: set[int] = set()
        merged = []
        for item in combined:
            item_id = int(item["id"])
            if item_id not in seen:
                seen.add(item_id)
                merged.append(item)
        return recent, merged

    async def remember_private(
        self,
        group_id: int,
        user_id: int,
        role: str,
        content: str,
        *,
        created_at: float | None = None,
    ) -> int:
        return await self.repository.add_private_message(
            group_id,
            user_id,
            role,
            content,
            search_text(content),
            created_at or time.time(),
        )

    async def private_history(
        self, group_id: int, user_id: int
    ) -> list[dict[str, str]]:
        rows = await self.repository.recent_private_messages(group_id, user_id)
        return [
            {
                "role": "assistant" if row["role"] == "bot" else "user",
                "content": str(row["content"]),
            }
            for row in rows
        ]

