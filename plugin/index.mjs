// DeepSeek Harness host entry: registers the autoSkill skill and runs its dashboard.
// The plugin adds nothing of its own; the skill directory is the product.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';

export const name = 'autoskill';
export const inject = ['skills'];
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const SKILL_DIR = join(PACKAGE_ROOT, 'skills', 'autoskill');
const VENDOR = join(SKILL_DIR, 'vendor', 'windows-x64');

export async function bundledSkills(root = PACKAGE_ROOT) {
  const path = join(root, 'skills', 'autoskill', 'SKILL.md');
  const text = await readFile(path, 'utf8');
  const split = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!split) throw new Error('Invalid bundled Skill');
  const field = (key) => {
    const match = split[1].match(new RegExp('^' + key + ':\s*(.+)$', 'm'));
    if (!match) throw new Error('Missing Skill ' + key);
    return match[1].trim();
  };
  const id = field('name');
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id)) throw new Error('Invalid Skill id');
  return [{ name: id, description: field('description'), content: split[2], source: 'bundled', path,
    resourceBase: { kind: 'directory', path: dirname(path) } }];
}

/** How to start the dashboard: bundled Python when present, never a shell, never a download. */
export function runtimeSpec(config = {}) {
  const port = config.port ?? 8787;
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('autoSkill port must be 1024-65535');
  const bundled = join(VENDOR, 'python', 'python.exe');
  const python = config.python ?? process.env.AUTOSKILL_DEV_PYTHON ?? (existsSync(bundled) ? bundled : null);
  const env = { PYTHONPATH: join(SKILL_DIR, 'app'), PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1',
    DO_NOT_TRACK: '1', PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: '1', PIP_NO_INDEX: '1', SE_OFFLINE: 'true', SE_AVOID_STATS: 'true' };
  if (python === bundled) env.PYTHONNOUSERSITE = '1';
  if (config.workspace) env.AUTOSKILL_WORKSPACE = resolve(config.workspace);
  return { port, python, env, origin: `http://127.0.0.1:${port}`, cwd: SKILL_DIR,
    args: ['-m', 'autoskill', 'dashboard', '--port', String(port)] };
}

export async function apply(ctx, config = {}) {
  for (const skill of await bundledSkills()) ctx.effect(() => ctx.skills.register(skill), 'autoskill: ' + skill.name);
  const spec = runtimeSpec(config);
  if (!spec.python) throw new Error('autoSkill: bundled runtime not found. Install the complete Windows offline package.');
  const instance = randomUUID();
  const child = spawn(spec.python, spec.args, { cwd: spec.cwd, windowsHide: true, stdio: ['ignore', 'ignore', 'ignore'],
    env: { ...process.env, ...spec.env, AUTOSKILL_INSTANCE_ID: instance } });
  let exited = false;
  child.on('error', () => { exited = true; });
  child.on('exit', () => { exited = true; });
  ctx.effect(() => () => child.kill(), 'autoskill: dashboard process');
  for (let i = 0; i < 60 && !exited; i++) {
    try {
      const response = await fetch(spec.origin + '/api/status', { signal: AbortSignal.timeout(500) });
      const body = await response.json();
      if (response.ok && body.name === 'autoSkill' && body.instance === instance) {
        ctx.logger?.info?.('autoSkill dashboard ready at ' + spec.origin);
        return;
      }
    } catch { /* not up yet */ }
    await delay(250);
  }
  child.kill();
  throw new Error('autoSkill dashboard did not start on port ' + spec.port + '; another program may be using it.');
}
