from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / "get_token.user.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


class _Buttons(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.buttons = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            attrs = dict(attrs)
            self.buttons[attrs.get("id")] = attrs


def _render(*, substrate=False, consumer=False, host="copilot.com", language="zh"):
    program = f"""
const location = {{hostname: {json.dumps(host)}}};
{_between('const IS_CONSUMER_SITE =', 'const PROXY_BASE =')}
let lang = {json.dumps(language)};
let latestToken = {json.dumps('substrate-fixture' if substrate else '')};
let latestConsumerToken = {json.dumps('chatai-fixture' if consumer else '')};
let latestMediaAuth = null;
const ic = () => '';
const hasGMCookie = () => true;
{_between('const I18N =', '// Colored inline-SVG icons')}
{_between('function siteBadge(', 'function showPanel(')}
const alerts = [], pushes = [];
const alert = text => alerts.push(text);
const getProxyBase = () => 'https://proxy.invalid';
const document = {{getElementById: () => ({{disabled:false,textContent:''}})}};
const getAllCookies = async () => [{{name:'fixture',value:'not-a-real-cookie'}}];
const pushUserConsumer = async () => {{ pushes.push('consumer'); return {{response:{{ok:true}},data:{{cookies:1}}}}; }};
{_between('async function pushConsumer()', '// Copy token to clipboard')}
(async () => {{
    const result = {{
        substrate: m365Section(), consumer: consumerSection(), panel: panelBody(),
        substrate_badge: siteBadge(IS_M365_SITE, 'other_site_m365'),
        consumer_badge: siteBadge(IS_CONSUMER_SITE, 'other_site_consumer')
    }};
    await pushConsumer();
    process.stdout.write(JSON.stringify({{...result,alerts,pushes}}));
}})().catch(error => {{console.error(error); process.exitCode=1;}});
"""
    completed = subprocess.run(
        ["node", "-e", program], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=20,
    )
    return json.loads(completed.stdout)


def _consumer_button(result):
    return _Buttons(result["consumer"]).buttons["m365-push-consumer"]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("host", ["copilot.com", "m365.cloud.microsoft"])
def test_titles_name_protocols_instead_of_separating_account_types(host, language):
    result = _render(substrate=True, host=host, language=language)
    assert "Substrate" in result["substrate"]
    assert ("工作/个人" if language == "zh" else "work/personal") in result["substrate"]
    assert ("ChatAI 兼容入口" if language == "zh" else "ChatAI compatibility") in result["consumer"]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_substrate_only_disables_chatai_and_points_to_substrate(language):
    result = _render(substrate=True, language=language)
    assert "disabled" in _consumer_button(result)
    assert "Substrate" in result["consumer"]
    assert "copilot.microsoft.com" not in result["consumer"]
    assert result["pushes"] == []
    assert len(result["alerts"]) == 1
    assert "Substrate" in result["alerts"][0]


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("host", ["copilot.com", "m365.cloud.microsoft", "login.live.com"])
def test_hostname_alone_does_not_claim_credentials_were_captured(host, language):
    result = _render(host=host, language=language)
    for key in ("substrate_badge", "consumer_badge"):
        assert ("当前页面" if language == "zh" else "this page") not in result[key]
        assert "#22c55e" not in result[key]
    assert result["pushes"] == []


@pytest.mark.parametrize("language", ["zh", "en"])
def test_badges_distinguish_substrate_capture_from_missing_chatai(language):
    result = _render(substrate=True, language=language)
    assert ("已捕获" if language == "zh" else "captured") in result["substrate_badge"]
    assert "#22c55e" in result["substrate_badge"]
    assert "#22c55e" not in result["consumer_badge"]


@pytest.mark.parametrize("substrate", [False, True])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_captured_chatai_stays_enabled_even_when_substrate_is_also_present(substrate, language):
    result = _render(substrate=substrate, consumer=True, language=language)
    assert "disabled" not in _consumer_button(result)
    assert result["pushes"] == ["consumer"]
    assert "#22c55e" in result["consumer_badge"]


@pytest.mark.parametrize("language", ["zh", "en"])
def test_frame_capture_scope_names_substrate_including_personal_sessions(language):
    result = _render(substrate=True, language=language)
    assert ("仅 Substrate" if language == "zh" else "Substrate only") in result["panel"]
