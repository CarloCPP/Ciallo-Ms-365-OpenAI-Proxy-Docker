from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings


SUBJECT = 'home:personal-user'
CONSUMER_COOKIES = [{'name': 'consumer-session', 'value': 'consumer-value', 'domain': '.copilot.com'}]
SUBSTRATE_COOKIES = [{'name': 'substrate-session', 'value': 'substrate-value', 'domain': '.copilot.com'}]


@pytest.fixture
def api(tmp_path, monkeypatch):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY='test-admin'))
    monkeypatch.setattr('m365_copilot_openai_proxy.routes_user._spawn_post_push_refresh', lambda *a, **kw: None)
    account = app.state.account_store.add(name='personal')
    key = app.state.key_store.add(account_id=account.id)
    return app, account, TestClient(app), {'Authorization': 'Bearer ' + key.key}


def push_substrate(client, headers, *, subject=SUBJECT, activate=True):
    return client.post('/user/account/token', headers=headers, json={
        'token': make_jwe(), 'substrate_account_id': subject,
        'display_email': 'personal@example.com', 'activate': activate,
    })


def push_consumer(client, headers, *, subject=SUBJECT, activate=True):
    return client.post('/user/account/consumer', headers=headers, json={
        'access_token': 'consumer-token-' + 'x' * 40, 'consumer_account_id': subject,
        'cookies': CONSUMER_COOKIES, 'email': 'personal@example.com',
        'expires_at': time.time() + 3600, 'activate': activate,
    })


def test_push_and_roundtrip_preserve_both_protocols_and_cookies(api, tmp_path):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    app.state.account_store.set_cookies(account.id, SUBSTRATE_COOKIES)
    old_token = account.token
    assert push_consumer(client, headers).status_code == 200
    assert account.provider == 'consumer'
    assert account.token == old_token
    assert account.cookies == SUBSTRATE_COOKIES
    assert account.consumer_cookies == CONSUMER_COOKIES
    consumer_token = account.consumer_token
    first_epoch = account.protocol_epoch
    response = client.post('/user/account/protocol', headers=headers, json={'provider': 'm365'})
    assert response.status_code == 200
    assert account.provider == 'm365'
    assert account.consumer_token == consumer_token
    assert account.consumer_cookies == CONSUMER_COOKIES
    assert account.protocol_epoch > first_epoch
    response = client.post('/user/account/protocol', headers=headers, json={'provider': 'consumer'})
    assert response.status_code == 200
    reloaded = AccountStore(tmp_path / 'accounts.json').get(account.id)
    assert reloaded.consumer_token == consumer_token
    assert reloaded.token == old_token
    assert reloaded.cookies == SUBSTRATE_COOKIES
    assert reloaded.consumer_cookies == CONSUMER_COOKIES
    raw = json.loads((tmp_path / 'accounts.json').read_text())
    assert raw[account.id]['consumer_cookies']['__enc__'] == 1


def test_save_inactive_consumer_does_not_change_active_protocol(api):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    assert push_consumer(client, headers, activate=False).status_code == 200
    assert account.provider == 'm365'
    assert account.consumer_token
    public = client.get('/user/me', headers=headers).json()['account']
    assert public['protocols']['m365']['active'] is True
    assert public['protocols']['consumer']['stored'] is True


@pytest.mark.parametrize('order', ['substrate-first', 'consumer-first'])
def test_other_personal_subject_cannot_replace_either_saved_protocol(api, order):
    app, account, client, headers = api
    if order == 'substrate-first':
        assert push_substrate(client, headers).status_code == 200
        response = push_consumer(client, headers, subject='home:other')
        assert response.status_code == 409
        assert account.provider == 'm365'
        assert not account.consumer_token
    else:
        assert push_consumer(client, headers).status_code == 200
        response = push_substrate(client, headers, subject='home:other')
        assert response.status_code == 409
        assert account.provider == 'consumer'
        assert not account.token


def test_switch_without_credentials_never_changes_active_provider(api):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    response = client.post('/user/account/protocol', headers=headers, json={'provider': 'consumer'})
    assert response.status_code == 409
    assert account.provider == 'm365'


def test_credential_logout_removes_both_saved_protocols(api):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    assert push_consumer(client, headers).status_code == 200
    app.state.account_store.clear_credentials(account.id)
    assert not account.token
    assert not account.consumer_token
    assert not getattr(account, 'consumer_cookies', [])
    assert not account.cookies


def test_corrupt_consumer_cookie_envelope_never_imports_substrate_cookies(api, tmp_path):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    app.state.account_store.set_cookies(account.id, SUBSTRATE_COOKIES)
    assert push_consumer(client, headers).status_code == 200
    path = tmp_path / 'accounts.json'
    raw = json.loads(path.read_text())
    raw[account.id]['consumer_cookies'] = {'__enc__': 1, 'n': 'broken', 'ct': 'broken'}
    path.write_text(json.dumps(raw))
    loaded = AccountStore(path).get(account.id)
    assert loaded.consumer_cookies == []
    assert loaded.cookies == SUBSTRATE_COOKIES


def test_switch_back_preserves_refresh_grant_but_rejects_prior_epoch_result(api):
    app, account, client, headers = api
    assert push_substrate(client, headers).status_code == 200
    assert push_consumer(client, headers).status_code == 200
    store = app.state.account_store
    store.set_consumer_refresh_token(account.id, 'existing-refresh-grant')
    epoch = account.protocol_epoch
    snapshot = (account.consumer_updated_at, account.consumer_token, account.consumer_account_id)
    assert client.post('/user/account/protocol', headers=headers, json={'provider': 'm365'}).status_code == 200
    assert client.post('/user/account/protocol', headers=headers, json={'provider': 'consumer'}).status_code == 200
    assert account.consumer_refresh_token == 'existing-refresh-grant'
    assert store.set_consumer_auth(
        account.id, CONSUMER_COOKIES, 'late-token', consumer_account_id=SUBJECT,
        expected_snapshot=snapshot, expected_epoch=epoch, activate=False,
    ) is None
    assert account.consumer_token == snapshot[1]


def test_activating_saved_substrate_cookies_establishes_renewal(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from test_m365_refresh_token_flow import _jwt
    from m365_copilot_openai_proxy.refresh_cookie_inject import _apply_opportunistic_token
    from m365_copilot_openai_proxy.routes_user import _BACKGROUND_TASKS

    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY='test-admin'))
    store = app.state.account_store
    account = store.add(name='personal')
    store.set_consumer_auth(account.id, CONSUMER_COOKIES, 'chatai-token', consumer_account_id=SUBJECT)
    key = app.state.key_store.add(account_id=account.id)
    personal_tenant = '84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa'
    original = _jwt(tid=personal_tenant, marker='pushed')
    renewed = _jwt(tid=personal_tenant, marker='renewed')

    async def browser_capture(account_id, cookies, *, allow_nudge=False):
        _apply_opportunistic_token(store, account_id, account.email, renewed, expected_epoch=account.protocol_epoch)
        return len(cookies), len(cookies)

    monkeypatch.setattr(app.state.refresh_scheduler, '_inject_cookies_one', browser_capture)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer ' + key.key}) as client:
            saved = await client.post('/user/account/token', json={
                'token': original, 'substrate_account_id': SUBJECT, 'activate': False,
            })
            assert saved.status_code == 200
            cookies = await client.post('/user/account/cookies', json={'cookies': SUBSTRATE_COOKIES})
            assert cookies.status_code == 200
            assert account.provider == 'consumer'
            assert account.token == original
            assert account.cookie_valid is False
            pending_before = set(_BACKGROUND_TASKS)
            switched = await client.post('/user/account/protocol', json={'provider': 'm365'})
            assert switched.status_code == 200
            await asyncio.gather(*(set(_BACKGROUND_TASKS) - pending_before))
            assert account.token == renewed
            assert account.token_source == 'cdp'
            assert account.cookie_valid is True
            assert account.consumer_token == 'chatai-token'
            assert account.consumer_cookies == CONSUMER_COOKIES

    asyncio.run(exercise())
