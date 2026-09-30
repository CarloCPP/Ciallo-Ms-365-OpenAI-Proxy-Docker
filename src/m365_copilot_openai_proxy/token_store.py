from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_write import write_text_atomic

SUBSTRATE_AUDIENCE_PREFIX = "https://substrate.office.com/"


_JWE_ENCRYPTION_SIZES = {
    # enc: (IV 字节数, tag 字节数, 密文块大小；GCM 无块对齐要求)
    "A128GCM": (12, 16, 1),
    "A192GCM": (12, 16, 1),
    "A256GCM": (12, 16, 1),
    "A128CBC-HS256": (16, 16, 16),
    "A192CBC-HS384": (16, 24, 16),
    "A256CBC-HS512": (16, 32, 16),
}


def _decode_jwe_segment(segment: str) -> bytes:
    if segment and not re.fullmatch(r"[A-Za-z0-9_-]+", segment):
        raise ValueError("Invalid JWE Base64url alphabet")
    decoded = base64.b64decode(
        segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True,
    )
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != segment:
        raise ValueError("Non-canonical JWE Base64url encoding")
    return decoded


def _unique_jwe_header(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    header: dict[str, Any] = {}
    for key, value in pairs:
        if key in header:
            raise ValueError("Duplicate JWE header member")
        header[key] = value
    return header


def _reject_jwe_json_constant(_value: str) -> None:
    raise ValueError("Non-JSON constant in JWE header")


def is_valid_substrate_jwe(token: str) -> bool:
    """检查支持的紧凑 JWE 结构及公开 aud 冲突，不解密或认证微软凭据。"""
    if not isinstance(token, str):
        return False
    parts = token.strip().split(".")
    if len(parts) != 5:
        return False
    try:
        protected, encrypted_key, iv, ciphertext, tag = map(_decode_jwe_segment, parts)
        header = json.loads(
            protected.decode("utf-8"),
            object_pairs_hook=_unique_jwe_header,
            parse_constant=_reject_jwe_json_constant,
        )
    except (ValueError, RecursionError):
        return False
    if not isinstance(header, dict):
        return False
    alg, enc = header.get("alg"), header.get("enc")
    if not isinstance(alg, str) or alg not in ("dir", "RSA-OAEP", "RSA-OAEP-256"):
        return False
    if not isinstance(enc, str) or enc not in _JWE_ENCRYPTION_SIZES:
        return False
    # dir 的密钥由带外提供，只有这种算法允许空 encrypted_key。
    if (alg == "dir") != (not encrypted_key):
        return False
    iv_size, tag_size, block_size = _JWE_ENCRYPTION_SIZES[enc]
    if len(iv) != iv_size or len(tag) != tag_size:
        return False
    if not ciphertext or len(ciphertext) % block_size:
        return False
    if "aud" in header:
        audiences = [header["aud"]] if isinstance(header["aud"], str) else header["aud"]
        if not isinstance(audiences, list) or not audiences:
            return False
        if not all(
            isinstance(audience, str) and is_substrate_token_claims({"aud": audience})
            for audience in audiences
        ):
            return False
    # 缺失公开 aud 不等于资源不匹配；真实受众与主体仍由上游鉴权。
    return True


# Token storage paths — initialized lazily via init_token_dir() or from TOKEN_DIR env var
_TOKEN_DIR: Path | None = None
_TOKEN_FILE: Path | None = None
_ENV_PATH = Path(".env")


def _get_token_dir() -> Path:
    global _TOKEN_DIR
    if _TOKEN_DIR is None:
        _TOKEN_DIR = Path(os.environ.get("TOKEN_DIR", "/home/app/token"))
    return _TOKEN_DIR


def _get_token_file() -> Path:
    global _TOKEN_FILE
    if _TOKEN_FILE is None:
        _TOKEN_FILE = _get_token_dir() / "token"
    return _TOKEN_FILE


def init_token_dir(token_dir: str) -> None:
    """Initialize token directory from Settings (called once at app startup)."""
    global _TOKEN_DIR, _TOKEN_FILE
    _TOKEN_DIR = Path(token_dir)
    _TOKEN_FILE = _TOKEN_DIR / "token"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        raise ValueError("not a JWT (expected header.payload.signature)")
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def is_substrate_token_claims(claims: dict[str, Any]) -> bool:
    return str(claims.get("aud", "")).startswith(SUBSTRATE_AUDIENCE_PREFIX)


def _write_profile_file(path: Path, text: str, *, mode: int | None = None) -> None:
    """Persist one profile field atomically.

    These are single-value files (token, username, tone, prompts), and a reader
    treats an empty one as "not set": a torn write of the token file reads back as
    "no token" and the account stops serving until someone re-pushes one.
    """
    write_text_atomic(path, text, mode=mode)


class AccessTokenStore:
    def __init__(self, token: str, env_path: Path | str = ".env"):
        self._token = token
        self._env_path = Path(env_path)
        self._mtime_ns = self._read_mtime()
        self._lock = threading.RLock()

    def get(self) -> str:
        with self._lock:
            self._reload_if_changed()
            return self._token

    def status(self) -> dict[str, Any]:
        token = self.get()
        now = time.time()
        # An unset global token is the normal state of a multi-account deployment:
        # every request resolves its token from the account behind the API key, so
        # nothing populates this one. Say so plainly instead of reporting a decode
        # failure for a token that was never meant to exist, matching
        # Account.token_status().
        if not token:
            return {"valid": False, "error": "No token", "expires_at": None, "seconds_remaining": 0}
        try:
            claims = decode_jwt_payload(token)
            if not is_substrate_token_claims(claims):
                return {
                    "valid": False,
                    "error": "Access token is not a substrate.office.com token.",
                    "expires_at": None,
                    "seconds_remaining": 0,
                }
            expires_at = int(claims["exp"])
        except Exception as exc:
            return {
                "valid": False,
                "error": f"Cannot decode access token: {exc}",
                "expires_at": None,
                "seconds_remaining": 0,
            }

        seconds_remaining = max(0, expires_at - int(now))
        return {
            "valid": seconds_remaining > 0,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "seconds_remaining": seconds_remaining,
        }

    def _reload_if_changed(self) -> None:
        mtime_ns = self._read_mtime()
        if mtime_ns is None or mtime_ns == self._mtime_ns:
            return
        token = read_token()
        if token:
            self._token = token
            self._mtime_ns = mtime_ns

    def _read_mtime(self) -> int | None:
        # Prefer isolated token file mtime
        try:
            return _TOKEN_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            pass
        # Fallback: .env file mtime
        try:
            return self._env_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None


def read_token() -> str | None:
    """Read token from isolated token file first, then fall back to .env."""
    # Try isolated token file (TOKEN_DIR volume)
    try:
        token = _get_token_file().read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    # Fallback: read from .env for backward compatibility
    return _read_env_token(_ENV_PATH)


def write_token(token: str) -> None:
    """Write token to isolated token file on TOKEN_DIR volume."""
    _write_profile_file(_get_token_file(), token, mode=0o600)


def write_username(username: str) -> None:
    """Write username to the token directory for persistence across restarts."""
    # Skip single-character values (avatar initials like "G")
    if len(username.strip()) <= 1:
        return
    _write_profile_file(_get_token_dir() / "username", username)


def read_username() -> str:
    """Read persisted username from the token directory."""
    try:
        name = (_get_token_dir() / "username").read_text(encoding="utf-8").strip()
        # Ignore single-character values (avatar initials like "G")
        return name if len(name) > 1 else ""
    except FileNotFoundError:
        return ""


def write_tone(tone: str) -> None:
    """Persist the selected conversation tone (mode) across restarts."""
    tone = (tone or "").strip()
    if not tone:
        return
    _write_profile_file(_get_token_dir() / "tone", tone)


def read_tone() -> str:
    """Read persisted conversation tone from the token directory."""
    try:
        return (_get_token_dir() / "tone").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_tool_prompt(prompt: str) -> None:
    """Persist a user-defined extra tool-call instruction across restarts."""
    _write_profile_file(_get_token_dir() / "tool_prompt", prompt or "")


def read_tool_prompt() -> str:
    """Read the persisted user-defined extra tool-call instruction."""
    try:
        return (_get_token_dir() / "tool_prompt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_system_prompt(prompt: str) -> None:
    """Persist a user-defined system-level tool-call instruction override across restarts."""
    _write_profile_file(_get_token_dir() / "system_prompt", prompt or "")


def read_system_prompt() -> str:
    """Read the persisted system-level tool-call instruction override (empty = use default)."""
    try:
        return (_get_token_dir() / "system_prompt").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _read_env_token(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if sep and key.strip() == "M365_ACCESS_TOKEN":
            return _clean_env_value(value)
    return None


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
