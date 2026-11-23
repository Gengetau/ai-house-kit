"""Unified CLI entrypoint (`ai-house`) for ai-house-kit."""
from __future__ import annotations

import argparse
import contextlib
import errno
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from worldkernel import vocab_life1

from .auth import BindRefused, check_bind
from .backup import create_backup, restore_backup, verify_backup
from .builtin_plugins.onebot_ws import ChannelConfigError
from .chat_agent import LLMAPIError, mask_secrets, openai_compatible_chat
from .daemon import HouseDaemon, make_http_server
from .plugin_api import PluginRegistry


def _parse_dotenv_file(path: Path) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Parse KEY=VALUE lines; return (values, errors, warnings).

    Docker Compose `.env` does NOT strip unquoted trailing `# comments`, so flag them in `doctor`
    while still stripping safely during native `ai-house up`.
    """
    values: Dict[str, str] = {}
    errors: List[str] = []
    warnings: List[str] = []
    if not path.is_file():
        return values, errors, warnings
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"{path.name}:{lineno} 缺少 '='，无法解析")
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if key == "ONEBOT_HTTP_URL":
            errors.append(f"{path.name}:{lineno} 使用了已废弃的 ONEBOT_HTTP_URL；请改用 ONEBOT_WS_URL（参考 docs/CHAT-AND-QQ.md）")
        if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
            val = val[1:-1]
        else:
            m = re.search(r"\s+#", val)
            if m:
                errors.append(f"{path.name}:{lineno} ({key}) 存在未加引号的行尾注释，Docker Compose 会把 '#' 当作变量值一部分")
                val = val[: m.start()].strip()
        values[key] = val
    return values, errors, warnings


def _load_dotenv(path: str = ".env") -> None:
    vals, _, _ = _parse_dotenv_file(Path(path))
    for k, v in vals.items():
        if not (os.environ.get(k) or "").strip():
            os.environ[k] = v


def cmd_up(args: argparse.Namespace) -> int:
    _load_dotenv(args.env_file)
    host = args.host or os.environ.get("AI_HOUSE_HOST") or "127.0.0.1"
    port = args.port if args.port is not None else int(os.environ.get("AI_HOUSE_PORT") or "8780")
    data_dir = args.data_dir or os.environ.get("AI_HOUSE_DATA_DIR") or "./runtime"
    pack = args.pack if args.pack is not None else (os.environ.get("AI_HOUSE_PACK_DIR") or None)
    try:
        check_bind(host, os.environ)
    except BindRefused as exc:
        print(f"[ai-house] {exc}", file=sys.stderr, flush=True)
        return 2
    try:
        daemon = HouseDaemon(
            runtime_dir=data_dir,
            pack_dir=pack,
            packs_root=args.packs_root,
            step_interval_s=args.step_seconds,
            virtual_clock=args.virtual_clock,
            allow_onboarding=(pack is None),
            env_file=args.env_file,
        )
    except ChannelConfigError as exc:
        print(f"[ai-house] QQ 接入配置有问题，没有启动：{exc}", file=sys.stderr, flush=True)
        return 2
    try:
        srv = make_http_server(daemon, host=host, port=port)
    except OSError as exc:
        daemon.close()
        winerr = getattr(exc, "winerror", None)
        if exc.errno in (errno.EADDRINUSE, 48, 98, 10048) or winerr in (10048, 10013):
            print(
                f"[ai-house] 端口 {host}:{port} 已被占用（可能已有 AI House 实例正在运行，或被其他程序占用）；"
                f"可改用 --port <端口> 启动，或先停止占用进程。",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"[ai-house] 无法监听 {host}:{port}：{exc}", file=sys.stderr, flush=True)
        return 2
    daemon.start_background_loop()
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    listen_note = f"（监听 {host}:{port}，若在容器内请访问宿主机映射端口）" if display_host != host else ""
    mode_label = (
        f"真实大模型 ({daemon.chat_agent.model})"
        if daemon.chat_agent.live_llm_enabled
        else "零配置内置演示模式（可在网页向导或 .env 填入 AI_HOUSE_API_KEY 切换）"
    )
    pack_line = (
        "待初始化（打开网页控制台创建你的第 1 位 AI 住客）"
        if daemon.needs_onboarding
        else f"{daemon.vocab.MANIFEST.get('profile')}（室友：{'、'.join(daemon.vocab.NAMES.values())}）"
    )
    print(
        f"\n🏡 AI House Studio 已启动！{listen_note}\n"
        f"   • 可视化工作台与群聊面板: http://{display_host}:{port}\n"
        f"   • 标准 Streamable MCP 端点: http://{display_host}:{port}/mcp\n"
        f"   • 健康检查接口:           http://{display_host}:{port}/api/health\n"
        f"   • 当前世界包: {pack_line}\n"
        f"   • 模型模式: {mode_label}\n",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ai-house] 正在保存状态并退出...", flush=True)
    finally:
        srv.server_close()
        daemon.close()
    return 0


def _roster_json_for_pack(pack_spec: Any) -> str:
    items = []
    for aid in pack_spec.ROSTER:
        meta = pack_spec.PEOPLE.get(aid) or {}
        items.append({
            "actor_id": aid,
            "display_name": pack_spec.NAMES.get(aid, aid),
            "home_district": "sakuraoka",
            "bio": str(meta.get("bio") or f"{pack_spec.NAMES.get(aid, aid)}，来自 AI 小屋的住客。")[:80],
        })
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def cmd_init(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).resolve()
    if env_path.exists() and not args.force:
        print(f"[ai-house] {env_path} 已存在；如需覆盖请加 --force", file=sys.stderr, flush=True)
        return 1
    pack_raw = (args.pack or "").strip()
    roster_json = ""
    if pack_raw:
        try:
            v = vocab_life1.load_pack(pack_raw)
        except FileNotFoundError as exc:
            print(f"[ai-house] {exc}", file=sys.stderr, flush=True)
            return 2
        roster_json = _roster_json_for_pack(v)

    owner_token = secrets.token_urlsafe(24)
    bridge_key = secrets.token_urlsafe(24)
    lines = [
        "# Generated by `ai-house init`",
        "AI_HOUSE_API_KEY=",
        "AI_HOUSE_BASE_URL=https://api.openai.com",
        "AI_HOUSE_MODEL=gpt-4o-mini",
        "AI_HOUSE_HOST=127.0.0.1",
        f"AI_HOUSE_PORT={int(args.port)}",
        f"AI_HOUSE_OWNER_TOKEN={owner_token}",
        f"AI_HOUSE_PACK_DIR={pack_raw}",
        f"BOTGAME_HOUSE_ROSTER_JSON={roster_json}",
        "WORLD_KERNEL_CITY_BRIDGE=0",
        "CITY_PROFILE=qingkong",
        "CITY_BRIDGE_URL=http://qingkong-city:7700/city/bridge/v1",
        f"CITY_BRIDGE_KEY={bridge_key}",
        "ONEBOT_WS_URL=",
        "ONEBOT_ACCESS_TOKEN=",
        "ONEBOT_OWNER_QQ=",
        "ONEBOT_ACTOR=",
        "ONEBOT_GROUPS=",
        "",
    ]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    rt = Path(args.data_dir).resolve()
    rt.mkdir(parents=True, exist_ok=True)

    print(
        f"✅ 已初始化配置文件: {env_path}\n"
        f"   • 数据目录: {rt}\n"
        f"   • 房主访问令牌 (AI_HOUSE_OWNER_TOKEN): {owner_token}\n"
        f"   • 已自动生成 AI_HOUSE_OWNER_TOKEN 与 CITY_BRIDGE_KEY（切勿提交到 Git；后续可用 ai-house token 查看）\n"
        f"   • 下一步运行环境自检: ai-house doctor\n"
        f"   • 直接本地启动:       ai-house up\n"
        f"   • 容器一键启动:       docker compose up -d\n",
        flush=True,
    )
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).resolve()
    vals, _errs, _warns = _parse_dotenv_file(env_path)
    env_tok = os.environ.get("AI_HOUSE_OWNER_TOKEN", "").strip()
    dotenv_tok = (vals.get("AI_HOUSE_OWNER_TOKEN") or "").strip()
    if env_tok:
        owner_token = env_tok
        source = "env"
    elif dotenv_tok:
        owner_token = dotenv_tok
        source = "dotenv"
    else:
        owner_token = ""
        source = "unset"

    if args.json:
        print(
            json.dumps(
                {
                    "ok": bool(owner_token),
                    "env_file": str(env_path),
                    "owner_token": owner_token,
                    "source": source,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if owner_token else 1

    if owner_token:
        lines = [
            f"配置文件: {env_path}",
            f"AI_HOUSE_OWNER_TOKEN: {owner_token}",
        ]
        if source == "env":
            lines.append("生效来源: 环境变量 AI_HOUSE_OWNER_TOKEN（覆盖配置文件）")
        print("\n".join(lines), flush=True)
        return 0

    print(
        f"[ai-house] 未在 {env_path} 或环境变量中找到 AI_HOUSE_OWNER_TOKEN；"
        f"可运行 `ai-house init` 生成，本机 127.0.0.1 默认免密钥直接访问。",
        file=sys.stderr,
        flush=True,
    )
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file)
    parsed, env_errs, env_warns = _parse_dotenv_file(env_path)
    eff_env = {**parsed, **{k: v for k, v in os.environ.items() if v != ""}}
    host = args.host or eff_env.get("AI_HOUSE_HOST") or "127.0.0.1"
    data_dir = Path(args.data_dir or eff_env.get("AI_HOUSE_DATA_DIR") or "./runtime")
    pack_arg = args.pack if args.pack is not None else (eff_env.get("AI_HOUSE_PACK_DIR") or None)

    checks: List[Dict[str, Any]] = []
    checks.append({
        "name": "python",
        "ok": sys.version_info >= (3, 11),
        "detail": f"{sys.version.split()[0]} (requires >=3.11)",
    })
    if env_path.is_file():
        checks.append({
            "name": "dotenv",
            "ok": len(env_errs) == 0,
            "detail": "; ".join(env_errs) if env_errs else f"{env_path} 语法 cleanly valid",
        })
    else:
        checks.append({
            "name": "dotenv",
            "ok": True,
            "detail": f"{env_path} 尚未创建（本机 127.0.0.1 可免配置演示启动，推荐 `ai-house init`）",
        })

    try:
        check_bind(host, eff_env)
        checks.append({"name": "bind_security", "ok": True, "detail": f"host={host} 安全校验通过"})
    except BindRefused as exc:
        checks.append({"name": "bind_security", "ok": False, "detail": str(exc)})

    if eff_env.get("WORLD_KERNEL_CITY_BRIDGE") == "1":
        bkey = eff_env.get("CITY_BRIDGE_KEY", "").strip()
        checks.append({
            "name": "city_bridge",
            "ok": len(bkey) >= 24,
            "detail": "CITY_BRIDGE_KEY 已配置" if len(bkey) >= 24 else "启用 WORLD_KERNEL_CITY_BRIDGE=1 时 CITY_BRIDGE_KEY 至少需 24 位",
        })

    ws_url = eff_env.get("ONEBOT_WS_URL", "").strip()
    if ws_url:
        ob_errs = []
        from urllib.parse import urlparse

        parsed_ws = urlparse(ws_url)
        if not ws_url.startswith(("ws://", "wss://")):
            ob_errs.append("ONEBOT_WS_URL 必须以 ws:// 或 wss:// 开头")
        elif parsed_ws.query or parsed_ws.username or parsed_ws.password:
            ob_errs.append("ONEBOT_WS_URL 里不能带 ?access_token= 或账号密码，请放进 ONEBOT_ACCESS_TOKEN")
        owners_raw = eff_env.get("ONEBOT_OWNER_QQ", "").strip()
        owner_parts = [x for x in re.split(r"[,\s]+", owners_raw) if x]
        if not owner_parts or not all(x.isdigit() for x in owner_parts):
            ob_errs.append("必须填写纯数字 ONEBOT_OWNER_QQ（多个用逗号分隔）")
        groups_raw = eff_env.get("ONEBOT_GROUPS", "").strip()
        if groups_raw:
            grp_parts = [x for x in re.split(r"[,\s]+", groups_raw) if x]
            if not all(x.isdigit() for x in grp_parts):
                ob_errs.append("ONEBOT_GROUPS 必须为纯数字群号（多个用逗号分隔）")
        if importlib.util.find_spec("websockets") is None:
            ob_errs.append("缺少 websockets 依赖，请运行 `pip install .[onebot]`")
        checks.append({
            "name": "onebot_ws",
            "ok": len(ob_errs) == 0,
            "detail": "; ".join(ob_errs) if ob_errs else "OneBot v11 WebSocket 配置与依赖就绪",
        })

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".doctor_probe.sqlite"
        with contextlib.closing(sqlite3.connect(str(probe))) as c:
            with c:
                c.execute("CREATE TABLE IF NOT EXISTS _p(x INTEGER)")
                c.execute("INSERT INTO _p VALUES(1)")
        probe.unlink(missing_ok=True)
        checks.append({"name": "data_dir_sqlite", "ok": True, "detail": f"{data_dir} 可写且 SQLite 正常"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "data_dir_sqlite", "ok": False, "detail": f"{data_dir} 不可写: {exc}"})

    try:
        v = vocab_life1.load_pack(pack_arg)
        checks.append({
            "name": "worldpack",
            "ok": True,
            "detail": f"{v.MANIFEST.get('profile')}（{len(v.ROSTER)} 人：{'、'.join(v.NAMES.values())}）",
        })
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "worldpack", "ok": False, "detail": str(exc)})

    api_key = eff_env.get("AI_HOUSE_API_KEY", "").strip()
    if args.live_ping:
        if not api_key:
            checks.append({"name": "llm_live_ping", "ok": False, "detail": "指定了 --live-ping 但未配置 AI_HOUSE_API_KEY"})
        else:
            try:
                base_url = eff_env.get("AI_HOUSE_BASE_URL", "https://api.openai.com").strip()
                model = eff_env.get("AI_HOUSE_MODEL", "gpt-4o-mini").strip()
                ans, toks = openai_compatible_chat("Reply OK.", "ping", api_key=api_key, base_url=base_url, model=model, timeout_s=12.0)
                checks.append({"name": "llm_live_ping", "ok": bool(ans), "detail": f"模型 {model} 连通 ({toks} tokens)"})
            except LLMAPIError as exc:
                checks.append({"name": "llm_live_ping", "ok": False, "detail": f"连通失败: {exc}"})
            except Exception as exc:  # noqa: BLE001
                checks.append({
                    "name": "llm_live_ping",
                    "ok": False,
                    "detail": f"连通失败: {mask_secrets(f'{type(exc).__name__}: {exc}', api_key=api_key)}",
                })
    else:
        checks.append({
            "name": "llm_mode",
            "ok": True,
            "detail": f"真实模型 ({eff_env.get('AI_HOUSE_MODEL', 'gpt-4o-mini')})" if api_key else "零配置演示模式（未填 AI_HOUSE_API_KEY）",
        })

    all_ok = all(c["ok"] for c in checks)
    if args.json:
        print(json.dumps({"ok": all_ok, "checks": checks, "warnings": env_warns}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            mark = "✅" if c["ok"] else "❌"
            print(f"{mark} [{c['name']}] {c['detail']}")
    return 0 if all_ok else 1


def cmd_plugins(args: argparse.Namespace) -> int:
    reg = PluginRegistry().discover(packs_root=Path(args.packs_root))
    doc = {
        "packs": sorted(reg.packs.keys()),
        "channels": ["webchat"] + sorted(reg.channels.keys()),
        "extensions": ["outings", "avatar", "chatlink", "city_bridge"] + sorted(reg.extensions.keys()),
        "providers": sorted(reg.providers.keys()),
        "tools": sorted(reg.tools.keys()),
        "manifests": [d.manifest.to_schema_dict() | {"source": d.source} for d in reg.definitions.values()],
        "load_errors": [e.to_dict() for e in reg.load_errors],
    }
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0


def cmd_pack_check(args: argparse.Namespace) -> int:
    try:
        v = vocab_life1.load_pack(args.pack)
    except FileNotFoundError as exc:
        print(f"[ai-house] {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps({
        "ok": True,
        "profile": v.MANIFEST.get("profile"),
        "roster": list(v.ROSTER),
        "names": v.NAMES,
        "cook_order": list(v.COOK_ORDER),
        "rooms": v.HOUSE["rooms"],
        "recipes": len(v.RECIPES),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    _load_dotenv(args.env_file)
    try:
        if args.verify:
            res = verify_backup(args.verify)
        else:
            data_dir = args.data_dir or os.environ.get("AI_HOUSE_DATA_DIR") or "./runtime"
            res = create_backup(
                data_dir,
                out_dir=args.out_dir,
                keep_count=args.keep_count,
                keep_days=args.keep_days,
            )
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[ai-house] 备份失败：{exc}", file=sys.stderr, flush=True)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.verify:
        print(f"✅ 备份校验通过：{res['archive']}（{res['verified_files']} 个文件，{len(res['dbs'])} 个 SQLite 库）")
    else:
        print(f"✅ 在线备份完成：{res['archive']}（{res['files']} 个文件，大小 {res['size']} 字节）")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    _load_dotenv(args.env_file)
    data_dir = args.data_dir or os.environ.get("AI_HOUSE_DATA_DIR") or "./runtime"
    try:
        res = restore_backup(args.archive, data_dir, overwrite=bool(args.overwrite))
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[ai-house] 恢复失败：{exc}", file=sys.stderr, flush=True)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"✅ 备份恢复完成：{res['target_dir']}（已恢复 {res['restored_files']} 个文件，{len(res['dbs'])} 个数据库）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai-house", description="Turnkey AI roommate sharehouse engine & studio")
    sub = ap.add_subparsers(dest="cmd")

    p_up = sub.add_parser("up", help="启动 AI 小屋守护进程、单页可视化工作台与 MCP 服务")
    p_up.add_argument("--host", default=None, help="监听主机地址（默认 127.0.0.1；公网/非回环绑定需先配置 AI_HOUSE_OWNER_TOKEN）")
    p_up.add_argument("--port", type=int, default=None, help="监听 HTTP 端口（默认 8780）")
    p_up.add_argument("--data-dir", default=None, help="运行时 SQLite 与状态持久化目录（默认 ./runtime）")
    p_up.add_argument("--pack", default=None, help="世界与室友内容包名称或目录（如 life1、indie-studio-3）")
    p_up.add_argument("--packs-root", default="./packs", help="自定义 WorldPack 扫描根目录（默认 ./packs）")
    p_up.add_argument("--env-file", default=".env", help="启动前加载的 .env 配置文件路径（默认 .env）")
    p_up.add_argument("--step-seconds", type=float, default=60.0, help="后台时钟推进步长（秒，默认 60.0）")
    p_up.add_argument("--virtual-clock", action="store_true", help="使用可倍速/重启独立运行的虚拟时钟")
    p_up.set_defaults(func=cmd_up)

    p_in = sub.add_parser("init", help="生成带强随机令牌的 .env 配置与 runtime 目录")
    p_in.add_argument("--env-file", default=".env", help="生成的 .env 配置文件路径（默认 .env）")
    p_in.add_argument("--data-dir", default="./runtime", help="预创建的运行时数据目录（默认 ./runtime）")
    p_in.add_argument("--pack", default="", help="预设 WorldPack 名称或目录（留空则首次打开网页由向导建角）")
    p_in.add_argument("--port", type=int, default=8780, help="写入 .env 的默认服务端口（默认 8780）")
    p_in.add_argument("--force", action="store_true", help="强制覆盖已存在的 .env 文件并重新生成令牌")
    p_in.set_defaults(func=cmd_init)

    p_tok = sub.add_parser("token", help="查看 .env 或环境变量中的房主访问令牌 (AI_HOUSE_OWNER_TOKEN)")
    p_tok.add_argument("--env-file", default=".env", help="要读取的 .env 配置文件路径（默认 .env）")
    p_tok.add_argument("--json", action="store_true", help="以 JSON 格式输出配置文件路径、来源与令牌")
    p_tok.set_defaults(func=cmd_token)

    p_doc = sub.add_parser("doctor", help="检查 Python、.env、安全令牌、SQLite、WorldPack 与模型连通性")
    p_doc.add_argument("--env-file", default=".env", help="待检测的 .env 配置文件路径（默认 .env）")
    p_doc.add_argument("--host", default=None, help="覆盖待校验证监听地址的 host")
    p_doc.add_argument("--data-dir", default=None, help="覆盖待检测的运行时数据目录")
    p_doc.add_argument("--pack", default=None, help="指定待校验的 WorldPack 名称或路径")
    p_doc.add_argument("--json", action="store_true", help="以 JSON 格式输出完整自检报告")
    p_doc.add_argument("--live-ping", action="store_true", help="向当前配置的 OpenAI 兼容接口发起真实连通性测试")
    p_doc.set_defaults(func=cmd_doctor)

    p_pl = sub.add_parser("plugins", help="列出已发现的四轴插件与世界内容包")
    p_pl.add_argument("--packs-root", default="./packs", help="自定义 WorldPack 扫描根目录（默认 ./packs）")
    p_pl.set_defaults(func=cmd_plugins)

    p_pk = sub.add_parser("pack-check", help="校验自定义世界与室友内容包 (WorldPack)")
    p_pk.add_argument("pack", help="WorldPack 目录路径或内置名称（如 life1、indie-studio-3）")
    p_pk.set_defaults(func=cmd_pack_check)

    p_bk = sub.add_parser("backup", help="对运行时 SQLite 与状态配置执行在线热备或 SHA-256 校验")
    p_bk.add_argument("--env-file", default=".env", help="启动配置 .env 路径（用于读取 AI_HOUSE_DATA_DIR）")
    p_bk.add_argument("--data-dir", default=None, help="运行时数据目录（默认读取 AI_HOUSE_DATA_DIR 或 ./runtime）")
    p_bk.add_argument("--out-dir", default=None, help="备份输出目录（默认为 <data-dir>/backups）")
    p_bk.add_argument("--keep-count", type=int, default=10, help="最多保留备份份数（默认 10）")
    p_bk.add_argument("--keep-days", type=int, default=14, help="最多保留备份天数（默认 14）")
    p_bk.add_argument("--verify", default=None, help="校验指定备份归档 (.tar.gz) 而不创建新备份")
    p_bk.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    p_bk.set_defaults(func=cmd_backup)

    p_rs = sub.add_parser("restore", help="在停止守护进程后，校验并原子恢复备份归档到运行目录")
    p_rs.add_argument("archive", help="待恢复的备份归档路径 (.tar.gz)")
    p_rs.add_argument("--env-file", default=".env", help="启动配置 .env 路径（用于读取 AI_HOUSE_DATA_DIR）")
    p_rs.add_argument("--data-dir", default=None, help="目标运行时数据目录（默认读取 AI_HOUSE_DATA_DIR 或 ./runtime）")
    p_rs.add_argument("--overwrite", action="store_true", help="覆盖目标运行目录中已有的数据库与状态文件")
    p_rs.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    p_rs.set_defaults(func=cmd_restore)

    ns = ap.parse_args(argv)
    if not getattr(ns, "cmd", None):
        return cmd_up(ap.parse_args(["up"]))
    return int(ns.func(ns))


if __name__ == "__main__":
    sys.exit(main())
