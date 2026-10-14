/* Shui! — a shared research notebook. No dependencies, no remote assets. */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  function icon(name) {
    const node = window.SHUI_LUCIDE?.[name] || window.SHUI_LUCIDE?.paper || ['svg', { viewBox: '0 0 24 24', width: 24, height: 24, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5 }, [['rect', { x: 5, y: 3, width: 14, height: 18, rx: 2 }], ['path', { d: 'M8 8h8M8 12h8M8 16h5' }]]];
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
  const state = { user: null, csrf: null, providers: [], projects: [], project: null, messages: new Map(), turns: new Map(), tools: new Map(), files: [], members: [], presence: [], activity: [], insights: null, cursor: 0, source: null, reconnectTimer: null, reconnectDelay: 3000, heartbeat: null, metaTimer: null, insightTimer: null, epoch: 0, tab: 'files', workspacePath: '', attachments: [], uploading: 0, sending: false, pendingSend: null, pendingTaskWindow: null, historyMore: true, loadingHistory: false, dialogAction: null, projectLoading: false, liveConnected: false, delegations: new Map(), assignableMembers: [], knownMentionMembers: [], assignableLoaded: false, selectedAssignees: [], mention: null, mentionIndex: 0, mentionMatches: [], delegationError: '', delegationLoading: false, delegationBusy: false };
  const labels = { idle: '准备就绪', running: '正在推进', queued: '排队中', waiting: '等待处理', complete: '已完成', failed: '任务失败', error: '需要关注', cancelled: '已取消', interrupted: '已中断' };
  const providerNames = { codex: 'Codex', claude: 'Claude', bridge: 'Bridge' };
  const roles = { owner: '管理员', editor: '协作者', viewer: '只读成员', member: '团队成员' };
  const activeStatuses = new Set(['queued', 'running', 'waiting']);
  const terminalStatuses = new Set(['complete', 'failed', 'cancelled', 'interrupted']);
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = crypto.getRandomValues(new Uint8Array(1))[0] & 15; return (c === 'x' ? r : (r & 3) | 8).toString(16); });
  function stored(key, value) { try { if (value === undefined) return localStorage.getItem(key); if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_) {} return null; }
  function toast(message, kind = '') { const n = el('div', `toast ${kind}`, message); $('toasts').append(n); setTimeout(() => n.remove(), kind === 'error' ? 6500 : 4000); }
  function fallbackCopyText(text) {
    const active = document.activeElement; const selection = window.getSelection();
    const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
    const inputSelection = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
    const field = el('textarea'); field.value = text; field.readOnly = true; field.tabIndex = -1;
    field.style.cssText = 'position:fixed;left:-9999px;top:0;font-size:16px';
    (document.querySelector('dialog[open]') || document.body).append(field);
    try { field.focus({ preventScroll: true }); field.select(); return document.execCommand('copy'); }
    finally {
      field.remove();
      if (active?.isConnected) { active.focus({ preventScroll: true }); if (inputSelection) active.setSelectionRange(...inputSelection); }
      if (selection) { selection.removeAllRanges(); ranges.forEach(range => { if (range.commonAncestorContainer.isConnected) selection.addRange(range); }); }
    }
  }
  async function copyMessageText(text) {
    try {
      try { if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable'); await navigator.clipboard.writeText(text); }
      catch (_) { if (!fallbackCopyText(text)) throw new Error('Copy unavailable'); }
      toast('已复制文本。'); return true;
    } catch (_) { toast('复制未完成，请选中文字后按 Ctrl/Cmd+C。', 'error'); return false; }
  }
  function dateValue(value) { if (!value) return null; const d = new Date(value); return Number.isNaN(d.getTime()) ? null : d; }
  function time(value) { const d = dateValue(value); return d ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''; }
  function when(value) { const d = dateValue(value); if (!d) return ''; const now = new Date(); return d.toDateString() === now.toDateString() ? `今天 ${time(value)}` : `${d.getMonth() + 1}月${d.getDate()}日 ${time(value)}`; }
  function bytes(n) { n = Number(n) || 0; if (n < 1024) return `${n} B`; if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`; return `${(n / 1048576).toFixed(1)} MB`; }
  function name(user) { return user?.display_name || user?.username || '团队成员'; }
  function avatar(user, cls = '') { const n = name(user); const a = el('span', `avatar tone-${[...n].reduce((v, c) => v + c.charCodeAt(0), 0) % 5} ${cls}`, /[\u3400-\u9fff]/.test(n) ? n.slice(-2) : n.slice(0, 2).toUpperCase()); a.title = n; return a; }
  function isOwner() { return state.user?.role === 'owner' || state.project?.role === 'owner'; }
  function canWrite() {
    if (!state.project) return false;
    if (state.project.parent_project_id) return state.project.delegation?.status === 'open' && ['owner', 'editor'].includes(state.project.role);
    return isOwner() || state.project.role === 'editor';
  }
  function parentProjectId() { return state.project?.parent_project_id || state.project?.id; }
  function canAssign() { return canWrite() && !state.project.parent_project_id && state.project.provider !== 'bridge'; }
  function canManageDelegation(task) {
    if (!task || !state.user || !state.project) return false;
    const role = state.project.parent_project_id ? state.project.parent_role : state.project.role;
    // SSE payloads are shared. Never use a can_manage flag computed for another user.
    return role === 'owner' || (role === 'editor' && ([task.created_by, ...(task.creator_ids || [])].some(id => String(id) === String(state.user.id)) || (task.assignees || []).some(user => String(user.id) === String(state.user.id))));
  }
  function projectAPI(suffix = '', pid = state.project?.id) { return `/projects/${encodeURIComponent(pid)}${suffix}`; }
  async function api(path, options = {}) {
    const method = options.method || 'GET'; const headers = new Headers(options.headers || {});
    const signal = options.signal || (['GET', 'HEAD'].includes(method) ? AbortSignal.timeout(20000) : undefined);
    if (!['GET', 'HEAD'].includes(method) && state.csrf) headers.set('X-CSRF-Token', state.csrf);
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
    let response;
    try { response = await fetch(`/api${path}`, { ...options, method, body, headers, signal, credentials: 'same-origin' }); }
    catch (_) { throw new Error(signal?.aborted ? '读取等待超时，请重试。' : '连接暂时中断，请检查网络后重试。'); }
    let data; try { data = await response.json(); } catch (_) { if (signal?.aborted) throw new Error('读取等待超时，请重试。'); data = null; }
    if (!response.ok) {
      if (response.status === 401 && path !== '/login' && path !== '/session') { showLogin(); throw new Error('登录已过期，请重新登录。'); }
      if (response.status === 403 && (data?.code === 'password_change_required' || data?.detail?.code === 'password_change_required')) { if (state.user) state.user.must_change_password = true; showPasswordChange(); throw new Error('请先修改初始密码，再进入工作空间。'); }
      const detail = data?.detail; const message = typeof detail === 'string' ? detail : detail?.message;
      const translated = { 'Current password is incorrect': '当前密码不正确，请重新输入。', 'New passwords do not match': '两次输入的新密码不一致。', 'New password must be 8–256 characters': '新密码需要 8–256 位字符。', 'Choose a new password different from your username and current password': '新密码不能与用户名或当前密码相同。', 'Display name is required': '请输入显示名称。', 'Stop the running task before deleting this project': '请先停止正在运行的任务，再删除项目。' };
      Object.assign(translated, { 'This collaboration task is closed': '协作任务已结束，此会话只读。', 'This collaboration task was cancelled': '协作任务已取消，无法提交汇总。', 'Only the task creator, assignees or project owner can manage this task': '只有发起人、被分派成员和项目管理员可以管理此任务。', 'At most 8 collaboration tasks can be open in one project': '每个项目最多同时开展 8 项协作任务，请先完成或取消已有任务。', 'Assignees must be active editors or owners of this project': '被分派成员的权限已变化，请刷新任务栏后重新选择。', 'Wait for the collaboration agent to finish or stop its pending turns before merging': '请等助手回复和排队消息处理完，再汇总。', 'Write a result summary before merging': '请填写最终摘要后再汇总。', 'Collaboration tasks need a native Codex or Claude project': '此项目的助手暂不支持独立协作会话。', 'The result was already submitted; manage its integration turn in the parent session': '结果已提交，请到主会话查看整合进度。', 'Project queue is full': '主会话队列已满，请稍后提交结果。' });
      const error = new Error(translated[message] || message || `请求未完成（${response.status}），请重试。`); error.status = response.status; throw error;
    }
    return data;
  }
  function setConnection(mode, text) { state.liveConnected = mode === 'connected'; const cls = `connection ${mode}`; if ($('connection').className !== cls) $('connection').className = cls; if ($('connection-text').textContent !== text) $('connection-text').textContent = text; }
  function connectionHealth() {
    if (state.connectionHealth?.epoch !== state.epoch) state.connectionHealth = { epoch: state.epoch, startedAt: Date.now(), sseOpen: false, lastSseSuccess: 0, lastPollSuccess: 0, lastPollAttempt: 0, pollFailures: 0, backlog: false };
    return state.connectionHealth;
  }
  function connectionPresentation(health, now = Date.now(), online = true) {
    if (!online) return { mode: 'reconnecting', text: '等待网络恢复' };
    if (health.sseOpen) return { mode: 'connected', text: '实时同步' };
    if ((health.lastPollSuccess && now - health.lastPollSuccess < 30000) || (health.lastSseSuccess && now - health.lastSseSuccess < 30000)) return { mode: 'connected', text: '同步正常' };
    return { mode: 'reconnecting', text: now - health.startedAt < 30000 ? '正在连接' : '正在重新连接' };
  }
  function renderConnectionHealth() { const presentation = connectionPresentation(connectionHealth(), Date.now(), navigator.onLine !== false); setConnection(presentation.mode, presentation.text); }
  function pollRetryDelay(failures) { return Math.min(30000, 5000 * (2 ** Math.min(Number(failures) || 0, 3))); }
  function stopEvents() { if (state.messageRenderFrame != null) cancelAnimationFrame(state.messageRenderFrame); state.messageRenderFrame = null; state.messageRenderIds?.clear(); state.insightObserver?.disconnect(); state.insightObserver = null; if (state.source) state.source.close(); state.source = null; clearTimeout(state.pollTimer); state.pollTimer = null; clearTimeout(state.reconnectTimer); state.reconnectTimer = null; clearTimeout(state.metaTimer); state.metaTimer = null; clearTimeout(state.insightTimer); state.insightTimer = null; clearInterval(state.heartbeat); state.heartbeat = null; }
  function clearProjectView() {
    closePendingTaskWindow();
    stopEvents(); state.epoch++; state.project = null; state.projectLoading = false; state.messages.clear(); state.turns.clear(); state.tools.clear(); state.files = []; state.members = []; state.presence = []; state.activity = []; state.activityLoaded = false; state.insights = null; state.renderedInsights = null; state.insightsNeedsRefresh = true; state.insightRevision = 0; state.attachments = []; state.uploading = 0; state.sending = false; state.pendingSend = null; state.workspacePath = ''; state.cursor = 0; state.historyMore = true; state.loadingHistory = false;
    resetDelegations();
    ['messages', 'shared-files', 'workspace-files', 'workspace-path', 'member-list', 'activity-list', 'presence', 'project-links', 'insights-summary', 'experiment-list', 'figure-list'].forEach(id => $(id).replaceChildren());
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
    try { const data = await api('/projects'); state.projects = (data.projects || []).filter(p => !p.parent_project_id); renderProjects(); const view = preferredSessionView(); const id = preferId || view?.parent_id || stored('shui-project'); const next = state.projects.find(p => String(p.id) === String(id)) || state.projects[0]; const child = !preferId && String(next?.id) === String(view?.parent_id) ? view?.child_id : null; if (next) await selectProject(child || next.id, child ? next.id : null); else showWelcome(); }
    catch (err) { $('project-list').replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', () => loadProjects(), 'refresh')); setConnection('', '加载失败'); }
  }
  function renderProjects() {
    const query = $('project-search').value.trim().toLowerCase(); const list = state.projects.filter(p => `${p.name} ${p.description || ''}`.toLowerCase().includes(query)); $('project-list').replaceChildren();
    if (!list.length) { $('project-list').append(el('p', 'empty-small', query ? '没有找到这个项目。' : '还没有项目，下一篇研究从这里开始。')); return; }
    list.forEach(p => { const active = String(parentProjectId()) === String(p.id); const b = button('', `project-item${active ? ' active' : ''}`, () => state.project?.parent_project_id ? openProjectWindow(p.id) : selectProject(p.id)); b.setAttribute('aria-current', active ? 'page' : 'false'); const s = el('span', 'project-symbol'); s.append(icon('paper')); const t = el('span', 'project-item-text'); t.append(el('strong', '', p.name), el('small', '', `${providerNames[p.provider] || p.provider} · ${labels[p.status] || '准备就绪'}`)); b.append(s, t); if (p.status === 'running') b.append(el('span', 'project-running-dot')); $('project-list').append(b); });
  }
  function validSessionView(value) {
    const validId = id => typeof id === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(id);
    return value && validId(value.parent_id) && (!value.child_id || validId(value.child_id)) ? { parent_id: value.parent_id, child_id: value.child_id || null } : null;
  }
  function projectViewURL(parentId, childId = null) {
    const url = new URL(location.href); url.searchParams.set('project', parentId); if (childId) url.searchParams.set('session', childId); else url.searchParams.delete('session'); url.hash = ''; return url.href;
  }
  function preferredSessionView() {
    const url = new URL(location.href);
    // Each window owns its URL. A child window must never change where its parent reloads.
    if (url.searchParams.has('project')) return validSessionView({ parent_id: url.searchParams.get('project'), child_id: url.searchParams.get('session') });
    try { return validSessionView(JSON.parse(sessionStorage.getItem('shui-session-view'))); } catch (_) { return null; }
  }
  function rememberSessionView(project) {
    const view = { parent_id: project.parent_project_id || project.id, child_id: project.parent_project_id ? project.id : null };
    try { history.replaceState(null, '', projectViewURL(view.parent_id, view.child_id)); } catch (_) {}
    try { sessionStorage.setItem('shui-session-view', JSON.stringify(view)); } catch (_) {}
    if (!view.child_id) stored('shui-project', view.parent_id);
    else { const member = project.delegation?.session_assignee || (project.delegation?.assignees?.length === 1 ? project.delegation.assignees[0] : null); try { window.name = taskWindowName(view.parent_id, member?.id || view.child_id); } catch (_) {} }
  }
  function clearSessionView() {
    try { sessionStorage.removeItem('shui-session-view'); const url = new URL(location.href); url.searchParams.delete('project'); url.searchParams.delete('session'); history.replaceState(null, '', url.href); } catch (_) {}
  }
  function showWelcome() { clearProjectView(); clearSessionView(); $('welcome').hidden = false; $('project-view').hidden = true; $('breadcrumb-project').textContent = '概览'; $('inspector-empty').hidden = false; ['files', 'insights', 'activity', 'tasks', 'members'].forEach(t => $(`panel-${t}`).hidden = true); $('project-error').hidden = true; $('conversation-empty').hidden = true; renderProjects(); setConnection('connected', '已登录'); }
  async function projectUnavailable(id, message = '项目已删除，或你已没有访问权限。') {
    if (String(state.project?.id) !== String(id)) return;
    const parentId = state.project?.parent_project_id;
    if (parentId) { state.project = null; if (message) toast(message); await selectProject(parentId); return; }
    state.projects = state.projects.filter(project => String(project.id) !== String(id)); showWelcome(); if (message) toast(message); await loadProjects();
  }
  async function selectProject(id, parentId = null) {
    if (!state.user || state.user.must_change_password) return;
    if (String(state.project?.id) === String(id) && !state.projectLoading) { closeDrawers(); return; }
    closePendingTaskWindow();
    const epoch = ++state.epoch; stopEvents(); closeDrawers(); state.projectLoading = true; state.messages.clear(); state.turns.clear(); state.tools.clear(); state.presence = []; state.activity = []; state.activityLoaded = false; state.insights = null; state.renderedInsights = null; state.insightsNeedsRefresh = true; state.insightRevision = 0; state.attachments = []; state.uploading = 0; state.pendingSend = null; state.sending = false; state.workspacePath = ''; state.cursor = 0; state.historyMore = true;
    resetDelegations();
    state.project = state.projects.find(p => String(p.id) === String(id)) || { id, name: '正在加载…', parent_project_id: parentId }; $('message-input').value = ''; $('message-input').style.height = ''; renderUploads(); renderProjects(); renderHeader();
    $('welcome').hidden = true; $('project-view').hidden = false; $('project-error').hidden = true; $('inspector-empty').hidden = true; $('messages').replaceChildren(el('div', 'loading-message', '正在打开共同的讨论…')); $('conversation-empty').hidden = true; $('load-history').hidden = true; $('shared-files').replaceChildren(el('div', 'skeleton')); $('workspace-files').replaceChildren(el('div', 'skeleton')); setConnection('reconnecting', '正在连接'); updateComposer();
    try {
      const d = await api(projectAPI('', id)); if (epoch !== state.epoch) return;
      state.project = d.project; state.files = d.files || []; state.members = d.members || []; state.insights = normalizeInsights(d.insights || d.research || d.project?.insights || d.project?.research || d.project); state.insightsNeedsRefresh = !(d.insights || d.research || d.project?.insights || d.project?.research); state.cursor = Number(d.events_cursor) || 0;
      (d.messages || []).forEach(m => {
        state.messages.set(String(m.id), m);
        if (m.role === 'user' && m.turn_id) state.turns.set(String(m.turn_id), { id: m.turn_id, author_id: m.author?.id, status: m.status });
      });
      if (d.project.active_turn) state.turns.set(String(d.project.active_turn.id), d.project.active_turn);
      state.projectLoading = false; state.historyMore = (d.messages || []).length >= 100; rememberSessionView(d.project); updateProjectInList(d.project); renderHeader(); renderMessages(); renderFiles(); renderMembers(); renderInsights(); renderTab(); updateComposer(); scrollBottom();
      connectionHealth(); connectEvents(epoch); state.lastMetaPoll = Date.now(); pollEvents(epoch); heartbeat(epoch); state.heartbeat = setInterval(() => heartbeat(epoch), 25000); loadWorkspace(''); loadDelegations();
    } catch (err) { if (epoch !== state.epoch) return; if (err.status === 404 || err.status === 403) { await projectUnavailable(id); return; } state.projectLoading = false; $('messages').replaceChildren(); $('project-error').replaceChildren(el('span', '', err.message), button('重试', 'quiet-btn', () => { state.project = null; selectProject(id); }, 'refresh')); $('project-error').hidden = false; setConnection('', '未连接'); updateComposer(); }
  }
  function updateProjectInList(p) { if (!p.parent_project_id) { const i = state.projects.findIndex(x => x.id === p.id); if (i >= 0) state.projects[i] = { ...state.projects[i], ...p }; else state.projects.unshift(p); } renderProjects(); }
  function refreshProjectMetaSoon() {
    clearTimeout(state.metaTimer);
    const epoch = state.epoch;
    state.metaTimer = setTimeout(async () => {
      if (epoch !== state.epoch || !state.project || document.hidden) return;
      if (state.metaRequest?.epoch === epoch) { state.metaRefreshPending = true; return; }
      const request = { epoch }; state.metaRequest = request; state.metaRefreshPending = false;
      try {
        const child = !!state.project?.parent_project_id;
        const d = await api(child ? projectAPI('/meta') : '/projects');
        if (epoch !== state.epoch || !state.project) return;
        if (child) { state.project = { ...state.project, ...d.project }; if (d.members) state.members = d.members; renderHeader(); renderMembers(); loadDelegations(true); return; }
        state.projects = d.projects || [];
        const current = state.projects.find(p => p.id === state.project.id);
        if (!current) { await projectUnavailable(state.project.id); return; }
        state.project = { ...state.project, ...current }; renderHeader();
        renderProjects(); loadDelegations(true);
      } catch (err) { if (epoch === state.epoch && [403, 404].includes(err.status)) await projectUnavailable(state.project?.id); }
      finally { if (state.metaRequest === request) state.metaRequest = null; if (epoch === state.epoch && state.metaRefreshPending) refreshProjectMetaSoon(); }
    }, 250);
  }
  function renderHeader() {
    const p = state.project; if (!p) return; $('project-title').textContent = p.name; $('breadcrumb-project').replaceChildren(); if (p.parent_project_id) { $('breadcrumb-project').append(projectWindowLink(p.parent_project_name || p.delegation?.parent_project_name || '主会话', 'breadcrumb-parent', p.parent_project_id), document.createTextNode(` / ${p.name}`)); } else $('breadcrumb-project').textContent = p.name; const description = p.parent_project_id ? `${p.parent_project_name || '主项目'} · 独立协作窗口` : p.description; $('project-description').textContent = description || ''; $('project-description').hidden = !description;
    $('provider-pill').replaceChildren(icon('spark'), document.createTextNode(providerNames[p.provider] || p.provider || '')); $('project-status').textContent = labels[p.status] || '正在加载'; $('project-model').textContent = p.model || ''; $('project-settings').hidden = !isOwner() || !!p.parent_project_id; $('add-member').hidden = !isOwner() || !!p.parent_project_id;
    const links = $('project-links'); links.replaceChildren();
    [['github_url', 'GitHub'], ['overleaf_url', 'Overleaf']].forEach(([key, label]) => { if (!p[key]) return; let url; try { url = new URL(p[key]); } catch (_) { return; } if (url.protocol !== 'https:' || url.username || url.password) return; const a = el('a', 'project-link', `${label} ↗`); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.setAttribute('aria-label', `打开 ${label} 项目（新窗口）`); links.append(a); });
    links.hidden = !links.childElementCount;
    const turn = p.active_turn; const n = Number(p.queued_count) || 0; const running = turn && !terminalStatuses.has(turn.status);
    $('queue-banner').hidden = !running && !n; $('queue-label').textContent = running ? `正在处理共同的任务${n ? ` · 后面还有 ${n} 条消息` : ''}` : `${n} 条消息等待处理`;
    $('cancel-active').hidden = !running || !canCancel(turn); renderPresence(); renderChildSession(); updateComposer();
  }
  function canCancel(t) { return !!t && canWrite() && !(state.project?.provider === 'bridge' && t.status === 'running') && (isOwner() || String(t.author_id) === String(state.user?.id)); }
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
  function resetDelegations() {
    state.delegations.clear(); state.assignableMembers = []; state.knownMentionMembers = []; state.assignableLoaded = false; state.selectedAssignees = []; state.delegationError = ''; state.delegationLoading = false; state.delegationBusy = false;
    $('assignment-context').value = ''; $('delegation-list').replaceChildren(); $('child-session-bar').hidden = true; $('child-session-context').open = false; $('child-session-context-body').replaceChildren(); $('child-session-context-body').removeAttribute('data-content'); hideMentions(); renderAssignees();
  }
  function mentionPositions(value) {
    const positions = []; let offset = 0; let fence = null; let ticks = 0;
    for (const line of String(value).split('\n')) {
      const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line);
      if (marker) { if (!fence) fence = marker[1]; else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !line.slice(marker[0].length).trim()) fence = null; ticks = 0; offset += line.length + 1; continue; }
      if (fence || /^(?: {4}|\t)/.test(line)) { offset += line.length + 1; continue; }
      const urls = [...line.matchAll(/(?:[a-z][a-z\d+.-]*:\/\/|www\.|mailto:|git@|(?:\.{0,2}\/|[\p{L}\p{N}_.-]+\/))[^\s，；！？<>]*/giu)].map(match => [match.index, match.index + match[0].length]); let urlIndex = 0;
      for (let i = 0; i < line.length; i++) {
        if (line[i] === '\\') { i++; continue; }
        if (line[i] === '`') { let end = i + 1; while (line[end] === '`') end++; const length = end - i; if (!ticks) ticks = length; else if (ticks === length) ticks = 0; i = end - 1; continue; }
        if (ticks || line[i] !== '@') continue;
        while (urlIndex < urls.length && urls[urlIndex][1] <= i) urlIndex++;
        if (urlIndex < urls.length && urls[urlIndex][0] <= i) continue;
        const previous = line[i - 1];
        if (!previous || /[\s（(，,：:;；!?！？\[【「『{《]/u.test(previous) || /\p{Script=Han}/u.test(previous)) positions.push(offset + i);
      }
      offset += line.length + 1;
    }
    return positions;
  }
  function mentionAtCursor(value, cursor) {
    const before = String(value).slice(0, cursor); const positions = mentionPositions(before); const start = positions[positions.length - 1];
    if (start === undefined) return null; const query = before.slice(start + 1);
    return /^[\p{L}\p{N}_.-]{0,40}$/u.test(query) ? { start, end: cursor, query: query.toLowerCase() } : null;
  }
  function assignableMember(member) { return !!member && member.is_active !== false && ['owner', 'editor'].includes(member.role); }
  function knownMemberMentions(value, members) {
    const text = String(value); const candidates = [...members].filter(member => assignableMember(member) && member.username).sort((a, b) => b.username.length - a.username.length);
    const found = [];
    for (const start of mentionPositions(text)) {
      for (const member of candidates) {
        const end = start + 1 + member.username.length;
        if (text.slice(start + 1, end).toLowerCase() !== member.username.toLowerCase()) continue;
        const next = text[end]; const sentencePeriod = next === '.' && /^\.+(?=$|[\s，,。；;！!?？）)\]】\p{Script=Han}])/u.test(text.slice(end));
        if (next && ((/[\p{L}\p{N}_.-]/u.test(next) && !/\p{Script=Han}/u.test(next) && !sentencePeriod) || next === '/' || next === '@')) continue;
        found.push({ start, end, member }); break;
      }
    }
    return found;
  }
  function currentAssignment() {
    const input = $('message-input').value;
    const known = [...new Map([...(state.knownMentionMembers || []), ...state.assignableMembers.filter(assignableMember)].map(member => [String(member.id), member])).values()];
    const typed = canAssign() ? knownMemberMentions(input, known) : [];
    const members = [...new Map([...state.selectedAssignees, ...typed.map(mention => mention.member)].map(member => [String(member.id), member])).values()].sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const invalid = members.filter(member => !state.assignableMembers.some(current => String(current.id) === String(member.id) && assignableMember(current)));
    const pending = canAssign() && state.assignableLoaded === false && mentionPositions(input).length > 0;
    return { members, typed, invalid, pending };
  }
  function assignmentProblem(assignment) {
    if (assignment.pending) return state.delegationLoading ? '正在确认 @ 成员，请稍候…' : '成员列表尚未加载，请刷新任务栏后重试。';
    if (assignment.invalid.length) return `${assignment.invalid.map(member => `@${member.username}`).join('、')} 已无法接收此任务，请移除或重新选择成员。`;
    if (assignment.members.length > 8) return '每项任务最多分派给 8 位成员，请移除多余的 @ 成员。';
    return '';
  }
  function hideMentions() { state.mention = null; state.mentionMatches = []; state.mentionIndex = 0; $('mention-list').hidden = true; $('message-input').setAttribute('aria-expanded', 'false'); $('message-input').removeAttribute('aria-activedescendant'); }
  function updateMentions() {
    const input = $('message-input'); const assignment = currentAssignment();
    if (!canAssign() || state.projectLoading || state.sending || input.selectionStart !== input.selectionEnd || assignment.members.length >= 8) { hideMentions(); return; }
    const mention = mentionAtCursor(input.value, input.selectionStart);
    if (!mention) { hideMentions(); return; }
    const previous = state.mention; state.mention = mention;
    state.mentionMatches = state.assignableMembers.filter(member => assignableMember(member) && !assignment.members.some(user => String(user.id) === String(member.id)) && `${member.username || ''} ${name(member)}`.toLowerCase().includes(mention.query)).slice(0, 8);
    if (!state.mentionMatches.length && assignment.typed.some(match => match.start === mention.start)) { hideMentions(); return; }
    if (!previous || previous.query !== mention.query || previous.start !== mention.start) state.mentionIndex = 0;
    state.mentionIndex = Math.min(state.mentionIndex, Math.max(0, state.mentionMatches.length - 1)); renderMentions();
  }
  function renderMentions() {
    const list = $('mention-list'); list.replaceChildren(); list.hidden = !state.mention; $('message-input').setAttribute('aria-expanded', state.mention ? 'true' : 'false');
    if (!state.mention) return;
    if (!state.mentionMatches.length) { list.append(el('p', 'empty-small', state.delegationLoading ? '正在加载成员…' : state.delegationError ? '成员加载失败，请在任务栏刷新后重试。' : '没有匹配的可分派成员。未选择成员时，@ 会作为普通文字发送。')); $('message-input').removeAttribute('aria-activedescendant'); return; }
    state.mentionMatches.forEach((member, index) => {
      const option = button('', `mention-option${index === state.mentionIndex ? ' selected' : ''}`, () => chooseAssignee(member)); option.id = `mention-option-${index}`; option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === state.mentionIndex)); option.tabIndex = -1;
      const info = el('span', 'mention-info'); info.append(el('strong', '', name(member)), el('small', '', `@${member.username}`)); option.append(avatar(member), info, el('span', 'mention-action', '分派'));
      option.addEventListener('mousedown', event => event.preventDefault()); list.append(option);
    });
    $('message-input').setAttribute('aria-activedescendant', `mention-option-${state.mentionIndex}`);
  }
  function chooseAssignee(member) {
    if (!canAssign() || state.sending || currentAssignment().members.length >= 8 || state.selectedAssignees.some(user => String(user.id) === String(member.id))) return;
    const input = $('message-input'); const mention = state.mention;
    if (!mention || !state.assignableMembers.some(user => String(user.id) === String(member.id))) return;
    input.setRangeText('', mention.start, mention.end, 'end'); state.selectedAssignees.push(member); hideMentions(); renderAssignees(); updateComposer(); input.focus();
  }
  function handleMentionKey(event) {
    if (event.isComposing || !state.mention || event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === 'Escape') { event.preventDefault(); hideMentions(); return true; }
    if (!state.mentionMatches.length) return false;
    if (['ArrowUp', 'ArrowDown'].includes(event.key)) { event.preventDefault(); state.mentionIndex = (state.mentionIndex + (event.key === 'ArrowDown' ? 1 : -1) + state.mentionMatches.length) % state.mentionMatches.length; renderMentions(); return true; }
    if (!event.shiftKey && (event.key === 'Enter' || event.key === 'Tab')) { event.preventDefault(); chooseAssignee(state.mentionMatches[state.mentionIndex]); return true; }
    return false;
  }
  function removeAssignee(member) {
    if (state.sending) return; const input = $('message-input'); const matches = currentAssignment().typed.filter(mention => String(mention.member.id) === String(member.id));
    matches.reverse().forEach(mention => input.setRangeText('', mention.start, mention.end, 'end'));
    state.selectedAssignees = state.selectedAssignees.filter(user => String(user.id) !== String(member.id)); updateComposer(); updateMentions(); input.focus();
  }
  function renderAssignees(assignment = currentAssignment()) {
    const list = $('assignment-chips'); list.replaceChildren(); $('assignment-panel').hidden = !assignment.members.length;
    if (assignment.members.length) list.append(el('span', 'assignment-label', '分派给'));
    assignment.members.forEach(member => {
      const typed = assignment.typed.some(mention => String(mention.member.id) === String(member.id)); const invalid = assignment.invalid.some(user => String(user.id) === String(member.id));
      const chip = el('span', `assignment-chip${invalid ? ' invalid' : ''}`); const remove = iconButton(`移除 ${name(member)}`, 'close', () => removeAssignee(member)); remove.disabled = state.sending;
      chip.append(avatar(member), el('span', '', `@${member.username}`)); if (typed) chip.append(el('small', 'assignment-source', '文本识别')); if (invalid) chip.append(el('small', 'assignment-source', '不可分派')); chip.append(remove); list.append(chip);
    });
  }
  function delegationStatus(task) { return task.status === 'completed' ? '已汇总' : task.status === 'cancelled' ? '已取消' : task.active_turn || task.session_status === 'running' ? '助手处理中' : Number(task.queued_count) > 0 ? '排队中' : '讨论中'; }
  function delegationCard(task, compact = false, request = null) {
    const card = el('article', `delegation-card${compact ? ' compact' : ''}`); card.dataset.delegationId = task.id;
    const head = el('div', 'delegation-card-heading'); head.append(el('strong', '', compact && task.session_assignee ? `@${task.session_assignee.username} 的协作窗口` : request?.title || task.title || '协作任务'), el('span', `delegation-status ${task.status}`, delegationStatus(task))); card.append(head);
    card.append(el('p', 'delegation-assignees', `${(task.assignees || []).map(user => `@${user.username || name(user)}`).join('、')} · 专用助手`));
    if (compact && Number(task.request_count) > 1) card.append(el('p', 'delegation-card-note', `本轮已收到 ${task.request_count} 项任务`));
    if (!compact) card.append(el('p', 'delegation-card-note', task.status === 'completed' ? '最终摘要已交给主会话，完整讨论保留在协作窗口。' : task.status === 'cancelled' ? '任务已取消，讨论记录仍可查看。' : '独立窗口 · 项目成员可查看'));
    if (task.status === 'completed' && task.merge_turn?.status) card.append(el('p', 'delegation-merge-state', `主会话整合：${labels[task.merge_turn.status] || task.merge_turn.status}`));
    const current = String(state.project?.id) === String(task.child_project_id); if (current) card.append(el('span', 'delegation-current', '正在此窗口查看')); else card.append(projectWindowLink('打开协作窗口', 'quiet-btn delegation-open', task.parent_project_id, task.child_project_id, task.session_assignee?.id)); return card;
  }
  function renderDelegations() {
    const list = $('delegation-list'); list.replaceChildren(); const seen = new Set(); const tasks = [...state.delegations.values()].sort((a, b) => Number(b.status === 'open') - Number(a.status === 'open') || String(b.created_at).localeCompare(String(a.created_at))).filter(task => { const child = String(task.child_project_id); if (seen.has(child)) return false; seen.add(child); return true; });
    if (state.delegationError) list.append(el('p', 'empty-small', state.delegationError), button('重新加载', 'quiet-btn', () => loadDelegations(), 'refresh'));
    else if (!tasks.length) list.append(el('p', 'empty-small', state.delegationLoading ? '正在加载任务…' : '暂无协作任务。在主会话输入 @ 选择成员。'));
    tasks.forEach(task => list.append(delegationCard(task, true)));
  }
  async function loadDelegations(quiet = false) {
    if (!state.project || state.delegationLoading) return; const epoch = state.epoch; const pid = parentProjectId(); state.delegationLoading = true; if (!quiet) renderDelegations();
    try {
      const data = await api(projectAPI('/delegations', pid)); if (epoch !== state.epoch) return;
      state.delegationError = ''; state.delegations = new Map((data.delegations || []).map(task => [String(task.id), task])); state.assignableMembers = data.assignable_members || []; state.assignableLoaded = true;
      // Preserve assignment intent across revocation/retry; show invalid recipients
      // instead of silently rerouting the same text into the parent conversation.
      state.knownMentionMembers = [...new Map([...state.knownMentionMembers, ...state.assignableMembers.filter(assignableMember)].map(member => [String(member.id), member])).values()]; renderAssignees();
      if (state.project.parent_project_id) { const task = (data.sessions || []).find(session => String(session.child_project_id) === String(state.project.id))?.delegation || [...state.delegations.values()].sort((a, b) => Number(b.status === 'open') - Number(a.status === 'open') || String(b.created_at).localeCompare(String(a.created_at))).find(task => String(task.child_project_id) === String(state.project.id)); if (task) state.project.delegation = task; }
      else preserveScroll(() => { state.messages.forEach(message => { if (message.delegation) renderMessage(message); }); });
    } catch (err) { if (epoch === state.epoch) state.delegationError = err.message; }
    finally { if (epoch === state.epoch) { state.delegationLoading = false; renderDelegations(); renderChildSession(); updateComposer(); updateMentions(); } }
  }
  function updateDelegation(task) {
    if (!task?.id) return; const old = state.delegations.get(String(task.id)); state.delegations.set(String(task.id), { ...old, ...task });
    if (state.project?.parent_project_id && String(task.child_project_id) === String(state.project.id) && (!state.project.delegation || task.id === state.project.delegation.id || String(task.created_at) > String(state.project.delegation.created_at))) state.project.delegation = { ...state.project.delegation, ...task };
    preserveScroll(() => { state.messages.forEach(message => { if (message.role === 'user' && ((task.merge_turn_id && String(message.turn_id) === String(task.merge_turn_id)) || String(message.merge_delegation?.id) === String(task.id))) { message.merge_delegation = { ...message.merge_delegation, ...task }; renderMessage(message); } else if (String(message.delegation?.id) === String(task.id)) renderMessage(message); }); }); renderDelegations(); renderChildSession(); updateComposer();
  }
  function taskWindowName(parentId, memberId) { return `shui-${parentId}-${memberId}`.replace(/[^a-zA-Z0-9_-]/g, '_'); }
  function memberForChild(childId) { const task = [...state.delegations.values()].find(task => String(task.child_project_id) === String(childId)); return task?.session_assignee?.id || (task?.assignees?.length === 1 ? task.assignees[0].id : childId); }
  function closeTaskWindow(popup) {
    if (state.taskWindowPlaceholders?.has(popup)) { try { if (!popup.closed && popup.location.href === 'about:blank') popup.close(); } catch (_) {} state.taskWindowPlaceholders.delete(popup); }
    if (state.pendingTaskWindow === popup) state.pendingTaskWindow = null;
  }
  function closePendingTaskWindow() { for (const popup of state.taskWindowPlaceholders || []) closeTaskWindow(popup); state.pendingTaskWindow = null; }
  function reserveTaskWindow(message = '正在打开协作会话…', target = '_blank') {
    state.taskWindows ||= new Map(); state.taskWindowPlaceholders ||= new Set(); let popup;
    try {
      const known = target !== '_blank' && state.taskWindows.get(target);
      if (known && !known.closed) return known;
      // Empty URL preserves an existing named window, even after the parent reloads.
      // Sever opener immediately; noopener as a feature would disable named reuse.
      popup = window.open('', target, 'popup,width=1080,height=840');
      if (!popup) return null; const blank = popup.location.href === 'about:blank'; if (blank) state.taskWindowPlaceholders.add(popup); popup.opener = null;
      if (popup.opener !== null) { closeTaskWindow(popup); return null; }
      if (blank) { popup.document.title = 'Shui! · 协作会话'; popup.document.body.textContent = message; }
      if (target !== '_blank') state.taskWindows.set(target, popup); return popup;
    } catch (_) { closeTaskWindow(popup); return null; }
  }
  function navigateTaskWindow(popup, parentId, childId = null) {
    try {
      if (!popup || popup.closed) return false; const target = new URL(projectViewURL(parentId, childId)); const current = new URL(popup.location.href);
      if (current.origin !== target.origin || current.searchParams.get('project') !== String(parentId) || current.searchParams.get('session') !== (childId ? String(childId) : null)) popup.location.replace(target.href);
      state.taskWindowPlaceholders?.delete(popup);
    } catch (_) { return false; }
    try { popup.focus(); } catch (_) {} return true;
  }
  function openProjectWindow(parentId, childId = null, memberId = null) {
    const target = childId ? taskWindowName(parentId, memberId || memberForChild(childId)) : '_blank';
    const popup = reserveTaskWindow('正在打开协作会话…', target); const opened = navigateTaskWindow(popup, parentId, childId);
    if (!opened) { closeTaskWindow(popup); toast('浏览器未打开协作窗口。请允许此站点弹窗，或右键会话链接选择在新窗口打开。', 'error'); } return opened;
  }
  function projectWindowLink(label, cls, parentId, childId = null, memberId = null) {
    const link = el('a', cls, label); link.href = projectViewURL(parentId, childId); link.target = '_blank'; link.rel = 'noopener noreferrer'; link.setAttribute('aria-label', `${label}（独立窗口）`);
    link.addEventListener('click', event => { if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); openProjectWindow(parentId, childId, memberId); }); return link;
  }
  function openDelegation(task) { return openProjectWindow(task.parent_project_id, task.child_project_id, task.session_assignee?.id); }
  function lastCompleteAssistant(messages, task = null) { return [...messages].reverse().find(message => message.role === 'assistant' && message.status === 'complete' && String(message.content || '').trim() && (!task || (message.delegation_id ? String(message.delegation_id) === String(task.id) : !task.created_at || (message.created_at && String(message.created_at) >= String(task.created_at))))); }
  function mergeReadiness(task, project, messages) {
    if (!task || task.status !== 'open') return '任务已结束，讨论记录保留。';
    if ((project.active_turn && activeStatuses.has(project.active_turn.status)) || (task.active_turn && activeStatuses.has(task.active_turn.status)) || Number(project.queued_count) > 0 || Number(task.queued_count) > 0) return '请等助手回复和排队消息处理完，再汇总。';
    if (!lastCompleteAssistant(messages, task)) return '助手完成本轮任务的回复后，才可汇总结果。';
    return '';
  }
  function renderChildSession() {
    const project = state.project; const task = project?.delegation; $('child-session-bar').hidden = !project?.parent_project_id; if (!project?.parent_project_id) return;
    $('return-parent').href = projectViewURL(project.parent_project_id);
    $('child-session-info').textContent = task ? `${(task.assignees || []).map(user => `@${user.username || name(user)}`).join('、')} 与专用助手在此窗口讨论；主会话留在原窗口，项目成员都能查看。` : '正在打开独立协作窗口…';
    const context = $('child-session-context-body'); const contextKey = JSON.stringify([task?.context, task?.attachment_ids]); $('child-session-context').hidden = !task?.context && !task?.attachments?.length;
    if (context.dataset.content !== contextKey) { context.dataset.content = contextKey; context.replaceChildren(); if (task?.context) context.append(el('p', '', task.context)); if (task?.attachments?.length) { const files = el('div', 'message-attachments'); task.attachments.forEach(file => files.append(button(file.name, 'attachment-chip', () => openFile(file), 'paper'))); context.append(files); } }
    $('child-session-status').textContent = task ? delegationStatus(task) : '正在加载'; $('child-session-status').className = `delegation-status ${task?.status || ''}`;
    const manage = canManageDelegation(task); const reason = mergeReadiness(task, project, sortedMessages());
    $('finish-delegation').hidden = !manage || task?.status !== 'open'; $('cancel-delegation').hidden = !manage || task?.status !== 'open';
    $('finish-delegation').disabled = !!reason || state.delegationBusy; $('finish-delegation').title = reason || '编辑最终摘要并交给主会话'; $('cancel-delegation').disabled = state.delegationBusy;
    $('child-session-help').textContent = !task ? '' : task.status !== 'open' ? task.status === 'completed' ? `最终摘要已提交主会话${task.merge_turn?.status ? `（${labels[task.merge_turn.status] || task.merge_turn.status}）` : ''}，此会话已只读。` : '任务已取消，此会话已只读。' : !canWrite() ? '你可以查看此会话。被分派成员、发起人和项目管理员可以讨论。' : reason || '只把最终摘要和成果引用带回主会话。';
  }
  function finishDelegation() {
    const task = state.project?.delegation; if (!canManageDelegation(task) || mergeReadiness(task, state.project, sortedMessages())) return;
    const epoch = state.epoch; const last = lastCompleteAssistant(sortedMessages(), task); const client_id = uuid();
    openDialog('完成任务并汇总', '协作任务', body => { body.append(el('p', 'form-help', '确认最终结论和成果链接。主会话只接收这份摘要，完整讨论留在此会话。')); field(body, '最终摘要', 'delegation-summary', { tag: 'textarea', required: true, maxLength: 12000, value: String(last.content).slice(0, 12000) }); }, async () => {
      const summary = $('delegation-summary').value.trim(); if (!summary) throw new Error('请填写最终摘要。');
      const data = await api(projectAPI(`/delegations/${encodeURIComponent(task.id)}/merge`, task.parent_project_id), { method: 'POST', body: { summary, client_id } });
      if (epoch !== state.epoch) return; updateDelegation(data.delegation); toast('最终摘要已提交主会话。此窗口保留完整讨论记录。'); refreshProjectMetaSoon();
    }, '提交摘要');
  }
  function cancelDelegation() {
    const task = state.project?.delegation; if (!canManageDelegation(task) || task.status !== 'open') return; const epoch = state.epoch;
    openDialog('取消协作任务', '协作任务', body => { body.append(el('p', 'form-help', '停止此会话的助手和排队任务，保留讨论记录。主会话继续运行。')); }, async () => {
      const data = await api(projectAPI(`/delegations/${encodeURIComponent(task.id)}/cancel`, task.parent_project_id), { method: 'POST', body: {} });
      if (epoch !== state.epoch) return; updateDelegation(data.delegation); toast('协作任务已取消，讨论记录已保留。'); refreshProjectMetaSoon();
    }, '确认取消任务');
  }
  function updateComposer() {
    const available = canWrite() && !state.projectLoading; const assignment = currentAssignment(); const assigning = assignment.members.length > 0; const problem = assignmentProblem(assignment); renderAssignees(assignment);
    $('message-input').disabled = !available || (assigning && state.sending); $('attach-button').disabled = !available || state.sending || state.uploading > 0; $('assignment-context').disabled = !available || state.sending;
    $('send-message').disabled = !available || state.sending || state.uploading > 0 || !!problem || (!$('message-input').value.trim() && (assigning || !state.attachments.length)); $('send-message-label').textContent = assigning ? '分派任务' : assignment.pending ? '确认成员中' : '发送';
    $('message-input').placeholder = !canWrite() && state.project ? '此会话只读' : assigning ? '描述任务目标、交付成果和约束…' : canAssign() ? '发送消息，@ 成员分派任务，或粘贴图片…' : '发送消息，或粘贴图片…';
    $('composer-note').textContent = !available && state.project && !state.projectLoading ? '只读权限' : state.sending ? '正在发送…' : problem || (assigning ? `将分别分派给 ${assignment.members.length} 位成员 · 复用各自的协作窗口` : canAssign() ? '输入完整 @用户名 即可分派，也可从提示列表选择 · Ctrl/Cmd+Enter 发送' : '');
  }
  function nearBottom() { const c = $('conversation'); return c.scrollHeight - c.scrollTop - c.clientHeight < 110; }
  function scrollBottom() { requestAnimationFrame(() => { const c = $('conversation'); c.scrollTop = c.scrollHeight; $('jump-latest').hidden = true; }); }
  function preserveScroll(fn, force = false) { const bottom = nearBottom(); fn(); if (bottom || force) scrollBottom(); else $('jump-latest').hidden = false; }
  function sortedMessages() { return [...state.messages.values()].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id))); }
  function queueMessageRender(id) {
    state.messageRenderIds ||= new Set(); state.messageRenderIds.add(String(id));
    if (state.messageRenderFrame != null) return; const epoch = state.epoch;
    state.messageRenderFrame = requestAnimationFrame(() => {
      state.messageRenderFrame = null; const ids = [...state.messageRenderIds]; state.messageRenderIds.clear(); if (epoch !== state.epoch) return;
      if (ids.length) preserveScroll(() => ids.forEach(key => { const message = state.messages.get(key); if (message) renderMessage(message); }));
    });
  }
  function renderMessages() { state.messageRenderIds?.clear(); const msgs = sortedMessages(); $('messages').replaceChildren(); msgs.forEach(m => $('messages').append(messageNode(m))); $('conversation-empty').hidden = !!msgs.length; $('load-history').hidden = !state.historyMore || !msgs.length; renderChildSession(); }
  function renderMessage(m) { state.messageRenderIds?.delete(String(m.id)); const old = $('messages').querySelector(`[data-message-id="${CSS.escape(String(m.id))}"]`); const n = messageNode(m); if (old) old.replaceWith(n); else { const next = sortedMessages().find(x => String(x.created_at) > String(m.created_at)); const nextNode = next && $('messages').querySelector(`[data-message-id="${CSS.escape(String(next.id))}"]`); $('messages').insertBefore(n, nextNode || null); } $('conversation-empty').hidden = true; renderChildSession(); }
  function safeHref(href) {
    let path = href.startsWith('<') && href.endsWith('>') ? href.slice(1, -1) : href;
    path = path.replace(/\\([\\()[\]<> ])/g, '$1');
    if (!path || /[\u0000-\u001f\u007f]/.test(path) || path.startsWith('//')) return null;
    if (/^https?:\/\//i.test(path)) { try { const url = new URL(path); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch (_) { return null; } }
    if (path.startsWith('/api/')) return path;
    if (/^[a-z][a-z0-9+.-]*:/i.test(path) && !/^sandbox:\//i.test(path) && !/^file:\/\//i.test(path)) return null;
    if (/^file:/i.test(path) && !/^file:\/\/\//i.test(path)) return null;
    if (/^[?#]/.test(path)) return null;
    // Agent source links may include a line anchor; preserve all other path bytes.
    path = path.replace(/(?::\d+(?::\d+)?|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$/, '');
    return state.project?.id ? artifactURL(path) : null;
  }
  function isDownloadHref(href) { return /^\/api\/projects\/[^/]+\/(?:artifact\?|files\/[^/?]+(?:\?|$))/.test(href); }
  function handleDownloadLink(event, href, filename) {
    if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); download(href, filename);
  }
  function inline(parent, text) {
    const regex = /(\*\*([^*\n]+)\*\*|`([^`\n]+)`|\[([^\]\n]+)\]\((<[^>\n]+>|(?:\\.|[^\s()\\]|\((?:\\.|[^()\\])*\))+)(?:\s+"[^"\n]*")?\))/g; let match; let last = 0;
    while ((match = regex.exec(text))) { parent.append(document.createTextNode(text.slice(last, match.index))); if (match[2]) parent.append(el('strong', '', match[2])); else if (match[3]) parent.append(el('code', '', match[3])); else { const href = safeHref(match[5]); if (href) { const a = el('a', '', match[4]); a.href = href; if (isDownloadHref(href)) { a.download = ''; a.addEventListener('click', event => handleDownloadLink(event, href)); } else { a.target = '_blank'; a.rel = 'noopener noreferrer'; } parent.append(a); } else parent.append(document.createTextNode(match[0])); } last = regex.lastIndex; }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function markdown(text) {
    const body = el('div', 'message-text'); const chunks = String(text || '').split(/(```[^\n]*\n[\s\S]*?(?:```|$))/g);
    chunks.forEach(chunk => { if (!chunk) return; if (chunk.startsWith('```')) { const content = chunk.replace(/^```[^\n]*\n/, '').replace(/```$/, ''); const pre = el('pre'); pre.append(el('code', '', content)); body.append(pre); return; }
      chunk.split(/\n\s*\n/).filter(Boolean).forEach(block => { if (/^#{1,4} /.test(block) && !block.includes('\n')) { const h = el('h3'); inline(h, block.replace(/^#{1,4} /, '')); body.append(h); } else if (block.split('\n').every(l => /^\s*(?:[-*]|\d+\.)\s/.test(l))) { const list = el(/^\s*\d+\./.test(block) ? 'ol' : 'ul'); block.split('\n').forEach(l => { const item = el('li'); inline(item, l.replace(/^\s*(?:[-*]|\d+\.)\s/, '')); list.append(item); }); body.append(list); } else { const p = el('p'); inline(p, block); body.append(p); } });
    }); return body;
  }
  function visibleMessageContent(message) {
    const task = message.merge_delegation;
    return task ? `协作结果${task.title ? `：${task.title}` : ''}\n\n${task.summary || ''}`.trim() : String(message.content || '');
  }
  function messageAttachmentGroup(files) {
    const group = el('div', 'message-attachments'); files.forEach(file => { const item = button('', 'attachment-chip', () => openFile(file)); item.append(icon('paper'), el('span', '', file.name), el('small', '', bytes(file.size))); group.append(item); }); return group;
  }
  function requestBriefNode(message) {
    const request = message.delegation_request;
    if (!state.project?.parent_project_id || message.role !== 'user' || !request) return null;
    const attachments = message.attachments?.length ? message.attachments : request.attachments || [];
    if (!request.context && !attachments.length) return null;
    const details = el('details', 'delegation-request-brief'); details.append(el('summary', '', '本次任务背景与附件'));
    if (request.context) details.append(el('p', 'delegation-request-context', request.context));
    if (attachments.length) details.append(messageAttachmentGroup(attachments));
    return details;
  }
  function messageNode(m) {
    const node = el('article', `message ${m.role || 'user'}${m.status === 'running' && m.role === 'assistant' ? ' streaming' : ''}`); node.dataset.messageId = m.id;
    const a = m.role === 'user' ? avatar(m.author, 'message-avatar') : el('span', 'avatar message-avatar'); if (m.role !== 'user') a.append(icon(m.role === 'system' ? 'notebook' : 'spark')); node.append(a);
    const body = el('div', 'message-body'); const meta = el('div', 'message-meta'); meta.append(el('strong', 'message-author', m.role === 'assistant' ? (providerNames[state.project?.provider] || '研究助手') : m.role === 'system' ? '工作空间' : name(m.author)), el('time', 'message-time', when(m.created_at)));
    if (m.status && m.status !== 'complete') meta.append(el('span', `message-status ${m.status}`, labels[m.status] || m.status)); body.append(meta);
    const content = visibleMessageContent(m);
    body.append(markdown(content || (m.role === 'assistant' && m.status === 'queued' ? '等待前面的任务完成…' : '')));
    if (m.merge_delegation) body.append(projectWindowLink('查看完整协作会话', 'quiet-btn delegation-open', m.merge_delegation.parent_project_id, m.merge_delegation.child_project_id, m.merge_delegation.session_assignee?.id));
    if (m.delegation) body.append(delegationCard(state.delegations.get(String(m.delegation.id)) || m.delegation, false, m.delegation_request));
    const brief = requestBriefNode(m); if (brief) body.append(brief); else if (m.attachments?.length) body.append(messageAttachmentGroup(m.attachments));
    if (m.error) body.append(el('div', 'message-error', m.error));
    const t = state.turns.get(String(m.turn_id)) || { id: m.turn_id, status: m.status, author_id: m.author?.id };
    if (m.role === 'assistant' && state.tools.has(String(m.turn_id))) { const ts = el('div', 'tool-events'); state.tools.get(String(m.turn_id)).slice(-6).forEach(tool => { const detail = el('details', 'tool-event'); const summary = el('summary'); summary.append(icon(tool.status === 'complete' ? 'check' : 'tool'), el('span', '', `${tool.name || '工具'} · ${labels[tool.status] || tool.status || '处理中'}`)); detail.append(summary); if (tool.content) detail.append(el('pre', '', String(tool.content).slice(0, 6000))); ts.append(detail); }); body.append(ts); }
    const actions = el('div', 'message-actions');
    if (content.trim()) actions.append(button('复制', 'quiet-btn message-copy', () => copyMessageText(content)));
    if (m.turn_id && m.role === 'user' && activeStatuses.has(t.status) && canCancel(t)) actions.append(button(t.status === 'queued' ? '取消排队' : '停止任务', 'quiet-btn', () => cancelTurn(t.id), 'stop'));
    if (m.turn_id && m.role === 'user' && ['failed', 'interrupted', 'cancelled'].includes(t.status) && canRetry(t)) actions.append(button('重新尝试', 'quiet-btn', () => retryTurn(t.id), 'refresh'));
    if (actions.childElementCount) body.append(actions);
    node.append(body); return node;
  }
  async function loadHistory() { if (!state.project || state.loadingHistory || !state.historyMore) return; const msgs = sortedMessages(); if (!msgs.length) return; const epoch = state.epoch; const c = $('conversation'); const beforeHeight = c.scrollHeight; state.loadingHistory = true; $('load-history').disabled = true; try { const d = await api(projectAPI(`/messages?before=${encodeURIComponent(msgs[0].created_at)}&limit=100`)); if (epoch !== state.epoch) return; const oldTop = c.scrollTop; const n = (d.messages || []).length; (d.messages || []).forEach(m => state.messages.set(String(m.id), m)); state.historyMore = n >= 100; renderMessages(); c.scrollTop = oldTop + c.scrollHeight - beforeHeight; } catch (err) { toast(err.message, 'error'); } finally { state.loadingHistory = false; $('load-history').disabled = false; } }
  async function sendMessage(event) {
    event?.preventDefault(); if (!canWrite() || state.sending || state.uploading || state.projectLoading) return; const content = $('message-input').value.trim(); const attachment_ids = state.attachments.map(f => f.id); if (!content && !attachment_ids.length) return;
    const assignment = currentAssignment(); const problem = assignmentProblem(assignment); if (problem) { updateComposer(); toast(problem, 'error'); return; }
    const assignee_ids = assignment.members.map(user => user.id); const assigning = assignee_ids.length > 0;
    if (assigning && (!canAssign() || !content)) return;
    if (assigning && content.length > 12000) { toast('任务说明最多 12000 字，请精简后再分派。', 'error'); return; }
    if (assigning && attachment_ids.length > 12) { toast('每项协作任务最多附带 12 个文件，请先移除多余附件。', 'error'); return; }
    const context = assigning ? $('assignment-context').value.trim() : ''; const pid = state.project.id; const epoch = state.epoch; const signature = JSON.stringify({ pid, content, attachment_ids, assignee_ids, context }); if (!state.pendingSend || state.pendingSend.signature !== signature) state.pendingSend = { signature, client_id: uuid() };
    const taskWindows = assigning ? assignment.members.map(member => ({ member_id: String(member.id), popup: reserveTaskWindow('正在分派任务，请稍候…', taskWindowName(pid, member.id)) })) : []; const navigated = new Set(); if (assigning) state.pendingTaskWindow = taskWindows[0]?.popup || null;
    const client_id = state.pendingSend.client_id; state.sending = true; hideMentions(); renderAssignees(); updateComposer();
    try {
      const d = await api(projectAPI(assigning ? '/delegations' : '/messages', pid), { method: 'POST', body: { content, attachment_ids, client_id, ...(assigning ? { assignee_ids, context } : {}) } }); if (epoch !== state.epoch) return;
      const deliveries = assigning && d.assignments?.length ? d.assignments : [d]; state.pendingSend = null;
      deliveries.forEach(delivery => { if (delivery.message) state.messages.set(String(delivery.message.id), delivery.message); if (delivery.turn && !assigning) updateTurn(delivery.turn); if (delivery.delegation) updateDelegation(delivery.delegation); }); preserveScroll(() => deliveries.forEach(delivery => { if (delivery.message) renderMessage(delivery.message); }), true);
      if ($('message-input').value.trim() === content) { $('message-input').value = ''; $('message-input').style.height = ''; } state.attachments = []; state.selectedAssignees = []; $('assignment-context').value = ''; renderAssignees(); renderUploads();
      if (assigning) {
        deliveries.forEach((delivery, index) => { const task = delivery.delegation; if (!task) return; const memberId = task.session_assignee?.id || (task.assignees?.length === 1 ? task.assignees[0].id : assignee_ids[index]); const reserved = taskWindows.find(item => item.member_id === String(memberId)); if (reserved && navigateTaskWindow(reserved.popup, task.parent_project_id, task.child_project_id)) navigated.add(reserved.popup); });
        state.pendingTaskWindow = null; const missing = deliveries.length - navigated.size; toast(missing > 0 ? '任务已分派。部分窗口被浏览器阻止，请点击对应任务卡「打开协作窗口」。' : '任务已送到各成员的专用协作窗口，主会话保留在这里。', missing > 0 ? 'error' : '');
      } else $('message-input').focus();
    }
    catch (err) { toast(`${err.message} 消息已保留，再次发送会安全重试。`, 'error'); }
    finally { taskWindows.forEach(item => { if (!navigated.has(item.popup)) closeTaskWindow(item.popup); }); if (epoch === state.epoch) { state.sending = false; renderAssignees(); updateComposer(); } }
  }
  async function cancelTurn(id) { try { await api(projectAPI(`/turns/${encodeURIComponent(id)}/cancel`), { method: 'POST', body: {} }); toast('已发出停止请求。'); } catch (err) { toast(err.message, 'error'); } }
  async function retryTurn(id) { const pid = state.project?.id; const epoch = state.epoch; try { const d = await api(projectAPI(`/turns/${encodeURIComponent(id)}/retry`, pid), { method: 'POST', body: { client_id: uuid() } }); if (epoch !== state.epoch) return; if (d.turn) updateTurn(d.turn); if (d.message) { state.messages.set(String(d.message.id), d.message); preserveScroll(() => renderMessage(d.message), true); } toast('已重新加入任务队列。'); } catch (err) { toast(err.message, 'error'); } }
  async function uploadFiles(fileList) {
    if (state.sending) { toast('消息正在发送，请稍后再添加或粘贴图片。', 'error'); return; }
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
  function clipboardImageFiles(data) {
    // files and items normally describe the same images. Use one source only.
    const files = Array.from(data?.files || []).filter(file => file.type.startsWith('image/'));
    if (files.length) return files;
    return Array.from(data?.items || []).filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean);
  }
  function handleComposerPaste(event) {
    if (event.defaultPrevented || !canWrite() || state.projectLoading || $('message-input').disabled) return;
    const files = clipboardImageFiles(event.clipboardData); if (!files.length) return;
    const text = event.clipboardData.getData('text/plain'); event.preventDefault();
    if (text) { const input = $('message-input'); input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input', { bubbles: true })); }
    uploadFiles(files);
  }
  function renderUploads(uploadingName) { $('upload-list').replaceChildren(); state.attachments.forEach(f => { const chip = el('div', 'upload-item'); chip.append(icon('paper'), el('span', '', f.name), iconButton('移除附件', 'close', () => { state.attachments = state.attachments.filter(x => x.id !== f.id); renderUploads(); updateComposer(); })); $('upload-list').append(chip); }); if (state.uploading) { const chip = el('div', 'upload-item uploading'); chip.append(icon('upload'), el('span', '', `正在上传${uploadingName ? ` ${uploadingName}` : '…'}`)); $('upload-list').append(chip); } }
  function localFileURL(f) { return `/api${projectAPI(`/files/${encodeURIComponent(f.id)}`, f.project_id || state.project.id)}`; }
  function previewableName(filename) { return /\.(?:pdf|png|jpe?g|webp|gif|svg)$/i.test(String(filename || '')); }
  function artifactURL(path) { return `/api${projectAPI(`/artifact?path=${encodeURIComponent(path)}`)}`; }
  function figureDownloadURL(figure, path) {
    const direct = figure?.download_url || figure?.url;
    return typeof direct === 'string' && direct.startsWith('/api/') ? direct : path ? artifactURL(path) : '';
  }
  function openFigure(figure) {
    const path = figure.path || figure.file?.path || figure.file?.relative_path; const url = figureDownloadURL(figure, path); if (!url) return;
    const filename = figure.title || path?.split('/').pop() || 'figure'; const mime = String(figure.mime_type || '').toLowerCase(); const image = /image\//.test(mime) || previewableName(path || filename); const pdf = mime === 'application/pdf' || /\.pdf$/i.test(path || filename); const inline = `${url}${url.includes('?') ? '&' : '?'}inline=1`;
    if (pdf) previewPDF(filename, inline, url); else if (image) previewImage(filename, inline, url); else download(url, filename);
  }
  function openArtifact(filename, path) { const url = artifactURL(path); if (/\.pdf$/i.test(filename)) previewPDF(filename, `${url}&inline=1`, url); else if (previewableName(filename)) previewImage(filename, `${url}&inline=1`, url); else download(url, filename); }
  function openFile(f) { const url = localFileURL(f); if (/\.pdf$/i.test(f.name)) previewPDF(f.name, `${url}?inline=1`, url); else if (previewableName(f.name)) previewImage(f.name, `${url}?inline=1`, url); else download(url, f.name); }
  async function download(url, filename) {
    state.pendingDownloads ||= new Map(); if (state.pendingDownloads.has(url)) return;
    const controller = new AbortController(); state.pendingDownloads.set(url, controller);
    const notice = el('div', 'toast download-progress'); const cancel = el('button', 'quiet-btn', '取消'); cancel.type = 'button'; cancel.addEventListener('click', () => controller.abort('cancelled')); notice.append(el('span', '', '正在准备下载…'), cancel); $('toasts').append(notice);
    const timeout = setTimeout(() => controller.abort('timeout'), 15000);
    try {
      // Check errors without buffering the file; the browser streams the GET to disk.
      let response; try { response = await fetch(url, { method: 'HEAD', credentials: 'same-origin', signal: controller.signal }); } catch (_) { if (controller.signal.reason === 'cancelled') return; throw new Error(controller.signal.aborted ? '下载连接等待超时，请重试。' : '连接暂时中断，请检查网络后重试。'); }
      if (!response.ok) {
        if (response.status === 401) { showLogin(); throw new Error('登录已过期，请重新登录后下载。'); }
        const errors = { 400: '文件路径无效，请让助手重新生成下载链接。', 403: '没有权限下载该文件。', 404: '文件不存在，请让助手检查文件或重新生成。' };
        throw new Error(errors[response.status] || `下载未完成（${response.status}），请重试。`);
      }
      const a = el('a'); a.href = url; a.download = filename || ''; document.body.append(a); a.click(); a.remove();
    } catch (err) { toast(err.message, 'error'); }
    finally { clearTimeout(timeout); notice.remove(); if (state.pendingDownloads.get(url) === controller) state.pendingDownloads.delete(url); }
  }
  const maxClipboardImageBytes = 20 * 1024 * 1024;
  const maxClipboardImagePixels = 16 * 1024 * 1024;
  let previewGeneration = 0;
  function checkClipboardImageSize(width, height) {
    if (!width || !height || width > 8192 || height > 8192 || width * height > maxClipboardImagePixels) throw new Error('图片尺寸过大，请下载后复制。');
  }
  async function readClipboardImageBlob(response) {
    if (Number(response.headers.get('content-length')) > maxClipboardImageBytes) { response.body?.cancel().catch(() => {}); throw new Error('图片超过 20 MB，请下载后复制。'); }
    const type = String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(type)) { response.body?.cancel().catch(() => {}); throw new Error('此图片暂不支持直接复制，请右键复制图片或下载。'); }
    if (!response.body?.getReader) { const blob = await response.blob(); if (blob.size > maxClipboardImageBytes) throw new Error('图片超过 20 MB，请下载后复制。'); return blob; }
    const reader = response.body.getReader(); const chunks = []; let size = 0;
    try {
      while (true) { const chunk = await reader.read(); if (chunk.done) break; size += chunk.value.byteLength; if (size > maxClipboardImageBytes) { await reader.cancel(); throw new Error('图片超过 20 MB，请下载后复制。'); } chunks.push(chunk.value); }
      return new Blob(chunks, { type });
    } finally { reader.releaseLock(); }
  }
  async function previewImageToPngBlob(src) {
    const url = new URL(src, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) throw new Error('此图片请右键复制图片或下载。');
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 20000);
    let blob;
    try { const response = await fetch(url.href, { credentials: 'same-origin', redirect: 'error', signal: controller.signal }); if (!response.ok) throw new Error('图片读取失败，请重新打开预览后重试。'); blob = await readClipboardImageBlob(response); }
    finally { clearTimeout(timeout); }
    const header = new Uint8Array(await blob.slice(0, 32).arrayBuffer());
    const signature = (offset, values) => values.every((byte, i) => header[offset + i] === byte);
    if (blob.type === 'image/png') {
      if (header.length < 24 || !signature(0, [137, 80, 78, 71, 13, 10, 26, 10]) || !signature(12, [73, 72, 68, 82])) throw new Error('图片读取失败，请右键复制图片或下载。');
      const view = new DataView(header.buffer); checkClipboardImageSize(view.getUint32(16), view.getUint32(20)); return blob;
    }
    const validRaster = blob.type === 'image/jpeg' ? signature(0, [255, 216, 255]) : blob.type === 'image/gif' ? signature(0, [71, 73, 70, 56]) && [55, 57].includes(header[4]) && header[5] === 97 : blob.type === 'image/webp' && signature(0, [82, 73, 70, 70]) && signature(8, [87, 69, 66, 80]);
    if (!validRaster) throw new Error('图片读取失败，请右键复制图片或下载。');
    const preview = $('figure-preview'); if (preview.src === src && preview.naturalWidth) checkClipboardImageSize(preview.naturalWidth, preview.naturalHeight);
    const objectURL = URL.createObjectURL(blob); const image = new Image(); const canvas = document.createElement('canvas');
    try {
      // Decode only raster MIME types; never load SVG or HTML clipboard content.
      image.src = objectURL; await image.decode(); checkClipboardImageSize(image.naturalWidth, image.naturalHeight);
      canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
      const context = canvas.getContext('2d'); if (!context) throw new Error('无法复制图片，请右键复制图片或下载。');
      context.drawImage(image, 0, 0);
      const png = await new Promise((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('图片转换失败，请右键复制图片或下载。')), 'image/png'));
      if (png.size > maxClipboardImageBytes) throw new Error('图片超过 20 MB，请下载后复制。'); return png;
    } finally { image.removeAttribute('src'); URL.revokeObjectURL(objectURL); canvas.width = 0; canvas.height = 0; }
  }
  async function copyPreviewImage() {
    const image = $('figure-preview'); const copy = $('preview-copy-image');
    if (!$('preview-dialog').open || image.hidden || !image.getAttribute('src') || copy.disabled) return false;
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined' || (ClipboardItem.supports && !ClipboardItem.supports('image/png'))) { toast('当前浏览器无法直接复制图片，请右键复制图片或下载。', 'error'); return false; }
    const generation = previewGeneration; copy.disabled = true; copy.textContent = '正在复制…';
    try {
      const png = previewImageToPngBlob(image.src).then(blob => { if (generation !== previewGeneration || !$('preview-dialog').open) throw new Error('预览已关闭，请重新打开图片后复制。'); return blob; });
      // Call write during the original click/key gesture, including on Safari.
      // Handle the conversion promise even if a browser rejects write immediately.
      png.catch(() => {});
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
      if (generation === previewGeneration) toast('已复制图片。'); return true;
    } catch (err) { if (generation === previewGeneration) toast(/^(图片|此图片|无法复制|预览已)/.test(err.message) ? err.message : '复制未完成，请右键复制图片或下载。', 'error'); return false; }
    finally { if (generation === previewGeneration) { copy.disabled = false; copy.textContent = '复制图片'; } }
  }
  function handlePreviewCopyShortcut(event) {
    if (event.defaultPrevented || event.repeat || event.isComposing || !(event.ctrlKey || event.metaKey) || event.shiftKey || event.altKey || event.key.toLowerCase() !== 'c') return;
    if (!$('preview-dialog').open || $('figure-preview').hidden || $('preview-copy-image').disabled) return;
    const target = event.target; const active = document.activeElement;
    if ([target, active].some(node => node instanceof Element && (node.matches('input, textarea, select') || node.isContentEditable))) return;
    if (window.getSelection()?.toString()) return;
    event.preventDefault(); copyPreviewImage();
  }
  function preparePreview(filename, downloadURL) { previewGeneration++; $('preview-copy-image').hidden = true; $('preview-copy-image').disabled = false; $('preview-copy-image').textContent = '复制图片'; $('preview-copy-image').title = '复制图片（Ctrl/Cmd+C）'; $('preview-help').textContent = '无法显示预览时，可下载文件后查看。'; $('preview-title').textContent = filename; $('preview-download').href = downloadURL; $('preview-download').download = filename; $('pdf-preview').hidden = true; $('figure-preview').hidden = true; $('pdf-preview').removeAttribute('src'); $('figure-preview').removeAttribute('src'); if (!$('preview-dialog').open) $('preview-dialog').showModal(); }
  function previewPDF(filename, inlineURL, downloadURL) { preparePreview(filename, downloadURL); $('pdf-preview').hidden = false; $('pdf-preview').src = inlineURL; }
  function previewImage(filename, inlineURL, downloadURL) {
    preparePreview(filename, downloadURL); $('figure-preview').hidden = false; $('figure-preview').alt = filename; $('figure-preview').src = inlineURL;
    const url = new URL(inlineURL, location.href); const svg = /\.svg$/i.test(filename) || /\.svg$/i.test(url.pathname) || /\.svg$/i.test(url.searchParams.get('path') || '');
    $('preview-copy-image').hidden = false; $('preview-copy-image').disabled = svg;
    $('preview-copy-image').title = svg ? '此图片请右键复制图片或下载' : '复制图片（Ctrl/Cmd+C）';
    $('preview-help').textContent = svg ? '此图片可右键复制或下载。' : 'Ctrl/Cmd+C 复制图片；选中文字时复制文字。';
  }
  function renderFiles() { $('shared-files').replaceChildren(); if (!state.files.length) $('shared-files').append(el('p', 'empty-small', '还没有共享文件。通过对话框上传论文、图片或结果。')); state.files.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).forEach(f => $('shared-files').append(fileRow(f.name, f.size, 'file', () => openFile(f), () => download(localFileURL(f), f.name)))); }
  function fileRow(filename, size, type, open, save) { const row = el('div', 'file-row'); const ext = filename.includes('.') ? filename.split('.').pop().slice(0, 4).toUpperCase() : 'FILE'; const mark = el('span', `file-type ${type === 'directory' ? 'directory' : ext.toLowerCase()}`); if (type === 'directory') mark.append(icon('folder')); else mark.textContent = ext; const info = button('', 'file-row-info', open); info.append(el('span', 'file-name', filename), el('span', 'file-size', type === 'directory' ? '文件夹' : bytes(size))); row.append(mark, info); if (save) row.append(iconButton('下载文件', 'download', save)); return row; }
  async function refreshFiles() { const epoch = state.epoch; if (!state.project) return; try { const d = await api(projectAPI('/files')); if (epoch !== state.epoch) return; state.files = d.files || []; renderFiles(); } catch (err) { toast(err.message, 'error'); } }
  async function loadWorkspace(path = '') {
    if (!state.project) return; const epoch = state.epoch; state.workspacePath = path; $('workspace-files').replaceChildren(el('p', 'empty-small', '正在打开…'));
    try { const d = await api(projectAPI(`/workspace?path=${encodeURIComponent(path)}`)); if (epoch !== state.epoch || path !== state.workspacePath) return; state.workspacePath = d.path || ''; renderWorkspacePath(); $('workspace-files').replaceChildren(); if (!d.entries?.length) $('workspace-files').append(el('p', 'empty-small', '这里还没有成果文件。')); (d.entries || []).sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'directory' ? -1 : 1)).forEach(f => { const url = artifactURL(f.path); $('workspace-files').append(fileRow(f.name, f.size, f.type, () => { if (f.type === 'directory') loadWorkspace(f.path); else openArtifact(f.name, f.path); }, f.type === 'file' ? () => download(url, f.name) : null)); }); }
    catch (err) { if (epoch === state.epoch) $('workspace-files').replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', () => loadWorkspace(path), 'refresh')); }
  }
  function renderWorkspacePath() { const root = $('workspace-path'); root.replaceChildren(button('成果', '', () => loadWorkspace(''))); let path = ''; state.workspacePath.split('/').filter(Boolean).forEach(part => { path += `${path ? '/' : ''}${part}`; const target = path; root.append(el('span', '', '/'), button(part, '', () => loadWorkspace(target))); }); }
  function insightText(value) {
    if (value === undefined || value === null || value === '') return '';
    if (Array.isArray(value)) return value.map(insightText).filter(Boolean).join('；');
    if (typeof value === 'object') return insightText(value.text ?? value.label ?? value.name ?? value.value ?? value.summary ?? '');
    return String(value);
  }
  function normalizeInsightItem(item, prefix, index) {
    if (typeof item === 'string') return { id: `${prefix}-${index}`, title: item };
    const x = item && typeof item === 'object' ? item : {};
    return { ...x, id: x.id || x.key || `${prefix}-${index}`, title: x.title || x.name || x.label || `${prefix === 'experiment' ? '实验' : '图'} ${index + 1}`, claim: x.claim || x.claims || x.hypothesis || x.question, validate: x.validate || x.validation || x.validates || x.target || (prefix === 'figure' ? x.purpose : undefined), result: x.result || x.result_summary || x.results || x.outcome || x.finding || x.findings, status: x.status || x.state, metric: x.metric || x.metrics, figures: x.figures || x.figure_ids || x.plots || [], path: x.path || x.relative_path || x.file_path || x.artifact_path || x.filename };
  }
  function normalizeInsights(raw) {
    const root = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    const summary = root.summary || root.overview || root.research_summary || {};
    const experiments = root.experiments || root.experiment_list || root.runs || [];
    const figures = root.figures || root.figure_list || root.plots || [];
    return { summary, experiments: (Array.isArray(experiments) ? experiments : []).map((x, i) => normalizeInsightItem(x, 'experiment', i)), figures: (Array.isArray(figures) ? figures : []).map((x, i) => normalizeInsightItem(x, 'figure', i)) };
  }
  function insightStatus(value) { return { complete: '已完成', completed: '已完成', running: '进行中', queued: '排队中', failed: '失败', planned: '待开始', pending: '待开始' }[String(value || '').toLowerCase()] || (value ? String(value) : ''); }
  function fact(label, value, empty = '尚未记录') { const wrap = el('div', 'insight-fact'); wrap.append(el('dt', '', label), el('dd', '', insightText(value) || empty)); return wrap; }
  function insightReferences(values) {
    if (!Array.isArray(values)) return insightText(values);
    return values.map(value => typeof value === 'object' ? (value.title || value.name || value.label || value.id) : value).filter(Boolean).join('、');
  }
  function insightsVisible() { return !document.hidden && !!state.project && state.tab === 'insights' && !$('panel-insights').hidden && (!window.matchMedia('(max-width:980px)').matches || $('inspector').classList.contains('open')); }
  function imageThumbnailName(filename) { return /\.(?:png|jpe?g|webp|gif|svg)$/i.test(String(filename || '')); }
  function observeInsightImages() {
    if (!insightsVisible()) return;
    if (!state.insightObserver && typeof IntersectionObserver !== 'undefined') state.insightObserver = new IntersectionObserver(entries => {
      if (!insightsVisible()) return;
      entries.forEach(entry => { const image = entry.target; if (entry.isIntersecting && image.dataset.src) { image.src = image.dataset.src; delete image.dataset.src; state.insightObserver?.unobserve(image); } });
    }, { root: $('panel-insights'), rootMargin: '150px 0px' });
    // Without IntersectionObserver the explicit figure button still opens a preview.
    if (state.insightObserver) $('figure-list').querySelectorAll('img[data-src]').forEach(image => state.insightObserver.observe(image));
  }
  function renderInsights() {
    if (!insightsVisible()) return;
    if (state.renderedInsights === state.insights) { observeInsightImages(); return; }
    state.insightObserver?.disconnect(); state.insightObserver = null; state.renderedInsights = state.insights;
    const data = state.insights || normalizeInsights(null); const summary = $('insights-summary'); const experiments = $('experiment-list'); const figures = $('figure-list');
    summary.replaceChildren(); experiments.replaceChildren(); figures.replaceChildren();
    const allExperiments = data.experiments || []; const allFigures = data.figures || []; const overview = data.summary || {};
    const stats = el('div', 'insight-stats');
    [[overview.experiment_count ?? allExperiments.length, '实验'], [overview.figure_count ?? allFigures.length, '图'], [overview.validation_count ?? allExperiments.filter(x => ['complete', 'completed'].includes(String(x.status).toLowerCase())).length, '已验证']].forEach(([value, label]) => { const item = el('div', 'insight-stat'); item.append(el('strong', '', value || 0), el('span', '', label)); stats.append(item); });
    summary.append(stats);
    const overall = insightText(typeof overview === 'string' ? overview : overview.result || overview.result_summary || overview.overall_result || overview.finding || overview.text);
    if (overall) summary.append(el('p', 'insight-overview', overall));
    if (!allExperiments.length) experiments.append(el('p', 'empty-small', '还没有登记实验。为实验写清楚要验证的 claim 后，这里会自动汇总。'));
    else { experiments.append(el('h4', 'insight-section-label', '实验')); allExperiments.forEach((experiment, index) => {
      const card = el('article', 'insight-card'); const head = el('div', 'insight-card-head'); head.append(el('h5', '', experiment.title || `实验 ${index + 1}`)); const status = insightStatus(experiment.status); if (status) head.append(el('span', `insight-status ${String(experiment.status).toLowerCase()}`, status)); card.append(head);
      const details = el('dl', 'insight-facts'); details.append(fact('Claim', experiment.claim), fact('验证', experiment.validate), fact('结果', experiment.result)); if (experiment.metric) details.append(fact('指标', experiment.metric)); if (experiment.figures?.length) details.append(fact('相关图', insightReferences(experiment.figures))); card.append(details); experiments.append(card);
    }); }
    if (!allFigures.length) figures.append(el('p', 'empty-small', '还没有登记论文图。生成图后，在图的元数据中注明它支持的 claim。'));
    else { figures.append(el('h4', 'insight-section-label', '图')); allFigures.forEach((figure, index) => {
      const card = el('article', 'insight-card figure-card'); const head = el('div', 'insight-card-head'); head.append(el('h5', '', figure.title || `图 ${index + 1}`)); const status = insightStatus(figure.status); if (status) head.append(el('span', `insight-status ${String(figure.status).toLowerCase()}`, status)); card.append(head);
      const path = figure.path || figure.file?.path || figure.file?.relative_path; const url = figureDownloadURL(figure, path); const kind = path ? String(path).split('.').pop().toUpperCase() : String(figure.mime_type || '').split('/').pop()?.toUpperCase() || ''; if (url) { const open = button('', 'insight-figure-open', () => openFigure(figure)); if (imageThumbnailName(path || figure.title) || String(figure.mime_type || '').startsWith('image/')) { const thumb = el('img', 'insight-figure-thumb'); thumb.alt = figure.title || path; thumb.loading = 'lazy'; thumb.decoding = 'async'; thumb.width = 240; thumb.height = 130; thumb.dataset.src = `${url}${url.includes('?') ? '&' : '?'}inline=1`; open.append(thumb); } open.append(el('span', '', `打开 ${kind || '文件'}`)); card.append(open); }
      const details = el('dl', 'insight-facts'); details.append(fact('Claim', figure.claim), fact('验证', figure.validate), fact('结果', figure.result)); if (figure.caption || figure.description) details.append(fact('说明', figure.caption || figure.description)); card.append(details); figures.append(card);
    }); }
    observeInsightImages();
  }
  async function loadInsights() {
    if (!state.project || !insightsVisible()) { state.insightsNeedsRefresh = true; return; } const epoch = state.epoch;
    if (state.insightRequest?.epoch === epoch) return;
    const request = { epoch, revision: state.insightRevision || 0 }; state.insightRequest = request; state.insightsNeedsRefresh = false;
    try {
      const d = await api(projectAPI('/research')); if (epoch !== state.epoch) return;
      const payload = d.insights || d.research || d; if (payload && (payload.experiments || payload.figures || payload.summary || payload.overview)) state.insights = normalizeInsights(payload);
      renderInsights();
    } catch (err) {
      if (epoch === state.epoch) { state.insightsNeedsRefresh = true; $('insights-summary').replaceChildren(el('p', 'empty-small', err.message), button('重新加载', 'quiet-btn', loadInsights, 'refresh')); }
    } finally {
      if (state.insightRequest === request) state.insightRequest = null;
      if (epoch === state.epoch && request.revision !== (state.insightRevision || 0)) { state.insightsNeedsRefresh = true; if (insightsVisible()) { clearTimeout(state.insightTimer); state.insightTimer = setTimeout(loadInsights, 250); } }
    }
  }
  function scheduleInsights() { state.insightsNeedsRefresh = true; state.insightRevision = (state.insightRevision || 0) + 1; clearTimeout(state.insightTimer); if (insightsVisible()) state.insightTimer = setTimeout(loadInsights, 250); }
  function normalizePresence(payload) { const p = payload?.members || payload?.users || payload?.online || payload?.online_users || (Array.isArray(payload) ? payload : []); return p.map(x => typeof x === 'object' ? (x.user || x) : state.members.find(m => String(m.id) === String(x))).filter(Boolean); }
  function renderPresence() { $('presence').replaceChildren(); const online = state.presence; online.slice(0, 4).forEach(u => $('presence').append(avatar(u))); if (online.length) $('presence').append(el('span', 'presence-label', `${online.length} 人在线`)); }
  function renderMembers() { $('member-list').replaceChildren(); if (!state.members.length) $('member-list').append(el('p', 'empty-small', '尚无成员信息。')); state.members.forEach(m => { const online = state.presence.some(u => String(u.id) === String(m.id)); const row = el('div', `member-row${online ? ' online' : ''}`); const info = el('div', 'member-info'); info.append(el('strong', '', name(m)), el('small', '', online ? '在线' : `@${m.username}`)); row.append(avatar(m), info, el('span', 'member-role', roles[m.role] || m.role)); if (isOwner() && !state.project?.parent_project_id && m.role !== 'owner' && String(m.id) !== String(state.user.id)) row.append(iconButton('移除成员', 'close', () => removeMember(m))); $('member-list').append(row); }); }
  async function refreshMembers() { const epoch = state.epoch; try { const d = await api(projectAPI('/members')); if (epoch !== state.epoch) return; state.members = d.members || []; renderMembers(); } catch (err) { toast(err.message, 'error'); } }
  async function removeMember(member) { openDialog('移除项目成员', 'PROJECT MEMBERS', body => { body.append(el('p', 'form-help', `移除 ${name(member)} 后，对方将无法访问这个项目。已有讨论会保留。`)); }, async () => { await api(projectAPI(`/members/${encodeURIComponent(member.id)}`), { method: 'DELETE' }); await refreshMembers(); toast('已移除成员。'); }, '确认移除'); }
  function renderTab() { ['files', 'insights', 'activity', 'tasks', 'members'].forEach(t => { $(`tab-${t}`).classList.toggle('active', state.tab === t); $(`tab-${t}`).setAttribute('aria-selected', state.tab === t ? 'true' : 'false'); $(`panel-${t}`).hidden = !state.project || state.tab !== t; }); $('inspector-empty').hidden = !!state.project; if (state.tab === 'insights') { renderInsights(); if (state.insightsNeedsRefresh) loadInsights(); } if (state.tab === 'activity' && !state.activityLoaded) loadActivity(); if (state.tab === 'tasks') renderDelegations(); }
  function activityDescription(event) { const p = event.payload || {}; const author = p.message?.author || p.user || p.author; const who = author ? name(author) : ''; const type = event.type; if (type === 'delegation.created') return '分派了一项协作任务'; if (type === 'delegation.updated') return `协作任务${p.delegation ? delegationStatus(p.delegation) : '已更新'}`; if (type === 'message.created') return `${who || (p.message?.role === 'assistant' ? providerNames[state.project?.provider] || '助手' : '成员')} ${p.message?.role === 'assistant' ? '开始回复' : '加入了一条讨论'}`; if (type === 'message.updated') return '更新了一条讨论'; if (type === 'turn.updated') return `研究任务${labels[p.turn?.status] || '已更新'}`; if (type === 'file.created') return `上传了 ${p.file?.name || '一个文件'}`; if (type === 'member.updated') return '项目成员已更新'; if (type === 'project.updated') return '项目设置已更新'; if (type === 'agent.tool') return `${p.name || '工具'} · ${labels[p.status] || p.status || '运行中'}`; return '工作空间已更新'; }
  function renderActivity() { const list = state.activity.filter(e => !['presence', 'agent.delta', 'message.updated'].includes(e.type)).slice(-70).reverse(); $('activity-list').replaceChildren(); if (!list.length) $('activity-list').append(el('p', 'empty-small', '研究的每一步，会在这里留下记录。')); list.forEach(e => { const row = el('div', 'activity-entry'); const mark = el('span', 'activity-icon'); mark.append(icon(e.type === 'file.created' ? 'paper' : e.type === 'member.updated' ? 'users' : e.type === 'turn.updated' ? 'check' : e.type === 'agent.tool' ? 'tool' : 'notebook')); const info = el('div'); info.append(el('p', '', activityDescription(e)), el('small', '', when(e.created_at))); row.append(mark, info); $('activity-list').append(row); }); }
  async function loadActivity() { if (!state.project) return; const epoch = state.epoch; try { const d = await api(projectAPI('/activity?after=0')); if (epoch !== state.epoch) return; const merged = new Map(state.activity.map(e => [String(e.id), e])); (d.events || []).forEach(e => merged.set(String(e.id), e)); state.activity = [...merged.values()].sort((a, b) => Number(a.id) - Number(b.id)).slice(-300); state.activityLoaded = true; renderActivity(); } catch (err) { if (epoch === state.epoch) $('activity-list').replaceChildren(el('p', 'empty-small', err.message), button('重试', 'quiet-btn', loadActivity, 'refresh')); } }
  async function heartbeat(epoch) { if (epoch !== state.epoch || !state.project || document.hidden) return; const pid = state.project.id; try { await api(projectAPI('/presence', pid), { method: 'POST', body: {}, signal: AbortSignal.timeout(20000) }); } catch (err) { if (epoch === state.epoch && (err.status === 404 || err.status === 403)) await projectUnavailable(pid); } }
  function acceptEvent(event) {
    const id = Number(event.id) || 0;
    if (id && id <= state.cursor) return;
    if (id) state.cursor = id;
    applyEvent(event);
  }
  async function pollEvents(epoch) {
    if (epoch !== state.epoch || !state.project || !state.user) return;
    let delay = 5000; const pid = state.project.id; const health = connectionHealth();
    try {
      if (!document.hidden && navigator.onLine !== false) {
        const interval = health.sseOpen ? 30000 : pollRetryDelay(health.pollFailures);
        if (health.backlog || !health.lastPollAttempt || Date.now() - health.lastPollAttempt >= interval) {
          health.lastPollAttempt = Date.now();
          const result = await api(projectAPI(`/event-batch?after=${state.cursor}`), { signal: AbortSignal.timeout(20000) });
          if (epoch !== state.epoch) return;
          (result.events || []).forEach(acceptEvent); health.lastPollSuccess = Date.now(); health.pollFailures = 0; health.backlog = result.events?.length === 200;
          if (health.backlog) delay = 50;
        }
        if (Date.now() - (state.lastMetaPoll || 0) > 10000) { state.lastMetaPoll = Date.now(); refreshProjectMetaSoon(); }
      }
    } catch (err) {
      if (epoch === state.epoch && (err.status === 404 || err.status === 403)) { await projectUnavailable(pid); return; }
      if (epoch === state.epoch) { health.pollFailures++; health.backlog = false; health.lastPollAttempt = Date.now(); delay = pollRetryDelay(health.pollFailures); }
    } finally {
      if (epoch === state.epoch && state.user && state.project) { renderConnectionHealth(); state.pollTimer = setTimeout(() => pollEvents(epoch), delay); }
    }
  }
  function connectEvents(epoch) {
    if (epoch !== state.epoch || !state.project || !state.user || document.hidden || navigator.onLine === false) return;
    if (state.source) state.source.close(); const health = connectionHealth(); health.sseOpen = false;
    const source = new EventSource(`/api${projectAPI(`/events?after=${state.cursor}`)}`, { withCredentials: true }); state.source = source;
    const alive = () => { if (epoch !== state.epoch || state.source !== source) return; health.sseOpen = true; health.lastSseSuccess = Date.now(); state.reconnectDelay = 3000; renderConnectionHealth(); };
    source.onopen = alive;
    // Transport heartbeat has no event ID and must never advance the business cursor.
    source.addEventListener('heartbeat', alive);
    const handle = evt => { if (epoch !== state.epoch || state.source !== source) return; let event; try { event = JSON.parse(evt.data); } catch (_) { return; } alive(); acceptEvent({ ...event, id: event.id || evt.lastEventId }); };
    source.onmessage = handle; ['message.created', 'message.updated', 'turn.updated', 'agent.delta', 'agent.tool', 'project.updated', 'file.created', 'artifact.changed', 'insights.updated', 'research.updated', 'member.updated', 'presence', 'delegation.created', 'delegation.updated'].forEach(type => source.addEventListener(type, handle));
    source.onerror = () => {
      if (epoch !== state.epoch || !state.user || state.source !== source || document.hidden) return;
      source.close(); state.source = null; health.sseOpen = false; renderConnectionHealth(); clearTimeout(state.reconnectTimer);
      state.reconnectTimer = setTimeout(() => connectEvents(epoch), state.reconnectDelay); state.reconnectDelay = Math.min(30000, state.reconnectDelay * 2);
    };
  }
  function applyEvent(event) {
    const p = event.payload || {}; const type = event.type;
    if (type !== 'presence' && type !== 'agent.delta') { state.activity.push(event); if (state.activity.length > 300) state.activity.shift(); if (state.tab === 'activity') renderActivity(); }
    if (type === 'message.created' || type === 'message.updated') { const m = p.message || p; if (!m.id) return; const old = state.messages.get(String(m.id)); const merged = { ...old, ...m }; state.messages.set(String(m.id), merged); preserveScroll(() => renderMessage(merged)); }
    else if (type === 'agent.delta') { let m = state.messages.get(String(p.message_id)); if (!m) { m = { id: p.message_id, role: 'assistant', content: '', status: 'running', created_at: event.created_at, turn_id: p.turn_id || state.project.active_turn?.id }; } m = { ...m, content: `${m.content || ''}${p.delta || ''}`, status: 'running' }; state.messages.set(String(m.id), m); queueMessageRender(m.id); }
    else if (type === 'turn.updated') { const t = p.turn || p; updateTurn(t); preserveScroll(() => { [t.user_message_id, t.assistant_message_id].forEach(id => { const m = state.messages.get(String(id)); if (m) { m.status = t.status; if (t.error && m.role === 'assistant') m.error = t.error; renderMessage(m); } }); }); refreshProjectMetaSoon(); }
    else if (type === 'project.updated') { const project = { ...(p.project || p) }; delete project.role; delete project.parent_role; state.project = { ...state.project, ...project }; if (project.insights || project.research) state.insights = normalizeInsights(project.insights || project.research); updateProjectInList(state.project); renderHeader(); renderInsights(); }
    else if (type === 'file.created') { const f = p.file || p; if (f.id && !state.files.some(x => x.id === f.id)) state.files.unshift(f); renderFiles(); }
    else if (type === 'artifact.changed') { scheduleInsights(); loadWorkspace(state.workspacePath); }
    else if (type === 'insights.updated' || type === 'research.updated') { if (p.insights || p.research) state.insights = normalizeInsights(p.insights || p.research); scheduleInsights(); }
    else if (type === 'delegation.created' || type === 'delegation.updated') { updateDelegation(p.delegation || p); if (state.project?.parent_project_id) refreshProjectMetaSoon(); }
    else if (type === 'member.updated') { refreshProjectMetaSoon(); loadDelegations(true); if (p.members) { state.members = p.members; renderMembers(); } else refreshMembers(); }
    else if (type === 'presence') { state.presence = normalizePresence(p); renderPresence(); renderMembers(); }
    else if (type === 'agent.tool') { const id = String(p.turn_id); const list = state.tools.get(id) || []; const current = [...list].reverse().find(x => x.name === p.name && !terminalStatuses.has(x.status)); if (current && p.status !== 'running') Object.assign(current, p); else list.push(p); state.tools.set(id, list.slice(-12)); const m = [...state.messages.values()].find(x => String(x.turn_id) === id && x.role === 'assistant'); if (m) queueMessageRender(m.id); }
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
      field(body, '模型（可选）', 'new-project-model', { placeholder: '默认 gpt-5.6-sol' });
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
  function openDrawer(id) { closeDrawers(); $(id).classList.add('open'); $('drawer-scrim').hidden = false; $('drawer-scrim').classList.add('visible'); if (id === 'inspector') renderTab(); }
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
  $('project-search').addEventListener('input', renderProjects); $('composer').addEventListener('submit', sendMessage); $('message-input').addEventListener('input', () => { const t = $('message-input'); t.style.height = 'auto'; t.style.height = `${Math.min(190, t.scrollHeight)}px`; updateComposer(); updateMentions(); });
  $('message-input').addEventListener('keydown', event => { if (handleMentionKey(event)) return; if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); sendMessage(); } });
  $('message-input').addEventListener('click', updateMentions); $('message-input').addEventListener('keyup', event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) updateMentions(); });
  $('message-input').addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== $('message-input') && !$('mention-list').contains(document.activeElement)) hideMentions(); }, 120));
  $('return-parent').addEventListener('click', event => { if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); if (state.project?.parent_project_id) openProjectWindow(state.project.parent_project_id); });
  $('finish-delegation').addEventListener('click', finishDelegation); $('cancel-delegation').addEventListener('click', cancelDelegation); $('refresh-delegations').addEventListener('click', () => loadDelegations());
  $('attach-button').addEventListener('click', () => $('file-input').click()); $('file-input').addEventListener('change', e => uploadFiles(e.target.files));
  $('composer').addEventListener('dragover', e => { e.preventDefault(); if (canWrite()) $('composer').classList.add('drag-over'); }); $('composer').addEventListener('dragleave', e => { if (!$('composer').contains(e.relatedTarget)) $('composer').classList.remove('drag-over'); }); $('composer').addEventListener('drop', e => { e.preventDefault(); $('composer').classList.remove('drag-over'); if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files); });
  $('message-input').addEventListener('paste', handleComposerPaste);
  $('load-history').addEventListener('click', loadHistory); $('conversation').addEventListener('scroll', () => { if (nearBottom()) $('jump-latest').hidden = true; }); $('jump-latest').addEventListener('click', scrollBottom); $('cancel-active').addEventListener('click', () => { if (state.project?.active_turn) cancelTurn(state.project.active_turn.id); });
  $('refresh-files').addEventListener('click', refreshFiles); $('refresh-workspace').addEventListener('click', () => loadWorkspace(state.workspacePath)); $('refresh-insights').addEventListener('click', loadInsights); $('refresh-activity').addEventListener('click', loadActivity);
  document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => { state.tab = b.dataset.tab; renderTab(); if (state.tab === 'activity') renderActivity(); if (state.tab === 'tasks') loadDelegations(); }));
  $('open-sidebar').addEventListener('click', () => openDrawer('sidebar')); $('close-sidebar').addEventListener('click', closeDrawers); $('open-inspector').addEventListener('click', () => openDrawer('inspector')); $('close-inspector').addEventListener('click', closeDrawers); $('drawer-scrim').addEventListener('click', closeDrawers);
  $('close-dialog').addEventListener('click', () => { if (!state.dialogBusy) $('form-dialog').close(); }); $('dialog-cancel').addEventListener('click', () => { if (!state.dialogBusy) $('form-dialog').close(); });
  $('form-dialog').addEventListener('cancel', event => { if (state.dialogBusy) event.preventDefault(); });
  $('dialog-form').addEventListener('submit', async e => { e.preventDefault(); const action = state.dialogAction; if (!action || $('dialog-submit').disabled || state.dialogBusy || (state.dialogCanSubmit && !state.dialogCanSubmit())) return; state.dialogBusy = true; $('dialog-submit').disabled = true; $('dialog-error').hidden = true; try { const close = await action(); if (close !== false && state.dialogAction === action) $('form-dialog').close(); } catch (err) { if (state.dialogAction === action) { $('dialog-error').textContent = err.message; $('dialog-error').hidden = false; } } finally { if (state.dialogAction === action) { state.dialogBusy = false; $('dialog-submit').disabled = !!state.dialogCanSubmit && !state.dialogCanSubmit(); } } });
  $('preview-download').addEventListener('click', event => handleDownloadLink(event, $('preview-download').href, $('preview-download').download));
  $('preview-copy-image').addEventListener('click', copyPreviewImage);
  $('close-preview').addEventListener('click', () => $('preview-dialog').close()); $('preview-dialog').addEventListener('close', () => { previewGeneration++; $('preview-copy-image').hidden = true; $('pdf-preview').removeAttribute('src'); $('figure-preview').removeAttribute('src'); $('pdf-preview').hidden = true; $('figure-preview').hidden = true; });
  document.addEventListener('keydown', handlePreviewCopyShortcut);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawers(); });
  document.querySelectorAll('[data-tab]').forEach(b => { b.setAttribute('aria-controls', `panel-${b.dataset.tab}`); b.addEventListener('keydown', e => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return; e.preventDefault(); const tabs = [...document.querySelectorAll('[data-tab]')]; const index = tabs.indexOf(b); const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; tabs[next].focus(); tabs[next].click(); }); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { if (state.source) state.source.close(); state.source = null; connectionHealth().sseOpen = false; clearTimeout(state.reconnectTimer); clearInterval(state.heartbeat); state.heartbeat = null; state.insightObserver?.disconnect(); state.insightObserver = null; }
    else if (state.project) { connectionHealth().startedAt = Date.now(); renderConnectionHealth(); heartbeat(state.epoch); if (!state.source) connectEvents(state.epoch); if (!state.heartbeat) state.heartbeat = setInterval(() => heartbeat(state.epoch), 25000); refreshProjectMetaSoon(); if (state.tab === 'insights') renderTab(); }
  });
  window.matchMedia('(max-width:980px)').addEventListener('change', () => { if (insightsVisible()) renderTab(); });
  window.addEventListener('online', () => { if (state.project) { const health = connectionHealth(); health.startedAt = Date.now(); health.lastPollAttempt = 0; clearTimeout(state.reconnectTimer); connectEvents(state.epoch); renderConnectionHealth(); } }); window.addEventListener('offline', () => { if (state.source) state.source.close(); state.source = null; connectionHealth().sseOpen = false; clearTimeout(state.reconnectTimer); setConnection('reconnecting', '等待网络恢复'); }); window.addEventListener('beforeunload', () => { stopEvents(); closePendingTaskWindow(); });
  async function boot() {
    if (state.bootPromise) return state.bootPromise;
    if (!$('app').hidden || (!$('login-view').hidden && $('boot').hidden)) return;
    const epoch = state.epoch;
    const run = (async () => {
      $('boot').hidden = false; $('boot-retry').hidden = true; $('boot-retry').disabled = true;
      try {
        let session;
        for (let attempt = 1; attempt <= 2; attempt++) {
          if (epoch !== state.epoch) return;
          $('boot-status').textContent = attempt === 1 ? '正在验证登录…' : '连接较慢，正在重试登录验证（2/2）…';
          try { session = await api('/session'); break; }
          catch (err) { if (epoch !== state.epoch) return; if (attempt === 2) throw err; $('boot-status').textContent = '暂时未连接，正在准备重试…'; await new Promise(resolve => setTimeout(resolve, 1000)); }
        }
        if (epoch !== state.epoch) return;
        if (session?.authenticated) { $('boot-status').textContent = '正在加载项目…'; await startSession(session); }
        else showLogin();
      } catch (err) {
        if (epoch !== state.epoch || !$('app').hidden || $('boot').hidden) return;
        $('boot-status').textContent = `暂时无法打开工作空间。${err.message}`; $('boot-retry').hidden = false; $('boot-retry').disabled = false;
      }
    })();
    state.bootPromise = run;
    try { await run; } finally { if (state.bootPromise === run) state.bootPromise = null; }
  }
  $('boot-retry').addEventListener('click', boot);
  boot();
})();
