"""Studio 离线诊断辅助逻辑；不读取凭据、不创建客户端、不发送请求。"""
from __future__ import annotations

from .substrate_client import _EMPTY_TURN_MARKER, _REFUSED_TURN_MARKER
from .tone_options import TONE_OPTIONS
from .tone_resolver import resolve_tone


_CATEGORIES = frozenset({
    "refused", "empty", "timeout", "invalid_request", "internal_error",
    "auth", "disengaged", "other",
})


def classify_error(detail: object) -> str:
    text = str(detail or "").lower()
    if _REFUSED_TURN_MARKER in text:
        return "refused"
    if _EMPTY_TURN_MARKER in text or "empty response" in text:
        return "empty"
    if "timeout" in text or "timed out" in text or "stopped sending data" in text:
        return "timeout"
    if "access token" in text or "unauthorized" in text or "forbidden" in text:
        return "auth"
    if "disengaged" in text:
        return "disengaged"
    if "invalid" in text or "bad request" in text or "not supported" in text:
        return "invalid_request"
    if "internalerror" in text or "internal error" in text:
        return "internal_error"
    return "other"


def public_result(
    *,
    category: str | None,
    chunks: int,
    chars: int,
    agent_id: object = None,
    error_detail: object = None,
) -> dict[str, object]:
    del agent_id, error_detail
    safe_category = category if category in _CATEGORIES else "other"
    return {
        "ok": category is None,
        "category": None if category is None else safe_category,
        "chunks": max(0, int(chunks)),
        "chars": max(0, int(chars)),
    }


def effective_runtime_config(key: object, runtime: dict, *, model: str) -> dict:
    # 与 HTTP 路径共用模型别名、持续会话后缀和内置 tone 回退规则。
    tone, _ = resolve_tone(
        model, runtime.get("tone_options") or TONE_OPTIONS,
        str(runtime.get("current_tone") or "Magic"),
    )
    global_prompt = str(runtime.get("global_tool_prompt") or "").strip()
    key_prompt = str(getattr(key, "tool_prompt", "") or "").strip()
    tool_prompt = "\n\n".join(part for part in (global_prompt, key_prompt) if part) or ""
    key_system = str(getattr(key, "system_prompt", "") or "").strip()
    system_override = key_system or str(runtime.get("system_prompt") or "")
    time_zone = str(getattr(key, "time_zone", "") or runtime.get("time_zone") or "Asia/Shanghai")
    key_idle = int(getattr(key, "ws_idle_timeout_minutes", 0) or 0)
    global_idle = int(runtime.get("ws_idle_timeout_minutes", 0) or 0)
    idle_minutes = key_idle or global_idle
    return {
        "tone": tone,
        "tool_prompt": tool_prompt,
        "system_override": system_override,
        "time_zone": time_zone,
        "idle_timeout": idle_minutes * 60 if idle_minutes > 0 else None,
    }
