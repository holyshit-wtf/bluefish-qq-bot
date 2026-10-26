from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import aiosqlite

from storage.database import Database


def anonymous_user_key(group_id: int, user_id: int) -> str:
    digest = hashlib.sha256(f"{group_id}:{user_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def _dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


@dataclass(slots=True)
class GameResult:
    ok: bool
    message: str
    affection: float = 0
    exp: int = 0
    token_balance: int = 0
    hunger: float = 0
    mood: float = 0


class Repository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def audit(
        self,
        action: str,
        result: str,
        *,
        group_id: int | None = None,
        user_id: int | None = None,
        actor: str | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> None:
        user_key = (
            anonymous_user_key(group_id, user_id)
            if group_id is not None and user_id is not None
            else None
        )
        await self.db.execute(
            """
            INSERT INTO audit_log(
                created_at, actor, group_id, user_key, action, result,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp or time.time(),
                actor,
                group_id,
                user_key,
                action,
                result,
                json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    async def get_setting(self, key: str, default: str = "") -> str:
        return str(
            await self.db.scalar(
                "SELECT value FROM system_settings WHERE key=?", (key,), default
            )
        )

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            """
            INSERT INTO system_settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    async def is_globally_paused(self) -> bool:
        return await self.get_setting("global_paused", "0") == "1"

    async def get_group(self, group_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT g.*, c.base_probability, c.cooldown_seconds,
                   c.must_answer, c.games_enabled, c.persona,
                   s.hunger, s.mood, s.updated_at AS state_updated_at
            FROM groups g
            JOIN group_config c ON c.group_id=g.group_id
            JOIN group_state s ON s.group_id=g.group_id
            WHERE g.group_id=?
            """,
            (group_id,),
        )
        return _dict(row)

    async def group_is_enabled(self, group_id: int) -> bool:
        group = await self.get_group(group_id)
        return bool(group and group["enabled"])

    async def enable_group(
        self, group_id: int, *, actor: str = "owner"
    ) -> None:
        now = time.time()
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO groups(group_id, enabled, enabled_at, created_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    enabled=1,
                    enabled_at=CASE
                        WHEN groups.enabled=0 THEN excluded.enabled_at
                        ELSE groups.enabled_at
                    END
                """,
                (group_id, now, now),
            )
            await connection.execute(
                """
                INSERT INTO group_config(group_id, updated_at) VALUES (?, ?)
                ON CONFLICT(group_id) DO NOTHING
                """,
                (group_id, now),
            )
            await connection.execute(
                """
                INSERT INTO group_state(group_id, updated_at) VALUES (?, ?)
                ON CONFLICT(group_id) DO NOTHING
                """,
                (group_id, now),
            )
            await connection.execute(
                """
                INSERT INTO audit_log(
                    created_at, actor, group_id, action, result, metadata_json
                ) VALUES (?, ?, ?, 'group_enable', 'success', '{}')
                """,
                (now, actor, group_id),
            )

    async def disable_group(
        self, group_id: int, *, actor: str = "owner"
    ) -> bool:
        await self.db.execute(
            "UPDATE groups SET enabled=0 WHERE group_id=?", (group_id,)
        )
        exists = await self.db.scalar(
            "SELECT COUNT(*) FROM groups WHERE group_id=?", (group_id,), 0
        )
        await self.audit(
            "group_disable", "success" if exists else "not_found", group_id=group_id,
            actor=actor
        )
        return bool(exists)

    async def list_groups(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT group_id, enabled, enabled_at FROM groups ORDER BY group_id"
        )
        return [dict(row) for row in rows]

    async def set_group_config(
        self, group_id: int, field: str, value: Any
    ) -> None:
        allowed = {
            "base_probability",
            "cooldown_seconds",
            "must_answer",
            "games_enabled",
            "persona",
        }
        if field not in allowed:
            raise ValueError("不允许修改该群配置")
        await self.db.execute(
            f"UPDATE group_config SET {field}=?, updated_at=? WHERE group_id=?",
            (value, time.time(), group_id),
        )

    async def touch_binding(
        self,
        user_id: int,
        group_id: int,
        onebot_message_id: int | None,
        nickname: str | None = None,
    ) -> None:
        now = time.time()
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO bindings(user_id, group_id, source_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    group_id=excluded.group_id,
                    source_message_id=excluded.source_message_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, group_id, onebot_message_id, now),
            )
            if nickname and nickname.strip():
                await connection.execute(
                    """
                    INSERT INTO user_nicknames(
                        group_id, user_id, nickname, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        nickname=excluded.nickname,
                        updated_at=excluded.updated_at
                    """,
                    (group_id, user_id, nickname.strip(), now),
                )

    async def get_binding(self, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT b.*, g.enabled
            FROM bindings b JOIN groups g ON g.group_id=b.group_id
            WHERE b.user_id=? AND g.enabled=1
            """,
            (user_id,),
        )
        return _dict(row)

    async def add_blocked_term(
        self, group_id: int, original: str, normalized: str, created_by: int
    ) -> bool:
        count = await self.db.scalar(
            "SELECT COUNT(*) FROM blocked_terms WHERE group_id=?",
            (group_id,),
            0,
        )
        if count >= 100:
            return False
        await self.db.execute(
            """
            INSERT INTO blocked_terms(
                group_id, normalized_term, original_term, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, normalized_term) DO UPDATE SET
                original_term=excluded.original_term
            """,
            (group_id, normalized, original, created_by, time.time()),
        )
        return True

    async def delete_blocked_term(
        self, group_id: int, normalized: str
    ) -> bool:
        exists = await self.db.scalar(
            """
            SELECT COUNT(*) FROM blocked_terms
            WHERE group_id=? AND normalized_term=?
            """,
            (group_id, normalized),
            0,
        )
        await self.db.execute(
            """
            DELETE FROM blocked_terms
            WHERE group_id=? AND normalized_term=?
            """,
            (group_id, normalized),
        )
        return bool(exists)

    async def list_blocked_terms(self, group_id: int) -> list[str]:
        rows = await self.db.fetchall(
            """
            SELECT original_term FROM blocked_terms
            WHERE group_id=? ORDER BY created_at
            """,
            (group_id,),
        )
        return [str(row[0]) for row in rows]

    async def find_blocked_term(
        self, group_id: int, normalized_content: str
    ) -> str | None:
        rows = await self.db.fetchall(
            """
            SELECT normalized_term FROM blocked_terms WHERE group_id=?
            """,
            (group_id,),
        )
        for row in rows:
            if row[0] and row[0] in normalized_content:
                return str(row[0])
        return None

    async def add_group_message(
        self,
        group_id: int,
        user_id: int | None,
        nickname: str,
        role: str,
        content: str,
        search_text: str,
        onebot_message_id: int | None,
        created_at: float,
        *,
        retention_limit: int = 400,
    ) -> int:
        if retention_limit < 1:
            raise ValueError("retention_limit 必须大于 0")
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO messages(
                    group_id, user_id, nickname, role, content, search_text,
                    onebot_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    nickname,
                    role,
                    content,
                    search_text,
                    onebot_message_id,
                    created_at,
                ),
            )
            message_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO messages_fts(rowid, content, search_text)
                VALUES (?, ?, ?)
                """,
                (message_id, content, search_text),
            )
            stale_rows = await (
                await connection.execute(
                    """
                    SELECT id FROM messages WHERE group_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (group_id, retention_limit),
                )
            ).fetchall()
            for row in stale_rows:
                await connection.execute(
                    "DELETE FROM messages_fts WHERE rowid=?", (row[0],)
                )
                await connection.execute("DELETE FROM messages WHERE id=?", (row[0],))
            return message_id

    async def recent_group_messages(
        self, group_id: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, user_id, nickname, role, content, created_at
            FROM messages WHERE group_id=?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (group_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    async def search_group_messages(
        self,
        group_id: int,
        fts_query: str,
        limit: int = 8,
        *,
        exclude_ids: Iterable[int] = (),
    ) -> list[dict[str, Any]]:
        if not fts_query:
            return []
        excluded = tuple(exclude_ids)
        exclusion = ""
        params: list[Any] = [fts_query, group_id]
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclusion = f" AND m.id NOT IN ({placeholders})"
            params.extend(excluded)
        params.append(limit)
        rows = await self.db.fetchall(
            f"""
            SELECT m.id, m.user_id, m.nickname, m.role, m.content,
                   m.created_at, bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages m ON m.id=messages_fts.rowid
            WHERE messages_fts MATCH ? AND m.group_id=?{exclusion}
            ORDER BY score ASC, m.created_at DESC LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in rows]

    async def add_private_message(
        self,
        group_id: int,
        user_id: int,
        role: str,
        content: str,
        search_text: str,
        created_at: float,
    ) -> int:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO private_messages(
                    group_id, user_id, role, content, search_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, user_id, role, content, search_text, created_at),
            )
            message_id = int(cursor.lastrowid)
            await connection.execute(
                """
                INSERT INTO private_messages_fts(rowid, content, search_text)
                VALUES (?, ?, ?)
                """,
                (message_id, content, search_text),
            )
            stale_rows = await (
                await connection.execute(
                    """
                    SELECT id FROM private_messages
                    WHERE group_id=? AND user_id=?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET 40
                    """,
                    (group_id, user_id),
                )
            ).fetchall()
            for row in stale_rows:
                await connection.execute(
                    "DELETE FROM private_messages_fts WHERE rowid=?", (row[0],)
                )
                await connection.execute(
                    "DELETE FROM private_messages WHERE id=?", (row[0],)
                )
            return message_id

    async def recent_private_messages(
        self, group_id: int, user_id: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT role, content, created_at FROM private_messages
            WHERE group_id=? AND user_id=?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (group_id, user_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

    async def message_by_onebot_id(
        self, group_id: int, onebot_message_id: int
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT id, role, content FROM messages
            WHERE group_id=? AND onebot_message_id=? LIMIT 1
            """,
            (group_id, onebot_message_id),
        )
        return _dict(row)

    async def reserve_send(
        self,
        scene: str,
        group_id: int | None,
        now: float,
        day_start: float,
        *,
        per_minute: int = 20,
        per_hour: int = 30,
        per_day: int = 200,
    ) -> int | None:
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                UPDATE send_events SET status='failed'
                WHERE status='reserved' AND created_at < ?
                """,
                (now - 120,),
            )
            cursor = await connection.execute(
                """
                SELECT
                  SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),
                  SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),
                  SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END)
                FROM send_events
                WHERE status IN ('reserved', 'sent')
                """,
                (now - 60, now - 3600, day_start),
            )
            row = await cursor.fetchone()
            minute_count = int(row[0] or 0)
            hour_count = int(row[1] or 0)
            day_count = int(row[2] or 0)
            if (
                minute_count >= per_minute
                or hour_count >= per_hour
                or day_count >= per_day
            ):
                return None
            cursor = await connection.execute(
                """
                INSERT INTO send_events(created_at, scene, group_id, status)
                VALUES (?, ?, ?, 'reserved')
                """,
                (now, scene, group_id),
            )
            return int(cursor.lastrowid)

    async def finish_send(
        self,
        reservation_id: int,
        *,
        success: bool,
        onebot_message_id: int | None = None,
        sent_at: float | None = None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE send_events
            SET status=?, sent_at=?, onebot_message_id=?
            WHERE id=?
            """,
            (
                "sent" if success else "failed",
                sent_at if success else None,
                onebot_message_id if success else None,
                reservation_id,
            ),
        )

    async def last_group_send_at(self, group_id: int) -> float | None:
        value = await self.db.scalar(
            """
            SELECT MAX(sent_at) FROM send_events
            WHERE group_id=? AND status='sent' AND sent_at IS NOT NULL
            """,
            (group_id,),
        )
        return float(value) if value is not None else None

    async def was_bot_message(self, group_id: int, onebot_message_id: int) -> bool:
        count = await self.db.scalar(
            """
            SELECT COUNT(*) FROM send_events
            WHERE group_id=? AND onebot_message_id=? AND status='sent'
            """,
            (group_id, onebot_message_id),
            0,
        )
        return bool(count)

    async def ensure_game_rows(
        self, group_id: int, user_id: int, day: str, now: float
    ) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO affection(
                    group_id, user_id, value, daily_gain, day, updated_at
                ) VALUES (?, ?, 0, 0, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    daily_gain=CASE WHEN day<>excluded.day THEN 0 ELSE daily_gain END,
                    day=excluded.day
                """,
                (group_id, user_id, day, now),
            )
            await connection.execute(
                """
                INSERT INTO growth(
                    group_id, user_id, exp, daily_gain, day, updated_at
                ) VALUES (?, ?, 0, 0, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    daily_gain=CASE WHEN day<>excluded.day THEN 0 ELSE daily_gain END,
                    day=excluded.day
                """,
                (group_id, user_id, day, now),
            )
            await connection.execute(
                """
                INSERT INTO tokens(
                    group_id, user_id, balance, chat_reward_day,
                    chat_reward_count, updated_at
                ) VALUES (?, ?, 0, ?, 0, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    chat_reward_count=CASE
                        WHEN chat_reward_day<>excluded.chat_reward_day THEN 0
                        ELSE chat_reward_count
                    END,
                    chat_reward_day=CASE
                        WHEN chat_reward_day<>excluded.chat_reward_day THEN excluded.chat_reward_day
                        ELSE chat_reward_day
                    END
                """,
                (group_id, user_id, day, now),
            )

    async def game_status(
        self, group_id: int, user_id: int, day: str, now: float
    ) -> dict[str, Any]:
        await self.ensure_game_rows(group_id, user_id, day, now)
        row = await self.db.fetchone(
            """
            SELECT a.value AS affection, a.daily_gain AS affection_daily,
                   gr.exp, gr.daily_gain AS growth_daily,
                   t.balance, t.last_signin_day, t.last_chat_reward_at,
                   t.chat_reward_count,
                   gs.hunger, gs.mood
            FROM affection a
            JOIN growth gr
              ON gr.group_id=a.group_id AND gr.user_id=a.user_id
            JOIN tokens t
              ON t.group_id=a.group_id AND t.user_id=a.user_id
            JOIN group_state gs ON gs.group_id=a.group_id
            WHERE a.group_id=? AND a.user_id=?
            """,
            (group_id, user_id),
        )
        return dict(row) if row else {}

    async def signin(
        self, group_id: int, user_id: int, day: str, now: float
    ) -> GameResult:
        await self.ensure_game_rows(group_id, user_id, day, now)
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT balance, last_signin_day FROM tokens
                    WHERE group_id=? AND user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchone()
            if row and row["last_signin_day"] == day:
                return GameResult(
                    False,
                    f"今天已经签过到啦，Token 余额 {row['balance']} 枚",
                    token_balance=int(row["balance"]),
                )
            current_balance = int(row["balance"] if row else 0)
            granted = min(3, max(0, 999 - current_balance))
            balance = current_balance + granted
            await connection.execute(
                """
                UPDATE tokens SET balance=?, last_signin_day=?, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (balance, day, now, group_id, user_id),
            )
            if granted == 0:
                message = f"签到成功，Token 已达上限，当前余额 {balance} 枚"
            else:
                message = (
                    f"签到成功，获得 {granted} 枚 Token，当前余额 {balance} 枚"
                )
            return GameResult(True, message, token_balance=balance)

    async def reward_chat(
        self, group_id: int, user_id: int, day: str, now: float
    ) -> bool:
        await self.ensure_game_rows(group_id, user_id, day, now)
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT balance, last_chat_reward_at, chat_reward_count
                    FROM tokens WHERE group_id=? AND user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchone()
            if row is None or int(row["chat_reward_count"]) >= 7:
                return False
            last_at = row["last_chat_reward_at"]
            if last_at is not None and now - float(last_at) < 15 * 60:
                return False
            balance = min(999, int(row["balance"]) + 1)
            await connection.execute(
                """
                UPDATE tokens SET balance=?, last_chat_reward_at=?,
                    chat_reward_count=chat_reward_count+1, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (balance, now, now, group_id, user_id),
            )
            return True

    async def feed(
        self,
        group_id: int,
        user_id: int,
        feed_kind: str,
        day: str,
        now: float,
    ) -> GameResult:
        await self.ensure_game_rows(group_id, user_id, day, now)
        if feed_kind not in {"rice", "token"}:
            raise ValueError("未知投喂类型")
        async with self.db.transaction() as connection:
            state = await (
                await connection.execute(
                    "SELECT hunger, mood FROM group_state WHERE group_id=?",
                    (group_id,),
                )
            ).fetchone()
            stats = await (
                await connection.execute(
                    """
                    SELECT a.value AS affection, a.daily_gain AS affection_daily,
                           gr.exp, gr.daily_gain AS growth_daily, t.balance
                    FROM affection a
                    JOIN growth gr
                      ON gr.group_id=a.group_id AND gr.user_id=a.user_id
                    JOIN tokens t
                      ON t.group_id=a.group_id AND t.user_id=a.user_id
                    WHERE a.group_id=? AND a.user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchone()
            if state is None or stats is None:
                return GameResult(False, "养成数据暂不可用")
            if float(state["hunger"]) >= 100:
                return GameResult(
                    False,
                    "已经吃得饱饱的啦，现在投喂不会生效，也没有扣除 Token",
                    float(stats["affection"]),
                    int(stats["exp"]),
                    int(stats["balance"]),
                    float(state["hunger"]),
                    float(state["mood"]),
                )
            if feed_kind == "rice":
                cooldown = await (
                    await connection.execute(
                        """
                        SELECT last_at FROM feed_cooldowns
                        WHERE group_id=? AND user_id=? AND feed_kind='rice'
                        """,
                        (group_id, user_id),
                    )
                ).fetchone()
                if cooldown and now - float(cooldown["last_at"]) < 1800:
                    remaining = int(
                        1800 - (now - float(cooldown["last_at"]))
                    )
                    return GameResult(
                        False,
                        f"白饭还在冷却，约 {max(1, (remaining + 59) // 60)} 分钟后可再次投喂",
                        float(stats["affection"]),
                        int(stats["exp"]),
                        int(stats["balance"]),
                        float(state["hunger"]),
                        float(state["mood"]),
                    )
                await connection.execute(
                    """
                    INSERT INTO feed_cooldowns(
                        group_id, user_id, feed_kind, last_at
                    ) VALUES (?, ?, 'rice', ?)
                    ON CONFLICT(group_id, user_id, feed_kind)
                    DO UPDATE SET last_at=excluded.last_at
                    """,
                    (group_id, user_id, now),
                )
                affection_gain = min(1, max(0, 10 - float(stats["affection_daily"])))
                exp_gain = min(2, max(0, 50 - int(stats["growth_daily"])))
                token_delta = 0
                feed_name = "白饭"
            else:
                if int(stats["balance"]) < 5:
                    return GameResult(
                        False,
                        f"Token 不足，投喂需要 5 枚，当前余额 {int(stats['balance'])} 枚",
                        float(stats["affection"]),
                        int(stats["exp"]),
                        int(stats["balance"]),
                        float(state["hunger"]),
                        float(state["mood"]),
                    )
                affection_gain = min(2, max(0, 10 - float(stats["affection_daily"])))
                exp_gain = min(5, max(0, 50 - int(stats["growth_daily"])))
                token_delta = -5
                feed_name = "Token"
            new_affection = min(100, float(stats["affection"]) + affection_gain)
            new_exp = min(500, int(stats["exp"]) + exp_gain)
            new_balance = int(stats["balance"]) + token_delta
            new_hunger = min(
                100, float(state["hunger"]) + (6 if feed_kind == "rice" else 20)
            )
            new_mood = min(
                100, float(state["mood"]) + (1 if feed_kind == "rice" else 3)
            )
            await connection.execute(
                """
                UPDATE affection SET value=?, daily_gain=daily_gain+?,
                    day=?, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (new_affection, affection_gain, day, now, group_id, user_id),
            )
            await connection.execute(
                """
                UPDATE growth SET exp=?, daily_gain=daily_gain+?,
                    day=?, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (new_exp, exp_gain, day, now, group_id, user_id),
            )
            await connection.execute(
                """
                UPDATE tokens SET balance=?, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (new_balance, now, group_id, user_id),
            )
            await connection.execute(
                """
                UPDATE group_state SET hunger=?, mood=?, updated_at=?
                WHERE group_id=?
                """,
                (new_hunger, new_mood, now, group_id),
            )
            return GameResult(
                True,
                f"投喂{feed_name}成功！饥饿 {new_hunger:.0f}，心情 {new_mood:.0f}，"
                f"好感 {new_affection:.0f}，经验 {new_exp}",
                new_affection,
                new_exp,
                new_balance,
                new_hunger,
                new_mood,
            )

    async def evolve_group_state(
        self, group_id: int, now: float
    ) -> tuple[float, float]:
        group = await self.get_group(group_id)
        if group is None:
            return 80, 60
        elapsed_hours = max(0.0, (now - float(group["state_updated_at"])) / 3600)
        hunger = min(100, max(0, float(group["hunger"]) - 2 * elapsed_hours))
        if group["mood"] > 60:
            mood = max(60, float(group["mood"]) - elapsed_hours)
        elif group["mood"] < 60:
            mood = min(60, float(group["mood"]) + elapsed_hours)
        else:
            mood = 60.0
        await self.db.execute(
            """
            UPDATE group_state SET hunger=?, mood=?, updated_at=?
            WHERE group_id=?
            """,
            (hunger, mood, now, group_id),
        )
        return hunger, mood

    async def leaderboard(
        self, group_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT a.user_id, a.value AS affection, gr.exp,
                   COALESCE(n.nickname, p.nickname, (
                       SELECT m.nickname FROM messages m
                       WHERE m.group_id=a.group_id AND m.user_id=a.user_id
                       ORDER BY m.created_at DESC LIMIT 1
                   ), '群友') AS nickname
            FROM affection a
            JOIN growth gr
              ON gr.group_id=a.group_id AND gr.user_id=a.user_id
            LEFT JOIN user_profiles p
              ON p.group_id=a.group_id AND p.user_id=a.user_id
            LEFT JOIN user_nicknames n
              ON n.group_id=a.group_id AND n.user_id=a.user_id
            WHERE a.group_id=?
            ORDER BY a.value DESC, gr.exp DESC, a.user_id ASC LIMIT ?
            """,
            (group_id, limit),
        )
        return [dict(row) for row in rows]

    async def get_profile(
        self, group_id: int, user_id: int
    ) -> dict[str, Any] | None:
        return _dict(
            await self.db.fetchone(
                "SELECT * FROM user_profiles WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
        )

    async def upsert_profile(
        self,
        group_id: int,
        user_id: int,
        nickname: str,
        summary: str,
        preferences: str,
        interaction_style: str,
        source_date: str,
        now: float,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO user_profiles(
                group_id, user_id, nickname, preferences, interaction_style,
                summary, source_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                nickname=excluded.nickname,
                preferences=excluded.preferences,
                interaction_style=excluded.interaction_style,
                summary=excluded.summary,
                source_date=excluded.source_date,
                updated_at=excluded.updated_at
            WHERE excluded.source_date >= user_profiles.source_date
            """,
            (
                group_id,
                user_id,
                nickname,
                preferences,
                interaction_style,
                summary,
                source_date,
                now,
            ),
        )

    async def add_meme(
        self,
        group_id: int,
        text: str,
        source_message_id: int | None,
        source_date: str,
        now: float,
    ) -> int:
        async with self.db.transaction() as connection:
            result = await connection.execute(
                """
                INSERT INTO group_memes(
                    group_id, text, source_message_id, source_date, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, text, source_message_id, source_date, now),
            )
            return int(result.lastrowid)

    async def save_daily_extraction(
        self,
        group_id: int,
        source_date: str,
        memes: list[tuple[str, int | None]],
        profiles: list[tuple[int, str, str, str, str]],
        now: float,
    ) -> None:
        async with self.db.transaction() as connection:
            for text, source_message_id in memes:
                await connection.execute(
                    """
                    INSERT INTO group_memes(
                        group_id, text, source_message_id, source_date, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (group_id, text, source_message_id, source_date, now),
                )
            for user_id, nickname, summary, preferences, style in profiles:
                await connection.execute(
                    """
                    INSERT INTO user_profiles(
                        group_id, user_id, nickname, preferences,
                        interaction_style, summary, source_date, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        nickname=excluded.nickname,
                        preferences=excluded.preferences,
                        interaction_style=excluded.interaction_style,
                        summary=excluded.summary,
                        source_date=excluded.source_date,
                        updated_at=excluded.updated_at
                    WHERE excluded.source_date >= user_profiles.source_date
                    """,
                    (
                        group_id,
                        user_id,
                        nickname,
                        preferences,
                        style,
                        summary,
                        source_date,
                        now,
                    ),
                )

    async def list_memes(
        self, group_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, text, source_message_id, source_date, created_at
            FROM group_memes WHERE group_id=?
            ORDER BY created_at DESC LIMIT ?
            """,
            (group_id, limit),
        )
        return [dict(row) for row in rows]

    async def daily_group_activity(
        self, group_id: int, start: float, end: float
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT id, user_id, nickname, content, created_at
            FROM messages
            WHERE group_id=? AND role='user'
              AND created_at>=? AND created_at<?
            ORDER BY created_at, id
            """,
            (group_id, start, end),
        )
        return [dict(row) for row in rows]

    async def activity_users(
        self, group_id: int, start: float, end: float, minimum: int = 3
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT user_id, MAX(nickname) AS nickname, COUNT(*) AS message_count
            FROM messages
            WHERE group_id=? AND role='user'
              AND user_id IS NOT NULL AND created_at>=? AND created_at<?
            GROUP BY user_id HAVING COUNT(*)>=?
            ORDER BY message_count DESC
            """,
            (group_id, start, end, minimum),
        )
        return [dict(row) for row in rows]

    async def claim_job(
        self, group_id: int, run_date: str, job_type: str, now: float
    ) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO job_runs(
                    group_id, run_date, job_type, status, started_at
                ) VALUES (?, ?, ?, 'running', ?)
                ON CONFLICT(group_id, run_date, job_type) DO NOTHING
                """,
                (group_id, run_date, job_type, now),
            )
            return cursor.rowcount == 1

    async def finish_job(
        self,
        group_id: int,
        run_date: str,
        job_type: str,
        status: str,
        now: float,
        error_summary: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE job_runs SET status=?, ended_at=?, error_summary=?
            WHERE group_id=? AND run_date=? AND job_type=?
            """,
            (status, now, error_summary, group_id, run_date, job_type),
        )

    async def get_job(
        self, group_id: int, run_date: str, job_type: str
    ) -> dict[str, Any] | None:
        return _dict(
            await self.db.fetchone(
                """
                SELECT * FROM job_runs
                WHERE group_id=? AND run_date=? AND job_type=?
                """,
                (group_id, run_date, job_type),
            )
        )

    async def restart_failed_job(
        self, group_id: int, run_date: str, job_type: str, now: float
    ) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE job_runs
                SET status='running', started_at=?, ended_at=NULL, error_summary=NULL
                WHERE group_id=? AND run_date=? AND job_type=? AND status='failed'
                """,
                (now, group_id, run_date, job_type),
            )
            return cursor.rowcount == 1

    async def failed_jobs(
        self, job_type: str, earliest_date: str
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM job_runs
            WHERE job_type=? AND run_date>=? AND status='failed'
            ORDER BY run_date, group_id
            """,
            (job_type, earliest_date),
        )
        return [dict(row) for row in rows]

    async def record_api_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_cny: float,
        call_type: str,
        success: bool,
        now: float,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO api_usage(
                created_at, model, input_tokens, output_tokens,
                estimated_cost_cny, call_type, success
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                model,
                input_tokens,
                output_tokens,
                estimated_cost_cny,
                call_type,
                int(success),
            ),
        )

    async def usage_summary(
        self, month_start: float
    ) -> dict[str, Any]:
        row = await self.db.fetchone(
            """
            SELECT COALESCE(SUM(estimated_cost_cny), 0) AS cost,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM api_usage WHERE created_at>=?
            """,
            (month_start,),
        )
        return dict(row) if row else {
            "cost": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    async def send_counts(
        self, minute_start: float, hour_start: float, day_start: float
    ) -> dict[str, int]:
        row = await self.db.fetchone(
            """
            SELECT
              SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),
              SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),
              SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END)
            FROM send_events
            WHERE status IN ('reserved', 'sent')
            """,
            (minute_start, hour_start, day_start),
        )
        return {
            "minute": int(row[0] or 0) if row else 0,
            "hour": int(row[1] or 0) if row else 0,
            "day": int(row[2] or 0) if row else 0,
        }

    async def audit_page(
        self, page: int, page_size: int = 20, since: float | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(1, page)
        params: list[Any] = []
        where = "WHERE 1=1"
        if since is not None:
            where += " AND created_at>=?"
            params.append(since)
        total = int(
            await self.db.scalar(
                f"SELECT COUNT(*) FROM audit_log {where}", params, 0
            )
        )
        params.extend([page_size, (page - 1) * page_size])
        rows = await self.db.fetchall(
            f"""
            SELECT created_at, actor, group_id, user_key, action, result,
                   metadata_json
            FROM audit_log {where}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            params,
        )
        return [dict(row) for row in rows], total

    async def prune_audit(self, cutoff: float) -> None:
        await self.db.execute("DELETE FROM audit_log WHERE created_at<?", (cutoff,))

    async def prune_api_usage(self, cutoff: float) -> None:
        await self.db.execute("DELETE FROM api_usage WHERE created_at<?", (cutoff,))

    async def get_user_full(
        self, group_id: int, user_id: int
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT a.user_id, a.value AS affection, gr.exp,
                   t.balance, gs.hunger, gs.mood,
                   p.summary, p.preferences, p.interaction_style,
                   COALESCE(n.nickname, p.nickname, '群友') AS nickname
            FROM affection a
            JOIN growth gr
              ON gr.group_id=a.group_id AND gr.user_id=a.user_id
            JOIN tokens t
              ON t.group_id=a.group_id AND t.user_id=a.user_id
            JOIN group_state gs ON gs.group_id=a.group_id
            LEFT JOIN user_profiles p
              ON p.group_id=a.group_id AND p.user_id=a.user_id
            LEFT JOIN user_nicknames n
              ON n.group_id=a.group_id AND n.user_id=a.user_id
            WHERE a.group_id=? AND a.user_id=?
            """,
            (group_id, user_id),
        )
        return _dict(row)

    async def adjust_affection(
        self, group_id: int, user_id: int, delta: float, now: float
    ) -> float | None:
        async with self.db.transaction() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT value FROM affection
                    WHERE group_id=? AND user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchone()
            if row is None:
                return None
            value = min(100, max(0, float(row["value"]) + delta))
            await connection.execute(
                """
                UPDATE affection SET value=?, updated_at=?
                WHERE group_id=? AND user_id=?
                """,
                (value, now, group_id, user_id),
            )
            return value

    async def delete_user_data(
        self, group_id: int, user_id: int, now: float
    ) -> bool:
        async with self.db.transaction() as connection:
            exists = await (
                await connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM affection
                       WHERE group_id=? AND user_id=?) +
                      (SELECT COUNT(*) FROM private_messages
                       WHERE group_id=? AND user_id=?) +
                      (SELECT COUNT(*) FROM messages
                       WHERE group_id=? AND user_id=?) +
                      (SELECT COUNT(*) FROM user_profiles
                       WHERE group_id=? AND user_id=?) +
                      (SELECT COUNT(*) FROM user_nicknames
                       WHERE group_id=? AND user_id=?)
                    """,
                    (
                        group_id,
                        user_id,
                        group_id,
                        user_id,
                        group_id,
                        user_id,
                        group_id,
                        user_id,
                        group_id,
                        user_id,
                    ),
                )
            ).fetchone()
            meme_rows = await (
                await connection.execute(
                    """
                    SELECT m.id FROM group_memes m
                    JOIN messages msg ON msg.id=m.source_message_id
                    WHERE m.group_id=? AND msg.user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchall()
            message_rows = await (
                await connection.execute(
                    "SELECT id FROM messages WHERE group_id=? AND user_id=?",
                    (group_id, user_id),
                )
            ).fetchall()
            private_rows = await (
                await connection.execute(
                    """
                    SELECT id FROM private_messages
                    WHERE group_id=? AND user_id=?
                    """,
                    (group_id, user_id),
                )
            ).fetchall()
            for row in message_rows:
                await connection.execute(
                    "DELETE FROM messages_fts WHERE rowid=?", (row["id"],)
                )
            for row in private_rows:
                await connection.execute(
                    "DELETE FROM private_messages_fts WHERE rowid=?", (row["id"],)
                )
            for row in meme_rows:
                await connection.execute(
                    "DELETE FROM group_memes WHERE id=?", (row["id"],)
                )
            await connection.execute(
                "DELETE FROM messages WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM private_messages WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM affection WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM growth WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM tokens WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM feed_cooldowns WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM user_nicknames WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            await connection.execute(
                "DELETE FROM user_profiles WHERE group_id=? AND user_id=?",
                (group_id, user_id),
            )
            binding = await (
                await connection.execute(
                    "SELECT group_id FROM bindings WHERE user_id=?",
                    (user_id,),
                )
            ).fetchone()
            if binding and int(binding["group_id"]) == group_id:
                replacement = await (
                    await connection.execute(
                        """
                        SELECT msg.group_id, msg.onebot_message_id
                        FROM messages msg
                        JOIN groups g ON g.group_id=msg.group_id
                        WHERE msg.user_id=? AND g.enabled=1
                        ORDER BY msg.created_at DESC LIMIT 1
                        """,
                        (user_id,),
                    )
                ).fetchone()
                if replacement:
                    await connection.execute(
                        """
                        UPDATE bindings SET group_id=?, source_message_id=?,
                            updated_at=? WHERE user_id=?
                        """,
                        (
                            replacement["group_id"],
                            replacement["onebot_message_id"],
                            now,
                            user_id,
                        ),
                    )
                else:
                    await connection.execute(
                        "DELETE FROM bindings WHERE user_id=?", (user_id,)
                    )
            total = sum(int(value or 0) for value in exists)
            return total > 0

    async def vacuum(self) -> None:
        await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def table_counts(self) -> dict[str, int]:
        names = (
            "groups",
            "messages",
            "private_messages",
            "group_memes",
            "user_profiles",
            "audit_log",
            "send_events",
            "api_usage",
        )
        result: dict[str, int] = {}
        for name in names:
            result[name] = int(
                await self.db.scalar(f"SELECT COUNT(*) FROM {name}", (), 0)
            )
        return result
