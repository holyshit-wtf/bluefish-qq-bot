from __future__ import annotations

import importlib.metadata
import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fail(message: str) -> None:
    print(f"[启动检查失败] {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    if sys.version_info[:2] != (3, 12):
        fail(
            f"当前 Python 为 {sys.version.split()[0]}，必须在 bluefish-bot "
            "Conda 环境内使用 Python 3.12"
        )
    missing = [
        name
        for name in ("aiosqlite", "jieba", "nonebot")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        fail("Python 依赖未完整安装：缺少 " + "、".join(missing))
    try:
        from core.config import ConfigError, load_settings
        from core.startup import PreflightError, probe_onebot
        from storage.database import Database
    except Exception as exc:
        fail(f"项目模块导入失败：{type(exc).__name__}: {exc}")

    try:
        settings = load_settings(ROOT)
        settings.create_runtime_dirs()
        settings.validate_secrets()
    except ConfigError as exc:
        fail(str(exc))

    async def check_runtime() -> dict[str, object]:
        try:
            login = await probe_onebot(settings)
        except PreflightError as exc:
            fail(f"{exc}。请先登录专用 QQ 并启动 NapCat。")
        database = Database(settings.database_path)
        await database.initialize()
        await database.close()
        return login

    import asyncio

    try:
        login_info = asyncio.run(check_runtime())
        with closing(sqlite3.connect(settings.database_path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            fts = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name='messages_fts' AND type='table'"
            ).fetchone()
    except Exception as exc:
        fail(f"数据库检查失败：{type(exc).__name__}: {exc}")
    if not result or result[0] != "ok":
        fail("SQLite 完整性检查未通过")
    if not fts or fts[0] != 1:
        fail("当前 SQLite 不支持或未创建 FTS5 表")

    try:
        nonebot_version = importlib.metadata.version("nonebot2")
    except importlib.metadata.PackageNotFoundError:
        nonebot_version = "unknown"
    print("启动检查通过")
    print(f"Python: {sys.version.split()[0]}")
    print(f"SQLite: {sqlite3.sqlite_version}")
    print(f"NoneBot2: {nonebot_version}")
    print(f"NapCat: {settings.napcat_ws_url}")
    print(
        f"QQ: {login_info['nickname'] or '未提供昵称'} ({login_info['user_id']})"
    )
    print(f"模型: {settings.model}")
    print(f"数据库: {settings.database_path}")


if __name__ == "__main__":
    main()

