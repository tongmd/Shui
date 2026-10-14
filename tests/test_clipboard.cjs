'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Run the shipped helpers, keeping backend/model calls outside this unit suite.
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function block(start, end) {
  const first = source.indexOf(start); const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing application block ${start}`);
  return source.slice(first, last);
}
const clipboardCode = [
  block('  function clipboardImageFiles(', '  function renderUploads('),
  block('  const maxClipboardImageBytes', '  function preparePreview('),
  block('  async function copyMessageText(', '  function dateValue('),
].join('\n');
function harness(extra = {}) {
  const uploaded = []; const notices = []; const requests = [];
  const input = { value: 'before old after', disabled: false, selectionStart: 7, selectionEnd: 10, events: [],
    setRangeText(text, start, end) { this.value = this.value.slice(0, start) + text + this.value.slice(end); this.selectionStart = this.selectionEnd = start + text.length; },
    dispatchEvent(event) { this.events.push(event.type); },
  };
  const elements = { 'message-input': input, 'preview-dialog': { open: false }, 'figure-preview': { hidden: true }, 'preview-copy-image': { disabled: false } };
  const context = vm.createContext({
    Blob, URL, DataView, Uint8Array, AbortController, setTimeout, clearTimeout, Event,
    $: id => elements[id], state: { projectLoading: false }, canWrite: () => true,
    uploadFiles: files => uploaded.push(...files), toast: (message, kind) => notices.push({ message, kind }),
    navigator: { clipboard: {} }, document: { activeElement: null }, window: { getSelection: () => ({ toString: () => '' }) },
    location: { href: 'https://shui.example/', origin: 'https://shui.example' },
    fetch: async (...args) => { requests.push(args); return new Response(null, { status: 404 }); },
    fallbackCopyText: () => false,
    ...extra,
  });
  vm.runInContext(clipboardCode, context);
  return { context, uploaded, notices, requests, input, elements };
}
function paste(data, extra = {}) { return { clipboardData: data, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra }; }
function imageData(files, text = '') { return { files, items: files.map(file => ({ kind: 'file', type: file.type, getAsFile: () => file })), getData: () => text }; }

test('plain text, missing clipboard data and non-image files keep native paste', () => {
  const h = harness();
  for (const data of [null, { files: [], items: [], getData: () => 'text' }, imageData([{ type: 'application/pdf' }])]) {
    const event = paste(data); h.context.handleComposerPaste(event); assert.equal(event.defaultPrevented, false);
  }
  assert.equal(h.uploaded.length, 0);
});
test('clipboard items/files overlap yields each independent image once', () => {
  const h = harness(); const first = { type: 'image/png', name: 'first.png' }; const second = { type: 'image/jpeg', name: 'second.jpg' };
  h.context.handleComposerPaste(paste(imageData([first, second])));
  assert.deepEqual(h.uploaded, [first, second]);
});
test('items-only clipboard ignores strings and null file handles', () => {
  const h = harness(); const file = { type: 'image/png' };
  const result = h.context.clipboardImageFiles({ items: [
    { kind: 'string', type: 'text/plain' }, { kind: 'file', type: 'image/png', getAsFile: () => null }, { kind: 'file', type: 'image/png', getAsFile: () => file },
  ] });
  assert.deepEqual(Array.from(result), [file]);
});
test('mixed clipboard replaces only selected text and dispatches composer input', () => {
  const h = harness(); const event = paste(imageData([{ type: 'image/png' }], '图片注释'));
  h.context.handleComposerPaste(event);
  assert.equal(event.defaultPrevented, true); assert.equal(h.input.value, 'before 图片注释 after');
  assert.equal(h.input.selectionStart, 11); assert.deepEqual(h.input.events, ['input']); assert.equal(h.uploaded.length, 1);
});
test('read-only, loading, disabled and already-handled paste cannot upload', () => {
  for (const mode of ['read-only', 'loading', 'disabled', 'handled']) {
    const h = harness(mode === 'read-only' ? { canWrite: () => false } : {});
    if (mode === 'loading') h.context.state.projectLoading = true;
    if (mode === 'disabled') h.input.disabled = true;
    const event = paste(imageData([{ type: 'image/png' }]), { defaultPrevented: mode === 'handled' });
    h.context.handleComposerPaste(event); assert.equal(h.uploaded.length, 0, mode);
  }
});
test('copy text writes original Markdown unchanged', async () => {
  let copied; const h = harness({ navigator: { clipboard: { writeText: async text => { copied = text; } } } });
  const text = '  # 标题\n\n```js\nx();\n```\n';
  assert.equal(await h.context.copyMessageText(text), true); assert.equal(copied, text); assert.equal(h.notices[0].kind, undefined);
});
test('denied text clipboard uses fallback and reports total failure honestly', async () => {
  const navigator = { clipboard: { writeText: async () => { throw new Error('denied'); } } };
  const failure = harness({ navigator }); assert.equal(await failure.context.copyMessageText('text'), false); assert.equal(failure.notices[0].kind, 'error');
  const recovered = harness({ navigator, fallbackCopyText: () => true }); assert.equal(await recovered.context.copyMessageText('text'), true); assert.equal(recovered.notices[0].kind, undefined);
});
test('image copy rejects remote and non-API URLs before fetching', async () => {
  const h = harness();
  for (const url of ['https://other.example/image.png', '/image.png', 'data:image/png;base64,AA==']) await assert.rejects(h.context.previewImageToPngBlob(url));
  assert.equal(h.requests.length, 0);
});
test('image reader rejects SVG, HTML and over-limit content length', async () => {
  const h = harness();
  for (const type of ['image/svg+xml', 'text/html']) await assert.rejects(h.context.readClipboardImageBlob(new Response('<svg/>', { headers: { 'content-type': type } })));
  await assert.rejects(h.context.readClipboardImageBlob(new Response('x', { headers: { 'content-type': 'image/png', 'content-length': String(21 * 1024 * 1024) } })), /20 MB/);
});
test('streaming image reader stops when length is missing and bytes exceed limit', async () => {
  const h = harness(); let cancelled = false; let released = false; let count = 0;
  const response = { headers: new Headers({ 'content-type': 'image/png' }), body: { getReader() { return {
    async read() { count++; return { done: false, value: new Uint8Array(11 * 1024 * 1024) }; },
    async cancel() { cancelled = true; }, releaseLock() { released = true; },
  }; } } };
  await assert.rejects(h.context.readClipboardImageBlob(response), /20 MB/); assert.equal(count, 2); assert.equal(cancelled, true); assert.equal(released, true);
});
test('PNG source preserves bytes and rejects excessive decoded dimensions', async () => {
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGO4a9wBAAOJAZk0vDxZAAAAAElFTkSuQmCC', 'base64');
  const h = harness({ fetch: async () => new Response(png, { headers: { 'content-type': 'image/png' } }) });
  const result = await h.context.previewImageToPngBlob('/api/projects/p/files/img'); assert.deepEqual(Buffer.from(await result.arrayBuffer()), png);
  for (const [width, height] of [[0, 1], [8193, 1], [5000, 5000]]) assert.throws(() => h.context.checkClipboardImageSize(width, height));
  assert.doesNotThrow(() => h.context.checkClipboardImageSize(4096, 4096));
});
test('missing image clipboard API gives visible error without false success', async () => {
  const h = harness(); h.elements['preview-dialog'].open = true; Object.assign(h.elements['figure-preview'], { hidden: false, getAttribute: () => '/api/project/image' });
  assert.equal(await h.context.copyPreviewImage(), false); assert.equal(h.notices[0].kind, 'error'); assert.equal(h.requests.length, 0);
});
