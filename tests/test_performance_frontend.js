'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function block(start, end) {
  const first = source.indexOf(start); const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, start); return source.slice(first, last);
}
function timers() {
  let next = 0; const pending = new Map();
  return { pending, setTimeout(fn) { pending.set(++next, fn); return next; }, clearTimeout(id) { pending.delete(id); }, run() { const [id, fn] = pending.entries().next().value; pending.delete(id); return fn(); } };
}

test('two hundred streamed updates render only the latest content once per animation frame', () => {
  const frames = []; const rendered = []; const state = { epoch: 1, messages: new Map() };
  const h = vm.createContext({ state, requestAnimationFrame: fn => { frames.push(fn); return frames.length; }, preserveScroll: fn => fn(), renderMessage: message => rendered.push(message.content) });
  vm.runInContext(block('  function queueMessageRender(', '  function renderMessages('), h);
  for (let i = 0; i < 200; i++) { state.messages.set('assistant', { content: `latest ${i}` }); h.queueMessageRender('assistant'); }
  assert.equal(frames.length, 1); assert.deepEqual(rendered, []); frames[0](); assert.deepEqual(rendered, ['latest 199']);
  h.queueMessageRender('assistant'); state.epoch++; frames[1](); assert.equal(rendered.length, 1, 'stale project callback must not draw into new project');
});

test('hidden research panel does not build any figure DOM or start thumbnails', () => {
  let built = 0; const h = vm.createContext({ insightsVisible: () => false, state: { insights: { figures: Array(500).fill({ path: 'large.pdf' }) } }, el: () => { built++; throw new Error('Hidden rendering must be skipped'); } });
  vm.runInContext(block('  function renderInsights(', '  async function loadInsights('), h); h.renderInsights(); assert.equal(built, 0);
});

test('thumbnails exclude PDFs and wait for actual panel intersection', () => {
  let callback; let visible = true; const observed = []; const released = [];
  const image = { dataset: { src: '/api/projects/p/artifact?path=large.png' } }; const offscreen = { dataset: { src: '/api/projects/p/artifact?path=later.png' } };
  const h = vm.createContext({ state: {}, insightsVisible: () => visible, $: id => id === 'figure-list' ? { querySelectorAll: () => [image, offscreen] } : {}, IntersectionObserver: class { constructor(fn) { callback = fn; } observe(node) { observed.push(node); } unobserve(node) { released.push(node); } } });
  vm.runInContext(block('  function imageThumbnailName(', '  function renderInsights('), h);
  assert.equal(h.imageThumbnailName('paper.pdf'), false); assert.equal(h.imageThumbnailName('figure.png'), true);
  h.observeInsightImages(); assert.equal(image.src, undefined); assert.equal(offscreen.src, undefined); assert.equal(observed.length, 2);
  callback([{ target: image, isIntersecting: true }, { target: offscreen, isIntersecting: false }]); assert.match(image.src, /large\.png/); assert.equal(offscreen.src, undefined); assert.equal(released.length, 1);
  visible = false; callback([{ target: offscreen, isIntersecting: true }]); assert.equal(offscreen.src, undefined);
});

function insightHarness() {
  const clock = timers(); let visible = false; let release; const requests = []; let rendered = 0;
  const state = { project: { id: 'p' }, epoch: 1, insightRevision: 0, insightsNeedsRefresh: false };
  const h = vm.createContext({ state, ...clock, insightsVisible: () => visible, projectAPI: suffix => `/projects/p${suffix}`, api: url => { requests.push(url); return new Promise(resolve => { release = resolve; }); }, normalizeInsights: value => value, renderInsights: () => rendered++, el: () => ({}), button: () => ({}), $: () => ({ replaceChildren() {} }) });
  vm.runInContext(block('  async function loadInsights(', '  function normalizePresence('), h);
  return { h, state, clock, requests, visible: value => { visible = value; }, resolve: data => release(data), rendered: () => rendered };
}

test('hidden research invalidations coalesce without any background fetch', async () => {
  const f = insightHarness(); for (let i = 0; i < 100; i++) f.h.scheduleInsights();
  assert.equal(f.requests.length, 0); assert.equal(f.clock.pending.size, 0); assert.equal(f.state.insightsNeedsRefresh, true);
  f.visible(true); const request = f.h.loadInsights(); await f.h.loadInsights(); assert.deepEqual(f.requests, ['/projects/p/research']);
  f.resolve({ insights: { figures: [], summary: {} } }); await request; assert.equal(f.rendered(), 1); assert.equal(f.state.insightRequest, null); assert.equal(f.state.insightsNeedsRefresh, false);
});

test('updates arriving during research fetch schedule one later refresh without concurrent requests', async () => {
  const f = insightHarness(); f.visible(true); const first = f.h.loadInsights();
  for (let i = 0; i < 20; i++) f.h.scheduleInsights(); assert.equal(f.clock.pending.size, 1);
  await f.clock.run(); assert.equal(f.requests.length, 1, 'inflight fetch must not be duplicated');
  f.resolve({ insights: { figures: [], summary: {} } }); await first; assert.equal(f.clock.pending.size, 1); assert.equal(f.state.insightsNeedsRefresh, true);
  const second = f.clock.run(); assert.equal(f.requests.length, 2); f.resolve({ insights: { figures: [], summary: { result: 'fresh' } } }); await second;
  assert.equal(f.clock.pending.size, 0); assert.equal(f.state.insightsNeedsRefresh, false); assert.equal(f.state.insights.summary.result, 'fresh');
});

test('child metadata polling uses the small meta endpoint and never runs concurrent metadata reads', async () => {
  const clock = timers(); const requests = []; let release;
  const state = { epoch: 1, project: { id: 'child', parent_project_id: 'parent' }, projects: [] };
  const h = vm.createContext({ state, document: { hidden: false }, ...clock, projectAPI: suffix => `/projects/child${suffix}`, api: url => { requests.push(url); return new Promise(resolve => { release = resolve; }); }, renderHeader() {}, renderMembers() {}, loadDelegations() {}, projectUnavailable() {} });
  vm.runInContext(block('  function refreshProjectMetaSoon(', '  function renderHeader('), h);
  h.refreshProjectMetaSoon(); const first = clock.run(); h.refreshProjectMetaSoon(); await clock.run(); assert.deepEqual(requests, ['/projects/child/meta']);
  release({ project: { ...state.project, role: 'editor' }, members: [] }); await first; assert.equal(clock.pending.size, 1);
  const second = clock.run(); release({ project: state.project, members: [] }); await second; assert.equal(requests.length, 2); assert.equal(state.metaRequest, null); assert.equal(clock.pending.size, 0);
});

function connectionHarness() {
  let now = 100000; let response = { events: [] }; const frames = []; const requests = []; const applied = []; const statuses = []; const clock = timers(); const delays = [];
  const state = { epoch: 1, project: { id: 'p' }, user: { id: 'u' }, cursor: 0, reconnectDelay: 3000 };
  const h = vm.createContext({ state, document: { hidden: false }, navigator: { onLine: true }, Date: { now: () => now }, setTimeout: (fn, delay) => { delays.push(delay); return clock.setTimeout(fn); }, clearTimeout: clock.clearTimeout, AbortSignal: { timeout: ms => ({ timeoutMs: ms }) }, projectAPI: suffix => `/projects/p${suffix}`, api: async (url, options) => { requests.push({ url, options }); if (response instanceof Error) throw response; return response; }, setConnection: (mode, text) => statuses.push({ mode, text }), refreshProjectMetaSoon() {}, projectUnavailable() {}, applyEvent: event => applied.push(event.id), EventSource: class { constructor(url) { this.url = url; this.listeners = {}; frames.push(this); } addEventListener(type, callback) { this.listeners[type] = callback; } close() { this.closed = true; } } });
  vm.runInContext(block('  function connectionHealth(', '  function stopEvents(') + block('  function acceptEvent(', '  function applyEvent('), h);
  return { h, state, frames, requests, applied, statuses, clock, delays, now: value => { now = value; }, response: value => { response = value; } };
}

test('SSE errors do not replace healthy polling with a reconnect warning and idle open streams stay healthy', () => {
  const f = connectionHarness(); f.h.connectEvents(1); const source = f.frames[0]; source.onopen(); f.state.connectionHealth.lastPollSuccess = 100000;
  source.onerror(); assert.equal(f.statuses.at(-1).text, '同步正常'); assert.equal(f.statuses.at(-1).mode, 'connected'); assert.equal(f.delays.at(-1), 3000);
  f.state.connectionHealth.sseOpen = true; f.now(200000); f.h.renderConnectionHealth(); assert.equal(f.statuses.at(-1).text, '实时同步', 'no business messages does not mean transport failure');
  f.h.navigator.onLine = false; f.h.renderConnectionHealth(); assert.equal(f.statuses.at(-1).text, '等待网络恢复');
});

test('only sustained failures of both channels show reconnecting; polling has twenty second timeout and backoff', async () => {
  const f = connectionHarness(); f.response(new Error('timeout')); await f.h.pollEvents(1); assert.equal(f.requests[0].options.signal.timeoutMs, 20000); assert.equal(f.statuses.at(-1).text, '正在连接'); assert.equal(f.delays.at(-1), 10000);
  f.now(131000); await f.h.pollEvents(1); assert.equal(f.statuses.at(-1).text, '正在重新连接'); assert.equal(f.delays.at(-1), 20000);
  f.now(152000); f.response({ events: [{ id: 1, type: 'message.created' }, { id: 2, type: 'message.updated' }] }); await f.h.pollEvents(1);
  assert.equal(f.statuses.at(-1).text, '同步正常'); assert.equal(f.state.connectionHealth.pollFailures, 0); assert.equal(f.state.cursor, 2); assert.deepEqual(f.applied, [1, 2]);
  f.now(158000); await f.h.pollEvents(1); assert.match(f.requests.at(-1).url, /after=2/); assert.deepEqual(f.applied, [1, 2], 'recovery replay must remain idempotent');
  assert.equal(f.h.pollRetryDelay(100), 30000);
});

test('transport heartbeat updates health without advancing event cursor or replaying business events', () => {
  const f = connectionHarness(); f.h.connectEvents(1); const first = f.frames[0]; first.onopen();
  first.listeners['message.created']({ data: JSON.stringify({ id: 7, type: 'message.created', payload: {} }) });
  f.now(115000); first.listeners.heartbeat({ data: '{}', lastEventId: '99999' }); assert.equal(f.state.cursor, 7); assert.equal(f.state.connectionHealth.lastSseSuccess, 115000); assert.deepEqual(f.applied, [7]);
  first.onerror(); f.h.connectEvents(1); const second = f.frames[1]; assert.match(second.url, /after=7/); second.onopen();
  second.listeners['message.created']({ data: JSON.stringify({ id: 7, type: 'message.created' }) }); second.listeners['message.created']({ data: JSON.stringify({ id: 8, type: 'message.created' }) }); assert.deepEqual(f.applied, [7, 8]);
});

function bootHarness(runRequest) {
  const elements = { app: { hidden: true }, 'login-view': { hidden: true }, boot: { hidden: false }, 'boot-retry': {}, 'boot-status': {} }; const requests = []; const delays = []; let starts = 0; let logins = 0;
  const state = { epoch: 1 };
  const h = vm.createContext({ state, $: id => elements[id], api: async path => { requests.push(path); return runRequest(requests.length); }, setTimeout: (fn, ms) => { delays.push(ms); queueMicrotask(fn); }, startSession: async () => { starts++; elements.app.hidden = false; elements.boot.hidden = true; state.epoch++; }, showLogin: () => { logins++; elements['login-view'].hidden = false; elements.boot.hidden = true; state.epoch++; } });
  vm.runInContext(block('  async function boot(', "  $('boot-retry').addEventListener"), h);
  return { h, state, elements, requests, delays, starts: () => starts, logins: () => logins };
}

test('boot automatically retries a failed session read once, shows stages and enters app only once', async () => {
  const f = bootHarness(attempt => { if (attempt === 1) throw new Error('temporary network failure'); return { authenticated: true }; }); await f.h.boot();
  assert.deepEqual(f.requests, ['/session', '/session']); assert.deepEqual(f.delays, [1000]); assert.equal(f.starts(), 1); assert.equal(f.elements['boot-status'].textContent, '正在加载项目…'); assert.equal(f.state.bootPromise, null);
  await f.h.boot(); assert.equal(f.requests.length, 2, 'stale retry click cannot bootstrap over an active app');
});

test('boot failure stops after bounded read retries with a usable retry action instead of an endless logo', async () => {
  const f = bootHarness(() => { throw new Error('offline'); }); await f.h.boot(); assert.equal(f.requests.length, 2); assert.equal(f.elements['boot-retry'].hidden, false); assert.equal(f.elements['boot-retry'].disabled, false); assert.match(f.elements['boot-status'].textContent, /暂时无法/); assert.equal(f.state.bootPromise, null);
});

test('concurrent boot clicks share a request and a stale session read cannot overwrite a newer view', async () => {
  let release; const f = bootHarness(() => new Promise(resolve => { release = resolve; })); const first = f.h.boot(); const duplicate = f.h.boot(); assert.equal(f.requests.length, 1); release({ authenticated: true }); await Promise.all([first, duplicate]); assert.equal(f.starts(), 1);
  let releaseStale; const stale = bootHarness(() => new Promise(resolve => { releaseStale = resolve; })); const pending = stale.h.boot(); stale.state.epoch++; stale.elements.app.hidden = false; stale.elements.boot.hidden = true; releaseStale({ authenticated: true }); await pending; assert.equal(stale.starts(), 0); assert.equal(stale.elements.app.hidden, false);
});

test('valid unauthenticated session opens login without automatic authentication attempts', async () => {
  const f = bootHarness(() => ({ authenticated: false })); await f.h.boot(); assert.deepEqual(f.requests, ['/session']); assert.equal(f.logins(), 1); assert.equal(f.starts(), 0);
});

test('missing icon bundle uses a local SVG fallback instead of crashing before boot', () => {
  const h = vm.createContext({ window: {}, NS: 'http://www.w3.org/2000/svg', document: { createElementNS: (_ns, tag) => ({ tag, attributes: {}, children: [], setAttribute(key, value) { this.attributes[key] = value; }, append(node) { this.children.push(node); } }) } });
  vm.runInContext(block('  function icon(', '  document.querySelectorAll'), h); const image = h.icon('missing'); assert.equal(image.tag, 'svg'); assert.equal(image.attributes['aria-hidden'], 'true'); assert.equal(image.children.length, 2);
});
