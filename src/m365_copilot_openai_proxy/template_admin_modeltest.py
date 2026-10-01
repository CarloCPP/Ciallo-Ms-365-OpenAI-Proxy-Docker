from __future__ import annotations

# Model connectivity probes (/admin/model-test). Which mode works is decided by
# Microsoft's rollout per account, so the only way to know is a real turn.
# Selected and all-model runs await each turn in catalog order. Two-stage per the template
# convention: loadModelTest() only fetches what it needs, renderModelTest() reads
# the cache, so a language switch costs no network and no extra upstream turns.
_ADMIN_MODELTEST_JS = """let __modelTest=[];
let __modelTestBusy=false;
let __modelTestSelected=new Set();
let __modelTestAccountId='';
let __modelTestRun=null;
let __modelTestProgress=null;
const _MT_COLORS={ok:'#22c55e',empty:'#f59e0b',refused:'#ef4444',throttled:'#a78bfa',error:'#94a3b8',running:'#38bdf8'};
function _mtBadge(v){
  const c=_MT_COLORS[v]||'#94a3b8';
  return '<span style="padding:.1rem .5rem;border-radius:99px;font-size:.7rem;white-space:nowrap;border:1px solid '+c+'66;background:'+c+'22;color:'+c+'">'+t('mt_v_'+v)+'</span>';
}
function _mtAccount(){
  const sel=document.getElementById('model-test-account');
  return (__accounts||[]).find(a=>a.id===(sel&&sel.value));
}
// The probe takes whatever a client would put in "model": a tone label/value for
// M365, a configured model id for the personal edition. __runtimeSettings is the
// script-scope cache the debug view already fetches (not a window property).
function _mtModels(acct){
  if(!acct)return [];
  if((acct.provider||'m365')==='consumer')
    return ((__runtimeSettings||{}).consumer_mode_options||[]).map(o=>({id:o.model,label:o.model+' \\u00b7 '+o.mode}));
  return _toneOptsSource().map(o=>({id:o.label||o.value,label:_toneLabel(o.value)}));
}
function _mtControls(){
  const controls=document.getElementById('model-test-controls');
  if(controls)controls.disabled=__modelTestBusy;
  const count=document.getElementById('model-test-selection-count');
  if(count)count.textContent=t('mt_selected_count').replace('{count}',__modelTestSelected.size);
  const buttons=[['run','mt_run'],['run-all','mt_run_all'],['run-selected','mt_run_selected'],['select-all','mt_select_all'],['clear','mt_clear']];
  buttons.forEach(([id,key])=>{
    const btn=document.getElementById('model-test-'+id);
    if(btn){btn.disabled=__modelTestBusy||(id==='run-selected'&&!__modelTestSelected.size);btn.textContent=t(key)}
  });
  const progress=document.getElementById('model-test-progress');
  if(progress)progress.textContent=__modelTestProgress
    ?t(__modelTestProgress.stopped?'mt_progress_stopped':(__modelTestBusy?'mt_progress_running':'mt_progress_done'))
      .replace('{done}',__modelTestProgress.done).replace('{total}',__modelTestProgress.total):'';
}
function toggleModelTestSelection(id,checked){
  if(__modelTestBusy)return;
  if(checked)__modelTestSelected.add(id);else __modelTestSelected.delete(id);
  _mtControls();
}
function selectModelTestModels(all){
  if(__modelTestBusy)return;
  __modelTestSelected=new Set(all?_mtModels(_mtAccount()).map(m=>m.id):[]);
  renderModelTestOptions();
}
function renderModelTestOptions(){
  const asel=document.getElementById('model-test-account');
  if(!asel)return;
  const accounts=__modelTestRun?[__modelTestRun.account]:(__accounts||[]);
  const curA=__modelTestRun?__modelTestRun.account.id:asel.value;
  asel.innerHTML=accounts.map(a=>'<option value="'+esc(a.id)+'">'+esc(acctLabel(a))+'</option>').join('');
  if(curA&&accounts.some(a=>a.id===curA))asel.value=curA;
  if(asel.value!==__modelTestAccountId){__modelTestSelected.clear();__modelTestAccountId=asel.value}
  asel.disabled=__modelTestBusy;
  refreshGlassSelect(asel);
  const models=__modelTestRun?__modelTestRun.models:_mtModels(_mtAccount());
  const available=new Set(models.map(m=>m.id));
  __modelTestSelected.forEach(id=>{if(!available.has(id))__modelTestSelected.delete(id)});
  const msel=document.getElementById('model-test-model');
  if(msel){
    const curM=__modelTestRun?__modelTestRun.singleModel:msel.value;
    msel.innerHTML=models.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.label)+'</option>').join('');
    if(curM&&available.has(curM))msel.value=curM;
    msel.disabled=__modelTestBusy;
    refreshGlassSelect(msel);
  }
  const list=document.getElementById('model-test-models');
  if(list)list.innerHTML=models.map(m=>'<label style="display:flex;align-items:flex-start;gap:.5rem;min-width:0;padding:.5rem .6rem;border:1px solid var(--inner-border);border-radius:8px;background:var(--inner);cursor:pointer">'
    +'<input type="checkbox" value="'+esc(m.id)+'"'+(__modelTestSelected.has(m.id)?' checked':'')+(__modelTestBusy?' disabled':'')
    +' onchange="toggleModelTestSelection(this.value,this.checked)" style="flex:none;margin:.15rem 0 0;accent-color:#38bdf8">'
    +'<span style="min-width:0;overflow-wrap:anywhere;font-size:.8rem;color:var(--strong)">'+esc(m.label)+'</span></label>').join('');
  const p=document.getElementById('model-test-prompt');
  if(p){p.placeholder=t('mt_prompt_ph');p.disabled=__modelTestBusy;if(__modelTestRun)p.value=__modelTestRun.prompt}
  _mtControls();
}
function _mtCleanup(cleanup){
  const status=cleanup&&cleanup.status;
  const labels={deleted:'mt_cleanup_deleted',not_created:'mt_cleanup_not_created',unsupported:'mt_cleanup_unsupported',failed:'mt_cleanup_failed',skipped:'mt_cleanup_skipped'};
  const messages={creation_unconfirmed:'mt_cleanup_creation_unconfirmed',identity_changed:'mt_cleanup_identity_changed',delete_unsupported:'mt_cleanup_delete_unsupported',delete_timeout:'mt_cleanup_delete_timeout',delete_failed:'mt_cleanup_delete_failed'};
  const label=t(labels[status]||'mt_cleanup_unknown');
  const message=cleanup&&cleanup.message;
  const detail=message?(messages[message]?t(messages[message]):message):'';
  const color=status==='deleted'?'#22c55e':(['failed','unsupported','skipped'].includes(status)?'#f59e0b':'var(--faint)');
  return '<span style="color:'+color+'">'+esc(label)+'</span>'+(detail?'<div style="margin-top:.2rem;color:var(--faint);overflow-wrap:anywhere">'+esc(detail)+'</div>':'');
}
function renderModelTest(){
  renderModelTestOptions();
  const box=document.getElementById('model-test-result');
  if(!box)return;
  if(!__modelTest.length){box.innerHTML='<span style="color:var(--faint)">'+t('mt_none')+'</span>';return}
  let h='<table class="admin-tbl"><thead><tr style="color:var(--muted);text-align:left">'
    +'<th style="padding:.3rem">'+t('mt_col_model')+'</th><th style="padding:.3rem">'+t('mt_col_verdict')+'</th>'
    +'<th style="padding:.3rem">'+t('mt_col_latency')+'</th><th style="padding:.3rem">'+t('mt_col_cleanup')+'</th><th style="padding:.3rem">'+t('mt_col_detail')+'</th></tr></thead><tbody>';
  __modelTest.forEach(row=>{
    const detail=row.auth_required?t('mt_auth_required'):(row.error||row.reply||'');
    h+='<tr style="border-top:1px solid #334155">'
      +'<td style="padding:.4rem;font-size:.78rem">'+esc(row.model)+'</td>'
      +'<td style="padding:.4rem">'+_mtBadge(row.verdict||'error')+'</td>'
      +'<td style="padding:.4rem;font-size:.75rem;color:var(--faint);white-space:nowrap">'+(row.latency_ms?(row.latency_ms+' ms'):'-')+'</td>'
      +'<td style="padding:.4rem;font-size:.75rem;min-width:100px;max-width:260px">'+(row.verdict==='running'?'-':_mtCleanup(row.cleanup))+'</td>'
      +'<td style="padding:.4rem;font-size:.75rem;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(detail)+'">'+esc(detail||'-')+'</td></tr>';
  });
  h+='</tbody></table><div style="font-size:.72rem;color:var(--faint);margin-top:.5rem">'+t('mt_legend')+'</div>';
  box.innerHTML=h;
}
async function loadModelTest(){
  // The debug view does not load accounts on its own, and the selector needs them.
  if(!(__accounts||[]).length){try{await loadAccounts()}catch(e){}}
  renderModelTest();
}
async function _mtProbe(accountId,model,prompt){
  try{
    const r=await fetch('/admin/model-test',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({account_id:accountId,model:model,prompt:prompt||''})});
    if(r.status===401){showInlineLogin();return null}
    const d=await r.json().catch(()=>({}));
    if(!r.ok)return {model:model,verdict:'error',error:(d.error&&d.error.message)||('HTTP '+r.status),latency_ms:0,reply:'',cleanup:d.cleanup};
    return d;
  }catch(e){return {model:model,verdict:'error',error:t('network_error'),latency_ms:0,reply:''}}
}
async function runModelTest(mode){
  if(__modelTestBusy)return;
  const acct=_mtAccount();
  if(!acct){await adminAlert(t('mt_no_account'));return}
  const msel=document.getElementById('model-test-model');
  const pin=document.getElementById('model-test-prompt');
  const prompt=(pin&&pin.value)||'';
  const models=_mtModels(acct);
  const targets=mode===true?models.map(m=>m.id):(mode==='selected'
    ?models.filter(m=>__modelTestSelected.has(m.id)).map(m=>m.id):[msel&&msel.value].filter(Boolean));
  if(!targets.length){await adminAlert(t(mode==='selected'?'mt_no_selection':'mt_no_model'));return}
  const accountId=acct.id;
  __modelTestBusy=true;__modelTest=[];
  __modelTestRun={account:{...acct},models:models,singleModel:msel&&msel.value,prompt:prompt};
  __modelTestProgress={done:0,total:targets.length,stopped:false};
  try{
    for(const model of targets){
      // Sequential on purpose: each probe is a real upstream turn on ONE account.
      __modelTest.push({model:model,verdict:'running'});
      renderModelTest();
      const row=await _mtProbe(accountId,model,prompt);
      if(row===null){
        __modelTest[__modelTest.length-1]={model:model,verdict:'error',auth_required:true,cleanup:{status:'not_created'}};
        __modelTestProgress.stopped=true;
        break;
      }
      __modelTest[__modelTest.length-1]={...row,model:model};
      __modelTestProgress.done++;
      renderModelTest();
    }
  }finally{
    __modelTestBusy=false;
    __modelTestRun=null;
    renderModelTest();
  }
}
"""
