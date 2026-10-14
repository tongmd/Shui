'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function block(start, end) {
  const first = source.indexOf(start); const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing application block ${start}`);
  return source.slice(first, last);
}
function harness(extra = {}) {
  const state = { user: { id: 'me', role: 'member' }, project: { id: 'parent', role: 'editor', provider: 'codex' }, delegations: new Map(), selectedAssignees: [], assignableMembers: [], knownMentionMembers: [], assignableLoaded: true, mentionMatches: [], mentionIndex: 0 };
  const context = vm.createContext({ URL, location: { href: 'https://shui.example/' }, state, activeStatuses: new Set(['running', 'queued', 'waiting']), isOwner: () => state.user.role === 'owner' || state.project?.role === 'owner', ...extra });
  vm.runInContext([
    block('  function canWrite(', '  function projectAPI('),
    block('  function mentionPositions(', '  function hideMentions('),
    block('  function handleMentionKey(', '  function renderAssignees('),
    block('  function lastCompleteAssistant(', '  function renderChildSession('),
    block('  function validSessionView(', '  function showWelcome('),
  ].join('\n'), context);
  return context;
}

test('member query follows a deliberate @ and supports cursor edits and Chinese names', () => {
  const h = harness();
  for (const value of ['@', '@membe', '一起做 @mem', '请找（@刘', 'one\n@two']) {
    const result = h.mentionAtCursor(value, value.length);
    assert.equal(result.start, value.lastIndexOf('@'), value);
    assert.equal(result.end, value.length);
    assert.equal(result.query, value.slice(result.start + 1).toLowerCase());
  }
  assert.equal(h.mentionAtCursor('@alice 后续文字', 3).query, 'al');
});

test('email, paths, escaped mentions and fenced or inline code never offer assignment', () => {
  const h = harness();
  for (const value of ['user@example.com', 'git@github.com', 'https://x/@foo', '\\@foo', 'foo@bar', '`@foo', '``abc @foo', '```python\n@foo', '~~~\n@foo', '    @foo', '\t@foo', '`code\n@foo', '```\nx\n``\n@foo']) {
    assert.equal(h.mentionAtCursor(value, value.length), null, value);
  }
  for (const value of ['`code` @foo', '``code ` x`` @foo', '```\ncode\n```\n@foo', '~~~\ncode\n~~~\n@foo', '\\`text @foo']) {
    assert.equal(h.mentionAtCursor(value, value.length).query, 'foo', value);
  }
});

test('mention keyboard selection is explicit and ignores IME or send shortcuts', () => {
  const selected = []; let renders = 0; let hidden = 0;
  const h = harness({ chooseAssignee: user => selected.push(user.id), renderMentions: () => renders++, hideMentions: () => hidden++ });
  h.state.mention = { start: 0, query: '' }; h.state.mentionMatches = [{ id: 'a' }, { id: 'b' }];
  const event = (key, extra = {}) => ({ key, prevented: false, preventDefault() { this.prevented = true; }, ...extra });
  const down = event('ArrowDown'); assert.equal(h.handleMentionKey(down), true); assert.equal(h.state.mentionIndex, 1); assert.equal(down.prevented, true);
  const enter = event('Enter'); h.handleMentionKey(enter); assert.deepEqual(selected, ['b']);
  h.handleMentionKey(event('ArrowDown')); assert.equal(h.state.mentionIndex, 0); h.handleMentionKey(event('Tab')); assert.deepEqual(selected, ['b', 'a']);
  for (const e of [event('Enter', { isComposing: true }), event('Enter', { ctrlKey: true }), event('Enter', { metaKey: true }), event('Enter', { shiftKey: true }), event('Tab', { shiftKey: true })]) assert.equal(h.handleMentionKey(e), false);
  assert.equal(h.handleMentionKey(event('Escape')), true); assert.equal(hidden, 1); assert.equal(renders, 2);
});

test('child uses effective write role, closure blocks global-owner bypass, and no nested assignment', () => {
  const h = harness(); assert.equal(h.canAssign(), true);
  h.state.project.provider = 'bridge'; assert.equal(h.canAssign(), false);
  h.state.project = { id: 'child', parent_project_id: 'parent', role: 'editor', delegation: { status: 'open' } };
  assert.equal(h.canWrite(), true); assert.equal(h.canAssign(), false); assert.equal(h.parentProjectId(), 'parent');
  h.state.project.role = 'viewer'; h.state.user.role = 'owner'; assert.equal(h.canWrite(), false);
  h.state.project.role = 'owner'; h.state.project.delegation.status = 'completed'; assert.equal(h.canWrite(), false);
  h.state.project.delegation.status = 'cancelled'; assert.equal(h.canWrite(), false);
});

test('shared task event cannot grant controls using another user’s can_manage value', () => {
  const h = harness(); const task = { created_by: 'other', assignees: [{ id: 'alice' }], can_manage: true };
  assert.equal(h.canManageDelegation(task), false);
  task.assignees.push({ id: 'me' }); task.can_manage = false; assert.equal(h.canManageDelegation(task), true);
  h.state.project.role = 'viewer'; assert.equal(h.canManageDelegation(task), false);
  h.state.project.role = 'owner'; assert.equal(h.canManageDelegation(task), true);
  h.state.project = { id: 'child', parent_project_id: 'parent', role: 'viewer', parent_role: 'editor' }; task.assignees = []; task.created_by = 'me'; assert.equal(h.canManageDelegation(task), true);
  h.state.project.parent_role = 'viewer'; assert.equal(h.canManageDelegation(task), false);
});

test('summary readiness requires a completed assistant answer and drains all active work', () => {
  const h = harness(); const task = { status: 'open' }; const answer = { role: 'assistant', status: 'complete', content: 'Done [result](results.pdf)' };
  assert.match(h.mergeReadiness(task, {}, []), /回复/);
  for (const status of ['queued', 'running', 'failed', 'cancelled', 'interrupted']) assert.match(h.mergeReadiness(task, {}, [{ ...answer, status }]), /回复/);
  assert.equal(h.mergeReadiness(task, {}, [answer]), '');
  for (const state of [{ active_turn: { status: 'running' } }, { active_turn: { status: 'queued' } }, { queued_count: 1 }]) {
    assert.match(h.mergeReadiness(task, state, [answer]), /排队/);
    assert.match(h.mergeReadiness({ ...task, ...state }, {}, [answer]), /排队/);
  }
  assert.match(h.mergeReadiness({ status: 'completed' }, {}, [answer]), /结束/);
  assert.equal(h.lastCompleteAssistant([answer, { role: 'user', content: 'thanks' }]), answer);
});

test('window URL takes priority over per-tab storage and ignores another window’s legacy child preference', () => {
  let saved = JSON.stringify({ parent_id: 'stored-parent', child_id: 'stored-child' });
  const h = harness({ sessionStorage: { getItem: () => saved }, stored: () => 'other-window-project' });
  assert.equal(h.preferredSessionView().child_id, 'stored-child');
  h.location.href = 'https://shui.example/?project=parent'; assert.equal(h.preferredSessionView().parent_id, 'parent'); assert.equal(h.preferredSessionView().child_id, null);
  h.location.href = 'https://shui.example/?project=parent&session=child'; assert.equal(h.preferredSessionView().child_id, 'child');
  saved = JSON.stringify({ parent_id: 'different', child_id: 'other' }); assert.equal(h.preferredSessionView().child_id, 'child');
  h.location.href = 'https://shui.example/';
  for (const invalid of ['not-json', 'null', '{"child_id":"child"}', '{"parent_id":"parent","child_id":22}']) { saved = invalid; assert.equal(h.preferredSessionView(), null); }
});

test('parent and child persist separate URLs while only parent updates legacy project preference', () => {
  const writes = []; const storage = new Map(); const location = { href: 'https://shui.example/' };
  const h = harness({ location, history: { replaceState: (_state, _title, href) => { location.href = href; } }, sessionStorage: { setItem: (key, value) => storage.set(key, value) }, stored: (...args) => writes.push(args) });
  h.rememberSessionView({ id: 'parent' }); assert.equal(location.href, 'https://shui.example/?project=parent'); assert.equal(h.preferredSessionView().child_id, null);
  h.rememberSessionView({ id: 'child', parent_project_id: 'parent' }); assert.equal(location.href, 'https://shui.example/?project=parent&session=child'); assert.equal(h.preferredSessionView().child_id, 'child');
  assert.deepEqual(writes, [['shui-project', 'parent']]); assert.equal(JSON.parse(storage.get('shui-session-view')).child_id, 'child');
});

function sendHarness() {
  const requests = []; const notices = []; const parentTurns = []; const opened = []; const windows = []; const order = []; let fail = false; let blocked = false; let nextId = 0; let responseGate = null;
  const input = { value: '', style: {}, focus() {} }; const elements = { 'message-input': input, 'assignment-context': { value: '' } };
  const state = { user: { id: 'me', role: 'member' }, project: { id: 'parent', role: 'editor', provider: 'codex' }, selectedAssignees: [], assignableMembers: ['alice', 'bob', 'memberli'].map(username => ({ id: username, username, role: 'editor' })), knownMentionMembers: [], assignableLoaded: true, attachments: [], messages: new Map(), epoch: 1, pendingSend: null, sending: false, uploading: 0, projectLoading: false };
  const context = vm.createContext({ state, $: id => elements[id], uuid: () => `request-${++nextId}`, canWrite: () => true, canAssign: () => true, projectAPI: (suffix, id) => `/projects/${id}${suffix}`, hideMentions() {}, renderAssignees() {}, updateComposer() {}, renderUploads() {}, renderMessage() {}, updateDelegation() {}, preserveScroll: fn => fn(), updateTurn: turn => parentTurns.push(turn), toast: (...values) => notices.push(values), reserveTaskWindow: () => { order.push('reserve'); if (blocked) return null; const popup = { closed: false }; windows.push(popup); return popup; }, navigateTaskWindow: (popup, parentId, childId) => { if (!popup || popup.closed) return false; opened.push({ parentId, childId }); popup.navigated = true; return true; }, closeTaskWindow: popup => { if (popup) popup.closed = true; if (state.pendingTaskWindow === popup) state.pendingTaskWindow = null; }, api: async (url, options) => {
    order.push('api'); requests.push({ url, ...options }); if (responseGate) await responseGate; if (fail) throw new Error('offline'); if (url.endsWith('/delegations')) { const assignments = options.body.assignee_ids.map((id, i) => ({ message: { id: `m${i + 1}` }, turn: { id: `turn-${id}` }, delegation: { id: `task-${id}`, parent_project_id: 'parent', child_project_id: `child-${id}`, session_assignee: { id, username: id } } })); return { ...assignments[0], assignments }; } return { message: { id: 'm1' }, turn: { id: 'parent-turn' } };
  } });
  vm.runInContext(block('  function mentionPositions(', '  function hideMentions(') + block('  function taskWindowName(', '  function memberForChild(') + block('  async function sendMessage(', '  async function cancelTurn('), context);
  return { context, state, input, elements, requests, notices, parentTurns, opened, windows, order, fail: value => { fail = value; }, blocked: value => { blocked = value; }, gate: value => { responseGate = value; } };
}

test('unknown @ text remains a normal message without a matching project member', async () => {
  const h = sendHarness(); h.input.value = '@unknown investigate this'; await h.context.sendMessage();
  assert.equal(h.requests[0].url, '/projects/parent/messages'); assert.equal(h.requests[0].body.assignee_ids, undefined); assert.equal(h.opened.length, 0); assert.equal(h.parentTurns.length, 1);
});

test('multiple assignees receive separate child sessions without inserting their turns into parent state', async () => {
  const h = sendHarness(); h.input.value = 'Run the comparison'; h.state.selectedAssignees = [{ id: 'alice' }, { id: 'bob' }]; h.elements['assignment-context'].value = 'Use only these constraints'; h.state.attachments = [{ id: 'file1' }];
  await h.context.sendMessage();
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/projects/parent/delegations'); assert.deepEqual(Array.from(h.requests[0].body.assignee_ids), ['alice', 'bob']); assert.equal(h.requests[0].body.context, 'Use only these constraints'); assert.deepEqual(Array.from(h.requests[0].body.attachment_ids), ['file1']); assert.equal(h.parentTurns.length, 0); assert.equal(h.opened.length, 2); assert.equal(h.state.messages.size, 2); assert.equal(h.input.value, ''); assert.equal(h.state.selectedAssignees.length, 0); assert.equal(h.state.project.id, 'parent'); assert.equal(h.state.pendingTaskWindow, null); assert.equal(h.windows[0].closed, false); assert.deepEqual(h.order, ['reserve', 'reserve', 'api']);
});

test('uncertain network failure keeps task draft and retries with identical client id', async () => {
  const h = sendHarness(); h.input.value = 'Investigate'; h.state.selectedAssignees = [{ id: 'alice' }]; h.fail(true);
  await h.context.sendMessage(); const id = h.requests[0].body.client_id;
  assert.equal(h.input.value, 'Investigate'); assert.equal(h.state.selectedAssignees.length, 1); assert.equal(h.state.sending, false);
  await h.context.sendMessage(); assert.equal(h.requests[1].body.client_id, id);
  h.input.value = 'Changed scope'; await h.context.sendMessage(); assert.notEqual(h.requests[2].body.client_id, id);
  h.fail(false); await h.context.sendMessage(); assert.equal(h.requests[3].body.client_id, h.requests[2].body.client_id); assert.equal(h.opened.length, 1);
});

test('task with no instructions or too many selected files is not submitted', async () => {
  const h = sendHarness(); h.state.selectedAssignees = [{ id: 'alice' }]; h.state.attachments = [{ id: 'file' }]; await h.context.sendMessage(); assert.equal(h.requests.length, 0);
  h.input.value = 'Do it'; h.state.attachments = Array.from({ length: 13 }, (_, i) => ({ id: i })); await h.context.sendMessage(); assert.equal(h.requests.length, 0); assert.match(h.notices[0][0], /12/);
});

test('shared project update keeps viewer permissions even when emitter payload carries owner role', () => {
  const h = harness({ updateProjectInList() {}, renderHeader() {}, renderInsights() {} });
  h.state.project = { id: 'child', role: 'viewer', parent_role: 'viewer', parent_project_id: 'parent', delegation: { status: 'open' } }; h.state.activity = [];
  vm.runInContext(block('  function applyEvent(', '  function field('), h);
  h.applyEvent({ type: 'project.updated', payload: { project: { id: 'child', role: 'owner', parent_role: 'owner', name: 'Updated name' } } });
  assert.equal(h.state.project.name, 'Updated name'); assert.equal(h.state.project.role, 'viewer'); assert.equal(h.state.project.parent_role, 'viewer'); assert.equal(h.canWrite(), false);
});

test('merged task displays and copies only the public summary, never worker instructions', () => {
  const h = harness(); vm.runInContext(block('  function visibleMessageContent(', '  function messageNode('), h);
  const original = 'RAW_INTERNAL_PROMPT child workspace /private/path native-session-123';
  const visible = h.visibleMessageContent({ content: original, merge_delegation: { title: 'Check reasoning', summary: 'Verified [result](/api/projects/child/artifact?path=report.pdf)' } });
  assert.equal(visible, '协作结果：Check reasoning\n\nVerified [result](/api/projects/child/artifact?path=report.pdf)');
  assert.equal(visible.includes('RAW_INTERNAL_PROMPT'), false);
  assert.equal(h.visibleMessageContent({ content: original }), original, 'ordinary message copy remains unchanged');
  assert.equal(h.visibleMessageContent({ content: original, merge_delegation: {} }), '协作结果', 'metadata never falls back to internal prompt');
});

function windowHarness(popup) {
  const calls = []; const notices = [];
  const h = harness({ window: { open: (...args) => { calls.push(args); return popup; } }, toast: (...args) => notices.push(args) });
  vm.runInContext(block('  function taskWindowName(', '  function lastCompleteAssistant('), h);
  return { h, calls, notices };
}

test('preopened popup severs opener synchronously before navigation and keeps parent URL', () => {
  const urls = []; const popup = { closed: false, opener: 'parent', document: { title: '', body: {} }, location: { href: 'about:blank', replace(url) { assert.equal(popup.opener, null); urls.push(url); this.href = url; } }, focus() {}, close() { this.closed = true; } };
  const { h, calls } = windowHarness(popup); const opened = h.reserveTaskWindow('Waiting');
  assert.equal(opened, popup); assert.equal(popup.opener, null); assert.equal(popup.document.body.textContent, 'Waiting'); assert.equal(calls[0][0], ''); assert.match(calls[0][2], /popup,width=/);
  assert.equal(h.navigateTaskWindow(opened, 'parent', 'child'), true); assert.deepEqual(urls, ['https://shui.example/?project=parent&session=child']); assert.equal(h.location.href, 'https://shui.example/');
  h.state.pendingTaskWindow = popup; h.closePendingTaskWindow(); assert.equal(popup.closed, false); assert.equal(h.state.pendingTaskWindow, null);
});

test('browser-blocked popup never replaces parent and leaves actionable notice', () => {
  const { h, notices } = windowHarness(null); assert.equal(h.openProjectWindow('parent', 'child'), false); assert.equal(h.location.href, 'https://shui.example/'); assert.match(notices[0][0], /弹窗/);
});

test('failed task creation closes its placeholder and retains retry id and draft', async () => {
  const h = sendHarness(); h.input.value = 'Investigate'; h.state.selectedAssignees = [{ id: 'alice' }]; h.fail(true);
  await h.context.sendMessage(); assert.equal(h.windows.length, 1); assert.equal(h.windows[0].closed, true); assert.equal(h.state.pendingTaskWindow, null); assert.equal(h.input.value, 'Investigate'); const client = h.requests[0].body.client_id;
  h.fail(false); await h.context.sendMessage(); assert.equal(h.requests[1].body.client_id, client); assert.equal(h.windows[1].closed, false); assert.equal(h.opened.length, 1);
});

test('navigation while create API is pending closes placeholder without opening or switching another session', async () => {
  let release; const h = sendHarness(); h.gate(new Promise(resolve => { release = resolve; })); h.input.value = 'Investigate'; h.state.selectedAssignees = [{ id: 'alice' }];
  const pending = h.context.sendMessage(); assert.equal(h.windows.length, 1); h.state.epoch++; h.state.project = { id: 'other' }; release(); await pending;
  assert.equal(h.windows[0].closed, true); assert.equal(h.opened.length, 0); assert.equal(h.state.project.id, 'other'); assert.equal(h.state.messages.size, 0);
});

test('blocked creation popup still creates task and keeps a parent task entry for reopening', async () => {
  const h = sendHarness(); h.blocked(true); h.input.value = 'Investigate'; h.state.selectedAssignees = [{ id: 'alice' }]; await h.context.sendMessage();
  assert.equal(h.requests.length, 1); assert.equal(h.state.messages.has('m1'), true); assert.equal(h.state.project.id, 'parent'); assert.equal(h.opened.length, 0); assert.match(h.notices[0][0], /任务卡/);
});

test('finishing a task preserves its child window while submitting summary to parent', async () => {
  let submit; let refreshes = 0;
  const task = { id: 'task', parent_project_id: 'parent', status: 'open' };
  const h = harness({ $: () => ({ value: 'Final summary' }), uuid: () => 'client', sortedMessages: () => [{ role: 'assistant', status: 'complete', content: 'Answer' }], openDialog: (_title, _label, _build, action) => { submit = action; }, api: async () => ({ delegation: { ...task, status: 'completed' } }), updateDelegation: result => { h.state.project.delegation = result; }, toast() {}, refreshProjectMetaSoon: () => refreshes++, projectAPI: (path, parent) => `/projects/${parent}${path}`, selectProject() { throw new Error('Must not replace child session'); } });
  h.state.project = { id: 'child', parent_project_id: 'parent', parent_role: 'owner', delegation: task }; h.state.messages = new Map();
  vm.runInContext(block('  function finishDelegation(', '  function cancelDelegation('), h);
  h.finishDelegation(); assert.equal(typeof submit, 'function'); await submit(); assert.equal(h.state.project.id, 'child'); assert.equal(h.state.project.delegation.status, 'completed'); assert.equal(refreshes, 1);
});

const mentionUsers = ['membe', 'memberli', 'alice', 'ann', 'anna'].map(username => ({ id: username, username, role: 'editor' }));
const realTaskText = '请@memberli检查示例报告，并把发现记录在项目笔记中。';

test('complete known usernames inside Chinese text derive recipients without altering any text', () => {
  const h = harness();
  for (const [text, expected] of [[realTaskText, ['memberli']], ['请@memberli，和@alice做', ['memberli', 'alice']], ['@MEMBERLI 看论文', ['memberli']], ['请@anna做；@ann 对照', ['anna', 'ann']]]) {
    const matches = h.knownMemberMentions(text, mentionUsers); assert.deepEqual(Array.from(matches, item => item.member.username), expected, text);
    matches.forEach(match => assert.equal(text.slice(match.start, match.end).toLowerCase(), `@${match.member.username}`));
  }
  assert.equal(h.mentionAtCursor('跟@membe', 7).query, 'membe');
});

test('known username recognition rejects prefixes, email, paths, URLs, code and revoked roles', () => {
  const h = harness(); const long = 'a'.repeat(40); const members = [...mentionUsers, { id: 'long', username: long, role: 'editor' }, { id: 'watcher', username: 'watcher', role: 'viewer' }, { id: 'inactive', username: 'inactive', role: 'editor', is_active: false }];
  for (const text of ['@mem', '@unknown', '@memberli2', '@memberli.work', '@memberli_name', '@memberli-name', `@${long}x`, 'user@memberli.com', 'git@memberli', 'https://x/@memberli', 'https://example.org/?user=@alice', 'https://x/#@alice', 'mailto:@alice', 'foo/@alice', 'foo/跟@alice', '/跟@alice', '\\@alice', '`跟@alice`', '``@alice``', '```python\n跟@alice\n```', '~~~\n@alice\n~~~', '    @alice', '\t@alice', '@watcher', '@inactive']) {
    assert.equal(h.knownMemberMentions(text, members).length, 0, text);
  }
  assert.equal(h.knownMemberMentions(`@${long}`, members)[0].member.id, 'long');
});

test('typing ann then anna changes derived intent without consuming the username', () => {
  const input = { value: '@ann' }; const h = harness({ $: () => input }); h.state.assignableMembers = mentionUsers;
  assert.deepEqual(Array.from(h.currentAssignment().members, user => user.id), ['ann']); assert.equal(input.value, '@ann');
  input.value = '@anna'; assert.deepEqual(Array.from(h.currentAssignment().members, user => user.id), ['anna']); assert.equal(input.value, '@anna');
  input.value = '@an'; assert.equal(h.currentAssignment().members.length, 0);
});

test('typed and explicit recipients deduplicate while the entire Chinese task is submitted unchanged', async () => {
  const h = sendHarness(); h.input.value = realTaskText; h.state.selectedAssignees = [{ id: 'memberli', username: 'memberli', role: 'editor' }];
  await h.context.sendMessage(); assert.equal(h.requests[0].url, '/projects/parent/delegations'); assert.equal(h.requests[0].body.content, realTaskText); assert.deepEqual(Array.from(h.requests[0].body.assignee_ids), ['memberli']); assert.equal(h.parentTurns.length, 0);
});

test('typed assignment visibly changes the send action before submission', () => {
  const input = { value: realTaskText }; const elements = { 'message-input': input, 'attach-button': {}, 'assignment-context': {}, 'send-message': {}, 'send-message-label': {}, 'composer-note': {} }; let rendered;
  const h = harness({ $: id => elements[id], renderAssignees: assignment => { rendered = assignment; } }); h.state.assignableMembers = mentionUsers; h.state.attachments = [];
  vm.runInContext(block('  function updateComposer(', '  function nearBottom('), h); h.updateComposer();
  assert.equal(elements['send-message-label'].textContent, '分派任务'); assert.equal(elements['send-message'].disabled, false); assert.equal(rendered.members[0].username, 'memberli'); assert.equal(input.value, realTaskText);
});

test('unloaded or failed member list blocks mention dispatch before creating a popup or normal turn', async () => {
  for (const loading of [true, false]) {
    const h = sendHarness(); h.state.assignableLoaded = false; h.state.delegationLoading = loading; h.state.assignableMembers = []; h.input.value = realTaskText;
    await h.context.sendMessage(); assert.equal(h.requests.length, 0); assert.equal(h.windows.length, 0); assert.equal(h.input.value, realTaskText); assert.match(h.notices[0][0], /成员/);
  }
  const plain = sendHarness(); plain.state.assignableLoaded = false; plain.input.value = 'user@alice.com and `@alice`'; await plain.context.sendMessage(); assert.equal(plain.requests[0].url, '/projects/parent/messages');
});

test('typed retry preserves client id across member order changes and never downgrades after revocation', async () => {
  const h = sendHarness(); h.input.value = '跟@alice和@bob检查'; h.state.knownMentionMembers = [...h.state.assignableMembers]; h.fail(true); await h.context.sendMessage(); const client = h.requests[0].body.client_id;
  h.state.assignableMembers.reverse(); await h.context.sendMessage(); assert.equal(h.requests[1].body.client_id, client);
  h.state.assignableMembers = h.state.assignableMembers.filter(member => member.id !== 'alice'); await h.context.sendMessage(); assert.equal(h.requests.length, 2); assert.match(h.notices.at(-1)[0], /无法接收/); assert.equal(h.input.value, '跟@alice和@bob检查');
  h.state.assignableMembers.push({ id: 'alice', username: 'alice', role: 'editor' }); h.fail(false); await h.context.sendMessage(); assert.equal(h.requests[2].body.client_id, client); assert.equal(h.requests[2].url, '/projects/parent/delegations');
});

test('removing a typed chip removes exactly matching @ spans and leaves surrounding Chinese intact', () => {
  const input = { value: '请@alice看一下，再跟@alice讨论。', setRangeText(text, start, end) { this.value = this.value.slice(0, start) + text + this.value.slice(end); }, focus() {} };
  const h = harness({ $: () => input, updateComposer() {}, updateMentions() {} }); h.state.assignableMembers = mentionUsers; h.state.selectedAssignees = [mentionUsers.find(user => user.id === 'alice')];
  h.removeAssignee({ id: 'alice' }); assert.equal(input.value, '请看一下，再跟讨论。'); assert.equal(h.currentAssignment().members.length, 0); assert.equal(h.state.selectedAssignees.length, 0);
});

test('nine typed recipients are rejected visibly instead of silently dropping a member', async () => {
  const h = sendHarness(); h.state.assignableMembers = Array.from({ length: 9 }, (_, i) => ({ id: `user${i}`, username: `user${i}`, role: 'editor' })); h.input.value = h.state.assignableMembers.map(user => `@${user.username}`).join(' ');
  await h.context.sendMessage(); assert.equal(h.requests.length, 0); assert.equal(h.windows.length, 0); assert.match(h.notices[0][0], /8/);
});

test('stable member window is reused without navigation or clearing its unsent discussion', () => {
  let focused = 0; let navigations = 0;
  const popup = { closed: false, opener: null, document: { title: 'Child', body: { textContent: 'UNSENT_DRAFT' } }, location: { href: 'https://shui.example/?project=parent&session=child', replace() { navigations++; } }, focus() { focused++; }, close() { this.closed = true; } };
  const { h, calls } = windowHarness(popup); const target = h.taskWindowName('parent', 'alice');
  const first = h.reserveTaskWindow('Waiting', target); assert.equal(first, popup); assert.equal(h.navigateTaskWindow(first, 'parent', 'child'), true);
  const second = h.reserveTaskWindow('Another task', target); assert.equal(second, popup); assert.equal(h.navigateTaskWindow(second, 'parent', 'child'), true);
  assert.equal(calls.length, 1); assert.equal(calls[0][0], ''); assert.equal(calls[0][1], 'shui-parent-alice'); assert.equal(navigations, 0); assert.equal(focused, 2); assert.equal(popup.document.body.textContent, 'UNSENT_DRAFT');
  h.closeTaskWindow(popup); h.closePendingTaskWindow(); assert.equal(popup.closed, false, 'failed/new request cleanup must not close a reused window');
});

test('named reuse after parent reload does not overwrite existing child before API completes', () => {
  const popup = { closed: false, opener: { old: true }, document: { title: 'Existing title', body: { textContent: 'DRAFT' } }, location: { href: 'https://shui.example/?project=parent&session=child', replace() { throw new Error('Must preserve current child'); } }, focus() {}, close() { this.closed = true; } };
  const { h, calls } = windowHarness(popup); const restored = h.reserveTaskWindow('Loading', h.taskWindowName('parent', 'alice'));
  assert.equal(restored, popup); assert.equal(popup.opener, null); assert.equal(popup.document.title, 'Existing title'); assert.equal(popup.document.body.textContent, 'DRAFT'); assert.equal(calls[0][0], ''); assert.equal(h.navigateTaskWindow(restored, 'parent', 'child'), true);
});

test('pending cleanup closes only newly created blank windows across multiple recipients', () => {
  const blanks = Array.from({ length: 2 }, () => ({ closed: false, opener: null, document: { body: {} }, location: { href: 'about:blank' }, close() { this.closed = true; } }));
  let index = 0; const h = harness({ window: { open: () => blanks[index++] } }); vm.runInContext(block('  function taskWindowName(', '  function lastCompleteAssistant('), h);
  h.reserveTaskWindow('Creating', 'alice-window'); h.reserveTaskWindow('Creating', 'bob-window'); h.closePendingTaskWindow(); assert.equal(blanks.every(popup => popup.closed), true); assert.equal(h.state.taskWindowPlaceholders.size, 0);
});

test('new requester can manage the shared member task while other project editors cannot', () => {
  const h = harness(); const task = { created_by: 'original', creator_ids: ['original', 'me'], assignees: [{ id: 'alice' }] };
  assert.equal(h.canManageDelegation(task), true); task.creator_ids = ['original']; assert.equal(h.canManageDelegation(task), false);
});

test('old completed task event cannot replace reopened task or its separate final summary', () => {
  const h = harness({ preserveScroll: fn => fn(), renderMessage() {}, renderDelegations() {}, renderChildSession() {}, updateComposer() {} });
  const old = { id: 'old', child_project_id: 'child', status: 'completed', summary: 'OLD_RESULT', created_at: '2026-10-14T01:00:00Z' }; const current = { id: 'new', child_project_id: 'child', status: 'open', created_at: '2026-10-14T02:00:00Z' };
  const summaryMessage = { id: 'summary', role: 'user', merge_delegation: { ...old } }; h.state.messages = new Map([['summary', summaryMessage]]); h.state.project = { id: 'child', parent_project_id: 'parent', delegation: current };
  vm.runInContext(block('  function updateDelegation(', '  function taskWindowName('), h);
  h.updateDelegation({ ...old, merge_turn: { status: 'complete' } }); assert.equal(h.state.project.delegation.id, 'new'); assert.equal(summaryMessage.merge_delegation.id, 'old'); assert.equal(summaryMessage.merge_delegation.summary, 'OLD_RESULT');
  h.updateDelegation({ ...current, request_count: 2 }); assert.equal(h.state.project.delegation.request_count, 2); assert.equal(summaryMessage.merge_delegation.summary, 'OLD_RESULT');
});

test('reopened member task cannot reuse a previous round assistant answer as its new summary', () => {
  const h = harness(); const task = { status: 'open', created_at: '2026-10-14T02:00:00Z' }; const old = { role: 'assistant', status: 'complete', content: 'OLD_RESULT', created_at: '2026-10-14T01:30:00Z' }; const fresh = { ...old, content: 'NEW_RESULT', created_at: '2026-10-14T02:01:00Z' };
  assert.match(h.mergeReadiness(task, {}, [old]), /本轮/); assert.equal(h.lastCompleteAssistant([old], task), undefined); assert.equal(h.mergeReadiness(task, {}, [old, fresh]), ''); assert.equal(h.lastCompleteAssistant([old, fresh], task), fresh);
});

test('nonempty closing-fence suffix remains code while sentence punctuation can end a username', () => {
  const h = harness();
  for (const text of ['```python\n```not-closing\n@alice', '~~~python\n~~~not-closing\n@alice']) assert.equal(h.knownMemberMentions(text, mentionUsers).length, 0, text);
  for (const text of ['@alice.', '请@alice。', 'Ask @alice... Then continue.', '@alice.继续']) assert.equal(h.knownMemberMentions(text, mentionUsers)[0].member.id, 'alice', text);
  for (const text of ['@alice.work', '@alice..work']) assert.equal(h.knownMemberMentions(text, mentionUsers).length, 0, text);
});

test('each child assignment shows its own folded context and source attachments without internal metadata', () => {
  const opened = [];
  const element = (tag, cls, text) => ({ tag, cls, text, children: [], append(...children) { this.children.push(...children); } });
  const h = harness({ el: element, button: (text, cls, action) => ({ ...element('button', cls, text), action }), icon: glyph => ({ glyph }), bytes: size => `${size} B`, openFile: file => opened.push(file) });
  vm.runInContext(block('  function messageAttachmentGroup(', '  function messageNode('), h);
  h.state.project = { id: 'child', parent_project_id: 'parent' };
  const file = { id: 'file-second', name: 'second-brief.pdf', size: 42, project_id: 'parent' };
  const message = { role: 'user', attachments: [file], delegation_request: { id: 'INTERNAL_REQUEST_ID', client_id: 'INTERNAL_CLIENT_ID', context: 'SECOND_REQUEST_BACKGROUND\nNew constraints', attachments: [file] } };
  const brief = h.requestBriefNode(message); assert.equal(brief.tag, 'details'); assert.equal(brief.children[0].text, '本次任务背景与附件'); assert.equal(brief.children[1].text, 'SECOND_REQUEST_BACKGROUND\nNew constraints'); assert.equal(brief.children[2].children.length, 1);
  brief.children[2].children[0].action(); assert.equal(opened[0], file); assert.equal(opened[0].project_id, 'parent'); assert.equal(JSON.stringify(brief).includes('INTERNAL_REQUEST_ID'), false); assert.equal(JSON.stringify(brief).includes('INTERNAL_CLIENT_ID'), false);
  const first = h.requestBriefNode({ ...message, attachments: [], delegation_request: { context: 'FIRST_REQUEST_BACKGROUND' } }); assert.equal(first.children[1].text, 'FIRST_REQUEST_BACKGROUND'); assert.equal(brief.children[1].text, 'SECOND_REQUEST_BACKGROUND\nNew constraints');
});

test('request brief is limited to child assignment messages and supports attachment-only requests', () => {
  const element = (tag, cls, text) => ({ tag, cls, text, children: [], append(...children) { this.children.push(...children); } });
  const h = harness({ el: element, messageAttachmentGroup: files => ({ files }) }); vm.runInContext(block('  function requestBriefNode(', '  function messageNode('), h);
  const request = { context: 'Background' }; assert.equal(h.requestBriefNode({ role: 'user', delegation_request: request }), null, 'parent card has its existing UI');
  h.state.project.parent_project_id = 'parent'; assert.equal(h.requestBriefNode({ role: 'assistant', delegation_request: request }), null); assert.equal(h.requestBriefNode({ role: 'user', content: 'ordinary followup' }), null); assert.equal(h.requestBriefNode({ role: 'user', delegation_request: {} }), null);
  const files = [{ id: 'parent-file', project_id: 'parent' }]; const brief = h.requestBriefNode({ role: 'user', delegation_request: { attachments: files } }); assert.equal(brief.children[1].files, files);
});

test('assistant summary matching prefers exact delegation identity over timestamps', () => {
  const h = harness(); const task = { id: 'current', created_at: '2026-10-14T02:00:00Z' }; const old = { role: 'assistant', status: 'complete', delegation_id: 'old', created_at: '2026-10-14T03:00:00Z', content: 'Wrong task despite later timestamp' }; const right = { ...old, delegation_id: 'current', content: 'Correct task' };
  assert.equal(h.lastCompleteAssistant([old], task), undefined); assert.equal(h.lastCompleteAssistant([old, right], task), right); assert.equal(h.lastCompleteAssistant([{ role: 'assistant', status: 'complete', content: 'No provenance' }], task), undefined);
});
