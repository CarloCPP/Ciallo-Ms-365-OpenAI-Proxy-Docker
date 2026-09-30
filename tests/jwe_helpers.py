from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def replace_segment(token: str, index: int, value: str) -> str:
    parts = token.split(".")
    parts[index] = value
    return ".".join(parts)


def make_jwe(header: dict | None = None) -> str:
    """仅供本地测试；GCM 正文真实加密，RSA 密钥段和 CBC 只构造合法尺寸。"""
    protected = {"alg": "dir", "enc": "A256GCM"}
    if header is not None:
        protected.update(header)
    encoded_header = b64url(json.dumps(protected, separators=(",", ":")).encode())
    enc = protected["enc"]
    if enc in ("A128GCM", "A192GCM", "A256GCM"):
        key = b"K" * (int(enc[1:4]) // 8)
        iv = b"I" * 12
        encrypted = AESGCM(key).encrypt(iv, b'{"fixture":true}', encoded_header.encode())
        ciphertext, tag = encrypted[:-16], encrypted[-16:]
    else:
        iv = b"I" * 16
        ciphertext = b"C" * 32
        tag = b"T" * {"A128CBC-HS256": 16, "A192CBC-HS384": 24, "A256CBC-HS512": 32}[enc]
    encrypted_key = b"" if protected["alg"] == "dir" else b"K" * 256
    return ".".join((encoded_header, b64url(encrypted_key), b64url(iv), b64url(ciphertext), b64url(tag)))
