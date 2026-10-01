"""Probe-owned conversations are removed without touching other requests."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from m365_copilot_openai_proxy import m365_cloud_client as cloud
from m365_copilot_openai_proxy import routes_admin_modeltest as routes
from m365_copilot_openai_proxy import substrate_client
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.refresh_via_rt import M365_NATIVE_CLIENT_ID
from m365_copilot_openai_proxy.substrate_client import SIGNALR_SEP

TID = "11111111-1111-1111-1111-111111111111"
OID = "22222222-2222-2222-2222-222222222222"


def _jwt(oid=OID):
    payload = {"tid": TID, "oid": oid, "aud": "https://substrate.office.com/", "exp": time.time() + 3600}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{encoded}.sig"


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="admin-key"))
    account = app.state.account_store.add(name="Probe", token=_jwt())
    app.state.account_store.set_refresh_token(
        account.id, "probe-rt", client_id=M365_NATIVE_CLIENT_ID,
        authority=TID, tenant_id=TID, object_id=OID,
    )
    admin = TestClient(app)
    admin.post("/admin/login", json={"password": "admin-key"})
    state = SimpleNamespace(app=app, account=account, admin=admin, ids=[], deleted=[],
                            modes=["ok"], on_frame=None, on_token=None, delete_error=None)

    async def token_response(**kwargs):
        if state.on_token:
            state.on_token()
        return SimpleNamespace(status_code=200, json=lambda: {
            "access_token": _jwt(), "expires_in": 3600,
        })

    async def action(token, action, state=None, **extra):
        assert action == "DeleteConversation", "probe must not enumerate history"
        if state_env.delete_error:
            raise state_env.delete_error
        state_env.deleted.append(extra["conversationId"])
        return {"success": True}

    state_env = state
    monkeypatch.setattr(cloud, "_TOKEN_CACHE", {})
    monkeypatch.setattr(cloud, "_post_token", token_response)
    monkeypatch.setattr(cloud, "_cloud_action", action)

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, data):
            return None

        async def recv(self):
            return "{}" + SIGNALR_SEP

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.frames:
                frame = self.frames.pop(0)
                if state.on_frame:
                    state.on_frame()
                return json.dumps(frame) + SIGNALR_SEP
            if self.mode == "timeout":
                await asyncio.Event().wait()
            raise StopAsyncIteration

    def connect(url, **kwargs):
        cid = parse_qs(urlsplit(url).query)["ConversationId"][0]
        state.ids.append(cid)
        sock = Socket()
        sock.mode = state.modes[min(len(state.ids) - 1, len(state.modes) - 1)]
        first = {"conversationId": cid, "messages": [{"author": "user", "text": "probe"}]}
        if sock.mode == "foreign":
            first["conversationId"] = "existing-control"
        if sock.mode == "id_only":
            first.pop("messages")
        sock.frames = [{"type": 1, "target": "update", "arguments": [first]}]
        if sock.mode in ("ok", "foreign", "id_only"):
            sock.frames.append({"type": 1, "target": "update", "arguments": [{"writeAtCursor": "pong"}]})
        if sock.mode == "refused":
            sock.frames.append({"type": 2, "item": {"turnState": "Failed", "result": {"value": "InternalError"}}})
        if sock.mode != "timeout":
            sock.frames.append({"type": 3})
        return sock

    monkeypatch.setattr(substrate_client.websockets, "connect", connect)
    state.probe = lambda: admin.post("/admin/model-test", json={"account_id": account.id, "model": "Magic"}).json()
    return state


@pytest.mark.parametrize("mode,verdict", [("ok", "ok"), ("refused", "refused"), ("timeout", "error")])
def test_probe_deletes_confirmed_creation_even_after_failure(env, monkeypatch, mode, verdict):
    env.modes = [mode]
    if mode == "timeout":
        monkeypatch.setattr(routes, "_PROBE_TIMEOUT_SECONDS", 0.03)
    result = env.probe()
    assert result["verdict"] == verdict
    assert result["cleanup"]["status"] == "deleted"
    assert env.deleted == env.ids
    assert env.app.state.session_store.items() == []


def test_existing_empty_retry_cleans_both_owned_conversations(env):
    env.modes = ["empty", "ok"]
    result = env.probe()
    assert result["verdict"] == "ok"
    assert len(env.ids) == 2
    assert set(env.deleted) == set(env.ids)


@pytest.mark.parametrize("mode", ["foreign", "id_only"])
def test_captured_or_foreign_id_is_not_creation_confirmation(env, mode):
    env.modes = [mode]
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["cleanup"] == {"status": "skipped", "message": "creation_unconfirmed"}
    assert not env.deleted


@pytest.mark.parametrize("change", ["epoch", "subject", "delete", "profile"])
def test_probe_skips_cleanup_after_identity_or_profile_change(env, change):
    def mutate():
        env.on_frame = None
        if change == "epoch":
            env.account.protocol_epoch += 1
        elif change == "subject":
            env.account.token = _jwt("33333333-3333-3333-3333-333333333333")
        elif change == "delete":
            env.app.state.account_store.remove(env.account.id)
        else:
            env.app.state.protocol_profile_store.apply(
                {"variants": ["feature.New"], "options_sets": ["newoption"]},
                scope="account", scope_id=env.account.id,
            )
    env.on_frame = mutate
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["cleanup"] == {"status": "skipped", "message": "identity_changed"}
    assert not env.deleted


def test_identity_switch_during_auth_never_reaches_delete(env):
    env.on_token = lambda: setattr(env.account, "protocol_epoch", env.account.protocol_epoch + 1)
    result = env.probe()
    assert result["cleanup"]["status"] == "skipped"
    assert not env.deleted


def test_delete_failure_does_not_replace_probe_reply(env):
    env.delete_error = cloud.CloudSessionError("denied")
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["reply"] == "pong"
    assert result["cleanup"] == {"status": "failed", "message": "delete_failed"}


def test_missing_verified_deletion_auth_is_unsupported(env):
    env.app.state.account_store.set_refresh_token(env.account.id, "")
    result = env.probe()
    assert result["cleanup"] == {"status": "unsupported", "message": "delete_unsupported"}
    assert not env.deleted


def test_cleanup_deadline_preserves_probe_latency_and_result(env, monkeypatch):
    clock = [100.0]
    async def delayed(*args, **kwargs):
        clock[0] += 20.0
        raise asyncio.TimeoutError
    monkeypatch.setattr(routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(cloud, "_cloud_action", delayed)
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["reply"] == "pong"
    assert result["cleanup"] == {"status": "failed", "message": "delete_timeout"}
    assert result["latency_ms"] == 0


def test_cleanup_has_a_finite_deadline(env, monkeypatch):
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(cloud, "_cloud_action", blocked)
    monkeypatch.setattr(routes, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    assert env.probe()["cleanup"] == {"status": "failed", "message": "delete_timeout"}


def test_connection_failure_does_not_claim_a_conversation_was_created(env, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("connection failed")
    monkeypatch.setattr(substrate_client.websockets, "connect", fail)
    result = env.probe()
    assert result["verdict"] == "error"
    assert result["cleanup"]["status"] == "not_created"
    assert not env.deleted


def test_probe_cancellation_waits_for_cleanup_and_remains_cancelled(env):
    env.modes = ["timeout"]
    async def run():
        endpoint = next(route.endpoint for route in env.app.routes if getattr(route, "path", "") == "/admin/model-test")
        async def body():
            return {"account_id": env.account.id, "model": "Magic"}
        request = SimpleNamespace(json=body, cookies=dict(env.admin.cookies))
        task = asyncio.create_task(endpoint(request))
        ready = asyncio.Event()
        env.on_frame = ready.set
        await asyncio.wait_for(ready.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert env.deleted == env.ids
    asyncio.run(run())


def test_unexpected_client_exception_still_cleans_created_conversation(env, monkeypatch):
    original = substrate_client.SubstrateCopilotClient.chat
    async def broken(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("unexpected client failure")
    monkeypatch.setattr(substrate_client.SubstrateCopilotClient, "chat", broken)
    result = env.probe()
    assert result["verdict"] == "error"
    assert result["error"] == "unexpected client failure"
    assert result["cleanup"]["status"] == "deleted"
    assert env.deleted == env.ids


def test_one_failed_delete_does_not_abandon_other_confirmed_retries(env, monkeypatch):
    env.modes = ["empty", "ok"]
    attempts = []
    async def action(token, action, **kwargs):
        attempts.append(kwargs["conversationId"])
        if len(attempts) == 1:
            raise cloud.CloudSessionError("first deletion failed")
        return {"store": {}, "__queryState": {}}
    monkeypatch.setattr(cloud, "_cloud_action", action)
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["cleanup"]["status"] == "failed"
    assert set(attempts) == set(env.ids)


def test_probe_uses_original_refresh_credential_not_a_replacement(env, monkeypatch):
    received = []
    async def token_response(**kwargs):
        received.append(kwargs["refresh_token"])
        return SimpleNamespace(status_code=200, json=lambda: {
            "access_token": _jwt(), "expires_in": 3600, "refresh_token": "rotated-old-rt",
        })
    monkeypatch.setattr(cloud, "_post_token", token_response)
    env.on_frame = lambda: setattr(env.account, "refresh_token", "replacement-rt")
    result = env.probe()
    assert result["cleanup"]["status"] == "deleted"
    assert received == ["probe-rt"]
    assert env.account.refresh_token == "replacement-rt"


def test_personal_jwe_never_borrows_enterprise_refresh_grant(env):
    from m365_copilot_openai_proxy.probe_conversations import ProbeConversations
    # Even a JWE whose second segment happens to parse as enterprise claims is
    # opaque; its embedded bytes must never authorize a cloud delete.
    env.account.token = _jwt() + ".ciphertext.tag"
    account = env.app.state.account_store.get_request_snapshot(env.account.id)
    probe = ProbeConversations(attempted=True, candidates={"owned"}, confirmed={"owned"})
    result = asyncio.run(routes._cleanup_probe(env.app, account, routes._probe_profile(env.app, account), probe))
    assert result == {"status": "unsupported", "message": "delete_unsupported"}
    assert not env.deleted


def test_consumer_create_is_retained_on_chat_failure_but_delete_is_unsupported(env):
    from m365_copilot_openai_proxy.consumer_client import ConsumerCopilotClient, ConsumerCopilotError
    from m365_copilot_openai_proxy.probe_conversations import ProbeConversations, probe_conversations
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url):
            return None
        async def post(self, url, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"id": "consumer-owned"})
        def ws_connect(self, *args, **kwargs):
            raise ConsumerCopilotError("failed after create")
    async def run():
        probe = ProbeConversations()
        scope = probe_conversations.set(probe)
        try:
            client = ConsumerCopilotClient(access_token="consumer", session_factory=lambda **kwargs: Session())
            with pytest.raises(ConsumerCopilotError, match="failed after create"):
                await client.chat("probe")
        finally:
            probe_conversations.reset(scope)
        env.account.provider = "consumer"
        account = env.app.state.account_store.get_request_snapshot(env.account.id)
        assert probe.confirmed == {"consumer-owned"}
        assert await routes._cleanup_probe(env.app, account, None, probe) == {
            "status": "unsupported", "message": "delete_unsupported",
        }
        assert probe_conversations.get() is None
        assert not env.deleted
    asyncio.run(run())


def test_concurrent_probes_never_share_creation_evidence(env):
    async def run():
        endpoint = next(route.endpoint for route in env.app.routes if getattr(route, "path", "") == "/admin/model-test")
        async def body():
            return {"account_id": env.account.id, "model": "Magic"}
        request = SimpleNamespace(json=body, cookies=dict(env.admin.cookies))
        results = await asyncio.gather(endpoint(request), endpoint(request))
        assert [result["cleanup"]["status"] for result in results] == ["deleted", "deleted"]
        assert len(env.ids) == len(set(env.ids)) == 2
        assert sorted(env.deleted) == sorted(env.ids)
        assert env.app.state.session_store.items() == []
    asyncio.run(run())


def test_http_success_with_business_denial_is_not_deleted(env, monkeypatch):
    async def denied(*args, **kwargs):
        return {"success": False, "error": "access denied"}
    monkeypatch.setattr(cloud, "_cloud_action", denied)
    result = env.probe()
    assert result["verdict"] == "ok"
    assert result["cleanup"] == {"status": "failed", "message": "delete_failed"}
