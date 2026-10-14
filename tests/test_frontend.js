'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Exercise the shipped renderer and download handler without a DOM dependency.
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function block(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing application block ${start}`);
  return source.slice(first, last);
}
const code = [
  block('  function projectAPI(', '  async function api('),
  block('  function safeHref(', '  function markdown('),
  block('  function artifactURL(', '  function figureDownloadURL('),
  block('  async function download(', '  function preparePreview('),
].join('\n');

function harness(status = 200) {
  const requests = []; const clicks = []; const notices = []; let logins = 0;
  function element(tag, cls, text) {
    return {
      tag, text, children: [], listeners: {},
      append(...nodes) { this.children.push(...nodes); },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      click() { clicks.push({ href: this.href, download: this.download }); },
      remove() { this.removed = true; },
    };
  }
  const context = vm.createContext({
    URL, encodeURIComponent, state: { project: { id: 'project-123' } },
    location: { href: 'https://shui.example/' }, el: element,
    document: { createTextNode: text => ({ text }), body: element('body') },
    fetch: async (url, options) => {
      requests.push({ url, method: options.method, credentials: options.credentials });
      if (status instanceof Error) throw status;
      return { ok: status >= 200 && status < 300, status,
        blob() { throw new Error('Files must not be buffered in memory'); } };
    },
    toast: (text, kind) => notices.push({ text, kind }),
    showLogin: () => { logins++; },
  });
  vm.runInContext(code, context);
  function render(text) { const parent = element('p'); context.inline(parent, text); return parent.children; }
  return { context, render, requests, clicks, notices, logins: () => logins };
}

test('agent file links become authenticated project downloads', () => {
  const h = harness();
  for (const [destination, expected] of [
    ['/srv/example/project/report.pdf', '/srv/example/project/report.pdf'],
    ['results/report.pptx', 'results/report.pptx'],
    ['./results/report.zip', './results/report.zip'],
    ['sandbox:/mnt/data/report.pdf', 'sandbox:/mnt/data/report.pdf'],
    ['file:///srv/example/project/report.pdf', 'file:///srv/example/project/report.pdf'],
    ['</srv/example/project/My Report.pdf>', '/srv/example/project/My Report.pdf'],
    ['<results/研究 图(修订).png>', 'results/研究 图(修订).png'],
    ['results/report(v2).pdf', 'results/report(v2).pdf'],
    ['results/report\\(v2\\).pdf', 'results/report(v2).pdf'],
    ['results/50%20done%23.pdf', 'results/50%20done%23.pdf'],
    ['results/question?answer#literal.pdf', 'results/question?answer#literal.pdf'],
  ]) {
    const links = h.render(`[Download](${destination})`).filter(node => node.tag === 'a');
    assert.equal(links.length, 1, destination);
    assert.equal(links[0].href, `/api/projects/project-123/artifact?path=${encodeURIComponent(expected)}`);
    assert.equal(links[0].download, '');
    assert.equal(typeof links[0].listeners.click, 'function');
    assert.equal(links[0].target, undefined);
  }
});

test('source line suffixes are removed only from local file destinations', () => {
  const h = harness();
  for (const suffix of [':12', ':12:4', '#L12', '#L12C4', '#L12-L15']) {
    const href = h.context.safeHref(`/project/app.py${suffix}`);
    assert.equal(new URL(href, 'https://shui.example').searchParams.get('path'), '/project/app.py');
  }
  assert.equal(h.context.safeHref('https://example.org/doc#L12'), 'https://example.org/doc#L12');
});

test('HTTP links and existing API links retain their destinations', () => {
  const h = harness();
  for (const href of ['https://example.org/report(v2).pdf', 'http://example.org/paper.pdf', '/api/projects/project-123/artifact?path=report.pdf', '/api/projects/project-123/files/file-456']) {
    const link = h.render(`[file](${href})`).find(node => node.tag === 'a');
    assert.equal(link.href, href);
    assert.equal(link.target, href.startsWith('http') ? '_blank' : undefined);
  }
  assert.equal(h.context.safeHref('/api/session'), '/api/session');
});

test('unsafe URL schemes and protocol-relative destinations are never links', () => {
  const h = harness();
  for (const href of ['javascript:alert(1)', 'data:text/html,hello', 'vbscript:msgbox(1)', '//evil.example/report.pdf', 'file://evil.example/private.pdf', 'https:evil.example', 'mailto:user@example.org']) {
    assert.equal(h.context.safeHref(href), null, href);
    const input = `[file](${href})`;
    const nodes = h.render(input);
    assert.equal(nodes.some(node => node.tag === 'a'), false, href);
    assert.equal(nodes.map(node => node.text || '').join(''), input);
  }
});

test('plain paths, code spans and emphasis retain their text', () => {
  const h = harness();
  const nodes = h.render('**Ready** `results/report.pdf` /srv/example/report.pdf');
  assert.equal(nodes.find(node => node.tag === 'strong').text, 'Ready');
  assert.equal(nodes.find(node => node.tag === 'code').text, 'results/report.pdf');
  assert.equal(nodes.some(node => node.tag === 'a'), false);
  h.context.state.project = null;
  assert.equal(h.render('[PDF](report.pdf)').some(node => node.tag === 'a'), false);
});

test('successful download checks HEAD then uses a native streaming download', async () => {
  const h = harness(); const url = '/api/projects/project-123/artifact?path=report.pdf';
  await h.context.download(url, 'report.pdf');
  assert.deepEqual(h.requests, [{ url, method: 'HEAD', credentials: 'same-origin' }]);
  assert.deepEqual(h.clicks, [{ href: url, download: 'report.pdf' }]);
  assert.deepEqual(h.notices, []);
  assert.equal(h.context.document.body.children[0].removed, true);
});

test('clicking an agent file link goes through the checked downloader', async () => {
  const h = harness(); let prevented = false;
  const link = h.render('[PDF](sandbox:/mnt/data/report.pdf)').find(node => node.tag === 'a');
  link.listeners.click({ button: 0, preventDefault() { prevented = true; } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(prevented, true);
  assert.equal(h.requests[0].method, 'HEAD');
  assert.equal(h.clicks[0].href, link.href);
});

for (const [status, message] of [[400, '文件路径无效'], [403, '没有权限'], [404, '文件不存在'], [500, '500']]) {
  test(`HTTP ${status} shows a useful error instead of saving an error response`, async () => {
    const h = harness(status);
    await h.context.download('/api/projects/project-123/artifact?path=report.pdf', 'report.pdf');
    assert.equal(h.clicks.length, 0);
    assert.equal(h.notices.length, 1);
    assert.equal(h.notices[0].kind, 'error');
    assert.ok(h.notices[0].text.includes(message));
  });
}

test('expired login opens authentication and does not download', async () => {
  const h = harness(401);
  await h.context.download('/api/projects/project-123/files/file-456');
  assert.equal(h.logins(), 1);
  assert.equal(h.clicks.length, 0);
  assert.ok(h.notices[0].text.includes('登录已过期'));
});

test('network failure reports a retryable download error', async () => {
  const h = harness(new Error('offline'));
  await h.context.download('/api/projects/project-123/artifact?path=report.pdf');
  assert.equal(h.clicks.length, 0);
  assert.ok(h.notices[0].text.includes('连接暂时中断'));
});
