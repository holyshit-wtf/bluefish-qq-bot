from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.config import Settings
from services.personality import (
    PERSONAS,
    affection_level,
    growth_stage,
)
from services.safety import normalize_blocked_term
from storage.repository import Repository


@dataclass(slots=True)
class CommandContext:
    user_id: int
    nickname: str
    group_id: int | None
    is_group_admin: bool
    is_owner: bool


@dataclass(slots=True)
class CommandPlan:
    kind: str
    args: list[str] = field(default_factory=list)
    raw_tail: str = ""
    parse_error: str | None = None


@dataclass(slots=True)
class RuntimeState:
    started_at: float
    version: str = "1.0.0"
    onebot_connected: bool = False
    napcat_version: str = "待握手"


class CommandService:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        runtime: RuntimeState,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.runtime = runtime

    def prepare(self, content: str, context: CommandContext) -> CommandPlan | None:
        stripped = content.strip()
        if not (stripped == "/鱼" or stripped.startswith("/鱼 ")):
            return None
        rest = stripped[2:].strip()
        if not rest:
            return CommandPlan("help", parse_error="命令不完整。")
        parts = rest.split()
        command = parts[0]
        args = parts[1:]

        player_commands = {"帮助", "状态", "画像", "签到", "投喂", "排行", "绑定"}
        admin_commands = {"配置", "禁忌"}
        owner_commands = {"群", "全局", "用户", "系统", "审计"}

        if command in player_commands:
            if command == "帮助":
                return CommandPlan("help")
            if command == "状态" and args:
                return CommandPlan("invalid", parse_error="状态命令不需要参数。")
            if command == "画像" and args:
                return CommandPlan("invalid", parse_error="画像命令不需要参数。")
            if command == "签到" and args:
                return CommandPlan("invalid", parse_error="签到命令不需要参数。")
            if command == "排行" and args:
                return CommandPlan("invalid", parse_error="排行命令不需要参数。")
            if command == "绑定" and args:
                return CommandPlan("invalid", parse_error="绑定命令不需要参数。")
            if command == "投喂":
                if len(args) != 1 or args[0].casefold() not in {"白饭", "token"}:
                    return CommandPlan(
                        "invalid", parse_error="用法：/鱼 投喂 白饭 或 /鱼 投喂 Token"
                    )
                return CommandPlan("feed", [args[0]])
            return CommandPlan(command)

        if command in admin_commands:
            if context.group_id is None:
                return CommandPlan(
                    "permission_error", parse_error="群管理员命令只能在群内使用。"
                )
            if not context.is_group_admin:
                return CommandPlan(
                    "permission_error", parse_error="只有本群管理员可以使用该命令。"
                )
            if command == "配置":
                if len(args) < 2:
                    return CommandPlan(
                        "invalid",
                        parse_error="配置项不完整。发送 /鱼 帮助 查看命令格式。",
                    )
                if args[0] == "人格":
                    return CommandPlan("config_persona", args[1:])
                if len(args) != 2:
                    return CommandPlan("invalid", parse_error="配置参数数量不正确。")
                return CommandPlan("config", args)
            if not args:
                return CommandPlan(
                    "invalid", parse_error="用法：/鱼 禁忌 添加/删除/列表"
                )
            action = args[0]
            tail = rest[rest.find(action) + len(action) :].strip()
            if action == "列表" and tail:
                return CommandPlan("invalid", parse_error="禁忌列表命令不需要参数。")
            if action not in {"添加", "删除", "列表"}:
                return CommandPlan("invalid", parse_error="未知的禁忌词操作。")
            if action in {"添加", "删除"} and not tail:
                return CommandPlan(
                    "invalid", parse_error=f"请在“{action}”后填写 1 至 32 字词语。"
                )
            if len(tail) > 32:
                return CommandPlan(
                    "invalid", parse_error="禁忌词长度不能超过 32 字。"
                )
            return CommandPlan(f"blocked_{action}", raw_tail=tail)

        if command in owner_commands:
            if not context.is_owner or context.group_id is not None:
                return CommandPlan(
                    "permission_error", parse_error="该主人命令只能由主人私聊执行。"
                )
            if command == "群":
                if not args or args[0] not in {"启用", "停用", "列表"}:
                    return CommandPlan(
                        "invalid", parse_error="用法：/鱼 群 启用/停用 <群号> 或 /鱼 群 列表"
                    )
                if args[0] == "列表":
                    return CommandPlan("owner_group_list")
                if len(args) != 2 or not args[1].isdigit():
                    return CommandPlan("invalid", parse_error="请提供有效群号。")
                if int(args[1]) <= 0:
                    return CommandPlan("invalid", parse_error="群号必须大于 0。")
                return CommandPlan(f"owner_group_{args[0]}", [args[1]])
            if command == "全局":
                if len(args) != 1 or args[0] not in {"暂停", "恢复"}:
                    return CommandPlan(
                        "invalid", parse_error="用法：/鱼 全局 暂停 或 /鱼 全局 恢复"
                    )
                return CommandPlan(f"owner_global_{args[0]}")
            if command == "用户":
                if len(args) < 2:
                    return CommandPlan("invalid", parse_error="用户命令参数不完整。")
                action = args[0]
                if action == "查看" and len(args) == 3:
                    if not args[1].isdigit() or not args[2].isdigit():
                        return CommandPlan("invalid", parse_error="群号和 QQ 号必须是数字。")
                    if int(args[1]) <= 0 or int(args[2]) <= 0:
                        return CommandPlan("invalid", parse_error="群号和 QQ 号必须大于 0。")
                    return CommandPlan("owner_user_view", args[1:])
                if action == "好感" and len(args) == 4:
                    try:
                        if not args[1].isdigit() or not args[2].isdigit():
                            raise ValueError
                        delta = float(args[3])
                        if not math.isfinite(delta):
                            raise ValueError
                    except ValueError:
                        return CommandPlan(
                            "invalid", parse_error="群号、QQ 号或增减值格式不正确。"
                        )
                    if int(args[1]) <= 0 or int(args[2]) <= 0:
                        return CommandPlan("invalid", parse_error="群号和 QQ 号必须大于 0。")
                    return CommandPlan("owner_user_favor", args[1:])
                if action == "删除" and len(args) == 3:
                    if not args[1].isdigit() or not args[2].isdigit():
                        return CommandPlan("invalid", parse_error="群号和 QQ 号必须是数字。")
                    if int(args[1]) <= 0 or int(args[2]) <= 0:
                        return CommandPlan("invalid", parse_error="群号和 QQ 号必须大于 0。")
                    return CommandPlan("owner_user_delete", args[1:])
                return CommandPlan(
                    "invalid",
                    parse_error="用法：/鱼 用户 查看/好感/删除 <群号> <QQ号> [增减值]",
                )
            if command == "系统":
                if len(args) != 1 or args[0] != "状态":
                    return CommandPlan("invalid", parse_error="用法：/鱼 系统 状态")
                return CommandPlan("owner_system_status")
            if command == "审计":
                if len(args) != 1 or not args[0].isdigit():
                    return CommandPlan("invalid", parse_error="用法：/鱼 审计 <页码>")
                return CommandPlan("owner_audit", [args[0]])

        return CommandPlan(
            "invalid",
            parse_error="没有这个命令。发送 /鱼 帮助 查看可用命令。",
        )

    async def execute(
        self, plan: CommandPlan, context: CommandContext
    ) -> str:
        if plan.parse_error:
            if "/鱼 帮助" in plan.parse_error:
                return plan.parse_error
            return f"{plan.parse_error} 发送 /鱼 帮助查看命令。"
        if plan.kind == "help":
            return await self._help(context)
        if plan.kind == "permission_error":
            return "没有权限执行该命令。"
        if plan.kind == "invalid":
            return "命令格式不正确。"
        if plan.kind == "绑定":
            return await self._binding(context)
        if plan.kind == "排行":
            group_id = await self._target_group(context)
            if group_id is None:
                return self._binding_prompt()
            group = await self.repository.get_group(group_id)
            if not group or not group["games_enabled"]:
                return "本群养成模块已关闭。"
            return await self._leaderboard(group_id)
        if plan.kind in {"状态", "画像", "签到", "feed"}:
            group_id = await self._target_group(context)
            if group_id is None:
                return self._binding_prompt()
            group = await self.repository.get_group(group_id)
            if not group or not group["games_enabled"]:
                return "本群养成模块已关闭。"
            now = time.time()
            day = self._day(now)
            if plan.kind == "状态":
                return await self._status(group_id, context, day, now)
            if plan.kind == "画像":
                profile = await self.repository.get_profile(group_id, context.user_id)
                if not profile:
                    return "这个群还没有你的画像哦，多聊几次再来看看。"
                return (
                    f"你的群友画像：{profile['summary'] or '还在慢慢形成'}"
                    f"；偏好：{profile['preferences'] or '暂无'}"
                    f"；互动风格：{profile['interaction_style'] or '暂无'}"
                )
            if plan.kind == "签到":
                result = await self.repository.signin(
                    group_id, context.user_id, day, now
                )
                return result.message
            feed_kind = "rice" if plan.args[0].casefold() == "白饭" else "token"
            result = await self.repository.feed(
                group_id, context.user_id, feed_kind, day, now
            )
            await self.repository.audit(
                "feed",
                "success" if result.ok else "rejected",
                group_id=group_id,
                user_id=context.user_id,
                metadata={"kind": feed_kind},
            )
            return result.message
        if plan.kind.startswith("config"):
            assert context.group_id is not None
            return await self._configure(plan, context)
        if plan.kind.startswith("blocked_"):
            assert context.group_id is not None
            return await self._blocked_terms(plan, context)
        if plan.kind.startswith("owner_group_"):
            return await self._owner_groups(plan)
        if plan.kind.startswith("owner_global_"):
            action = plan.kind.rsplit("_", 1)[1]
            await self.repository.set_setting(
                "global_paused", "1" if action == "暂停" else "0"
            )
            await self.repository.audit(f"global_{action}", "success", actor="owner")
            return "全局自动发送已暂停。" if action == "暂停" else "全局自动发送已恢复。"
        if plan.kind == "owner_user_view":
            return await self._owner_user_view(plan)
        if plan.kind == "owner_user_favor":
            return await self._owner_user_favor(plan)
        if plan.kind == "owner_user_delete":
            return await self._owner_user_delete(plan)
        if plan.kind == "owner_system_status":
            return await self._system_status()
        if plan.kind == "owner_audit":
            return await self._audit(int(plan.args[0]))
        return "命令暂不可用。"

    async def _help(self, context: CommandContext) -> str:
        persona = "标准贪吃鲸娘"
        group_id = await self._target_group(context)
        if group_id is not None:
            group = await self.repository.get_group(group_id)
            if group:
                persona = str(group["persona"])
        return (
            "玩家命令：状态、画像、签到、投喂 白饭、投喂 Token、排行、绑定。"
            f"当前群人格：{persona}。管理员可发送 /鱼 配置 或 /鱼 禁忌。"
        )

    async def _target_group(self, context: CommandContext) -> int | None:
        if context.group_id is not None:
            return context.group_id
        binding = await self.repository.get_binding(context.user_id)
        return int(binding["group_id"]) if binding else None

    @staticmethod
    def _binding_prompt() -> str:
        return "你还没有绑定群。先在机器人已启用的群里发一条消息，再来私聊吧。"

    async def _binding(self, context: CommandContext) -> str:
        binding = await self.repository.get_binding(context.user_id)
        if not binding:
            return self._binding_prompt()
        return f"你当前绑定的群是 {binding['group_id']}。"

    async def _status(
        self, group_id: int, context: CommandContext, day: str, now: float
    ) -> str:
        status = await self.repository.game_status(
            group_id, context.user_id, day, now
        )
        stage = growth_stage(int(status["exp"]))
        title = f" {stage.title}" if stage.title else ""
        return (
            f"好感 {status['affection']:.0f}（{affection_level(status['affection'])}）"
            f"，成长 {stage.level} 级{title}，经验 {status['exp']}/500，"
            f"饥饿 {status['hunger']:.0f}，心情 {status['mood']:.0f}，"
            f"Token {status['balance']}"
        )

    async def _leaderboard(self, group_id: int) -> str:
        rows = await self.repository.leaderboard(group_id)
        if not rows:
            return "这个群还没有养成排行，投喂或签到后就会出现在这里。"
        parts = []
        for index, row in enumerate(rows, start=1):
            level = min(50, int(row["exp"]) // 10)
            parts.append(
                f"{index}.{row['nickname']} 好感{float(row['affection']):.0f} Lv.{level}"
            )
        return "群好感排行：" + "；".join(parts)

    async def _configure(
        self, plan: CommandPlan, context: CommandContext
    ) -> str:
        group_id = context.group_id
        assert group_id is not None
        if plan.kind == "config_persona":
            persona = " ".join(plan.args)
            if persona not in PERSONAS:
                return f"人格可选：{' / '.join(PERSONAS)}。"
            await self.repository.set_group_config(group_id, "persona", persona)
            await self.repository.audit(
                "config_persona", "success", group_id=group_id,
                user_id=context.user_id, actor="group_admin",
                metadata={"persona": persona},
            )
            return f"本群人格已切换为 {persona}。"
        field_name, raw = plan.args
        mapping = {
            "概率": ("base_probability", 0, 100, "插话概率"),
            "冷却": ("cooldown_seconds", 60, 86400, "随机插话冷却"),
            "必答": ("must_answer", None, None, "必答开关"),
            "养成": ("games_enabled", None, None, "养成开关"),
        }
        if field_name not in mapping:
            return "可配置项：概率、冷却、必答、养成、人格。"
        field_key, minimum, maximum, label = mapping[field_name]
        if field_name in {"必答", "养成"}:
            if raw not in {"开", "关"}:
                return f"{label}只能设置为“开”或“关”。"
            value: Any = 1 if raw == "开" else 0
        else:
            try:
                number = int(raw)
            except ValueError:
                return f"{label}必须是整数。"
            assert minimum is not None and maximum is not None
            if not minimum <= number <= maximum:
                return f"{label}范围是 {minimum} 至 {maximum}。"
            value = number
        await self.repository.set_group_config(group_id, field_key, value)
        await self.repository.audit(
            f"config_{field_key}", "success", group_id=group_id,
            user_id=context.user_id, actor="group_admin",
            metadata={"value": value},
        )
        return f"{label}已设置为 {raw}。"

    async def _blocked_terms(
        self, plan: CommandPlan, context: CommandContext
    ) -> str:
        group_id = context.group_id
        assert group_id is not None
        if plan.kind == "blocked_列表":
            terms = await self.repository.list_blocked_terms(group_id)
            if not terms:
                return "本群暂未设置禁忌词。"
            return "本群禁忌词：" + "、".join(terms)
        original = plan.raw_tail.strip()
        normalized = normalize_blocked_term(original)
        if not normalized or len(original) > 32:
            return "禁忌词必须为 1 至 32 个字符，且不能只有空白。"
        if plan.kind == "blocked_添加":
            added = await self.repository.add_blocked_term(
                group_id, original, normalized, context.user_id
            )
            if not added:
                return "本群禁忌词已达 100 条上限。"
            await self.repository.audit(
                "blocked_add", "success", group_id=group_id,
                user_id=context.user_id, actor="group_admin",
            )
            return "禁忌词已添加。"
        deleted = await self.repository.delete_blocked_term(group_id, normalized)
        await self.repository.audit(
            "blocked_delete", "success" if deleted else "not_found",
            group_id=group_id, user_id=context.user_id, actor="group_admin",
        )
        return "禁忌词已删除。" if deleted else "没有找到这个禁忌词。"

    async def _owner_groups(self, plan: CommandPlan) -> str:
        if plan.kind == "owner_group_list":
            groups = await self.repository.list_groups()
            if not groups:
                return "暂无群记录。"
            return "群列表：" + "；".join(
                f"{row['group_id']} {'启用' if row['enabled'] else '停用'}"
                for row in groups
            )
        group_id = int(plan.args[0])
        if plan.kind == "owner_group_启用":
            await self.repository.enable_group(group_id, actor="owner")
            return f"群 {group_id} 已启用。"
        disabled = await self.repository.disable_group(group_id, actor="owner")
        return (
            f"群 {group_id} 已停用。"
            if disabled
            else f"未找到群 {group_id}，没有可停用的记录。"
        )

    async def _owner_user_view(self, plan: CommandPlan) -> str:
        group_id, user_id = int(plan.args[0]), int(plan.args[1])
        state = await self.repository.get_user_full(group_id, user_id)
        if not state:
            return "未找到该用户在此群的数据。"
        stage = growth_stage(int(state["exp"]))
        return (
            f"用户 {state['nickname']} ({user_id})@群{group_id}："
            f"好感 {state['affection']:.0f}，"
            f"成长 {stage.level} 级/{state['exp']} 经验，Token {state['balance']}，"
            f"饥饿 {state['hunger']:.0f}，心情 {state['mood']:.0f}。"
            f"画像：{state['summary'] or '暂无'}；"
            f"偏好：{state['preferences'] or '暂无'}；"
            f"风格：{state['interaction_style'] or '暂无'}"
        )

    async def _owner_user_favor(self, plan: CommandPlan) -> str:
        group_id, user_id, delta = (
            int(plan.args[0]),
            int(plan.args[1]),
            float(plan.args[2]),
        )
        value = await self.repository.adjust_affection(
            group_id, user_id, delta, time.time()
        )
        if value is None:
            await self.repository.audit(
                "owner_affection_adjust", "not_found", group_id=group_id,
                user_id=user_id, actor="owner",
            )
            return "未找到该用户在此群的数据。"
        await self.repository.audit(
            "owner_affection_adjust", "success", group_id=group_id,
            user_id=user_id, actor="owner", metadata={"delta": delta},
        )
        return f"好感已调整为 {value:.0f}。"

    async def _owner_user_delete(self, plan: CommandPlan) -> str:
        group_id, user_id = int(plan.args[0]), int(plan.args[1])
        deleted = await self.repository.delete_user_data(
            group_id, user_id, time.time()
        )
        await self.repository.audit(
            "owner_user_delete", "success" if deleted else "not_found",
            group_id=group_id, user_id=user_id, actor="owner",
        )
        return "该用户在本群的数据已删除。" if deleted else "未找到可删除的数据。"

    async def _system_status(self) -> str:
        now = time.time()
        local = datetime.now(self.settings.timezone)
        month_start = local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        usage = await self.repository.usage_summary(month_start)
        sends = await self.repository.send_counts(
            now - 60,
            now - 3600,
            datetime.now(self.settings.timezone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp(),
        )
        runtime_seconds = max(0, int(now - self.runtime.started_at))
        hours, remainder = divmod(runtime_seconds, 3600)
        minutes = remainder // 60
        paused = await self.repository.is_globally_paused()
        version = await self.repository.get_setting("runtime_versions", "未记录")
        return (
            f"版本 {self.runtime.version}，运行 {hours}小时{minutes}分，"
            f"OneBot {'在线' if self.runtime.onebot_connected else '离线'}，"
            f"NapCat {self.runtime.napcat_version}，"
            f"全局{'暂停' if paused else '运行'}。"
            f"发送计数 分/时/日 {sends['minute']}/{sends['hour']}/{sends['day']}。"
            f"本月模型估算 ¥{float(usage['cost']):.2f}。"
            f"组件记录：{version}"
        )

    async def _audit(self, page: int) -> str:
        since = time.time() - 30 * 86400
        rows, total = await self.repository.audit_page(
            page, page_size=20, since=since
        )
        if not rows:
            return f"没有更多审计记录，共 {total} 条。"
        result_codes = {
            "success": "成",
            "rejected": "拒",
            "failed": "败",
            "warning": "警",
            "skipped": "跳",
            "dropped": "丢",
            "not_found": "无",
        }
        entries = []
        for row in rows:
            stamp = datetime.fromtimestamp(
                float(row["created_at"]), self.settings.timezone
            ).strftime("%m%d")
            action = str(row["action"])[:6]
            code = result_codes.get(str(row["result"]), "记")
            entries.append(
                f"{stamp}{action}{code}"
            )
        return f"审计{page}/{math.ceil(total / 20)}({total}):" + ";".join(entries)

    def _day(self, timestamp: float) -> str:
        return datetime.fromtimestamp(
            timestamp, self.settings.timezone
        ).date().isoformat()

