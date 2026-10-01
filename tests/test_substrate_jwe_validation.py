from __future__ import annotations

import base64
import dataclasses
import json
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from jwe_helpers import b64url, make_jwe, replace_segment
from m365_copilot_openai_proxy.account_store import Account
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotClient, SubstrateCopilotError
from m365_copilot_openai_proxy.token_store import is_valid_substrate_jwe

SUBSTRATE = "https://substrate.office.com/"


def _with_header(header) -> str:
    return replace_segment(make_jwe(), 0, b64url(json.dumps(header).encode()))


def _jwt(exp=None) -> str:
    payload = {
        "aud": SUBSTRATE,
        "oid": "11111111-1111-4111-8111-111111111111",
        "tid": "22222222-2222-4222-8222-222222222222",
        "exp": time.time() + 3600 if exp is None else exp,
    }
    return b64url(b'{"alg":"HS256"}') + "." + b64url(json.dumps(payload).encode()) + ".fixture"


def _invalid_cases():
    valid = make_jwe()
    rsa = make_jwe({"alg": "RSA-OAEP"})
    cases = [
        ("rsa-key-undecodable", replace_segment(rsa, 1, "a")),
        ("rsa-key-empty", replace_segment(rsa, 1, "")),
        ("dir-key-nonempty", replace_segment(valid, 1, "YQ")),
        ("four-parts", ".".join(valid.split(".")[:-1])),
        ("six-parts", valid + ".extra"),
        ("empty-ciphertext", replace_segment(valid, 3, "")),
        ("header-null", _with_header(None)),
        ("header-array", _with_header([])),
        ("missing-alg", _with_header({"enc": "A256GCM"})),
        ("missing-enc", _with_header({"alg": "dir"})),
        ("unknown-alg", replace_segment(rsa, 0, b64url(b'{"alg":"unsupported","enc":"A256GCM"}'))),
        ("unknown-enc", _with_header({"alg": "dir", "enc": "unsupported"})),
    ]
    for field in ("alg", "enc"):
        for label, value in [("blank", " "), ("bool", True), ("list", []), ("dict", {}), ("null", None)]:
            header = {"alg": "dir", "enc": "A256GCM", field: value}
            cases.append((f"{field}-{label}", _with_header(header)))
    for index in range(5):
        source = rsa if index == 1 else valid
        for label, value in [("bad-char", "YW!"), ("whitespace", "Y Q"), ("padding", "YQ=="), ("unicode", "测试"), ("undecodable", "a")]:
            cases.append((f"segment-{index}-{label}", replace_segment(source, index, value)))
    # 16 字节 tag 的最后一个 Base64url 字符只有高两位有意义；改低位不能静默放行。
    tag = b64url(b"T" * 16)
    cases.append(("noncanonical-tag", replace_segment(valid, 4, tag[:-1] + "B")))
    for index, lengths in [(2, [0, 1, 11, 13]), (4, [0, 1, 15, 17])]:
        for length in lengths:
            cases.append((f"gcm-segment-{index}-length-{length}", replace_segment(valid, index, b64url(b"X" * length))))
    for enc, tag_length in [("A128CBC-HS256", 16), ("A192CBC-HS384", 24), ("A256CBC-HS512", 32)]:
        cbc = make_jwe({"enc": enc})
        for index, length in [(2, 15), (2, 17), (3, 15), (3, 17), (4, tag_length - 1), (4, tag_length + 1)]:
            cases.append((f"{enc}-segment-{index}-length-{length}", replace_segment(cbc, index, b64url(b"X" * length))))
    for field in ("alg", "enc", "aud"):
        header = '{"alg":"dir","enc":"A256GCM","' + field + '":' + json.dumps({"alg": "dir", "enc": "A256GCM", "aud": SUBSTRATE}[field]) + '}'
        if field == "aud":
            header = '{"alg":"dir","enc":"A256GCM","aud":"https://graph.microsoft.com/","aud":' + json.dumps(SUBSTRATE) + '}'
        cases.append((f"duplicate-{field}", replace_segment(valid, 0, b64url(header.encode()))))
    for label, aud in [
        ("graph", "https://graph.microsoft.com/"),
        ("designer", "https://designerappservice.officeapps.live.com/"),
        ("lookalike", "https://substrate.office.com.evil.invalid/"),
        ("userinfo", "https://substrate.office.com@evil.invalid/"),
        ("http", "http://substrate.office.com/"),
        ("empty", ""), ("null", None), ("number", 5), ("dict", {}),
        ("empty-list", []), ("bad-list-member", [SUBSTRATE, None]),
        ("mixed", [SUBSTRATE, "https://graph.microsoft.com/"]),
    ]:
        cases.append(("aud-" + label, make_jwe({"aud": aud})))
    # 五段凭据的密钥段即使恰好可解析为 JWT claims，也不能跳过 JWE 检查。
    cases.append(("claims-in-encrypted-key", replace_segment(make_jwe({"alg": "RSA-OAEP", "aud": "https://graph.microsoft.com/"}), 1, _jwt().split(".")[1])))
    return [pytest.param(token, id=name) for name, token in cases]


INVALID = _invalid_cases()
API_INVALID = [case for case in INVALID if case.id in {
    "rsa-key-undecodable", "gcm-segment-4-length-1", "aud-graph", "claims-in-encrypted-key",
}]


@pytest.mark.parametrize("token", INVALID)
def test_invalid_jwe_is_rejected(token):
    assert not is_valid_substrate_jwe(token)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_jwe_header_rejects_non_json_constants(constant):
    header = '{"alg":"dir","enc":"A256GCM","extra":' + constant + '}'
    token = replace_segment(make_jwe(), 0, b64url(header.encode()))
    assert not is_valid_substrate_jwe(token)


def test_jwe_json_recursion_limit_is_a_rejection(monkeypatch):
    from m365_copilot_openai_proxy import token_store

    token = make_jwe()
    def recursion_limit(*args, **kwargs):
        raise RecursionError("JSON nesting exceeds this interpreter's limit")

    monkeypatch.setattr(token_store.json, "loads", recursion_limit)
    assert not is_valid_substrate_jwe(token)


@pytest.mark.parametrize("alg", ["dir", "RSA-OAEP", "RSA-OAEP-256"])
@pytest.mark.parametrize("enc", ["A128GCM", "A192GCM", "A256GCM", "A128CBC-HS256", "A192CBC-HS384", "A256CBC-HS512"])
def test_supported_jwe_shapes_are_accepted(alg, enc):
    assert is_valid_substrate_jwe(make_jwe({"alg": alg, "enc": enc}))


@pytest.mark.parametrize("header", [{}, {"aud": SUBSTRATE}, {"aud": [SUBSTRATE, SUBSTRATE + "sydney/"]}])
def test_absent_or_compatible_public_audience_keeps_structural_compatibility(header):
    token = make_jwe(header)
    assert is_valid_substrate_jwe(token)
    assert Account(token=token, token_updated_at=time.time()).token_status()["valid"]
    assert SubstrateCopilotClient(token)._is_consumer


def test_gcm_fixture_is_locally_authenticated_not_just_random_segments():
    header, key, iv, ciphertext, tag = make_jwe().split(".")
    decode = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    assert key == ""
    assert AESGCM(b"K" * 32).decrypt(decode(iv), decode(ciphertext) + decode(tag), header.encode()) == b'{"fixture":true}'


@pytest.mark.parametrize("token", [None, 1, b"x", [], {}])
def test_non_string_tokens_are_rejected(token):
    assert not is_valid_substrate_jwe(token)


@pytest.mark.parametrize("token", INVALID)
def test_bad_jwe_is_not_reported_as_valid_or_used_by_client(token):
    assert not Account(token=token, token_updated_at=time.time()).token_status()["valid"]
    with pytest.raises(SubstrateCopilotError):
        SubstrateCopilotClient(token)


@pytest.mark.parametrize("token", API_INVALID)
@pytest.mark.parametrize("provider", ["m365", "consumer", "unbound"])
def test_bad_push_preserves_account_and_key_state(tmp_path, token, provider):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="fixture-admin"))
    store = app.state.account_store
    account = None
    if provider != "unbound":
        account = store.add(name="fixture", token=_jwt())
        if provider == "consumer":
            store.set_consumer_auth(
                account.id, [{"name": "fixture", "value": "cookie", "domain": ".copilot.com"}],
                "fixture-consumer-token", "MSA", consumer_account_id="home:fixture",
                consumer_refresh_token="fixture-rt-" + "r" * 40,
                consumer_refresh_token_client_id="14638111-3389-403d-b206-a6a71d9f8f16",
                consumer_refresh_token_scope="140e65af-45d1-4427-bf08-3e7295db6836/ChatAI.ReadWrite",
            )
    key = app.state.key_store.add(name="fixture", account_id=account.id if account else "")
    before_account = dataclasses.asdict(account) if account else None
    before_key = dataclasses.asdict(key)
    before_disk = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    response = TestClient(app).post("/user/account/token", headers={"Authorization": "Bearer " + key.key}, json={"token": token})
    assert response.status_code == 400
    assert dataclasses.asdict(app.state.key_store.get(key.id)) == before_key
    if account:
        assert dataclasses.asdict(store.get(account.id)) == before_account
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before_disk


def test_structural_control_can_be_pushed_without_claiming_upstream_authentication(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="fixture-admin"))
    key = app.state.key_store.add(name="fixture")
    token = make_jwe()
    response = TestClient(app).post("/user/account/token", headers={"Authorization": "Bearer " + key.key}, json={"token": token})
    assert response.status_code == 200
    account_id = app.state.key_store.get(key.id).account_id
    assert app.state.account_store.get(account_id).token == token


def _jwe_with_claims_in_key() -> str:
    claims = {
        "aud": SUBSTRATE, "exp": time.time() + 7200,
        "name": "Other Account", "email": "other@example.invalid",
        "tid": "fixture-tenant", "oid": "fixture-object",
    }
    return replace_segment(make_jwe({"alg": "RSA-OAEP"}), 1, b64url(json.dumps(claims).encode()))


@pytest.mark.parametrize("extractor_name", ["extract_identity", "_studio_subject"])
def test_jwe_key_cannot_supply_account_identity_or_studio_subject(extractor_name):
    from m365_copilot_openai_proxy import account_store

    token = _jwe_with_claims_in_key()
    assert is_valid_substrate_jwe(token)
    assert getattr(account_store, extractor_name)(token) == ("", "")


@pytest.mark.parametrize("token", [None, 1, b"token", {}])
@pytest.mark.parametrize("extractor_name", ["extract_identity", "_studio_subject"])
def test_best_effort_identity_still_handles_non_string_values(token, extractor_name):
    from m365_copilot_openai_proxy import account_store

    assert getattr(account_store, extractor_name)(token) == ("", "")


def test_jwe_key_cannot_displace_another_account_on_push(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="fixture-admin"))
    store = app.state.account_store
    other = store.add(name="Other Account", token=_jwt())
    other.email = "other@example.invalid"
    other_key = app.state.key_store.add(name="other", account_id=other.id)
    key = app.state.key_store.add(name="new")
    before_other = dataclasses.asdict(other)
    before_key = dataclasses.asdict(other_key)
    response = TestClient(app).post(
        "/user/account/token", headers={"Authorization": "Bearer " + key.key},
        json={"token": _jwe_with_claims_in_key()},
    )
    assert response.status_code == 200
    assert response.json()["displaced"] == 0
    assert dataclasses.asdict(store.get(other.id)) == before_other
    assert dataclasses.asdict(app.state.key_store.get(other_key.id)) == before_key
    new_account = store.get(app.state.key_store.get(key.id).account_id)
    assert new_account.id != other.id
    assert new_account.email == ""
    assert new_account.token_status()["seconds_remaining"] <= 3600
    assert SubstrateCopilotClient(new_account.token)._oid == "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("expiry", [0, 1])
def test_jwt_expiry_guard_is_preserved(expiry):
    with pytest.raises(SubstrateCopilotError, match="expired"):
        SubstrateCopilotClient(_jwt(expiry))


def test_normal_jwt_client_is_unchanged():
    assert not SubstrateCopilotClient(_jwt())._is_consumer
