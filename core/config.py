from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when runtime configuration is incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    deepseek_api_key: str
    owner_qq: int
    napcat_ws_url: str
    timezone_name: str = "Asia/Shanghai"
    monthly_budget_cny: float = 500.0
    database_path: Path | None = None
    data_dir: Path | None = None
    backup_dir: Path | None = None
    log_dir: Path | None = None
    model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_input_price_per_million: float = 2.0
    deepseek_output_price_per_million: float = 8.0
    deepseek_search_enabled: bool = True
    deepseek_search_timeout_seconds: float = 8.0
    deepseek_search_price_cny_per_request: float = 0.01
    group_history_retention: int = 400
    group_context_messages: int = 80
    group_context_fts_limit: int = 16
    max_model_concurrency: int = 2
    model_queue_limit: int = 20
    send_limit_per_minute: int = 20
    send_limit_per_hour: int = 30
    send_limit_per_day: int = 200

    def __post_init__(self) -> None:
        root = self.project_root.resolve()
        object.__setattr__(self, "project_root", root)
        object.__setattr__(
            self,
            "data_dir",
            (self.data_dir or root / "data").resolve(),
        )
        object.__setattr__(
            self,
            "database_path",
            (self.database_path or root / "data" / "bluefish.db").resolve(),
        )
        object.__setattr__(
            self,
            "backup_dir",
            (self.backup_dir or root / "data" / "backups").resolve(),
        )
        object.__setattr__(
            self,
            "log_dir",
            (self.log_dir or root / "logs").resolve(),
        )

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"未知时区: {self.timezone_name}") from exc

    def validate_secrets(self) -> None:
        if not self.deepseek_api_key.strip():
            raise ConfigError("缺少 DEEPSEEK_API_KEY")
        if self.owner_qq <= 0:
            raise ConfigError("OWNER_QQ 必须是正整数")
        if not self.napcat_ws_url.startswith(("ws://", "wss://")):
            raise ConfigError("NAPCAT_WS_URL 必须是 ws:// 或 wss:// 地址")
        if self.monthly_budget_cny < 0:
            raise ConfigError("MONTHLY_BUDGET_CNY 不能为负数")
        if not self.model:
            raise ConfigError("DEEPSEEK_MODEL 不能为空")
        if not self.deepseek_base_url.startswith(("http://", "https://")):
            raise ConfigError("DEEPSEEK_BASE_URL 必须是 http:// 或 https:// 地址")
        if (
            self.deepseek_input_price_per_million < 0
            or self.deepseek_output_price_per_million < 0
        ):
            raise ConfigError("DeepSeek 单价不能为负数")
        if self.deepseek_search_timeout_seconds <= 0:
            raise ConfigError("DEEPSEEK_SEARCH_TIMEOUT_SECONDS 必须大于 0")
        if self.deepseek_search_price_cny_per_request < 0:
            raise ConfigError("DEEPSEEK_SEARCH_PRICE_CNY_PER_REQUEST 不能为负数")
        if self.group_context_messages < 1:
            raise ConfigError("GROUP_CONTEXT_MESSAGES 必须大于 0")
        if not (
            self.group_context_messages
            <= self.group_history_retention
        ):
            raise ConfigError(
                "GROUP_HISTORY_RETENTION 不能小于 GROUP_CONTEXT_MESSAGES"
            )
        if self.group_context_fts_limit < 0:
            raise ConfigError("GROUP_CONTEXT_FTS_LIMIT 不能为负数")

    def create_runtime_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings(
    project_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    *,
    require_secrets: bool = True,
) -> Settings:
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    values = os.environ if env is None else env
    settings = Settings(
        project_root=root,
        deepseek_api_key=values.get("DEEPSEEK_API_KEY", "").strip(),
        owner_qq=_as_int(values.get("OWNER_QQ", "0"), "OWNER_QQ"),
        napcat_ws_url=values.get("NAPCAT_WS_URL", "").strip(),
        timezone_name=values.get("TIMEZONE", "Asia/Shanghai").strip(),
        monthly_budget_cny=_as_float(
            values.get("MONTHLY_BUDGET_CNY", "500"),
            "MONTHLY_BUDGET_CNY",
        ),
        model=values.get("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        deepseek_base_url=values.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ).rstrip("/"),
        deepseek_input_price_per_million=_as_float(
            values.get("DEEPSEEK_INPUT_PRICE_PER_MILLION", "2"),
            "DEEPSEEK_INPUT_PRICE_PER_MILLION",
        ),
        deepseek_output_price_per_million=_as_float(
            values.get("DEEPSEEK_OUTPUT_PRICE_PER_MILLION", "8"),
            "DEEPSEEK_OUTPUT_PRICE_PER_MILLION",
        ),
        deepseek_search_enabled=_as_bool(
            values.get("DEEPSEEK_SEARCH_ENABLED", "true"),
            "DEEPSEEK_SEARCH_ENABLED",
        ),
        deepseek_search_timeout_seconds=_as_float(
            values.get("DEEPSEEK_SEARCH_TIMEOUT_SECONDS", "8"),
            "DEEPSEEK_SEARCH_TIMEOUT_SECONDS",
        ),
        deepseek_search_price_cny_per_request=_as_float(
            values.get("DEEPSEEK_SEARCH_PRICE_CNY_PER_REQUEST", "0.01"),
            "DEEPSEEK_SEARCH_PRICE_CNY_PER_REQUEST",
        ),
        group_history_retention=_as_int(
            values.get("GROUP_HISTORY_RETENTION", "400"),
            "GROUP_HISTORY_RETENTION",
        ),
        group_context_messages=_as_int(
            values.get("GROUP_CONTEXT_MESSAGES", "80"),
            "GROUP_CONTEXT_MESSAGES",
        ),
        group_context_fts_limit=_as_int(
            values.get("GROUP_CONTEXT_FTS_LIMIT", "16"),
            "GROUP_CONTEXT_FTS_LIMIT",
        ),
        max_model_concurrency=_as_int(
            values.get("MODEL_CONCURRENCY", "2"), "MODEL_CONCURRENCY"
        ),
        model_queue_limit=_as_int(
            values.get("MODEL_QUEUE_LIMIT", "20"), "MODEL_QUEUE_LIMIT"
        ),
        send_limit_per_minute=_as_int(
            values.get("SEND_LIMIT_PER_MINUTE", "20"),
            "SEND_LIMIT_PER_MINUTE",
        ),
        send_limit_per_hour=_as_int(
            values.get("SEND_LIMIT_PER_HOUR", "30"),
            "SEND_LIMIT_PER_HOUR",
        ),
        send_limit_per_day=_as_int(
            values.get("SEND_LIMIT_PER_DAY", "200"),
            "SEND_LIMIT_PER_DAY",
        ),
        database_path=_optional_path(values.get("DATABASE_PATH"), root),
        data_dir=_optional_path(values.get("DATA_DIR"), root),
        backup_dir=_optional_path(values.get("BACKUP_DIR"), root),
        log_dir=_optional_path(values.get("LOG_DIR"), root),
    )
    if settings.timezone_name:
        _ = settings.timezone
    if settings.max_model_concurrency != 2:
        raise ConfigError("MODEL_CONCURRENCY 按需求固定为 2")
    if settings.model_queue_limit < 1:
        raise ConfigError("MODEL_QUEUE_LIMIT 必须大于 0")
    if settings.deepseek_search_timeout_seconds <= 0:
        raise ConfigError("DEEPSEEK_SEARCH_TIMEOUT_SECONDS 必须大于 0")
    if settings.deepseek_search_price_cny_per_request < 0:
        raise ConfigError(
            "DEEPSEEK_SEARCH_PRICE_CNY_PER_REQUEST 不能为负数"
        )
    if settings.group_context_messages < 1:
        raise ConfigError("GROUP_CONTEXT_MESSAGES 必须大于 0")
    if settings.group_context_messages > settings.group_history_retention:
        raise ConfigError(
            "GROUP_HISTORY_RETENTION 不能小于 GROUP_CONTEXT_MESSAGES"
        )
    if settings.group_context_fts_limit < 0:
        raise ConfigError("GROUP_CONTEXT_FTS_LIMIT 不能为负数")
    if not (
        1
        <= settings.send_limit_per_minute
        <= settings.send_limit_per_hour
        <= settings.send_limit_per_day
    ):
        raise ConfigError(
            "发送限额必须满足 1 <= 分钟上限 <= 小时上限 <= 日上限"
        )
    if require_secrets:
        settings.validate_secrets()
    return settings


def _as_int(value: str, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数") from exc


def _as_float(value: str, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数字") from exc


def _as_bool(value: str, name: str) -> bool:
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是 true 或 false")


def _optional_path(value: str | None, root: Path) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate

