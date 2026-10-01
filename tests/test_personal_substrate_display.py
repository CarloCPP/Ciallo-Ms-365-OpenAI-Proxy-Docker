from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from jwe_helpers import b64url, make_jwe
from m365_copilot_openai_proxy.account_serializers import account_public, user_account_public
from m365_copilot_openai_proxy.account_store import Account, AccountStore
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings


def jwt(email="work@example.com", tid="enterprise"):
    claims = {"aud": "https://substrate.office.com/", "oid": "user", "tid": tid,
              "email": email, "exp": time.time() + 3600}
    return b64url(b'{"alg":"HS256"}') + "." + b64url(json.dumps(claims).encode()) + ".fixture"


@pytest.mark.parametrize("account,expected", [
    (Account(provider="consumer"), True),
    (Account(token=make_jwe()), True),
    (Account(token=jwt(tid="84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa")), True),
    (Account(token=jwt()), False),
    (Account(token="broken.a.b.c.d"), False),
])
def test_personal_badge_independent_of_transport(account, expected):
    for render in (account_public, user_account_public):
        public = render(account)
        assert public.get("is_personal") is expected
        assert public["provider"] == account.provider


def test_jwe_display_email_is_persisted_without_binding_or_deduplication(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="test-admin"))
    store = app.state.account_store
    other = store.add(token=jwt())
    other_key = app.state.key_store.add(account_id=other.id)
    mine = store.add(name="ZY")
    key = app.state.key_store.add(account_id=mine.id)
    client = TestClient(app)
    response = client.post("/user/account/token", headers={"Authorization": "Bearer " + key.key},
                           json={"token": make_jwe(), "display_email": "work@example.com"})
    assert response.status_code == 200
    assert app.state.key_store.get(key.id).account_id == mine.id
    assert app.state.key_store.get(other_key.id).account_id == other.id
    reloaded = AccountStore(tmp_path / "accounts.json").get(mine.id)
    assert account_public(reloaded)["email"] == "work@example.com"
    assert reloaded.email == ""
    assert store.find_by_email("work@example.com").id == other.id
    assert not reloaded.studio_agent_ready


@pytest.mark.parametrize("next_value", ["", "not-an-email", "a@example.com\nInjected", None, {"email": "a@example.com"}])
def test_new_push_never_retains_unusable_display_email(tmp_path, next_value):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="test-admin"))
    key = app.state.key_store.add()
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + key.key}
    assert client.post("/user/account/token", headers=headers, json={"token": make_jwe(), "display_email": "old@example.com"}).status_code == 200
    acc = app.state.account_store.get(app.state.key_store.get(key.id).account_id)
    assert account_public(acc)["email"] == "old@example.com"
    assert client.post("/user/account/token", headers=headers, json={"token": make_jwe(), "display_email": next_value}).status_code == 200
    assert account_public(acc)["email"] == ""


def test_jwt_identity_cannot_be_overridden_by_browser_display_field(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="test-admin"))
    key = app.state.key_store.add()
    response = TestClient(app).post("/user/account/token", headers={"Authorization": "Bearer " + key.key},
                                   json={"token": jwt(), "display_email": "other@example.com"})
    assert response.status_code == 200
    acc = app.state.account_store.get(app.state.key_store.get(key.id).account_id)
    assert account_public(acc)["email"] == "work@example.com"


@pytest.mark.parametrize("mutation", ["clear_token", "clear_credentials", "different_token"])
def test_display_email_does_not_survive_credential_replacement(tmp_path, mutation):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="test-admin"))
    key = app.state.key_store.add()
    response = TestClient(app).post("/user/account/token", headers={"Authorization": "Bearer " + key.key},
                                   json={"token": make_jwe(), "display_email": "old@example.com"})
    assert response.status_code == 200
    store = app.state.account_store
    acc = store.get(app.state.key_store.get(key.id).account_id)
    assert account_public(acc)["email"] == "old@example.com"
    if mutation == "different_token":
        store.update_token(acc.id, make_jwe({"kid": "different"}))
    else:
        getattr(store, mutation)(acc.id)
    assert getattr(acc, "substrate_display_email", "") == ""
