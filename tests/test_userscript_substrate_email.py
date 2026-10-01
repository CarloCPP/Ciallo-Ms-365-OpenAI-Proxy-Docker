from __future__ import annotations

import json
import subprocess

import pytest

from test_userscript_consumer_email import _email_resolution_source


def resolve(storage, cookies=(), token="header.key.iv.cipher.tag", session=None, *, identity=False):
    program = """
const values = %s;
const localStorage = {
    get length() { return Object.keys(values).length; },
    key(i) { return Object.keys(values)[i]; },
    getItem(k) { return values[k] ?? null; }
};
const tabValues = %s;
const sessionStorage = {
    get length() { return Object.keys(tabValues).length; },
    key(i) { return Object.keys(tabValues)[i]; },
    getItem(k) { return tabValues[k] ?? null; }
};
%s
captureSubstrateIdentity(%s);
console.log(JSON.stringify(getSubstrateIdentity(%s, %s)));
""" % (json.dumps(storage), json.dumps(session or {}), _email_resolution_source(), json.dumps(token), json.dumps(cookies), json.dumps(token))
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    result = json.loads(result.stdout)
    return result if identity else result["email"]


def records(*, active="one", token_subject="one", email="one@example.com"):
    return {
        "msal.account.keys": json.dumps(["account1", "account2"]),
        "account1": json.dumps({"homeAccountId": "one", "username": email}),
        "account2": json.dumps({"homeAccountId": "two", "username": "two@example.com"}),
        "msal.active-account-filters": json.dumps({"homeAccountId": active}),
        "cached-token": json.dumps({"credentialType": "AccessToken", "homeAccountId": token_subject,
                                     "secret": "header.key.iv.cipher.tag", "target": "substrate"}),
    }


def test_matching_substrate_token_and_active_account_supplies_email():
    assert resolve(records()) == "one@example.com"


def test_switched_active_account_does_not_relabel_older_jwe():
    assert resolve(records(active="two")) == ""


def test_encrypted_token_cache_uses_unique_current_account_only():
    storage = records()
    storage.pop("cached-token")
    assert resolve(storage) == "one@example.com"
    storage.pop("msal.active-account-filters")
    assert resolve(storage) == ""


@pytest.mark.parametrize("cookies,expected", [
    ([{"name": "MSPPre", "value": "one%40example.com", "domain": ".login.live.com"}], "one@example.com"),
    ([{"name": "MSPPre", "value": "one%40example.com", "domain": ".evil.invalid"}], ""),
    ([{"name": "MSPPre", "value": "one%40example.com two%40example.com", "domain": ".login.live.com"}], ""),
])
def test_cookie_fallback_is_unambiguous_and_microsoft_scoped(cookies, expected):
    assert resolve(records(email=""), cookies) == expected


def test_ambiguous_token_subject_is_not_labeled():
    storage = records()
    storage["other-token"] = json.dumps({"credentialType": "AccessToken", "homeAccountId": "two", "secret": "header.key.iv.cipher.tag"})
    assert resolve(storage) == ""


def test_session_account_supplies_email_when_local_msal_account_is_encrypted():
    storage = {
        "msal.3.account.keys": json.dumps(["encrypted-account"]),
        "encrypted-account": json.dumps({"id": "opaque", "nonce": "opaque", "data": "encrypted"}),
        "msal.active-account-filters": json.dumps({"homeAccountId": "one", "tenantId": "msa"}),
    }
    session = {
        "msal.2.account.keys": json.dumps(["session-account"]),
        "session-account": json.dumps({"homeAccountId": "one", "realm": "msa", "username": "current@example.com"}),
    }
    assert resolve(storage, session=session) == "current@example.com"


def test_subject_is_canonical_msal_id_not_display_email():
    assert resolve(records(email="label@example.com"), identity=True) == {
        "account_id": "home:one", "email": "label@example.com",
    }


def test_conflicting_active_subject_exports_neither_subject_nor_label():
    assert resolve(records(active="two"), identity=True) == {"account_id": "", "email": ""}


def test_cookie_email_cannot_supply_a_subject():
    cookies = [{"name": "MSPPre", "value": "one%40example.com", "domain": ".live.com"}]
    assert resolve({}, cookies, identity=True) == {"account_id": "", "email": ""}


def test_jwe_encrypted_key_is_never_used_as_identity_claims():
    import base64

    key = base64.urlsafe_b64encode(json.dumps({"homeAccountId": "attacker", "email": "fake@example.com"}).encode()).decode().rstrip("=")
    assert resolve({}, token=f"header.{key}.iv.cipher.tag", identity=True) == {"account_id": "", "email": ""}


def test_unscoped_cache_key_is_not_a_canonical_subject():
    storage = {"msal.account.keys": json.dumps(["account1"]), "account1": json.dumps({"username": "label@example.com"})}
    assert resolve(storage, identity=True) == {"account_id": "", "email": ""}


def test_session_and_local_active_subject_conflict_is_rejected():
    session = {"msal.active-account-filters": json.dumps({"homeAccountId": "two"})}
    assert resolve(records(), session=session, identity=True) == {"account_id": "", "email": ""}


@pytest.mark.parametrize("known_at_capture", [True, False])
def test_opaque_jwe_is_not_rebound_after_browser_account_changes(known_at_capture):
    from test_userscript_consumer_email import SCRIPT

    storage = records()
    storage.pop("cached-token")
    if not known_at_capture:
        storage.pop("msal.active-account-filters")
    hook = SCRIPT[SCRIPT.index("    const OrigWebSocket = pageWindow.WebSocket;"):
                  SCRIPT.index("    function getProxyBase()")]
    program = rf"""
const values={json.dumps(storage)};
const localStorage={{get length(){{return Object.keys(values).length}},key(i){{return Object.keys(values)[i]}},getItem(k){{return values[k]??null}}}};
let latestToken='',latestConsumerToken='',latestConsumerIdentity='',capturedPayloads=[];
const pageWindow={{WebSocket:class {{send(){{}}}}}};
const SUBSTRATE_WS_RE=/wss:\/\/substrate\.office\.com\/.*[?&]access_token=([^&]+)/;
const CONSUMER_WS_RE=/[?&]accessToken=([^&]+)/,CONSUMER_IDENTITY_RE=/[?&]X-UserIdentityType=([^&]+)/;
const showPanel=()=>{{}},renderCaptured=()=>{{}};
{_email_resolution_source()}
{hook}
new pageWindow.WebSocket('wss://substrate.office.com/chat?access_token=header.key.iv.cipher.tag');
values['msal.active-account-filters']=JSON.stringify({{homeAccountId:'two'}});
const stale=getSubstrateIdentity([],latestToken);
new pageWindow.WebSocket('wss://substrate.office.com/chat?access_token=header.key.iv.newcipher.tag');
console.log(JSON.stringify([stale,getSubstrateIdentity([],latestToken)]));
"""
    completed = subprocess.run(["node", "-"], input=program, capture_output=True, text=True, check=True)
    stale, fresh = json.loads(completed.stdout)
    assert stale == {"account_id": "", "email": ""}
    assert fresh == {"account_id": "home:two", "email": "two@example.com"}
