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
  const state = { user: { id: 'me', role: 'member' }, project: { id: 'parent', role: 'editor', provider: 'codex' }, selectedAssignees: [], mentionMatches: [], mentionIndex: 0 };
  const context = vm.createContext({ URL, location: { href: 'https://shui.example/' }, state, activeStatuses: new Set(['running', 'queued', 'waiting']), isOwner: () => state.user.role === 'owner' || state.project?.role === 'owner', ...extra });
  vm.runInContext([
    block('  function canWrite(', '  function projectAPI('),
    block('  function mentionAtCursor(', '  function hideMentions('),
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
  const state = { user: { id: 'me', role: 'member' }, project: { id: 'parent', role: 'editor', provider: 'codex' }, selectedAssignees: [], attachments: [], messages: new Map(), epoch: 1, pendingSend: null, sending: false, uploading: 0, projectLoading: false };
  const context = vm.createContext({ state, $: id => elements[id], uuid: () => `request-${++nextId}`, canWrite: () => true, canAssign: () => true, projectAPI: (suffix, id) => `/projects/${id}${suffix}`, hideMentions() {}, renderAssignees() {}, updateComposer() {}, renderUploads() {}, renderMessage() {}, updateDelegation() {}, preserveScroll: fn => fn(), updateTurn: turn => parentTurns.push(turn), toast: (...values) => notices.push(values), reserveTaskWindow: () => { order.push('reserve'); if (blocked) return null; const popup = { closed: false }; windows.push(popup); return popup; }, navigateTaskWindow: (popup, parentId, childId) => { if (!popup || popup.closed) return false; opened.push({ parentId, childId }); popup.navigated = true; return true; }, closeTaskWindow: popup => { if (popup) popup.closed = true; if (state.pendingTaskWindow === popup) state.pendingTaskWindow = null; }, api: async (url, options) => {
    order.push('api'); requests.push({ url, ...options }); if (responseGate) await responseGate; if (fail) throw new Error('offline'); return { message: { id: 'm1' }, turn: { id: 'child-turn' }, ...(url.endsWith('/delegations') ? { delegation: { id: 'task', parent_project_id: 'parent', child_project_id: 'child' } } : {}) };
  } });
  vm.runInContext(block('  async function sendMessage(', '  async function cancelTurn('), context);
  return { context, state, input, elements, requests, notices, parentTurns, opened, windows, order, fail: value => { fail = value; }, blocked: value => { blocked = value; }, gate: value => { responseGate = value; } };
}

test('plain @ text stays a normal message unless member selection explicitly enabled task mode', async () => {
  const h = sendHarness(); h.input.value = '@alice investigate this'; await h.context.sendMessage();
  assert.equal(h.requests[0].url, '/projects/parent/messages'); assert.equal(h.requests[0].body.assignee_ids, undefined); assert.equal(h.opened.length, 0); assert.equal(h.parentTurns.length, 1);
});

test('multiple assignees create one child session without inserting its turn into parent state', async () => {
  const h = sendHarness(); h.input.value = 'Run the comparison'; h.state.selectedAssignees = [{ id: 'alice' }, { id: 'bob' }]; h.elements['assignment-context'].value = 'Use only these constraints'; h.state.attachments = [{ id: 'file1' }];
  await h.context.sendMessage();
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/projects/parent/delegations'); assert.deepEqual(Array.from(h.requests[0].body.assignee_ids), ['alice', 'bob']); assert.equal(h.requests[0].body.context, 'Use only these constraints'); assert.deepEqual(Array.from(h.requests[0].body.attachment_ids), ['file1']); assert.equal(h.parentTurns.length, 0); assert.equal(h.opened.length, 1); assert.equal(h.input.value, ''); assert.equal(h.state.selectedAssignees.length, 0); assert.equal(h.state.project.id, 'parent'); assert.equal(h.state.pendingTaskWindow, null); assert.equal(h.windows[0].closed, false); assert.deepEqual(h.order, ['reserve', 'api']);
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
  vm.runInContext(block('  function closeTaskWindow(', '  function lastCompleteAssistant('), h);
  return { h, calls, notices };
}

test('preopened popup severs opener synchronously before navigation and keeps parent URL', () => {
  const urls = []; const popup = { closed: false, opener: 'parent', document: { title: '', body: {} }, location: { replace(url) { assert.equal(popup.opener, null); urls.push(url); } }, focus() {}, close() { this.closed = true; } };
  const { h, calls } = windowHarness(popup); const opened = h.reserveTaskWindow('Waiting');
  assert.equal(opened, popup); assert.equal(popup.opener, null); assert.equal(popup.document.body.textContent, 'Waiting'); assert.equal(calls[0][0], 'about:blank'); assert.match(calls[0][2], /popup,width=/);
  assert.equal(h.navigateTaskWindow(opened, 'parent', 'child'), true); assert.deepEqual(urls, ['https://shui.example/?project=parent&session=child']); assert.equal(h.location.href, 'https://shui.example/');
  h.state.pendingTaskWindow = popup; h.closePendingTaskWindow(); assert.equal(popup.closed, true); assert.equal(h.state.pendingTaskWindow, null);
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
