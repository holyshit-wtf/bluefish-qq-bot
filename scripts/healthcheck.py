from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import ConfigError, load_settings


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        kernel32.CloseHandle(process)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    try:
        settings = load_settings(ROOT)
    except ConfigError as exc:
        print(f"[健康检查失败] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not settings.database_path.exists():
        print("数据库不存在，机器人尚未完成首次启动。")
        raise SystemExit(1)

    try:
        with closing(sqlite3.connect(settings.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("SQLite 完整性检查失败")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = [
                "groups",
                "messages",
                "private_messages",
                "group_memes",
                "user_profiles",
                "audit_log",
                "api_usage",
                "send_events",
            ]
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tables
            }
            paused = connection.execute(
                "SELECT value FROM system_settings WHERE key='global_paused'"
            ).fetchone()
            versions = connection.execute(
                "SELECT value FROM system_settings WHERE key='runtime_versions'"
            ).fetchone()
    except Exception as exc:
        print(f"[健康检查失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    lock_path = settings.data_dir / "bot.lock"
    running_pid: int | None = None
    if lock_path.exists():
        try:
            candidate = int(lock_path.read_text(encoding="ascii").strip())
            if pid_is_running(candidate):
                running_pid = candidate
        except (OSError, ValueError):
            pass

    print("健康检查通过")
    print(f"数据库版本: {version}")
    print(f"机器人进程: {f'运行中 (PID {running_pid})' if running_pid else '未运行'}")
    print(f"全局状态: {'暂停' if paused and paused[0] == '1' else '运行'}")
    print(f"数据表计数: {counts}")
    if versions:
        try:
            runtime = json.loads(versions[0])
        except (TypeError, json.JSONDecodeError):
            runtime = versions[0]
        print(f"最近运行版本: {runtime}")


if __name__ == "__main__":
    main()
