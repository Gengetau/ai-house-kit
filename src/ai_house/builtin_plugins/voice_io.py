"""Stage 3.2 optional speech-to-text & text-to-speech plugin (`voice_io`, default disabled)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..chat_identity import quote_untrusted
from ..llm import LLMAPIError, _extract_http_error_detail, mask_secrets
from ..media_safety import (
    DEFAULT_MAX_MEDIA_BYTES,
    fetch_media_bytes,
    media_ext_for_mime,
    mime_for_path,
    parse_data_uri,
    resolve_runtime_media_path,
    to_data_uri,
)
from ..plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    ToolContext,
    ToolRegistration,
)

PLUGIN_ID = "voice_io"


def _audio_url(base_url: str, suffix: str) -> str:
    url = (base_url or "https://api.openai.com").strip().rstrip("/")
    clean_suf = "/" + suffix.lstrip("/")
    if url.endswith(clean_suf):
        return url
    return (url + clean_suf) if url.endswith("/v1") else (url + "/v1" + clean_suf)


def _wav_info(raw: bytes) -> Dict[str, Any]:
    if not raw.startswith(b"RIFF"):
        return {}
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            rate = int(wf.getframerate() or 0)
            frames = int(wf.getnframes() or 0)
            channels = int(wf.getnchannels() or 0)
            return {
                "channels": channels,
                "sample_rate": rate,
                "duration_s": round(frames / rate, 3) if rate > 0 else 0.0,
            }
    except (wave.Error, EOFError, ValueError):
        return {}


def _load_audio_bytes(
    source: str,
    *,
    daemon: Any = None,
    allow_loopback: bool = False,
) -> Tuple[bytes, str, str]:
    raw_src = str(source or "").strip()
    if not raw_src:
        raise ValueError("缺少音频来源")
    if raw_src.lower().startswith("data:"):
        mime, raw = parse_data_uri(raw_src, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
        ext = media_ext_for_mime(mime, default="wav")
        return raw, mime, f"audio.{ext}"
    if raw_src.lower().startswith(("http://", "https://")):
        ctype, raw = fetch_media_bytes(raw_src, max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout_s=12.0, allow_loopback=allow_loopback)
        mime = ctype if ctype.startswith("audio/") else "audio/wav"
        ext = media_ext_for_mime(mime, default="wav")
        return raw, mime, f"audio.{ext}"
    if raw_src.startswith(("media/", "tts/")):
        rt_dir = getattr(daemon, "runtime_dir", None)
        if not rt_dir:
            raise ValueError("运行时目录未初始化")
        fp = resolve_runtime_media_path(rt_dir, raw_src)
        if not fp.is_file():
            raise ValueError("未找到本地语音文件")
        raw = fp.read_bytes()
        if len(raw) > DEFAULT_MAX_MEDIA_BYTES:
            raise ValueError("音频体积超过上限")
        mime = mime_for_path(fp, "audio/wav")
        return raw, mime, fp.name
    raise ValueError("不支持的音频来源（仅支持 http(s)://、data:audio/... 或 media/...）")


def _encode_multipart(
    fields: Mapping[str, str],
    *,
    file_field: str,
    filename: str,
    mime: str,
    raw: bytes,
) -> Tuple[bytes, str]:
    boundary = f"----aihouse-{uuid.uuid4().hex}"
    chunks: List[bytes] = []
    for k, v in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(raw)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def transcribe_audio_source(
    source: str,
    *,
    language: str = "",
    cfg: Optional[Mapping[str, Any]] = None,
    daemon: Any = None,
) -> Dict[str, Any]:
    """Transcribe an audio source via OpenAI-compatible `/v1/audio/transcriptions`, degrading cleanly on fault."""
    conf = dict(cfg or {})
    api_key = str(conf.get("api_key") or "").strip()
    base_url = str(conf.get("base_url") or "").strip().rstrip("/")
    stt_model = str(conf.get("stt_model") or "whisper-1").strip() or "whisper-1"
    if not api_key:
        return {"ok": False, "degraded": True, "status": "unconfigured", "error": "未配置语音识别 API Key", "text": ""}
    if not base_url or not base_url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "degraded": True,
            "status": "misconfigured",
            "error": "语音接口 Base URL 必须以 http:// 或 https:// 开头",
            "text": "",
        }

    allow_loopback = bool(conf.get("allow_loopback_media", False))
    try:
        raw, mime, filename = _load_audio_bytes(source, daemon=daemon, allow_loopback=allow_loopback)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "degraded": True,
            "status": "invalid_source",
            "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            "text": "",
        }

    fields: Dict[str, str] = {"model": stt_model}
    if str(language or "").strip():
        fields["language"] = str(language).strip()[:16]
    body_bytes, content_type = _encode_multipart(
        fields, file_field="file", filename=filename, mime=mime, raw=raw
    )
    url = _audio_url(base_url, "/audio/transcriptions")
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw_resp = resp.read().decode("utf-8", errors="replace")
        try:
            doc = json.loads(raw_resp)
            text = str(doc.get("text") or "").strip() if isinstance(doc, Mapping) else raw_resp.strip()
        except ValueError:
            text = raw_resp.strip()
        cleaned = quote_untrusted(text)[:500]
        if not cleaned:
            return {"ok": False, "degraded": True, "status": "empty_response", "error": "未识别出语音内容", "text": ""}
        res: Dict[str, Any] = {"ok": True, "degraded": False, "status": "ok", "model": stt_model, "text": cleaned}
        winfo = _wav_info(raw)
        if winfo:
            res["audio_info"] = winfo
        return res
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error_detail(exc, api_key=api_key)
        return {"ok": False, "degraded": True, "status": "upstream_error", "error": f"HTTP {exc.code}: {detail}", "text": ""}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "degraded": True,
            "status": "upstream_error",
            "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            "text": "",
        }


def synthesize_speech(
    text: str,
    *,
    scope: str = "group",
    actor: str = "",
    voice: str = "",
    cfg: Optional[Mapping[str, Any]] = None,
    daemon: Any = None,
) -> Dict[str, Any]:
    """Synthesize speech via OpenAI-compatible `/v1/audio/speech` after leak & guard checks."""
    clean_text = str(text or "").strip()[:400]
    if not clean_text:
        return {"ok": False, "degraded": False, "status": "invalid_input", "error": "待合成文本不能为空"}

    if daemon is not None:
        check_ch = f"private:{actor}" if scope == "private" else "group"
        guard = getattr(daemon, "guard", None)
        if guard is not None:
            why = guard.check(check_ch, clean_text)
            if why:
                return {"ok": False, "degraded": False, "status": "blocked", "error": f"guard:{why}"}
        mem = getattr(daemon, "memory", None)
        if scope != "private" and actor and mem is not None and actor in getattr(daemon.vocab, "ROSTER", ()):
            now = float(daemon.clock.now())
            if mem.check_outbound_leak(actor, "group", clean_text, now=now):
                return {"ok": False, "degraded": False, "status": "blocked", "error": "memory_leak:private_to_public"}

    conf = dict(cfg or {})
    api_key = str(conf.get("api_key") or "").strip()
    base_url = str(conf.get("base_url") or "").strip().rstrip("/")
    tts_model = str(conf.get("tts_model") or "tts-1").strip() or "tts-1"
    eff_voice = str(voice or conf.get("tts_voice") or "alloy").strip() or "alloy"
    fmt = str(conf.get("response_format") or "wav").strip().lower()
    if fmt not in ("wav", "mp3", "opus"):
        fmt = "wav"
    if not api_key:
        return {"ok": False, "degraded": True, "status": "unconfigured", "error": "未配置语音合成 API Key"}
    if not base_url or not base_url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "degraded": True,
            "status": "misconfigured",
            "error": "语音接口 Base URL 必须以 http:// 或 https:// 开头",
        }

    url = _audio_url(base_url, "/audio/speech")
    payload = {
        "model": tts_model,
        "input": clean_text,
        "voice": eff_voice,
        "response_format": fmt,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20.0) as resp:
            raw_audio = resp.read(DEFAULT_MAX_MEDIA_BYTES + 1)
            if len(raw_audio) > DEFAULT_MAX_MEDIA_BYTES:
                raise ValueError("合成音频体积超过上限")
            if not raw_audio:
                raise LLMAPIError(502, "empty audio response")
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error_detail(exc, api_key=api_key)
        return {"ok": False, "degraded": True, "status": "upstream_error", "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "degraded": True,
            "status": "upstream_error",
            "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
        }

    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/opus"}[fmt]
    media_rel = ""
    rt_dir = getattr(daemon, "runtime_dir", None) if daemon is not None else None
    if rt_dir:
        digest = hashlib.sha256(raw_audio).hexdigest()[:16]
        out_dir = Path(rt_dir) / "media" / "tts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{digest}.{fmt}"
        out_path.write_bytes(raw_audio)
        media_rel = f"media/tts/{out_path.name}"

    out: Dict[str, Any] = {
        "ok": True,
        "degraded": False,
        "status": "ok",
        "model": tts_model,
        "voice": eff_voice,
        "format": fmt,
        "mime": mime,
        "bytes": len(raw_audio),
        "media_rel": media_rel,
        "audio_base64": base64.b64encode(raw_audio).decode("ascii"),
        "data_uri": to_data_uri(raw_audio, mime),
    }
    if fmt == "wav":
        winfo = _wav_info(raw_audio)
        if winfo:
            out["wav_info"] = winfo
    return out


def enrich_chat_records(
    records: Sequence[str],
    *,
    daemon: Any,
    cfg: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """Transcribe up to 2 inbound voice refs cleanly; faults degrade to empty annotations."""
    notes: List[str] = []
    for raw_src in list(records or ())[:2]:
        res = transcribe_audio_source(str(raw_src), daemon=daemon, cfg=cfg)
        if res.get("ok") and res.get("text"):
            notes.append(f"[语音转写：{res['text']}]")
    return notes


def _tool_transcribe_audio(
    *,
    source: str = "",
    language: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    daemon = ctx.daemon if ctx is not None else None
    return transcribe_audio_source(source, language=language, cfg=cfg, daemon=daemon)


def _tool_synthesize_voice(
    *,
    text: str = "",
    voice: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    daemon = ctx.daemon if ctx is not None else None
    scope = ctx.scope if ctx is not None else "group"
    actor = ctx.actor if ctx is not None else ""
    res = synthesize_speech(text, scope=scope, actor=actor, voice=voice, cfg=cfg, daemon=daemon)
    # Keep tool output compact for LLM tool loop while preserving media_rel
    if res.get("ok"):
        return {
            "ok": True,
            "media_rel": res.get("media_rel"),
            "format": res.get("format"),
            "bytes": res.get("bytes"),
        }
    return res


def _route_transcribe(rctx: PluginRequestContext) -> Dict[str, Any]:
    body = rctx.body if isinstance(rctx.body, Mapping) else {}
    source = str(body.get("source") or body.get("url") or body.get("data_uri") or "").strip()
    language = str(body.get("language") or "").strip()
    return transcribe_audio_source(source, language=language, cfg=rctx.plugin_config, daemon=rctx.daemon)


def _route_speak(rctx: PluginRequestContext) -> Dict[str, Any]:
    body = rctx.body if isinstance(rctx.body, Mapping) else {}
    text = str(body.get("text") or "").strip()
    voice = str(body.get("voice") or "").strip()
    scope = str(body.get("scope") or "private").strip().lower()
    actor = str(body.get("actor") or "").strip()
    return synthesize_speech(text, scope=scope, actor=actor, voice=voice, cfg=rctx.plugin_config, daemon=rctx.daemon)


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id=PLUGIN_ID,
        name="语音（转文字与语音合成）",
        kind="bundle",
        version="0.3.0",
        description="可选接入 OpenAI 兼容 STT/TTS 服务（/v1/audio/transcriptions 与 /v1/audio/speech）；未配置或异常时静默降级。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="base_url",
                label="语音接口 Base URL",
                field_type="string",
                default="https://api.openai.com",
                env_var="AI_HOUSE_VOICE_BASE_URL",
                placeholder="https://api.openai.com",
                description="OpenAI 兼容音频接口基址。",
            ),
            ConfigField(
                key="api_key",
                label="语音服务 API Key",
                field_type="string",
                default="",
                secret=True,
                env_var="AI_HOUSE_VOICE_API_KEY",
                description="STT/TTS 服务访问密钥。",
            ),
            ConfigField(
                key="stt_model",
                label="语音转文字模型",
                field_type="string",
                default="whisper-1",
                env_var="AI_HOUSE_VOICE_STT_MODEL",
                description="如 whisper-1 或 gpt-4o-mini-transcribe。",
            ),
            ConfigField(
                key="tts_model",
                label="语音合成模型",
                field_type="string",
                default="tts-1",
                env_var="AI_HOUSE_VOICE_TTS_MODEL",
                description="如 tts-1 或 gpt-4o-mini-tts。",
            ),
            ConfigField(
                key="tts_voice",
                label="默认音色",
                field_type="string",
                default="alloy",
                env_var="AI_HOUSE_VOICE_TTS_VOICE",
                description="语音合成默认音色名。",
            ),
            ConfigField(
                key="response_format",
                label="合成音频格式",
                field_type="select",
                default="wav",
                options=(("wav", "WAV（免外部解码）"), ("mp3", "MP3"), ("opus", "Opus")),
                env_var="AI_HOUSE_VOICE_FORMAT",
                description="TTS 响应音频编码格式。",
            ),
            ConfigField(
                key="allow_loopback_media",
                label="允许读取本机回环音频地址",
                field_type="bool",
                default=False,
                env_var="AI_HOUSE_VOICE_ALLOW_LOOPBACK",
                description="本地 OneBot / 测试服务提供 127.0.0.1 语音 URL 时开启。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "transcribe_audio",
                "description": "将音频 URL、data URI 或本地 media/ 语音转为文字。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "音频来源"},
                        "language": {"type": "string", "description": "可选语言代码如 zh"},
                    },
                    "required": ["source"],
                },
            },
            handler=_tool_transcribe_audio,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
        ToolRegistration(
            spec={
                "name": "synthesize_voice",
                "description": "将文字合成为语音音频并保存到本地媒体目录（发前经过隐私与关键词检查）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要朗读的中文或外文内容"},
                        "voice": {"type": "string", "description": "可选音色名"},
                    },
                    "required": ["text"],
                },
            },
            handler=_tool_synthesize_voice,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
    ),
    routes=(
        PluginRoute(method="POST", subpath="/transcribe", handler=_route_transcribe, tier="write"),
        PluginRoute(method="POST", subpath="/speak", handler=_route_speak, tier="write"),
    ),
)
