from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / "get_token.user.js").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT[SCRIPT.index(start):SCRIPT.index(end)]


class _Controls(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.buttons = {}
        self.details = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details += 1
        if tag == "button":
            self.buttons[attrs.get("id")] = {**attrs, "collapsed": self.details > 0}

    def handle_endtag(self, tag):
        if tag == "details":
            self.details -= 1


def _render(*, substrate=False, consumer=False, host="copilot.com", language="zh"):
    program = f"""
const location = {{hostname: {json.dumps(host)}}};
{_between('const IS_CONSUMER_SITE =', 'const PROXY_BASE =')}
let lang = {json.dumps(language)};
let latestToken = {json.dumps('substrate-fixture' if substrate else '')};
let latestConsumerToken = {json.dumps('chatai-fixture' if consumer else '')};
let latestMediaAuth = null;
const pushActivation = {{m365:true,consumer:true}};
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
    const result = {{panel: panelBody()}};
    await pushConsumer();
    process.stdout.write(JSON.stringify({{...result,alerts,pushes}}));
}})().catch(error => {{console.error(error); process.exitCode=1;}});
"""
    completed = subprocess.run(
        ["node", "-e", program], check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=20,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("host", ["copilot.com", "m365.cloud.microsoft", "login.live.com"])
@pytest.mark.parametrize("substrate,consumer", [(False, False), (True, False), (False, True), (True, True)])
def test_protocol_entries_are_always_visible_but_require_their_own_capture(host, language, substrate, consumer):
    result = _render(substrate=substrate, consumer=consumer, host=host, language=language)
    controls = _Controls(result["panel"])
    for button, captured in (("m365-one-click", substrate), ("m365-push-consumer", consumer)):
        assert not controls.buttons[button]["collapsed"]
        assert ("disabled" not in controls.buttons[button]) is captured
    assert result["pushes"] == (["consumer"] if consumer else [])
    if not consumer:
        assert "copilot.microsoft.com" in result["panel"]
        assert "ChatAI" in result["alerts"][0]


@pytest.mark.parametrize("activate", [True, False])
@pytest.mark.parametrize("provider", ["m365", "consumer"])
def test_push_exports_selected_protocol_and_canonical_identity_without_relabeling(activate, provider):
    # Exercise the real payload builders against real MSAL selection; the only
    # boundary replaced is transport, so cookie email cannot become the subject.
    program = f"""
const values = {{
  'msal.account.keys': JSON.stringify(['account']),
  'account': JSON.stringify({{homeAccountId:'ONE', username:'label@example.com'}}),
  'msal.active-account-filters': JSON.stringify({{homeAccountId:'ONE'}}),
  'token': JSON.stringify({{credentialType:'AccessToken',homeAccountId:'ONE',secret:'header.key.iv.cipher.tag',target:'substrate'}}),
  'chatai': JSON.stringify({{credentialType:'AccessToken',homeAccountId:'ONE',secret:'chat-token',target:'ChatAI.ReadWrite',clientId:'client'}})
}};
const localStorage = {{get length(){{return Object.keys(values).length}},key(i){{return Object.keys(values)[i]}},getItem(k){{return values[k]??null}}}};
const pushActivation = {{m365:{str(activate).lower()},consumer:{str(activate).lower()}}};
const latestConsumerToken='chat-token',latestConsumerIdentity='MSA';
const getUserApiKey=()=> 'key';
const tr=k=>k;
const hasGMCookie=()=>true;
const getAllCookies=async()=>[];
const requests=[];
const gmFetch=async(url,options)=>{{requests.push({{url,body:JSON.parse(options.body)}});return {{ok:true,json:async()=>({{}})}}}};
{_between('// ---- Consumer account email resolution', '// ---- End consumer account email resolution')}
{_between('async function pushUserToken(', 'async function pushUserMediaAuth(')}
captureSubstrateIdentity('header.key.iv.cipher.tag');
(async()=>{{
  if ({json.dumps(provider)}==='m365') await pushUserToken('https://proxy.invalid','header.key.iv.cipher.tag');
  else await pushUserConsumer('https://proxy.invalid',[]);
  console.log(JSON.stringify(requests));
}})().catch(e=>{{console.error(e);process.exitCode=1}});
"""
    result = subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True, encoding="utf-8")
    request, = json.loads(result.stdout)
    assert request["body"]["activate"] is activate
    if provider == "m365":
        assert request["url"].endswith("/user/account/token")
        assert request["body"]["substrate_account_id"] == "home:one"
        assert request["body"]["display_email"] == "label@example.com"
        assert request["body"]["token"] == "header.key.iv.cipher.tag"
        assert "access_token" not in request["body"]
    else:
        assert request["url"].endswith("/user/account/consumer")
        assert request["body"]["consumer_account_id"] == "home:one"
        assert request["body"]["access_token"] == "chat-token"
        assert "token" not in request["body"]
