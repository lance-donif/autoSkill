// Contract tests for the DeepSeek Harness plugin. They prove the entry points have the expected
// shape; they do not prove the plugin loads inside a real DSH host (none is available here).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { existsSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { bundledSkills, runtimeSpec, findSkillDir, PACKAGE_ROOT, SKILL_DIR } from '../plugin/index.mjs';

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
  vm.runInNewContext(await readFile(new URL('../plugin/client.js', import.meta.url), 'utf8'), { window });
  assert.equal(registration.id, 'dsh-plugin-autoskill');
  const plugin = registration.factory((name) => { assert.equal(name, 'react'); return { createElement: (tag, props) => ({ tag, props }) }; });
  const records = [];
  const ctx = { effect: (fn) => fn(), slots: { inject: (_, fn) => fn(), register: (options, component) => { records.push({ options, component }); return () => {}; } } };
  plugin.apply(ctx, { port: 8788 });
  assert.equal(records.length, 4);
  const frames = records.filter((r) => r.options.name === 'main').map((r) => r.component());
  assert.deepEqual(frames.map((f) => f.props.src), ['http://127.0.0.1:8788/?embed=1#/dashboard', 'http://127.0.0.1:8788/?embed=1#/schedules']);
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
