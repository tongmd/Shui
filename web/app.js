/* Shui! — a shared research notebook. No dependencies, no remote assets. */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  function icon(name) {
    const node = window.SHUI_LUCIDE[name] || window.SHUI_LUCIDE.paper;
    function build([tag, attributes = {}, children = []]) {
      const element = document.createElementNS(NS, tag);
      Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
      children.forEach(child => element.append(build(child)));
      return element;
    }
    const svg = build(node);
    svg.setAttribute('aria-hidden', 'true');
    return svg;
  }
  document.querySelectorAll('[data-icon]').forEach(e => e.append(icon(e.dataset.icon)));
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = String(text); return e; }
  function button(label, cls, action, glyph) { const b = el('button', cls, label); b.type = 'button'; if (glyph) b.prepend(icon(glyph)); if (action) b.addEventListener('click', action); return b; }
  function iconButton(label, glyph, action) { const b = button('', 'icon-btn', action, glyph); b.title = label; b.setAttribute('aria-label', label); return b; }
  const state = { user: null, csrf: null, providers: [], projects: [], project: null, messages: new Map(), turns: new Map(), tools: new Map(), files: [], members: [], presence: [], activity: [], cursor: 0, source: null, reconnectTimer: null, reconnectDelay: 1200, heartbeat: null, metaTimer: null, epoch: 0, tab: 'files', workspacePath: '', attachments: [], uploading: 0, sending: false, pendingSend: null, historyMore: true, loadingHistory: false, dialogAction: null, projectLoading: false, liveConnected: false };
  const labels = { idle: '准备就绪', running: '正在推进', queued: '排队中', waiting: '等待处理', complete: '已完成', failed: '任务失败', error: '需要关注', cancelled: '已取消', interrupted: '已中断' };
  const providerNames = { codex: 'Codex', claude: 'Claude', bridge: 'Bridge' };
  const roles = { owner: '管理员', editor: '协作者', viewer: '只读成员', member: '团队成员' };
  const activeStatuses = new Set(['queued', 'running', 'waiting']);
  const terminalStatuses = new Set(['complete', 'failed', 'cancelled', 'interrupted']);
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = crypto.getRandomValues(new Uint8Array(1))[0] & 15; return (c === 'x' ? r : (r & 3) | 8).toString(16); });
  function stored(key, value) { try { if (value === undefined) return localStorage.getItem(key); if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_) {} return null; }
  function toast(message, kind = '') { const n = el('div', `toast ${kind}`, message); $('toasts').append(n); setTimeout(() => n.remove(), kind === 'error' ? 6500 : 4000); }
  function dateValue(value) { if (!value) return null; const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
  function time(value) { const d = dateValue(value); return d ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''; }
  function when(value) { const d = dateValue(value); if (!d) return ''; const now = new Date(); return d.toDateString() === now.toDateString() ? `今天 ${time(value)}` : `${d.getMonth() + 1}月${d.getDate()}日 ${time(value)}`; }
  function bytes(n) { n = Number(n) || 0; if (n < 1024) return `${n} B`; if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`; return `${(n / 1048576).toFixed(1)} MB`; }
  function name(user) { return user?.display_name || user?.username || '团队成员'; }
  function avatar(user, cls = '') { const n = name(user); const a = el('span', `avatar tone-${[...n].reduce((v, c) => v + c.charCodeAt(0), 0) % 5} ${cls}`, /[\u3400-\u9fff]/.test(n) ? n.slice(-2) : n.slice(0, 2).toUpperCase()); a.title = n; return a; }
  function isOwner() { return state.user?.role === 'owner' || state.project?.role === 'owner'; }
  function canWrite() { return !!state.project && (isOwner() || state.project.role === 'editor'); }
  function projectAPI(suffix = '', pid = state.project?.id) { return `/projects/${encodeURIComponent(pid)}${suffix}`; }
  async function api(path, options = {}) {
    const method = options.method || 'GET'; const headers = new Headers(options.headers || {});
    if (!['GET', 'HEAD'].includes(method) && state.csrf) headers.set('X-CSRF-Token', state.csrf);
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
    let response;
    try { response = await fetch(`/api${path}`, { ...options, method, body, headers, credentials: 'same-origin' }); }
    catch (_) { throw new Error('连接暂时中断，请检查网络后重试。'); }
    let data; try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      if (response.status === 401 && path !== '/login' && path !== '/session') { showLogin(); throw new Error('登录已过期，请重新登录。'); }
      if (response.status === 403 && (data?.code === 'password_change_required' || data?.detail?.code === 'password_change_required')) { if (state.user) state.user.must_change_password = true; showPasswordChange(); throw new Error('请先修改初始密码，再进入工作空间。'); }
      const detail = data?.detail; const message = typeof detail === 'string' ? detail : detail?.message;
      const translated = { 'Current password is incorrect': '当前密码不正确，请重新输入。', 'New passwords do not match': '两次输入的新密码不一致。', 'New password must be 8–256 characters': '新密码需要 8–256 位字符。', 'Choose a new password different from your username and current password': '新密码不能与用户名或当前密码相同。', 'Display name is required': '请输入显示名称。', 'Stop the running task before deleting this project': '请先停止正在运行的任务，再删除项目。' };
      const error = new Error(translated[message] || message || `请求未完成（${response.status}），请重试。`); error.status = response.status; throw error;
    }
    return data;
  }
  function setConnection(mode, text) { state.liveConnected = mode === 'connected'; $('connection').className = `connection ${mode}`; $('connection-text').textContent = text; }
  function stopEvents() { if (state.source) state.source.close(); state.source = null; clearTimeout(state.pollTimer); state.pollTimer = null; clearTimeout(state.reconnectTimer); state.reconnectTimer = null; clearTimeout(state.metaTimer); state.metaTimer = null; clearInterval(state.heartbeat); state.heartbeat = null; }
  function clearProjectView() {
    stopEvents(); state.epoch++; state.project = null; state.projectLoading = false; state.messages.clear(); state.turns.clear(); state.tools.clear(); state.files = []; state.members = []; state.presence = []; state.activity = []; state.attachments = []; state.uploading = 0; state.sending = false; state.pendingSend = null; state.workspacePath = ''; state.cursor = 0; state.historyMore = true; state.loadingHistory = false;
    ['messages', 'shared-files', 'workspace-files', 'workspace-path', 'member-list', 'activity-list', 'presence', 'project-links'].forEach(id => $(id).replaceChildren());
    ['project-title', 'project-description', 'project-status', 'project-model', 'queue-label', 'preview-title'].forEach(id => $(id).textContent = '');
    $('message-input').value = ''; $('message-input').style.height = ''; $('file-input').value = ''; $('queue-banner').hidden = true; $('load-history').hidden = true; $('jump-latest').hidden = true; $('composer').classList.remove('drag-over');
    $('pdf-preview').removeAttribute('src'); $('preview-download').removeAttribute('href'); $('preview-download').removeAttribute('download');
    state.dialogAction = null; state.dialogCanSubmit = null; state.dialogBusy = false;
    if ($('form-dialog').open) $('form-dialog').close(); if ($('preview-dialog').open) $('preview-dialog').close(); closeDrawers(); renderUploads(); updateComposer();
  }
  function closeWorkspace() { clearProjectView(); state.projects = []; }
  function showLogin() { closeWorkspace(); state.user = null; state.csrf = null; state.providers = []; $('boot').hidden = true; $('app').hidden = true; $('password-view').hidden = true; $('login-view').hidden = false; $('login-password').value = ''; $('password-form').reset(); }
  function showPasswordChange() {
    const entering = $('password-view').hidden; closeWorkspace(); state.providers = [];
    $('boot').hidden = true; $('app').hidden = true; $('login-view').hidden = true; $('password-view').hidden = false;
    if (entering) { $('password-form').reset(); $('password-error').hidden = true; }
    $('password-account-name').textContent = state.user?.username || ''; $('password-username').value = state.user?.username || '';
    if (entering) $('password-current').focus();
  }
  async function startSession(session) {
    state.user = session.user; state.csrf = session.csrf; state.providers = session.providers || [];
    if (state.user.must_change_password) { showPasswordChange(); return; }
    $('password-view').hidden = true; $('password-form').reset(); $('boot').hidden = true; $('login-view').hidden = true; $('app').hidden = false; $('user-name').textContent = name(state.user); $('user-role').textContent = roles[state.user.role] || '团队成员';
    const a = avatar(state.user); a.id = 'user-avatar'; $('user-avatar').replaceWith(a); $('create-project').hidden = state.user.role !== 'owner'; $('welcome-create').hidden = state.user.role !== 'owner'; $('manage-users').hidden = state.user.role !== 'owner';
    setConnection('connected', '已登录'); await loadProjects();
  }
  async function loadProjects(preferId) {
    if (!state.user || state.user.must_change_password) return;
    $('project-list').replaceChildren(el('div', 'skeleton'), el('div', 'skeleton'));
    try { const data = await api('/projects'); state.projects = data.projects || []; renderProjects(); const id = preferId || stored('shui-project'); const next = state.projects.find(p => String(p.id) === String(id)) || state.projects[0]; if (next) await selectProject(next.id); else showWelcome(); }
    catch (err) { $('project-list').replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', () => loadProjects(), 'refresh')); setConnection('', '加载失败'); }
  }
  function renderProjects() {
    const query = $('project-search').value.trim().toLowerCase(); const list = state.projects.filter(p => `${p.name} ${p.description || ''}`.toLowerCase().includes(query)); $('project-list').replaceChildren();
    if (!list.length) { $('project-list').append(el('p', 'empty-small', query ? '没有找到这个项目。' : '还没有项目，下一篇研究从这里开始。')); return; }
    list.forEach(p => { const b = button('', `project-item${state.project?.id === p.id ? ' active' : ''}`, () => selectProject(p.id)); b.setAttribute('aria-current', state.project?.id === p.id ? 'page' : 'false'); const s = el('span', 'project-symbol'); s.append(icon('paper')); const t = el('span', 'project-item-text'); t.append(el('strong', '', p.name), el('small', '', `${providerNames[p.provider] || p.provider} · ${labels[p.status] || '准备就绪'}`)); b.append(s, t); if (p.status === 'running') b.append(el('span', 'project-running-dot')); $('project-list').append(b); });
  }
  function showWelcome() { clearProjectView(); stored('shui-project', null); $('welcome').hidden = false; $('project-view').hidden = true; $('breadcrumb-project').textContent = '概览'; $('inspector-empty').hidden = false; ['files', 'activity', 'members'].forEach(t => $(`panel-${t}`).hidden = true); $('project-error').hidden = true; $('conversation-empty').hidden = true; renderProjects(); setConnection('connected', '已登录'); }
  async function projectUnavailable(id, message = '项目已删除，或你已没有访问权限。') {
    if (String(state.project?.id) !== String(id)) return;
    state.projects = state.projects.filter(project => String(project.id) !== String(id)); showWelcome(); if (message) toast(message); await loadProjects();
  }
  async function selectProject(id) {
    if (!state.user || state.user.must_change_password) return;
    if (String(state.project?.id) === String(id) && !state.projectLoading) { closeDrawers(); return; }
    const epoch = ++state.epoch; stopEvents(); closeDrawers(); state.projectLoading = true; state.messages.clear(); state.turns.clear(); state.tools.clear(); state.presence = []; state.activity = []; state.attachments = []; state.uploading = 0; state.pendingSend = null; state.sending = false; state.workspacePath = ''; state.cursor = 0; state.historyMore = true;
    state.project = state.projects.find(p => String(p.id) === String(id)) || { id, name: '正在加载…' }; $('message-input').value = ''; $('message-input').style.height = ''; renderUploads(); renderProjects(); renderHeader();
    $('welcome').hidden = true; $('project-view').hidden = false; $('project-error').hidden = true; $('inspector-empty').hidden = true; $('messages').replaceChildren(el('div', 'loading-message', '正在打开共同的讨论…')); $('conversation-empty').hidden = true; $('load-history').hidden = true; $('shared-files').replaceChildren(el('div', 'skeleton')); $('workspace-files').replaceChildren(el('div', 'skeleton')); setConnection('reconnecting', '正在连接'); updateComposer();
    try {
      const d = await api(projectAPI('', id)); if (epoch !== state.epoch) return;
      state.project = d.project; state.files = d.files || []; state.members = d.members || []; state.cursor = Number(d.events_cursor) || 0;
      (d.messages || []).forEach(m => {
        state.messages.set(String(m.id), m);
        if (m.role === 'user' && m.turn_id) state.turns.set(String(m.turn_id), { id: m.turn_id, author_id: m.author?.id, status: m.status });
      });
      if (d.project.active_turn) state.turns.set(String(d.project.active_turn.id), d.project.active_turn);
      state.projectLoading = false; state.historyMore = (d.messages || []).length >= 100; stored('shui-project', id); updateProjectInList(d.project); renderHeader(); renderMessages(); renderFiles(); renderMembers(); renderTab(); updateComposer(); scrollBottom();
      connectEvents(epoch); state.lastStreamEvent = 0; state.lastMetaPoll = 0; pollEvents(epoch); heartbeat(epoch); state.heartbeat = setInterval(() => heartbeat(epoch), 25000); loadWorkspace(''); loadActivity();
    } catch (err) { if (epoch !== state.epoch) return; if (err.status === 404 || err.status === 403) { await projectUnavailable(id); return; } state.projectLoading = false; $('messages').replaceChildren(); $('project-error').replaceChildren(el('span', '', err.message), button('重试', 'quiet-btn', () => { state.project = null; selectProject(id); }, 'refresh')); $('project-error').hidden = false; setConnection('', '未连接'); updateComposer(); }
  }
  function updateProjectInList(p) { const i = state.projects.findIndex(x => x.id === p.id); if (i >= 0) state.projects[i] = { ...state.projects[i], ...p }; else state.projects.unshift(p); renderProjects(); }
  function refreshProjectMetaSoon() {
    clearTimeout(state.metaTimer);
    const epoch = state.epoch;
    state.metaTimer = setTimeout(async () => {
      try {
        const d = await api('/projects');
        if (epoch !== state.epoch || !state.project) return;
        state.projects = d.projects || [];
        const current = state.projects.find(p => p.id === state.project.id);
        if (!current) { await projectUnavailable(state.project.id); return; }
        state.project = { ...state.project, ...current }; renderHeader();
        renderProjects();
      } catch (_) { /* The stream still supplies message and turn updates. */ }
    }, 250);
  }
  function renderHeader() {
    const p = state.project; if (!p) return; $('project-title').textContent = p.name; $('breadcrumb-project').textContent = p.name; $('project-description').textContent = p.description || ''; $('project-description').hidden = !p.description;
    $('provider-pill').replaceChildren(icon('spark'), document.createTextNode(providerNames[p.provider] || p.provider || '')); $('project-status').textContent = labels[p.status] || '正在加载'; $('project-model').textContent = p.model || ''; $('project-settings').hidden = !isOwner(); $('add-member').hidden = !isOwner();
    const links = $('project-links'); links.replaceChildren();
    [['github_url', 'GitHub'], ['overleaf_url', 'Overleaf']].forEach(([key, label]) => { if (!p[key]) return; let url; try { url = new URL(p[key]); } catch (_) { return; } if (url.protocol !== 'https:' || url.username || url.password) return; const a = el('a', 'project-link', `${label} ↗`); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.setAttribute('aria-label', `打开 ${label} 项目（新窗口）`); links.append(a); });
    links.hidden = !links.childElementCount;
    const turn = p.active_turn; const n = Number(p.queued_count) || 0; const running = turn && !terminalStatuses.has(turn.status);
    $('queue-banner').hidden = !running && !n; $('queue-label').textContent = running ? `正在处理共同的任务${n ? ` · 后面还有 ${n} 条消息` : ''}` : `${n} 条消息等待处理`;
    $('cancel-active').hidden = !running || !canCancel(turn); renderPresence(); updateComposer();
  }
  function canCancel(t) { return !!t && !(state.project?.provider === 'bridge' && t.status === 'running') && (isOwner() || (canWrite() && String(t.author_id) === String(state.user?.id))); }
  function canRetry(t) { return !!t && canWrite() && (isOwner() || String(t.author_id) === String(state.user?.id)); }
  function updateTurn(t) {
    if (!t?.id) return;
    const old = state.turns.get(String(t.id));
    const wasQueued = old?.status === 'queued';
    const isQueued = t.status === 'queued';
    if (wasQueued !== isQueued) state.project.queued_count = Math.max(0, (Number(state.project.queued_count) || 0) + (isQueued ? 1 : -1));
    state.turns.set(String(t.id), { ...old, ...t });
    if (t.status === 'running') { state.project.active_turn = t; state.project.status = 'running'; }
    else if (state.project.active_turn?.id === t.id && terminalStatuses.has(t.status)) {
      state.project.active_turn = null;
      state.project.status = ['failed', 'interrupted'].includes(t.status) ? 'error' : state.project.queued_count ? 'waiting' : 'idle';
    }
    else if (state.project.status !== 'running') state.project.status = state.project.queued_count ? 'waiting' : (['failed', 'interrupted'].includes(t.status) ? 'error' : 'idle');
    renderHeader();
    updateProjectInList(state.project);
  }
  function updateComposer() { const available = canWrite() && !state.projectLoading; $('message-input').disabled = !available; $('attach-button').disabled = !available || state.uploading > 0; $('send-message').disabled = !available || state.sending || state.uploading > 0 || (!$('message-input').value.trim() && !state.attachments.length); $('message-input').placeholder = state.project?.role === 'viewer' && !isOwner() ? '只读项目' : '发送消息…'; $('composer-note').textContent = state.project?.role === 'viewer' && !isOwner() ? '只读权限' : state.sending ? '正在发送…' : ''; }
  function nearBottom() { const c = $('conversation'); return c.scrollHeight - c.scrollTop - c.clientHeight < 110; }
  function scrollBottom() { requestAnimationFrame(() => { const c = $('conversation'); c.scrollTop = c.scrollHeight; $('jump-latest').hidden = true; }); }
  function preserveScroll(fn, force = false) { const bottom = nearBottom(); fn(); if (bottom || force) scrollBottom(); else $('jump-latest').hidden = false; }
  function sortedMessages() { return [...state.messages.values()].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id))); }
  function renderMessages() { const msgs = sortedMessages(); $('messages').replaceChildren(); msgs.forEach(m => $('messages').append(messageNode(m))); $('conversation-empty').hidden = !!msgs.length; $('load-history').hidden = !state.historyMore || !msgs.length; }
  function renderMessage(m) { const old = $('messages').querySelector(`[data-message-id="${CSS.escape(String(m.id))}"]`); const n = messageNode(m); if (old) old.replaceWith(n); else { const next = sortedMessages().find(x => String(x.created_at) > String(m.created_at)); const nextNode = next && $('messages').querySelector(`[data-message-id="${CSS.escape(String(next.id))}"]`); $('messages').insertBefore(n, nextNode || null); } $('conversation-empty').hidden = true; }
  function safeHref(href) { try { const u = new URL(href, location.href); return ['http:', 'https:'].includes(u.protocol) ? u.href : null; } catch (_) { return null; } }
  function inline(parent, text) {
    const regex = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\))/g; let match; let last = 0;
    while ((match = regex.exec(text))) { parent.append(document.createTextNode(text.slice(last, match.index))); if (match[2]) parent.append(el('strong', '', match[2])); else if (match[3]) parent.append(el('code', '', match[3])); else { const href = safeHref(match[5]); if (href) { const a = el('a', '', match[4]); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; parent.append(a); } else parent.append(document.createTextNode(match[0])); } last = regex.lastIndex; }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function markdown(text) {
    const body = el('div', 'message-text'); const chunks = String(text || '').split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g);
    chunks.forEach(chunk => { if (!chunk) return; if (chunk.startsWith('```')) { const content = chunk.replace(/^```[^\n]*\n/, '').replace(/```$/, ''); const pre = el('pre'); pre.append(el('code', '', content)); body.append(pre); return; }
      chunk.split(/\n\s*\n/).filter(Boolean).forEach(block => { if (/^#{1,4} /.test(block) && !block.includes('\n')) { const h = el('h3'); inline(h, block.replace(/^#{1,4} /, '')); body.append(h); } else if (block.split('\n').every(l => /^\s*(?:[-*]|\d+\.)\s/.test(l))) { const list = el(/^\s*\d+\./.test(block) ? 'ol' : 'ul'); block.split('\n').forEach(l => { const item = el('li'); inline(item, l.replace(/^\s*(?:[-*]|\d+\.)\s/, '')); list.append(item); }); body.append(list); } else { const p = el('p'); inline(p, block); body.append(p); } });
    }); return body;
  }
  function messageNode(m) {
    const node = el('article', `message ${m.role || 'user'}${m.status === 'running' && m.role === 'assistant' ? ' streaming' : ''}`); node.dataset.messageId = m.id;
    const a = m.role === 'user' ? avatar(m.author, 'message-avatar') : el('span', 'avatar message-avatar'); if (m.role !== 'user') a.append(icon(m.role === 'system' ? 'notebook' : 'spark')); node.append(a);
    const body = el('div', 'message-body'); const meta = el('div', 'message-meta'); meta.append(el('strong', 'message-author', m.role === 'assistant' ? (providerNames[state.project?.provider] || '研究助手') : m.role === 'system' ? '工作空间' : name(m.author)), el('time', 'message-time', when(m.created_at)));
    if (m.status && m.status !== 'complete') meta.append(el('span', `message-status ${m.status}`, labels[m.status] || m.status)); body.append(meta);
    body.append(markdown(m.content || (m.role === 'assistant' && m.status === 'queued' ? '等待前面的任务完成…' : '')));
    if (m.attachments?.length) { const files = el('div', 'message-attachments'); m.attachments.forEach(f => { const b = button('', 'attachment-chip', () => openFile(f)); b.append(icon('paper'), el('span', '', f.name), el('small', '', bytes(f.size))); files.append(b); }); body.append(files); }
    if (m.error) body.append(el('div', 'message-error', m.error));
    const t = state.turns.get(String(m.turn_id)) || { id: m.turn_id, status: m.status, author_id: m.author?.id };
    if (m.role === 'assistant' && state.tools.has(String(m.turn_id))) { const ts = el('div', 'tool-events'); state.tools.get(String(m.turn_id)).slice(-6).forEach(tool => { const detail = el('details', 'tool-event'); const summary = el('summary'); summary.append(icon(tool.status === 'complete' ? 'check' : 'tool'), el('span', '', `${tool.name || '工具'} · ${labels[tool.status] || tool.status || '处理中'}`)); detail.append(summary); if (tool.content) detail.append(el('pre', '', String(tool.content).slice(0, 6000))); ts.append(detail); }); body.append(ts); }
    if (m.turn_id && m.role === 'user' && activeStatuses.has(t.status) && canCancel(t)) { const actions = el('div', 'message-actions'); actions.append(button(t.status === 'queued' ? '取消排队' : '停止任务', 'quiet-btn', () => cancelTurn(t.id), 'stop')); body.append(actions); }
    if (m.turn_id && m.role === 'user' && ['failed', 'interrupted', 'cancelled'].includes(t.status) && canRetry(t)) { const actions = el('div', 'message-actions'); actions.append(button('重新尝试', 'quiet-btn', () => retryTurn(t.id), 'refresh')); body.append(actions); }
    node.append(body); return node;
  }
  async function loadHistory() { if (!state.project || state.loadingHistory || !state.historyMore) return; const msgs = sortedMessages(); if (!msgs.length) return; const epoch = state.epoch; const c = $('conversation'); const beforeHeight = c.scrollHeight; state.loadingHistory = true; $('load-history').disabled = true; try { const d = await api(projectAPI(`/messages?before=${encodeURIComponent(msgs[0].created_at)}&limit=100`)); if (epoch !== state.epoch) return; const oldTop = c.scrollTop; const n = (d.messages || []).length; (d.messages || []).forEach(m => state.messages.set(String(m.id), m)); state.historyMore = n >= 100; renderMessages(); c.scrollTop = oldTop + c.scrollHeight - beforeHeight; } catch (err) { toast(err.message, 'error'); } finally { state.loadingHistory = false; $('load-history').disabled = false; } }
  async function sendMessage(event) {
    event?.preventDefault(); if (!canWrite() || state.sending || state.uploading || state.projectLoading) return; const content = $('message-input').value.trim(); const attachment_ids = state.attachments.map(f => f.id); if (!content && !attachment_ids.length) return;
    const pid = state.project.id; const epoch = state.epoch; const signature = JSON.stringify({ pid, content, attachment_ids }); if (!state.pendingSend || state.pendingSend.signature !== signature) state.pendingSend = { signature, client_id: uuid() };
    const client_id = state.pendingSend.client_id; state.sending = true; updateComposer();
    try { const d = await api(projectAPI('/messages', pid), { method: 'POST', body: { content, attachment_ids, client_id } }); if (epoch !== state.epoch) return; state.pendingSend = null; state.messages.set(String(d.message.id), d.message); if (d.turn) updateTurn(d.turn); preserveScroll(() => renderMessage(d.message), true); if ($('message-input').value.trim() === content) { $('message-input').value = ''; $('message-input').style.height = ''; } state.attachments = []; renderUploads(); $('message-input').focus(); }
    catch (err) { toast(`${err.message} 消息已保留，再次发送会安全重试。`, 'error'); }
    finally { if (epoch === state.epoch) { state.sending = false; updateComposer(); } }
  }
  async function cancelTurn(id) { try { await api(projectAPI(`/turns/${encodeURIComponent(id)}/cancel`), { method: 'POST', body: {} }); toast('已发出停止请求。'); } catch (err) { toast(err.message, 'error'); } }
  async function retryTurn(id) { const pid = state.project?.id; const epoch = state.epoch; try { const d = await api(projectAPI(`/turns/${encodeURIComponent(id)}/retry`, pid), { method: 'POST', body: { client_id: uuid() } }); if (epoch !== state.epoch) return; if (d.turn) updateTurn(d.turn); if (d.message) { state.messages.set(String(d.message.id), d.message); preserveScroll(() => renderMessage(d.message), true); } toast('已重新加入任务队列。'); } catch (err) { toast(err.message, 'error'); } }
  async function uploadFiles(fileList) {
    if (!canWrite() || state.projectLoading) return; const files = [...fileList]; const pid = state.project.id; const epoch = state.epoch;
    for (const file of files) {
      if (epoch !== state.epoch) break; if (file.size > 50 * 1024 * 1024) { toast(`${file.name} 超过 50 MB，请选择较小的文件。`, 'error'); continue; }
      state.uploading++; renderUploads(file.name); updateComposer(); const form = new FormData(); form.append('file', file);
      try { const d = await api(projectAPI('/files', pid), { method: 'POST', body: form }); if (epoch !== state.epoch) continue; const f = d.file || d; state.attachments.push(f); if (!state.files.some(x => x.id === f.id)) state.files.unshift(f); renderFiles(); }
      catch (err) { toast(`${file.name} 上传未完成。${err.message}`, 'error'); }
      finally { if (epoch === state.epoch) { state.uploading = Math.max(0, state.uploading - 1); renderUploads(); updateComposer(); } }
    }
    $('file-input').value = '';
  }
  function renderUploads(uploadingName) { $('upload-list').replaceChildren(); state.attachments.forEach(f => { const chip = el('div', 'upload-item'); chip.append(icon('paper'), el('span', '', f.name), iconButton('移除附件', 'close', () => { state.attachments = state.attachments.filter(x => x.id !== f.id); renderUploads(); updateComposer(); })); $('upload-list').append(chip); }); if (state.uploading) { const chip = el('div', 'upload-item uploading'); chip.append(icon('upload'), el('span', '', `正在上传${uploadingName ? ` ${uploadingName}` : '…'}`)); $('upload-list').append(chip); } }
  function localFileURL(f) { return `/api${projectAPI(`/files/${encodeURIComponent(f.id)}`, f.project_id || state.project.id)}`; }
  function openFile(f) { const url = localFileURL(f); if (/\.pdf$/i.test(f.name)) previewPDF(f.name, `${url}?inline=1`, url); else download(url, f.name); }
  function download(url, filename) { const a = el('a'); a.href = url; a.download = filename || ''; document.body.append(a); a.click(); a.remove(); }
  function previewPDF(filename, inlineURL, downloadURL) { $('preview-title').textContent = filename; $('preview-download').href = downloadURL; $('preview-download').download = filename; $('pdf-preview').src = inlineURL; if (!$('preview-dialog').open) $('preview-dialog').showModal(); }
  function renderFiles() { $('shared-files').replaceChildren(); if (!state.files.length) $('shared-files').append(el('p', 'empty-small', '还没有共享文件。通过对话框上传论文、图片或结果。')); state.files.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).forEach(f => $('shared-files').append(fileRow(f.name, f.size, 'file', () => openFile(f), () => download(localFileURL(f), f.name)))); }
  function fileRow(filename, size, type, open, save) { const row = el('div', 'file-row'); const ext = filename.includes('.') ? filename.split('.').pop().slice(0, 4).toUpperCase() : 'FILE'; const mark = el('span', `file-type ${type === 'directory' ? 'directory' : ext.toLowerCase()}`); if (type === 'directory') mark.append(icon('folder')); else mark.textContent = ext; const info = button('', 'file-row-info', open); info.append(el('span', 'file-name', filename), el('span', 'file-size', type === 'directory' ? '文件夹' : bytes(size))); row.append(mark, info); if (save) row.append(iconButton('下载文件', 'download', save)); return row; }
  async function refreshFiles() { const epoch = state.epoch; if (!state.project) return; try { const d = await api(projectAPI('/files')); if (epoch !== state.epoch) return; state.files = d.files || []; renderFiles(); } catch (err) { toast(err.message, 'error'); } }
  async function loadWorkspace(path = '') {
    if (!state.project) return; const epoch = state.epoch; state.workspacePath = path; $('workspace-files').replaceChildren(el('p', 'empty-small', '正在打开…'));
    try { const d = await api(projectAPI(`/workspace?path=${encodeURIComponent(path)}`)); if (epoch !== state.epoch || path !== state.workspacePath) return; state.workspacePath = d.path || ''; renderWorkspacePath(); $('workspace-files').replaceChildren(); if (!d.entries?.length) $('workspace-files').append(el('p', 'empty-small', '这里还没有成果文件。')); (d.entries || []).sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'directory' ? -1 : 1)).forEach(f => { const url = `/api${projectAPI(`/artifact?path=${encodeURIComponent(f.path)}`)}`; $('workspace-files').append(fileRow(f.name, f.size, f.type, () => { if (f.type === 'directory') loadWorkspace(f.path); else if (/\.pdf$/i.test(f.name)) previewPDF(f.name, `${url}&inline=1`, url); else download(url, f.name); }, f.type === 'file' ? () => download(url, f.name) : null)); }); }
    catch (err) { if (epoch === state.epoch) $('workspace-files').replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', () => loadWorkspace(path), 'refresh')); }
  }
  function renderWorkspacePath() { const root = $('workspace-path'); root.replaceChildren(button('成果', '', () => loadWorkspace(''))); let path = ''; state.workspacePath.split('/').filter(Boolean).forEach(part => { path += `${path ? '/' : ''}${part}`; const target = path; root.append(el('span', '', '/'), button(part, '', () => loadWorkspace(target))); }); }
  function normalizePresence(payload) { const p = payload?.members || payload?.users || payload?.online || payload?.online_users || (Array.isArray(payload) ? payload : []); return p.map(x => typeof x === 'object' ? (x.user || x) : state.members.find(m => String(m.id) === String(x))).filter(Boolean); }
  function renderPresence() { $('presence').replaceChildren(); const online = state.presence; online.slice(0, 4).forEach(u => $('presence').append(avatar(u))); if (online.length) $('presence').append(el('span', 'presence-label', `${online.length} 人在线`)); }
  function renderMembers() { $('member-list').replaceChildren(); if (!state.members.length) $('member-list').append(el('p', 'empty-small', '尚无成员信息。')); state.members.forEach(m => { const online = state.presence.some(u => String(u.id) === String(m.id)); const row = el('div', `member-row${online ? ' online' : ''}`); const info = el('div', 'member-info'); info.append(el('strong', '', name(m)), el('small', '', online ? '在线' : `@${m.username}`)); row.append(avatar(m), info, el('span', 'member-role', roles[m.role] || m.role)); if (isOwner() && m.role !== 'owner' && String(m.id) !== String(state.user.id)) row.append(iconButton('移除成员', 'close', () => removeMember(m))); $('member-list').append(row); }); }
  async function refreshMembers() { const epoch = state.epoch; try { const d = await api(projectAPI('/members')); if (epoch !== state.epoch) return; state.members = d.members || []; renderMembers(); } catch (err) { toast(err.message, 'error'); } }
  async function removeMember(member) { openDialog('移除项目成员', 'PROJECT MEMBERS', body => { body.append(el('p', 'form-help', `移除 ${name(member)} 后，对方将无法访问这个项目。已有讨论会保留。`)); }, async () => { await api(projectAPI(`/members/${encodeURIComponent(member.id)}`), { method: 'DELETE' }); await refreshMembers(); toast('已移除成员。'); }, '确认移除'); }
  function renderTab() { ['files', 'activity', 'members'].forEach(t => { $(`tab-${t}`).classList.toggle('active', state.tab === t); $(`tab-${t}`).setAttribute('aria-selected', state.tab === t ? 'true' : 'false'); $(`panel-${t}`).hidden = !state.project || state.tab !== t; }); $('inspector-empty').hidden = !!state.project; }
  function activityDescription(event) { const p = event.payload || {}; const author = p.message?.author || p.user || p.author; const who = author ? name(author) : ''; const type = event.type; if (type === 'message.created') return `${who || (p.message?.role === 'assistant' ? providerNames[state.project?.provider] || '助手' : '成员')} ${p.message?.role === 'assistant' ? '开始回复' : '加入了一条讨论'}`; if (type === 'message.updated') return '更新了一条讨论'; if (type === 'turn.updated') return `研究任务${labels[p.turn?.status] || '已更新'}`; if (type === 'file.created') return `上传了 ${p.file?.name || '一个文件'}`; if (type === 'member.updated') return '项目成员已更新'; if (type === 'project.updated') return '项目设置已更新'; if (type === 'agent.tool') return `${p.name || '工具'} · ${labels[p.status] || p.status || '运行中'}`; return '工作空间已更新'; }
  function renderActivity() { const list = state.activity.filter(e => !['presence', 'agent.delta', 'message.updated'].includes(e.type)).slice(-70).reverse(); $('activity-list').replaceChildren(); if (!list.length) $('activity-list').append(el('p', 'empty-small', '研究的每一步，会在这里留下记录。')); list.forEach(e => { const row = el('div', 'activity-entry'); const mark = el('span', 'activity-icon'); mark.append(icon(e.type === 'file.created' ? 'paper' : e.type === 'member.updated' ? 'users' : e.type === 'turn.updated' ? 'check' : e.type === 'agent.tool' ? 'tool' : 'notebook')); const info = el('div'); info.append(el('p', '', activityDescription(e)), el('small', '', when(e.created_at))); row.append(mark, info); $('activity-list').append(row); }); }
  async function loadActivity() { if (!state.project) return; const epoch = state.epoch; try { const d = await api(projectAPI('/activity?after=0')); if (epoch !== state.epoch) return; const merged = new Map(state.activity.map(e => [String(e.id), e])); (d.events || []).forEach(e => merged.set(String(e.id), e)); state.activity = [...merged.values()].sort((a, b) => Number(a.id) - Number(b.id)).slice(-300); renderActivity(); } catch (err) { if (epoch === state.epoch) $('activity-list').replaceChildren(el('p', 'empty-small', err.message), button('重试', 'quiet-btn', loadActivity, 'refresh')); } }
  async function heartbeat(epoch) { if (epoch !== state.epoch || !state.project || document.hidden) return; const pid = state.project.id; try { await api(projectAPI('/presence', pid), { method: 'POST', body: {} }); } catch (err) { if (epoch === state.epoch && (err.status === 404 || err.status === 403)) await projectUnavailable(pid); } }
  function acceptEvent(event) {
    const id = Number(event.id) || 0;
    if (id && id <= state.cursor) return;
    if (id) state.cursor = id;
    applyEvent(event);
  }
  async function pollEvents(epoch) {
    if (epoch !== state.epoch || !state.project || !state.user) return;
    let delay = 1200; const pid = state.project.id;
    try {
      if (!document.hidden && Date.now() - (state.lastStreamEvent || 0) > 2000) {
        const result = await api(projectAPI(`/event-batch?after=${state.cursor}`), { signal: AbortSignal.timeout(8000) });
        if (epoch !== state.epoch) return;
        (result.events || []).forEach(acceptEvent);
        setConnection('connected', '同步正常');
        if (result.events?.length === 200) delay = 50;
      }
      if (!document.hidden && Date.now() - (state.lastMetaPoll || 0) > 10000) {
        state.lastMetaPoll = Date.now(); refreshProjectMetaSoon();
      }
    } catch (err) {
      if (epoch === state.epoch && (err.status === 404 || err.status === 403)) { await projectUnavailable(pid); return; }
      if (epoch === state.epoch && Date.now() - (state.lastStreamEvent || 0) > 10000) setConnection('reconnecting', '正在重新连接');
    } finally {
      if (epoch === state.epoch && state.user && state.project) state.pollTimer = setTimeout(() => pollEvents(epoch), delay);
    }
  }
  function connectEvents(epoch) {
    if (epoch !== state.epoch || !state.project || !state.user) return; if (state.source) state.source.close(); const source = new EventSource(`/api${projectAPI(`/events?after=${state.cursor}`)}`, { withCredentials: true }); state.source = source;
    source.onopen = () => { if (epoch !== state.epoch) return; state.reconnectDelay = 1200; };
    const handle = evt => { if (epoch !== state.epoch) return; let event; try { event = JSON.parse(evt.data); } catch (_) { return; } state.lastStreamEvent = Date.now(); setConnection('connected', '实时同步'); acceptEvent({ ...event, id: event.id || evt.lastEventId }); };
    source.onmessage = handle; ['message.created', 'message.updated', 'turn.updated', 'agent.delta', 'agent.tool', 'project.updated', 'file.created', 'member.updated', 'presence'].forEach(type => source.addEventListener(type, handle));
    source.onerror = () => { if (epoch !== state.epoch || !state.user) return; source.close(); if (state.source === source) state.source = null; setConnection('reconnecting', '正在重新连接'); clearTimeout(state.reconnectTimer); state.reconnectTimer = setTimeout(() => connectEvents(epoch), state.reconnectDelay); state.reconnectDelay = Math.min(15000, state.reconnectDelay * 1.6); };
  }
  function applyEvent(event) {
    const p = event.payload || {}; const type = event.type;
    if (type !== 'presence' && type !== 'agent.delta') { state.activity.push(event); if (state.activity.length > 300) state.activity.shift(); if (state.tab === 'activity') renderActivity(); }
    if (type === 'message.created' || type === 'message.updated') { const m = p.message || p; if (!m.id) return; const old = state.messages.get(String(m.id)); const merged = { ...old, ...m }; state.messages.set(String(m.id), merged); preserveScroll(() => renderMessage(merged)); }
    else if (type === 'agent.delta') { let m = state.messages.get(String(p.message_id)); if (!m) { m = { id: p.message_id, role: 'assistant', content: '', status: 'running', created_at: event.created_at, turn_id: p.turn_id || state.project.active_turn?.id }; } m = { ...m, content: `${m.content || ''}${p.delta || ''}`, status: 'running' }; state.messages.set(String(m.id), m); preserveScroll(() => renderMessage(m)); }
    else if (type === 'turn.updated') { const t = p.turn || p; updateTurn(t); preserveScroll(() => { [t.user_message_id, t.assistant_message_id].forEach(id => { const m = state.messages.get(String(id)); if (m) { m.status = t.status; if (t.error && m.role === 'assistant') m.error = t.error; renderMessage(m); } }); }); refreshProjectMetaSoon(); }
    else if (type === 'project.updated') { const project = p.project || p; state.project = { ...state.project, ...project }; updateProjectInList(state.project); renderHeader(); }
    else if (type === 'file.created') { const f = p.file || p; if (f.id && !state.files.some(x => x.id === f.id)) state.files.unshift(f); renderFiles(); }
    else if (type === 'member.updated') { if (p.members) { state.members = p.members; renderMembers(); } else refreshMembers(); }
    else if (type === 'presence') { state.presence = normalizePresence(p); renderPresence(); renderMembers(); }
    else if (type === 'agent.tool') { const id = String(p.turn_id); const list = state.tools.get(id) || []; const current = [...list].reverse().find(x => x.name === p.name && !terminalStatuses.has(x.status)); if (current && p.status !== 'running') Object.assign(current, p); else list.push(p); state.tools.set(id, list.slice(-12)); const m = [...state.messages.values()].find(x => String(x.turn_id) === id && x.role === 'assistant'); if (m) preserveScroll(() => renderMessage(m)); }
  }
  function field(body, label, id, options = {}) { const wrap = el('label', '', label); const input = el(options.tag || 'input'); input.id = id; input.name = id; if (options.tag === 'select') (options.options || []).forEach(([value, text]) => { const o = el('option', '', text); o.value = value; input.append(o); }); else if (options.tag !== 'textarea') input.type = options.type || 'text'; if (options.placeholder) input.placeholder = options.placeholder; if (options.required) input.required = true; if (options.maxLength) input.maxLength = options.maxLength; if (options.autocomplete) input.autocomplete = options.autocomplete; if (options.minLength) input.minLength = options.minLength; if (options.value !== undefined) input.value = options.value || ''; if (options.disabled) input.disabled = true; wrap.append(input); body.append(wrap); return input; }
  function openDialog(title, eyebrow, build, action, submit = '保存') { $('dialog-title').textContent = title; $('dialog-eyebrow').textContent = eyebrow; $('dialog-body').replaceChildren(); $('dialog-error').hidden = true; $('dialog-submit').textContent = submit; $('dialog-submit').className = 'btn primary'; $('dialog-submit').disabled = false; $('dialog-submit').hidden = !action; $('dialog-cancel').textContent = action ? '取消' : '关闭'; state.dialogAction = action; state.dialogCanSubmit = null; state.dialogBusy = false; build($('dialog-body')); if (!$('form-dialog').open) $('form-dialog').showModal(); }
  function providerChoices(body) { const wrap = el('div'); wrap.append(el('div', 'form-help', '项目助手')); const options = el('div', 'provider-options'); const available = state.providers.filter(p => p.installed); const preferred = available[0]?.id; if (!preferred) options.append(el('p', 'empty-small', '目前没有可用的项目助手。请管理员先在主机上完成配置。')); available.forEach(p => { const label = el('label', 'provider-option'); const input = el('input'); input.type = 'radio'; input.name = 'project-provider'; input.value = p.id; input.checked = p.id === preferred; input.required = true; const text = el('span', '', p.name || providerNames[p.id] || p.id); label.append(input, text); options.append(label); }); wrap.append(options); body.append(wrap); if (!preferred) $('dialog-submit').disabled = true; }
  function projectLinkFields(body, prefix, project = {}) {
    field(body, 'GitHub 仓库', `${prefix}-github`, { type: 'url', value: project.github_url, maxLength: 2048, placeholder: 'https://github.com/owner/repository' });
    field(body, 'Overleaf 项目', `${prefix}-overleaf`, { type: 'url', value: project.overleaf_url, maxLength: 2048, placeholder: 'https://www.overleaf.com/project/…' });
    body.append(el('p', 'form-help', '关联这篇论文的代码仓库与 Overleaf 项目。可先创建，稍后补充链接。'));
  }
  function projectLinkValues(prefix) { return { github_url: $(`${prefix}-github`).value.trim(), overleaf_url: $(`${prefix}-overleaf`).value.trim() }; }
  function createProject() {
    openDialog('新建项目', 'NEW RESEARCH PROJECT', body => {
      field(body, '项目名称', 'new-project-name', { required: true, maxLength: 120, placeholder: '例如：InfiniteWorld' });
      field(body, '研究简介', 'new-project-description', { tag: 'textarea', maxLength: 2000, placeholder: '这篇研究希望回答什么问题？' });
      projectLinkFields(body, 'new-project'); providerChoices(body);
      field(body, '模型（可选）', 'new-project-model', { placeholder: '使用助手默认模型' });
      field(body, '已有会话 ID（可选）', 'new-project-session', { placeholder: '接续已有的 Codex / Claude 会话' });
    }, async () => {
      const provider = $('dialog-form').querySelector('input[name="project-provider"]:checked')?.value;
      const payload = { name: $('new-project-name').value.trim(), description: $('new-project-description').value.trim(), provider, ...projectLinkValues('new-project') };
      if ($('new-project-model').value.trim()) payload.model = $('new-project-model').value.trim();
      if ($('new-project-session').value.trim()) payload.native_session_id = $('new-project-session').value.trim();
      const p = await api('/projects', { method: 'POST', body: payload }); const project = p.project || p; updateProjectInList(project); await selectProject(project.id); toast('新的研究空间已准备好。');
    }, '创建项目');
  }
  function projectSettings() {
    if (!state.project || !isOwner()) return;
    const project = { ...state.project }; const busy = !!project.active_turn || Number(project.queued_count) > 0 || project.status === 'running'; const bound = !!project.native_session_id;
    openDialog('项目设置', 'PROJECT SETTINGS', body => {
      field(body, '项目名称', 'settings-name', { value: project.name, required: true, maxLength: 120 });
      field(body, '研究简介', 'settings-description', { tag: 'textarea', value: project.description, maxLength: 2000 }); projectLinkFields(body, 'settings', project);
      body.append(el('p', 'form-help', `项目助手 · ${providerNames[project.provider] || project.provider}`));
      field(body, '模型', 'settings-model', { value: project.model, placeholder: '使用助手默认模型', disabled: busy });
      field(body, '共享的原生会话 ID', 'settings-session', { value: project.native_session_id, placeholder: '首个任务开始后会自动绑定', disabled: busy || bound });
      body.append(el('p', 'form-help', busy ? '正在进行任务。队列清空后可以更改模型；会话已绑定时不可改绑。' : bound ? '该项目已绑定共享会话。为保护共同上下文，绑定后不可改绑；新会话请创建新项目。' : '可以绑定已有会话；首次任务开始后会自动绑定，之后不可改绑。'));
      if (project.permission_mode === 'full-access') { const badge = el('div', 'permission-badge'); const text = el('div'); text.append(el('strong', '', 'Full access · 完整执行权限'), el('p', '', '此项目由管理员启用完整执行权限。助手可执行研究任务、操作文件和运行工具。')); badge.append(icon('shield'), text); body.append(badge); }
      if (state.user?.role === 'owner') { const section = el('div', 'project-danger'); section.append(el('p', '', '删除项目后，成员将无法再访问此项目。'), button('删除项目', 'btn danger-outline', () => deleteProject(project))); body.append(section); }
    }, async () => {
      const data = { name: $('settings-name').value.trim(), description: $('settings-description').value.trim(), ...projectLinkValues('settings') };
      if (!busy) { const model = $('settings-model').value.trim(); if (model !== (project.model || '')) data.model = model || null; if (!bound) { const native = $('settings-session').value.trim(); if (native) data.native_session_id = native; } }
      const p = await api(projectAPI('', project.id), { method: 'PATCH', body: data }); const updated = p.project || p; if (state.project?.id === project.id) { state.project = { ...state.project, ...updated }; renderHeader(); } updateProjectInList(updated); toast('项目设置已保存。');
    });
  }
  function deleteProject(project) {
    if (state.user?.role !== 'owner' || String(state.project?.id) !== String(project.id)) return;
    openDialog('删除项目', 'DELETE PROJECT', body => {
      body.append(el('p', 'delete-project-scope', '此操作会删除 Shui! 中的项目及讨论记录，取消尚未开始的任务，并移除成员对上传文件和工作成果的访问权限。此操作无法撤销。'));
      body.append(el('p', 'delete-project-scope', '关联的 GitHub 仓库、Overleaf 项目、服务器工作文件和原生助手会话历史会保留。正在运行的任务需要先停止。'));
      const nameLabel = el('p', 'delete-project-name', '请输入项目名称确认删除：'); nameLabel.append(el('strong', '', project.name)); body.append(nameLabel);
      const confirmation = field(body, '项目名称', 'delete-project-confirm', { required: true, autocomplete: 'off', maxLength: 120 }); confirmation.spellcheck = false;
      state.dialogCanSubmit = () => confirmation.value === project.name;
      confirmation.addEventListener('input', () => { $('dialog-submit').disabled = state.dialogBusy || !state.dialogCanSubmit(); });
      $('dialog-submit').className = 'btn danger'; $('dialog-submit').disabled = true;
    }, async () => {
      const confirmName = $('delete-project-confirm').value; if (confirmName !== project.name) throw new Error('请输入完全一致的项目名称。');
      await api(projectAPI('', project.id), { method: 'DELETE', body: { confirm_name: confirmName } });
      await projectUnavailable(project.id, `已删除项目「${project.name}」。`);
    }, '永久删除项目');
  }
  function addMember() { const pid = state.project?.id; openDialog('添加成员', 'PROJECT MEMBERS', body => { field(body, '团队账号', 'member-username', { required: true, placeholder: '输入已有用户名', autocomplete: 'off' }); field(body, '项目角色', 'member-role-input', { tag: 'select', options: [['editor', '协作者 · 讨论、上传与任务'], ['viewer', '只读成员 · 查看讨论与文件']] });  }, async () => { await api(projectAPI('/members', pid), { method: 'POST', body: { username: $('member-username').value.trim(), role: $('member-role-input').value } }); if (state.project?.id === pid) await refreshMembers(); toast('成员已加入项目。'); }, '添加成员'); }
  async function manageUsers() {
    if (state.user?.role !== 'owner') return;
    let list;
    openDialog('团队账号', 'TEAM MANAGEMENT', body => {
      const toolbar = el('div', 'admin-toolbar'); toolbar.append(el('p', 'admin-caption', '统一管理账号与项目权限。'), button('创建账号', 'btn secondary', createUser, 'plus'));
      list = el('div', 'user-admin-list'); list.id = 'admin-users'; list.append(el('p', 'empty-small', '正在加载团队成员…')); body.append(toolbar, list);
    }, null);
    try {
      const d = await api('/users'); if (!list.isConnected) return; list.replaceChildren();
      (d.users || []).forEach(u => {
        const row = button('', 'user-admin-row', () => editUser(u)); row.setAttribute('aria-label', `管理 ${name(u)} 的账号与项目权限`);
        const info = el('div', 'user-admin-info'); info.append(el('strong', '', name(u)), el('small', '', `@${u.username}`));
        const status = el('div', 'user-admin-status'); status.append(el('span', 'member-role', roles[u.role] || u.role));
        if (u.is_active === false) status.append(el('span', 'account-status disabled-account', '已停用'));
        else if (u.must_change_password) status.append(el('span', 'account-status password-pending', '待修改初始密码'));
        row.append(avatar(u), info, status, icon('settings')); list.append(row);
      });
      if (!d.users?.length) list.append(el('p', 'empty-small', '尚无团队账号。'));
    } catch (err) { if (list.isConnected) list.replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', manageUsers, 'refresh')); }
  }
  function createUser() {
    openDialog('创建账号', 'TEAM MANAGEMENT', body => {
      body.append(button('返回团队账号', 'quiet-btn admin-back', manageUsers));
      field(body, '用户名', 'user-create-username', { required: true, autocomplete: 'off', maxLength: 40, placeholder: '例如：editor' });
      field(body, '显示名称', 'user-create-name', { maxLength: 80, placeholder: '讨论中显示的名字' });
      body.append(el('p', 'account-password-note', '初始密码与用户名相同。成员首次登录时必须自行修改密码，之后才能进入工作空间。'));
      field(body, '团队角色', 'user-create-role', { tag: 'select', options: [['member', '团队成员'], ['owner', '管理员']] });
    }, async () => {
      const d = await api('/users', { method: 'POST', body: { username: $('user-create-username').value.trim(), display_name: $('user-create-name').value.trim(), role: $('user-create-role').value } });
      toast('账号已创建。初始密码与用户名相同。'); await editUser(d.user || d); return false;
    }, '创建并分配项目');
  }
  async function editUser(user) {
    if (state.user?.role !== 'owner') return;
    const self = String(user.id) === String(state.user.id); const membershipInputs = []; let membershipsLoaded = false; let membersList; let roleInput; let activeInput; let ownerNote;
    const save = async () => {
      if (!membershipsLoaded) throw new Error('项目权限仍在加载，请稍后保存。');
      const body = { display_name: $('user-edit-name').value.trim() }; if (!self) { body.role = roleInput.value; body.is_active = activeInput.checked; }
      const memberships = membershipInputs.filter(x => ['editor', 'viewer'].includes(x.input.value)).map(x => ({ project_id: x.project_id, role: x.input.value }));
      const d = await api(`/users/${encodeURIComponent(user.id)}`, { method: 'PATCH', body }); const updated = d.user || d;
      if (self) { state.user = { ...state.user, ...updated }; $('user-name').textContent = name(state.user); const a = avatar(state.user); a.id = 'user-avatar'; $('user-avatar').replaceWith(a); }
      try { await api(`/users/${encodeURIComponent(user.id)}/memberships`, { method: 'PUT', body: { memberships } }); }
      catch (err) { throw new Error(`账号信息已保存，但项目权限未保存。${err.message}`); }
      if (state.project) await refreshMembers(); toast('账号与项目权限已保存。'); await manageUsers(); return false;
    };
    function updateMembershipControls() {
      const owner = roleInput.value === 'owner'; ownerNote.hidden = !owner;
      membershipInputs.forEach(x => { x.input.disabled = owner || x.role === 'owner'; });
    }
    openDialog('账号设置', `@${user.username}`, body => {
      body.append(button('返回团队账号', 'quiet-btn admin-back', manageUsers));
      field(body, '显示名称', 'user-edit-name', { value: user.display_name || user.username, required: true, maxLength: 80 });
      roleInput = field(body, '团队角色', 'user-edit-role', { tag: 'select', value: user.role, disabled: self, options: [['member', '团队成员'], ['owner', '管理员']] });
      const activeLabel = el('label', 'checkbox-field'); activeInput = el('input'); activeInput.id = 'user-edit-active'; activeInput.type = 'checkbox'; activeInput.checked = user.is_active !== false; activeInput.disabled = self; activeLabel.append(activeInput, el('span', '', '允许此账号登录')); body.append(activeLabel);
      if (self) body.append(el('p', 'form-help', '当前账号保留管理员权限。'));
      body.append(el('h3', 'dialog-subhead', '项目权限'), el('p', 'form-help', '协作者可讨论、上传文件和运行任务；只读成员可查看项目内容。'));
      ownerNote = el('p', 'account-password-note', '管理员拥有所有 Shui! 项目的最高权限。'); body.append(ownerNote);
      membersList = el('div', 'account-project-list'); membersList.append(el('p', 'empty-small', '正在加载项目…')); body.append(membersList);
      roleInput.addEventListener('change', updateMembershipControls); updateMembershipControls();
      if (!self) {
        body.append(el('h3', 'dialog-subhead', '登录密码'));
        const status = el('p', 'form-help', user.must_change_password ? '成员尚未修改初始密码。' : '成员已设置自己的密码。');
        const reset = button('重置初始密码', 'quiet-btn danger', () => { resetPanel.hidden = false; reset.hidden = true; });
        const resetPanel = el('div', 'password-reset-confirm'); resetPanel.hidden = true;
        const cancel = button('取消', 'quiet-btn', () => { resetPanel.hidden = true; reset.hidden = false; });
        const confirm = button('确认重置', 'btn secondary danger', async () => {
          confirm.disabled = true; cancel.disabled = true; $('dialog-submit').disabled = true;
          try { const d = await api(`/users/${encodeURIComponent(user.id)}/reset-password`, { method: 'POST', body: {} }); Object.assign(user, d.user || d); status.textContent = '初始密码已重置为用户名，成员下次登录必须修改密码。'; resetPanel.hidden = true; reset.hidden = false; toast('密码已重置，原有登录已退出。'); }
          catch (err) { toast(err.message, 'error'); }
          finally { confirm.disabled = false; cancel.disabled = false; if (state.dialogAction === save) $('dialog-submit').disabled = !membershipsLoaded; }
        });
        const actions = el('div', 'reset-actions'); actions.append(cancel, confirm); resetPanel.append(el('p', '', '密码将重置为用户名，原有登录会退出。成员下次登录必须设置新密码。'), actions); body.append(status, reset, resetPanel);
      }
    }, save, '保存账号与权限');
    $('dialog-submit').disabled = true;
    try {
      const d = await api(`/users/${encodeURIComponent(user.id)}/memberships`); if (!membersList.isConnected || state.dialogAction !== save) return;
      membersList.replaceChildren();
      (d.memberships || []).forEach(m => {
        const row = el('label', 'account-project-row'); row.append(el('span', '', m.name)); const input = el('select'); input.setAttribute('aria-label', `${m.name} 的项目权限`);
        const options = m.role === 'owner' ? [['owner', '项目管理员']] : [['', '未加入'], ['editor', '协作者'], ['viewer', '只读成员']];
        options.forEach(([value, label]) => { const o = el('option', '', label); o.value = value; input.append(o); }); input.value = m.role || ''; row.append(input); membersList.append(row); membershipInputs.push({ project_id: m.project_id, role: m.role, input });
      });
      if (!d.memberships?.length) membersList.append(el('p', 'empty-small', '还没有项目，可在创建项目后分配权限。'));
      membershipsLoaded = true; $('dialog-submit').disabled = false; updateMembershipControls();
    } catch (err) { if (membersList.isConnected && state.dialogAction === save) membersList.replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', () => editUser(user), 'refresh')); }
  }
  function closeDrawers() { $('sidebar').classList.remove('open'); $('inspector').classList.remove('open'); $('drawer-scrim').classList.remove('visible'); $('drawer-scrim').hidden = true; }
  function openDrawer(id) { closeDrawers(); $(id).classList.add('open'); $('drawer-scrim').hidden = false; $('drawer-scrim').classList.add('visible'); }
  $('login-form').addEventListener('submit', async event => { event.preventDefault(); const submit = $('login-form').querySelector('button'); submit.disabled = true; $('login-error').hidden = true; try { const d = await api('/login', { method: 'POST', body: { username: $('login-username').value.trim(), password: $('login-password').value } }); $('login-password').value = ''; await startSession(d); } catch (err) { $('login-error').textContent = err.message; $('login-error').hidden = false; } finally { submit.disabled = false; } });
  $('password-form').addEventListener('submit', async event => {
    event.preventDefault(); if ($('password-submit').disabled) return;
    const currentPassword = $('password-current').value; const newPassword = $('password-new').value; const confirmPassword = $('password-confirm').value;
    $('password-error').hidden = true;
    try {
      if ([...newPassword].length < 8 || [...newPassword].length > 256) throw new Error('新密码需要 8–256 位字符。');
      if (newPassword.toLowerCase() === (state.user?.username || '').toLowerCase()) throw new Error('新密码不能与用户名相同。');
      if (newPassword === currentPassword) throw new Error('新密码不能与当前密码相同。');
      if (newPassword !== confirmPassword) throw new Error('两次输入的新密码不一致。');
      $('password-submit').disabled = true; $('password-logout').disabled = true; $('password-submit').textContent = '正在保存…';
      const session = await api('/password', { method: 'POST', body: { current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword } });
      await startSession(session); toast('密码已更新。');
    } catch (err) { $('password-error').textContent = err.message; $('password-error').hidden = false; }
    finally { $('password-submit').disabled = false; $('password-logout').disabled = false; $('password-submit').textContent = '保存并进入工作空间'; }
  });
  async function logout() { try { await api('/logout', { method: 'POST', body: {} }); showLogin(); } catch (err) { toast(err.message, 'error'); } }
  $('logout').addEventListener('click', logout); $('password-logout').addEventListener('click', logout);
  $('create-project').addEventListener('click', createProject); $('welcome-create').addEventListener('click', createProject); $('project-settings').addEventListener('click', projectSettings); $('add-member').addEventListener('click', addMember); $('manage-users').addEventListener('click', manageUsers);
  $('project-search').addEventListener('input', renderProjects); $('composer').addEventListener('submit', sendMessage); $('message-input').addEventListener('input', () => { const t = $('message-input'); t.style.height = 'auto'; t.style.height = `${Math.min(190, t.scrollHeight)}px`; updateComposer(); });
  $('message-input').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); sendMessage(); } });
  $('attach-button').addEventListener('click', () => $('file-input').click()); $('file-input').addEventListener('change', e => uploadFiles(e.target.files));
  $('composer').addEventListener('dragover', e => { e.preventDefault(); if (canWrite()) $('composer').classList.add('drag-over'); }); $('composer').addEventListener('dragleave', e => { if (!$('composer').contains(e.relatedTarget)) $('composer').classList.remove('drag-over'); }); $('composer').addEventListener('drop', e => { e.preventDefault(); $('composer').classList.remove('drag-over'); if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files); });
  $('message-input').addEventListener('paste', e => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); uploadFiles(files); } });
  $('load-history').addEventListener('click', loadHistory); $('conversation').addEventListener('scroll', () => { if (nearBottom()) $('jump-latest').hidden = true; }); $('jump-latest').addEventListener('click', scrollBottom); $('cancel-active').addEventListener('click', () => { if (state.project?.active_turn) cancelTurn(state.project.active_turn.id); });
  $('refresh-files').addEventListener('click', refreshFiles); $('refresh-workspace').addEventListener('click', () => loadWorkspace(state.workspacePath)); $('refresh-activity').addEventListener('click', loadActivity);
  document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => { state.tab = b.dataset.tab; renderTab(); if (state.tab === 'activity') renderActivity(); }));
  $('open-sidebar').addEventListener('click', () => openDrawer('sidebar')); $('close-sidebar').addEventListener('click', closeDrawers); $('open-inspector').addEventListener('click', () => openDrawer('inspector')); $('close-inspector').addEventListener('click', closeDrawers); $('drawer-scrim').addEventListener('click', closeDrawers);
  $('close-dialog').addEventListener('click', () => { if (!state.dialogBusy) $('form-dialog').close(); }); $('dialog-cancel').addEventListener('click', () => { if (!state.dialogBusy) $('form-dialog').close(); });
  $('form-dialog').addEventListener('cancel', event => { if (state.dialogBusy) event.preventDefault(); });
  $('dialog-form').addEventListener('submit', async e => { e.preventDefault(); const action = state.dialogAction; if (!action || $('dialog-submit').disabled || state.dialogBusy || (state.dialogCanSubmit && !state.dialogCanSubmit())) return; state.dialogBusy = true; $('dialog-submit').disabled = true; $('dialog-error').hidden = true; try { const close = await action(); if (close !== false && state.dialogAction === action) $('form-dialog').close(); } catch (err) { if (state.dialogAction === action) { $('dialog-error').textContent = err.message; $('dialog-error').hidden = false; } } finally { if (state.dialogAction === action) { state.dialogBusy = false; $('dialog-submit').disabled = !!state.dialogCanSubmit && !state.dialogCanSubmit(); } } });
  $('close-preview').addEventListener('click', () => $('preview-dialog').close()); $('preview-dialog').addEventListener('close', () => { $('pdf-preview').removeAttribute('src'); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawers(); });
  document.querySelectorAll('[data-tab]').forEach(b => { b.setAttribute('aria-controls', `panel-${b.dataset.tab}`); b.addEventListener('keydown', e => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return; e.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')]; const index = tabs.indexOf(b); const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; tabs[next].focus(); tabs[next].click(); }); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { heartbeat(state.epoch); if (state.project && !state.source) connectEvents(state.epoch); } });
  window.addEventListener('online', () => { if (state.project) { clearTimeout(state.reconnectTimer); connectEvents(state.epoch); } }); window.addEventListener('offline', () => setConnection('reconnecting', '等待网络恢复')); window.addEventListener('beforeunload', stopEvents);
  async function boot() { try { const session = await api('/session'); if (session?.authenticated) await startSession(session); else showLogin(); } catch (err) { $('boot').replaceChildren(el('span', '', 'Shui!'), el('p', '', err.message), button('重新连接', 'btn primary', boot, 'refresh')); } }
  boot();
})();
