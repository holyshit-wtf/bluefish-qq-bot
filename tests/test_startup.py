from __future__ import annotations

import json
from dataclasses import replace

import pytest
import websockets

from core.startup import PreflightError, probe_onebot


@pytest.mark.asyncio
async def test_onebot_startup_probe_checks_login_and_status(
    settings, monkeypatch
):
    monkeypatch.setenv("NAPCAT_ACCESS_TOKEN", "secret-token")
    seen_actions: list[str] = []
    seen_authorization: list[str] = []

    async def handler(websocket):
        seen_authorization.append(websocket.request.headers.get("Authorization", ""))
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
        for _ in range(2):
            request = json.loads(await websocket.recv())
            action = request["action"]
            seen_actions.append(action)
            if action == "get_login_info":
                data = {"user_id": 10000, "nickname": "测试机器人"}
            else:
                data = {"online": True, "good": True}
            await websocket.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "echo": request["echo"],
                    }
                )
            )

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = replace(
            settings, napcat_ws_url=f"ws://127.0.0.1:{port}"
        )
        result = await probe_onebot(configured)

    assert result["user_id"] == 10000
    assert result["nickname"] == "测试机器人"
    assert seen_actions == ["get_login_info", "get_status"]
    assert seen_authorization == ["Bearer secret-token"]


@pytest.mark.asyncio
async def test_onebot_startup_probe_rejects_logged_out_account(
    settings, monkeypatch
):
    monkeypatch.delenv("NAPCAT_ACCESS_TOKEN", raising=False)

    async def handler(websocket):
        request = json.loads(await websocket.recv())
        await websocket.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 0},
                    "echo": request["echo"],
                }
            )
        )

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = replace(
            settings, napcat_ws_url=f"ws://127.0.0.1:{port}"
        )
        with pytest.raises(PreflightError, match="尚未登录"):
            await probe_onebot(configured)


@pytest.mark.asyncio
async def test_onebot_startup_probe_rejects_nonzero_retcode(
    settings, monkeypatch
):
    monkeypatch.delenv("NAPCAT_ACCESS_TOKEN", raising=False)

    async def handler(websocket):
        request = json.loads(await websocket.recv())
        await websocket.send(
            json.dumps(
                {
                    "status": "failed",
                    "retcode": 1404,
                    "data": {},
                    "echo": request["echo"],
                }
            )
        )

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = replace(
            settings, napcat_ws_url=f"ws://127.0.0.1:{port}"
        )
        with pytest.raises(PreflightError, match="get_login_info 失败"):
            await probe_onebot(configured)
