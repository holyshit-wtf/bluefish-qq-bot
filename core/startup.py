from __future__ import annotations

import asyncio
import json
import socket
from typing import Any
from urllib.parse import urlparse

import websockets

from core.config import Settings


class PreflightError(RuntimeError):
    pass


async def probe_onebot(settings: Settings, timeout: float = 6.0) -> dict[str, Any]:
    parsed = urlparse(settings.napcat_ws_url)
    if not parsed.hostname:
        raise PreflightError("NAPCAT_WS_URL 缺少主机名")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        await asyncio.to_thread(
            _check_tcp,
            (parsed.hostname, port),
            timeout,
        )
    except OSError as exc:
        raise PreflightError(
            f"无法连接 NapCat WebSocket {parsed.hostname}:{port}（{exc}）"
        ) from exc

    headers: dict[str, str] = {}
    token = _access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with websockets.connect(
            settings.napcat_ws_url,
            additional_headers=headers or None,
            open_timeout=timeout,
            close_timeout=2,
        ) as websocket:
            login = await _call_action(websocket, "get_login_info", timeout)
            user_id = _action_data(login).get("user_id")
            try:
                numeric_user_id = int(user_id)
            except (TypeError, ValueError) as exc:
                raise PreflightError(
                    "NapCat 已连接，但 QQ 尚未登录（无法取得登录账号）"
                ) from exc
            if numeric_user_id <= 0:
                raise PreflightError("NapCat 已连接，但 QQ 尚未登录")

            status = await _call_action(websocket, "get_status", timeout)
            status_data = _action_data(status)
            if status_data.get("online") is False or status_data.get("good") is False:
                raise PreflightError("OneBot 报告当前账号离线或连接状态异常")
            return {
                "user_id": numeric_user_id,
                "nickname": str(_action_data(login).get("nickname") or ""),
                "status": status_data,
            }
    except PreflightError:
        raise
    except asyncio.TimeoutError as exc:
        raise PreflightError("NapCat WebSocket 握手或 OneBot API 调用超时") from exc
    except Exception as exc:
        raise PreflightError(
            f"NapCat WebSocket 握手失败：{type(exc).__name__}: {exc}"
        ) from exc


async def _call_action(
    websocket: Any, action: str, timeout: float
) -> dict[str, Any]:
    echo = f"preflight-{action}"
    await websocket.send(
        json.dumps(
            {"action": action, "params": {}, "echo": echo},
            ensure_ascii=False,
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(websocket.recv(), remaining)
        try:
            response = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PreflightError(f"{action} 返回值不是有效 JSON") from exc
        if not isinstance(response, dict):
            raise PreflightError(f"{action} 返回值结构异常")
        if response.get("echo") != echo:
            continue
        if (
            response.get("status") not in {None, "ok"}
            or response.get("retcode") not in {None, 0}
        ):
            raise PreflightError(
                f"{action} 失败：{response.get('retcode', 'unknown')}"
            )
        return response


def _action_data(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _access_token() -> str:
    import os

    return os.environ.get("NAPCAT_ACCESS_TOKEN", "").strip()


def _check_tcp(address: tuple[str, int], timeout: float) -> None:
    with socket.create_connection(address, timeout):
        pass

