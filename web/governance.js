/* Shui! project governance workspace. Secrets are write-only. */
(() => {
  'use strict';
  let ctx, root, data = {}, selected = null, section = 'ideas', returnFocus, loading = false, poll, epoch = 0, members = [];
  const labels = { 'feedback.created':'添加反馈','idea.imported':'接入想法','resource.authority_created':'设置审批负责人','resource.cancelled':'取消申请','resource.escalated':'向上申请','resource.approver_set':'设置审批负责人','resource.approver_removed':'移除审批负责人','idea.created':'新建想法',complete:'已完成',stopped:'已停止','idea.revised':'修改想法','idea.feedback':'添加反馈','idea.queued':'提交 Agent 任务','resource.requested':'申请资源','resource.decided':'资源决定','resource.receipt':'使用回执','pool.created':'创建额度池','pool.capacity_added':'补充额度',error:'失败',cancelled:'已取消', draft:'草稿', pending:'待审批', submitted:'待审批', approved:'已批准', rejected:'未批准', cancelled:'已取消', active:'可用', running:'执行中', queued:'排队中', completed:'已完成', succeeded:'已完成', failed:'失败', gpu:'显卡', tokens:'Token', token:'Token', money:'资金', check:'初步校验', draft_assist:'起草润色', decision:'决策建议' };
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list = k => Array.isArray(data[k]) ? data[k] : [];
  const base = () => `/api/projects/${encodeURIComponent(ctx.projectId)}`;
  async function api(path, body, method) {
    const headers = {Accept:'application/json'};
    if (body !== undefined) { body={...body};for(const k of ['parent_id','approver_id','allocation_id','turn_id','credential_id'])if(body[k]==='')delete body[k];headers['Content-Type']='application/json'; headers['X-CSRF-Token']=ctx.csrf || ''; }
    const res = await fetch(base()+path, {method:method || (body === undefined ? 'GET':'POST'), headers, credentials:'same-origin', ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const result = await res.json().catch(()=>({}));
    if (!res.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message || result.message || `请求失败（${res.status}）`);
    return result;
  }
  function note(text, error=false) { if(!root)return;const n=root.querySelector('.sg-notice'); n.textContent=text; n.classList.toggle('sg-error',error); n.hidden=!text; }
  function button(text, action, id='', cls='') { if(!canWrite()&&!['select','session','verify'].includes(action))return '';if(['pool','capacity','credential','rotate-credential','revoke-credential','approver'].includes(action)&&!owner())return '';return `<button type="button" class="sg-btn ${cls}" data-action="${esc(action)}" data-id="${esc(id)}">${esc(text)}</button>`; }
  function badge(status) { return `<span class="sg-status">${esc(labels[status] || status || '待处理')}</span>`; }
  function field(name, label, type='text', value='', required=true, options) {
    const input = options ? `<select name="${esc(name)}" ${required?'required':''}>${options.map(o=>`<option value="${esc(o[0])}">${esc(o[1])}</option>`).join('')}</select>` : type==='textarea' ? `<textarea name="${esc(name)}" rows="5" ${required?'required':''}>${esc(value)}</textarea>` : `<input name="${esc(name)}" type="${esc(type)}" value="${esc(value)}" ${required?'required':''} ${type==='number'?'min="0.000001" step="any"':''} ${type==='password'?'autocomplete="new-password"':''}>`;
    return `<label>${esc(label)}${input}</label>`;
  }
  function owner() { return ctx.role==='owner' || ['owner','admin'].includes(ctx.user?.role) || data.role==='owner' || data.can_manage; }
  function canWrite(){return owner() || ctx.role==='editor';}
  function person(id){const user=members.find(x=>String(x.id)===String(id)) || list('approvers').find(x=>String(x.id)===String(id));return user?.display_name || user?.name || user?.username || (String(id)===String(ctx.user?.id)?ctx.user.display_name || ctx.user.username:'成员');}
  function canApprove(r){return owner() || (canWrite() && String(r.approver_id)===String(ctx.user?.id));}
  function upstream(r){const current=list('approvers').find(a=>String(a.id)===String(r.approver_id));return list('approvers').filter(a=>String(a.id)!==String(r.approver_id)&&(current?.parent_id?String(a.id)===String(current.parent_id):a.is_owner));}
  function canOwnRequest(r){return canWrite() && (owner() || String(r.author_id)===String(ctx.user?.id));}
  async function refresh() {
    if (loading) return; loading=true;
    const requestEpoch=epoch;try {const next=await api('/governance');if(requestEpoch!==epoch||!root)return;data=next;render();}
    catch(e) { note(e.message,true); }
    finally {if(requestEpoch===epoch)loading=false;}
  }
  function ideasView() {
    const ideas=list('ideas');
    const idea=ideas.find(i=>String(i.id)===String(selected));
    return `<div class="sg-ideas"><aside class="sg-list"><div class="sg-toolbar"><h2>想法</h2>${button('新想法','new-idea','','sg-primary')}</div>${ideas.length ? ideas.map(i=>`<button class="sg-idea ${String(i.id)===String(selected)?'selected':''}" data-action="select" data-id="${esc(i.id)}"><strong>${esc(i.title)}</strong>${badge(i.status)}</button>`).join('') : '<p class="sg-empty">还没有想法</p>'}</aside><main class="sg-detail">${idea ? detail(idea) : '<div class="sg-empty">选择一个想法，或直接新建。</div>'}</main></div>`;
  }
  function related(key, idea) { return list(key).filter(x=>String(x.idea_id)===String(idea.id)); }
  function detail(idea) {
    const revisions=related('revisions',idea), revision=revisions[revisions.length-1];
    const feedback=related('feedback',idea), requests=related('resource_requests',idea), executions=related('executions',idea);
    return `<div class="sg-detail-head"><h2>${esc(idea.title)}</h2>${button('修改','revise',idea.id)}</div><div class="sg-body">${esc(idea.body || revision?.body || '')}</div><div class="sg-actions">${button('申请资源','request',idea.id,'sg-primary')}${button('添加反馈','feedback',idea.id)}${list('allocations').some(a=>a.status==='active'&&Number(a.remaining)>0&&requests.some(r=>String(r.id)===String(a.request_id)&&canOwnRequest(r)))?button('开始执行','execute',idea.id):''}</div><div class="sg-actions sg-agent">${button('Agent 校验','assist-check',idea.id)}${button('Agent 润色','assist-draft',idea.id)}${button('决策建议','assist-decision',idea.id)}</div><section class="sg-block"><h3>资源申请</h3>${requests.length?requests.map(requestCard).join(''):'<p class="sg-empty">暂无申请</p>'}</section><section class="sg-block"><h3>执行与成果</h3>${executions.length?executions.map(x=>`<article class="sg-card"><div>${badge(x.status)} ${esc(x.content || x.summary || labels[x.kind?.replace('assist:','')] || x.kind || '')}</div>${button('打开会话','session',x.turn_id || '')}<div class="sg-body">${esc(x.result || '')}</div>${esc(x.error || '')}</article>`).join(''):'<p class="sg-empty">暂无执行</p>'}</section><section class="sg-block"><h3>反馈</h3>${feedback.length?feedback.map(x=>`<article class="sg-card"><div class="sg-meta">${esc(x.author_name || person(x.author_id || x.created_by))} ${date(x.created_at)}${x.reply_to?` · 回复 ${esc((feedback.find(f=>String(f.id)===String(x.reply_to))?.content || '反馈').slice(0,30))}`:''}</div><div class="sg-body">${esc(x.content || x.body)}</div>${button('回复','reply',x.id)}</article>`).join(''):'<p class="sg-empty">暂无反馈</p>'}</section><details class="sg-block"><summary>版本记录（${revisions.length}）</summary>${revisions.slice().reverse().map(x=>`<article class="sg-card"><h3>${esc(x.title)}</h3><p class="sg-meta">${date(x.created_at)}</p><div class="sg-body">${esc(x.body)}</div></article>`).join('')}</details>`;
  }
  function date(v) { if(!v)return '';const d=new Date(typeof v==='number'&&v<1e12?v*1000:v);return isNaN(d)?'':d.toLocaleString('zh-CN'); }
  function requestCard(r) {
    const allocations=list('allocations').filter(a=>String(a.request_id)===String(r.id));
    return `<article class="sg-card"><div class="sg-card-head"><strong>${esc(labels[r.kind] || r.kind)} · ${esc(r.amount)} ${esc(r.unit)}</strong>${badge(r.status)}</div><div class="sg-body">${esc(r.reason || '')}</div>${r.note?`<div class="sg-body">决定：${esc(r.note)}</div>`:''}${r.parent_id?`<p class="sg-meta">上级申请 ${esc((list('resource_requests').find(p=>String(p.id)===String(r.parent_id))?.reason || '资源申请').slice(0,60))}</p>`:''}<div class="sg-actions">${['pending','submitted'].includes(r.status)?`${canApprove(r)?button('批准','approve',r.id)+button('拒绝','reject',r.id):''}${canOwnRequest(r)?button('取消','cancel',r.id):''}${!r.parent_id&&(canOwnRequest(r)||canApprove(r))&&upstream(r).length?button('向上申请','escalate',r.id):''}`:''}${r.status==='approved'&&canOwnRequest(r)?button('释放未用额度','cancel',r.id):''}${allocations.map(a=>`${canOwnRequest(r)&&a.status==='active'&&Number(a.remaining)>0?button('提交使用回执','receipt',a.id):''}<span>已批 ${esc(a.amount)} · 已用 ${esc(a.used_amount ?? a.consumed ?? 0)} · 剩余 ${esc(a.remaining ?? '')}</span>`).join('')}</div>${list('receipts').filter(x=>allocations.some(a=>String(a.id)===String(x.allocation_id))).map(x=>`<div class="sg-receipt"><strong>使用 ${esc(x.amount)} ${esc(r.unit)}</strong><div class="sg-body">${esc(x.evidence)}</div><span class="sg-meta">用户提交 · ${date(x.created_at)}</span></div>`).join('')}</article>`;
  }
  function resourcesView() {
    return `<div class="sg-content"><div class="sg-toolbar"><h2>资源</h2><div class="sg-actions">${button('审批负责人','approver')}${button('设置额度池','pool','','sg-primary')}</div></div><div class="sg-pools">${list('pools').map(p=>`<article class="sg-card"><h3>${esc(labels[p.kind] || p.kind)}</h3><p>${esc(p.capacity)} ${esc(p.unit)} · 已预留 ${esc(p.reserved ?? p.reserved_amount ?? 0)} · 已用 ${esc(p.consumed || 0)}</p>${button('补充额度','capacity',p.id)}</article>`).join('') || '<p class="sg-empty">尚未设置额度池</p>'}</div>${list('approvers').length?`<section class="sg-block"><h3>审批负责人</h3>${list('approvers').map(a=>`<div class="sg-card">${esc(a.name||a.username)}${a.parent_id?` → ${esc(person(a.parent_id))}`:''}</div>`).join('')}</section>`:''}<h3>全部申请</h3>${list('resource_requests').map(requestCard).join('') || '<p class="sg-empty">暂无申请</p>'}</div>`;
  }
  function ledgerView() {
    const verification=data.ledger || data.verification || data.ledger_verification || {};
    return `<div class="sg-content"><div class="sg-toolbar"><h2>记录</h2>${button('核验记录','verify')}</div><div class="sg-ledger-status">${verification.valid===true || verification.ok===true?'记录校验通过':verification.valid===false || verification.ok===false?'记录校验失败':'尚未校验'}</div>${list('events').slice().reverse().map(e=>`<article class="sg-card"><div class="sg-card-head"><strong>${esc(labels[e.event_type || e.kind || e.type] || e.event_type || e.kind || e.type)}</strong><span>${date(e.at || e.created_at)}</span></div><div>${esc(e.actor_name || person(e.actor_id))}</div><details><summary>记录指纹</summary><code>${esc(e.hash || e.event_hash || '')}</code></details></article>`).join('') || '<p class="sg-empty">暂无记录</p>'}</div>`;
  }
  function render() {
    if(!root)return;
    root.querySelectorAll('[data-section]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.section===section)));
    const main=root.querySelector('.sg-main');
    main.innerHTML=section==='ideas'?ideasView():section==='resources'?resourcesView():section==='ledger'?ledgerView():`<div class="sg-content"><div class="sg-toolbar"><h2>凭据</h2>${button('保存凭据','credential','','sg-primary')}</div><div class="sg-vault-list">正在读取…</div></div>`;
    if(section==='vault')loadVault();
  }
  function formDialog(title,fields,onSubmit) {
    if(root.querySelector('.sg-form-layer'))return;
    const layer=document.createElement('div');layer.className='sg-form-layer';
    layer.innerHTML=`<form class="sg-form" role="dialog" aria-modal="true" aria-label="${esc(title)}"><h2>${esc(title)}</h2>${fields}<p class="sg-form-error" role="alert"></p><div class="sg-actions"><button class="sg-btn sg-primary" type="submit">保存</button><button class="sg-btn" type="button" data-dismiss>取消</button></div></form>`;
    root.append(layer); const form=layer.querySelector('form'); const old=document.activeElement;
    const kind=form.elements.kind, unit=form.elements.unit;if(kind&&unit){kind.addEventListener('change',()=>{unit.value=kind.value==='gpu'?'GPU·小时':kind.value==='tokens'?'Token':kind.value==='money'?'元':'份';});}
    const dismiss=()=>{layer.remove();old?.focus();};
    layer.querySelector('[data-dismiss]').onclick=dismiss;
    form.onsubmit=async e=>{e.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;try{await onSubmit(Object.fromEntries(new FormData(form)));dismiss();await refresh();}catch(error){layer.querySelector('.sg-form-error').textContent=error.message;submit.disabled=false;}};
    layer.querySelector('input,textarea,select')?.focus();
  }
  async function act(action,id) {
    const idea=list('ideas').find(x=>String(x.id)===String(id));
    if(action==='select'){selected=id;render();return;}
    if(action==='new-idea')return formDialog('新想法',field('title','标题')+field('body','想法','textarea')+field('parent_id','关联想法','text','',false,[['','独立想法'],...list('ideas').map(i=>[i.id,i.title])]),async x=>{const r=await api('/ideas',x);selected=r.idea?.id || r.id;});
    if(action==='revise')return formDialog('修改想法',field('title','标题','text',idea.title)+field('body','想法','textarea',idea.body || ''),x=>api(`/ideas/${id}/revisions`,x));
    if(action==='feedback'||action==='reply')return formDialog(action==='reply'?'回复反馈':'添加反馈',field('content','内容','textarea'),x=>api(`/ideas/${action==='reply'?selected:id}/feedback`,{...x,...(action==='reply'?{reply_to:id}:{})}));
    if(action==='escalate')return formDialog('向上申请',field('approver_id','上级审批人','text','',true,upstream(list('resource_requests').find(r=>String(r.id)===String(id))).map(a=>[a.id,a.name || a.username]))+field('reason','资源缺口与依据','textarea'),x=>api(`/resource-requests/${id}/escalate`,x));
    if(action==='request'){
      const parent=list('resource_requests').find(x=>String(x.id)===String(id));
      return formDialog('申请资源',field('kind','资源','text','',true,[['gpu','显卡'],['tokens','Token'],['money','资金'],['other','其他']])+field('amount','数量','number')+field('unit','单位','text','GPU·小时')+field('reason','用途与验收条件','textarea')+field('approver_id','审批人','text','',false,[['','项目负责人'],...list('approvers').map(a=>[a.id,a.name||a.username])])+(action==='request'?field('parent_id','上级申请','text','',false,[['','不关联'],...list('resource_requests').map(r=>[r.id,`${labels[r.kind]||r.kind} ${r.amount} ${r.unit} · ${(r.reason||'').slice(0,32)}`])]):''),x=>api('/resource-requests',{...x,amount:x.amount,idea_id:action==='escalate'?parent.idea_id:id,...(action==='escalate'?{parent_id:id}:{})}));
    }
    if(action==='approve'||action==='reject'){
      const r=list('resource_requests').find(x=>String(x.id)===String(id));
      return formDialog(action==='approve'?'批准资源':'拒绝申请',(action==='approve'?field('amount','批准数量','number',r.amount):'')+field('note','决定与依据','textarea','',false),x=>api(`/resource-requests/${id}/decision`,{...x,decision:action==='approve'?'approve':'reject',...(x.amount?{amount:x.amount}:{})}));
    }
    if(action==='cancel'){await api(`/resource-requests/${id}/cancel`,{});await refresh();return;}
    if(action==='capacity')return formDialog('补充额度',field('amount','增加数量','number'),x=>api(`/resource-pools/${id}/capacity`,{amount:x.amount}));
    if(action==='approver')return formDialog('审批负责人',field('user_id','成员','text','',true,members.filter(m=>m.role==='editor'&&!list('approvers').some(a=>String(a.id)===String(m.id))).map(m=>[m.id,m.display_name||m.username]))+field('parent_id','上级审批人','text','',false,[['','项目负责人'],...list('approvers').map(a=>[a.id,a.name||a.username])]),x=>api('/resource-approvers',x));
    if(action==='pool'){const vault=await vaultRequest('/credentials');const credentials=(Array.isArray(vault)?vault:vault.credentials||[]).filter(c=>c.status==='active');return formDialog('设置额度池',field('kind','资源','text','',true,[['gpu','显卡'],['tokens','Token'],['money','资金'],['other','其他']])+field('unit','单位')+field('capacity','总额度','number')+field('credential_id','使用凭据','text','',false,[['','不绑定'],...credentials.map(c=>[c.id,`${c.name} · ${c.provider||c.category||''}`])]),x=>api('/resource-pools',{...x,capacity:x.capacity}));}
    if(action==='receipt')return formDialog('使用回执',field('amount','实际用量','number')+field('evidence','成果或证据','textarea')+field('turn_id','关联执行 ID','text','',false),x=>api(`/allocations/${id}/receipts`,{...x,amount:x.amount}));
    if(action==='execute')return formDialog('开始执行',field('content','交给 Agent 的任务','textarea',idea?.body || '')+field('allocation_id','使用额度','text','',false,list('allocations').filter(a=>list('resource_requests').some(r=>String(r.id)===String(a.request_id)&&String(r.idea_id)===String(id)&&canOwnRequest(r))&&a.status==='active'&&Number(a.remaining)>0).map(a=>[a.id,`${a.amount} ${a.unit||''}`])),async x=>{const r=await api(`/ideas/${id}/execute`,x);note('任务已提交');sessionLink(r.turn?.id || r.execution?.turn_id || '');});
    if(action.startsWith('assist-')){note('正在提交 Agent 任务…');const r=await api(`/ideas/${id}/assist`,{kind:action.slice(7)});note('Agent 任务已提交');sessionLink(r.turn?.id || r.execution?.turn_id || '');await refresh();return;}
    if(action==='session'){ctx.onOpenSession?.(id);close();return;}
    if(action==='verify'){const v=(await api('/governance')).ledger;note(v.valid || v.ok?'记录校验通过':'记录校验失败',!(v.valid || v.ok));return;}
    if(action==='credential')return credentialForm();
    if(action==='rotate-credential')return formDialog('更新凭据',field('secret','新凭据','password'),async x=>{await vaultRequest(`/credentials/${encodeURIComponent(id)}/rotate`,x);delete x.secret;});
    if(action==='revoke-credential'){await vaultRequest(`/credentials/${encodeURIComponent(id)}/revoke`,{});await loadVault();}
  }
  function sessionLink(id){if(!root)return;const n=root.querySelector('.sg-notice');n.append(' ',Object.assign(document.createElement('button'),{textContent:'打开会话',className:'sg-btn',onclick:()=>{ctx.onOpenSession?.(id);close();}}));}
  async function vaultRequest(path,body,method){ return api(path,body,method); }
  async function loadVault(){try{const r=await vaultRequest('/credentials');const rows=Array.isArray(r)?r:r.credentials || r.items || [];const area=root?.querySelector('.sg-vault-list');if(!area)return;area.innerHTML=rows.length?rows.map(c=>`<article class="sg-card"><div class="sg-card-head"><h3>${esc(c.name || c.label || c.provider)}</h3>${badge(c.status)}</div><p>${esc(c.provider || c.category || '')} ${esc(c.masked || c.masked_secret || (c.secret_saved?'凭据已保存':''))}</p><p>${esc(c.endpoint || c.host || '')}</p>${c.status!=='revoked'?button('更新','rotate-credential',c.id)+button('撤销','revoke-credential',c.id):''}</article>`).join(''):'<p class="sg-empty">尚未保存凭据</p>';}catch(e){const area=root?.querySelector('.sg-vault-list');if(area)area.textContent=e.message;}}
  function credentialForm(){return formDialog('保存凭据',field('name','名称')+field('secret','凭据','password')+field('endpoint','服务地址','url','',false)+field('purpose','用途','textarea','',false),async x=>{await vaultRequest('/credentials',x);delete x.secret;});}
  function keydown(e){if(!root)return; if(e.key==='Escape'){const layer=root.querySelector('.sg-form-layer');if(layer)layer.remove();else close();}if(e.key==='Tab'){const scope=root.querySelector('.sg-form')||root;const controls=[...scope.querySelectorAll('button,input,textarea,select,a')].filter(x=>!x.disabled&&x.getClientRects().length);if(!controls.length)return;const first=controls[0],last=controls[controls.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}}
  async function open(options){if(root)close();epoch++;loading=false;ctx=options;members=options.members||[];selected=null;section='ideas';data={};returnFocus=document.activeElement;root=document.createElement('section');root.className='shui-governance';root.setAttribute('role','dialog');root.setAttribute('aria-modal','true');root.setAttribute('aria-label','Shui! 想法与资源');root.innerHTML=`<header class="sg-header"><strong>Shui<span>!</span></strong><nav aria-label="工作区">${[['ideas','想法'],['resources','资源'],['vault','凭据'],['ledger','记录']].map(([id,text])=>`<button class="sg-btn" data-section="${id}" aria-pressed="${id==='ideas'}">${text}</button>`).join('')}</nav><button class="sg-btn" data-close aria-label="返回会话">返回会话</button></header><div class="sg-notice" role="status" hidden></div><div class="sg-main"><p class="sg-empty">正在读取…</p></div>`;document.body.append(root);document.body.classList.add('sg-open');root.onclick=async e=>{const b=e.target.closest('button');if(!b)return;if(b.hasAttribute('data-close'))return close();if(b.dataset.section){section=b.dataset.section;render();return;}if(b.dataset.action){b.disabled=true;try{await act(b.dataset.action,b.dataset.id);}catch(error){note(error.message,true);}finally{b.disabled=false;}}};document.addEventListener('keydown',keydown);root.querySelector('button')?.focus();try{const r=await api('/members');members=r.members||[];}catch{}await refresh();poll=setInterval(()=>{if(root&&!root.querySelector('.sg-form-layer')&&section!=='vault')refresh();},8000);}
  function close(){epoch++;loading=false;clearInterval(poll);document.removeEventListener('keydown',keydown);root?.remove();root=null;document.body.classList.remove('sg-open');returnFocus?.focus();}
  window.ShuiGovernance={open,close,refresh};
})();
