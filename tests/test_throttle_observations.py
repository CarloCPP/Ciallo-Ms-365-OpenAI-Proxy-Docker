from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.account_concurrency import AccountConcurrency
from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.consumer_client import AccountThrottled
from m365_copilot_openai_proxy.dependencies import _throttled


_RESET_AT = "2030-01-01T00:00:00Z"
_RESET_EPOCH = datetime.fromisoformat(_RESET_AT.replace("Z", "+00:00")).timestamp()


class _Client:
    mode = "reasoning"

    def __init__(self):
        self.error = None
        self.partial = False
        self.empty = False
        self.started = None
        self.release = None

    async def chat_stream(self, prompt):
        error = self.error
        if self.started is not None and prompt == "slow":
            self.started.set()
            await self.release.wait()
        if self.partial:
            yield "partial"
        if error is not None:
            raise error
        if not self.empty:
            yield "accepted"

    async def chat(self, prompt):
        return "".join([part async for part in self.chat_stream(prompt)])


@pytest.fixture
def state(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("m365_copilot_openai_proxy.account_store.time.time", lambda: clock[0])
    store = AccountStore(tmp_path / "accounts.json")
    account = store.add(name="test")
    store.set_consumer_auth(account.id, [], "opaque-fixture", consumer_account_id="home:first")
    target = _Client()
    app = SimpleNamespace(state=SimpleNamespace(
        account_store=store,
        account_concurrency_gate=AccountConcurrency(),
        account_concurrency=2,
    ))
    return store, account, target, _throttled(app, account, target), clock


def _refuse(target):
    target.error = AccountThrottled("throttled", next_available_at=_RESET_AT)


async def _complete(client, streaming):
    if streaming:
        async for _ in client.chat_stream("hello"):
            pass
    else:
        await client.chat("hello")


def test_throttle_observation_keeps_request_mode_across_reload(state, tmp_path):
    store, account, target, client, _clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))

    reloaded = AccountStore(tmp_path / "accounts.json").get(account.id)
    assert reloaded.throttled_until == _RESET_EPOCH
    assert getattr(reloaded, "throttled_mode", "") == "reasoning"
    assert getattr(reloaded, "throttled_at", 0.0) == 1000.0


@pytest.mark.parametrize("streaming", [False, True])
def test_later_completed_turn_clears_same_mode_observation(state, tmp_path, streaming):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))

    target.error = None
    clock[0] += 1
    asyncio.run(_complete(client, streaming))
    reloaded = AccountStore(tmp_path / "accounts.json").get(account.id)
    assert reloaded.throttled_until == 0.0
    assert getattr(reloaded, "throttled_mode", "") == ""
    assert getattr(reloaded, "throttled_at", 0.0) == 0.0


def test_another_mode_success_does_not_erase_the_observed_refusal(state):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))

    target.error = None
    target.mode = "chat"
    clock[0] += 1
    asyncio.run(client.chat("hello"))
    assert store.get(account.id).throttled_until == _RESET_EPOCH
    assert getattr(store.get(account.id), "throttled_mode", "") == "reasoning"

    target.mode = "reasoning"
    asyncio.run(_complete(client, True))
    assert store.get(account.id).throttled_until == 0.0


@pytest.mark.parametrize("ending", ["closed", "error", "empty"])
def test_incomplete_or_empty_turn_does_not_clear_observation(state, ending):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    clock[0] += 1
    target.error = RuntimeError("disconnected") if ending == "error" else None
    target.partial = ending != "empty"
    target.empty = ending == "empty"

    async def run():
        stream = client.chat_stream("hello")
        if ending == "closed":
            assert await anext(stream) == "partial"
            await stream.aclose()
        elif ending == "error":
            with pytest.raises(RuntimeError, match="disconnected"):
                async for _ in stream:
                    pass
        else:
            async for _ in stream:
                pass

    asyncio.run(run())
    assert store.get(account.id).throttled_until == _RESET_EPOCH


def test_success_already_in_flight_cannot_clear_a_newer_refusal(state):
    store, account, target, client, clock = state

    async def run():
        target.started = asyncio.Event()
        target.release = asyncio.Event()
        older = asyncio.create_task(client.chat("slow"))
        await target.started.wait()
        clock[0] += 1
        _refuse(target)
        with pytest.raises(AccountThrottled):
            await client.chat("hello")
        target.error = None
        clock[0] += 1
        target.release.set()
        await older
        assert store.get(account.id).throttled_until == _RESET_EPOCH
        await client.chat("hello")
        assert store.get(account.id).throttled_until == 0.0

    asyncio.run(run())


def test_provider_switch_preserves_observation_but_ignores_old_client(state):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    clock[0] += 1
    store.update_token(account.id, make_jwe(), substrate_account_id="home:first")
    assert store.get(account.id).throttled_until == _RESET_EPOCH

    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    assert store.get(account.id).provider == "m365"
    assert store.get(account.id).throttled_until == _RESET_EPOCH


def test_same_provider_credential_renewal_does_not_claim_quota_recovery(state):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    clock[0] += 1
    store.set_consumer_auth(account.id, [], "renewed-fixture")
    assert store.get(account.id).throttled_until == _RESET_EPOCH


def test_consumer_identity_switch_discards_old_observation_and_client(state):
    store, account, target, client, clock = state
    _refuse(target)
    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    clock[0] += 1
    store.set_consumer_auth(account.id, [], "other-fixture", consumer_account_id="home:second")
    assert store.get(account.id).throttled_until == 0.0

    with pytest.raises(AccountThrottled):
        asyncio.run(client.chat("hello"))
    assert store.get(account.id).throttled_until == 0.0
