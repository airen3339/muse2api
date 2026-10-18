// Self-contained regression for admin.html keepalive feedback.
// Extracts the actual keepaliveOne/keepaliveAll sources and runs them in a vm
// sandbox with stubbed api/toast/DOM/loadPool. No dependencies, no network.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, '..', 'admin.html'), 'utf8');

function extractFn(name) {
  const re = new RegExp('async function ' + name + '\\([^)]*\\)\\{[\\s\\S]*?\\n\\}');
  const m = html.match(re);
  if (!m) throw new Error('function ' + name + ' not found in admin.html');
  return m[0];
}

const BTN_TEXT = '⚡ 一键保活所有账号';

function makeHarness({ apiImpl } = {}) {
  const toasts = [];
  const btn = { disabled: false, innerText: BTN_TEXT };
  const sandbox = {
    console,
    $: (id) => (id === 'btnKeepaliveAll' ? btn : null),
    toast: (msg, kind) => toasts.push({ msg, kind: kind || undefined }),
    loadPool: async () => { sandbox.loadPoolCalls = (sandbox.loadPoolCalls || 0) + 1; },
    api: apiImpl || (async () => { throw new Error('api not stubbed'); }),
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(extractFn('keepaliveAll') + '\n' + extractFn('keepaliveOne'), ctx);
  const runAll = (resp) => sandbox.keepaliveAll(resp);
  return { toasts, btn, sandbox, runAll, keepaliveOne: (id) => sandbox.keepaliveOne(id) };
}

const summary = (refreshed, skippedCount = 0, extra = {}) => ({
  checked_at: 1,
  refreshed_count: refreshed.length,
  refreshed,
  skipped_count: skippedCount,
  skipped: Array.from({ length: skippedCount }, (_, i) => ({ id: 's' + i, label: 'skip' + i, reason: '会话充足且近期已保活' })),
  ...extra,
});

test('keepaliveAll all success reports green with per-account success count', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([{ ok: true, id: 'a1' }, { ok: true, id: 'a2' }]),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'ok');
  assert.match(t.msg, /2/);
  assert.equal(h.btn.disabled, false);
  assert.equal(h.btn.innerText, BTN_TEXT);
});

test('keepaliveAll mixed results report warn with succeeded and failed counts', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([{ ok: true, id: 'a1' }, { ok: false, id: 'a2', error: 'boom' }, { ok: true, id: 'a3' }]),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'warn');
  assert.match(t.msg, /成功 2/);
  assert.match(t.msg, /失败 1/);
  assert.doesNotMatch(t.msg, /成功保活 3/);
});

test('keepaliveAll all failures report bad and never claim success', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([{ ok: false, id: 'a1', error: 'x' }, { ok: false, id: 'a2', error: 'y' }]),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'bad');
  assert.doesNotMatch(t.msg, /成功保活/);
  assert.match(t.msg, /全部失败/);
  assert.match(t.msg, /共 2 个账号/);
});

test('keepaliveAll malformed refreshed entries are not counted as success', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([undefined, null, { id: 'a3' }, { ok: false, id: 'a4', error: 'e' }]),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.ok(['bad', 'warn'].includes(t.kind));
  assert.doesNotMatch(t.msg, /成功保活 4/);
  assert.doesNotMatch(t.msg, /成功 1/);
  assert.doesNotMatch(t.msg, /成功 2/);
  assert.doesNotMatch(t.msg, /成功 4/);
});

test('keepaliveAll empty refreshed with skips is not claimed as success', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([], 3),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.ok(['ok', 'warn'].includes(t.kind));
  assert.doesNotMatch(t.msg, /成功保活/);
  assert.match(t.msg, /3/);
});

test('keepaliveAll empty refreshed and empty skipped reports neutral info', async () => {
  const h = makeHarness({
    apiImpl: async () => summary([], 0),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.doesNotMatch(t.msg, /成功保活/);
  assert.doesNotMatch(t.msg, /失败/);
});

test('keepaliveAll busy response keeps warn hint and skip success path', async () => {
  const h = makeHarness({
    apiImpl: async () => ({ status: 'busy' }),
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'warn');
  assert.match(t.msg, /后台运行中/);
  assert.doesNotMatch(t.msg, /成功保活/);
});

test('keepaliveAll missing or incomplete results must not claim no work or success', async () => {
  for (const response of [{ refreshed_count: 2 }, { refreshed: [], refreshed_count: 2 }]) {
    const h = makeHarness({ apiImpl: async () => response });
    await h.runAll();
    const t = h.toasts[h.toasts.length - 1];
    assert.equal(t.kind, 'warn');
    assert.match(t.msg, /无法确认/);
    assert.doesNotMatch(t.msg, /无需保活|成功保活/);
  }
});

test('keepaliveAll busy response still restores button in finally', async () => {
  const h = makeHarness({
    apiImpl: async () => ({ status: 'busy' }),
  });
  await h.runAll();
  assert.equal(h.btn.disabled, false);
  assert.equal(h.btn.innerText, BTN_TEXT);
});

test('keepaliveAll API throw reports bad and restores button', async () => {
  const h = makeHarness({
    apiImpl: async () => { throw new Error('网络错误'); },
  });
  await h.runAll();
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'bad');
  assert.match(t.msg, /保活执行失败/);
  assert.equal(h.btn.disabled, false);
  assert.equal(h.btn.innerText, BTN_TEXT);
});

test('keepaliveOne success does not fabricate a new expiry time', async () => {
  const h = makeHarness({
    apiImpl: async () => ({ ok: true, id: 'a1' }),
  });
  await h.keepaliveOne('a1');
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'ok');
  assert.doesNotMatch(t.msg, /新到期时间/);
  assert.doesNotMatch(t.msg, /到期时间已更新/);
});

test('keepaliveOne failure stays bad with error detail', async () => {
  const h = makeHarness({
    apiImpl: async () => ({ ok: false, id: 'a1', error: '会话失效' }),
  });
  await h.keepaliveOne('a1');
  const t = h.toasts[h.toasts.length - 1];
  assert.equal(t.kind, 'bad');
  assert.match(t.msg, /保活失败/);
  assert.match(t.msg, /会话失效/);
});
