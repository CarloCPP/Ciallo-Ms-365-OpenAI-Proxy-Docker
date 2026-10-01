from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / "get_token.user.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


def _push(action: str, *, language="zh", media=True, replies=None, cookies=True, missing_media_key=False):
    """执行原处理函数和 gmFetch，仅替换 GM 传输及浏览器环境。"""
    config = json.dumps(dict(
        language=language, media=media, replies=replies or {}, cookies=cookies,
        missing_media_key=missing_media_key, action=action,
    ))
    program = f"""
const config = {config};
let lang = config.language;
let latestToken = 'fixture-substrate';
let latestMediaAuth = config.media ? {{authorization:'Bearer fixture-media',host:'jp-prod.asyncgw.teams.microsoft.com'}} : null;
let latestDesignerAuth = null, latestRefreshToken = '';
const IS_M365_SITE = false;
const requests = [], alerts = [];
const elements = {{}};
const document = {{getElementById: id => elements[id] ||= {{disabled:false,textContent:''}}}};
const alert = text => alerts.push(text);
const getProxyBase = () => 'https://proxy.invalid';
const getUserApiKey = () => config.missing_media_key && requests.length === (config.action === 'pushToken' ? 1 : 2) ? '' : 'fixture-key';
const hasGMCookie = () => true;
const getAllCookies = async () => config.cookies ? [{{name:'fixture',value:'not-real'}}] : [];
const getUsername = () => 'fixture';
const getMsalLocalStorage = () => ({{}});
const getCurrentChatUrl = () => '';
const GM_xmlhttpRequest = options => {{
    const endpoint = new URL(options.url).pathname.split('/').pop();
    requests.push({{endpoint, method:options.method, body:JSON.parse(options.data)}});
    const reply = config.replies[endpoint] || {{}};
    if (reply.event === 'error') {{ options.onerror('fixture network error'); return; }}
    if (reply.event === 'timeout') {{ options.ontimeout(); return; }}
    options.onload({{status:reply.status ?? 200, responseText:reply.raw ?? JSON.stringify(reply.data ?? {{
        token_status:{{seconds_remaining:3600}}, injected:1, total:1
    }})}});
}};
{_between('const I18N =', '// Colored inline-SVG icons')}
{_between('function gmFetch(', '// Get ALL cookies')}
{_between('async function pushUserToken(', '// Push a consumer')}
{_between('async function pushUserMediaAuth(', 'async function pushLatestMediaAuthSilently(')}
{_between('async function pushToken()', '// Push cookies')}
{_between('async function oneClickSetup()', '// Push the most recent captured chat payload')}
(async () => {{
    await {action}();
    process.stdout.write(JSON.stringify({{requests,alerts,elements}}));
}})().catch(error => {{console.error(error); process.exitCode=1;}});
"""
    completed = subprocess.run(
        ["node", "-e", program], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=20,
    )
    result = json.loads(completed.stdout)
    assert len(result["alerts"]) == 1
    if action == "oneClickSetup":
        assert result["elements"]["m365-one-click"]["disabled"] is False
        assert result["elements"]["m365-one-click-text"]["textContent"] == (
            "一键推送" if language == "zh" else "Push"
        )
    return result


def _media_line(result, language):
    prefix = "Media Bearer 推送：" if language == "zh" else "Media Bearer push: "
    lines = [line for line in result["alerts"][0].splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, result["alerts"]
    return lines[0]


def _assert_token_success(result, action, language):
    if action == "oneClickSetup":
        expected = "Token 推送：成功" if language == "zh" else "Token push: success"
    else:
        expected = "Token 已更新" if language == "zh" else "Token updated"
    assert expected in result["alerts"][0]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("action", ["pushToken", "oneClickSetup"])
def test_bundled_media_success_reports_its_own_status_and_preserves_request_order(action, language):
    result = _push(action, language=language)
    expected = ["token", "media-auth"] if action == "pushToken" else ["token", "cookies", "media-auth"]
    assert [r["endpoint"] for r in result["requests"]] == expected
    assert result["requests"][-1] == {
        "endpoint": "media-auth", "method": "POST",
        "body": {"authorization": "Bearer fixture-media", "host": "jp-prod.asyncgw.teams.microsoft.com"},
    }
    _assert_token_success(result, action, language)
    assert ("成功" if language == "zh" else "success") in _media_line(result, language)
    assert "fixture-media" not in result["alerts"][0]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("action", ["pushToken", "oneClickSetup"])
@pytest.mark.parametrize("reply", [
    {"status": 400, "data": {"error": {"message": "fixture rejected"}}},
    {"status": 403, "data": {"error": "fixture forbidden"}},
    {"status": 503, "data": {}},
    {"status": 200, "raw": "<html>fixture login page</html>"},
    {"event": "error"},
    {"event": "timeout"},
], ids=["http400", "http403", "http503", "html200", "network", "timeout"])
def test_media_failure_does_not_disappear_or_replace_token_success(action, language, reply):
    result = _push(action, language=language, replies={"media-auth": reply})
    _assert_token_success(result, action, language)
    line = _media_line(result, language)
    assert ("失败" if language == "zh" else "failed") in line
    assert ("成功" if language == "zh" else "success") not in line
    assert [r["endpoint"] for r in result["requests"]].count("media-auth") == 1
    if "event" in reply:
        assert ("网络" if language == "zh" else "Network") in line
    elif reply.get("raw"):
        assert "JSON" in line
    elif reply.get("data", {}).get("error"):
        assert "fixture" in line
    else:
        assert "503" in line


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("action", ["pushToken", "oneClickSetup"])
def test_uncaptured_media_is_explicitly_skipped_without_request(action, language):
    result = _push(action, language=language, media=False)
    _assert_token_success(result, action, language)
    line = _media_line(result, language)
    assert ("未执行" if language == "zh" else "skipped") in line
    assert ("尚未捕获" if language == "zh" else "not captured") in line
    assert "media-auth" not in [r["endpoint"] for r in result["requests"]]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("action", ["pushToken", "oneClickSetup"])
@pytest.mark.parametrize("reply", [
    {"status": 400, "data": {"error": "fixture token rejected"}},
    {"event": "error"},
], ids=["rejected", "network"])
def test_token_failure_reports_media_not_attempted(action, language, reply):
    result = _push(action, language=language, replies={"token": reply})
    assert [r["endpoint"] for r in result["requests"]] == ["token"]
    line = _media_line(result, language)
    assert ("未执行" if language == "zh" else "skipped") in line
    assert ("前置步骤未成功" if language == "zh" else "prerequisite did not succeed") in line


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("failure", ["missing", "rejected", "network"])
def test_one_click_cookie_failure_keeps_token_result_and_reports_media_skipped(language, failure):
    reply = {"event": "error"} if failure == "network" else {"status": 400, "data": {"error": "fixture cookies rejected"}}
    result = _push("oneClickSetup", language=language, cookies=failure != "missing", replies={"cookies": reply})
    _assert_token_success(result, "oneClickSetup", language)
    assert "media-auth" not in [r["endpoint"] for r in result["requests"]]
    line = _media_line(result, language)
    assert ("未执行" if language == "zh" else "skipped") in line
    assert ("前置步骤未成功" if language == "zh" else "prerequisite did not succeed") in line


@pytest.mark.parametrize("action", ["pushToken", "oneClickSetup"])
def test_missing_media_push_response_cannot_be_reported_as_success(action):
    result = _push(action, missing_media_key=True)
    _assert_token_success(result, action, "zh")
    assert "失败" in _media_line(result, "zh")
    assert "media-auth" not in [r["endpoint"] for r in result["requests"]]


def test_one_click_keeps_cookie_warning_alongside_media_result():
    result = _push("oneClickSetup", replies={"cookies": {"data": {"injected": 1, "total": 2, "warning": "fixture cookie warning"}}})
    assert "Cookie 推送：成功（警告） (1/2)" in result["alerts"][0]
    assert "fixture cookie warning" in result["alerts"][0]
    assert "成功" in _media_line(result, "zh")
