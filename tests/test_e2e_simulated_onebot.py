from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
import websockets

from core.config import Settings
from storage.database import Database
from storage.repository import Repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_full_local_onebot_to_deepseek_to_send_loop(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        deepseek_api_key="test-key",
        owner_qq=9999,
        napcat_ws_url="ws://127.0.0.1:1",
        database_path=tmp_path / "data" / "e2e.db",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backup",
        log_dir=tmp_path / "logs",
    )
    database = Database(settings.database_path)
    await database.initialize()
    repository = Repository(database)
    await repository.enable_group(10001)
    await database.close()

    sent_group_messages: list[dict] = []
    search_requests: list[dict] = []
    send_received = asyncio.Event()
    connection_count = 0
    event_dispatched = False

    async def onebot_handler(websocket):
        nonlocal connection_count, event_dispatched
        connection_count += 1
        connection_index = connection_count
        if connection_index >= 2:
            await websocket.send(
                json.dumps(
                    {
                        "post_type": "meta_event",
                        "meta_event_type": "lifecycle",
                        "sub_type": "connect",
                        "time": 1_792_870_000,
                        "self_id": 10000,
                    }
                )
            )
        while True:
            try:
                raw_request = await websocket.recv()
            except websockets.ConnectionClosed:
                return
            request = json.loads(raw_request)
            action = request.get("action")
            if action == "get_login_info":
                data = {"user_id": 10000, "nickname": "模拟机器人"}
            elif action == "get_status":
                data = {"online": True, "good": True}
            elif action == "get_version_info":
                data = {"app_name": "NapCat", "app_version": "mock-1.0"}
            elif action == "send_group_msg":
                sent_group_messages.append(request)
                data = {"message_id": 43210}
            else:
                data = {}
            await websocket.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "echo": request.get("echo"),
                    },
                    ensure_ascii=False,
                )
            )
            if action == "send_group_msg":
                send_received.set()
            if (
                connection_index >= 2
                and action == "get_version_info"
                and not event_dispatched
            ):
                event_dispatched = True
                await websocket.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "message_type": "group",
                            "sub_type": "normal",
                            "time": 1_792_870_000,
                            "self_id": 10000,
                            "user_id": 20001,
                            "message_id": 42,
                            "message": [
                                {
                                    "type": "at",
                                    "data": {"qq": "10000"},
                                },
                                {
                                    "type": "text",
                                    "data": {"text": " 请回复最新游戏攻略"},
                                },
                            ],
                            "raw_message": (
                                "[CQ:at,qq=10000] 请回复最新游戏攻略"
                            ),
                            "font": 0,
                            "sender": {
                                "user_id": 20001,
                                "nickname": "本地联调",
                                "role": "member",
                            },
                            "group_id": 10001,
                            "to_me": True,
                        },
                        ensure_ascii=False,
                    )
                )

    async def http_handler(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = 0
            for line in headers.decode("latin-1").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            request_line = headers.decode("latin-1").split("\r\n", 1)[0]
            path = request_line.split(" ")[1]
            payload = {}
            if content_length:
                payload = json.loads(
                    await reader.readexactly(content_length)
                )
            if path == "/v1/tools/search":
                search_requests.append(payload)
                response_body = {
                    "results": [
                        {
                            "title": "本地联调攻略",
                            "url": "https://example.com/guide",
                            "snippet": "先找钥匙再开门。",
                            "summary": "第一先找钥匙，第二再打开东侧门。",
                            "site_name": "联调资料站",
                            "date": None,
                            "authority": 3,
                            "rerank_score": 2.5,
                        }
                    ]
                }
            else:
                response_body = {
                    "choices": [
                        {"message": {"content": "**本地联调回复。**"}}
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                    },
                }
            body = json.dumps(
                response_body,
                ensure_ascii=False,
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    http_server = await asyncio.start_server(
        http_handler, "127.0.0.1", 0
    )
    http_port = http_server.sockets[0].getsockname()[1]
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"

    process = None
    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    try:
        async with websockets.serve(
            onebot_handler, "127.0.0.1", 0
        ) as onebot_server:
            onebot_port = onebot_server.sockets[0].getsockname()[1]
            env = os.environ.copy()
            env.update(
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{http_port}",
                    "OWNER_QQ": "9999",
                    "NAPCAT_WS_URL": f"ws://127.0.0.1:{onebot_port}",
                    "NAPCAT_ACCESS_TOKEN": "mock-token",
                    "DRIVER": "~websockets",
                    "DATABASE_PATH": str(settings.database_path),
                    "DATA_DIR": str(settings.data_dir),
                    "BACKUP_DIR": str(settings.backup_dir),
                    "LOG_DIR": str(settings.log_dir),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1",
                }
            )
            env.pop("ONEBOT_WS_URLS", None)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "bot.py",
                cwd=PROJECT_ROOT,
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            await asyncio.wait_for(send_received.wait(), timeout=20)
            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                with closing(
                    sqlite3.connect(settings.database_path)
                ) as connection:
                    bot_count = connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE role='bot'"
                    ).fetchone()[0]
                if bot_count or process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        http_server.close()
        await http_server.wait_closed()
        stdout_file.close()
        stderr_file.close()

    assert sent_group_messages
    sent = sent_group_messages[0]
    assert int(sent["params"]["group_id"]) == 10001
    rendered = json.dumps(sent["params"], ensure_ascii=False)
    assert '"type": "reply"' in rendered
    assert '"id": "42"' in rendered
    assert "本地联调回复。" in rendered
    assert "**" not in rendered
    assert search_requests
    assert search_requests[0]["ori_question"] == "请回复最新游戏攻略"
    verify_database = Database(settings.database_path)
    await verify_database.initialize()
    roles = await verify_database.fetchall(
        "SELECT role, content FROM messages ORDER BY id"
    )
    assert [row["role"] for row in roles] == ["user", "bot"]
    assert roles[-1]["content"] == "本地联调回复。"
    send_row = await verify_database.fetchone(
        "SELECT status, onebot_message_id FROM send_events ORDER BY id DESC LIMIT 1"
    )
    assert send_row["status"] == "sent"
    assert send_row["onebot_message_id"] == 43210
    usage = await verify_database.fetchone(
        "SELECT input_tokens, output_tokens, success FROM api_usage "
        "WHERE call_type='group_chat'"
    )
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 8
    search_usage = await verify_database.fetchone(
        "SELECT estimated_cost_cny, success FROM api_usage "
        "WHERE call_type='web_search'"
    )
    assert search_usage["estimated_cost_cny"] > 0
    assert search_usage["success"] == 1
    await verify_database.close()
    diagnostics = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in diagnostics
