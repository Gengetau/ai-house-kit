"""One-shot connectivity check against a OneBot v11 forward-WebSocket server (used by `ai-house doctor --onebot-ping`).

It connects the same way the channel does (token only in the Authorization header), asks `get_version_info` and
`get_login_info`, and reports in plain words what is wrong. It never sends a chat message, never prints the
token and never prints an account number.
"""
from __future__ import annotations

import json
import re
import socket
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse

_DOCKER_HINT = "如果小屋跑在 Docker 里而协议端在宿主机上，地址要写 ws://host.docker.internal:端口，不能写 127.0.0.1"


_FIREWALL_HINT = (
    "多半是宿主机防火墙把 Docker 网络发来的包丢掉了：请放行 Docker 网桥网段访问这个端口，"
    "或者让小屋用 host 网络运行并改用 ws://127.0.0.1:端口"
)


def _unreachable_hint(url: str, exc: BaseException) -> str:
    host = (urlparse(url).hostname or "").lower()
    if isinstance(exc, TimeoutError) and "handshake" in str(exc):
        return "端口能连上，但对方没有应答 WebSocket 握手：这个端口可能不是 OneBot 的正向 WebSocket 端口"
    if host in ("127.0.0.1", "localhost", "::1"):
        return f"协议端没有启动、端口不对，或被防火墙挡住。{_DOCKER_HINT}"
    if isinstance(exc, TimeoutError):  # packets silently dropped: nothing refused us, nothing answered either
        if host == "host.docker.internal":
            return f"一直没有应答（超时）。{_FIREWALL_HINT}"
        return "一直没有应答（超时）：地址或端口不对，或者中间有防火墙把包丢掉了"
    if host == "host.docker.internal" and isinstance(exc, socket.gaierror):
        return "解析不了 host.docker.internal：不在 Docker 里运行就改用 127.0.0.1；Linux 上的 Docker 需要加 extra_hosts: host.docker.internal:host-gateway"
    if host == "host.docker.internal":  # the address is already right, so do not tell them to change it
        return "协议端没有在宿主机上监听这个端口，或只监听了 127.0.0.1（要监听 0.0.0.0）；也请核对端口号"
    return f"协议端没有启动、端口不对，或被防火墙挡住。{_DOCKER_HINT}"


def _result(ok: bool, code: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": ok, "code": code, "detail": detail, **extra}


def _scrub(text: str, token: str) -> str:
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    return re.sub(r"\d{5,}", "***", out)[:200]


def _where(url: str) -> str:
    p = urlparse(url)
    return f"{p.hostname or '?'}:{p.port or (443 if p.scheme == 'wss' else 80)}"


def probe_onebot(
    url: str,
    token: str = "",
    *,
    owner_ids: Iterable[str] = (),
    timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """Connect, ask who is logged in, disconnect. Returns {"ok", "code", "detail", ...}; never raises."""
    url = str(url or "").strip()
    token = str(token or "").strip()
    if not url.startswith(("ws://", "wss://")):
        return _result(False, "bad_url", "ONEBOT_WS_URL 必须以 ws:// 或 wss:// 开头")
    try:
        from websockets.exceptions import InvalidHandshake, InvalidMessage, InvalidStatus, WebSocketException
        from websockets.sync.client import connect
    except ImportError:
        return _result(False, "no_websockets", '缺少 websockets 依赖，请运行 `pip install ".[onebot]"`')

    where = _where(url)
    headers = {"Authorization": f"Bearer {token}"} if token else None
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    try:
        ws = connect(url, additional_headers=headers, open_timeout=max(1.0, float(timeout_s)), max_size=1024 * 1024)
    except InvalidStatus as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status in (401, 403):
            hint = "ONEBOT_ACCESS_TOKEN 和协议端里设的令牌不一致" if token else "协议端设了令牌，但 ONEBOT_ACCESS_TOKEN 是空的"
            hint += "；地址指到协议端的 HTTP 接口端口（它有自己的令牌）而不是正向 WebSocket 端口时，也会这样"
            return _result(False, "unauthorized", f"{where} 拒绝了连接（HTTP {status}）：{hint}")
        return _result(False, "bad_handshake", f"{where} 不是 OneBot 的正向 WebSocket 端口（握手返回 HTTP {status}）；请确认协议端开启了「正向 WebSocket 服务」而不是 HTTP 或反向 WebSocket")
    except InvalidMessage:  # the peer answered, but not with an HTTP/WebSocket handshake (e.g. a plain HTTP port or garbage)
        return _result(False, "bad_handshake", f"{where} 不是 OneBot 的正向 WebSocket 端口（对方没有按 WebSocket 握手应答）；请确认协议端开启了「正向 WebSocket 服务」而不是 HTTP 或反向 WebSocket")
    except InvalidHandshake as exc:
        return _result(False, "bad_handshake", f"{where} 握手失败（{type(exc).__name__}）；请确认这是协议端的正向 WebSocket 端口")
    except (OSError, TimeoutError, WebSocketException) as exc:
        return _result(False, "unreachable", f"连不上 {where}（{type(exc).__name__}）：{_unreachable_hint(url, exc)}")
    except Exception as exc:  # noqa: BLE001 - a doctor check must not crash the report
        return _result(False, "unreachable", f"连不上 {where}（{_scrub(f'{type(exc).__name__}: {exc}', token)}）")

    answers: Dict[str, Dict[str, Any]] = {}
    try:
        with ws:
            ws.send(json.dumps({"action": "get_version_info", "params": {}, "echo": "doctor-version"}))
            ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "doctor-login"}))
            while len(answers) < 2:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    raw = ws.recv(timeout=left)
                except TimeoutError:
                    break
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and str(data.get("echo") or "").startswith("doctor-"):
                    answers[str(data["echo"])] = data
    except (OSError, WebSocketException) as exc:
        if "doctor-login" not in answers:
            return _result(False, "dropped", f"已连上 {where}，但连接在应答前断开了（{type(exc).__name__}）；请看协议端日志")

    version = answers.get("doctor-version") or {}
    vdata = version.get("data") if isinstance(version.get("data"), dict) else {}
    app = " ".join(x for x in (str(vdata.get("app_name") or "").strip(), str(vdata.get("app_version") or "").strip()) if x)
    app = re.sub(r"[^\w .+\-]", "", app)[:60]
    label = f"{where}（{app}）" if app else where

    login: Optional[Dict[str, Any]] = answers.get("doctor-login")
    if login is None:
        return _result(False, "no_answer", f"已连上 {label}，但 {int(timeout_s)} 秒内没有收到应答；这个端口可能不是 OneBot v11 接口", app=app)
    retcode = login.get("retcode")
    ldata = login.get("data") if isinstance(login.get("data"), dict) else {}
    bot_id = str(ldata.get("user_id") or "").strip()
    if retcode in (1401, 1403):
        return _result(False, "unauthorized", f"已连上 {label}，但协议端不认这个令牌（retcode {retcode}）：请核对 ONEBOT_ACCESS_TOKEN", app=app)
    if str(login.get("status") or "") != "ok" or retcode not in (0, None) or not bot_id or bot_id == "0":
        return _result(False, "not_logged_in", f"已连上 {label}，但协议端还没有登录 QQ（retcode {retcode}）；请先在协议端完成登录", app=app)
    if bot_id in {str(x).strip() for x in owner_ids if str(x).strip()}:
        return _result(
            False, "owner_is_bot",
            f"已连上 {label} 且已登录，但 ONEBOT_OWNER_QQ 里填的是室友自己登录的这个号；那里要填你本人的 QQ 号", app=app,
        )
    return _result(True, "ok", f"已连上 {label}，协议端已登录 QQ", app=app)
