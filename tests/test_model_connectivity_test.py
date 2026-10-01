"""/admin/model-test contract: one real turn, four actionable verdicts.

The upstream client is faked -- what matters here is that the probe rides the same
client factory real traffic uses (so a pass means something), that it starts no
session, and that each upstream outcome maps to the verdict an operator acts on
differently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.consumer_client import AccountThrottled
from m365_copilot_openai_proxy.routes_admin_modeltest import classify_probe
from m365_copilot_openai_proxy.substrate_client import _M365_REFUSAL_TEXTS
from m365_copilot_openai_proxy.substrate_client import SubstrateCopilotError


class _FakeClient:
    """Stands in for SubstrateCopilotClient; `outcome` drives the probe result."""

    outcome: object = "pong"
    seen: list[tuple] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def chat(self, prompt, additional_context, session=None, images=None):
        _FakeClient.seen.append((prompt, additional_context, session, images, self.kwargs))
        if isinstance(_FakeClient.outcome, Exception):
            raise _FakeClient.outcome
        return _FakeClient.outcome


@pytest.fixture
def env(tmp_path):
    _FakeClient.outcome = "pong"
    _FakeClient.seen = []
    app = create_app(
        Settings(TOKEN_DIR=str(tmp_path), API_KEY="admin-key"),
        copilot_client_factory=lambda **kwargs: _FakeClient(**kwargs),
    )
    admin = TestClient(app)
    assert admin.post("/admin/login", json={"password": "admin-key"}).status_code == 200
    account = app.state.account_store.add(name="Pool", token="header.body.sig")
    tone = app.state.tone_options[0]
    return app, admin, account, str(tone.get("label") or tone.get("value"))


def test_classify_probe_maps_each_outcome():
    assert classify_probe("pong") == "ok"
    assert classify_probe("   ") == "empty"
    assert classify_probe("", "M365 refused this turn") == "refused"
    assert classify_probe("", "empty response twice in a row") == "refused"
    assert classify_probe("", "websocket closed") == "error"
    # Quota is not availability: the mode may be perfectly fine.
    assert classify_probe("", "over quota", throttled=True) == "throttled"


def test_a_canned_refusal_reply_is_not_reported_as_working():
    """M365 declines a mode in two ways, and only one of them raises: it also answers
    with a single canned line, which arrives here as an ordinary non-empty reply. Read
    as "ok", a sweep of the picker reports every mode the tenant may not use as
    working -- the exact column an operator decides on. The set is imported from
    substrate_client, so a reworded upstream line cannot silently pass this."""
    for line in _M365_REFUSAL_TEXTS:
        assert classify_probe(line) == "refused", line
        # Leading/trailing whitespace from the stream must not defeat the match.
        assert classify_probe(f"\n{line}  ") == "refused", line
    # A reply that merely mentions the phrase is still a real answer.
    assert classify_probe(f"You asked why I said: {next(iter(_M365_REFUSAL_TEXTS))}") == "ok"


def test_probe_answers_ok_and_reports_the_selector(env):
    app, admin, account, tone = env
    r = admin.post("/admin/model-test", json={"account_id": account.id, "model": tone})

    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "ok"
    assert body["reply"] == "pong"
    assert body["reply_len"] == 4
    assert body["provider"] == "m365"
    assert body["upstream_selector"]
    assert body["latency_ms"] >= 0
    # No session: a probe must not continue (or reset) a live conversation, and
    # the account's own token must reach the client the same way /v1 does.
    prompt, context, session, images, kwargs = _FakeClient.seen[-1]
    assert session is None and images is None and context == []
    assert kwargs["token"] == "header.body.sig"
    assert app.state.session_store.items() == []


def test_probe_reports_empty_reply_as_mode_unavailable(env):
    _app, admin, account, tone = env
    _FakeClient.outcome = ""

    body = admin.post("/admin/model-test", json={"account_id": account.id, "model": tone}).json()

    assert body["verdict"] == "empty"
    assert body["reply"] == ""


def test_probe_separates_refusal_from_transport_failure(env):
    _app, admin, account, tone = env
    _FakeClient.outcome = SubstrateCopilotError("M365 refused this turn (tone=Balanced)")
    refused = admin.post("/admin/model-test", json={"account_id": account.id, "model": tone}).json()

    _FakeClient.outcome = SubstrateCopilotError("websocket closed before any reply")
    broken = admin.post("/admin/model-test", json={"account_id": account.id, "model": tone}).json()

    assert refused["verdict"] == "refused"
    assert "refused this turn" in refused["error"]
    assert broken["verdict"] == "error"


def test_probe_reports_quota_as_throttled(env):
    _app, admin, account, tone = env
    _FakeClient.outcome = AccountThrottled("daily limit reached")

    body = admin.post("/admin/model-test", json={"account_id": account.id, "model": tone}).json()

    assert body["verdict"] == "throttled"


def test_probe_uses_a_custom_prompt_when_given(env):
    _app, admin, account, tone = env

    admin.post(
        "/admin/model-test",
        json={"account_id": account.id, "model": tone, "prompt": "draw me a cat"},
    )

    assert _FakeClient.seen[-1][0] == "draw me a cat"


def test_probe_rejects_missing_arguments_and_unknown_accounts(env):
    _app, admin, account, tone = env

    assert admin.post("/admin/model-test", json={"model": tone}).status_code == 400
    assert admin.post("/admin/model-test", json={"account_id": account.id}).status_code == 400
    assert admin.post("/admin/model-test", content=b"not json").status_code == 400
    assert admin.post(
        "/admin/model-test", json={"account_id": "nope", "model": tone}
    ).status_code == 404


def test_probe_requires_admin(env):
    app, _admin, account, tone = env
    anon = TestClient(app)

    r = anon.post("/admin/model-test", json={"account_id": account.id, "model": tone})

    assert r.status_code in (401, 403)
    assert not _FakeClient.seen


def _run_model_test_ui(tmp_path, steps):
    import shutil
    import subprocess

    from m365_copilot_openai_proxy.template_admin_i18n import _ADMIN_I18N_JS
    from m365_copilot_openai_proxy.template_admin_modeltest import _ADMIN_MODELTEST_JS

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for model-test UI behavior tests")
    harness = r"""
const assert=require('assert');
const decode=s=>s.replace(/&quot;/g,'"').replace(/&gt;/g,'>').replace(/&lt;/g,'<').replace(/&amp;/g,'&');
class Element {
  constructor(id){this.id=id;this.value='';this.disabled=false;this.textContent='';this.html='';this.inputs=[]}
  set innerHTML(html){
    this.html=html;
    if(this.id==='model-test-account'||this.id==='model-test-model'){
      this.options=Array.from(html.matchAll(/<option value="([^"]*)"/g),m=>decode(m[1]));
      this.value=this.options[0]||'';
    }
    if(this.id==='model-test-models')this.inputs=Array.from(html.matchAll(/<input\b([^>]*)>/g),m=>{
      const a=m[1],flags=a.replace(/"[^"]*"|'[^']*'/g,'');return {value:decode((a.match(/value="([^"]*)"/)||[])[1]||''),checked:/\bchecked\b/.test(flags),disabled:/\bdisabled\b/.test(flags)};
    });
  }
  get innerHTML(){return this.html}
}
const ids=['controls','account','model','prompt','models','selection-count','progress','run','run-all','run-selected','select-all','clear','result'];
const elements=Object.fromEntries(ids.map(id=>['model-test-'+id,new Element('model-test-'+id)]));
const el=id=>elements['model-test-'+id];
const document={getElementById:id=>elements[id]||null};
let lang='en';
const t=k=>i18n[lang][k]??k;
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
let __accounts=[{id:'first',name:'First',provider:'consumer'},{id:'second',name:'Second',provider:'consumer'}];
let __runtimeSettings={consumer_mode_options:[{model:'alpha',mode:'one'},{model:'beta',mode:'two'},{model:'gamma',mode:'three'}]};
const acctLabel=a=>a.name,refreshGlassSelect=()=>{};
const alerts=[];
const adminAlert=async s=>alerts.push(s);
let logins=0;
const showInlineLogin=()=>logins++;
const calls=[],pending=[];
const fetch=(url,init)=>new Promise(resolve=>{calls.push(JSON.parse(init.body));pending.push(resolve)});
const settle=async(body={verdict:'ok',reply:'pong',cleanup:{status:'deleted'}},status=200)=>{
  assert.ok(pending.length,'a probe must be awaiting its response');
  pending.shift()({status,ok:status===200,json:async()=>body});
  await new Promise(resolve=>setImmediate(resolve));
};
const selected=()=>el('models').inputs.filter(x=>x.checked).map(x=>x.value);
const check=(id,value)=>{const input=el('models').inputs.find(x=>x.value===id);assert.ok(input);input.checked=value;toggleModelTestSelection(id,value)};
"""
    script = tmp_path / "model-test-ui.js"
    script.write_text(
        _ADMIN_I18N_JS + harness + _ADMIN_MODELTEST_JS
        + "\n(async()=>{renderModelTest();" + steps
        + "})().then(()=>console.log('model-test-ui completed')).catch(e=>{console.error(e);process.exit(1)});",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "model-test-ui completed" in result.stdout, "UI scenario did not finish"


def test_probe_ui_selected_batch_is_sequential_and_frozen_across_rerenders(tmp_path):
    _run_model_test_ui(tmp_path, """
      check('gamma',true);check('beta',true);check('alpha',true);check('beta',false);
      el('prompt').value='original prompt';
      const run=runModelTest('selected');
      assert.deepStrictEqual(calls,[{account_id:'first',model:'alpha',prompt:'original prompt'}]);
      assert.strictEqual(el('controls').disabled,true);
      assert.ok(el('models').inputs.every(x=>x.disabled));
      await runModelTest(true);
      assert.strictEqual(calls.length,1,'repeat clicks must not start another run');
      __accounts[0].id='changed';
      __runtimeSettings.consumer_mode_options=[{model:'replacement',mode:'four'}];
      el('account').value='second';el('prompt').value='changed prompt';
      lang='zh';renderModelTest();
      assert.strictEqual(el('account').value,'first');
      assert.strictEqual(el('prompt').value,'original prompt');
      assert.deepStrictEqual(selected(),['alpha','gamma']);
      assert.strictEqual(calls.length,1,'language render must not send probes');
      await settle();
      assert.deepStrictEqual(calls,[
        {account_id:'first',model:'alpha',prompt:'original prompt'},
        {account_id:'first',model:'gamma',prompt:'original prompt'}
      ]);
      const detached=el('run-selected');
      elements['model-test-run-selected']=new Element('model-test-run-selected');
      elements['model-test-controls']=new Element('model-test-controls');
      renderModelTest();
      assert.strictEqual(el('run-selected').disabled,true);
      await settle();await run;
      assert.strictEqual(el('controls').disabled,false);
      assert.strictEqual(el('run-all').disabled,false);
      assert.strictEqual(detached.disabled,true,'completion must not touch an obsolete button');
      assert.ok(el('progress').textContent.includes('2 / 2'));
    """)


def test_probe_ui_selection_survives_language_but_not_account_or_catalog_removal(tmp_path):
    _run_model_test_ui(tmp_path, """
      check('alpha',true);check('gamma',true);
      lang='zh';renderModelTest();
      assert.deepStrictEqual(selected(),['alpha','gamma']);
      __runtimeSettings.consumer_mode_options.splice(0,1);renderModelTest();
      assert.deepStrictEqual(selected(),['gamma']);
      __runtimeSettings.consumer_mode_options.push({model:'alpha',mode:'one'});renderModelTest();
      assert.deepStrictEqual(selected(),['gamma'],'removed selections must not resurrect');
      el('account').value='second';renderModelTest();
      assert.deepStrictEqual(selected(),[]);
      selectModelTestModels(true);
      assert.deepStrictEqual(selected(),['beta','gamma','alpha']);
      selectModelTestModels(false);
      await runModelTest('selected');
      assert.deepStrictEqual(calls,[],'an empty selection must not probe');
      assert.strictEqual(el('run-selected').disabled,true);
    """)


def test_probe_ui_single_and_all_keep_their_targets_and_401_stops_batch(tmp_path):
    _run_model_test_ui(tmp_path, """
      el('model').value='beta';
      const single=runModelTest(false);
      assert.deepStrictEqual(calls.map(c=>c.model),['beta']);
      await settle();await single;
      const all=runModelTest(true);
      assert.deepStrictEqual(calls.map(c=>c.model),['beta','alpha']);
      await settle({},401);await all;
      assert.strictEqual(logins,1);
      assert.deepStrictEqual(calls.map(c=>c.model),['beta','alpha'],'401 must terminate the queue');
      assert.strictEqual(el('controls').disabled,false);
      assert.strictEqual(el('run-all').disabled,false);
      assert.ok(el('result').innerHTML.includes(t('mt_auth_required')));
      const retry=runModelTest(true);
      await settle();await settle();await settle();await retry;
      assert.deepStrictEqual(calls.slice(2).map(c=>c.model),['alpha','beta','gamma']);
    """)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_probe_ui_cleanup_is_separate_and_escapes_server_details(tmp_path, language):
    _run_model_test_ui(tmp_path, "lang=" + repr(language) + ";" + """
      check('alpha',true);
      const run=runModelTest('selected');
      await settle({verdict:'ok',reply:'<b>reply</b>',cleanup:{status:'failed',message:'<img src=x onerror="alert(1)">'}});
      await run;
      assert.strictEqual(el('run-selected').disabled,false);
      const html=el('result').innerHTML;
      assert.ok(html.includes(t('mt_v_ok')));
      assert.ok(html.includes(t('mt_cleanup_failed')));
      assert.ok(html.includes('&lt;b&gt;reply&lt;/b&gt;'));
      assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
      assert.ok(!html.includes('<img'));
      renderModelTest();assert.deepStrictEqual(selected(),['alpha']);
      const legacy=runModelTest(false);
      await settle({verdict:'empty',reply:''});await legacy;
      assert.ok(el('result').innerHTML.includes(t('mt_v_empty')));
      assert.ok(el('result').innerHTML.includes(t('mt_cleanup_unknown')));
      const failure=runModelTest(false);
      await settle({error:{message:'<upstream error>'},cleanup:{status:'unsupported',message:'delete_unsupported'}},500);
      await failure;
      assert.ok(el('result').innerHTML.includes(t('mt_v_error')));
      assert.ok(el('result').innerHTML.includes('&lt;upstream error&gt;'));
      assert.ok(el('result').innerHTML.includes(t('mt_cleanup_unsupported')));
      assert.ok(el('result').innerHTML.includes(t('mt_cleanup_delete_unsupported')));
    """)
