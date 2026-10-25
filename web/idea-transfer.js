/* Explicit, confirmed import. No credentials or session data cross windows. */
(() => {
 'use strict';
 const ORIGIN='https://circuidea.pages.dev', source=window.opener, maxBytes=90000;
 let session=null,pending=null,dialog=null,activeId='',completed=new Set();
 function reply(type,requestId){if(source)source.postMessage({type,requestId},ORIGIN);}
 function validate(value){
  if(!value||!Array.isArray(value.ideas)||value.ideas.length!==1)return null;
  const idea=value.ideas[0];
  if(!idea||typeof idea.id!=='string'||!idea.id.startsWith('circuidea:')||idea.id.length>200||typeof idea.title!=='string'||!idea.title.trim()||idea.title.length>160||typeof idea.body!=='string'||idea.body.length>50000)return null;
  if(new TextEncoder().encode(JSON.stringify(value)).length>maxBytes)return null;
  for(const key of ['reviews','resources','events','tags'])if(!Array.isArray(idea[key])||idea[key].length>2000)return null;
  return JSON.parse(JSON.stringify(value));
 }
 function element(tag,text){const n=document.createElement(tag);if(text)n.textContent=text;return n;}
 function show(){
  if(!pending||!session?.user||session.user.must_change_password||dialog)return;
  const request=pending;dialog=element('dialog');dialog.setAttribute('aria-label','接收想法');dialog.style.cssText='max-width:540px;width:calc(100% - 32px);box-sizing:border-box;padding:24px;border:1px solid #ccc;border-radius:12px';
  const form=element('form');form.append(element('h2','送到 Shui!'),element('p',request.payload.ideas[0].title));
  const body=element('p',request.payload.ideas[0].body.slice(0,1200));body.style.cssText='white-space:pre-wrap;max-height:180px;overflow:auto';form.append(body);
  const label=element('label','项目'),select=element('select');select.required=true;select.style.cssText='display:block;width:100%;margin:8px 0 20px;padding:8px';
  for(const project of session.projects||[]){const o=element('option',project.name);o.value=String(project.id);select.append(o);}label.append(select);form.append(label);
  const status=element('p');status.setAttribute('role','status');form.append(status);
  const cancel=element('button','取消');cancel.type='button';const submit=element('button','保存想法');submit.type='submit';submit.disabled=!select.options.length;form.append(cancel,submit);
  const close=()=>{dialog.close();dialog.remove();dialog=null;pending=null;activeId='';};
  cancel.onclick=()=>{reply('shui-idea-cancelled',request.requestId);close();};
  dialog.addEventListener('cancel',event=>{event.preventDefault();cancel.click();});
  form.onsubmit=async event=>{
   event.preventDefault();if(!select.value||submit.disabled)return;submit.disabled=true;cancel.disabled=true;status.textContent='正在保存…';
   try{
    const response=await fetch(`/api/projects/${encodeURIComponent(select.value)}/ideas/import`,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf||''},body:JSON.stringify(request.payload)});
    if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{}throw Error(typeof detail==='string'?detail:'未能保存，请重试。');}
    completed.add(request.requestId);reply('shui-idea-imported',request.requestId);const projectId=select.value;close();session.onSelectProject?.(projectId);
   }catch(error){status.textContent=error.message;submit.disabled=false;cancel.disabled=false;}
  };
  dialog.append(form);document.body.append(dialog);dialog.showModal();
 }
 window.addEventListener('message',event=>{
  if(!source||event.origin!==ORIGIN||event.source!==source||typeof event.data?.requestId!=='string'||event.data.requestId.length>100)return;
  const {type,requestId}=event.data;
  if(type==='shui-idea-hello'){
   if(completed.has(requestId)){reply('shui-idea-imported',requestId);return;}
   if(activeId&&activeId!==requestId)return;activeId=requestId;reply('shui-idea-ready',requestId);
  } else if(type==='shui-idea-payload'&&requestId===activeId&&!pending&&!completed.has(requestId)){
   const payload=validate(event.data.payload);if(payload){pending={requestId,payload};show();}
  }
 });
 window.ShuiIdeaTransfer={onSession(value){session=value;show();}};
})();
