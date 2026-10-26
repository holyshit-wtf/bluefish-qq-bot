from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from contextlib import closing
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    file_handler = TimedRotatingFileHandler(
        log_dir / "bluefish.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError as exc:
        print(f"[启动失败] Python 依赖缺失：{exc}", file=sys.stderr)
        return 2
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    napcat_url = os.environ.get("NAPCAT_WS_URL", "").strip()
    if napcat_url:
        os.environ["ONEBOT_WS_URLS"] = json.dumps([napcat_url])
    token = os.environ.get("NAPCAT_ACCESS_TOKEN")
    if token and not os.environ.get("ONEBOT_ACCESS_TOKEN"):
        os.environ["ONEBOT_ACCESS_TOKEN"] = token

    from core.config import ConfigError, load_settings
    from core.instance_lock import (
        InstanceAlreadyRunningError,
        InstanceLock,
    )

    lock = None
    try:
        import nonebot
        from nonebot.adapters.onebot.v11 import Adapter

        from core.startup import PreflightError, probe_onebot
        from storage.database import Database

        settings = load_settings(PROJECT_ROOT)
        settings.create_runtime_dirs()
        configure_logging(settings.log_dir)
        lock = InstanceLock(settings.data_dir / "bot.lock")
        lock.acquire()

        async def startup_checks() -> dict[str, object]:
            login = await probe_onebot(settings)
            database = Database(settings.database_path)
            await database.initialize()
            await database.close()
            with closing(sqlite3.connect(settings.database_path)) as connection:
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                fts = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='messages_fts'"
                ).fetchone()
            if not integrity or integrity[0] != "ok":
                raise PreflightError("SQLite 完整性检查未通过")
            if not fts or fts[0] != 1:
                raise PreflightError("SQLite FTS5 未启用")
            return login

        login_info = asyncio.run(startup_checks())
        nonebot.init()
        nonebot.get_driver().register_adapter(Adapter)
        nonebot.load_plugin("plugins.bluefish")
        logging.getLogger(__name__).info(
            "启动检查通过：QQ %s (%s), Python %s, SQLite %s",
            login_info.get("nickname") or "未提供昵称",
            login_info.get("user_id"),
            sys.version.split()[0],
            sqlite3.sqlite_version,
        )
        nonebot.run()
        return 0
    except ModuleNotFoundError as exc:
        logging.getLogger(__name__).error("Python 依赖缺失：%s", exc)
        print(f"[启动失败] Python 依赖缺失：{exc}", file=sys.stderr)
        return 2
    except (ConfigError, InstanceAlreadyRunningError, PreflightError) as exc:
        logging.getLogger(__name__).error("启动失败：%s", exc)
        print(f"[启动失败] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

