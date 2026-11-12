// Contract tests for the DeepSeek Harness plugin. They prove the entry points have the expected
// shape; they do not prove the plugin loads inside a real DSH host (none is available here).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import vm from 'node:vm';
import { bundledSkills, runtimeSpec, PACKAGE_ROOT, SKILL_DIR } from '../plugin/index.mjs';

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

test('without a bundled runtime the plugin reports it instead of looking for one online', () => {
  const before = process.env.AUTOSKILL_DEV_PYTHON;
  delete process.env.AUTOSKILL_DEV_PYTHON;
  try {
    const spec = runtimeSpec();
    const bundled = existsSync(join(SKILL_DIR, 'vendor', 'windows-x64', 'python', 'python.exe'));
    assert.equal(spec.python === null, !bundled);
  } finally {
    if (before !== undefined) process.env.AUTOSKILL_DEV_PYTHON = before;
  }
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

test('package manifest points at files that exist', async () => {
  const manifest = JSON.parse(await readFile(join(PACKAGE_ROOT, 'package.json'), 'utf8'));
  for (const target of [manifest.main, manifest.exports['./client'], manifest.dsh.bundle.patch]) assert.ok(existsSync(join(PACKAGE_ROOT, target)), target);
  for (const entry of manifest.files) assert.ok(existsSync(join(PACKAGE_ROOT, entry)), entry);
});
