// Contract tests for the DeepSeek Harness plugin. They prove the entry points have the expected
// shape; they do not prove the plugin loads inside a real DSH host (none is available here).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { existsSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { bundledSkills, runtimeSpec, runtimeContext, findSkillDir, PACKAGE_ROOT, SKILL_DIR } from '../plugin/index.mjs';

test('registers the one autoSkill skill with its directory as resource base', async () => {
  const skills = await bundledSkills();
  assert.equal(skills.length, 1);
  assert.equal(skills[0].name, 'autoskill');
  assert.equal(skills[0].resourceBase.kind, 'directory');
  assert.equal(skills[0].resourceBase.path, SKILL_DIR);
  assert.match(skills[0].content, /autoskill record start/);
  assert.ok(skills[0].description.length > 40);
});

test('dashboard is started by argv with offline switches, never through a shell', () => {
  assert.throws(() => runtimeSpec({ port: 80 }));
  assert.throws(() => runtimeSpec({ port: 8787.5 }));
  const spec = runtimeSpec({ port: 8788, python: 'C:/py/python.exe', workspace: './ws' });
  assert.equal(spec.origin, 'http://127.0.0.1:8788');
  assert.deepEqual(spec.args, ['-m', 'autoskill', 'dashboard', '--port', '8788']);
  assert.equal(spec.cwd, SKILL_DIR);
  assert.ok(existsSync(join(spec.env.PYTHONPATH, 'autoskill', 'cli.py')));
  for (const key of ['DO_NOT_TRACK', 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD', 'PIP_NO_INDEX', 'SE_OFFLINE']) assert.ok(spec.env[key], key);
  assert.ok(spec.env.AUTOSKILL_WORKSPACE.endsWith('ws'));
});

test('registered commands bind the same custom workspace and port as the service', async () => {
  const spec = runtimeSpec({ workspace: './a workspace/资料', port: 8876 });
  const token = runtimeContext(spec);
  const binding = JSON.parse(Buffer.from(token, 'base64url').toString('utf8'));
  assert.equal(binding.workspace, spec.env.AUTOSKILL_WORKSPACE);
  assert.equal(binding.port, 8876);
  assert.equal(binding.host, 'dsh');
  const skills = await bundledSkills(SKILL_DIR, spec);
  assert.ok(skills[0].content.includes(`autoskill --runtime-context=${token} record start`));
  assert.ok(!skills[0].content.includes('autoskill record start'));
  assert.match(token, /^[a-zA-Z0-9_-]+$/);
});

test('host guidance accompanies the entry skill without rewriting a user case skill', async () => {
  const root = mkdtempSync(join(tmpdir(), 'autoskill host-guidance-'));
  try {
    const skill = join(root, 'autoskill');
    const cases = join(root, 'autoskill-cases');
    mkdirSync(skill);
    mkdirSync(cases);
    writeFileSync(join(skill, 'SKILL.md'), '---\nname: autoskill\ndescription: 测试系统\n---\n运行测试。\n');
    writeFileSync(join(cases, 'SKILL.md'), '---\nname: autoskill-cases\ndescription: 用户用例格式\n---\n保留用户模板。\n');
    const skills = await bundledSkills(skill);
    assert.equal(skills.length, 2);
    assert.match(skills[0].content, /todo_write/);
    assert.match(skills[0].content, /turn\/start/);
    assert.match(skills[0].content, /test_report.status 为 ready/);
    assert.match(skills[0].content, /present 工具/);
    assert.match(skills[1].content, /保留用户模板。/);
    assert.ok(!skills[1].content.includes('todo_write'));
    assert.equal(skills[1].resourceBase.path, cases);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('without a provisioned runtime the plugin reports it instead of looking for one online', () => {
  const before = process.env.AUTOSKILL_DEV_PYTHON;
  delete process.env.AUTOSKILL_DEV_PYTHON;
  try {
    const root = mkdtempSync(join(tmpdir(), 'autoskill-no-runtime-'));
    try { assert.equal(runtimeSpec({}, join(root, 'skills', 'autoskill')).python, null); }
    finally { rmSync(root, { recursive: true, force: true }); }
  } finally {
    if (before !== undefined) process.env.AUTOSKILL_DEV_PYTHON = before;
  }
});

test('Linux source deployment uses its local venv and keeps paths with spaces as argv', { skip: process.platform !== 'linux' }, () => {
  const root = mkdtempSync(join(tmpdir(), 'autoskill linux-'));
  try {
    mkdirSync(join(root, '.venv', 'bin'), { recursive: true });
    writeFileSync(join(root, '.venv', 'bin', 'python'), '');
    const spec = runtimeSpec({}, join(root, 'skills', 'autoskill'));
    assert.equal(spec.python, join(root, 'skills', 'autoskill', '..', '..', '.venv', 'bin', 'python'));
    assert.equal(spec.env.PYTHONPATH, join(root, 'skills', 'autoskill', 'app'));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('client registers dashboard and schedules panels pointing at the loopback dashboard', async () => {
  let registration;
  const window = { __ModuleLoader__: { load: (r) => { registration = r; } } };
  const forbidStorage = () => { throw new Error('Plugin activation must not touch user storage'); };
  Object.defineProperties(window, { localStorage: { get: forbidStorage }, sessionStorage: { get: forbidStorage } });
  const document = {};
  Object.defineProperty(document, 'cookie', { get: forbidStorage, set: forbidStorage });
  const clock = { now: 1700000000000, random: 0.5 };
  vm.runInNewContext(await readFile(new URL('../plugin/client.js', import.meta.url), 'utf8'), {
    window, document, Date: { now: () => clock.now }, Math: { random: () => clock.random },
  });
  assert.equal(registration.id, 'dsh-plugin-autoskill');
  const plugin = registration.factory((name) => { assert.equal(name, 'react'); return { createElement: (tag, props) => ({ tag, props }) }; });
  const records = [];
  const ctx = { effect: (fn) => fn(), slots: { inject: (_, fn) => fn(), register: (options, component) => { records.push({ options, component }); return () => undefined; } } };
  plugin.apply(ctx, { port: 8788 });
  assert.equal(records.length, 4);
  const panels = records.filter((r) => r.options.name === 'main');
  const urls = panels.map((panel) => new URL(panel.component().props.src));
  assert.deepEqual(urls.map((url) => url.origin), ['http://127.0.0.1:8788', 'http://127.0.0.1:8788']);
  assert.deepEqual(urls.map((url) => url.searchParams.get('embed')), ['1', '1']);
  assert.deepEqual(urls.map((url) => url.hash), ['#/dashboard', '#/schedules']);
  const loadId = urls[0].searchParams.get('activation');
  assert.ok(loadId);
  assert.equal(urls[1].searchParams.get('activation'), loadId);
  for (let render = 0; render < 3; render++) {
    clock.now += 1000;
    clock.random += 0.01;
    assert.deepEqual(panels.map((panel) => panel.component().props.src), urls.map((url) => url.href));
  }
  clock.now = 1700000000000;
  clock.random = 0.5;
  plugin.apply(ctx, { port: 8788 });
  const reactivated = records.slice(4).filter((r) => r.options.name === 'main').map((panel) => new URL(panel.component().props.src));
  assert.notEqual(reactivated[0].searchParams.get('activation'), loadId);
  assert.equal(reactivated[0].searchParams.get('activation'), reactivated[1].searchParams.get('activation'));
  assert.deepEqual(reactivated.map((url) => [url.origin, url.searchParams.get('embed'), url.hash]),
    [['http://127.0.0.1:8788', '1', '#/dashboard'], ['http://127.0.0.1:8788', '1', '#/schedules']]);
  assert.deepEqual(panels.map((panel) => panel.component().props.src), urls.map((url) => url.href));
  assert.throws(() => plugin.apply(ctx, { port: 1 }));
});

test('the skill is found in both the offline package layout and a source checkout', () => {
  const root = mkdtempSync(join(tmpdir(), 'autoskill-layout-'));
  try {
    mkdirSync(join(root, 'skills', 'autoskill'), { recursive: true });
    writeFileSync(join(root, 'skills', 'autoskill', 'SKILL.md'), 'x');
    assert.equal(findSkillDir(root), join(root, 'skills', 'autoskill'));
    mkdirSync(join(root, 'autoskill'));
    writeFileSync(join(root, 'autoskill', 'SKILL.md'), 'x');
    assert.equal(findSkillDir(root), join(root, 'autoskill'));
    const spec = runtimeSpec({}, join(root, 'autoskill'));
    assert.equal(spec.bundled, process.platform === 'linux'
      ? join(root, 'autoskill', 'vendor', `linux-${process.arch}`, 'python', 'bin', 'python3')
      : join(root, 'autoskill', 'vendor', 'windows-x64', 'python', 'python.exe'));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('Linux offline bundle takes its native Python before the source venv', { skip: process.platform !== 'linux' }, () => {
  const root = mkdtempSync(join(tmpdir(), 'autoskill native-'));
  const before = process.env.AUTOSKILL_DEV_PYTHON;
  delete process.env.AUTOSKILL_DEV_PYTHON;
  try {
    const skill = join(root, 'autoskill');
    const python = join(skill, 'vendor', `linux-${process.arch}`, 'python', 'bin', 'python3');
    mkdirSync(join(python, '..'), { recursive: true });
    writeFileSync(python, '');
    const spec = runtimeSpec({}, skill);
    assert.equal(spec.python, python);
    assert.equal(spec.env.PYTHONNOUSERSITE, '1');
  } finally {
    rmSync(root, { recursive: true, force: true });
    if (before !== undefined) process.env.AUTOSKILL_DEV_PYTHON = before;
  }
});

test('package manifest points at files that exist', async () => {
  const manifest = JSON.parse(await readFile(join(PACKAGE_ROOT, 'package.json'), 'utf8'));
  for (const target of [manifest.main, manifest.exports['./client'], manifest.dsh.bundle.patch]) assert.ok(existsSync(join(PACKAGE_ROOT, target)), target);
  for (const entry of manifest.files) assert.ok(existsSync(join(PACKAGE_ROOT, entry)), entry);
});
